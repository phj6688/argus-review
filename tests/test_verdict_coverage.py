"""Coverage in the verdict document: a clean result must prove the reviewer looked.

The defect this pins: fast mode ran zero checks on a diff carrying a SQL injection
and returned `approve` with `engine.reviewer_model` naming a model that never ran.
Absence of findings is only evidence of absence when coverage says so.
"""

import pytest

from argus import verdict

ALL_LEVELS = (verdict.COVERAGE_FULL, verdict.COVERAGE_PARTIAL,
              verdict.COVERAGE_NONE, verdict.COVERAGE_UNKNOWN)

# How much each verdict restrains a merge. Coverage is allowed to move a result up
# this scale and never down: knowing less about a change cannot make it safer.
_BLOCKING_RANK = {
    "approve": 0,
    verdict.INSUFFICIENT_EVIDENCE: 1,
    "comment": 2,
    "changes_requested": 3,
}


def _finding(**overrides) -> dict:
    row = {
        "id": 1, "fingerprint": "argusfp:aa", "file": "a.py", "line": 3,
        "severity": "major", "confidence": 0.8, "title": "t", "description": "d",
        "proposed_fix": "f", "rule_id": None, "lane": "llm",
        "verdict_is_real": True, "verdict_confidence": 0.7, "is_bug": True,
        "judge_verdict": None, "judge_confidence": None, "judge_model": None,
    }
    row.update(overrides)
    return row


def _coverage(level: str) -> dict:
    return {"level": level, "lanes": []}


def test_no_findings_under_zero_coverage_is_not_approve():
    assert verdict.decision([], _coverage(verdict.COVERAGE_NONE)) == \
        verdict.INSUFFICIENT_EVIDENCE


def test_no_findings_under_partial_coverage_is_not_approve():
    assert verdict.decision([], _coverage(verdict.COVERAGE_PARTIAL)) == \
        verdict.INSUFFICIENT_EVIDENCE


def test_no_findings_under_full_coverage_approves():
    assert verdict.decision([], _coverage(verdict.COVERAGE_FULL)) == "approve"


def test_legacy_row_without_coverage_keeps_approving():
    """439 historical runs predate the coverage column; they must not all flip."""
    assert verdict.decision([]) == "approve"
    assert verdict.decision([], _coverage(verdict.COVERAGE_UNKNOWN)) == "approve"


def test_findings_still_request_changes_under_zero_coverage():
    """Evidence found is evidence found. A probe hit outranks thin coverage."""
    hit = _finding(lane="probe")
    assert verdict.decision([hit], _coverage(verdict.COVERAGE_NONE)) == "changes_requested"


def test_refuted_findings_do_not_rescue_thin_coverage():
    unreal = _finding(verdict_is_real=False)
    assert verdict.decision([unreal], _coverage(verdict.COVERAGE_NONE)) == \
        verdict.INSUFFICIENT_EVIDENCE


def test_document_carries_the_coverage_block():
    doc = verdict.document(
        {"id": 7, "repo": None, "pr": 0, "model": "gpt-6.1-sol", "status": "done",
         "source_kind": "diff",
         "coverage_json": '{"level": "none", "lanes": ['
                          '{"lane": "llm", "status": "skipped", "reason": "mode=fast"}]}'},
        [],
    )
    assert doc["verdict"] == verdict.INSUFFICIENT_EVIDENCE
    assert doc["coverage"]["level"] == verdict.COVERAGE_NONE
    assert doc["coverage"]["lanes"] == [
        {"lane": "llm", "status": "skipped", "reason": "mode=fast"}]


def test_document_reports_no_reviewer_model_when_no_model_ran():
    """The row stores the configured model; fast mode never calls it. Reporting it
    asserts provenance that did not happen."""
    doc = verdict.document(
        {"id": 8, "repo": None, "pr": 0, "model": "gpt-6.1-sol", "status": "done",
         "source_kind": "diff",
         "coverage_json": '{"level": "none", "lanes": ['
                          '{"lane": "llm", "status": "skipped", "reason": "mode=fast"}]}'},
        [],
    )
    assert doc["engine"]["reviewer_model"] is None


def test_document_reports_reviewer_model_when_the_llm_lane_ran():
    doc = verdict.document(
        {"id": 9, "repo": "o/r", "pr": 1, "model": "gpt-6.1-sol", "status": "done",
         "coverage_json": '{"level": "full", "lanes": ['
                          '{"lane": "llm", "status": "ran"}]}'},
        [_finding()],
    )
    assert doc["engine"]["reviewer_model"] == "gpt-6.1-sol"


def test_document_reports_the_fallback_model_that_actually_ran():
    """On a fallback the row keeps the primary in `model` and records the switch
    separately, so the row is the wrong source. The lane record is the one that
    names what read the diff."""
    doc = verdict.document(
        {"id": 12, "repo": "o/r", "pr": 3, "model": "gpt-6.1-sol", "status": "done",
         "model_used": "claude-sonnet-5-5", "fallback_from": "gpt-6.1-sol",
         "coverage_json": '{"level": "full", "lanes": ['
                          '{"lane": "llm", "status": "ran", "model": "claude-sonnet-5-5"}]}'},
        [_finding()],
    )
    assert doc["engine"]["reviewer_model"] == "claude-sonnet-5-5"


