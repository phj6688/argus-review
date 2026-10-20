"""Subscription command arguments, isolation, and process lifecycle."""

from contextlib import contextmanager
import json
from pathlib import Path
import signal
import subprocess
import threading

import pytest

from argus import cli_providers, client
from argus.errors import classify_error
from provider_helpers import PROVIDER_ENV, fake_popen as fake_popen

TOOL = client.ToolSpec("result", {
    "type": "object", "properties": {"reply": {"type": "string"}}, "required": ["reply"],
}, 100)
ATTACK = 'Review this diff: --dangerously-skip-permissions "quoted"\n\'next line\''


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for name in PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "subscription"
    home.mkdir()
    monkeypatch.setenv("ARGUS_CODEX_HOME", str(home))


def test_claude_exact_argv_stdin_and_empty_workspace(fake_popen):
    result = cli_providers.get("claude-cli").complete_tool("claude-sonnet-5-5", "system", ATTACK, TOOL)
    assert result == {"reply": "ok"}
    call, = fake_popen.calls
    system_file, contents = call.files["--system-prompt-file"]
    assert contents == "system"
    assert call.argv == [
        "claude", "-p", "--safe-mode", "--disable-slash-commands", "--tools", "", "--strict-mcp-config",
        "--no-session-persistence", "--permission-prompts", "none", "--model", "claude-sonnet-5-5",
        "--system-prompt-file", str(system_file), "--output-format", "json", "--json-schema",
        json.dumps(TOOL.input_schema),
    ]
    assert isinstance(call.argv, list)
    assert ATTACK not in call.argv
    assert "--dangerously-skip-permissions" not in call.argv
    assert call.communications == [(ATTACK + "\n\nReturn the result as the structured output.", 600)]
    assert call.kwargs.get("shell", False) is False
    assert call.kwargs["start_new_session"] is True
    assert call.kwargs["text"] is True
    for stream in ("stdin", "stdout", "stderr"):
        assert call.kwargs[stream] == subprocess.PIPE
    cwd = Path(call.kwargs["cwd"])
    assert call.cwd_exists and call.cwd_contents == []
    assert system_file.parent != cwd
    assert system_file.parent.parent == cwd.parent
    assert not cwd.parent.exists()


@pytest.mark.parametrize("config", [None, "/dedicated/config"])
def test_child_environment_is_an_allowlist(monkeypatch, fake_popen, config):
    parent = {name: f"test-{name}" for name in (
        "HOME", "PATH", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
        "OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_API_KEY", "GITHUB_TOKEN", "OPENROUTER_API_KEY",
        "ARGUS_X", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
    )}
    for name, value in parent.items():
        monkeypatch.setenv(name, value)
    if config is not None:
        monkeypatch.setenv("ARGUS_CLAUDE_CONFIG_DIR", config)
    cli_providers.get("claude-cli").complete_tool("model", "system", "user", TOOL)
    assert fake_popen.calls[0].kwargs["env"] == {
        "HOME": parent["HOME"], "PATH": parent["PATH"],
        "CLAUDE_CODE_OAUTH_TOKEN": parent["CLAUDE_CODE_OAUTH_TOKEN"],
        "CLAUDE_CONFIG_DIR": config or parent["CLAUDE_CONFIG_DIR"],
    }


@pytest.mark.parametrize("provider", ["claude-cli", "codex-cli"])
@pytest.mark.parametrize("model", ["--model", "has space", "model;touch", "model\nflag", ""])
def test_model_validation_happens_before_launch(fake_popen, provider, model):
    with pytest.raises(ValueError, match="model"):
        cli_providers.get(provider).complete_tool(model, "s", "u", TOOL)
    assert fake_popen.calls == []


@pytest.mark.parametrize("provider, variable", [
    ("claude-cli", "ARGUS_CLAUDE_BIN"), ("codex-cli", "ARGUS_CODEX_BIN"),
])
def test_configured_binary_is_one_argv_element(monkeypatch, fake_popen, provider, variable):
    monkeypatch.setenv(variable, "/custom path/review-cli")
    cli_providers.get(provider).complete_tool("model", "s", "u", TOOL)
    assert fake_popen.calls[0].argv[0] == "/custom path/review-cli"


