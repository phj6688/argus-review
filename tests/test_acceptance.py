"""Acceptance contracts with a temporary database and local responses."""

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import yaml

from argus import acceptance, github
from argus.api import db


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = str(tmp_path / "argus.db")
    monkeypatch.setenv("ARGUS_DB_PATH", path)
    monkeypatch.setenv("GITHUB_TOKEN", "read-secret")
    monkeypatch.setattr(github, "_quota", {})
    monkeypatch.setattr(github, "_pause_until", 0)
    monkeypatch.setattr(github, "_graphql_pause_until", 0)
    monkeypatch.setattr(github, "_cache", github.OrderedDict())
    monkeypatch.setattr(httpx, "get", lambda *a, **k: pytest.fail("Unexpected HTTP"))
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("Unexpected HTTP"))
    db.init_db(path)
    db.init_db(path)
    return path


def seed(database, **updates):
    rid = db.create_review(database, "owner/repo", 3, "test", head_sha="a" * 40)
    data = dict(review_id=rid, repo="owner/repo", pr=3, head_sha="a" * 40,
                fingerprint=f"fp-{rid}", severity="major", category="contracts",
                path="app.py", line=10, end_line=12, title="Missing Check", placement="inline",
                mode="posted", gh_review_id=20, gh_comment_id=rid + 100,
                posted_at=datetime.now(timezone.utc).isoformat()) | updates
    with sqlite3.connect(database) as conn:
        cols = ",".join(data)
        cursor = conn.execute(f"INSERT INTO published_findings ({cols}) VALUES ({','.join('?' for _ in data)})",
                              tuple(data.values()))
        conn.execute("INSERT OR IGNORE INTO publishing_identities VALUES ('owner/repo', 'publisher', '2026-01-01')")
        return cursor.lastrowid


def rows(path, table):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]


def test_configuration_defaults():
    environment = yaml.safe_load(Path("docker-compose.yml").read_text())["services"]["argus-api"]["environment"]
    example = dict(line.split("=", 1) for line in Path(".env.example").read_text().splitlines()
                   if line and not line.startswith("#") and "=" in line)
    for key, value in {"ARGUS_PUBLISH": "0", "ARGUS_PUBLISH_REPOS": "", "ARGUS_PUBLISH_TOKEN": "",
                       "ARGUS_PUBLISH_MIN_SEVERITY": "minor", "ARGUS_PUBLISH_MAX_COMMENTS": "4",
                       "ARGUS_OUTCOME_INTERVAL_TICKS": "10", "ARGUS_REPLY_LOOP": "1", "ARGUS_RECHECK_MAX_PER_RUN": "10", "ARGUS_REPLY_MAX_WRITES_PER_RUN": "20"}.items():
        assert environment[key] == "${" + key + ":-" + value + "}"
        assert example[key] == value


@pytest.mark.parametrize("text,expected", [
    (None, "none"), ("", "other"), ("\n **FIXED** now", "fixed"),
    *[(verb + " this", "fixed") for verb in
      ("fix", "done", "addressed", "resolved", "applied", "implemented", "added", "updated", "changed")],
    *[(verb + " this", "rejected") for verb in
      ("reject", "rejected", "rejecting", "not changing", "won't fix", "won’t fix", "wontfix",
       "declined", "disagree", "no change", "keeping", "intentional", "by design", "false positive")],
    *[(verb + " this", "deferred") for verb in
      ("defer", "deferred", "follow-up", "followup", "tracked in", "out of scope", "later")],
    ("Thanks, fixed", "other"), ("Thanks\nFixed", "other"), ("fixing", "other"),
    ("donezo", "other"), ("** **\nFixed", "other"),
])
def test_reply_opening(text, expected):
    assert acceptance.classify_reply(text) == expected


@pytest.mark.parametrize("reply,addressed,resolved,label,loose", [
    ("fixed", False, False, "strict_accepted", True),
    ("none", True, True, "loose_accepted", True),
    ("rejected", True, True, "rejected", False),
    ("deferred", True, True, "deferred", False),
    ("rejected", True, False, "rejected", False),
    ("deferred", True, False, "deferred", False),
    ("other", True, False, "loose_accepted", True),
    ("none", False, True, "unconfirmed", False),
    ("other", False, False, "unconfirmed", False),
])
def test_labels(reply, addressed, resolved, label, loose):
    assert acceptance.labels(reply, addressed, resolved) == (label, loose)


@pytest.mark.parametrize("query", ["mutation { x }", "{ x }", "# comment\nquery { x }",
                                        "query { x } mutation { y }", "querying { x }"])
def test_graphql_refuses_writes(database, query):
    with pytest.raises(ValueError):
        github.graphql_read(query, {})


def test_graphql_read_records_quota(database, monkeypatch):
    def post(url, **kwargs):
        assert url == "https://api.github.com/graphql"
        assert kwargs["headers"]["Authorization"] == "Bearer read-secret"
        assert kwargs["json"] == {"query": "query { viewer { login } }", "variables": {}}
        return httpx.Response(200, json={"data": {"viewer": {"login": "reader"}}},
                              headers={"x-ratelimit-resource": "graphql", "x-ratelimit-remaining": "900"},
                              request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx, "post", post)
    assert github.graphql_read("query { viewer { login } }", {})["viewer"]["login"] == "reader"
    assert github.quota_snapshot()["graphql"]["remaining"] == 900
    monkeypatch.setattr(github, "_pause_until", time.time() + 60)
    with pytest.raises(github.GitHubRateLimited):
        github.graphql_read("query { viewer { login } }", {})


