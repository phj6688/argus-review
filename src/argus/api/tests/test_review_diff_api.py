"""POST /review-diff and GET /reviews/{id}/verdict: the agent-facing surface."""

import httpx
import pytest
from fastapi.testclient import TestClient

from argus.api import db, main
from argus.review import ReviewResult
from argus.schema import Finding, JudgeOpinion, Severity, Verdict, VerifiedFinding

DIFF = """diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,3 @@
 import httpx
+resp = httpx.get(url)
 print(resp)
"""


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_DB_PATH", str(tmp_path / "argus.db"))
    db.init_db()
    with TestClient(main.app) as test_client:
        yield test_client


def _verified(title="no timeout", severity=Severity.major, judge=None, lane="llm"):
    finding = Finding(file="src/app.py", line=2, severity=severity, confidence=0.8,
                      title=title, description="d", proposed_fix="p")
    return VerifiedFinding(
        finding=finding, verdict=Verdict(is_real=True, confidence=0.9, reasoning="r"),
        lane=lane, judge=judge, fingerprint=f"argusfp:{title.replace(' ', '')}",
    )


def test_fast_mode_returns_verdict_document_synchronously(client, monkeypatch):
    calls = {}

    def fake_diff_review(files, repo, head_sha, model, bug_cap, fast=False, **kwargs):
        calls.update(fast=fast, repo=repo, n_files=len(files))
        return ReviewResult(raw_count=0, bugs=[], nits=[], coverage=[
            {"lane": "probe", "status": "ran", "checks": 0, "eligible": 6},
            {"lane": "llm", "status": "skipped", "reason": "mode=fast"},
        ])

    monkeypatch.setattr(main, "run_diff_review", fake_diff_review)
    response = client.post("/review-diff", json={"diff": DIFF, "mode": "fast"})
    assert response.status_code == 201
    body = response.json()
    assert calls == {"fast": True, "repo": None, "n_files": 1}
    # Nothing read the code, so the clean result is not a pass. See
    # test_coverage_surface.py for the full contract.
    assert body["verdict"] == "insufficient_evidence"
    assert body["source"]["kind"] == "diff"
    assert body["findings"] == []
    assert "unverified" not in body


def test_full_mode_runs_in_background_and_verdict_endpoint_serves_it(client, monkeypatch):
    judged = _verified(judge=JudgeOpinion(verdict="real", confidence=0.9, model="glm"))
    monkeypatch.setattr(
        main, "run_diff_review",
        lambda files, repo, head_sha, model, bug_cap, **kwargs: ReviewResult(
            raw_count=1, bugs=[judged], nits=[], all_findings=[judged]),
    )
    created = client.post("/review-diff", json={
        "diff": DIFF, "repo": "o/r", "baseline": ["argusfp:notimeout", "argusfp:gone"],
    })
    assert created.status_code == 201
    review_id = created.json()["id"]

    # TestClient runs background tasks before returning, so the run is done.
    doc = client.get(f"/reviews/{review_id}/verdict").json()
    assert doc["verdict"] == "changes_requested"
    item = doc["findings"][0]
    assert item["evidence_tier"] == "consensus"
    assert item["baseline_state"] == "unchanged"
    assert doc["resolved"] == ["argusfp:gone"]
    assert doc["source"] == {"kind": "diff", "repo": "o/r", "pr": None,
                             "base_sha": None, "head_sha": None}


def test_verdict_endpoint_reports_running_and_404(client, monkeypatch):
    review_id = db.create_review(None, "o/r", 1, "m")
    assert client.get(f"/reviews/{review_id}/verdict").json() == {
        "review_id": review_id, "status": "running"}
    assert client.get("/reviews/9999/verdict").status_code == 404


def test_verdict_endpoint_serves_pr_reviews_too(client):
    review_id = db.create_review(None, "o/r", 7, "m")
    probe = _verified(title="probe: fp-1", lane="probe", severity=Severity.critical)
    db.save_result(None, review_id, ReviewResult(raw_count=0, bugs=[probe], nits=[]))
    doc = client.get(f"/reviews/{review_id}/verdict").json()
    assert "unverified" not in doc
    assert doc["source"] == {"kind": "pr", "repo": "o/r", "pr": 7,
                             "base_sha": None, "head_sha": None}
    assert doc["findings"][0]["evidence_tier"] == "deterministic"
    assert doc["counts"]["by_tier"] == {"deterministic": 1}


def test_disputed_bug_does_not_request_changes(client):
    review_id = db.create_review(None, "o/r", 8, "m")
    disputed = _verified(judge=JudgeOpinion(verdict="fp", confidence=0.8, model="glm"))
    db.save_result(None, review_id, ReviewResult(raw_count=1, bugs=[disputed], nits=[]))
    doc = client.get(f"/reviews/{review_id}/verdict").json()
    assert doc["findings"][0]["evidence_tier"] == "disputed"
    assert doc["verdict"] == "comment"


def test_rejects_sourceless_and_unparseable_requests(client):
    assert client.post("/review-diff", json={}).status_code == 422
    assert client.post("/review-diff", json={"diff": "not a diff"}).status_code == 422
    assert client.post("/review-diff", json={"mode": "weird", "diff": DIFF}).status_code == 422


@pytest.mark.parametrize("include_safe", [False, True])
def test_compare_drops_unsafe_current_and_previous_paths(client, monkeypatch, caplog, include_safe):
    files = [
        {"filename": "../../x", "status": "modified", "patch": "+unsafe"},
        {"filename": "bad\nname.py", "status": "modified", "patch": "+unsafe"},
        {"filename": "renamed.py", "previous_filename": "../../old.py",
         "status": "renamed", "patch": "+unsafe"},
        {"filename": "other.py", "previous_filename": "bad\nold.py",
         "status": "renamed", "patch": "+unsafe"},
    ]
    if include_safe:
        files.append({"filename": "src/safe.py", "previous_filename": "src/old.py",
                      "status": "renamed", "patch": "+safe"})
    monkeypatch.setattr(main.github, "get", lambda *_: httpx.Response(200, json={"files": files}))
    reviewed = []

    def review(files, *args, **kwargs):
        reviewed.extend(files)
        return ReviewResult(raw_count=0, bugs=[], nits=[])

    monkeypatch.setattr(main, "run_diff_review", review)
    monkeypatch.setattr(main.diff.log, "propagate", False)
    main.diff.log.addHandler(caplog.handler)
    try:
        response = client.post("/review-diff", json={
            "repo": "o/r", "base_sha": "aaaaaaa", "head_sha": "bbbbbbb", "mode": "fast",
        })
    finally:
        main.diff.log.removeHandler(caplog.handler)
    if include_safe:
        assert response.status_code == 201
        assert reviewed == [main.FilePatch("src/safe.py", "renamed", "+safe")]
    else:
        assert response.status_code == 422
        assert "zero files" in response.json()["detail"]
        assert reviewed == []
        assert db.list_reviews() == []
    assert sum("Dropping diff file with unsafe path" in r.message for r in caplog.records) == 4
