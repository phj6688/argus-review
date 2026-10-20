"""complete_tool asks for its tool with tool_choice auto, for every model family.

The served Claude ids (claude-sonnet-5-5, claude-opus-5-5, claude-fable-5-1) answer
HTTP 400 to a forced tool choice, so the forced call made the Claude fallback dead.
One path now serves both families: auto, a strict tool definition, and an
instruction on the user turn that names the tool. It repeats the one instruction
the call cannot work without, on the turn every model reads.
The fakes below reproduce the two rejections measured live on 2026-10-08.
"""

import copy
import json

import httpx
import pytest

from argus import client, review
from argus.api import db, main
from argus.client import ToolCallMismatch, ToolSpec
from argus.review import ReviewResult
from argus.schema import Severity

MODELS = ["claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1", "gpt-6.1-sol"]
CLAUDE_MODELS = [m for m in MODELS if m.startswith("claude")]

TOOL = ToolSpec(
    name="report_findings",
    input_schema={"type": "object", "properties": {"findings": {"type": "array"}},
                  "required": ["findings"], "additionalProperties": False},
    max_tokens=100,
)

ONE_CALL = {"type": "auto", "disable_parallel_tool_use": True}

FINDING = {"file": "app/users.py", "line": 5, "severity": "critical", "confidence": 0.9,
           "title": "SQL injection", "description": "name is concatenated into the query",
           "proposed_fix": "use a parameter placeholder"}


class _Rejected(Exception):
    """Stands in for the gateway's HTTP 400."""


class _Block:
    def __init__(self, type, name=None, input=None, text=None):
        self.type = type
        self.name = name
        self.input = input
        self.text = text


class _Response:
    def __init__(self, *blocks, stop_reason="end_turn"):
        self.content = list(blocks)
        self.stop_reason = stop_reason


def _tool_use(payload, name="report_findings"):
    return _Block("tool_use", name=name, input=payload)


def _prose():
    return _Response(_Block("text", text="Here are my findings, in prose."))


def _require_closed_objects(schema):
    """The Claude API refuses a strict tool whose object schemas stay open."""
    if isinstance(schema, dict):
        if schema.get("type") == "object" and schema.get("additionalProperties") is not False:
            raise _Rejected("400 For 'object' type, 'additionalProperties' must be "
                            "explicitly set to false")
        for value in schema.values():
            _require_closed_objects(value)
    elif isinstance(schema, list):
        for value in schema:
            _require_closed_objects(value)