@pytest.mark.parametrize("file,expected", [
    ({"filename": "app.py", "patch": "@@ -8,1 +8,1 @@\n-x\n+y"}, True),
    ({"filename": "app.py", "patch": "@@ -14,0 +14,1 @@\n+y"}, True),
    ({"filename": "app.py", "patch": "@@ -15,1 +15,1 @@\n-x\n+y"}, False),
    ({"filename": "app.py", "status": "removed"}, True),
    ({"filename": "new.py", "previous_filename": "app.py", "status": "renamed"}, True),
    ({"filename": "other.py", "status": "removed"}, False),
    ({"filename": "app.py"}, None),
])
def test_addressed_region(file, expected):
    assert acceptance.region_changed({"path": "app.py", "line": 10, "end_line": 12}, [file]) == expected


def test_collector_replies_resolution_changes_and_cursor(database, monkeypatch):
    seed(database)
    seed(database, path="deleted.py", gh_comment_id=102)
    seed(database, path="renamed.py", gh_comment_id=103)
    seed(database, path="distant.py", gh_comment_id=104)
    stamp = datetime.now(timezone.utc).isoformat()
    calls = []
    def get(url, **kwargs):
        assert kwargs["headers"]["Authorization"] == "Bearer read-secret"
        calls.append((url, kwargs.get("params")))
        if url.endswith("/pulls/comments"):
            page = (kwargs.get("params") or {}).get("page")
            if str(page) == "2":
                body = [{"id": 9, "in_reply_to_id": 101, "body": "Rejected",
                         "user": {"login": "author"}, "author_association": "MEMBER", "created_at": "2026-01-01T00:00:00Z", "updated_at": stamp}]
                headers = {}
            else:
                body = [{"id": 7, "in_reply_to_id": 101, "body": "fixed", "user": {"login": "publisher"},
                         "created_at": "2025-01-01T00:00:00Z", "updated_at": stamp},
                        {"id": 8, "in_reply_to_id": 101, "body": "fixed", "user": {"login": "author"}, "author_association": "MEMBER",
                         "created_at": "2026-02-01T00:00:00Z", "updated_at": stamp}]
                headers = {"link": '<https://api.github.com/repos/owner/repo/pulls/comments?page=2>; rel="next"'}
        elif "/compare/" in url:
            body = {"status": "ahead", "files": [{"filename": "app.py", "patch": "@@ -10 +10 @@\n-a\n+b"},
                              {"filename": "deleted.py", "status": "removed"},
                              {"filename": "new.py", "previous_filename": "renamed.py", "status": "renamed"},
                              {"filename": "distant.py", "patch": "@@ -100 +100 @@\n-a\n+b"}]}
            headers = {}
        else:
            body = {"state": "open", "head": {"sha": "b" * 40}}
            headers = {}
        return httpx.Response(200, json=body, headers=headers, request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx, "get", get)
    def post(url, **kwargs):
        assert kwargs["json"]["query"].lstrip().startswith("query")
        return httpx.Response(200, json={"data": {"repository": {"pullRequest": {"reviewThreads": {
            "nodes": [{"isResolved": False, "comments": {"nodes": [{"databaseId": 101}]}},
                      {"isResolved": True, "comments": {"nodes": [{"databaseId": 102}]}}]}}}}},
                              request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx, "post", post)
    acceptance.collect(database)
    outcomes = rows(database, "finding_outcomes")
    assert len([o for o in outcomes if o["kind"] == "reply"]) == 2
    assert len([o for o in outcomes if o["kind"] == "addressed"]) == 4
    assert len([o for o in outcomes if o["kind"] == "addressed" and o["value"] == "true"]) == 3
    assert len([o for o in outcomes if o["kind"] == "resolved"]) == 2
    assert len([c for c in calls if "/compare/" in c[0]]) == 1
    assert acceptance._stamp(rows(database, "outcome_cursors")[0]["since"]) == acceptance._stamp(stamp) - timedelta(seconds=1)
    assert rows(database, "known_fps_live")[0]["reply_id"] == "9"
    acceptance.collect(database)
    assert len(rows(database, "finding_outcomes")) == len(outcomes)
    report = acceptance.stats(database)
    assert report["totals"]["posted"] == 4
    assert report["totals"]["rejected"] == 1
    assert report["totals"]["strict_accepted"] == 0
    assert report["totals"]["loose_accepted"] == 2
    assert report["totals"]["unconfirmed"] == 1
    assert report["totals"]["strict_rate"] == pytest.approx(0 / 4)
    assert report["totals"]["loose_rate"] == pytest.approx(2 / 4)
    assert report["by_repo"]["owner/repo"]["posted"] == 4
    assert report["by_severity"]["major"]["posted"] == 4
    assert report["bot_logins"] == {"owner/repo": ["publisher"]}
    assert report["last_collection_at"]
    assert "resolved" in report["note"] and "marker" in report["note"]


