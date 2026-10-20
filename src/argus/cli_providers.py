"""Subscription CLI providers with isolated subprocesses."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import threading
from contextlib import contextmanager

from . import client, providers
from .client import CliError, ToolCallMismatch, ToolSpec
from .repo_policy import env_int

_cli_semaphore = threading.BoundedSemaphore(env_int("ARGUS_CLI_MAX_CONCURRENCY", 1))
_MODEL = re.compile(r"[A-Za-z0-9._:/~-]+")
_BINARIES = {
    providers.CLAUDE_CLI: ("ARGUS_CLAUDE_BIN", "claude"),
    providers.CODEX_CLI: ("ARGUS_CODEX_BIN", "codex"),
}
_PING_TOOL = ToolSpec(
    name="ping",
    input_schema={
        "type": "object",
        "properties": {"reply": {"type": "string"}},
        "required": ["reply"],
        "additionalProperties": False,
    },
    max_tokens=client.PING_MAX_TOKENS,
)


def _validate_model(model: str) -> None:
    if not _MODEL.fullmatch(model) or model.startswith("-"):
        raise ValueError(f"Invalid CLI model name: {model!r}")


def _binary(name: str) -> str:
    variable, default = _BINARIES[name]
    return os.environ.get(variable) or default


def _codex_home() -> Path:
    setting = os.environ.get("ARGUS_CODEX_HOME", "").strip()
    hint = "run: CODEX_HOME=<dir> codex login   then set ARGUS_CODEX_HOME=<dir>"
    if not setting:
        raise CliError(f"ARGUS_CODEX_HOME is not set; {hint}")
    home = Path(setting).resolve()
    if not home.is_dir():
        raise CliError(f"ARGUS_CODEX_HOME is not an existing directory; {hint}")
    return home


def _environment(name: str, cwd: Path) -> dict[str, str]:
    if name == providers.CODEX_CLI:
        return {
            "HOME": str(cwd.parent / "home"),
            "PATH": os.environ.get("PATH", os.defpath),
            "CODEX_HOME": str(_codex_home()),
        }
    env = {
        key: os.environ[key]
        for key in ("HOME", "PATH", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR")
        if key in os.environ
    }
    if os.environ.get("ARGUS_CLAUDE_CONFIG_DIR"):
        env["CLAUDE_CONFIG_DIR"] = os.environ["ARGUS_CLAUDE_CONFIG_DIR"]
    return env


@contextmanager
def _workspace():
    with tempfile.TemporaryDirectory(prefix="argus-cli-") as directory:
        cwd = Path(directory) / "work"
        files = Path(directory) / "files"
        cwd.mkdir()
        files.mkdir()
        (Path(directory) / "home").mkdir()
        yield cwd, files


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _run(
    name: str, argv: list[str], cwd: Path, prompt: str = "", timeout: float = 30,
) -> subprocess.CompletedProcess:
    env = _environment(name, cwd)
    # Waiting for a CLI slot must not occupy a global slot.
    with _cli_semaphore, client.llm_slot():
        try:
            proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, start_new_session=True, cwd=cwd, env=env,
            )
        except OSError as error:
            raise CliError(f"cannot start {argv[0]!r} for {name}: {error}") from error
        try:
            stdout, stderr = proc.communicate(input=prompt, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            _kill_group(proc)
            proc.communicate()
            raise TimeoutError(f"{name} timed out after {timeout}s") from error
        finally:
            # The leader can exit while a child it spawned keeps running, so the
            # group is signalled whether or not the leader is still alive.
            _kill_group(proc)
            if proc.poll() is None:
                proc.communicate()
        return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)


def _exit_error(name: str, result: subprocess.CompletedProcess) -> CliError:
    return CliError(f"{name} exited with status {result.returncode}: {result.stderr[-300:]}")


def _events(stdout: str) -> list[dict]:
    events = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _event_error(event: dict) -> CliError:
    error = event.get("error", event)
    message = str(error.get("message", error) if isinstance(error, dict) else error)
    limited = any(marker in message.lower() for marker in ("429", "rate limit", "usage limit"))
    return CliError(message[:300], status_code=429 if limited else None)


def _ping(provider, model: str) -> str:
    result = provider.complete_tool(
        model, "Return a structured reply.", "Reply with the single word: ok", _PING_TOOL,
    )
    reply = result.get("reply")
    if not isinstance(reply, str):
        raise ToolCallMismatch(f"{provider.name} did not return a reply string")
    return reply


def _doctor(provider, model: str) -> list[tuple[str, bool, str]]:
    name = provider.name
    binary = shutil.which(_binary(name), path=os.environ.get("PATH", os.defpath))
    checks = [(f"{name} binary", binary is not None, binary or f"{_binary(name)} not found")]
    if binary is None:
        return checks
    if name == providers.CODEX_CLI:
        try:
            home = _codex_home()
            found = ["AGENTS.md"] if (home / "AGENTS.md").exists() else []
            skills = home / "skills"
            if skills.is_dir():
                found.extend(f"skills/{entry.name}" for entry in sorted(skills.iterdir())
                             if entry.name != ".system")
            detail = f"warning: found {', '.join(found)} in ARGUS_CODEX_HOME" if found else str(home)
            checks.append((f"{name} home", True, detail))
        except (CliError, OSError) as error:
            checks.append((f"{name} home", False, str(error)))
            return checks
    auth_args = ["auth", "status"] if name == providers.CLAUDE_CLI else ["login", "status"]
    for check, args in (("version", ["--version"]), ("auth", auth_args)):
        try:
            with _workspace() as (cwd, _):
                result = _run(name, [binary, *args], cwd)
            if result.returncode:
                raise _exit_error(name, result)
            detail = (result.stdout.strip() or result.stderr.strip())[:300]
            if check == "auth" and name == providers.CLAUDE_CLI:
                status = json.loads(result.stdout)
                if not isinstance(status, dict) or not status.get("loggedIn"):
                    raise CliError(f"{name} is not logged in; run claude auth login")
                method = status.get("authMethod")
                detail = f"logged in via {method}"
                if method != "claude.ai":
                    detail = f"warning: authMethod is {method!r}; expected claude.ai"
            checks.append((f"{name} {check}", True, detail))
        except Exception as error:
            checks.append((f"{name} {check}", False, str(error)[:300]))
            return checks
    leak_vars = (
        ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")
        if name == providers.CLAUDE_CLI else ("OPENAI_API_KEY", "CODEX_API_KEY")
    )
    present = [key for key in leak_vars if os.environ.get(key)]
    if present:
        checks.append((
            f"{name} environment", True,
            f"warning: {', '.join(present)} set in parent; stripped from subprocess environment",
        ))
    fallback = os.environ.get("ARGUS_FALLBACK_MODEL", "").strip()
    if fallback and "/" not in fallback:
        checks.append((
            "fallback", True,
            "warning: ARGUS_FALLBACK_MODEL uses the same subscription; "
            "use a vendor/model id for an OpenRouter fallback",
        ))
    try:
        checks.append((f"{name} ping", True, provider.ping(model)))
    except Exception as error:
        checks.append((f"{name} ping", False, str(error)[:300]))
    return checks


class ClaudeCliProvider:
    name = providers.CLAUDE_CLI

    def complete_tool(self, model: str, system: str, user: str, tool: ToolSpec) -> dict:
        _validate_model(model)
        with _workspace() as (cwd, files):
            system_file = files / "system.txt"
            system_file.write_text(system, encoding="utf-8")
            # --safe-mode skips CLAUDE.md, hooks, MCP servers, and skills.
            argv = [
                _binary(self.name), "-p", "--safe-mode", "--disable-slash-commands", "--tools", "",
                "--strict-mcp-config", "--no-session-persistence", "--permission-prompts", "none",
                "--model", model, "--system-prompt-file", str(system_file),
                "--output-format", "json", "--json-schema", json.dumps(tool.input_schema),
            ]
            result = _run(
                self.name, argv, cwd, user + "\n\nReturn the result as the structured output.",
                client._env_number("ARGUS_CLI_TIMEOUT", 600, float),
            )
        try:
            data = json.loads(result.stdout)
        except ValueError as error:
            if result.returncode:
                raise _exit_error(self.name, result) from error
            raise CliError(f"{self.name} returned invalid JSON") from error
        if not isinstance(data, dict):
            raise CliError(f"{self.name} returned a non-object response")
        if data.get("is_error"):
            raise CliError(str(data.get("result", "CLI call failed")),
                           status_code=data.get("api_error_status"))
        output = data.get("structured_output")
        if not isinstance(output, dict):
            raise ToolCallMismatch(f"{self.name} did not return structured output for {tool.name}")
        return output

    def ping(self, model: str) -> str:
        return _ping(self, model)

    def doctor(self, model: str) -> list[tuple[str, bool, str]]:
        return _doctor(self, model)


class CodexCliProvider:
    name = providers.CODEX_CLI

    def complete_tool(self, model: str, system: str, user: str, tool: ToolSpec) -> dict:
        _validate_model(model)
        with _workspace() as (cwd, files):
            schema_file = files / "schema.json"
            last_file = files / "last.txt"
            schema_file.write_text(json.dumps(providers.strict_schema(tool.input_schema)),
                                   encoding="utf-8")
            argv = [
                _binary(self.name), "exec", "--sandbox", "read-only", "--skip-git-repo-check",
                "--ephemeral", "--ignore-user-config", "--ignore-rules",
                "-c", 'approval_policy="never"', "--color", "never",
                "--disable", "shell_tool", "--disable", "unified_exec", "--disable", "apps",
                "--disable", "plugins", "--disable", "browser_use", "--disable", "in_app_browser",
                "--disable", "computer_use", "--disable", "hooks", "--disable", "skill_search",
                "--disable", "tool_suggest", "--disable", "view_image", "--disable", "multi_agent",
                "--disable", "image_generation", "--disable", "code_mode_host",
                "--disable", "multi_agent_v2", "--disable", "goals", "--disable", "sleep_tool",
                "-c", 'web_search="disabled"',
                "-c", "project_doc_max_bytes=0", "-m", model,
                "--output-schema", str(schema_file), "-o", str(last_file), "--json", "-",
            ]
            result = _run(
                self.name, argv, cwd,
                system + "\n\n" + user
                + "\n\nReturn the result as JSON matching the output schema. Do not run commands.",
                client._env_number("ARGUS_CLI_TIMEOUT", 600, float),
            )
            events = _events(result.stdout)
            failed = next((e for e in events if e.get("type") == "turn.failed"), None)
            if failed is not None:
                raise _event_error(failed)
            try:
                output = json.loads(last_file.read_text(encoding="utf-8"))
                if not isinstance(output, dict):
                    raise ValueError("output is not an object")
            except (OSError, ValueError) as error:
                # A bare "error" event can be a transient stream warning, so it
                # only matters when no structured output came back.
                reported = next((e for e in events if e.get("type") == "error"), None)
                if reported is not None:
                    raise _event_error(reported) from error
                if result.returncode:
                    raise _exit_error(self.name, result) from error
                raise ToolCallMismatch(
                    f"{self.name} did not return structured output for {tool.name}"
                ) from error
        return providers.drop_nulls(output, tool.input_schema)

    def ping(self, model: str) -> str:
        return _ping(self, model)

    def doctor(self, model: str) -> list[tuple[str, bool, str]]:
        return _doctor(self, model)


def get(name: str) -> ClaudeCliProvider | CodexCliProvider:
    if name == providers.CLAUDE_CLI:
        return ClaudeCliProvider()
    if name == providers.CODEX_CLI:
        return CodexCliProvider()
    raise ValueError(f"Unknown CLI provider: {name!r}")
