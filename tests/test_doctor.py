"""Provider checks stop before paid calls when configuration fails."""

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from anthropic import AuthenticationError

from argus import cli, cli_providers, client, doctor
from provider_helpers import PROVIDER_ENV, fake_popen as fake_popen


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for name in PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "subscription"
    home.mkdir()
    monkeypatch.setenv("ARGUS_CODEX_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def checks_io(monkeypatch, fake_popen):
    seen = SimpleNamespace(gets=[], pings=[], tools=[], status=200, models=[],
                           auth={"loggedIn": True, "authMethod": "claude.ai"}, login_code=0)

    def get(url, **kwargs):
        seen.gets.append((url, kwargs))
        return httpx.Response(seen.status, json={"data": seen.models}, request=httpx.Request("GET", url))

    def ping(model):
        seen.pings.append(model)
        return "ok"

    def complete(model, system, user, tool):
        seen.tools.append((model, system, user, tool))
        return {"reply": "ok"}

    def started(call):
        fake_popen.returncode = 0
        if call.argv[0].endswith("codex"):
            home = Path(call.kwargs["env"]["HOME"])
            assert home.is_dir() and list(home.iterdir()) == []
        if "--version" in call.argv:
            fake_popen.stdout = "1.2.3"
        elif "auth" in call.argv:
            fake_popen.stdout = json.dumps(seen.auth)
        elif "login" in call.argv:
            fake_popen.returncode = seen.login_code
            fake_popen.stdout = "logged in" if not seen.login_code else "not logged in"
            fake_popen.stderr = "not logged in" if seen.login_code else ""

    fake_popen.on_start = started
    monkeypatch.setattr(doctor.httpx, "get", get)
    monkeypatch.setattr(client, "ping", ping)
    monkeypatch.setattr(client, "complete_tool", complete)
    monkeypatch.setattr(cli_providers.shutil, "which", lambda binary, **kw: f"/bin/{binary}")
    monkeypatch.setattr(cli_providers.ClaudeCliProvider, "ping", lambda self, model: ping(model))
    monkeypatch.setattr(cli_providers.CodexCliProvider, "ping", lambda self, model: ping(model))
    return seen


def test_unconfigured_provider_fails_before_any_call(checks_io):
    checks = doctor.run_checks("model")
    assert len(checks) == 1
    assert checks[0][:2] == ("provider", False)
    assert checks_io.gets == checks_io.pings == checks_io.tools == []


@pytest.mark.parametrize("missing", ["ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"])
def test_gateway_missing_variable_stops_before_ping(monkeypatch, checks_io, missing):
    monkeypatch.setenv("ARGUS_PROVIDER", "gateway")
    for name in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
        if name != missing:
            monkeypatch.setenv(name, "set")
    checks = doctor.run_checks("model")
    assert checks == [("credentials", False, f"{missing} not set")]
    assert checks_io.pings == []


def test_gateway_credentials_then_ping(monkeypatch, checks_io):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.test")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token")
    checks = doctor.run_checks("model")
    assert [name for name, _, _ in checks] == ["credentials", "ping"]
    assert all(ok for _, ok, _ in checks)
    assert checks_io.pings == ["model"]


