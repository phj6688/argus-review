"""Tests for the measurement-foundation additions: operator marks (ground truth),
the rule-grounding shadow gate, raw single-pass capture, and the distinct-PR count.
"""

import pytest
from fastapi.testclient import TestClient

from argus import rules
from argus.api import db, main, metrics
from argus.review import ReviewResult
from argus.schema import Finding, Severity, Verdict, VerifiedFinding


def _vf(severity: Severity, line: int, *, is_real: bool = True, rule_id: str | None = "R1") -> VerifiedFinding:
    return VerifiedFinding(
        finding=Finding(
            file="src/app.py", line=line, severity=severity, confidence=0.8,
            title=f"issue {line}", description="d", proposed_fix="f", rule_id=rule_id,
        ),
        verdict=Verdict(is_real=is_real, confidence=0.9, reasoning="r"),
    )


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "argus.db"
    monkeypatch.setenv("ARGUS_DB_PATH", str(path))
    db.init_db(str(path))
    return str(path)


@pytest.fixture
def client(db_path):
    with TestClient(main.app) as test_client:
        yield test_client


def _seed_review_with_findings(path, repo="example/app", pr=7):
    review_id = db.create_review(path, repo, pr, "claude-x")
    result = ReviewResult(
        raw_count=3,
        bugs=[_vf(Severity.critical, 10), _vf(Severity.major, 20, rule_id=None)],
        nits=[_vf(Severity.nit, 30)],
        dropped=0,
        all_findings=[
            _vf(Severity.critical, 10), _vf(Severity.major, 20, rule_id=None),
            _vf(Severity.nit, 30), _vf(Severity.minor, 40, is_real=False),
        ],
    )
    db.save_result(path, review_id, result)
    return review_id


# ---------------------------------------------------------------- rule grounding

def test_is_grounded_resolves_seed_rules_and_rejects_invented():
    rules.reset_cache()
    assert rules.is_grounded("transaction-atomicity") is True
    assert rules.is_grounded("AUTH-GATE") is True          # case-insensitive
    assert rules.is_grounded("  loopback-binding ") is True  # trimmed
    assert rules.is_grounded("totally-made-up-rule") is False
    assert rules.is_grounded(None) is False
    assert rules.is_grounded("") is False


# ---------------------------------------------------------------- marks (ground truth)

def test_add_mark_and_latest_wins(db_path):
    review_id = _seed_review_with_findings(db_path)
    fid = db.get_findings(db_path, review_id)[0]["id"]
    db.add_mark(db_path, fid, "unsure")
    db.add_mark(db_path, fid, "real", note="confirmed by reading the source")
    latest = db.latest_marks_for_review(db_path, review_id)
    assert latest[fid]["mark"] == "real"          # newest label wins
    assert latest[fid]["note"] == "confirmed by reading the source"


def test_add_mark_rejects_bad_value_and_missing_finding(db_path):
    review_id = _seed_review_with_findings(db_path)
    fid = db.get_findings(db_path, review_id)[0]["id"]
    with pytest.raises(ValueError):
        db.add_mark(db_path, fid, "definitely")
    with pytest.raises(KeyError):
        db.add_mark(db_path, 999999, "real")


def test_mark_precision_excludes_non_verdicts(db_path):
    review_id = _seed_review_with_findings(db_path)
    fids = [f["id"] for f in db.get_findings(db_path, review_id)]
    db.add_mark(db_path, fids[0], "real")
    db.add_mark(db_path, fids[1], "real")
    db.add_mark(db_path, fids[2], "fp")
    # stale/cant_locate are reported but not part of the precision denominator.
    prec = metrics.mark_precision(db_path)
    assert prec["labeled"] == 3
    assert prec["located"] == 3          # 2 real + 1 fp
    assert prec["precision"] == round(2 / 3, 3)
    assert prec["real"] == 2 and prec["fp"] == 1


