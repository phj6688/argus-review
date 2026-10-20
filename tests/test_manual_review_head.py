"""Manual reviews bind their saved head before reading a changing PR."""

from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from argus import github, review
from argus.api import db, main
from argus.jarvis import live

REPO = "example/a"
HEAD = "a" * 40
NEW_HEAD = "b" * 40


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = str(tmp_path / "argus.db")
    monkeypatch.setenv("ARGUS_DB_PATH", path)
    monkeypatch.delenv("ARGUS_FALLBACK_MODEL", raising=False)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: pytest.fail("unexpected request"))
    monkeypatch.setattr(live, "_pr_head_sha", lambda *a: HEAD)
    monkeypatch.setattr(main, "_publish_completed_review", Mock())
    db.init_db(path)
    return path


def test_manual_review_stores_head_before_runner(database, monkeypatch):
    def run(repo, pr, model, cap, *, expected_head_sha=None):
        assert (repo, pr) == (REPO, 1)
        assert db.list_reviews(database)[0]["head_sha"] == HEAD
        assert expected_head_sha == HEAD
        return review.ReviewResult(raw_count=0, head_sha=HEAD)

    monkeypatch.setattr(main, "run_review", run)
    response = TestClient(main.app).post("/reviews", json={"repo": REPO, "pr": 1})
    assert response.status_code == 201
    row = db.get_review(database, response.json()["id"])
    assert row["status"] == "done"
    assert row["head_sha"] == HEAD


@pytest.mark.parametrize("fallback", [False, True])
def test_push_while_manual_review_reads_files_gives_head_moved(database, monkeypatch, fallback):
    current = [HEAD]
    monkeypatch.setattr(live, "_pr_head_sha", lambda *a: current[0])

    def files(*args):
        current[0] = NEW_HEAD
        return []

    monkeypatch.setattr(review, "fetch_pr_files", files)
    monkeypatch.setattr(live, "_pr_meta", lambda *a: {"head_sha": current[0]})
    monkeypatch.setattr(review, "_deep_review", lambda *a, **k: pytest.fail("head must be checked first"))
    if fallback:
        monkeypatch.setenv("ARGUS_FALLBACK_MODEL", "fallback")

        def run(repo, pr, model, *args, **kwargs):
            if model != "fallback":
                raise RuntimeError("primary unavailable")
            return review.run_review(repo, pr, model, *args, **kwargs)

        monkeypatch.setattr(main, "run_review", run)
    response = TestClient(main.app).post("/reviews", json={"repo": REPO, "pr": 1})
    assert response.status_code == 201
    row = db.get_review(database, response.json()["id"])
    assert row["status"] == "error"
    assert row["error_class"] == "HeadMoved"
    assert row["head_sha"] == HEAD
    main._publish_completed_review.assert_not_called()


@pytest.mark.parametrize("error", [httpx.ConnectError("temporary"), github.GitHubAuthError("no token")])
@pytest.mark.parametrize("fails", [False, True])
def test_unresolved_manual_head_stays_missing(database, monkeypatch, error, fails):
    def lookup(*args):
        raise error

    def run(*args, expected_head_sha=None):
        assert db.list_reviews(database)[0]["head_sha"] is None
        assert expected_head_sha is None
        if fails:
            raise RuntimeError("review failed")
        return review.ReviewResult(raw_count=0, head_sha=NEW_HEAD)

    monkeypatch.setattr(live, "_pr_head_sha", lookup)
    monkeypatch.setattr(main, "run_review", run)
    response = TestClient(main.app).post("/reviews", json={"repo": REPO, "pr": 1})
    assert response.status_code == 201
    row = db.get_review(database, response.json()["id"])
    assert row["status"] == ("error" if fails else "done")
    assert row["head_sha"] is None
