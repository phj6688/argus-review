"""Structured output and hardening flags for codex-cli."""

import json
from pathlib import Path
import subprocess

import pytest

from argus import cli_providers, client
from argus.errors import classify_error
from argus.review import REVIEW_TOOL
from provider_helpers import PROVIDER_ENV, fake_popen as fake_popen

TOOL = client.ToolSpec("result", {
    "type": "object", "properties": {"reply": {"type": "string"}, "rule_id": {"type": "string"}},
    "required": ["reply"],
}, 100)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for name in PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "subscription"
    home.mkdir()
    monkeypatch.setenv("ARGUS_CODEX_HOME", str(home))
    return home


def test_codex_exact_argv_stdin_and_strict_schema(fake_popen):
    user = 'diff --dangerously-skip-permissions "quoted"\n\'next line\''
    fake_popen.last = '{"reply": "ok", "rule_id": null}'
    assert cli_providers.get("codex-cli").complete_tool("gpt-6.1-sol", "system", user, TOOL) == {
        "reply": "ok",
    }
    call, = fake_popen.calls
    schema_file, contents = call.files["--output-schema"]
    last_file = Path(call.argv[call.argv.index("-o") + 1])
    assert json.loads(contents) == {
        "type": "object", "properties": {
            "reply": {"type": "string"}, "rule_id": {"type": ["string", "null"]},
        }, "required": ["reply", "rule_id"], "additionalProperties": False,
    }
    assert call.argv == [
        "codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral",
        "--ignore-user-config", "--ignore-rules", "-c", 'approval_policy="never"', "--color", "never",
        "--disable", "shell_tool", "--disable", "unified_exec", "--disable", "apps",
        "--disable", "plugins", "--disable", "browser_use", "--disable", "in_app_browser",
        "--disable", "computer_use", "--disable", "hooks", "--disable", "skill_search",
        "--disable", "tool_suggest", "--disable", "view_image", "--disable", "multi_agent",
        "--disable", "image_generation", "--disable", "code_mode_host", "--disable", "multi_agent_v2",
        "--disable", "goals", "--disable", "sleep_tool",
        "-c", 'web_search="disabled"', "-c", "project_doc_max_bytes=0",
        "-m", "gpt-6.1-sol", "--output-schema", str(schema_file), "-o", str(last_file), "--json", "-",
    ]
    assert isinstance(call.argv, list)
    assert user not in call.argv
    assert "--dangerously-skip-permissions" not in call.argv
    assert call.communications == [(
        "system\n\n" + user + "\n\nReturn the result as JSON matching the output schema. Do not run commands.",
        600,
    )]
    assert call.kwargs.get("shell", False) is False
    assert call.kwargs["start_new_session"] is True
    assert call.kwargs["text"] is True
    for stream in ("stdin", "stdout", "stderr"):
        assert call.kwargs[stream] == subprocess.PIPE
    cwd = Path(call.kwargs["cwd"])
    assert call.cwd_exists and call.cwd_contents == []
    assert schema_file.parent == last_file.parent != cwd
    assert schema_file.parent.parent == cwd.parent
    assert not cwd.parent.exists()


def test_codex_environment_uses_dedicated_login_and_empty_home(monkeypatch, fake_popen, clean_env):
    parent = {name: f"test-{name}" for name in (
        "HOME", "PATH", "CODEX_HOME", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_API_KEY",
        "GITHUB_TOKEN", "OPENROUTER_API_KEY", "ARGUS_X", "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CONFIG_DIR",
    )}
    for name, value in parent.items():
        monkeypatch.setenv(name, value)

    def started(call):
        env = call.kwargs["env"]
        home = Path(env["HOME"])
        assert home.is_dir() and list(home.iterdir()) == []
        assert home.parent == Path(call.kwargs["cwd"]).parent
        assert env == {"HOME": str(home), "PATH": "test-PATH", "CODEX_HOME": str(clean_env)}
        assert parent["HOME"] not in env.values()
        assert parent["CODEX_HOME"] not in env.values()

    fake_popen.on_start = started
    cli_providers.get("codex-cli").complete_tool("model", "s", "u", TOOL)
    assert not Path(fake_popen.calls[0].kwargs["env"]["HOME"]).exists()


@pytest.mark.parametrize("setting", [None, "", " \t", "missing", "file"])
def test_codex_requires_dedicated_home_before_spawning(monkeypatch, fake_popen, tmp_path, setting):
    monkeypatch.delenv("ARGUS_CODEX_HOME", raising=False)
    if setting == "file":
        path = tmp_path / "file"
        path.write_text("not a directory")
        setting = str(path)
    elif setting == "missing":
        setting = str(tmp_path / "missing")
    if setting is not None:
        monkeypatch.setenv("ARGUS_CODEX_HOME", setting)
    with pytest.raises(client.CliError, match="ARGUS_CODEX_HOME") as caught:
        cli_providers.get("codex-cli").complete_tool("model", "s", "u", TOOL)
    assert "CODEX_HOME=<dir> codex login" in str(caught.value)
    assert fake_popen.calls == []


