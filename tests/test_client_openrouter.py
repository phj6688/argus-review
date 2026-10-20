"""OpenRouter lane of the LLM client: dispatch, payload shape, salvage, retries."""

import json

import httpx
import pytest

from argus import client
from argus.client import ToolSpec

TOOL = ToolSpec(name="report_findings", input_schema={"type": "object"}, max_tokens=100)


class _Response:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}
        self.request = httpx.Request("POST", client.OPENROUTER_URL)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(f"{self.status_code}", request=self.request,
                                        response=self)


def _tool_reply(arguments):
    return {"choices": [{"message": {"tool_calls": [
        {"function": {"name": "report_findings", "arguments": arguments}}]}}]}


def test_dispatch_predicate():
    assert client.is_openrouter_model("~x-ai/grok-latest")
    assert client.is_openrouter_model("x-ai/grok-4.5")
    assert client.is_openrouter_model("z-ai/glm-5.2")
    assert not client.is_openrouter_model("claude-opus-4-8")
    assert not client.is_openrouter_model("claude-sonnet-5-5")


def test_complete_tool_routes_slash_models_to_openrouter(monkeypatch):
    seen = {}

    def fake_post(url, timeout=None, json=None, headers=None):
        seen.update(url=url, payload=json, headers=headers)
        return _Response(payload=_tool_reply('{"findings": []}'))

    monkeypatch.setenv(client.ENV_OPENROUTER_KEY, "sk-or-test")
    monkeypatch.setattr(client.httpx, "post", fake_post)
    result = client.complete_tool("~x-ai/grok-latest", "sys", "user", TOOL)
    assert result == {"findings": []}
    assert seen["url"] == client.OPENROUTER_URL
    assert seen["headers"]["Authorization"] == "Bearer sk-or-test"
    payload = seen["payload"]
    assert payload["model"] == "~x-ai/grok-latest"
    assert payload["messages"][0] == {"role": "system", "content": "sys"}
    assert payload["tools"][0]["function"]["name"] == "report_findings"
    assert payload["tool_choice"] == "auto"
    assert payload["max_tokens"] == 100


def test_arguments_may_arrive_as_dict(monkeypatch):
    monkeypatch.setenv(client.ENV_OPENROUTER_KEY, "k")
    monkeypatch.setattr(client.httpx, "post",
                        lambda *a, **k: _Response(payload=_tool_reply({"findings": [1]})))
    assert client.complete_tool("x/y", "s", "u", TOOL) == {"findings": [1]}


def test_content_json_salvage_when_tool_ignored(monkeypatch):
    reply = {"choices": [{"message": {
        "content": 'Here you go:\n```json\n{"findings": []}\n```'}}]}
    monkeypatch.setenv(client.ENV_OPENROUTER_KEY, "k")
    monkeypatch.setattr(client.httpx, "post", lambda *a, **k: _Response(payload=reply))
    assert client.complete_tool("x/y", "s", "u", TOOL) == {"findings": []}


def test_no_tool_call_and_no_json_raises(monkeypatch):
    reply = {"choices": [{"message": {"content": "cannot help"}}]}
    monkeypatch.setenv(client.ENV_OPENROUTER_KEY, "k")
    monkeypatch.setattr(client.httpx, "post", lambda *a, **k: _Response(payload=reply))
    with pytest.raises(ValueError, match="did not call"):
        client.complete_tool("x/y", "s", "u", TOOL)


def test_missing_key_is_backend_not_configured(monkeypatch):
    monkeypatch.delenv(client.ENV_OPENROUTER_KEY, raising=False)
    with pytest.raises(client.BackendNotConfigured, match="OPENROUTER_API_KEY"):
        client.complete_tool("x/y", "s", "u", TOOL)


def test_transient_429_retries_then_succeeds(monkeypatch):
    replies = iter([
        _Response(status_code=429, headers={"retry-after": "0"}),
        _Response(status_code=503),
        _Response(payload=_tool_reply('{"ok": true}')),
    ])
    monkeypatch.setenv(client.ENV_OPENROUTER_KEY, "k")
    monkeypatch.setattr(client.httpx, "post", lambda *a, **k: next(replies))
    monkeypatch.setattr(client.time, "sleep", lambda s: None)
    assert client.complete_tool("x/y", "s", "u", TOOL) == {"ok": True}


def test_402_fails_immediately_without_retry(monkeypatch):
    calls = {"n": 0}

    def fake_post(*args, **kwargs):
        calls["n"] += 1
        return _Response(status_code=402)

    monkeypatch.setenv(client.ENV_OPENROUTER_KEY, "k")
    monkeypatch.setattr(client.httpx, "post", fake_post)
    monkeypatch.setattr(client.time, "sleep", lambda s: None)
    with pytest.raises(httpx.HTTPStatusError):
        client.complete_tool("x/y", "s", "u", TOOL)
    assert calls["n"] == 1


def test_claude_models_never_touch_openrouter(monkeypatch):
    monkeypatch.setenv("ARGUS_PROVIDER", "gateway")
    def boom(*args, **kwargs):
        raise AssertionError("claude ids must not hit OpenRouter")

    class _Block:
        type = "tool_use"
        name = "report_findings"
        input = {"findings": []}

    class _Messages:
        def create(self, **kwargs):
            return type("R", (), {"content": [_Block()]})()

    monkeypatch.setattr(client.httpx, "post", boom)
    monkeypatch.setattr(client, "build_client",
                        lambda *args: type("C", (), {"messages": _Messages()})())
    assert client.complete_tool("claude-opus-4-8", "s", "u", TOOL) == {"findings": []}


def test_ping_openrouter_path(monkeypatch):
    reply = {"choices": [{"message": {"content": " ok "}}]}
    monkeypatch.setenv(client.ENV_OPENROUTER_KEY, "k")
    monkeypatch.setattr(client.httpx, "post", lambda *a, **k: _Response(payload=reply))
    assert client.ping("~x-ai/grok-latest") == "ok"


def test_review_arguments_string_roundtrip(monkeypatch):
    findings = {"findings": [{"file": "a.py", "line": 1, "severity": "major",
                              "confidence": 0.8, "title": "t", "description": "d",
                              "proposed_fix": "p"}]}
    monkeypatch.setenv(client.ENV_OPENROUTER_KEY, "k")
    monkeypatch.setattr(client.httpx, "post",
                        lambda *a, **k: _Response(payload=_tool_reply(json.dumps(findings))))
    assert client.complete_tool("x/y", "s", "u", TOOL) == findings