def test_quota_pause_preserves_cursor(database, monkeypatch):
    seed(database)
    with sqlite3.connect(database) as conn:
        conn.execute("INSERT INTO outcome_cursors(repo, since) VALUES ('owner/repo', '2026-01-01T00:00:00Z')")
    monkeypatch.setattr(github, "_pause_until", time.time() + 60)
    with pytest.raises(github.GitHubRateLimited):
        acceptance.collect(database)
    assert rows(database, "outcome_cursors")[0]["since"] == "2026-01-01T00:00:00Z"


def test_wilson():
    assert acceptance.wilson(0, 0) == [None, None]
    assert acceptance.wilson(5, 10) == pytest.approx([0.236593, 0.763407], abs=1e-6)
    assert acceptance.wilson(0, 10)[0] == pytest.approx(0)
    assert acceptance.wilson(10, 10)[1] == pytest.approx(1)


def test_stats_exclude_dry_run_and_apply_since(database):
    assert acceptance.stats(database)["last_run"] is None
    seed(database, mode="dry_run")
    seed(database, posted_at=(datetime.now(timezone.utc) - timedelta(days=40)).isoformat())
    assert acceptance.stats(database)["totals"]["posted"] == 1
    assert acceptance.stats(database, since=datetime.now(timezone.utc).date().isoformat())["totals"]["posted"] == 0
    assert json.loads(json.dumps(acceptance.stats(database)))["totals"]["strict_rate"] == 0


def test_rejection_suppresses_same_file_and_normalized_title(database):
    from argus.jarvis import live
    from argus.schema import Finding

    fid = seed(database)
    item = Finding(file="app.py", line=10, severity="major", confidence=0.9,
                   title="  MISSING   check ", description="Input", proposed_fix="Validate")
    live.invalidate_live_fps()
    assert live.suppress_known_fps("owner/repo", [item])[0] == [item]
    with db.connect(database) as conn:
        acceptance._record(conn, fid, "reply", "rejected", 900, db._now_iso(), db._now_iso(),
                           body="Rejected", author_association="MEMBER")
        acceptance._sync_rejections(conn, conn.execute("SELECT * FROM published_findings").fetchall(), db._now_iso())
    other = item.model_copy(update={"file": "other.py"})
    kept, suppressed = live.suppress_known_fps("owner/repo", [item, other])
    assert kept == [other]
    assert suppressed == [item]
    assert live.suppress_known_fps("owner/other", [item])[0] == [item]


def test_live_suppression_cache_expires(database, monkeypatch):
    from argus.jarvis import live

    live.invalidate_live_fps()
    clock = [100.0]
    monkeypatch.setattr(live.time, "monotonic", lambda: clock[0])
    assert not live._live_fp_keys()
    with sqlite3.connect(database) as conn:
        conn.execute("INSERT INTO known_fps_live VALUES ('owner/repo','app.py','missing check','fp','9','2026-01-01')")
    clock[0] = 159.0
    assert not live._live_fp_keys()
    clock[0] = 160.0
    assert ("owner/repo", "app.py", "missing check") in live._live_fp_keys()


def test_watcher_collection_interval_and_failure_isolation(database, monkeypatch):
    import asyncio
    from argus.api import watcher

    monkeypatch.setenv("ARGUS_OUTCOME_INTERVAL_TICKS", "2")
    monkeypatch.setattr(watcher, "_ticks", {})
    monkeypatch.setattr(watcher, "discover_repos", lambda path: None)
    monkeypatch.setattr(watcher, "_watched_repos", lambda path: [])
    calls = []
    def collect(path):
        calls.append(path)
        raise RuntimeError("failed collection")
    monkeypatch.setattr(acceptance, "collect", collect)
    async def ticks():
        for _ in range(4):
            assert await watcher.poll_once(database) == 0
            task = watcher._outcome_tasks.get(database)
            if task:
                await task
    asyncio.run(ticks())
    assert calls == [database, database]
    assert watcher._ticks[database] == 4


def test_stats_endpoint_and_read_only_scripts(database, monkeypatch, capsys):
    import runpy
    import sys
    from argus.api import main

    seed(database)
    payload = main.get_stats()["ops"]["acceptance"]
    assert payload == acceptance.stats(database)
    with sqlite3.connect(database) as conn:
        conn.execute("INSERT INTO known_fps_live VALUES ('owner/repo','app.py','missing check','fp','9','2026-01-01')")
    before = Path(database).read_bytes()
    monkeypatch.setattr(sys, "argv", ["acceptance_report.py", database, "--since", "2026-01-01"])
    runpy.run_path("scripts/acceptance_report.py", run_name="__main__")
    assert json.loads(capsys.readouterr().out)["totals"]["posted"] == 1
    monkeypatch.setattr(sys, "argv", ["export_known_fps.py", database])
    runpy.run_path("scripts/export_known_fps.py", run_name="__main__")
    exported = json.loads(capsys.readouterr().out)
    assert exported == {"repo": "owner/repo", "file": "app.py", "title": "missing check",
                        "fingerprint": "fp", "reply_id": "9", "rejected_at": "2026-01-01",
                        "verdict": "FALSE_POSITIVE",
                        "reason": "Author rejected the published finding (reply 9)"}
    assert Path(database).read_bytes() == before