@pytest.mark.parametrize("stdout, error", [
    ('{"is_error": false}', client.ToolCallMismatch),
    ('{"structured_output": null}', client.ToolCallMismatch),
    ('{"structured_output": []}', client.ToolCallMismatch),
    ('[]', client.CliError),
    ('not JSON', client.CliError),
])
def test_claude_rejects_missing_or_invalid_output(fake_popen, stdout, error):
    fake_popen.stdout = stdout
    with pytest.raises(error):
        cli_providers.get("claude-cli").complete_tool("model", "s", "u", TOOL)


def test_claude_nonzero_exit_reports_stderr(fake_popen):
    fake_popen.stdout = "not JSON"
    fake_popen.stderr = "failure details"
    fake_popen.returncode = 1
    with pytest.raises(client.CliError, match="failure details"):
        cli_providers.get("claude-cli").complete_tool("model", "s", "u", TOOL)


def test_claude_rate_limit_is_not_retried_by_dispatcher(monkeypatch, fake_popen):
    monkeypatch.setenv("ARGUS_PROVIDER", "claude-cli")
    fake_popen.stdout = json.dumps({"is_error": True, "api_error_status": 429, "result": "usage limit"})
    with pytest.raises(client.CliError, match="usage limit") as caught:
        client.complete_tool("model", "s", "u", TOOL)
    assert caught.value.status_code == 429
    assert classify_error(caught.value) == "gateway_error"
    assert len(fake_popen.calls) == 1


@pytest.mark.parametrize("provider", ["claude-cli", "codex-cli"])
def test_timeout_kills_process_group_and_reaps(monkeypatch, fake_popen, provider):
    monkeypatch.setenv("ARGUS_CLI_TIMEOUT", "0.25")

    def timeout_once(call):
        if len(call.communications) == 1:
            raise subprocess.TimeoutExpired(call.argv, 0.25)

    fake_popen.on_communicate = timeout_once
    with pytest.raises(TimeoutError, match="timed out after 0.25s") as caught:
        cli_providers.get(provider).complete_tool("model", "s", "u", TOOL)
    call, = fake_popen.calls
    assert (call.pid, signal.SIGKILL) in fake_popen.kills
    assert len(call.communications) == 2
    assert call.communications[1] == (None, None)
    assert classify_error(caught.value) == "timeout"
    assert not Path(call.kwargs["cwd"]).exists()


@pytest.mark.parametrize("provider", ["claude-cli", "codex-cli"])
def test_interruption_kills_and_reaps(monkeypatch, fake_popen, provider):
    def interrupt_once(call):
        if len(call.communications) == 1:
            raise KeyboardInterrupt

    fake_popen.on_communicate = interrupt_once
    with pytest.raises(KeyboardInterrupt):
        cli_providers.get(provider).complete_tool("model", "s", "u", TOOL)
    call, = fake_popen.calls
    assert (call.pid, signal.SIGKILL) in fake_popen.kills
    assert len(call.communications) == 2
    assert not Path(call.kwargs["cwd"]).exists()


@pytest.mark.parametrize("provider", ["claude-cli", "codex-cli"])
def test_timeout_tolerates_an_already_exited_process_group(monkeypatch, fake_popen, provider):
    def gone(*args):
        raise ProcessLookupError

    def timeout_once(call):
        if len(call.communications) == 1:
            raise subprocess.TimeoutExpired(call.argv, 600)

    fake_popen.on_communicate = timeout_once
    monkeypatch.setattr(cli_providers.os, "killpg", gone)
    with pytest.raises(TimeoutError):
        cli_providers.get(provider).complete_tool("model", "s", "u", TOOL)
    assert len(fake_popen.calls[0].communications) == 2


