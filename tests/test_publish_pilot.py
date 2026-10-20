"""Persist the actual rendered placement and recover published identities."""

import json
import sqlite3
import sys
from unittest.mock import Mock

import httpx
import pytest

from argus import cli, diff, github
from argus.api import db, main
from argus.review import ReviewResult
from argus.schema import Finding, Severity, Verdict, VerifiedFinding

HEAD = "a" * 40


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = str(tmp_path / "argus.db")
    monkeypatch.setenv("ARGUS_DB_PATH", path)
    monkeypatch.setenv("ARGUS_PUBLISH", "dry-run")
    monkeypatch.setenv("ARGUS_PUBLISH_REPOS", "owner/repo")
    monkeypatch.setenv("ARGUS_PUBLISH_TOKEN", "write-secret")
    monkeypatch.setenv("ARGUS_PUBLISH_BOT_LOGIN", "publisher")
    monkeypatch.setenv("ARGUS_PUBLISH_MAX_COMMENTS", "1")
    monkeypatch.setattr(httpx, "get", Mock(side_effect=AssertionError("unexpected HTTP")))
    monkeypatch.setattr(httpx, "post", Mock(side_effect=AssertionError("unexpected HTTP")))
    monkeypatch.setattr(diff, "fetch_pr_files", lambda *a: [diff.FilePatch("app.py", "modified", "@@ -9,3 +9,3 @@\n a\n-b\n+c\n d")])
    db.init_db(path)
    return path


def stored_review(database):
    rid = db.create_review(database, "owner/repo", 3, "test", head_sha=HEAD)
    findings = [VerifiedFinding(
        finding=Finding(file="app.py", line=line, severity=severity, confidence=0.9,
                        title=fp, description="Check input", proposed_fix="Validate"),
        verdict=Verdict(is_real=True, confidence=0.9), fingerprint=fp,
    ) for fp, line, severity in [("inline", 10, Severity.major), ("capped", 11, Severity.major),
                                 ("outside", 100, Severity.major), ("nit", 9, Severity.nit)]]
    db.save_result(database, rid, ReviewResult(raw_count=4, bugs=findings[:3], nits=findings[3:]))
    return rid


def records(database):
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("SELECT * FROM published_findings ORDER BY fingerprint")]


def test_dry_run_records_actual_placements_idempotently(database):
    rid = stored_review(database)
    main._publish_completed_review(database, rid, "owner/repo", 3)
    main._publish_completed_review(database, rid, "owner/repo", 3)
    items = records(database)
    assert [(i["fingerprint"], i["placement"], i["mode"]) for i in items] == [
        ("capped", "summary", "dry_run"), ("inline", "inline", "dry_run"), ("outside", "summary", "dry_run")]
    assert all(i["gh_review_id"] is None for i in items)
    assert items[1]["line"] == 10


def test_post_maps_ids_and_retry_recovers(database, monkeypatch):
    rid = stored_review(database)
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    posted = []
    requests = []
    def handler(request):
        assert request.headers["Authorization"] == "Bearer write-secret"
        requests.append(request)
        base = "/repos/owner/repo/pulls/3"
        if request.method == "POST":
            payload = json.loads(request.content)
            posted.append(payload)
            body = {"id": 70, "user": {"login": "publisher"}}
        elif request.url.path == base:
            body = {"head": {"sha": HEAD}}
        elif request.url.path == base + "/reviews":
            body = [{"id": 70, "body": posted[0]["body"], "user": {"login": "publisher"}}] if posted else []
        elif request.url.path.endswith("/comments"):
            body = [{"id": 501, "body": "unrelated"}, {"id": 502, "user": {"login": "publisher"}, "pull_request_review_id": 70, **posted[0]["comments"][0]}] if posted else []
        else:
            raise AssertionError(request.url)
        return httpx.Response(200, json=body)
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        monkeypatch.setattr(httpx, "get", http.get)
        monkeypatch.setattr(httpx, "post", http.post)
        main._publish_completed_review(database, rid, "owner/repo", 3)
        first = records(database)
        assert [i["gh_review_id"] for i in first] == [70, 70, 70]
        assert [i["gh_comment_id"] for i in first] == [None, 502, None]
        with sqlite3.connect(database) as conn:
            conn.execute("DELETE FROM published_findings")
        main._publish_completed_review(database, rid, "owner/repo", 3)
    assert len(posted) == 1
    assert len(records(database)) == 3
    assert any(r.url.path.endswith("/reviews/70/comments") for r in requests)
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT bot_login FROM publishing_identities").fetchone()[0] == "publisher"
    assert github.quota_snapshot() == {}


