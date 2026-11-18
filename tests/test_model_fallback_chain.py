"""A retired model id moves the review to the next model, and the move is remembered.

The gateway retired gpt-6.1-sol on 2026-11-18 (the third retirement). Every review
paid the dead call, then the Claude fallback spent its whole 8k budget on thinking
and never called the tool. The fakes below replay the exact error bodies seen live.
"""

import copy
import logging
from types import SimpleNamespace

import httpx
import pytest
from anthropic import BadRequestError, NotFoundError

from argus import automark, availability, client, judges, review
from argus.api import db, main
from argus.availability import ModelUnavailable
from argus.client import ToolCallMismatch, ToolSpec
from argus.review import ReviewResult
from argus.schema import Finding, Severity

PROD_400 = ("Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
            "'message': 'unknown provider for model gpt-6.1-sol'}}")
TOOL = ToolSpec(name="report_findings",
                input_schema={"type": "object", "properties": {"findings": {"type": "array"}},
                              "required": ["findings"], "additionalProperties": False},
                max_tokens=100)


@pytest.fixture(autouse=True)
def fresh_marks(monkeypatch):
    availability.reset()
    monkeypatch.delenv(availability.ENV_TTL, raising=False)
    monkeypatch.delenv(client.ENV_CLAUDE_EFFORT, raising=False)
    monkeypatch.delenv("ARGUS_FALLBACK_MODEL", raising=False)
    yield
    availability.reset()


def _request():
    return httpx.Request("POST", "http://router.test/v1/messages")


def _anthropic(cls, status, message, body=None):
    return cls(message, response=httpx.Response(status, request=_request()), body=body)


def _http(status, payload):
    request = httpx.Request("POST", "http://router.test/v1/chat/completions")
    response = httpx.Response(status, request=request, json=payload)
    return httpx.HTTPStatusError(f"{status}", request=request, response=response)


# --- A. the classifier ---------------------------------------------------------

@pytest.mark.parametrize("error", [
    _anthropic(BadRequestError, 400, PROD_400),
    _anthropic(BadRequestError, 400, "Error code: 400",
               body={"error": {"type": "invalid_request_error",
                               "message": "unknown provider for model gpt-6.1-sol"}}),
    _anthropic(NotFoundError, 404, "Error code: 404",
               body={"type": "error", "error": {"type": "not_found_error", "message": "model: x"}}),
    _http(400, {"error": {"message": "unknown provider for model gpt-6.1-sol",
                          "type": "invalid_request_error", "code": "model_not_found"}}),
    _http(404, {"error": {"message": "The model `gpt-6.1-sol` does not exist or you do not "
                                     "have access to it.", "code": None}}),
], ids=["prod-400-text", "prod-400-body", "anthropic-404", "judge-route-400", "openai-404"])
def test_retired_model_errors_are_unavailable(error):
    assert availability.is_model_unavailable(error)


@pytest.mark.parametrize("error", [
    _anthropic(BadRequestError, 400, "tools.0.custom.strict: Extra inputs are not permitted"),
    _anthropic(BadRequestError, 400, "messages.0.content: Field required"),
    _anthropic(BadRequestError, 400, "invalid model"),
    _http(400, {"error": {"message": "max_tokens must be at least 1", "code": "invalid_value"}}),
    _http(404, {"detail": "Not Found"}),
    _http(500, {"error": {"message": "unknown provider for model gpt-6.1-sol"}}),
    RuntimeError("unknown provider for model gpt-6.1-sol"),
], ids=["strict-400", "payload-400", "vague-400", "chat-400", "bad-url-404", "server-500",
        "no-status"])
def test_other_errors_are_not_unavailable(error):
    assert not availability.is_model_unavailable(error)