def test_document_labels_legacy_rows_unknown_rather_than_inventing_coverage():
    doc = verdict.document(
        {"id": 10, "repo": "o/r", "pr": 2, "model": "m", "status": "done"}, [])
    assert doc["coverage"]["level"] == verdict.COVERAGE_UNKNOWN
    assert doc["verdict"] == "approve"
    assert doc["engine"]["reviewer_model"] == "m"


def test_a_reviewer_that_skipped_files_is_not_full_coverage():
    """compress_diff drops over-budget files into a names-only section, so the
    reviewer never sees their contents. A lane that ran on a truncated diff has
    not covered the change, and a clean result on it is not a pass."""
    partial_read = [{"lane": verdict.LANE_LLM, "status": verdict.LANE_RAN,
                     "model": "m", "files_reviewed": 4, "files_omitted": 3}]
    assert verdict.coverage_level(partial_read) == verdict.COVERAGE_PARTIAL
    assert verdict.decision([], {"level": verdict.COVERAGE_PARTIAL}) == \
        verdict.INSUFFICIENT_EVIDENCE


def test_a_reviewer_that_read_everything_is_full_coverage():
    whole = [{"lane": verdict.LANE_LLM, "status": verdict.LANE_RAN,
              "model": "m", "files_reviewed": 4, "files_omitted": 0}]
    assert verdict.coverage_level(whole) == verdict.COVERAGE_FULL


def test_a_failed_reviewer_lane_reports_no_model():
    """A lane that errored completed no invocation, so naming its configured model
    is the same false provenance as naming it on a skipped lane."""
    doc = verdict.document(
        {"id": 13, "repo": "o/r", "pr": 4, "model": "gpt-6.1-sol", "status": "done",
         "coverage_json": '{"level": "none", "lanes": ['
                          '{"lane": "llm", "status": "error", "reason": "timeout"}]}'},
        [],
    )
    assert doc["engine"]["reviewer_model"] is None
    assert doc["verdict"] == verdict.INSUFFICIENT_EVIDENCE


def test_a_crashed_run_never_reports_a_pass():
    """A run that raised never reached save_result, so it has no lane records and
    NULL coverage — which the legacy grandfathering reads as `approve`. The run's
    own status is the authority here: a review that did not complete produced no
    evidence about the code, whatever the absence of findings looks like."""
    doc = verdict.document(
        {"id": 20, "repo": "o/r", "pr": 5, "model": "gpt-6.1-sol", "status": "error",
         "error": "InternalServerError", "error_class": "InternalServerError"},
        [],
    )
    assert doc["verdict"] == verdict.INSUFFICIENT_EVIDENCE
    assert doc["coverage"]["level"] == verdict.COVERAGE_NONE
    assert doc["engine"]["reviewer_model"] is None


def test_a_crashed_run_says_why_in_the_coverage_block():
    doc = verdict.document(
        {"id": 21, "repo": "o/r", "pr": 6, "model": "m", "status": "error",
         "error_class": "RateLimitError"},
        [],
    )
    lanes = {rec["lane"]: rec for rec in doc["coverage"]["lanes"]}
    assert lanes[verdict.LANE_LLM]["status"] == "error"
    assert "RateLimitError" in lanes[verdict.LANE_LLM]["reason"]


def test_a_completed_run_is_unaffected_by_the_status_rule():
    doc = verdict.document(
        {"id": 22, "repo": "o/r", "pr": 7, "model": "m", "status": "done"}, [])
    assert doc["verdict"] == "approve"
    assert doc["coverage"]["level"] == verdict.COVERAGE_UNKNOWN


@pytest.mark.parametrize("level", ALL_LEVELS)
@pytest.mark.parametrize("findings, floor", [
    ([], "approve"),
    ([_finding(verdict_is_real=False)], "approve"),
    ([_finding(is_bug=False, severity="nit")], "comment"),
    ([_finding(judge_verdict="fp")], "comment"),
    ([_finding()], "changes_requested"),
    ([_finding(lane="probe", severity="critical")], "changes_requested"),
    ([_finding(), _finding(id=2, is_bug=False, severity="nit")], "changes_requested"),
])
def test_coverage_can_only_make_a_verdict_more_blocking(findings, floor, level):
    """The invariant the whole design rests on. Coverage answers "did you look",
    which can withhold a pass but must never cancel a finding: a required lane
    that has not reported cannot talk seven confirmed bugs down to approve.
    Findings are therefore resolved before coverage is consulted at all."""
    result = verdict.decision(findings, {"level": level, "lanes": []})
    assert _BLOCKING_RANK[result] >= _BLOCKING_RANK[floor], (
        f"coverage={level} downgraded {floor} to {result}")


def test_coverage_level_derives_from_lane_records():
    """The engine records lanes; the level is derived, so the two can never disagree."""
    assert verdict.coverage_level([{"lane": "llm", "status": "ran"}]) == verdict.COVERAGE_FULL
    assert verdict.coverage_level([
        {"lane": "probe", "status": "ran", "checks": 6},
        {"lane": "llm", "status": "skipped", "reason": "mode=fast"},
    ]) == verdict.COVERAGE_PARTIAL
    assert verdict.coverage_level([
        {"lane": "probe", "status": "ran", "checks": 0},
        {"lane": "llm", "status": "skipped", "reason": "mode=fast"},
    ]) == verdict.COVERAGE_NONE
    assert verdict.coverage_level([]) == verdict.COVERAGE_NONE