def test_placement_checks_current_patch(database, monkeypatch, capsys):
    rid = stored_review(database)
    main._publish_completed_review(database, rid, "owner/repo", 3)
    monkeypatch.setattr(cli, "fetch_pr_files", lambda *a: [diff.FilePatch("app.py", "modified", "@@ -20 +20 @@\n+x")])
    monkeypatch.setattr(sys, "argv", ["argus", "publish-placement", str(rid), "--db", database])
    cli.main()
    output = capsys.readouterr().out
    assert "inline" in output and "commentable=no" in output
    assert "summary" in output and "capped" in output


@pytest.mark.parametrize("transport_error", [False, True])
def test_successful_post_survives_comment_lookup_failure(database, monkeypatch, caplog, transport_error):
    rid = stored_review(database)
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"id": 70, "user": {"login": "publisher"}})
        if request.url.path.endswith("/reviews/70/comments"):
            if transport_error:
                raise RuntimeError("private body write-secret")
            return httpx.Response(503, json={"message": "unavailable"})
        if request.url.path.endswith("/3"):
            return httpx.Response(200, json={"head": {"sha": HEAD}})
        return httpx.Response(200, json=[])
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        monkeypatch.setattr(httpx, "get", http.get)
        monkeypatch.setattr(httpx, "post", http.post)
        main._publish_completed_review(database, rid, "owner/repo", 3)
    assert db.get_review(database, rid)["publish_status"] == "posted"
    assert all(item["gh_review_id"] == 70 for item in records(database))
    assert len(records(database)) == 3
    assert "GitHub publishing failed" not in caplog.text
    assert "review was posted" in caplog.text
    assert "collector" in caplog.text
    assert ("RuntimeError" if transport_error else "HTTPStatusError") in caplog.text
    assert "private body" not in caplog.text and "write-secret" not in caplog.text
    assert "write-secret" not in db.get_review(database, rid)["publish_error"]
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1
    assert "unavailable" not in caplog.text


def test_fallback_records_summary_and_retains_separate_dry_run(database, monkeypatch):
    rid = stored_review(database)
    main._publish_completed_review(database, rid, "owner/repo", 3)
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    attempts = []
    def handler(request):
        if request.method == "POST":
            attempts.append(json.loads(request.content))
            if len(attempts) == 1:
                return httpx.Response(422, json={"message": "invalid line"})
            return httpx.Response(200, json={"id": 70, "user": {"login": "publisher"}})
        if request.url.path.endswith("/3"):
            return httpx.Response(200, json={"head": {"sha": HEAD}})
        return httpx.Response(200, json=[])
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        monkeypatch.setattr(httpx, "get", http.get)
        monkeypatch.setattr(httpx, "post", http.post)
        main._publish_completed_review(database, rid, "owner/repo", 3)
    items = records(database)
    assert len(items) == 6
    assert len(attempts) == 2
    assert all(i["placement"] == "summary" and i["gh_comment_id"] is None
               for i in items if i["mode"] == "posted")
    assert any(i["placement"] == "inline" for i in items if i["mode"] == "dry_run")


def test_each_successful_publishing_login_is_saved(database):
    from argus.publish import PublishOutcome

    rid = stored_review(database)
    for login in ("first", "second", "first"):
        db.save_publish_outcome(database, rid, PublishOutcome("posted", bot_login=login))
    with sqlite3.connect(database) as conn:
        identities = conn.execute("SELECT bot_login, first_seen_at FROM publishing_identities ORDER BY bot_login").fetchall()
    assert [row[0] for row in identities] == ["first", "second"]
    assert all(row[1] for row in identities)


@pytest.fixture
def collector_state(database, monkeypatch):
    from argus import acceptance

    monkeypatch.setenv("GITHUB_TOKEN", "read-secret")
    monkeypatch.setattr(github, "_quota", {})
    monkeypatch.setattr(github, "_pause_until", 0)
    monkeypatch.setattr(github, "_graphql_pause_until", 0)
    monkeypatch.setattr(github, "_cache", github.OrderedDict())
    monkeypatch.setattr(github, "graphql_read", lambda *a: {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}})
    return acceptance


def age_review(database, rid, seconds=180):
    from datetime import datetime, timedelta, timezone

    stamp = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    with db.connect(database) as conn:
        conn.execute("UPDATE reviews SET created_at=? WHERE id=?", (stamp, rid))
    return stamp