def test_mark_precision_none_without_labels(db_path):
    _seed_review_with_findings(db_path)
    assert metrics.mark_precision(db_path)["precision"] is None


def test_mark_endpoint_records_and_surfaces_label(client, db_path):
    review_id = _seed_review_with_findings(db_path)
    fid = db.get_findings(db_path, review_id)[0]["id"]
    resp = client.post(f"/findings/{fid}/mark", json={"mark": "real", "note": "verified"})
    assert resp.status_code == 201
    detail = client.get(f"/reviews/{review_id}").json()
    marked = next(b for b in detail["bugs"] if b["id"] == fid)
    assert marked["mark"]["mark"] == "real"
    assert marked["mark"]["note"] == "verified"


def test_mark_endpoint_records_provenance_marker(client, db_path):
    # The marker carries provenance (e.g. automark:judge) so the gold corpus and
    # human precision can exclude auto-marks; it must round-trip through the API.
    import sqlite3
    review_id = _seed_review_with_findings(db_path)
    fid = db.get_findings(db_path, review_id)[0]["id"]
    resp = client.post(f"/findings/{fid}/mark",
                       json={"mark": "fp", "marker": "automark:judge", "note": "auto"})
    assert resp.status_code == 201
    stored = sqlite3.connect(db_path).execute(
        "SELECT marker FROM marks WHERE finding_id=? ORDER BY id DESC LIMIT 1", (fid,)
    ).fetchone()[0]
    assert stored == "automark:judge"


def test_mark_endpoint_validates(client, db_path):
    review_id = _seed_review_with_findings(db_path)
    fid = db.get_findings(db_path, review_id)[0]["id"]
    assert client.post(f"/findings/{fid}/mark", json={"mark": "nope"}).status_code == 422
    assert client.post("/findings/999999/mark", json={"mark": "real"}).status_code == 404


# ------------------------------------------------------- raw capture + distinct PRs

def test_save_result_persists_raw_single_pass_findings(db_path):
    review_id = _seed_review_with_findings(db_path)
    conn = db.connect(db_path)
    try:
        raw = conn.execute(
            "SELECT COUNT(*) AS n FROM findings_raw WHERE review_id = ?", (review_id,)
        ).fetchone()["n"]
        kept = conn.execute(
            "SELECT COUNT(*) AS n FROM findings WHERE review_id = ?", (review_id,)
        ).fetchone()["n"]
    finally:
        conn.close()
    assert raw == 4   # every single-pass finding, including the verifier-refuted one
    assert kept == 3  # only the verified survivors reach findings


def test_distinct_pr_count_collapses_re_reviews(db_path):
    # Same PR reviewed on two pushes -> two done reviews, one distinct PR.
    for sha in ("sha1", "sha2"):
        rid = db.create_review(db_path, "example/repo", 5, "claude-x", head_sha=sha)
        db.save_result(db_path, rid, ReviewResult(raw_count=0))
    rid2 = db.create_review(db_path, "example/repo", 6, "claude-x", head_sha="sha3")
    db.save_result(db_path, rid2, ReviewResult(raw_count=0))
    assert metrics.done_review_count(db_path) == 3
    assert metrics.distinct_pr_count(db_path) == 2


def test_mark_precision_excludes_namespaced_machine_markers(db_path):
    # Machine provenance is namespaced (automark:*, agent:*); a bare marker is a
    # human identity. Only the humans may move the precision number.
    review_id = _seed_review_with_findings(db_path)
    fids = [f["id"] for f in db.get_findings(db_path, review_id)]
    db.add_mark(db_path, fids[0], "real", marker="alice")
    db.add_mark(db_path, fids[1], "real", marker="automark:judge")
    db.add_mark(db_path, fids[2], "fp", marker="agent:ship-worker")
    prec = metrics.mark_precision(db_path)
    assert prec["labeled"] == 1
    assert prec["precision"] == 1.0
    combined = metrics.mark_precision(db_path, include_auto=True)
    assert combined["labeled"] == 3
