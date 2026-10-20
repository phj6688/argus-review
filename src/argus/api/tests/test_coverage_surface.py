"""End to end: a fast review must not read as a pass, over HTTP.

This is the reproduction the whole change exists for. A diff carrying a SQL
injection and a shell injection went through mode=fast and came back
verdict=approve, reviewer_model=gpt-5.6-sol, status=done, in 0.1s.
"""

import pytest
from fastapi.testclient import TestClient

from argus import verdict as verdict_mod
from argus.api import db, main
from argus.review import ReviewResult
from argus.schema import Finding, Severity, Verdict, VerifiedFinding

BAD_DIFF = """diff --git a/bad.py b/bad.py
new file mode 100644
index 0000000..36941f0
--- /dev/null
+++ b/bad.py
@@ -0,0 +1,5 @@
+import os
+def get_user(conn, name):
+    conn.execute("SELECT * FROM users WHERE name = '" + name + "'")
+def run(cmd):
+    os.system("ls " + cmd)
"""

FAST_COVERAGE = [
    {"lane": "probe", "status": "ran", "checks": 0, "eligible": 6,
     "note": "no changed file matches any probe glob"},
    {"lane": "llm", "status": "skipped", "reason": "mode=fast: the reviewer lane is not run"},
]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_DB_PATH", str(tmp_path / "argus.db"))
    db.init_db()
    with TestClient(main.app) as test_client:
        yield test_client


def test_fast_mode_on_a_bad_diff_does_not_return_approve(client, monkeypatch):
    monkeypatch.setattr(
        main, "run_diff_review",
        lambda files, repo, head_sha, model, bug_cap, fast=False: ReviewResult(
            raw_count=0, bugs=[], nits=[], coverage=FAST_COVERAGE),
    )
    body = client.post("/review-diff", json={"diff": BAD_DIFF, "mode": "fast"}).json()
    assert body["verdict"] == verdict_mod.INSUFFICIENT_EVIDENCE
    assert body["coverage"]["level"] == verdict_mod.COVERAGE_NONE
    assert body["engine"]["reviewer_model"] is None


def test_the_coverage_block_says_which_lane_was_skipped_and_why(client, monkeypatch):
    monkeypatch.setattr(
        main, "run_diff_review",
        lambda files, repo, head_sha, model, bug_cap, fast=False: ReviewResult(
            raw_count=0, bugs=[], nits=[], coverage=FAST_COVERAGE),
    )
    body = client.post("/review-diff", json={"diff": BAD_DIFF, "mode": "fast"}).json()
    lanes = {rec["lane"]: rec for rec in body["coverage"]["lanes"]}
    assert lanes["llm"]["status"] == "skipped"
    assert "fast" in lanes["llm"]["reason"]
    assert lanes["probe"]["checks"] == 0
    assert lanes["probe"]["eligible"] == 6


def test_coverage_survives_the_round_trip_through_the_verdict_endpoint(client, monkeypatch):
    monkeypatch.setattr(
        main, "run_diff_review",
        lambda files, repo, head_sha, model, bug_cap, fast=False: ReviewResult(
            raw_count=0, bugs=[], nits=[], coverage=FAST_COVERAGE),
    )
    created = client.post("/review-diff", json={"diff": BAD_DIFF, "mode": "fast"}).json()
    fetched = client.get(f"/reviews/{created['review_id']}/verdict").json()
    assert fetched["verdict"] == verdict_mod.INSUFFICIENT_EVIDENCE
    assert fetched["coverage"] == created["coverage"]
    assert fetched["engine"]["reviewer_model"] is None


def test_a_probe_hit_still_requests_changes_on_a_thin_lane_set(client, monkeypatch):
    """Coverage gates the absence of findings, never their presence."""
    hit = VerifiedFinding(
        finding=Finding(file="deploy/docker-compose.yml", line=2, severity=Severity.major,
                        confidence=1.0, title="probe: app_bound_to_all_interfaces",
                        description="d", proposed_fix="bind 127.0.0.1"),
        verdict=Verdict(is_real=True, confidence=1.0, reasoning="probe matched"),
        lane="probe", fingerprint="argusfp:bind",
    )
    covered = [{"lane": "probe", "status": "ran", "checks": 1, "eligible": 6},
               {"lane": "llm", "status": "skipped", "reason": "mode=fast"}]
    monkeypatch.setattr(
        main, "run_diff_review",
        lambda files, repo, head_sha, model, bug_cap, fast=False: ReviewResult(
            raw_count=0, bugs=[hit], nits=[], coverage=covered),
    )
    body = client.post("/review-diff", json={"diff": BAD_DIFF, "mode": "fast"}).json()
    assert body["verdict"] == "changes_requested"
    assert body["coverage"]["level"] == verdict_mod.COVERAGE_PARTIAL