@pytest.mark.parametrize("login", ["visitor", None])
def test_reconciliation_does_not_adopt_foreign_identity(database, collector_state, monkeypatch, login):
    rid = stored_review(database)
    stamp = age_review(database, rid)
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    remote = {"id": 70, "body": f"<!-- argus:review={rid} head={HEAD} -->",
              "submitted_at": stamp, "user": {"login": login}}
    monkeypatch.setattr(github, "list_cached", lambda *a, **k: [remote])
    collector_state.collect(database)
    assert records(database) == []
    with db.connect(database) as conn:
        assert conn.execute("SELECT * FROM publishing_identities").fetchall() == []


def test_reconciliation_retries_unknown_identity(database, collector_state, monkeypatch):
    rid = stored_review(database)
    age_review(database, rid)
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    monkeypatch.delenv("ARGUS_PUBLISH_BOT_LOGIN")
    monkeypatch.setenv("ARGUS_PUBLISH_TOKEN", "unresolved-token")
    monkeypatch.setattr(httpx, "get", Mock(side_effect=httpx.ConnectError("offline")))
    monkeypatch.setattr(github, "list_cached", lambda *a, **k: [])
    collector_state.collect(database)
    with db.connect(database) as conn:
        assert conn.execute("SELECT * FROM publish_reconciliations").fetchall() == []


@pytest.mark.parametrize("failure", ["timeout", "storage"])
@pytest.mark.parametrize("remote_lines,expected_lines", [
    ({}, (10, 10)),
    ({"line": None, "start_line": None, "original_line": 223, "original_start_line": 221}, (221, 223)),
    ({"line": 66, "start_line": 56, "original_line": 65, "original_start_line": 55}, (55, 65)),
    ({"line": None, "start_line": None, "original_line": 65}, (65, 65)),
    ({"line": None, "start_line": None, "original_line": None, "original_start_line": None}, (10, 10)),
], ids=["rendered", "outdated", "remapped", "single-line", "finding-fallback"])
def test_collector_recovers_lost_post(database, collector_state, monkeypatch, failure, remote_lines, expected_lines):
    rid = stored_review(database)
    stamp = age_review(database, rid)
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    posted = []
    reads = []
    def handler(request):
        base = "/repos/owner/repo/pulls/3"
        if request.method == "POST":
            payload = json.loads(request.content)
            posted.append({"id": 70, "body": payload["body"], "submitted_at": stamp,
                           "user": {"login": "publisher"}})
            posted[0]["comments"] = [{**c, **remote_lines, "user": {"login": "publisher"}, "id": 502, "pull_request_review_id": 70}
                                     for c in payload["comments"]]
            if failure == "timeout":
                raise httpx.ReadTimeout("response lost")
            return httpx.Response(200, json=posted[0])
        reads.append((request.url.path, request.headers["authorization"]))
        if request.url.path == base:
            body = {"state": "open", "head": {"sha": HEAD}}
        elif request.url.path == base + "/reviews":
            body = posted
        elif request.url.path in (base + "/comments", base + "/reviews/70/comments"):
            body = posted[0]["comments"] if posted else []
        elif request.url.path == "/repos/owner/repo/pulls/comments":
            body = []
        else:
            raise AssertionError(request.url)
        return httpx.Response(200, json=body)
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        monkeypatch.setattr(httpx, "get", http.get)
        monkeypatch.setattr(httpx, "post", http.post)
        with monkeypatch.context() as patch:
            if failure == "storage":
                patch.setattr(db, "save_publish_outcome", Mock(side_effect=RuntimeError("storage failed")))
            main._publish_completed_review(database, rid, "owner/repo", 3)
        assert records(database) == []
        reads.clear()
        collector_state.collect(database)
    items = records(database)
    assert len(items) == 3
    assert [i["gh_comment_id"] for i in items] == [None, 502, None]
    assert (items[1]["line"], items[1]["end_line"]) == expected_lines
    assert items[1]["path"] == "app.py" and items[1]["head_sha"] == HEAD
    assert all(i["gh_review_id"] == 70 and i["mode"] == "posted" and i["posted_at"] == stamp for i in items)
    review = db.get_review(database, rid)
    assert review["publish_status"] == "posted" and review["published_at"] == stamp
    assert review["published_comments"] == 1 and "recover" in review["publish_error"]
    with db.connect(database) as conn:
        assert conn.execute("SELECT found FROM publish_reconciliations WHERE review_id=?", (rid,)).fetchone()[0] == 1
        assert conn.execute("SELECT bot_login FROM publishing_identities").fetchone()[0] == "publisher"
    assert all(token == "Bearer read-secret" for _, token in reads)
    assert collector_state.stats(database)["last_run"]["reconciled_found"] == 1
    assert collector_state.stats(database)["last_run"]["failures"] == 0


@pytest.mark.parametrize("age,mode,eligible", [(180, "1", True), (60, "1", False),
                                             (8 * 86400, "1", False), (180, "dry-run", False)])
