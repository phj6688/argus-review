"""The judge lane picks its judge from the reviewer and reports itself in coverage.

A lane that quietly did not run is the defect this contract exists to prevent, and
the judge lane has already demonstrated it: it was dark for 2945 findings because
nothing recorded its absence.
"""

import pytest

from argus import review as review_mod
from argus import verdict
from argus.diff import FilePatch
from argus import judges
from argus.review import run_diff_review
from argus.schema import Finding, Review, Severity, Verdict

PY_PATCH = """@@ -1,3 +1,4 @@
 import os
+os.system("ls " + cmd)
 print(1)
"""


def _files():
    return [FilePatch(filename="src/app.py", status="modified", patch=PY_PATCH)]


def _lane(result, name):
    return next((rec for rec in result.coverage if rec["lane"] == name), None)


@pytest.fixture()
def reviewing(monkeypatch):
    """A full review that yields exactly one bug-class finding."""
    finding = Finding(file="src/app.py", line=2, severity=Severity.major,
                      confidence=0.8, title="shell injection", description="d",
                      proposed_fix="use subprocess")
    monkeypatch.setattr(review_mod, "_deep_review",
                        lambda diff, rules, model, intent, pr_section="": Review(findings=[finding]))
    monkeypatch.setattr(review_mod, "_verify",
                        lambda f, diff, model, **_: Verdict(is_real=True, confidence=0.9, reasoning="r"))


@pytest.fixture()
def both_judges(monkeypatch):
    monkeypatch.setenv(judges.ENV_BASE_URL, "http://gateway.test/v1/chat/completions")
    monkeypatch.setenv(judges.ENV_ANTHROPIC_MODEL, "claude-opus-5-5")
    monkeypatch.setenv(judges.ENV_ANTHROPIC_KEY, "claude-key")
    monkeypatch.setenv(judges.ENV_OPENAI_MODEL, "gpt-6.1-sol")
    monkeypatch.setenv(judges.ENV_OPENAI_KEY, "gpt-key")


def test_the_judge_lane_records_itself_as_skipped_when_off(reviewing, monkeypatch):
    monkeypatch.delenv(review_mod.JUDGE_LIVE_ENV, raising=False)
    result = run_diff_review(_files(), repo=None, model="gpt-6.1-sol")
    rec = _lane(result, "judge")
    assert rec["status"] == verdict.LANE_SKIPPED
    assert "ARGUS_JUDGE_LIVE" in rec["reason"]


def test_a_gpt_reviewer_gets_the_claude_judge_and_it_is_recorded(
        reviewing, both_judges, monkeypatch):
    monkeypatch.setenv(review_mod.JUDGE_LIVE_ENV, "1")
    seen = {}

    def _judge(finding, snippet, reviewer_model=None):
        seen["reviewer"] = reviewer_model
        return {"verdict": "real", "confidence": 0.9}

    from argus import automark
    monkeypatch.setattr(automark, "judge_finding", _judge)

    result = run_diff_review(_files(), repo=None, model="gpt-6.1-sol")
    rec = _lane(result, "judge")
    assert seen["reviewer"] == "gpt-6.1-sol"
    assert rec["status"] == verdict.LANE_RAN
    assert rec["model"] == "claude-opus-5-5"
    assert rec["family"] == judges.ANTHROPIC
    judged = [i for i in result.bugs if i.judge]
    assert judged and judged[0].judge.model == "claude-opus-5-5"


def test_a_claude_reviewer_gets_the_gpt_judge(reviewing, both_judges, monkeypatch):
    monkeypatch.setenv(review_mod.JUDGE_LIVE_ENV, "1")
    from argus import automark
    monkeypatch.setattr(automark, "judge_finding",
                        lambda f, s, reviewer_model=None: {"verdict": "real", "confidence": 0.8})
    result = run_diff_review(_files(), repo=None, model="claude-opus-5-5")
    rec = _lane(result, "judge")
    assert rec["model"] == "gpt-6.1-sol"
    assert rec["family"] == judges.OPENAI


