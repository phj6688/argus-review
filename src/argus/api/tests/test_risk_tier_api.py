import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from argus.api import db, main
from argus.review import ReviewResult


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = str(tmp_path / "reviews.db")
    monkeypatch.setenv("ARGUS_DB_PATH", path)
    return path


def test_old_schema_migrates_without_inventing_risk(database):
    with sqlite3.connect(database) as conn:
        conn.execute(
            "CREATE TABLE reviews (id INTEGER PRIMARY KEY, repo TEXT NOT NULL, "
            "pr INTEGER NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL, "
            "created_at TEXT NOT NULL, raw_count INTEGER, dropped INTEGER)"
        )
        conn.execute(
            "INSERT INTO reviews VALUES (1, 'o/r', 2, 'test-model', 'done', "
            "'2026-01-01T00:00:00+00:00', 0, 0)"
        )
    db.init_db(database)
    with sqlite3.connect(database) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(reviews)")}
    assert {"risk_tier", "risk_json"} <= columns
    row = db.get_review(database, 1)
    assert row["risk_tier"] is None
    assert row["risk_json"] is None
    listed = db.list_reviews(database)
    assert len(listed) == 1
    assert listed[0]["risk_json"] is None
    with TestClient(main.app) as client:
        assert client.get("/reviews").json()["reviews"][0]["risk"] is None
        assert client.get("/reviews/1").json()["risk"] is None
        assert client.get("/reviews/1/verdict").json()["risk"] is None


def test_persisted_risk_reaches_summary_detail_and_verdict(database):
    db.init_db(database)
    review_id = db.create_review(database, "o/r", 2, "test-model")
    risk = {
        "tier": "risky", "changed_lines": 12, "max_lines": 300, "files": 1,
        "matches": [{"path": "deploy/docker-compose.prod.yml", "pattern": "docker-compose*"}],
        "matches_total": 1, "incomplete": False,
        "reasons": ["tripwire path deploy/docker-compose.prod.yml (docker-compose*)"],
    }
    label = (
        "Risk tier: risky (path and size rule only; needs the operator checkpoint; "
        "not a correctness verdict)"
    )
    db.save_result(database, review_id, ReviewResult(raw_count=0, risk_tier=risk))
    stored = db.get_review(database, review_id)
    assert stored["risk_tier"] == "risky"
    assert json.loads(stored["risk_json"]) == risk
    assert json.loads(db.list_reviews(database)[0]["risk_json"]) == risk
    with TestClient(main.app) as client:
        summary = client.get("/reviews").json()["reviews"][0]
        assert summary["risk"] == {"tier": "risky", "label": label, "reasons": risk["reasons"]}
        assert client.get(f"/reviews/{review_id}").json()["risk"] == {**risk, "label": label}
        assert client.get(f"/reviews/{review_id}/verdict").json()["risk"] == {**risk, "label": label}


def test_result_without_risk_persists_nulls(database):
    db.init_db(database)
    review_id = db.create_review(database, "o/r", 2, "test-model")
    db.save_result(database, review_id, ReviewResult(raw_count=0))
    stored = db.get_review(database, review_id)
    assert stored["risk_tier"] is None
    assert stored["risk_json"] is None


@pytest.mark.parametrize("count, unsafe, incomplete", [(299, False, False), (300, False, True),
                                                       (3, True, True)])
def test_compare_preserves_counts_renames_and_completeness(monkeypatch, count, unsafe, incomplete):
    listed = [
        {"filename": f"src/file{i}.py", "previous_filename": f"infra/old{i}.py",
         "status": "renamed", "patch": None, "additions": 4, "deletions": 3}
        for i in range(count)
    ]
    if unsafe:
        listed[0]["filename"] = "../bad.py"
    monkeypatch.setattr(main.github, "get", lambda *_: httpx.Response(200, json={"files": listed}))
    request = main.DiffReviewRequest(repo="o/r", base_sha="aaaaaaa", head_sha="bbbbbbb")
    files = main._files_for_diff_request(request)
    assert len(files) == count - int(unsafe)
    assert files.unseen is incomplete
    assert files[-1].previous_filename == f"infra/old{count - 1}.py"
    assert (files[-1].additions, files[-1].deletions) == (4, 3)
