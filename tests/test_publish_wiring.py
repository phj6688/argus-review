"""Publishing persistence, completed-review wiring, and preview without live calls."""

import json
import logging
import sqlite3
import sys
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from argus import cli, diff, publish
from argus.api import db, main
from argus.jarvis import live
from argus.review import ReviewResult
from argus.schema import Finding, Severity, Verdict, VerifiedFinding

HEAD = "a" * 40
PATCH = "@@ -10 +10 @@\n+accept(value)"


@pytest.fixture
def database(tmp_path, monkeypatch, caplog):
    path = str(tmp_path / "argus.db")
    monkeypatch.setenv("ARGUS_DB_PATH", path)
    for key in ("ARGUS_PUBLISH", "ARGUS_PUBLISH_REPOS", "ARGUS_PUBLISH_TOKEN",
                "ARGUS_PUBLISH_MIN_SEVERITY", "ARGUS_PUBLISH_MAX_COMMENTS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(httpx, "get", Mock(side_effect=AssertionError("unexpected HTTP")))
    monkeypatch.setattr(httpx, "post", Mock(side_effect=AssertionError("unexpected HTTP")))
    db.init_db(path)
    loggers = [logging.getLogger(name) for name in ("argus.api", "argus.db")]
    for logger in loggers:
        logger.addHandler(caplog.handler)
    try:
        yield path
    finally:
        for logger in loggers:
            logger.removeHandler(caplog.handler)


def result(head=None):
    item = VerifiedFinding(
        finding=Finding(file="src/app.py", line=10, severity=Severity.major, confidence=0.9,
                        title="Missing check", description="Unchecked input", proposed_fix="Validate it"),
        verdict=Verdict(is_real=True, confidence=0.9), fingerprint="fp-one",
    )
    return ReviewResult(raw_count=1, bugs=[item], nits=[], head_sha=head)


def create(database, head=HEAD, pr=42):
    return db.create_review(database, "owner/repo", pr, "test-reviewer", head_sha=head)


def configure(monkeypatch, mode="dry-run"):
    monkeypatch.setenv("ARGUS_PUBLISH", mode)
    monkeypatch.setenv("ARGUS_PUBLISH_REPOS", "owner/repo")
    monkeypatch.setenv("ARGUS_PUBLISH_TOKEN", "write-secret")
    monkeypatch.setenv("ARGUS_PUBLISH_BOT_LOGIN", "publisher")


def stub_files(monkeypatch):
    monkeypatch.setattr(diff, "fetch_pr_files", lambda repo, pr: [diff.FilePatch("src/app.py", "modified", PATCH)])


def test_columns_exist_and_old_rows_load(tmp_path):
    path = str(tmp_path / "legacy.db")
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE reviews (id INTEGER PRIMARY KEY, repo TEXT, pr INTEGER, "
                     "model TEXT, status TEXT, created_at TEXT, raw_count INTEGER, dropped INTEGER)")
        conn.execute("INSERT INTO reviews VALUES (1, 'owner/repo', 42, 'test-reviewer', 'done', '2026-01-01', 0, 0)")
    db.init_db(path)
    db.init_db(path)
    row = db.get_review(path, 1)
    assert row["status"] == "done"
    for key in ("publish_status", "published_at", "publish_error", "published_comments", "publish_payload"):
        assert key in row
        assert row[key] is None
    with db.connect(path) as conn:
        columns = {row["name"]: row["type"] for row in conn.execute("PRAGMA table_info(reviews)")}
    assert columns["published_comments"] == "INTEGER"


@pytest.mark.parametrize("status", ["dry_run", "posted", "error", "skipped"])
def test_outcome_roundtrip_preserves_done(database, status):
    rid = create(database)
    db.save_result(database, rid, result())
    payload = {"body": "preview", "comments": []}
    db.save_publish_outcome(database, rid, publish.PublishOutcome(status, 2, 1, "detail", payload))
    row = db.get_review(database, rid)
    assert row["status"] == "done"
    assert row["publish_status"] == status
    assert row["published_comments"] == 2
    assert row["publish_error"] == "detail"
    assert bool(row["published_at"]) == (status == "posted")
    assert json.loads(row["publish_payload"]) == payload


def test_large_payload_capped_as_valid_json(database):
    rid = create(database)
    payload = {"body": "é\\\"" * 100_000, "comments": []}
    db.save_publish_outcome(database, rid, publish.PublishOutcome("dry_run", payload=payload))
    stored = db.get_review(database, rid)["publish_payload"]
    assert len(stored.encode()) <= 200_000
    assert json.loads(stored)["truncated"] is True


@pytest.mark.parametrize("mode", [None, "0", "dry-run", "1"])
def test_off_or_unlisted_no_patch_fetch(database, monkeypatch, mode):
    if mode is not None:
        monkeypatch.setenv("ARGUS_PUBLISH", mode)
    monkeypatch.setattr(diff, "fetch_pr_files", Mock(side_effect=AssertionError("must not fetch")))
    rid = create(database)
    main.run_one_review(database, rid, "owner/repo", 42, result)
    row = db.get_review(database, rid)
    assert row["status"] == "done"
    assert row["publish_status"] is None
    diff.fetch_pr_files.assert_not_called()


@pytest.mark.parametrize("head,pr", [(None, 42), ("abc", 42), ("z" * 40, 42), (HEAD, 0)])
def test_non_pr_or_invalid_head_skips_fetch(database, monkeypatch, caplog, head, pr):
    configure(monkeypatch)
    monkeypatch.setattr(diff, "fetch_pr_files", Mock(side_effect=AssertionError("must not fetch")))
    rid = create(database, head, pr)
    with caplog.at_level("INFO", logger="argus.api"):
        main.run_one_review(database, rid, "owner/repo", pr, result)
    row = db.get_review(database, rid)
    assert row["status"] == "done"
    assert row["publish_status"] is None
    diff.fetch_pr_files.assert_not_called()
    skips = list({id(r): r for r in caplog.records if "publishing skipped" in r.message}.values())
    assert len(skips) == (1 if pr > 0 else 0)
    if pr > 0:
        assert skips[0].levelname == "INFO"
        assert "owner/repo#42" in skips[0].message
        assert f"review {rid}" in skips[0].message
        assert ("missing" if head is None else "invalid") in skips[0].message
        assert "head" in skips[0].message


def test_manual_pr_review_saves_head_and_dry_run_items(database, monkeypatch):
    configure(monkeypatch)
    stub_files(monkeypatch)
    monkeypatch.setattr(live, "_pr_head_sha", lambda *args: HEAD)
    monkeypatch.setattr(main, "run_review", lambda *args, **kwargs: result(HEAD))
    response = TestClient(main.app).post("/reviews", json={"repo": "owner/repo", "pr": 42})
    assert response.status_code == 201
    rid = response.json()["id"]
    row = db.get_review(database, rid)
    assert row["status"] == "done"
    assert row["head_sha"] == HEAD
    assert row["publish_status"] == "dry_run"
    assert json.loads(row["publish_payload"])["commit_id"] == HEAD
    with db.connect(database) as conn:
        items = conn.execute(
            "SELECT review_id, head_sha, mode, path, line FROM published_findings"
        ).fetchall()
    assert [tuple(item) for item in items] == [(rid, HEAD, "dry_run", "src/app.py", 10)]
    httpx.get.assert_not_called()
    httpx.post.assert_not_called()


@pytest.mark.parametrize("mode", ["dry-run", "1"])
def test_unresolved_manual_head_stores_findings_without_publishing(database, monkeypatch, caplog, mode):
    configure(monkeypatch, mode)

    def lookup(*args):
        raise httpx.ConnectError("temporary")

    def run(*args, expected_head_sha=None):
        assert expected_head_sha is None
        return result("b" * 40)

    monkeypatch.setattr(live, "_pr_head_sha", lookup)
    monkeypatch.setattr(main, "run_review", run)
    monkeypatch.setattr(diff, "fetch_pr_files", Mock(side_effect=AssertionError("must not fetch")))
    with caplog.at_level("INFO", logger="argus.api"):
        response = TestClient(main.app).post("/reviews", json={"repo": "owner/repo", "pr": 42})
    assert response.status_code == 201
    rid = response.json()["id"]
    row = db.get_review(database, rid)
    assert row["status"] == "done"
    assert row["head_sha"] is None
    assert row["publish_status"] is None
    assert [item["title"] for item in db.get_findings(database, rid)] == ["Missing check"]
    detail = TestClient(main.app).get(f"/reviews/{rid}").json()
    assert detail["bugs"][0]["finding"]["title"] == "Missing check"
    skips = {id(r): r for r in caplog.records if "missing head SHA" in r.getMessage()}
    assert len(skips) == 1
    assert next(iter(skips.values())).levelno == logging.INFO
    assert not any("differs from stored head" in r.getMessage() for r in caplog.records)
    diff.fetch_pr_files.assert_not_called()
    httpx.get.assert_not_called()
    httpx.post.assert_not_called()


@pytest.mark.parametrize("result_head", [None, HEAD, "b" * 40])
def test_watcher_head_preserved(database, caplog, result_head):
    rid = create(database)
    with caplog.at_level("WARNING", logger="argus.db"):
        db.save_result(database, rid, result(result_head))
    assert db.get_review(database, rid)["head_sha"] == HEAD
    warnings = list({id(r): r for r in caplog.records
                     if r.name == "argus.db" and r.levelname == "WARNING"}.values())
    assert len(warnings) == (1 if result_head and result_head != HEAD else 0)
    if warnings:
        assert f"review {rid}" in warnings[0].message
        assert HEAD in warnings[0].message
        assert result_head in warnings[0].message


def test_dry_run_persists_exact_payload(database, monkeypatch):
    configure(monkeypatch)
    stub_files(monkeypatch)
    rid = create(database)
    main.run_one_review(database, rid, "owner/repo", 42, result)
    row = db.get_review(database, rid)
    assert row["status"] == "done"
    assert row["publish_status"] == "dry_run"
    assert row["published_at"] is None
    payload = json.loads(row["publish_payload"])
    assert len(payload["comments"]) == 1
    assert "**Missing check**" in payload["comments"][0]["body"]
    httpx.get.assert_not_called()
    httpx.post.assert_not_called()


def test_posting_uses_write_token_and_records_result(database, monkeypatch):
    configure(monkeypatch, "1")
    stub_files(monkeypatch)
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer write-secret"
        if request.method == "GET" and request.url.path.endswith("/42"):
            return httpx.Response(200, json={"head": {"sha": HEAD}})
        return httpx.Response(200, json=[] if request.method == "GET" else {"id": 1})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        monkeypatch.setattr(publish.httpx, "get", http.get)
        monkeypatch.setattr(publish.httpx, "post", http.post)
        rid = create(database)
        main.run_one_review(database, rid, "owner/repo", 42, result)
    row = db.get_review(database, rid)
    assert row["status"] == "done"
    assert row["publish_status"] == "posted"
    assert row["published_comments"] == 1
    assert row["published_at"]
    assert len(requests) == 5
    assert json.loads(next(r.content for r in requests if r.method == "POST")) == json.loads(row["publish_payload"])


@pytest.mark.parametrize("failure", ["fetch", "post", "token", "publish"])
def test_publish_failure_never_fails_completed_review(database, monkeypatch, failure):
    configure(monkeypatch, "1")
    stub_files(monkeypatch)
    if failure == "fetch":
        monkeypatch.setattr(diff, "fetch_pr_files", Mock(side_effect=RuntimeError("fetch failed")))
    elif failure == "token":
        monkeypatch.delenv("ARGUS_PUBLISH_TOKEN")
        monkeypatch.setenv("GITHUB_TOKEN", "read-secret")
    elif failure == "publish":
        monkeypatch.setattr(publish, "publish_review", Mock(side_effect=RuntimeError("unexpected failure")))
    else:
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: httpx.Response(
            200, json={"head": {"sha": HEAD}} if a[0].endswith("/42") else [],
            request=httpx.Request("GET", a[0])))
        monkeypatch.setattr(httpx, "post", Mock(side_effect=RuntimeError("post failed")))
    rid = create(database)
    main.run_one_review(database, rid, "owner/repo", 42, result)
    row = db.get_review(database, rid)
    assert row["status"] == "done"
    assert row["publish_status"] == "error"
    assert row["publish_error"]
    assert row["error"] is None
    assert db.get_findings(database, rid)


