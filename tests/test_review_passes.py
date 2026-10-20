import importlib

import pytest

from argus import review


BASE_INSTRUCTION = "Review the changes above and report every defect via report_findings."
GAP_INSTRUCTION = (
    "Review the change again and report ONLY defects that are not already listed above. "
    "Be especially thorough on the classes reviewers most often miss: tests that do not "
    "exercise or assert the changed behaviour, docs, comments, changelogs or descriptions "
    "that contradict the code, unknown or invalid input and flags accepted silently, "
    "failures recorded as success, missing timeouts, and boundary conditions. Report "
    "nothing rather than restating a listed finding. Report all findings in one "
    "report_findings call."
)
GOOD = dict(file="a.py", line=10, severity="major", confidence=0.9,
            title="Missing request timeout", description="The request can hang",
            proposed_fix="Set a timeout", end_line=None, category="resources",
            evidence="request(url)", rule_id=None)


def _stub(monkeypatch, *replies):
    responses = iter(replies)
    calls = []

    def complete(model, system, user, tool):
        calls.append((model, system, user, tool))
        reply = next(responses)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(review, "complete_tool", complete)
    return calls


@pytest.mark.parametrize("first", [[GOOD], []], ids=["findings", "none"])
def test_second_pass_preserves_context_and_replaces_final_instruction(monkeypatch, first):
    monkeypatch.setattr(review, "REVIEW_PASSES", 2)
    calls = _stub(monkeypatch, {"findings": first}, {"findings": []})
    parsed = review._deep_review("NUMBERED FILES", "HOUSE", "model", "INTENT", "PR BODY")
    assert len(calls) == 2
    assert all(c[0] == "model" and c[1] == review.REVIEW_SYSTEM
               and c[3] == review.REVIEW_TOOL for c in calls)
    first_user, second_user = [c[2] for c in calls]
    assert first_user.endswith(BASE_INSTRUCTION)
    prefix = first_user.removesuffix(BASE_INSTRUCTION)
    listing = "- a.py:10 [major] Missing request timeout" if first else "- none"
    assert second_user == (
        prefix + "# Findings already reported by a first reviewer\n" + listing
        + "\n\n" + GAP_INSTRUCTION)
    assert BASE_INSTRUCTION not in second_user
    assert [f.title for f in parsed.findings] == [f["title"] for f in first]
    assert parsed.parsing_stats["findings_per_pass"] == [len(first), 0]


@pytest.mark.parametrize("changes,duplicate", [
    ({"title": "REQUEST TIMEOUT missing!", "evidence": "different()", "line": 13}, True),
    ({"title": "Missing timeout", "evidence": "different()", "line": 7}, True),
    ({"title": "Missing optional timeout", "evidence": "different()"}, True),
    ({"title": "Missing timeout for slow services", "evidence": "different()"}, False),
    ({"title": "Wrong destination", "evidence": "  request(url)  \nother()"}, True),
    ({"title": "Wrong destination", "evidence": "\nrequest(url)"}, False),
    ({"file": "b.py"}, False),
    ({"line": 14}, False),
    ({"line": 6}, False),
    ({"title": "Wrong destination", "evidence": "other()\nrequest(url)"}, False),
    ({"title": "Wrong destination", "evidence": ""}, False),
])
def test_later_duplicates_keep_earlier_finding(monkeypatch, changes, duplicate):
    monkeypatch.setattr(review, "REVIEW_PASSES", 2)
    later = {**GOOD, **changes, "description": "Later description"}
    _stub(monkeypatch, {"findings": [GOOD]}, {"findings": [later]})
    parsed = review._deep_review("diff", None, "model")
    assert len(parsed.findings) == (1 if duplicate else 2)
    assert parsed.findings[0].description == "The request can hang"
    assert parsed.parsing_stats["duplicates_dropped"] == int(duplicate)
    assert parsed.parsing_stats["findings_per_pass"] == [1, 1]


def test_empty_evidence_and_titles_do_not_make_unrelated_findings_duplicates(monkeypatch):
    monkeypatch.setattr(review, "REVIEW_PASSES", 2)
    _stub(monkeypatch,
          {"findings": [{**GOOD, "title": "!!!", "evidence": " "}]},
          {"findings": [{**GOOD, "title": "???", "evidence": ""}]})
    parsed = review._deep_review("diff", None, "model")
    assert len(parsed.findings) == 2
    assert parsed.parsing_stats["duplicates_dropped"] == 0