def _install(monkeypatch, *replies):
    calls = []
    queue = list(replies)

    def create(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        reply = queue.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(client.providers, "resolve", lambda model: client.providers.GATEWAY)
    monkeypatch.setattr(client, "build_client", lambda *args: SimpleNamespace(
        messages=SimpleNamespace(create=create)))
    return calls


def _block(kind, **fields):
    return SimpleNamespace(type=kind, name=fields.get("name"), input=fields.get("input"))


def _reply(*blocks, stop_reason="end_turn"):
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason, usage=None)


def _call(payload):
    return _block("tool_use", name="report_findings", input=payload)


def test_complete_tool_raises_model_unavailable_and_marks_it(monkeypatch, caplog):
    calls = _install(monkeypatch, _anthropic(BadRequestError, 400, PROD_400),
                     _anthropic(BadRequestError, 400, PROD_400))
    with caplog.at_level(logging.WARNING, logger="argus.availability"):
        for _ in range(2):
            with pytest.raises(ModelUnavailable) as caught:
                client.complete_tool("gpt-6.1-sol", "sys", "user", TOOL)
    assert caught.value.status_code == 400
    assert isinstance(caught.value.__cause__, BadRequestError)
    assert len(calls) == 2, "no retry of a request the gateway cannot serve"
    assert availability.is_marked_unavailable("gpt-6.1-sol")
    warnings = [r for r in caplog.records if "marked unavailable" in r.getMessage()]
    assert len(warnings) == 1, "one line when the model is first marked"
    assert "gpt-6.1-sol" in warnings[0].getMessage()
    assert "unknown provider for model" in warnings[0].getMessage()


def test_a_strict_rejection_is_not_marked(monkeypatch):
    _install(monkeypatch, _anthropic(BadRequestError, 400, "bad payload"))
    with pytest.raises(BadRequestError):
        client.complete_tool("gpt-6.1-sol", "sys", "user", TOOL)
    assert not availability.is_marked_unavailable("gpt-6.1-sol")


# --- B. the ordered chain ------------------------------------------------------

def test_fallback_models_keeps_order_and_drops_the_primary(monkeypatch):
    monkeypatch.setenv("ARGUS_FALLBACK_MODEL", " gpt-6.2-sol, primary ,claude-sonnet-5-5,gpt-6.2-sol,")
    assert review.fallback_models("primary") == ["gpt-6.2-sol", "claude-sonnet-5-5"]
    runner, model = main._fallback_for("primary", lambda m: lambda: m)
    assert model == "gpt-6.2-sol" and runner() == "gpt-6.2-sol"


def test_a_single_fallback_id_parses_as_before(monkeypatch):
    monkeypatch.setenv("ARGUS_FALLBACK_MODEL", "claude-sonnet-5-5")
    assert review.fallback_models("gpt-6.1-sol") == ["claude-sonnet-5-5"]
    assert review.fallback_models("claude-sonnet-5-5") == []


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "argus.db"
    monkeypatch.setenv("ARGUS_DB_PATH", str(path))
    db.init_db(str(path))
    return str(path)


def _ok():
    return ReviewResult(raw_count=0, bugs=[], nits=[], dropped=0)


class _Runner:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _gone(model):
    return ModelUnavailable(model, "unknown provider for model " + model, 400)


def test_the_chain_runs_in_order_until_one_model_answers(db_path):
    primary = _Runner(_gone("gpt-6.1-sol"))
    second = _Runner(RuntimeError("second lane down"))
    third = _Runner(_ok())
    rid = db.create_review(db_path, "example/x", 1, "gpt-6.1-sol")
    error = main.run_one_review(db_path, rid, "example/x", 1, primary, primary_model="gpt-6.1-sol",
                                fallbacks=[("gpt-6.2-sol", second), ("claude-sonnet-5-5", third)])
    assert error is None
    assert (primary.calls, second.calls, third.calls) == (1, 1, 1)
    row = db.get_review(db_path, rid)
    assert row["status"] == "done"
    assert row["model_used"] == "claude-sonnet-5-5"
    assert row["fallback_from"] == "gpt-6.1-sol"
    assert "primary gpt-6.1-sol failed: ModelUnavailable" in row["fallback_reason"]
    assert "fallback gpt-6.2-sol failed: RuntimeError: second lane down" in row["fallback_reason"]