def test_publish_record_write_failure_does_not_escape(database, monkeypatch):
    configure(monkeypatch)
    stub_files(monkeypatch)
    monkeypatch.setattr(db, "save_publish_outcome", Mock(side_effect=RuntimeError("storage unavailable")))
    rid = create(database)
    main.run_one_review(database, rid, "owner/repo", 42, result)
    assert db.get_review(database, rid)["status"] == "done"


def test_successful_fallback_also_publishes(database, monkeypatch):
    configure(monkeypatch)
    stub_files(monkeypatch)
    rid = create(database)
    main.run_one_review(database, rid, "owner/repo", 42,
                        Mock(side_effect=RuntimeError("primary failed")),
                        fallback_runner=result, primary_model="primary", fallback_model="fallback")
    row = db.get_review(database, rid)
    assert row["status"] == "done"
    assert row["publish_status"] == "dry_run"
    assert row["model_used"] == "fallback"


def test_failed_review_does_not_publish(database, monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(diff, "fetch_pr_files", Mock(side_effect=AssertionError("must not fetch")))
    rid = create(database)
    main.run_one_review(database, rid, "owner/repo", 42, Mock(side_effect=RuntimeError("review failed")))
    row = db.get_review(database, rid)
    assert row["status"] == "error"
    assert row["publish_status"] is None
    diff.fetch_pr_files.assert_not_called()


def test_cli_preview_prints_without_posting(database, monkeypatch, capsys):
    configure(monkeypatch, "1")
    monkeypatch.setattr(cli, "fetch_pr_files", lambda repo, pr: [diff.FilePatch("src/app.py", "modified", PATCH)])
    rid = create(database)
    db.save_result(database, rid, result())
    monkeypatch.setattr(sys, "argv", ["argus", "publish-preview", str(rid), "--db", database])
    cli.main()
    output = capsys.readouterr().out
    assert "**Argus review**" in output
    assert "src/app.py:10" in output
    assert "**Missing check**" in output
    assert "Prompt for agents" in output
    assert db.get_review(database, rid)["publish_status"] is None
    httpx.get.assert_not_called()
    httpx.post.assert_not_called()


def test_cli_preview_default_db(database, monkeypatch, capsys):
    stub_files(monkeypatch)
    monkeypatch.setattr(cli, "fetch_pr_files", diff.fetch_pr_files)
    rid = create(database)
    db.save_result(database, rid, result())
    monkeypatch.setattr(sys, "argv", ["argus", "publish-preview", str(rid)])
    cli.main()
    assert "**Argus review**" in capsys.readouterr().out


@pytest.mark.parametrize("kind", ["missing", "running", "diff"])
def test_cli_preview_rejects_unavailable_review(database, monkeypatch, kind):
    rid = 999 if kind == "missing" else create(database, pr=0 if kind == "diff" else 42)
    if kind == "diff":
        db.save_result(database, rid, result())
    monkeypatch.setattr(sys, "argv", ["argus", "publish-preview", str(rid), "--db", database])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code != 0


def test_patch_fetch_uses_read_token_publishing_uses_separate_write_token(database, monkeypatch):
    configure(monkeypatch, "1")
    monkeypatch.setenv("GITHUB_TOKEN", "read-secret")
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/files"):
            assert request.method == "GET"
            assert request.headers["authorization"] == "Bearer read-secret"
            return httpx.Response(200, json=[{"filename": "src/app.py", "status": "modified", "patch": PATCH}])
        assert request.headers["authorization"] == "Bearer write-secret"
        if request.method == "GET" and request.url.path.endswith("/42"):
            return httpx.Response(200, json={"head": {"sha": HEAD}})
        return httpx.Response(200, json=[] if request.method == "GET" else {"id": 1})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        monkeypatch.setattr(httpx, "get", http.get)
        monkeypatch.setattr(httpx, "post", http.post)
        rid = create(database)
        main.run_one_review(database, rid, "owner/repo", 42, result)
    row = db.get_review(database, rid)
    assert row["status"] == "done"
    assert row["publish_status"] == "posted"
    assert row["published_comments"] == 1
    assert [request.url.path.rsplit("/", 1)[1] for request in requests] == ["files", "42", "comments", "reviews", "reviews", "comments"]


def test_moved_head_preserves_completed_status(database, monkeypatch):
    configure(monkeypatch, "1")
    stub_files(monkeypatch)
    monkeypatch.setattr(httpx, "get", lambda url, **kwargs: httpx.Response(
        200, json={"head": {"sha": "b" * 40}}, request=httpx.Request("GET", url)))
    rid = create(database)
    main.run_one_review(database, rid, "owner/repo", 42, result)
    row = db.get_review(database, rid)
    assert row["status"] == "done"
    assert row["publish_status"] == "skipped"
    assert "PR head moved" in row["publish_error"]
    httpx.post.assert_not_called()


def test_list_reviews_omits_payload_and_preserves_other_fields(database):
    rid = create(database)
    db.save_result(database, rid, result())
    before = main.list_reviews()
    payload = {"body": "preview", "comments": []}
    db.save_publish_outcome(database, rid, publish.PublishOutcome("dry_run", payload=payload))
    stored = db.get_review(database, rid)
    assert json.loads(stored["publish_payload"]) == payload
    assert db.list_reviews(database) == [{key: value for key, value in stored.items() if key != "publish_payload"}]
    after = main.list_reviews()
    assert "publish_payload" not in after["reviews"][0]
    for key, value in before["reviews"][0].items():
        if not key.startswith("publish"):
            assert after["reviews"][0][key] == value


def test_list_reviews_works_without_payload_column(tmp_path):
    path = str(tmp_path / "old.db")
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE reviews (id INTEGER, created_at TEXT)")
        conn.executemany("INSERT INTO reviews VALUES (?, ?)", [(1, "today"), (2, "today")])
    assert db.list_reviews(path) == [{"id": 2, "created_at": "today"}, {"id": 1, "created_at": "today"}]
