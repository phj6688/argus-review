"""Evidence-tier derivation and verdict-document assembly."""

from argus import verdict


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


def test_tier_matrix():
    assert verdict.evidence_tier("probe", None) == "deterministic"
    assert verdict.evidence_tier("llm", "real") == "consensus"
    assert verdict.evidence_tier("llm", "fp") == "disputed"
    assert verdict.evidence_tier("llm", "unsure") == "single_critic"
    assert verdict.evidence_tier("llm", None) == "single_critic"
    assert verdict.evidence_tier(None, None) == "single_critic"


def test_decision_bug_requests_changes():
    assert verdict.decision([_finding()]) == "changes_requested"


def test_decision_disputed_bug_downgrades_to_comment():
    assert verdict.decision([_finding(judge_verdict="fp")]) == "comment"


def test_decision_nit_comments_and_empty_approves():
    nit = _finding(is_bug=False, severity="nit")
    assert verdict.decision([nit]) == "comment"
    assert verdict.decision([]) == "approve"
    unreal = _finding(verdict_is_real=False)
    assert verdict.decision([unreal]) == "approve"


def test_document_baseline_states_and_resolved():
    findings = [
        _finding(fingerprint="argusfp:aa"),
        _finding(id=2, fingerprint="argusfp:bb", severity="minor", is_bug=False),
    ]
    doc = verdict.document(
        {"id": 9, "repo": "o/r", "pr": 4, "model": "m", "status": "done"},
        findings,
        baseline=["argusfp:aa", "argusfp:gone"],
    )
    states = {f["fingerprint"]: f["baseline_state"] for f in doc["findings"]}
    assert states == {"argusfp:aa": "unchanged", "argusfp:bb": "new"}
    assert doc["resolved"] == ["argusfp:gone"]
    assert doc["verdict"] == "changes_requested"
    assert doc["counts"]["bugs"] == 1
    assert doc["counts"]["by_tier"] == {"single_critic": 2}


def test_document_excludes_refuted_findings_and_sorts_bugs_first():
    findings = [
        _finding(id=1, verdict_is_real=False),
        _finding(id=2, fingerprint="argusfp:bb", severity="nit", is_bug=False, confidence=0.9),
        _finding(id=3, fingerprint="argusfp:cc", severity="critical", confidence=0.6),
    ]
    doc = verdict.document({"id": 1, "repo": "o/r", "pr": 1, "model": "m", "status": "done"}, findings)
    assert [f["id"] for f in doc["findings"]] == [3, 2]
    assert doc["source"]["kind"] == "pr"
    assert doc["resolved"] == []


def test_document_diff_source_passthrough():
    doc = verdict.document(
        {"id": 2, "repo": None, "pr": 0, "model": "m", "status": "done",
         "source_kind": "diff", "base_sha": "b", "head_sha": "h"},
        [],
    )
    assert doc["source"] == {"kind": "diff", "repo": None, "pr": None,
                             "base_sha": "b", "head_sha": "h"}
    assert doc["verdict"] == "approve"


def test_document_consensus_tier_from_judge_agreement():
    doc = verdict.document(
        {"id": 3, "repo": "o/r", "pr": 1, "model": "m", "status": "done"},
        [_finding(judge_verdict="real", judge_confidence=0.9, judge_model="glm")],
    )
    item = doc["findings"][0]
    assert item["evidence_tier"] == "consensus"
    assert item["judge"] == {"verdict": "real", "confidence": 0.9, "model": "glm"}