def test_when_every_model_fails_the_error_names_each(db_path):
    rid = db.create_review(db_path, "example/x", 1, "p")
    error = main.run_one_review(db_path, rid, "example/x", 1, _Runner(_gone("p")), primary_model="p",
                                fallbacks=[("a", _Runner(_gone("a"))),
                                           ("b", _Runner(RuntimeError("b down")))])
    assert isinstance(error, RuntimeError)
    row = db.get_review(db_path, rid)
    assert row["status"] == "error" and row["error_class"] == "RuntimeError"
    assert row["error"].startswith("RuntimeError: b down (fallback b; primary p failed:")
    assert "fallback a failed: ModelUnavailable" in row["error"]


# --- C. skip while marked -------------------------------------------------------

def test_a_marked_primary_is_skipped_without_a_call(db_path):
    availability.mark_unavailable("gpt-6.1-sol", _gone("gpt-6.1-sol"))
    primary = _Runner(AssertionError("must not be called"))
    fallback = _Runner(_ok())
    rid = db.create_review(db_path, "example/x", 1, "gpt-6.1-sol")
    main.run_one_review(db_path, rid, "example/x", 1, primary, primary_model="gpt-6.1-sol",
                        fallbacks=[("gpt-6.2-sol", fallback)])
    assert primary.calls == 0 and fallback.calls == 1
    row = db.get_review(db_path, rid)
    assert row["model_used"] == "gpt-6.2-sol" and row["fallback_from"] == "gpt-6.1-sol"
    assert row["fallback_reason"] == "primary gpt-6.1-sol skipped: marked unavailable"