def test_third_pass_lists_merged_findings_and_deduplicates_against_both_passes(monkeypatch):
    monkeypatch.setattr(review, "REVIEW_PASSES", 3, raising=False)
    second = {**GOOD, "file": "b.py", "title": "Silent invalid flag"}
    third = {**GOOD, "file": "c.py", "title": "Incorrect boundary"}
    calls = _stub(monkeypatch, {"findings": [GOOD]}, {"findings": [GOOD, second]},
                  {"findings": [GOOD, second, third]})
    parsed = review._deep_review("diff", None, "model")
    assert len(calls) == 3
    assert "- a.py:10 [major] Missing request timeout" in calls[2][2]
    assert "- b.py:10 [major] Silent invalid flag" in calls[2][2]
    assert calls[2][2].endswith(GAP_INSTRUCTION)
    assert [f.file for f in parsed.findings] == ["a.py", "b.py", "c.py"]
    assert parsed.parsing_stats == {
        "findings_received": 6, "findings_repaired": 0, "findings_dropped": 0,
        "passes_run": 3, "findings_per_pass": [1, 2, 3],
        "duplicates_dropped": 3, "later_pass_errors": 0,
    }


@pytest.mark.parametrize("failure", [RuntimeError("unavailable"), None, {}, {"findings": "bad"}])
def test_later_failure_preserves_first_pass_and_records_warning(monkeypatch, caplog, failure):
    monkeypatch.setattr(review, "REVIEW_PASSES", 2)
    calls = _stub(monkeypatch, {"findings": [GOOD]}, failure)
    parsed = review._deep_review("diff", None, "model")
    assert len(calls) == 2
    assert [f.title for f in parsed.findings] == ["Missing request timeout"]
    assert parsed.parsing_stats == {
        "findings_received": 1, "findings_repaired": 0, "findings_dropped": 0,
        "passes_run": 2, "findings_per_pass": [1],
        "duplicates_dropped": 0, "later_pass_errors": 1,
    }
    assert any(r.levelname == "WARNING" and "pass 2" in r.message for r in caplog.records)


def test_third_pass_runs_after_second_pass_failure(monkeypatch):
    monkeypatch.setattr(review, "REVIEW_PASSES", 3, raising=False)
    calls = _stub(monkeypatch, {"findings": [GOOD]}, RuntimeError("unavailable"),
                  {"findings": [{**GOOD, "file": "b.py"}]})
    parsed = review._deep_review("diff", None, "model")
    assert "- a.py:10 [major] Missing request timeout" in calls[2][2]
    assert [f.file for f in parsed.findings] == ["a.py", "b.py"]
    assert parsed.parsing_stats["passes_run"] == 3
    assert parsed.parsing_stats["findings_per_pass"] == [1, 1]
    assert parsed.parsing_stats["later_pass_errors"] == 1


@pytest.mark.parametrize("failure,error", [(RuntimeError("unavailable"), RuntimeError),
                                          ({}, ValueError)])
def test_first_pass_failures_still_raise(monkeypatch, failure, error):
    calls = _stub(monkeypatch, failure)
    with pytest.raises(error):
        review._deep_review("diff", None, "model")
    assert len(calls) == 1


def test_parsing_stats_sum_all_passes_before_deduplication(monkeypatch):
    monkeypatch.setattr(review, "REVIEW_PASSES", 2)
    _stub(monkeypatch, {"findings": [GOOD, {"title": "no file"}]},
          {"findings": [{**GOOD, "line": "10"}, None,
                        {**GOOD, "file": "b.py", "severity": "MAJOR"}]})
    parsed = review._deep_review("diff", None, "model")
    assert [f.file for f in parsed.findings] == ["a.py", "b.py"]
    assert parsed.parsing_stats == {
        "findings_received": 5, "findings_repaired": 2, "findings_dropped": 2,
        "passes_run": 2, "findings_per_pass": [1, 2],
        "duplicates_dropped": 1, "later_pass_errors": 0,
    }


def test_default_runs_one_reviewer_call_when_environment_is_unset(monkeypatch):
    try:
        with monkeypatch.context() as patch:
            patch.delenv("ARGUS_REVIEW_PASSES", raising=False)
            importlib.reload(review)
            calls = _stub(patch, {"findings": []})
            parsed = review._deep_review("diff", None, "model")
            assert len(calls) == 1
            assert parsed.parsing_stats["passes_run"] == 1
            assert parsed.parsing_stats["findings_per_pass"] == [0]
    finally:
        importlib.reload(review)


@pytest.mark.parametrize("value,want", [("1", 1), ("2", 2), ("3", 3),
                                       ("", 1), ("bad", 1), ("1.5", 1),
                                       ("0", 1), ("4", 1), ("-1", 1)])
def test_environment_controls_number_of_passes(monkeypatch, value, want):
    try:
        with monkeypatch.context() as patch:
            patch.setenv("ARGUS_REVIEW_PASSES", value)
            importlib.reload(review)
            calls = _stub(patch, *[{"findings": []} for _ in range(want)])
            parsed = review._deep_review("diff", None, "model")
            assert len(calls) == want
            assert parsed.parsing_stats["passes_run"] == want
            assert parsed.parsing_stats["findings_per_pass"] == [0] * want
    finally:
        importlib.reload(review)