@pytest.mark.parametrize("timeout, expected", [("garbage", 600), ("0", 600), ("-1", 600), ("1.5", 1.5)])
def test_cli_timeout_configuration(monkeypatch, fake_popen, timeout, expected):
    monkeypatch.setenv("ARGUS_CLI_TIMEOUT", timeout)
    cli_providers.get("claude-cli").complete_tool("model", "s", "u", TOOL)
    assert fake_popen.calls[0].communications[0][1] == expected


@pytest.mark.parametrize("provider", ["claude-cli", "codex-cli"])
def test_missing_binary_is_a_cli_error(monkeypatch, provider):
    def missing(*args, **kwargs):
        raise FileNotFoundError("binary missing")

    monkeypatch.setattr(cli_providers.subprocess, "Popen", missing)
    with pytest.raises(client.CliError, match="binary missing"):
        cli_providers.get(provider).complete_tool("model", "s", "u", TOOL)


@pytest.mark.parametrize("provider", ["claude-cli", "codex-cli"])
def test_cli_slot_is_acquired_before_global_slot(monkeypatch, fake_popen, provider):
    order = []

    @contextmanager
    def slot(name):
        order.append(f"enter {name}")
        try:
            yield
        finally:
            order.append(f"exit {name}")

    monkeypatch.setattr(cli_providers, "_cli_semaphore", slot("cli"))
    monkeypatch.setattr(client, "llm_slot", lambda: slot("global"))
    cli_providers.get(provider).complete_tool("model", "s", "u", TOOL)
    assert order == ["enter cli", "enter global", "exit global", "exit cli"]


def test_queued_cli_call_does_not_take_global_slot(monkeypatch, fake_popen):
    semaphore = threading.BoundedSemaphore(1)
    first_started, second_waiting, release = (threading.Event() for _ in range(3))
    results, errors = [], []

    class ObservedSemaphore:
        def __enter__(self):
            if threading.current_thread().name == "second":
                second_waiting.set()
            semaphore.acquire()

        def __exit__(self, *args):
            semaphore.release()

    def hold_first(call):
        if len(fake_popen.calls) == 1:
            first_started.set()
            assert release.wait(2)

    def run(provider):
        try:
            results.append(cli_providers.get(provider).complete_tool("model", "s", "u", TOOL))
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(cli_providers, "_cli_semaphore", ObservedSemaphore())
    fake_popen.on_communicate = hold_first
    before = client.llm_in_flight()
    first = threading.Thread(target=run, args=("claude-cli",), name="first", daemon=True)
    second = threading.Thread(target=run, args=("codex-cli",), name="second", daemon=True)
    first.start()
    try:
        assert first_started.wait(2)
        second.start()
        assert second_waiting.wait(2)
        assert second.is_alive()
        assert len(fake_popen.calls) == 1
        assert client.llm_in_flight() == before + 1
    finally:
        release.set()
        first.join(2)
        if second.ident is not None:
            second.join(2)
    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert results == [{"reply": "ok"}, {"reply": "ok"}]
    assert client.llm_in_flight() == before


def test_dispatch_to_claude_and_slash_fallback(monkeypatch, fake_popen):
    monkeypatch.setenv("ARGUS_PROVIDER", "claude-cli")
    assert client.complete_tool("model", "s", "u", TOOL) == {"reply": "ok"}
    assert len(fake_popen.calls) == 1
    requests = []

    def openrouter(payload):
        requests.append(payload)
        return {"choices": [{"message": {"tool_calls": [
            {"function": {"name": "result", "arguments": '{"reply": "fallback"}'}}
        ]}}]}

    monkeypatch.setattr(client, "_openrouter_request", openrouter)
    assert client.complete_tool("vendor/model", "s", "u", TOOL) == {"reply": "fallback"}
    assert requests[0]["model"] == "vendor/model"
    assert len(fake_popen.calls) == 1


@pytest.mark.parametrize("provider", ["claude-cli", "codex-cli"])
def test_cli_ping_returns_structured_reply(monkeypatch, fake_popen, provider):
    monkeypatch.setenv("ARGUS_PROVIDER", provider)
    assert client.ping("model") == "ok"
    assert len(fake_popen.calls) == 1