def test_reconciliation_candidates_and_missing_checked_once(database, collector_state, monkeypatch, age, mode, eligible):
    rid = stored_review(database)
    age_review(database, rid, age)
    monkeypatch.setenv("ARGUS_PUBLISH", mode)
    calls = []
    def listing(url, params):
        calls.append(url)
        return []
    monkeypatch.setattr(github, "list_cached", listing)
    collector_state.collect(database)
    assert calls == (["/repos/owner/repo/pulls/3/reviews"] if eligible else [])
    assert collector_state.stats(database)["last_run"]["reconciled_missing"] == int(eligible)
    collector_state.collect(database)
    assert len(calls) == int(eligible)
    with db.connect(database) as conn:
        assert [row[0] for row in conn.execute("SELECT found FROM publish_reconciliations")] == ([0] if eligible else [])


def test_reconciliation_failure_retries_only_failed_review(database, collector_state, monkeypatch, caplog):
    first = stored_review(database)
    second = stored_review(database)
    for rid in (first, second):
        age_review(database, rid)
    with db.connect(database) as conn:
        conn.execute("UPDATE reviews SET pr=4 WHERE id=?", (second,))
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    calls = []
    def listing(url, params):
        calls.append(url)
        if "/3/" in url:
            raise ValueError("private body")
        return []
    monkeypatch.setattr(github, "list_cached", listing)
    for _ in range(2):
        collector_state.collect(database)
        assert collector_state.stats(database)["last_run"]["failures"] == 1
    assert len([url for url in calls if "/3/" in url]) == 2
    assert len([url for url in calls if "/4/" in url]) == 1
    assert "ValueError" in caplog.text and "private body" not in caplog.text


@pytest.mark.parametrize("column,value", [("repo", "owner/other"), ("pr", 0), ("status", "running"),
                                         ("head_sha", "z" * 40), ("head_sha", "abc"), ("head_sha", None),
                                         ("publish_status", "posted"), ("publish_status", "dry_run"),
                                         ("publish_status", "skipped")])
def test_reconciliation_excludes_ineligible_reviews(database, collector_state, monkeypatch, column, value):
    rid = stored_review(database)
    age_review(database, rid)
    with db.connect(database) as conn:
        conn.execute(f"UPDATE reviews SET {column}=? WHERE id=?", (value, rid))
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    collector_state.collect(database)
    assert collector_state.stats(database)["last_run"]["failures"] == 0
    with db.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM publish_reconciliations").fetchone()[0] == 0


def test_reconciliation_storage_failure_rolls_back_and_retries(database, collector_state, monkeypatch):
    from argus import publish, verdict

    rid = stored_review(database)
    stamp = age_review(database, rid)
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    payload = publish.render_review(HEAD, rid, verdict.document(db.get_review(database, rid), db.get_findings(database, rid)),
                                    diff.fetch_pr_files("owner/repo", 3), max_comments=1)
    review = {"id": 70, "submitted_at": stamp, "body": payload["body"], "user": {"login": "publisher"}}
    comments = [{**c, "user": {"login": "publisher"}, "id": 502, "pull_request_review_id": 70} for c in payload["comments"]]
    def listing(url, params):
        if url.endswith("/reviews"):
            return [review]
        return comments if url.endswith("/3/comments") else []
    monkeypatch.setattr(github, "list_cached", listing)
    monkeypatch.setattr(github, "get_json_cached", lambda *a: ({"state": "open", "head": {"sha": HEAD}}, ""))
    with db.connect(database) as conn:
        conn.execute("CREATE TRIGGER fail_recovery BEFORE INSERT ON publish_reconciliations "
                     "BEGIN SELECT RAISE(ABORT, 'storage failure'); END")
    collector_state.collect(database)
    assert records(database) == []
    assert db.get_review(database, rid)["publish_status"] is None
    with db.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM publishing_identities").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM publish_reconciliations").fetchone()[0] == 0
        conn.execute("DROP TRIGGER fail_recovery")
    collector_state.collect(database)
    assert len(records(database)) == 3
    assert collector_state.stats(database)["last_run"]["reconciled_found"] == 1


def test_reconciliation_rate_limit_stops_run(database, collector_state, monkeypatch):
    import time

    rid = stored_review(database)
    age_review(database, rid)
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    monkeypatch.setattr(github, "_pause_until", time.time() + 60)
    with pytest.raises(github.GitHubRateLimited):
        collector_state.collect(database)
    run = collector_state.stats(database)["last_run"]
    assert run["stopped_by_rate_limit"] and run["failures"] == 0
    with db.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM publish_reconciliations").fetchone()[0] == 0