def test_old_posted_finding_on_open_pr_still_collects_resolution(database, monkeypatch):
    monkeypatch.setattr(github, "list_cached", lambda *a, **k: [])
    seed(database, posted_at=(datetime.now(timezone.utc) - timedelta(days=40)).isoformat())
    monkeypatch.setattr(github, "get_json_cached", lambda *a, **k: ({"state": "open", "head": {"sha": "a" * 40}}, ""))
    monkeypatch.setattr(github, "graphql_read", lambda *a: {"repository": {"pullRequest": {"reviewThreads": {
        "nodes": [{"isResolved": True, "comments": {"nodes": [{"databaseId": 101}]}}]}}}})
    acceptance.collect(database)
    assert rows(database, "finding_outcomes")[0]["kind"] == "resolved"


def test_interrupted_reply_pagination_replays_without_losing_cursor(database, monkeypatch):
    seed(database)
    since = "2026-01-01T00:00:00Z"
    with sqlite3.connect(database) as conn:
        conn.execute("INSERT INTO outcome_cursors(repo, since) VALUES ('owner/repo', ?)", (since,))
    def get(url, **kwargs):
        params = kwargs.get("params") or {}
        if str(params.get("page")) == "2":
            return httpx.Response(429, json={"message": "rate limit"}, headers={"retry-after": "60"},
                                  request=httpx.Request("GET", url))
        return httpx.Response(200, json=[{"id": 9, "in_reply_to_id": 101, "body": "fixed",
                                         "user": {"login": "author"}, "author_association": "MEMBER", "created_at": since,
                                         "updated_at": "2026-02-01T00:00:00Z"}],
                              headers={"etag": '"one"', "link": '<https://api.github.com/repos/owner/repo/pulls/comments?page=2>; rel="next"'},
                              request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx, "get", get)
    with pytest.raises(github.GitHubRateLimited):
        acceptance.collect(database)
    assert not rows(database, "finding_outcomes")
    assert rows(database, "outcome_cursors")[0]["since"] == since
    assert github.pause_until() > time.time()


def test_pause_after_replies_keeps_committed_reply_and_cursor(database, monkeypatch):
    seed(database)
    now = db._now_iso()
    monkeypatch.setattr(github, "list_cached", lambda *a, **k: [
        {"id": 9, "in_reply_to_id": 101, "body": "fixed", "user": {"login": "author"}, "author_association": "MEMBER",
         "created_at": now, "updated_at": now}])
    def paused(*args, **kwargs):
        raise github.GitHubRateLimited(time.time() + 60)
    monkeypatch.setattr(github, "get_json_cached", paused)
    with pytest.raises(github.GitHubRateLimited):
        acceptance.collect(database)
    assert rows(database, "finding_outcomes")[0]["value"] == "fixed"
    assert acceptance._stamp(rows(database, "outcome_cursors")[0]["since"]) == acceptance._stamp(now) - timedelta(seconds=1)
    assert rows(database, "outcome_cursors")[0]["last_collected_at"] is None
    run = acceptance.stats(database)["last_run"]
    assert run["stopped_by_rate_limit"] and run["finished_at"]
    assert run["failures"] == 0


def test_missing_comment_ids_recovered_before_replies(database, monkeypatch):
    seed(database, gh_comment_id=None)
    now = db._now_iso()
    def listing(url, params=None):
        if url.endswith("/reviews/20/comments"):
            return [{"id": 101, "body": "Finding\n<!-- argus:fp=fp-1 -->"}]
        return [{"id": 9, "in_reply_to_id": 101, "body": "fixed", "user": {"login": "author"}, "author_association": "MEMBER",
                 "created_at": now, "updated_at": now}]
    monkeypatch.setattr(github, "list_cached", listing)
    monkeypatch.setattr(github, "get_json_cached", lambda *a, **k: ({"state": "closed", "closed_at": "2026-01-01T00:00:00Z", "head": {"sha": "a" * 40}}, ""))
    acceptance.collect(database)
    assert rows(database, "published_findings")[0]["gh_comment_id"] == 101
    assert rows(database, "finding_outcomes")[0]["value"] == "fixed"


def test_addressed_resolved_rejection_does_not_seed_suppression(database, monkeypatch):
    seed(database)
    now = db._now_iso()
    monkeypatch.setattr(github, "list_cached", lambda *a, **k: [
        {"id": 9, "in_reply_to_id": 101, "body": "rejected", "user": {"login": "author"}, "author_association": "MEMBER",
         "created_at": now, "updated_at": now}])
    monkeypatch.setattr(github, "get_json_cached", lambda *a: ({"state": "open", "head": {"sha": "b" * 40}}, ""))
    monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Response(
        200, json={"status": "ahead", "files": [{"filename": "app.py", "status": "removed"}]},
        request=httpx.Request("GET", url)))
    monkeypatch.setattr(github, "graphql_read", lambda *a: {"repository": {"pullRequest": {"reviewThreads": {
        "nodes": [{"isResolved": True, "comments": {"nodes": [{"databaseId": 101}]}}]}}}})
    acceptance.collect(database)
    assert acceptance.stats(database)["totals"]["rejected"] == 1
    assert not rows(database, "known_fps_live")