def test_codex_review_round_trip_preserves_end_line_and_removes_rule_id(fake_popen):
    finding = {
        "file": "example.py", "line": 1, "end_line": None, "severity": "major",
        "confidence": 0.9, "title": "Missing value", "description": "The value is absent.",
        "proposed_fix": "Return the value.", "category": "input_handling", "evidence": "return None",
    }
    fake_popen.last = json.dumps({"findings": [{**finding, "rule_id": None}]})
    result = cli_providers.get("codex-cli").complete_tool("model", "s", "u", REVIEW_TOOL)
    assert result == {"findings": [finding]}


def test_codex_round_trip_restores_nested_optional_fields(fake_popen):
    tool = client.ToolSpec("result", {
        "type": "object", "properties": {"items": {"type": "array", "items": {
            "$ref": "#/$defs/entry",
        }}},
        "$defs": {"entry": {"type": "object", "properties": {
            "keep": {"type": ["integer", "null"]}, "drop": {"type": "string"},
        }, "required": ["keep"]}},
    }, 100)
    fake_popen.last = '{"items": [{"keep": null, "drop": null}]}'
    result = cli_providers.get("codex-cli").complete_tool("model", "s", "u", tool)
    assert result == {"items": [{"keep": None}]}


@pytest.mark.parametrize("last", [None, "", "garbled", "[]", "null"])
def test_missing_or_invalid_last_file_is_a_tool_mismatch(fake_popen, last):
    fake_popen.last = last
    with pytest.raises(client.ToolCallMismatch):
        cli_providers.get("codex-cli").complete_tool("model", "s", "u", TOOL)


@pytest.mark.parametrize("event_type", ["turn.failed", "error"])
@pytest.mark.parametrize("message, status", [
    ("Usage limit reached", 429), ("RATE LIMIT", 429), ("HTTP 429", 429), ("request failed", None),
])
def test_failure_events_classify_rate_limits(fake_popen, event_type, message, status):
    fake_popen.stdout = json.dumps({"type": event_type, "error": {"message": message}})
    fake_popen.last = None
    with pytest.raises(client.CliError, match=message) as caught:
        cli_providers.get("codex-cli").complete_tool("model", "s", "u", TOOL)
    assert caught.value.status_code == status
    assert classify_error(caught.value) == "gateway_error"


def test_turn_failed_rejects_even_a_written_result(fake_popen):
    fake_popen.stdout = '{"type": "turn.failed", "error": {"message": "usage limit"}}'
    with pytest.raises(client.CliError) as caught:
        cli_providers.get("codex-cli").complete_tool("model", "s", "u", TOOL)
    assert caught.value.status_code == 429


def test_bare_error_with_a_successful_result_is_recoverable(fake_popen):
    fake_popen.stdout = '\n'.join([
        "non-JSON progress", "[]", '{"type": "error", "message": "stream reconnecting"}',
        '{"type": "turn.completed"}',
    ])
    assert cli_providers.get("codex-cli").complete_tool("model", "s", "u", TOOL) == {"reply": "ok"}


def test_nonzero_exit_without_output_reports_stderr_tail(fake_popen):
    fake_popen.last = None
    fake_popen.returncode = 1
    fake_popen.stderr = "omitted-prefix" + "x" * 400 + "diagnostic-tail"
    with pytest.raises(client.CliError) as caught:
        cli_providers.get("codex-cli").complete_tool("model", "s", "u", TOOL)
    assert str(caught.value).endswith(fake_popen.stderr[-300:])
    assert "omitted-prefix" not in str(caught.value)


def test_event_message_is_bounded(fake_popen):
    fake_popen.last = None
    fake_popen.stdout = json.dumps({"type": "error", "message": "x" * 400})
    with pytest.raises(client.CliError) as caught:
        cli_providers.get("codex-cli").complete_tool("model", "s", "u", TOOL)
    assert str(caught.value) == "x" * 300


def test_rate_limit_is_not_retried_by_dispatcher(monkeypatch, fake_popen):
    monkeypatch.setenv("ARGUS_PROVIDER", "codex-cli")
    fake_popen.stdout = '{"type": "turn.failed", "error": {"message": "usage limit"}}'
    with pytest.raises(client.CliError) as caught:
        client.complete_tool("model", "s", "u", TOOL)
    assert caught.value.status_code == 429
    assert len(fake_popen.calls) == 1