@pytest.mark.parametrize("key, warns", [("unusual-key", True), ("sk-ant-test", False)])
def test_anthropic_key_format_is_only_a_warning(monkeypatch, checks_io, key, warns):
    monkeypatch.setenv("ARGUS_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    checks = doctor.run_checks("model")
    warnings = [detail for _, ok, detail in checks if ok and detail.startswith("warning:")]
    assert bool(warnings) is warns
    assert all(ok for _, ok, _ in checks)
    assert checks_io.pings == ["model"]


def test_anthropic_authentication_error_is_bad_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    def fail(model):
        raise AuthenticationError("unauthorized", response=httpx.Response(
            401, request=httpx.Request("POST", "https://api.test/messages")), body=None)

    monkeypatch.setattr(client, "ping", fail)
    checks = doctor.run_checks("model")
    assert checks[-1] == ("ping", False, "bad key")
    assert len(checks) == 2


@pytest.mark.parametrize("model, parameters, ok", [
    ("vendor/missing", None, False), ("~vendor/alias", None, True),
    ("vendor/model", ["temperature"], False), ("vendor/model", ["tools"], True),
])
def test_openrouter_catalogue_checks(monkeypatch, checks_io, model, parameters, ok):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    if parameters is not None:
        checks_io.models = [{"id": model, "supported_parameters": parameters}]
    checks = doctor.run_checks(model)
    assert checks[1][0:2] == ("model", ok)
    if model.startswith("~"):
        assert checks[1][2].startswith("warning:")
    elif parameters is None:
        assert "not found" in checks[1][2]
    elif not ok:
        assert "does not support tools" in checks[1][2]
    assert checks_io.pings == ([model] if ok else [])
    assert len(checks) == (3 if ok else 2)
    url, kwargs = checks_io.gets[0]
    assert url == "https://openrouter.ai/api/v1/models"
    assert kwargs["headers"] == {"Authorization": "Bearer or-key"}


def test_openrouter_missing_key_stops_before_catalogue(monkeypatch, checks_io):
    monkeypatch.setenv("ARGUS_PROVIDER", "openrouter")
    checks = doctor.run_checks("vendor/model")
    assert checks == [("credentials", False, "OPENROUTER_API_KEY not set")]
    assert checks_io.gets == checks_io.pings == []


@pytest.mark.parametrize("base", [None, "http://localhost:11434/v1/", " \t",
                                  "  http://localhost:11434/v1/ \t"])
def test_openai_catalogue_then_one_structured_call(monkeypatch, checks_io, base):
    monkeypatch.setenv("ARGUS_PROVIDER", "openai")
    if base:
        monkeypatch.setenv("OPENAI_BASE_URL", base)
    checks = doctor.run_checks("model")
    assert [name for name, _, _ in checks] == ["models", "ping"]
    assert all(ok for _, ok, _ in checks)
    expected_base = (base or "").strip() or "https://api.openai.com/v1"
    assert checks_io.gets[0][0] == expected_base.rstrip("/") + "/models"
    assert checks_io.gets[0][1]["headers"] == {}
    assert checks_io.pings == []
    model, _, _, tool = checks_io.tools[0]
    assert model == "model"
    assert tool.input_schema["properties"] == {"reply": {"type": "string"}}
    assert len(checks_io.tools) == 1


def test_openai_401_stops_before_structured_call(monkeypatch, checks_io):
    monkeypatch.setenv("ARGUS_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    checks_io.status = 401
    assert doctor.run_checks("model") == [("models", False, "bad key")]
    assert checks_io.tools == []
    assert checks_io.gets[0][1]["headers"] == {"Authorization": "Bearer test-key"}


def test_openai_invalid_structured_ping_fails(monkeypatch):
    monkeypatch.setenv("ARGUS_PROVIDER", "openai")
    monkeypatch.setattr(client, "complete_tool", lambda *args: {"reply": None})
    checks = doctor.run_checks("model")
    assert checks[-1][0:2] == ("ping", False)
    assert "reply string" in checks[-1][2]


@pytest.mark.parametrize("provider", ["claude-cli", "codex-cli"])
def test_cli_missing_binary_stops_first(monkeypatch, checks_io, fake_popen, provider):
    monkeypatch.setenv("ARGUS_PROVIDER", provider)
    monkeypatch.setattr(cli_providers.shutil, "which", lambda *args, **kw: None)
    checks = doctor.run_checks("model")
    assert len(checks) == 1
    assert checks[0][0:2] == (f"{provider} binary", False)
    assert fake_popen.calls == []
    assert checks_io.pings == []


def test_claude_non_subscription_auth_warns(monkeypatch, checks_io):
    monkeypatch.setenv("ARGUS_PROVIDER", "claude-cli")
    checks_io.auth["authMethod"] = "api_key"
    checks = doctor.run_checks("model")
    auth = next(check for check in checks if check[0] == "claude-cli auth")
    assert auth[1] is True and auth[2].startswith("warning:")
    assert checks_io.pings == ["model"]


@pytest.mark.parametrize("provider, leaked", [
    ("claude-cli", "ANTHROPIC_API_KEY"), ("claude-cli", "ANTHROPIC_BASE_URL"),
    ("claude-cli", "ANTHROPIC_AUTH_TOKEN"), ("codex-cli", "OPENAI_API_KEY"),
    ("codex-cli", "CODEX_API_KEY"),
])
def test_cli_parent_credentials_warn_without_leaking(monkeypatch, fake_popen, provider, leaked):
    monkeypatch.setenv("ARGUS_PROVIDER", provider)
    monkeypatch.setenv(leaked, "must-stay-private")
    checks = doctor.run_checks("model")
    warning = next(detail for _, ok, detail in checks if ok and detail.startswith("warning:"))
    assert leaked in warning
    assert "must-stay-private" not in str(checks)
    assert all(leaked not in call.kwargs["env"] for call in fake_popen.calls)


@pytest.mark.parametrize("provider, auth_args", [
    ("claude-cli", ["auth", "status"]), ("codex-cli", ["login", "status"]),
])
def test_cli_version_and_auth_use_isolated_checks(
    monkeypatch, fake_popen, checks_io, clean_env, provider, auth_args,
):
    monkeypatch.setenv("ARGUS_PROVIDER", provider)
    monkeypatch.setenv("HOME", "/test/home")
    monkeypatch.setenv("PATH", "/test/bin")
    checks = doctor.run_checks("model")
    assert all(ok for _, ok, _ in checks)
    binary = "/bin/" + provider.removesuffix("-cli")
    assert [call.argv for call in fake_popen.calls] == [[binary, "--version"], [binary, *auth_args]]
    for call in fake_popen.calls:
        assert call.communications == [("", 30)]
        env = call.kwargs["env"]
        if provider == "codex-cli":
            assert env == {"HOME": env["HOME"], "PATH": "/test/bin", "CODEX_HOME": str(clean_env)}
            assert env["HOME"] != "/test/home"
            assert not Path(env["HOME"]).exists()
        else:
            assert env == {"HOME": "/test/home", "PATH": "/test/bin"}
        assert call.cwd_exists and call.cwd_contents == []
        assert not Path(call.kwargs["cwd"]).exists()
    assert checks_io.pings == ["model"]


@pytest.mark.parametrize("provider", ["claude-cli", "codex-cli"])
def test_cli_failed_auth_stops_before_ping(monkeypatch, checks_io, provider):
    monkeypatch.setenv("ARGUS_PROVIDER", provider)
    checks_io.auth["loggedIn"] = False
    checks_io.login_code = 1
    checks = doctor.run_checks("model")
    assert len(checks) == (4 if provider == "codex-cli" else 3)
    assert checks[-1][0:2] == (f"{provider} auth", False)
    assert checks_io.pings == []


@pytest.mark.parametrize("setting", [None, " \t", "missing", "file"])
def test_codex_home_check_stops_before_subprocesses(
    monkeypatch, fake_popen, checks_io, tmp_path, setting,
):
    monkeypatch.setenv("ARGUS_PROVIDER", "codex-cli")
    monkeypatch.delenv("ARGUS_CODEX_HOME", raising=False)
    if setting in ("missing", "file"):
        path = tmp_path / setting
        if setting == "file":
            path.write_text("not a directory")
        setting = str(path)
    if setting is not None:
        monkeypatch.setenv("ARGUS_CODEX_HOME", setting)
    checks = doctor.run_checks("model")
    assert len(checks) == 2
    assert checks[1][:2] == ("codex-cli home", False)
    assert "CODEX_HOME=<dir> codex login" in checks[1][2]
    assert fake_popen.calls == checks_io.pings == []


@pytest.mark.parametrize("entry, warns", [
    (None, False), ("AGENTS.md", True), ("skills/.system", False),
    ("skills/custom", True), ("skills/custom.md", True),
])
def test_codex_home_check_warns_about_personal_context(monkeypatch, clean_env, entry, warns):
    monkeypatch.setenv("ARGUS_PROVIDER", "codex-cli")
    if entry:
        path = clean_env / entry
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix:
            path.write_text("personal instructions")
        else:
            path.mkdir()
    checks = doctor.run_checks("model")
    assert checks[1][:2] == ("codex-cli home", True)
    assert checks[1][2].startswith("warning:") is warns
    if warns:
        assert entry in checks[1][2]


@pytest.mark.parametrize("provider", ["claude-cli", "codex-cli"])
@pytest.mark.parametrize("fallback, warns", [
    (None, False), (" \t", False), ("vendor/model", False), ("other-model", True),
])
def test_cli_doctor_warns_when_fallback_uses_same_subscription(
    monkeypatch, checks_io, provider, fallback, warns,
):
    monkeypatch.setenv("ARGUS_PROVIDER", provider)
    if fallback is not None:
        monkeypatch.setenv("ARGUS_FALLBACK_MODEL", fallback)
    checks = doctor.run_checks("model")
    warnings = [check for check in checks if check[0] == "fallback"]
    assert len(warnings) == int(warns)
    if warns:
        _, ok, detail = warnings[0]
        assert ok and detail.startswith("warning:")
        assert "subscription" in detail and "OpenRouter" in detail
    assert checks_io.pings == ["model"]


def test_doctor_command_prints_failure_and_exits(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "run_checks", lambda model: [
        ("credentials", False, "missing credentials"), ("ping", True, "must not print"),
    ])
    with pytest.raises(SystemExit) as caught:
        cli._run_doctor()
    assert caught.value.code not in (None, 0)
    output = capsys.readouterr().out
    assert "FAIL credentials: missing credentials" in output
    assert "must not print" not in output
    assert "Traceback" not in output


@pytest.mark.parametrize("provider", ["gateway", "anthropic", "claude-cli", "codex-cli"])
def test_slash_model_checks_the_resolved_provider(monkeypatch, checks_io, fake_popen, provider):
    monkeypatch.setenv("ARGUS_PROVIDER", provider)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    checks_io.models = [{"id": "vendor/model", "supported_parameters": ["tools"]}]
    checks = doctor.run_checks("vendor/model")
    assert checks[0] == ("credentials", True, "OPENROUTER_API_KEY set")
    assert all(ok for _, ok, _ in checks)
    assert checks_io.gets[0][0] == "https://openrouter.ai/api/v1/models"
    assert fake_popen.calls == []


def test_doctor_command_names_the_resolved_provider(monkeypatch, checks_io, capsys):
    monkeypatch.setenv("ARGUS_PROVIDER", "claude-cli")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setattr(cli, "DEFAULT_MODEL", "vendor/model")
    checks_io.models = [{"id": "vendor/model", "supported_parameters": ["tools"]}]
    cli._run_doctor()
    assert "provider: openrouter" in capsys.readouterr().out