class _Messages:
    """Records every create() call and replays scripted replies, one per call."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class _ClaudeMessages(_Messages):
    """What the served Claude ids were measured to reject."""

    def create(self, **kwargs):
        choice = kwargs.get("tool_choice") or {}
        if choice.get("type") in ("tool", "any"):
            raise _Rejected('400 tool_choice: type "tool" and "any" are not supported '
                            "for this model.")
        for definition in kwargs.get("tools", []):
            if definition.get("strict"):
                _require_closed_objects(definition["input_schema"])
        return super().create(**kwargs)


class _Routing:
    """One fake client, one scripted messages endpoint per model id."""

    def __init__(self, **by_model):
        self.by_model = by_model
        self.messages = self

    def create(self, **kwargs):
        return self.by_model[kwargs["model"]].create(**kwargs)


def _messages_for(model, *replies):
    cls = _ClaudeMessages if model.startswith("claude") else _Messages
    return cls(*replies)


def _install(monkeypatch, messages):
    monkeypatch.setenv("ARGUS_PROVIDER", "gateway")
    holder = type("Client", (), {"messages": messages})()
    monkeypatch.setattr(client, "build_client", lambda *args: holder)
    return messages


def _complete(monkeypatch, model, *replies, tool=TOOL):
    fake = _install(monkeypatch, _messages_for(model, *replies))
    return fake, lambda: client.complete_tool(model, "sys", "user text", tool)


# --- the request shape -------------------------------------------------------

@pytest.mark.parametrize("model", MODELS)
def test_every_family_gets_auto_and_a_strict_tool(monkeypatch, model):
    fake, call = _complete(monkeypatch, model, _Response(_tool_use({"findings": []})))
    assert call() == {"findings": []}
    sent = fake.calls[0]
    assert sent["tool_choice"] == ONE_CALL
    assert sent["tools"][0]["strict"] is True
    assert sent["tools"][0]["name"] == "report_findings"
    assert sent["tools"][0]["input_schema"] == TOOL.input_schema
    assert sent["model"] == model and sent["max_tokens"] == 100
    assert sent["system"] == "sys"


@pytest.mark.parametrize("model", MODELS)
def test_the_user_turn_names_the_tool(monkeypatch, model):
    fake, call = _complete(monkeypatch, model, _Response(_tool_use({"findings": []})))
    call()
    (turn,) = fake.calls[0]["messages"]
    assert turn["role"] == "user"
    assert turn["content"].startswith("user text")
    assert "report_findings" in turn["content"][len("user text"):]


@pytest.mark.parametrize("model", MODELS)
def test_thinking_blocks_before_the_tool_call_are_skipped(monkeypatch, model):
    reply = _Response(_Block("thinking"), _tool_use({"findings": [FINDING]}))
    _, call = _complete(monkeypatch, model, reply)
    assert call() == {"findings": [FINDING]}


def test_the_claude_fake_rejects_the_old_shapes():
    fake = _ClaudeMessages()
    with pytest.raises(_Rejected, match="not supported"):
        fake.create(tool_choice={"type": "tool", "name": "x"}, tools=[])
    with pytest.raises(_Rejected, match="not supported"):
        fake.create(tool_choice={"type": "any"}, tools=[])
    with pytest.raises(_Rejected, match="additionalProperties"):
        fake.create(tool_choice={"type": "auto"},
                    tools=[{"strict": True, "input_schema": {"type": "object"}}])


# --- a reply without the tool call -------------------------------------------

@pytest.mark.parametrize("model", MODELS)
def test_a_reply_without_the_tool_call_is_retried_once(monkeypatch, model):
    fake, call = _complete(monkeypatch, model, _prose(),
                           _Response(_tool_use({"findings": [FINDING]})))
    assert call() == {"findings": [FINDING]}
    assert len(fake.calls) == 2
    assert fake.calls[0] == fake.calls[1]


@pytest.mark.parametrize("model", MODELS)
def test_a_second_miss_raises_the_existing_error(monkeypatch, model):
    fake, call = _complete(monkeypatch, model, _prose(), _prose())
    with pytest.raises(ToolCallMismatch, match="model did not call report_findings") as caught:
        call()
    assert isinstance(caught.value, ValueError)
    assert len(fake.calls) == 2, "one retry, then the failure path"


@pytest.mark.parametrize("model", MODELS)
def test_two_calls_in_one_reply_are_retried_not_dropped(monkeypatch, model):
    split = _Response(_tool_use({"findings": [FINDING]}), _tool_use({"findings": [FINDING]}))
    fake, call = _complete(monkeypatch, model, split,
                           _Response(_tool_use({"findings": [FINDING, FINDING]})))
    assert call() == {"findings": [FINDING, FINDING]}
    assert len(fake.calls) == 2


@pytest.mark.parametrize("model", MODELS)
def test_two_calls_twice_raises_and_says_how_many(monkeypatch, model):
    split = _Response(_tool_use({"findings": []}), _tool_use({"findings": []}))
    fake, call = _complete(monkeypatch, model, split, split)
    with pytest.raises(ToolCallMismatch, match="made 2 tool calls") as caught:
        call()
    assert isinstance(caught.value, ValueError)
    assert len(fake.calls) == 2


@pytest.mark.parametrize("model", MODELS)
def test_a_stray_second_call_is_a_miss_not_ignored(monkeypatch, model):
    mixed = _Response(_tool_use({"findings": [FINDING]}), _tool_use({"x": 1}, name="something_else"))
    fake, call = _complete(monkeypatch, model, mixed, mixed)
    with pytest.raises(ToolCallMismatch, match="made 2 tool calls"):
        call()
    assert len(fake.calls) == 2


def test_a_call_to_another_tool_is_a_miss(monkeypatch):
    other = _Response(_tool_use({"x": 1}, name="something_else"))
    fake, call = _complete(monkeypatch, "claude-sonnet-5-5", other,
                           _Response(_tool_use({"findings": []})))
    assert call() == {"findings": []}
    assert len(fake.calls) == 2


def _no_content():
    reply = _Response()
    reply.content = None
    return reply


def test_a_reply_without_a_content_list_is_a_miss_not_a_crash(monkeypatch):
    # Seen live from the gateway: a reply object whose content was null raised
    # TypeError and discarded the whole review instead of asking once more.
    fake, call = _complete(monkeypatch, "gpt-6.1-sol", _no_content(),
                           _Response(_tool_use({"findings": []})))
    assert call() == {"findings": []}
    assert len(fake.calls) == 2
    _, call = _complete(monkeypatch, "gpt-6.1-sol", _no_content(), _no_content())
    with pytest.raises(ToolCallMismatch, match=r"blocks: \[\]"):
        call()


def test_the_error_names_what_came_back(monkeypatch):
    truncated = _Response(_Block("thinking"), stop_reason="max_tokens")
    _, call = _complete(monkeypatch, "claude-sonnet-5-5", truncated, truncated)
    with pytest.raises(ToolCallMismatch) as caught:
        call()
    assert "thinking" in str(caught.value)
    assert "max_tokens" in str(caught.value)


@pytest.mark.parametrize("failure", [RuntimeError("gateway down"), _Rejected("400 bad request")],
                         ids=["transport", "gateway-400"])
def test_errors_other_than_a_missing_call_are_not_retried(monkeypatch, failure):
    fake, call = _complete(monkeypatch, "claude-sonnet-5-5", failure)
    with pytest.raises(type(failure)):
        call()
    assert len(fake.calls) == 1


# --- the review tools stay strict-compatible ---------------------------------

@pytest.mark.parametrize("tool", [review.REVIEW_TOOL, review.VERDICT_TOOL],
                         ids=lambda tool: tool.name)
def test_review_tools_have_closed_object_schemas(tool):
    _require_closed_objects(tool.input_schema)


# --- both families through the real review calls -----------------------------

@pytest.mark.parametrize("model", MODELS)
def test_review_and_verify_run_on_each_family(monkeypatch, model):
    monkeypatch.setattr(review, "REVIEW_PASSES", 2)
    verdict = {"is_real": True, "confidence": 0.9, "reasoning": "the query is built by concatenation"}
    fake = _install(monkeypatch, _messages_for(
        model,
        _Response(_tool_use({"findings": [FINDING]}, "report_findings")),
        _Response(_tool_use({"findings": []}, "report_findings")),
        _Response(_tool_use(verdict, "report_verdict")),
    ))
    parsed = review._deep_review("diff", None, model)
    assert parsed.findings[0].severity is Severity.critical
    judged = review._verify(parsed.findings[0], "diff", model)
    assert judged.is_real is True
    assert [c["tools"][0]["name"] for c in fake.calls] == [
        "report_findings", "report_findings", "report_verdict"]
    assert all(c["tool_choice"] == ONE_CALL for c in fake.calls)
    assert all(c["tools"][0]["strict"] is True for c in fake.calls)


# --- the fallback lane, primary and fallback in different families -----------

@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "argus.db"
    monkeypatch.setenv("ARGUS_DB_PATH", str(path))
    db.init_db(str(path))
    return str(path)


def _runner(model):
    def run():
        parsed = review._deep_review("diff", None, model)
        return ReviewResult(raw_count=len(parsed.findings))
    return run


def test_claude_fallback_completes_when_the_gpt_primary_fails(monkeypatch, db_path):
    routing = _Routing(**{
        "gpt-6.1-sol": _Messages(RuntimeError("primary lane down")),
        "claude-sonnet-5-5": _ClaudeMessages(
            _Response(_tool_use({"findings": [FINDING]})),
            _Response(_tool_use({"findings": []}))),
    })
    _install(monkeypatch, routing)
    review_id = db.create_review(db_path, "example/x", 1, "gpt-6.1-sol")
    main.run_one_review(
        db_path, review_id, "example/x", 1, _runner("gpt-6.1-sol"),
        fallback_runner=_runner("claude-sonnet-5-5"),
        primary_model="gpt-6.1-sol", fallback_model="claude-sonnet-5-5",
    )
    row = db.get_review(db_path, review_id)
    assert row["status"] == "done"
    assert row["model_used"] == "claude-sonnet-5-5"
    assert row["fallback_from"] == "gpt-6.1-sol"
    sent = routing.by_model["claude-sonnet-5-5"].calls[0]
    assert sent["tool_choice"] == ONE_CALL


# --- the OpenRouter lane follows the same policy -----------------------------

class _HttpResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
        self.headers = {}
        self.request = httpx.Request("POST", client.OPENROUTER_URL)

    def json(self):
        return self._payload


def _or_tool_reply(arguments):
    return {"choices": [{"message": {"tool_calls": [
        {"function": {"name": "report_findings", "arguments": arguments}}]}}]}


def _or_prose_reply():
    return {"choices": [{"message": {"content": "no structured answer"}}]}


def _post_script(monkeypatch, *payloads):
    seen = []
    queue = list(payloads)

    def fake_post(url, timeout=None, json=None, headers=None):
        seen.append(copy.deepcopy(json))
        return _HttpResponse(queue.pop(0))

    monkeypatch.setenv(client.ENV_OPENROUTER_KEY, "k")
    monkeypatch.setattr(client.httpx, "post", fake_post)
    return seen


def test_openrouter_lane_sends_auto_and_names_the_tool(monkeypatch):
    seen = _post_script(monkeypatch, _or_tool_reply('{"findings": []}'))
    assert client.complete_tool("z-ai/glm-5.2", "sys", "user text", TOOL) == {"findings": []}
    payload = seen[0]
    assert payload["tool_choice"] == "auto"
    assert payload["parallel_tool_calls"] is False
    last = payload["messages"][-1]
    assert last["role"] == "user"
    assert last["content"].startswith("user text")
    assert "report_findings" in last["content"][len("user text"):]


def test_openrouter_lane_retries_a_miss_once(monkeypatch):
    seen = _post_script(monkeypatch, _or_prose_reply(),
                        _or_tool_reply(json.dumps({"findings": [FINDING]})))
    assert client.complete_tool("z-ai/glm-5.2", "s", "u", TOOL) == {"findings": [FINDING]}
    assert len(seen) == 2


def test_openrouter_lane_two_calls_are_retried_not_dropped(monkeypatch):
    split = {"choices": [{"message": {"tool_calls": [
        {"function": {"name": "report_findings", "arguments": '{"findings": []}'}},
        {"function": {"name": "report_findings", "arguments": '{"findings": []}'}}]}}]}
    seen = _post_script(monkeypatch, split, _or_tool_reply(json.dumps({"findings": [FINDING]})))
    assert client.complete_tool("z-ai/glm-5.2", "s", "u", TOOL) == {"findings": [FINDING]}
    assert len(seen) == 2


def test_openrouter_lane_two_calls_twice_raises(monkeypatch):
    split = {"choices": [{"message": {"tool_calls": [
        {"function": {"name": "report_findings", "arguments": "{}"}},
        {"function": {"name": "report_findings", "arguments": "{}"}}]}}]}
    seen = _post_script(monkeypatch, split, split)
    with pytest.raises(ToolCallMismatch, match="made 2 tool calls"):
        client.complete_tool("z-ai/glm-5.2", "s", "u", TOOL)
    assert len(seen) == 2


def test_openrouter_lane_a_stray_second_call_is_a_miss_not_ignored(monkeypatch):
    mixed = {"choices": [{"message": {"tool_calls": [
        {"function": {"name": "report_findings", "arguments": '{"findings": []}'}},
        {"function": {"name": "something_else", "arguments": "{}"}}]}}]}
    seen = _post_script(monkeypatch, mixed, mixed)
    with pytest.raises(ToolCallMismatch, match="made 2 tool calls"):
        client.complete_tool("z-ai/glm-5.2", "s", "u", TOOL)
    assert len(seen) == 2


def test_openrouter_lane_a_call_to_another_tool_is_a_miss(monkeypatch):
    other = {"choices": [{"message": {
        "content": '{"findings": []}',
        "tool_calls": [{"function": {"name": "something_else", "arguments": "{}"}}]}}]}
    seen = _post_script(monkeypatch, other, _or_tool_reply('{"findings": [1]}'))
    assert client.complete_tool("z-ai/glm-5.2", "s", "u", TOOL) == {"findings": [1]}
    assert len(seen) == 2


def test_openrouter_lane_second_miss_raises(monkeypatch):
    seen = _post_script(monkeypatch, _or_prose_reply(), _or_prose_reply())
    with pytest.raises(ToolCallMismatch, match="did not call report_findings"):
        client.complete_tool("z-ai/glm-5.2", "s", "u", TOOL)
    assert len(seen) == 2
