import pytest
from pydantic import ValidationError

from argus import review
from argus.review import rank_and_cap
from argus.schema import Finding, Severity, Verdict, VerifiedFinding


def _finding(severity: Severity = Severity.major, confidence: float = 0.9, line: int = 1) -> Finding:
    return Finding(
        file="a.py", line=line, severity=severity, confidence=confidence,
        title="t", description="d", proposed_fix="f",
    )


def _vf(severity: Severity, confidence: float, is_real: bool, line: int = 1) -> VerifiedFinding:
    return VerifiedFinding(
        finding=_finding(severity, confidence, line),
        verdict=Verdict(is_real=is_real, confidence=0.9, reasoning="r"),
    )


def test_rank_and_cap_drops_unverified():
    bugs, nits = rank_and_cap([_vf(Severity.major, 0.9, is_real=False)])
    assert bugs == []
    assert nits == []


def test_rank_and_cap_separates_bugs_from_nits_and_orders_by_severity():
    items = [
        _vf(Severity.nit, 0.9, is_real=True),
        _vf(Severity.critical, 0.5, is_real=True),
        _vf(Severity.major, 0.9, is_real=True),
    ]
    bugs, nits = rank_and_cap(items)
    assert [b.finding.severity for b in bugs] == [Severity.critical, Severity.major]
    assert [n.finding.severity for n in nits] == [Severity.nit]


def test_rank_and_cap_orders_by_confidence_within_severity():
    items = [_vf(Severity.major, 0.3, is_real=True, line=1), _vf(Severity.major, 0.8, is_real=True, line=2)]
    bugs, _ = rank_and_cap(items)
    assert [b.finding.confidence for b in bugs] == [0.8, 0.3]


def test_rank_and_cap_caps_bug_count():
    items = [_vf(Severity.major, 0.9, is_real=True, line=i) for i in range(1, 15)]
    bugs, _ = rank_and_cap(items, bug_cap=10)
    assert len(bugs) == 10


def test_rank_and_cap_honors_configured_bug_cap():
    # Evaluation #2 (unit level): a cap of 3 returns at most 3 bug-class findings.
    items = [_vf(Severity.major, 0.9, is_real=True, line=i) for i in range(1, 15)]
    bugs, _ = rank_and_cap(items, bug_cap=3)
    assert len(bugs) == 3


def test_run_review_accepts_bug_cap_passthrough():
    import inspect

    from argus.review import run_review

    params = inspect.signature(run_review).parameters
    assert "bug_cap" in params, "run_review must accept a bug_cap passthrough"
    assert params["bug_cap"].default == 10


def test_verdict_parses_without_reasoning():
    # Judges sometimes omit reasoning despite the tool schema; that must not
    # kill the review (4 permanent review deaths before this default existed).
    verdict = Verdict.model_validate({"is_real": True, "confidence": 0.7})
    assert verdict.reasoning == ""


def test_verify_retries_judge_once_on_invalid_payload(monkeypatch):
    payloads = [
        {"confidence": 0.9},  # invalid: is_real missing
        {"is_real": False, "confidence": 0.9, "reasoning": "refuted"},
    ]
    calls = []

    def fake_complete_tool(model, system, user, tool):
        calls.append(tool.name)
        return payloads[len(calls) - 1]

    monkeypatch.setattr(review, "complete_tool", fake_complete_tool)
    verdict = review._verify(_finding(), "diff", "model")
    assert calls == ["report_verdict", "report_verdict"]
    assert verdict.is_real is False
    assert verdict.reasoning == "refuted"


def test_verify_raises_when_retry_payload_is_also_invalid(monkeypatch):
    calls = []

    def fake_complete_tool(model, system, user, tool):
        calls.append(tool.name)
        return {"confidence": 0.9}  # invalid: is_real missing

    monkeypatch.setattr(review, "complete_tool", fake_complete_tool)
    with pytest.raises(ValidationError):
        review._verify(_finding(), "diff", "model")
    assert len(calls) == 2, "exactly one retry, then the error path"


def test_deep_review_repairs_findings_and_logs_counts_once(monkeypatch, caplog):
    monkeypatch.setattr(review, "REVIEW_PASSES", 2)
    payload = {"findings": [
        {"file": "a.py", "line": 2, "severity": "major", "confidence": 0.8,
         "description": "Wrong return value. Extra detail.", "proposed_fix": "Fix it"},
        {"title": "No location"},
    ]}
    payloads = iter([payload, {"findings": []}])
    monkeypatch.setattr(review, "complete_tool", lambda *args: next(payloads))
    parsed = review._deep_review("diff", None, "model")
    assert [f.title for f in parsed.findings] == ["Wrong return value."]
    assert parsed.parsing_stats == {
        "findings_received": 2, "findings_repaired": 1, "findings_dropped": 1,
        "passes_run": 2, "findings_per_pass": [1, 0],
        "duplicates_dropped": 0, "later_pass_errors": 0,
    }
    warnings = [r for r in caplog.records if r.name == "argus.review" and r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "received=2" in warnings[0].message
    assert "repaired=1" in warnings[0].message
    assert "dropped=1" in warnings[0].message