def test_full_mode_still_approves_a_clean_diff(client, monkeypatch):
    ran = [{"lane": "probe", "status": "ran", "checks": 6, "eligible": 6},
           {"lane": "llm", "status": "ran", "model": "gpt-6.1-sol"}]
    monkeypatch.setattr(
        main, "run_diff_review",
        lambda files, repo, head_sha, model, bug_cap: ReviewResult(
            raw_count=0, bugs=[], nits=[], coverage=ran),
    )
    created = client.post("/review-diff", json={"diff": BAD_DIFF}).json()
    doc = client.get(f"/reviews/{created['id']}/verdict").json()
    assert doc["verdict"] == "approve"
    assert doc["coverage"]["level"] == verdict_mod.COVERAGE_FULL
    assert doc["engine"]["reviewer_model"] == "gpt-6.1-sol"


def test_review_detail_and_verdict_document_never_disagree(client, monkeypatch):
    """The console reads /reviews/{id}.decision, agents read /verdict.verdict.
    Two names for one judgement: they must resolve identically, or the console
    shows a pass the machine surface refuses to give."""
    monkeypatch.setattr(
        main, "run_diff_review",
        lambda files, repo, head_sha, model, bug_cap, fast=False: ReviewResult(
            raw_count=0, bugs=[], nits=[], coverage=FAST_COVERAGE),
    )
    created = client.post("/review-diff", json={"diff": BAD_DIFF, "mode": "fast"}).json()
    review_id = created["review_id"]
    detail = client.get(f"/reviews/{review_id}").json()
    doc = client.get(f"/reviews/{review_id}/verdict").json()
    assert detail["decision"] == doc["verdict"] == verdict_mod.INSUFFICIENT_EVIDENCE


def test_a_run_that_records_no_lanes_does_not_approve(client, monkeypatch):
    """A NULL coverage column means "predates the column" and keeps the legacy
    reading. A run that happened after the column exists and still recorded
    nothing is a different claim, and must not inherit that grandfathering.
    Otherwise the safe default is the permissive one, which is this whole bug."""
    monkeypatch.setattr(
        main, "run_diff_review",
        lambda files, repo, head_sha, model, bug_cap, fast=False: ReviewResult(
            raw_count=0, bugs=[], nits=[]),
    )
    body = client.post("/review-diff", json={"diff": BAD_DIFF, "mode": "fast"}).json()
    assert body["verdict"] == verdict_mod.INSUFFICIENT_EVIDENCE
    assert body["coverage"]["level"] == verdict_mod.COVERAGE_NONE


def test_rows_predating_the_column_keep_the_legacy_reading(client):
    """Historical runs did run the reviewer; flipping 1256 of them to
    "not reviewed" would be a false statement in the other direction."""
    review_id = db.create_review(None, "o/r", 11, "m")
    conn = db.connect(None)
    conn.execute("UPDATE reviews SET status = 'done', coverage_json = NULL WHERE id = ?",
                 (review_id,))
    conn.commit()
    conn.close()
    doc = client.get(f"/reviews/{review_id}/verdict").json()
    assert doc["coverage"]["level"] == verdict_mod.COVERAGE_UNKNOWN
    assert doc["verdict"] == "approve"


def test_review_detail_surfaces_run_status_for_pollers(client, monkeypatch):
    """`GET /reviews/{id}` omitted `status`, so a polling consumer had to infer
    termination from `decision` being a value it recognised. That inference breaks
    the moment the decision vocabulary grows: a poller reads an unrecognised
    decision as "not finished yet" and burns its whole budget on a review that
    completed. Surface the run status so termination never depends on the verdict."""
    monkeypatch.setattr(
        main, "run_diff_review",
        lambda files, repo, head_sha, model, bug_cap, fast=False: ReviewResult(
            raw_count=0, bugs=[], nits=[], coverage=FAST_COVERAGE),
    )
    created = client.post("/review-diff", json={"diff": BAD_DIFF, "mode": "fast"}).json()
    detail = client.get(f"/reviews/{created['review_id']}").json()
    assert detail["status"] == db.STATUS_DONE
    assert detail["decision"] == verdict_mod.INSUFFICIENT_EVIDENCE