def test_no_decorrelated_judge_is_a_recorded_skip_not_a_silent_pass(
        reviewing, monkeypatch):
    """Judge on, but no counterpart configured. The lane must say so rather than
    leave the operator thinking a second opinion happened."""
    monkeypatch.setenv(review_mod.JUDGE_LIVE_ENV, "1")
    for var in (judges.ENV_ANTHROPIC_KEY, judges.ENV_ANTHROPIC_MODEL,
                judges.ENV_OPENAI_KEY, judges.ENV_OPENAI_MODEL):
        monkeypatch.delenv(var, raising=False)
    result = run_diff_review(_files(), repo=None, model="gpt-6.1-sol")
    rec = _lane(result, "judge")
    assert rec["status"] == verdict.LANE_SKIPPED
    assert "not configured" in rec["reason"]
    assert all(item.judge is None for item in result.bugs)


def test_fast_mode_still_records_the_judge_lane_as_skipped():
    """Fast mode has no reviewer, so it has no finding for a judge to second-guess.
    That is a reason to skip the lane, not a reason to omit it: a consumer reading
    this document must not have to already know which lanes exist to notice that
    one is missing."""
    result = run_diff_review(_files(), repo=None, fast=True)
    rec = _lane(result, "judge")
    assert rec is not None, "the judge lane vanished from fast-mode coverage"
    assert rec["status"] == verdict.LANE_SKIPPED
    assert "fast" in rec["reason"]


@pytest.mark.parametrize("base", [None, "", " \t"])
def test_missing_judge_base_url_records_a_skipped_lane(reviewing, both_judges, monkeypatch, base):
    monkeypatch.setenv(review_mod.JUDGE_LIVE_ENV, "1")
    if base is None:
        monkeypatch.delenv(judges.ENV_BASE_URL, raising=False)
    else:
        monkeypatch.setenv(judges.ENV_BASE_URL, base)
    result = run_diff_review(_files(), repo=None, model="gpt-6.1-sol")
    rec = _lane(result, "judge")
    assert rec["status"] == verdict.LANE_SKIPPED
    assert rec["reason"] == "ARGUS_JUDGE_BASE_URL not set"
    assert all(item.judge is None for item in result.bugs)


def _bug(i):
    from argus.schema import VerifiedFinding
    return VerifiedFinding(
        finding=Finding(file="app.py", line=1, severity=Severity.major, confidence=0.9,
                        title=f"Bug {i}", description="Wrong", proposed_fix="Fix"),
        verdict=Verdict(is_real=True, confidence=0.9, reasoning="confirmed"))


def test_a_judge_lane_where_every_call_failed_is_an_error(monkeypatch):
    import json
    from types import SimpleNamespace
    from unittest.mock import Mock

    import httpx

    from argus import automark

    monkeypatch.setenv(review_mod.JUDGE_LIVE_ENV, "1")
    monkeypatch.setattr(judges, "judge_for", lambda _: SimpleNamespace(model="judge", family="second"))
    monkeypatch.setattr(judges, "family_of", lambda _: "first")
    request = httpx.Request("POST", "https://example.invalid")
    response = httpx.Response(503, text="private body", request=request)
    error = httpx.HTTPStatusError("private body", request=request, response=response)
    monkeypatch.setattr(automark, "judge_finding", Mock(side_effect=[error, error]))
    record = review_mod._judge_lane([_bug(1), _bug(2)], {"app.py": "+x()"}, "reviewer")
    assert record["status"] == verdict.LANE_ERROR
    assert record["reason"] == "http_503 x2"
    assert record["failures"] == 2
    assert record["checks"] == 0
    assert record["eligible"] == 2
    assert record["failure_causes"] == {"http_503": 2}
    assert (record["model"], record["family"], record["reviewer_family"]) == ("judge", "second", "first")
    assert "private" not in json.dumps(record)
    reviewer = {"lane": verdict.LANE_LLM, "status": verdict.LANE_RAN, "files_omitted": 0}
    assert verdict.coverage_level([reviewer, record]) == verdict.COVERAGE_PARTIAL
