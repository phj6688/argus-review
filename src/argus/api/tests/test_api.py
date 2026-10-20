"""API tests with a stubbed run_review so no network or API key is needed.

Covers the persistence round-trip and the list/detail endpoints. The review
engine is monkeypatched at the point main.py looks it up, so these tests never
touch the provider backend.
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from argus.api import db, main
from argus.review import ReviewResult
from argus.schema import Finding, Severity, Verdict, VerifiedFinding


def _verified(severity: Severity, line: int, is_real: bool = True) -> VerifiedFinding:
    return VerifiedFinding(
        finding=Finding(
            file="src/app.py", line=line, severity=severity, confidence=0.8,
            title=f"issue {line}", description="d", proposed_fix="f", rule_id="R1",
        ),
        verdict=Verdict(is_real=is_real, confidence=0.9, reasoning="r"),
    )


def _stub_result() -> ReviewResult:
    return ReviewResult(
        raw_count=3,
        bugs=[_verified(Severity.critical, 10), _verified(Severity.major, 20)],
        nits=[_verified(Severity.nit, 30)],
        dropped=0,
    )


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "argus.db"
    monkeypatch.setenv("ARGUS_DB_PATH", str(path))
    db.init_db(str(path))
    return str(path)


@pytest.fixture
def client(db_path, monkeypatch):
    from argus.jarvis import live

    monkeypatch.setattr(live, "_pr_head_sha", lambda *a: None)
    monkeypatch.setattr(main, "run_review", lambda *a, **kw: _stub_result())
    with TestClient(main.app) as test_client:
        yield test_client


def test_health_returns_model_without_network(client, monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("shallow /health must not hit the network")

    monkeypatch.setattr(main, "ping", _boom)
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["model"]
    assert body["status"] == "ok"


def test_health_deep_pings_backend(client, monkeypatch):
    # deep=1 is gated (E4-c): a credentialed caller (shared token) still pings.
    monkeypatch.setattr(main, "ping", lambda model=None: "ok")
    monkeypatch.setenv("ARGUS_HEALTH_TOKEN", "s3cret")
    response = client.get("/health", params={"deep": 1},
                          headers={"x-argus-health-token": "s3cret"})
    assert response.status_code == 200
    assert response.json()["ping"] == "ok"


def test_post_review_returns_id_and_runs_in_background(client):
    response = client.post("/reviews", json={"repo": "example/app", "pr": 7})
    assert response.status_code == 201
    body = response.json()
    assert isinstance(body["id"], int)
    # TestClient runs background tasks after the response, so by the time the
    # POST returns to the caller the run has already persisted its findings.
    detail = client.get(f"/reviews/{body['id']}")
    assert detail.status_code == 200
    # ReviewDetail carries no status; the run completing is proven by findings.
    assert detail.json()["bugs"]


def test_review_status_starts_running(db_path, monkeypatch):
    # Drive the lifecycle directly: create the row, assert running before the task.
    review_id = db.create_review(db_path, "example/app", 7, "claude-x")
    row = db.get_review(db_path, review_id)
    assert row["status"] == "running"


def test_background_task_persists_findings_and_flips_to_done(db_path):
    review_id = db.create_review(db_path, "example/app", 7, "claude-x")
    main.run_one_review(db_path, review_id, "example/app", 7, _stub_result)
    detail = db.get_review(db_path, review_id)
    assert detail["status"] == "done"
    assert detail["raw_count"] == 3
    assert detail["dropped"] == 0
    findings = db.get_findings(db_path, review_id)
    assert len(findings) == 3
    bugs = [f for f in findings if f["is_bug"]]
    assert len(bugs) == 2
    # the nit finding is persisted with is_bug = 0
    assert any(f["severity"] == "nit" and not f["is_bug"] for f in findings)
    # verdict fields survive
    assert all(f["verdict_is_real"] == 1 for f in findings)


def test_background_task_marks_error_when_run_review_raises(db_path):
    review_id = db.create_review(db_path, "example/app", 7, "claude-x")

    def _broken():
        raise RuntimeError("boom")

    main.run_one_review(db_path, review_id, "example/app", 7, _broken)
    detail = db.get_review(db_path, review_id)
    assert detail["status"] == "error"
    # The failure reason is captured, not just the status (no log archaeology).
    assert detail["error"] == "RuntimeError: boom"
    assert db.get_findings(db_path, review_id) == []


# Field sets the web view models require (web/lib/types.ts). A missing key the
# UI reads crashes the screen, so assert the contract by key set, not by hand.
_SUMMARY_KEYS = {"id", "repo", "pr", "title", "model", "counts", "action", "age"}
_LIVE_KEYS = {"repo", "pr", "title", "model", "stage", "elapsed"}
_DETAIL_KEYS = {
    "id", "repo", "pr", "title", "model", "time", "model_used", "fallback_from",
    "fallback_reason", "decision", "bugs", "nits", "thread",
}


def test_reviews_empty_returns_live_null_and_empty_list(client):
    body = client.get("/reviews").json()
    assert body == {"live": None, "reviews": []}


def test_list_reviews_returns_view_model_envelope(client):
    first = client.post("/reviews", json={"repo": "example/a", "pr": 1}).json()["id"]
    second = client.post("/reviews", json={"repo": "example/b", "pr": 2}).json()["id"]
    body = client.get("/reviews").json()

    # Envelope shape, not a bare array (the bug: a bare array broke reviews.slice).
    assert set(body) == {"live", "reviews"}
    assert body["live"] is None  # stub flips runs to done, nothing left running

    summaries = body["reviews"]
    assert all(set(s) >= _SUMMARY_KEYS for s in summaries)
    # ReviewSummary.id is a string; counts is a full severity tally.
    ids = [s["id"] for s in summaries]
    assert ids == sorted(ids, reverse=True)  # newest (higher autoincrement id) first
    assert str(first) in ids and str(second) in ids
    sample = summaries[0]
    assert set(sample["counts"]) == {"critical", "major", "minor", "nit"}
    # the stub persists 1 critical + 1 major bug and 1 nit per review
    assert sample["counts"]["critical"] == 1
    assert sample["counts"]["nit"] == 1


def test_get_detail_returns_review_detail_shape(client):
    review_id = client.post("/reviews", json={"repo": "example/app", "pr": 7}).json()["id"]
    detail = client.get(f"/reviews/{review_id}").json()

    assert set(detail) >= _DETAIL_KEYS
    assert detail["repo"] == "example/app"
    assert detail["pr"] == 7
    assert detail["id"] == str(review_id)
    # findings split by is_bug into VerifiedFinding-shaped bugs/nits
    assert len(detail["bugs"]) == 2
    assert len(detail["nits"]) == 1
    bug = detail["bugs"][0]
    # id (for POST /findings/{id}/mark) and mark (operator ground truth) join the shape.
    assert set(bug) == {"id", "finding", "verdict", "mark"}
    assert isinstance(bug["id"], int)
    assert bug["mark"] is None  # unlabeled until the operator marks it
    assert set(bug["finding"]) >= {
        "file", "line", "severity", "confidence", "title", "description",
        "proposed_fix", "rule_id",
    }
    assert set(bug["verdict"]) == {"is_real", "confidence", "reasoning"}
    assert bug["verdict"]["is_real"] is True
    # any confirmed bug -> the engine would request changes
    assert detail["decision"] == "changes_requested"


def test_running_review_surfaces_as_live(client, db_path):
    # A row left in `running` (no background completion) maps to LiveReview.
    db.create_review(db_path, "example/live", 9, "claude-x")
    body = client.get("/reviews").json()
    assert body["live"] is not None
    assert set(body["live"]) >= _LIVE_KEYS
    assert body["live"]["repo"] == "example/live"
    assert body["live"]["pr"] == 9
    # the running row is not duplicated into the summaries list
    assert all(s["repo"] != "example/live" for s in body["reviews"])


def test_get_detail_404_for_unknown_id(client):
    assert client.get("/reviews/999999").status_code == 404


def test_persistence_survives_fresh_connection(db_path):
    review_id = db.create_review(db_path, "example/app", 7, "claude-x")
    main.run_one_review(db_path, review_id, "example/app", 7, _stub_result)

    # open a brand-new connection (not the one that wrote) and read it back
    fresh = sqlite3.connect(db_path)
    fresh.row_factory = sqlite3.Row
    review_row = fresh.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
    finding_rows = fresh.execute(
        "SELECT * FROM findings WHERE review_id = ? ORDER BY id", (review_id,)
    ).fetchall()
    fresh.close()

    assert review_row["status"] == "done"
    assert review_row["repo"] == "example/app"
    assert len(finding_rows) == 3
    assert finding_rows[0]["title"] == "issue 10"


def test_findings_fk_cascade_on_review_delete(db_path):
    review_id = db.create_review(db_path, "example/app", 7, "claude-x")
    main.run_one_review(db_path, review_id, "example/app", 7, _stub_result)
    conn = db.connect(db_path)
    conn.execute("DELETE FROM reviews WHERE id = ?", (review_id,))
    conn.commit()
    orphans = conn.execute(
        "SELECT COUNT(*) AS n FROM findings WHERE review_id = ?", (review_id,)
    ).fetchone()["n"]
    conn.close()
    assert orphans == 0


def test_reconcile_orphaned_running_retires_stale_rows(db_path):
    # Rows left `running` by a dead process (e.g. a redeploy mid-review) are
    # retired to error/interrupted on startup with a completion stamp; a terminal
    # row is untouched and the sweep is idempotent.
    orphan_a = db.create_review(db_path, "example/ghost", 7, "claude-x")
    orphan_b = db.create_review(db_path, "example/ghost", 8, "claude-x")
    done = db.create_review(db_path, "example/done", 9, "claude-x")
    main.run_one_review(db_path, done, "example/done", 9, _stub_result)

    reclaimed = db.reconcile_orphaned_running(db_path)

    assert sorted(reclaimed) == sorted([orphan_a, orphan_b])
    conn = db.connect(db_path)
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM reviews")}
    conn.close()
    for rid in (orphan_a, orphan_b):
        assert rows[rid]["status"] == db.STATUS_ERROR
        assert rows[rid]["error_class"] == "interrupted"
        assert rows[rid]["completed_at"] is not None
    assert rows[done]["status"] == db.STATUS_DONE  # a completed run is never rewritten
    assert db.reconcile_orphaned_running(db_path) == []  # nothing left to sweep


def test_startup_sweep_retires_orphan_and_clears_live(db_path):
    # Seed an orphaned `running` row BEFORE boot, then start the app: the lifespan
    # startup sweep must retire it to interrupted, so a dead run never surfaces as
    # a live review. Exercises the lifespan hook, not just the bare db call.
    rid = db.create_review(db_path, "example/ghost", 7, "claude-x")
    with TestClient(main.app) as c:
        assert c.get("/reviews").json()["live"] is None
    row = db.get_review(db_path, rid)
    assert row["status"] == db.STATUS_ERROR
    assert row["error_class"] == "interrupted"