def test_cursor_replays_same_second_updates(database, monkeypatch):
    monkeypatch.setattr(github, "graphql_read", lambda *a: {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}})
    seed(database)
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    comments = [{"id": 8, "body": "unrelated", "user": {"login": "author"}, "author_association": "MEMBER",
                 "created_at": stamp, "updated_at": stamp}]
    def listing(url, params):
        return [c for c in comments if acceptance._stamp(c["updated_at"]) > acceptance._stamp(params["since"])]
    monkeypatch.setattr(github, "list_cached", listing)
    monkeypatch.setattr(github, "get_json_cached", lambda *a: ({"state": "closed", "closed_at": stamp, "head": {"sha": "a" * 40}}, ""))
    acceptance.collect(database)
    comments.append({"id": 9, "in_reply_to_id": 101, "body": "fixed", "user": {"login": "author"}, "author_association": "MEMBER",
                     "created_at": stamp, "updated_at": stamp})
    acceptance.collect(database)
    assert [o["value"] for o in rows(database, "finding_outcomes") if o["kind"] == "reply"] == ["fixed"]


def test_exhausted_query_quota_leaves_rest_available(database, monkeypatch):
    reset = int(time.time()) + 60
    def post(url, **kwargs):
        return httpx.Response(200, json={"data": {}},
                              headers={"x-ratelimit-resource": "graphql", "x-ratelimit-remaining": "0",
                                       "x-ratelimit-reset": str(reset)}, request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(github.GraphQLRateLimited):
        github.graphql_read("query { viewer { login } }", {})
    assert github.pause_until() == 0
    monkeypatch.setattr(httpx, "get", lambda url, **kwargs: httpx.Response(
        200, json={"state": "open"}, request=httpx.Request("GET", url)))
    assert github.get("/repos/owner/repo/pulls/3").status_code == 200
    with pytest.raises(github.GitHubRateLimited):
        github.graphql_read("query { viewer { login } }", {})


def test_compare_failure_isolated_and_run_stats(database, monkeypatch, caplog):
    seed(database)
    second = seed(database, pr=4, head_sha="b" * 40, placement="summary")
    monkeypatch.setattr(github, "list_cached", lambda *a, **k: [])
    monkeypatch.setattr(github, "graphql_read", lambda *a: {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}})
    def get(url, **kwargs):
        if "a" * 40 in url:
            raise httpx.HTTPStatusError("private body", request=httpx.Request("GET", "https://api.github.com"),
                                        response=httpx.Response(404))
        return httpx.Response(200, json={"status": "ahead", "files": [{"filename": "app.py", "status": "removed"}]},
                              request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(github, "get_json_cached", lambda *a: ({"state": "open", "head": {"sha": "c" * 40}}, ""))
    acceptance.collect(database)
    assert any(o["finding_id"] == second and o["kind"] == "addressed" for o in rows(database, "finding_outcomes"))
    warnings = list({id(r): r for r in caplog.records if r.levelname == "WARNING"}.values())
    assert len(warnings) == 1
    assert "owner/repo" in warnings[0].message and "3" in warnings[0].message
    assert "HTTPStatusError" in warnings[0].message and "private body" not in caplog.text
    assert warnings[0].exc_info is None
    run = acceptance.stats(database)["last_run"]
    assert set(run) == {"started_at", "finished_at", "repos", "prs_checked", "prs_skipped_final", "failures", "stopped_by_rate_limit",
                        "graphql_paused", "compares_not_ancestor", "reconciled_found", "reconciled_missing", "bot", "rechecks_attempted", "writes_attempted"}
    assert run["repos"] == 1 and run["prs_checked"] == 2 and run["failures"] == 1
    assert run["finished_at"] >= run["started_at"] and not run["stopped_by_rate_limit"]
    assert len(rows(database, "outcome_prs")) == 2


def test_final_pr_costs_no_requests(database):
    seed(database, gh_comment_id=None)
    with sqlite3.connect(database) as conn:
        conn.execute("INSERT INTO outcome_prs VALUES (?, ?, ?, ?, ?, ?, ?)",
                     ("owner/repo", 3, "closed", 1, (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(), "b" * 40, db._now_iso()))
    acceptance.collect(database)
    run = acceptance.stats(database)["last_run"]
    assert run["prs_checked"] == 0 and run["prs_skipped_final"] == 1
    assert run["failures"] == 0


def test_reply_cursor_clamped_and_pending_recovery_replays(database, monkeypatch):
    now = datetime.now(timezone.utc)
    seed(database, posted_at=(now - timedelta(days=90)).isoformat())
    with sqlite3.connect(database) as conn:
        conn.execute("INSERT INTO outcome_cursors(repo, since) VALUES (?, ?)",
                     ("owner/repo", (now - timedelta(days=80)).isoformat()))
    calls = []
    def listing(url, params):
        calls.append((url, params))
        return []
    monkeypatch.setattr(github, "list_cached", listing)
    monkeypatch.setattr(github, "get_json_cached", lambda *a: ({"state": "open", "head": {"sha": "a" * 40}}, ""))
    monkeypatch.setattr(github, "graphql_read", lambda *a: {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}})
    acceptance.collect(database)
    since = acceptance._stamp(calls[0][1]["since"])
    assert now - timedelta(days=30) <= since <= datetime.now(timezone.utc) - timedelta(days=30)
    seed(database, gh_comment_id=None, posted_at=(now - timedelta(days=60)).isoformat())
    calls.clear()
    acceptance.collect(database)
    reply_since = next(params["since"] for url, params in calls if url.endswith("/pulls/comments"))
    assert acceptance._stamp(reply_since) <= now - timedelta(days=60)


def test_second_identity_and_typed_accounts_are_excluded(database, monkeypatch):
    seed(database)
    now = db._now_iso()
    with sqlite3.connect(database) as conn:
        conn.execute("INSERT INTO publishing_identities VALUES (?, ?, ?)", ("owner/repo", "second", now))
    monkeypatch.setattr(github, "list_cached", lambda *a, **k: [
        {"id": i, "in_reply_to_id": 101, "body": "fixed", "user": user, "created_at": now, "updated_at": now}
        for i, user in enumerate([{"login": "SECOND"}, {"login": "other", "type": "Bot"}], 1)])
    monkeypatch.setattr(github, "get_json_cached", lambda *a: ({"state": "closed", "head": {"sha": "a" * 40}}, ""))
    acceptance.collect(database)
    assert not [o for o in rows(database, "finding_outcomes") if o["kind"] == "reply"]
    assert acceptance.stats(database)["bot_logins"] == {"owner/repo": ["publisher", "second"]}


def test_missing_identity_only_fails_replies_for_its_repo(database, monkeypatch, caplog):
    seed(database, repo="owner/missing")
    seed(database)
    monkeypatch.setattr(github, "list_cached", lambda *a, **k: [])
    fetched = []
    def cached(url):
        fetched.append(url)
        return {"state": "closed", "merged_at": "2026-01-01", "head": {"sha": "a" * 40}}, ""
    monkeypatch.setattr(github, "get_json_cached", cached)
    acceptance.collect(database)
    assert fetched == ["/repos/owner/missing/pulls/3", "/repos/owner/repo/pulls/3"]
    assert "owner/missing" in caplog.text and "RuntimeError" in caplog.text
    assert acceptance.stats(database)["last_run"]["failures"] == 1
    assert rows(database, "outcome_prs")[0]["merged"] == 1


def test_comment_recovery_failure_only_fails_its_review(database, monkeypatch, caplog):
    seed(database, gh_comment_id=None)
    seed(database, pr=4, gh_review_id=21, gh_comment_id=None)
    def listing(url, params):
        if "/reviews/20/" in url:
            raise ValueError("private body")
        if "/reviews/21/" in url:
            return [{"id": 102, "body": "Finding\n<!-- argus:fp=fp-2 -->"}]
        return []
    monkeypatch.setattr(github, "list_cached", listing)
    monkeypatch.setattr(github, "get_json_cached", lambda *a: ({"state": "closed", "head": {"sha": "a" * 40}}, ""))
    acceptance.collect(database)
    assert rows(database, "published_findings")[1]["gh_comment_id"] == 102
    assert "ValueError" in caplog.text and "private body" not in caplog.text
    assert acceptance.stats(database)["last_run"]["failures"] == 1


def test_rates_placements_and_closed_subset(database):
    now = db._now_iso()
    for pr in (3, 4):
        for placement, signal in [("inline", "fixed"), ("summary", "addressed"), ("inline", "rejected"),
                                  ("inline", "deferred"), ("summary", None)]:
            fid = seed(database, pr=pr, placement=placement)
            if signal:
                with db.connect(database) as conn:
                    acceptance._record(conn, fid, "addressed" if signal == "addressed" else "reply",
                                       "true" if signal == "addressed" else signal, fid, now, now,
                                       body=signal, author_association="MEMBER")
    with sqlite3.connect(database) as conn:
        conn.execute("INSERT INTO outcome_prs VALUES ('owner/repo',3,'closed',1,?,?,?)", (now, "a" * 40, now))
        conn.execute("INSERT INTO outcome_prs VALUES ('owner/repo',4,'open',0,NULL,?,?)", ("a" * 40, now))
    report = acceptance.stats(database)
    counts = report["totals"]
    assert "decided" not in counts
    assert counts["posted"] == 10 and counts["unconfirmed"] == 2
    assert counts["rejected"] == counts["deferred"] == 2
    assert counts["strict_rate"] == 0.2 and counts["loose_rate"] == 0.4
    assert counts["strict_wilson_95"] == pytest.approx([0.056682, 0.509838], abs=1e-6)
    assert counts["loose_wilson_95"] == pytest.approx([0.168180, 0.687326], abs=1e-6)
    assert report["by_placement"]["inline"]["posted"] == 6
    assert report["by_placement"]["summary"]["strict_accepted"] == 0
    final = report["final"]
    assert set(final) == {"totals", "by_repo", "by_severity", "by_placement", "unverified"}
    assert final["totals"]["posted"] == 5 and final["totals"]["strict_rate"] == 0.2
    assert final["by_repo"]["owner/repo"]["posted"] == 5
    assert final["by_severity"]["major"]["posted"] == 5
    assert final["by_placement"]["summary"]["posted"] == 2


@pytest.mark.parametrize("patch,line,expected", [
    ("@@ -45,7 +45,7 @@\n a\n b\n c\n-old\n+new\n d\n e\n f", 43, False),
    ("@@ -45,7 +45,7 @@\n a\n b\n c\n-old\n+new\n d\n e\n f", 46, True),
    ("@@ -48,6 +48,7 @@\n a\n b\n c\n+new\n d\n e\n f", 52, True),
    ("@@ -48,6 +48,7 @@\n a\n b\n c\n+new\n d\n e\n f", 53, False),
    ("@@ -48,4 +48,4 @@\n a\n-b\n-c\n+x\n\\ No newline at end of file\n+y\n d", 52, True),
    ("@@ -50,0 +51 @@\n+new", 52, True),
    ("@@ -50,0 +51 @@\n+new", 53, False),
    (None, 48, None),
])
def test_changed_lines_only(patch, line, expected):
    assert acceptance.region_changed({"path": "app.py", "line": line, "end_line": None},
                                     [{"filename": "app.py", "patch": patch}]) is expected


def collector_http(monkeypatch, *, status="ahead", query_error=None, compare_error=None, reply_error=False, replies=()):
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        if "/compare/" in url:
            if compare_error and "a" * 40 in url:
                raise compare_error
            body = {"status": status, "files": [{"filename": "app.py", "status": "removed"}]}
        elif url.endswith("/pulls/comments"):
            if reply_error:
                raise ValueError("private body")
            body = list(replies)
        else:
            body = {"state": "open", "head": {"sha": "c" * 40}}
        return httpx.Response(200, json=body, headers={"etag": "test"}, request=httpx.Request("GET", url))
    def query(*args):
        calls.append("query")
        if query_error:
            raise query_error
        return {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}
    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(github, "graphql_read", query)
    return calls


@pytest.mark.parametrize("status,value,count", [("ahead", "true", 0), ("identical", "false", 0),
                                                 ("diverged", "unknown", 1), ("behind", "unknown", 1)])
def test_compare_once_without_cache(database, monkeypatch, status, value, count):
    seed(database)
    calls = collector_http(monkeypatch, status=status)
    acceptance.collect(database)
    judged = [o for o in rows(database, "finding_outcomes") if o["kind"] == "addressed"]
    assert [(o["value"], o["evidence_id"]) for o in judged] == [(value, "c" * 40)]
    assert acceptance.stats(database)["last_run"]["compares_not_ancestor"] == count
    acceptance.collect(database)
    assert len([url for url in calls if "/compare/" in url]) == 1
    assert not any("/compare/" in key for key in github._cache)
    assert github._cache


@pytest.mark.parametrize("failure", ["query", "compare", "limit"])
def test_pr_steps_isolated_and_rejection_always_synced(database, monkeypatch, caplog, failure):
    first = seed(database)
    second = seed(database, head_sha="b" * 40)
    with db.connect(database) as conn:
        acceptance._record(conn, first, "reply", "rejected", 900, db._now_iso(),
                           body="Rejected", author_association="MEMBER")
    error = github.GitHubRateLimited(time.time() + 60) if failure == "limit" else ValueError("private body")
    calls = collector_http(monkeypatch, query_error=error if failure == "query" else None,
                           compare_error=error if failure != "query" else None)
    if failure == "limit":
        with pytest.raises(github.GitHubRateLimited):
            acceptance.collect(database)
    else:
        acceptance.collect(database)
        assert any(o["finding_id"] == second and o["kind"] == "addressed"
                   for o in rows(database, "finding_outcomes"))
        assert acceptance.stats(database)["last_run"]["failures"] == 1
        assert "private body" not in caplog.text and "ValueError" in caplog.text
    assert rows(database, "known_fps_live")[0]["reply_id"] == "900"
    assert any("/compare/" in url for url in calls)


def test_query_pause_keeps_later_repo_replies(database, monkeypatch):
    seed(database)
    seed(database, repo="owner/second")
    with db.connect(database) as conn:
        conn.execute("INSERT INTO publishing_identities VALUES ('owner/second','publisher',?)", (db._now_iso(),))
    stamp = db._now_iso()
    calls = collector_http(monkeypatch, query_error=github.GraphQLRateLimited(time.time() + 60), replies=[
        {"id": 900, "in_reply_to_id": 102, "body": "fixed", "user": {"login": "author"}, "author_association": "MEMBER",
         "created_at": stamp, "updated_at": stamp}])
    acceptance.collect(database)
    assert calls.count("query") == 1
    assert [o["value"] for o in rows(database, "finding_outcomes") if o["kind"] == "reply"] == ["fixed"]
    assert "https://api.github.com/repos/owner/second/pulls/comments" in calls
    assert len([o for o in rows(database, "finding_outcomes") if o["kind"] == "addressed"]) == 2
    run = acceptance.stats(database)["last_run"]
    assert run["graphql_paused"] and not run["stopped_by_rate_limit"]
    assert run["failures"] == 0


def test_missing_comments_stop_pinning_after_three_lookups(database, monkeypatch):
    now = datetime.now(timezone.utc)
    posted = now - timedelta(days=60)
    seed(database, gh_comment_id=None, posted_at=posted.isoformat())
    calls = []
    def listing(url, params):
        calls.append((url, params))
        return []
    monkeypatch.setattr(github, "list_cached", listing)
    monkeypatch.setattr(github, "get_json_cached", lambda *a: ({"state": "open", "head": {"sha": "a" * 40}}, ""))
    for _ in range(4):
        acceptance.collect(database)
    assert len([url for url, _ in calls if "/reviews/" in url]) == 3
    since = [acceptance._stamp(params["since"]) for url, params in calls if url.endswith("/pulls/comments")]
    assert since[0] < posted and since[1] < posted
    assert since[2] >= now - timedelta(days=30) and since[3] >= now - timedelta(days=30)
    assert rows(database, "published_findings")[0]["comment_lookups"] == 3


def test_reply_failure_still_checks_pr(database, monkeypatch):
    seed(database)
    collector_http(monkeypatch, reply_error=True)
    acceptance.collect(database)
    assert rows(database, "outcome_prs")[0]["state"] == "open"
    assert any(o["kind"] == "addressed" for o in rows(database, "finding_outcomes"))
    assert acceptance.stats(database)["last_run"]["failures"] == 1


def test_reopened_pr_is_active(database, monkeypatch):
    seed(database)
    now = db._now_iso()
    with db.connect(database) as conn:
        conn.execute("INSERT INTO outcome_prs VALUES (?,?, 'closed',0,?,?,?)",
                     ("owner/repo", 3, (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(), "a" * 40, now))
        conn.execute("INSERT INTO pr_heads(repo,pr,head_sha,first_seen_at,status,is_open) VALUES (?,?,?,?,?,1)",
                     ("owner/repo", 3, "c" * 40, now, "queued"))
    collector_http(monkeypatch)
    acceptance.collect(database)
    assert rows(database, "outcome_prs")[0]["state"] == "open"
    assert acceptance.stats(database)["last_run"]["prs_skipped_final"] == 0


def test_query_escape_refused_without_request(database, caplog):
    before = github.refusal_count()
    with pytest.raises(github.GitHubDenied):
        github.graphql_read(r'query { repository(owner:"\u006b", name:"repo") { id } }', {})
    assert github.refusal_count() == before + 1
    assert "refused request" in caplog.text


@pytest.mark.parametrize("code,headers,body", [
    (200, {"x-ratelimit-remaining": "0"}, {"data": {}}),
    (403, {"x-ratelimit-remaining": "0"}, {}),
    (429, {"retry-after": "60"}, {}),
    (403, {}, {"message": "rate limit exceeded"}),
    (200, {}, {"errors": [{"type": "RATE_LIMITED"}]}),
])
def test_query_limits_have_separate_exception(database, monkeypatch, code, headers, body):
    monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Response(
        code, headers=headers, json=body, request=httpx.Request("POST", url)))
    with pytest.raises(github.GraphQLRateLimited):
        github.graphql_read("query { viewer { login } }", {})
    assert github.pause_until() == 0
    with pytest.raises(github.GraphQLRateLimited):
        github.graphql_read("query { viewer { login } }", {})
    monkeypatch.setattr(github, "_pause_until", time.time() + 120)
    with pytest.raises(github.GitHubRateLimited) as error:
        github.graphql_read("query { viewer { login } }", {})
    assert type(error.value) is github.GitHubRateLimited


@pytest.mark.parametrize("files,value", [([], "false"), ([{"filename": "app.py"}], "unknown"),
                                         ([{"filename": "app.py", "patch": "@@ -48 +48 @@\n-a\n+b"}], "false")])
def test_unchanged_and_unavailable_patches_stay_judged(database, monkeypatch, files, value):
    seed(database)
    calls = collector_http(monkeypatch)
    def get(url, **kwargs):
        calls.append(url)
        return httpx.Response(200, json={"status": "ahead", "files": files},
                              headers={"etag": "large"}, request=httpx.Request("GET", url))
    monkeypatch.setattr(github, "get_json_cached", lambda *a: ({"state": "open", "head": {"sha": "c" * 40}}, ""))
    monkeypatch.setattr(httpx, "get", get)
    for _ in range(2):
        acceptance.collect(database)
    assert [o["value"] for o in rows(database, "finding_outcomes") if o["kind"] == "addressed"] == [value]
    assert len([url for url in calls if "/compare/" in url]) == 1
    assert not github._cache
    assert acceptance.stats(database)["totals"]["loose_accepted"] == 0


def test_compare_memo_and_partial_judgments(database, monkeypatch):
    first = seed(database)
    seed(database)
    seed(database, pr=4)
    with db.connect(database) as conn:
        acceptance._record(conn, first, "addressed", "false", "c" * 40, db._now_iso())
    calls = collector_http(monkeypatch)
    acceptance.collect(database)
    assert len([url for url in calls if "/compare/" in url]) == 1
    assert [o["value"] for o in rows(database, "finding_outcomes") if o["kind"] == "addressed"] == ["false", "true", "true"]


def test_resolution_and_compare_failures_count_separately(database, monkeypatch, caplog):
    seed(database)
    error = ValueError("private body")
    collector_http(monkeypatch, query_error=error, compare_error=error)
    acceptance.collect(database)
    assert acceptance.stats(database)["last_run"]["failures"] == 2
    assert "private body" not in caplog.text


def test_failed_compare_retries_without_rejudging_other_heads(database, monkeypatch):
    first = seed(database)
    second = seed(database, head_sha="b" * 40)
    collector_http(monkeypatch, compare_error=ValueError("failure"))
    acceptance.collect(database)
    assert [o["finding_id"] for o in rows(database, "finding_outcomes") if o["kind"] == "addressed"] == [second]
    calls = collector_http(monkeypatch)
    acceptance.collect(database)
    assert [url for url in calls if "/compare/" in url] == [f"https://api.github.com/repos/owner/repo/compare/{'a' * 40}...{'c' * 40}"]
    assert {o["finding_id"] for o in rows(database, "finding_outcomes") if o["kind"] == "addressed"} == {first, second}
