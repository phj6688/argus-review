"""Direct API credentials, strict-tool compatibility, and chat requests."""

import copy
from types import SimpleNamespace

import httpx
import pytest
from anthropic import BadRequestError

from argus import client, providers

TOOL = client.ToolSpec("report_findings", {"type": "object"}, 100)
REPLY = {"choices": [{"message": {"tool_calls": [
    {"function": {"name": "report_findings", "arguments": '{"findings": []}'}}
]}}]}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (
        "ARGUS_PROVIDER", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENROUTER_API_KEY", "ARGUS_OPENAI_TOKEN_PARAM",
        "ARGUS_CLI_TIMEOUT", "ARGUS_LLM_MAX_RETRIES", "ARGUS_LLM_TIMEOUT",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("base", [None, "https://anthropic.test/custom/"])
def test_direct_key_constructs_an_api_key_client(monkeypatch, base):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    if base:
        monkeypatch.setenv("ANTHROPIC_BASE_URL", base)
    with client.build_client() as sdk:
        assert sdk.api_key == "sk-ant-test"
        assert sdk.auth_token is None
        assert str(sdk.base_url) == (base or "https://api.anthropic.com")


def test_direct_key_with_blank_base_url_uses_the_public_api(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "")
    with client.build_client() as sdk:
        assert str(sdk.base_url) == "https://api.anthropic.com"


def test_gateway_pair_uses_auth_token(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.test/")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "gateway-token")
    with client.build_client() as sdk:
        assert sdk.auth_token == "gateway-token"
        assert sdk.api_key is None
        assert str(sdk.base_url) == "https://gateway.test/"


@pytest.mark.parametrize("stray_token", ["leftover-token", ""])
def test_direct_key_does_not_inherit_gateway_auth(monkeypatch, stray_token):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", stray_token)
    with client.build_client() as sdk:
        assert sdk.api_key == "sk-ant-test"
        assert not sdk.auth_token
        assert "Authorization" not in sdk.auth_headers


def test_gateway_does_not_inherit_direct_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.test/")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "gateway-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-unrelated")
    with client.build_client() as sdk:
        assert not sdk.api_key
        assert sdk.auth_headers == {"Authorization": "Bearer gateway-token"}


@pytest.mark.parametrize("provider, api_key, auth_token", [
    ("anthropic", "sk-ant-test", None), ("gateway", None, "gateway-token"),
])
def test_explicit_provider_selects_its_credential(monkeypatch, provider, api_key, auth_token):
    monkeypatch.setenv("ARGUS_PROVIDER", provider)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.test/")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "gateway-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    with client.build_client(providers.resolve("model")) as sdk:
        assert sdk.api_key == api_key
        assert sdk.auth_token == auth_token
        assert str(sdk.base_url) == "https://gateway.test/"


@pytest.mark.parametrize("provider, missing", [
    ("gateway", "ANTHROPIC_BASE_URL"), ("gateway", "ANTHROPIC_AUTH_TOKEN"),
    ("anthropic", "ANTHROPIC_API_KEY"),
])
def test_explicit_provider_does_not_use_another_credential(monkeypatch, provider, missing):
    for name, value in {
        "ANTHROPIC_BASE_URL": "https://gateway.test/", "ANTHROPIC_AUTH_TOKEN": "gateway-token",
        "ANTHROPIC_API_KEY": "sk-ant-test",
    }.items():
        if name != missing:
            monkeypatch.setenv(name, value)
    with pytest.raises(client.BackendNotConfigured, match=missing):
        client.build_client(provider)


@pytest.mark.parametrize("provider", ["anthropic", "gateway"])
@pytest.mark.parametrize("operation", ["complete_tool", "ping"])
def test_dispatch_builds_client_for_resolved_provider(monkeypatch, provider, operation):
    monkeypatch.setenv("ARGUS_PROVIDER", provider)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.test/")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "gateway-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    built = []

    def sdk(**kwargs):
        built.append(kwargs)
        block = (SimpleNamespace(type="tool_use", name=TOOL.name, input={"findings": []})
                 if operation == "complete_tool" else SimpleNamespace(type="text", text="ok"))
        return SimpleNamespace(messages=SimpleNamespace(
            create=lambda **kw: SimpleNamespace(content=[block])))

    monkeypatch.setattr(client, "Anthropic", sdk)
    if operation == "complete_tool":
        assert client.complete_tool("model", "s", "u", TOOL) == {"findings": []}
    else:
        assert client.ping("model") == "ok"
    assert len(built) == 1
    assert built[0].get("api_key") == ("sk-ant-test" if provider == "anthropic" else None)
    assert built[0].get("auth_token") == ("gateway-token" if provider == "gateway" else None)


@pytest.mark.parametrize("env", [{}, {"ANTHROPIC_BASE_URL": "https://gateway.test"},
                                     {"ANTHROPIC_AUTH_TOKEN": "token"}])
def test_missing_credentials_explains_both_configuration_methods(monkeypatch, env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(client.BackendNotConfigured) as caught:
        client.build_client()
    for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
        assert key in str(caught.value)


@pytest.mark.parametrize("provider", ["anthropic", "gateway"])
def test_strict_rejection_retries_once_without_strict(monkeypatch, caplog, provider):
    monkeypatch.setenv("ARGUS_PROVIDER", provider)
    calls = []

    def create(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        if len(calls) == 1:
            raise BadRequestError("strict is unsupported", response=httpx.Response(
                400, request=httpx.Request("POST", "https://api.test/messages")), body=None)
        return SimpleNamespace(content=[SimpleNamespace(
            type="tool_use", name=TOOL.name, input={"findings": []})])

    monkeypatch.setattr(client, "build_client", lambda *args: SimpleNamespace(
        messages=SimpleNamespace(create=create)))
    assert client.complete_tool("model", "system", "user", TOOL) == {"findings": []}
    assert len(calls) == 2
    assert calls[0]["tools"][0]["strict"] is True
    assert "strict" not in calls[1]["tools"][0]
    assert "strict" in caplog.text


@pytest.mark.parametrize("message, attempts", [("invalid model", 1), ("strict unsupported", 2)])
def test_bad_request_does_not_retry_indefinitely(monkeypatch, message, attempts):
    monkeypatch.setenv("ARGUS_PROVIDER", "anthropic")
    calls = []
    error = BadRequestError(message, response=httpx.Response(
        400, request=httpx.Request("POST", "https://api.test/messages")), body=None)

    def create(**kwargs):
        calls.append(kwargs)
        raise error

    monkeypatch.setattr(client, "build_client", lambda *args: SimpleNamespace(
        messages=SimpleNamespace(create=create)))
    with pytest.raises(BadRequestError) as caught:
        client.complete_tool("model", "system", "user", TOOL)
    assert caught.value is error
    assert len(calls) == attempts


@pytest.fixture
def chat(monkeypatch):
    monkeypatch.setenv("ARGUS_PROVIDER", "openai")
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, json=REPLY, request=httpx.Request("POST", url))

    monkeypatch.setattr(client.httpx, "post", post)
    return calls


@pytest.mark.parametrize("base, url", [
    (None, "https://api.openai.com/v1/chat/completions"),
    ("http://localhost:11434/v1///", "http://localhost:11434/v1/chat/completions"),
])
@pytest.mark.parametrize("key", [None, "", "test-key"])
def test_openai_url_auth_and_payload(monkeypatch, chat, base, url, key):
    if base is not None:
        monkeypatch.setenv("OPENAI_BASE_URL", base)
    if key is not None:
        monkeypatch.setenv("OPENAI_API_KEY", key)
    assert client.complete_tool("vendor/model", "system", "user", TOOL) == {"findings": []}
    assert len(chat) == 1
    actual_url, kwargs = chat[0]
    assert actual_url == url
    assert kwargs["headers"] == ({"Authorization": f"Bearer {key}"} if key else {})
    payload = kwargs["json"]
    assert payload["model"] == "vendor/model"
    assert payload["max_completion_tokens"] == 100
    assert "max_tokens" not in payload
    assert "temperature" not in payload
    assert payload["messages"][0] == {"role": "system", "content": "system"}
    assert payload["tools"][0]["function"]["parameters"] == TOOL.input_schema


def test_openai_legacy_token_parameter(monkeypatch, chat):
    monkeypatch.setenv("ARGUS_OPENAI_TOKEN_PARAM", "max_tokens")
    client.complete_tool("model", "s", "u", TOOL)
    payload = chat[0][1]["json"]
    assert payload["max_tokens"] == 100
    assert "max_completion_tokens" not in payload
    assert "temperature" not in payload


@pytest.mark.parametrize("status, attempts", [(429, 2), (402, 1)])
def test_openai_retry_policy(monkeypatch, status, attempts):
    monkeypatch.setenv("ARGUS_PROVIDER", "openai")
    calls, sleeps = [], []

    def post(url, **kwargs):
        calls.append(kwargs)
        return httpx.Response(status if len(calls) == 1 else 200, json=REPLY,
                              headers={"retry-after": "3"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(client.httpx, "post", post)
    monkeypatch.setattr(client.time, "sleep", sleeps.append)
    if status == 402:
        with pytest.raises(httpx.HTTPStatusError) as caught:
            client.complete_tool("model", "s", "u", TOOL)
        assert caught.value.response.status_code == 402
        assert sleeps == []
    else:
        assert client.complete_tool("model", "s", "u", TOOL) == {"findings": []}
        assert sleeps == [3.0]
    assert len(calls) == attempts


def test_openai_salvages_json_text(monkeypatch):
    monkeypatch.setenv("ARGUS_PROVIDER", "openai")
    reply = {"choices": [{"message": {"content": 'Result:\n```json\n{"findings": []}\n```'}}]}
    monkeypatch.setattr(client.httpx, "post", lambda url, **kw: httpx.Response(
        200, json=reply, request=httpx.Request("POST", url)))
    assert client.complete_tool("model", "s", "u", TOOL) == {"findings": []}


def test_openai_ping(monkeypatch):
    monkeypatch.setenv("ARGUS_PROVIDER", "openai")
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, json={"choices": [{"message": {"content": " ok \n"}}]},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(client.httpx, "post", post)
    assert client.ping("model") == "ok"
    assert calls[0][0] == "https://api.openai.com/v1/chat/completions"
    assert calls[0][1]["json"]["max_completion_tokens"] == client.PING_MAX_TOKENS
    assert "temperature" not in calls[0][1]["json"]