def test_the_mark_ends_after_the_ttl(db_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(availability.time, "monotonic", lambda: now[0])
    monkeypatch.setenv(availability.ENV_TTL, "60")
    availability.mark_unavailable("gpt-6.1-sol", _gone("gpt-6.1-sol"))
    now[0] += 59
    assert availability.is_marked_unavailable("gpt-6.1-sol")
    now[0] += 2
    assert not availability.is_marked_unavailable("gpt-6.1-sol")
    primary = _Runner(_ok())
    rid = db.create_review(db_path, "example/x", 1, "gpt-6.1-sol")
    main.run_one_review(db_path, rid, "example/x", 1, primary, primary_model="gpt-6.1-sol",
                        fallbacks=[("gpt-6.2-sol", _Runner(_ok()))])
    assert primary.calls == 1
    assert db.get_review(db_path, rid)["model_used"] is None


def test_the_default_ttl_and_zero_turns_it_off(monkeypatch):
    assert availability.ttl_seconds() == 1800.0
    monkeypatch.setenv(availability.ENV_TTL, "junk")
    assert availability.ttl_seconds() == 1800.0
    monkeypatch.setenv(availability.ENV_TTL, "0")
    availability.mark_unavailable("m", RuntimeError("gone"))
    assert not availability.is_marked_unavailable("m")


def test_marks_are_per_lane():
    availability.mark_unavailable("gpt-6.1-sol", RuntimeError("gone"), availability.JUDGE)
    assert availability.is_marked_unavailable("gpt-6.1-sol", availability.JUDGE)
    assert not availability.is_marked_unavailable("gpt-6.1-sol")


def test_when_every_model_is_marked_they_are_all_tried(db_path):
    for model in ("p", "f"):
        availability.mark_unavailable(model, _gone(model))
    primary = _Runner(_ok())
    rid = db.create_review(db_path, "example/x", 1, "p")
    main.run_one_review(db_path, rid, "example/x", 1, primary, primary_model="p",
                        fallbacks=[("f", _Runner(_ok()))])
    assert primary.calls == 1


def test_without_a_fallback_a_marked_primary_still_runs(db_path):
    availability.mark_unavailable("p", _gone("p"))
    primary = _Runner(_gone("p"))
    rid = db.create_review(db_path, "example/x", 1, "p")
    main.run_one_review(db_path, rid, "example/x", 1, primary, primary_model="p")
    assert primary.calls == 1
    assert db.get_review(db_path, rid)["error_class"] == "ModelUnavailable"


def test_concurrent_marks_log_once(caplog):
    import threading

    with caplog.at_level(logging.WARNING, logger="argus.availability"):
        threads = [threading.Thread(target=availability.mark_unavailable,
                                    args=("m", RuntimeError("gone"))) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    assert sum("marked unavailable" in r.getMessage() for r in caplog.records) == 1


# --- D. the Claude budget -----------------------------------------------------

def test_claude_gets_medium_effort_and_thinking_headroom(monkeypatch):
    calls = _install(monkeypatch, _reply(_call({"findings": []})))
    client.complete_tool("claude-sonnet-5-5", "sys", "user", TOOL)
    assert calls[0]["output_config"] == {"effort": "medium"}
    assert calls[0]["max_tokens"] == 100 + client.CLAUDE_THINKING_ALLOWANCE
    assert calls[0]["timeout"] > client.DEFAULT_TIMEOUT_SECONDS


@pytest.mark.parametrize("raw, effort", [("low", "low"), ("HIGH", "high"), ("bogus", "medium")])
def test_the_effort_is_set_by_env(monkeypatch, raw, effort):
    monkeypatch.setenv(client.ENV_CLAUDE_EFFORT, raw)
    calls = _install(monkeypatch, _reply(_call({"findings": []})))
    client.complete_tool("claude-opus-5-5", "sys", "user", TOOL)
    assert calls[0]["output_config"] == {"effort": effort}


def test_gpt_on_the_gateway_lane_is_unchanged(monkeypatch):
    calls = _install(monkeypatch, _reply(_call({"findings": []})))
    client.complete_tool("gpt-6.2-sol", "sys", "user", review.REVIEW_TOOL)
    assert "output_config" not in calls[0] and "timeout" not in calls[0]
    assert calls[0]["max_tokens"] == review.REVIEW_MAX_TOKENS


def test_thinking_that_runs_out_of_budget_retries_with_more(monkeypatch):
    thinking_only = _reply(_block("thinking"), stop_reason="max_tokens")
    calls = _install(monkeypatch, thinking_only, _reply(_call({"findings": []})))
    assert client.complete_tool("claude-sonnet-5-5", "sys", "user", review.REVIEW_TOOL) == {
        "findings": []}
    first, second = calls
    assert first["max_tokens"] == review.REVIEW_MAX_TOKENS + client.CLAUDE_THINKING_ALLOWANCE
    assert second["max_tokens"] == client.RETRY_MAX_TOKENS_CAP
    assert second["timeout"] > first["timeout"]
    assert {k: v for k, v in first.items() if k not in ("max_tokens", "timeout")} == {
        k: v for k, v in second.items() if k not in ("max_tokens", "timeout")}


def test_a_tool_call_cut_at_max_tokens_is_retried_not_returned(monkeypatch):
    cut = _reply(_block("thinking"), _call({}), stop_reason="max_tokens")
    calls = _install(monkeypatch, cut, _reply(_call({"findings": []}), stop_reason="tool_use"))
    assert client.complete_tool("claude-sonnet-5-5", "sys", "user", TOOL) == {"findings": []}
    assert calls[1]["max_tokens"] == 2 * calls[0]["max_tokens"]


def test_a_second_cut_raises_and_never_returns_partial_input(monkeypatch):
    cut = _reply(_call({}), stop_reason="max_tokens")
    _install(monkeypatch, cut, cut)
    with pytest.raises(ToolCallMismatch, match="cut off at max_tokens") as caught:
        client.complete_tool("claude-sonnet-5-5", "sys", "user", TOOL)
    assert caught.value.truncated


def test_a_miss_that_did_not_hit_the_limit_is_retried_unchanged(monkeypatch):
    prose = _reply(_block("text"))
    calls = _install(monkeypatch, prose, _reply(_call({"findings": []})))
    client.complete_tool("claude-sonnet-5-5", "sys", "user", TOOL)
    assert calls[0] == calls[1]


def test_a_model_that_rejects_effort_is_asked_again_without_it(monkeypatch):
    rejected = _anthropic(BadRequestError, 400, "output_config.effort: Extra inputs are not permitted")
    calls = _install(monkeypatch, rejected, _reply(_call({"findings": []})))
    assert client.complete_tool("claude-fable-5-2", "sys", "user", TOOL) == {"findings": []}
    assert "output_config" in calls[0] and "output_config" not in calls[1]


# --- E. the judge lists --------------------------------------------------------

@pytest.fixture
def judge_env(monkeypatch):
    monkeypatch.setenv(judges.ENV_BASE_URL, "http://router.test/v1/chat/completions")
    monkeypatch.setenv(judges.ENV_OPENAI_MODEL, "gpt-6.1-sol, gpt-6.2-sol")
    monkeypatch.setenv(judges.ENV_OPENAI_KEY, "gpt-key")
    monkeypatch.setenv(judges.ENV_ANTHROPIC_MODEL, "claude-opus-5-5")
    monkeypatch.setenv(judges.ENV_ANTHROPIC_KEY, "claude-key")


def _judge_post(monkeypatch, dead):
    sent = []

    def post(url, *, timeout=None, headers=None, json=None):
        sent.append(json["model"])
        request = httpx.Request("POST", url)
        if json["model"] in dead:
            return httpx.Response(400, request=request, json={"error": {
                "message": f"unknown provider for model {json['model']}",
                "code": "model_not_found"}})
        return httpx.Response(200, request=request, json={"choices": [{"message": {
            "content": '{"verdict":"real","confidence":0.9,"evidence":"e","judge_model":"forged"}'}}]})

    monkeypatch.setattr(automark.httpx, "post", post)
    return sent


def _finding():
    return Finding(file="a.py", line=3, severity=Severity.major, confidence=0.8,
                   title="t", description="d", proposed_fix="f")


def test_the_judge_moves_to_the_next_id_of_its_family(judge_env, monkeypatch):
    sent = _judge_post(monkeypatch, dead={"gpt-6.1-sol"})
    out = automark.judge_finding(_finding(), "  3: x", reviewer_model="claude-sonnet-5-5")
    assert out["verdict"] == "real"
    assert out[automark.JUDGE_MODEL_KEY] == "gpt-6.2-sol"
    assert sent == ["gpt-6.1-sol", "gpt-6.2-sol"]
    assert availability.is_marked_unavailable("gpt-6.1-sol", availability.JUDGE)
    automark.judge_finding(_finding(), "  3: x", reviewer_model="claude-sonnet-5-5")
    assert sent[2:] == ["gpt-6.2-sol"], "the marked id is not called again"


def test_a_judge_error_that_is_not_unavailability_is_not_walked(judge_env, monkeypatch):
    def post(url, *, timeout=None, headers=None, json=None):
        request = httpx.Request("POST", url)
        return httpx.Response(400, request=request, json={"error": {"message": "bad payload"}})

    monkeypatch.setattr(automark.httpx, "post", post)
    with pytest.raises(httpx.HTTPStatusError):
        automark.judge_finding(_finding(), "  3: x", reviewer_model="claude-sonnet-5-5")
    assert not availability.is_marked_unavailable("gpt-6.1-sol", availability.JUDGE)


def test_every_judge_id_gone_still_raises(judge_env, monkeypatch):
    _judge_post(monkeypatch, dead={"gpt-6.1-sol", "gpt-6.2-sol"})
    with pytest.raises(httpx.HTTPStatusError):
        automark.judge_finding(_finding(), "  3: x", reviewer_model="claude-sonnet-5-5")


def test_a_judge_list_may_not_mix_families(judge_env, monkeypatch):
    monkeypatch.setenv(judges.ENV_OPENAI_MODEL, "gpt-6.2-sol, claude-sonnet-5-5")
    with pytest.raises(judges.NoDecorrelatedJudge, match="claude-sonnet-5-5"):
        judges.judge_for("claude-opus-5-5")


def test_a_single_judge_id_is_unchanged(judge_env, monkeypatch):
    monkeypatch.setenv(judges.ENV_OPENAI_MODEL, "gpt-6.2-sol")
    spec = judges.judge_for("claude-opus-5-5")
    assert (spec.model, spec.fallbacks) == ("gpt-6.2-sol", ())


def test_the_judge_lane_records_the_model_that_answered(judge_env, monkeypatch):
    from argus.schema import Verdict, VerifiedFinding

    monkeypatch.setenv(review.JUDGE_LIVE_ENV, "1")
    _judge_post(monkeypatch, dead={"gpt-6.1-sol"})
    item = VerifiedFinding(finding=_finding(),
                           verdict=Verdict(is_real=True, confidence=0.9, reasoning="r"))
    record = review._judge_lane([item], {"a.py": "  3: x"}, "claude-sonnet-5-5")
    assert record["status"] == "ran" and record["model"] == "gpt-6.2-sol"
    assert item.judge.model == "gpt-6.2-sol"


# --- review round 1 ------------------------------------------------------------

@pytest.mark.parametrize("first, second", [
    ("output_config.effort: Extra inputs are not permitted", "tools.0.custom.strict: not supported"),
    ("tools.0.custom.strict: not supported", "output_config.effort: Extra inputs are not permitted"),
], ids=["effort-then-strict", "strict-then-effort"])
def test_effort_and_strict_rejections_are_both_removed(monkeypatch, first, second):
    calls = _install(monkeypatch, _anthropic(BadRequestError, 400, first),
                     _anthropic(BadRequestError, 400, second), _reply(_call({"findings": []})))
    assert client.complete_tool("claude-fable-5-2", "sys", "user", TOOL) == {"findings": []}
    assert len(calls) == 3
    assert "output_config" not in calls[2] and "strict" not in calls[2]["tools"][0]


def test_a_third_rejection_is_raised_not_looped(monkeypatch):
    calls = _install(monkeypatch,
                     _anthropic(BadRequestError, 400, "output_config.effort: not permitted"),
                     _anthropic(BadRequestError, 400, "strict: not supported"),
                     _anthropic(BadRequestError, 400, "strict: still not supported"))
    with pytest.raises(BadRequestError, match="still"):
        client.complete_tool("claude-fable-5-2", "sys", "user", TOOL)
    assert len(calls) == 3


def test_a_route_404_that_names_no_model_is_not_unavailable():
    error = _anthropic(NotFoundError, 404, "Error code: 404",
                       body={"type": "error", "error": {"type": "not_found_error",
                                                        "message": "Not Found"}})
    assert not availability.is_model_unavailable(error)


def test_judge_selection_reads_each_mark_once(judge_env, monkeypatch):
    monkeypatch.setenv(judges.ENV_OPENAI_MODEL, "gpt-6.2-sol")
    clock = [1000.0]

    def tick():
        clock[0] += 1000.0
        return clock[0]

    monkeypatch.setenv(availability.ENV_TTL, "1500")
    monkeypatch.setattr(availability.time, "monotonic", lambda: clock[0])
    availability.mark_unavailable("gpt-6.2-sol", RuntimeError("gone"), availability.JUDGE)
    # Every later clock read moves past the expiry, as a slow second pass would.
    monkeypatch.setattr(availability.time, "monotonic", tick)
    spec = judges.judge_for("claude-opus-5-5")
    assert (spec.model, spec.fallbacks) == ("gpt-6.2-sol", ())
