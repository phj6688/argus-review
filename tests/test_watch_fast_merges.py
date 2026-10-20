"""Closed listing recovery uses local responses and a temporary database."""

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from argus import github
from argus.api import db, main, watch_metrics, watch_store, watcher
from argus.repo_policy import RepoDenied
from argus.review import ReviewResult

NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
REPO = "example/a"
SHA = "a" * 40


def merged(number=1, **extra):
    return {"author_association": "OWNER", "number": number, "head": {"sha": SHA}, "user": {"login": "human"},
            "merged_at": (NOW - timedelta(minutes=2)).isoformat(), **extra}


@pytest.fixture
def path(tmp_path, monkeypatch, caplog):
    path = str(tmp_path / "argus.db")
    db.init_db(path)
    monkeypatch.setattr(watch_store, "utcnow", lambda: NOW)
    monkeypatch.setattr(watcher, "_ticks", {})
    monkeypatch.setattr(watcher, "_repo_pushed_at", {}, raising=False)
    monkeypatch.setattr(watcher, "_previous_watched_repos", {}, raising=False)
    monkeypatch.setattr(github, "_quota", {})
    monkeypatch.setattr(github, "_pause_until", 0)
    monkeypatch.setattr(github, "_cache", github.OrderedDict())
    monkeypatch.setenv("GITHUB_TOKEN", "fake")
    monkeypatch.setenv("ARGUS_WATCH_OWNERS", "example")
    monkeypatch.delenv("ARGUS_WATCH_FAST_MERGES", raising=False)
    monkeypatch.delenv("ARGUS_WATCH_INCLUDE_DRAFTS", raising=False)
    monkeypatch.delenv("ARGUS_WATCH_INCLUDE_BOTS", raising=False)
    monkeypatch.setenv("ARGUS_WATCH_DISCOVERY_TICKS", "1")
    monkeypatch.setenv("ARGUS_OUTCOME_INTERVAL_TICKS", "1000")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: pytest.fail("unexpected request"))
    monkeypatch.setattr(watcher, "_open_prs", lambda repo: [])
    monkeypatch.setattr(watch_metrics, "daily_report", lambda path: None)
    logger = logging.getLogger("argus.watcher")
    logger.addHandler(caplog.handler)
    try:
        yield path
    finally:
        logger.removeHandler(caplog.handler)


def discovery(monkeypatch):
    records = [{"full_name": REPO, "pushed_at": (NOW - timedelta(minutes=3)).isoformat()}]
    monkeypatch.setattr(github, "owned_repo_records", lambda: records)
    return records


def test_advanced_push_queues_once_and_worker_finishes(path, monkeypatch, caplog):
    records = discovery(monkeypatch)
    calls = []

    def get(url, **kwargs):
        request = httpx.Request("GET", url, params=kwargs["params"], headers=kwargs["headers"])
        calls.append(request)
        if request.headers.get("If-None-Match"):
            return httpx.Response(304, request=request)
        return httpx.Response(200, json=[merged()], request=request, headers={
            "ETag": "closed-page", "Link": '<https://api.github.com/repos/example/a/pulls?page=2>; rel="next"'})

    monkeypatch.setattr(httpx, "get", get)
    with caplog.at_level(logging.INFO, logger="argus.watcher"):
        assert asyncio.run(watcher.poll_once(path)) == 0
        assert calls == []
        records[0]["pushed_at"] = NOW.isoformat()
        assert asyncio.run(watcher.poll_once(path)) == 1
        assert asyncio.run(watcher.poll_once(path)) == 0
    assert len(calls) == 1
    assert dict(calls[0].url.params) == {
        "state": "closed", "sort": "updated", "direction": "desc", "per_page": "20", "page": "1"}
    row = watch_store.heads(path)[0]
    assert row["status"] == "queued"
    assert row["merged_before_seen"] == 1
    assert row["is_open"] == row["baseline"] == 0
    assert row["eligible_at"] == NOW.isoformat()
    assert row["eligible_pr_updated_at"] == merged()["merged_at"]
    assert len({id(r) for r in caplog.records if REPO + "#1" in r.message}) == 1

    seen = []
    def review(repo, pr, model, cap, **kwargs):
        seen.append((repo, pr, kwargs["expected_head_sha"]))
        return ReviewResult(raw_count=0)

    monkeypatch.setattr(main, "run_review", review)
    monkeypatch.setattr(main, "_publish_completed_review", lambda *a: None)
    assert watcher.process_one(path)
    assert seen == [(REPO, 1, SHA)]
    completed = watch_store.heads(path)[0]
    assert completed["status"] == "done"
    assert db.get_review(path, completed["review_id"])["head_sha"] == SHA
    records[0]["pushed_at"] = (NOW + timedelta(seconds=1)).isoformat()
    assert asyncio.run(watcher.poll_once(path)) == 0
    assert len(watch_store.heads(path)) == 1
    assert len(calls) == 2
    assert calls[1].headers["If-None-Match"] == "closed-page"
    assert not watcher.process_one(path)


@pytest.mark.parametrize("extra", [
    {"merged_at": None},
    {"merged_at": (NOW - timedelta(minutes=61)).isoformat()},
    {"merged_at": (NOW + timedelta(minutes=5, seconds=1)).isoformat()},
    {"draft": True}, {"user": {"type": "Bot"}}, {"user": {"login": "helper[bot]"}},
    {"head": {"sha": "bad"}}, {"head": {"sha": "g" * 40}},
])
def test_ineligible_closed_heads_are_ignored(path, extra):
    assert watch_store.record_fast_merges(path, REPO, [merged(**extra)], NOW) == 0
    assert watch_store.heads(path) == []


def test_include_flags_and_atomic_deduplication(path, monkeypatch):
    monkeypatch.setenv("ARGUS_WATCH_INCLUDE_DRAFTS", "1")
    monkeypatch.setenv("ARGUS_WATCH_INCLUDE_BOTS", "1")
    prs = [merged(draft=True, user={"type": "Bot"}), merged(2)]
    watch_store.record_listing(path, REPO, [merged(2)], NOW)
    assert watch_store.record_fast_merges(path, REPO, prs * 2, NOW) == 1
    assert watch_store.record_fast_merges(path, REPO, prs, NOW) == 0
    assert len(watch_store.heads(path)) == 2
    assert watch_store.heads(path)[0]["merged_before_seen"] == 1


def test_fast_merge_survives_listing_and_retry_but_obeys_repo_restrictions(path):
    watch_store.record_fast_merges(path, REPO, [merged()], NOW)
    head = watch_store.claim(path, NOW)
    watch_store.finish(path, head, None, RuntimeError("temporary"), NOW)
    watch_store.record_listing(path, REPO, [], NOW)
    assert watch_store.claim(path, NOW) is None
    assert watch_store.claim(path, NOW + timedelta(minutes=2))["id"] == head["id"]
    watch_store.finish(path, watch_store.heads(path)[0], None, RuntimeError("temporary"), NOW)
    watch_store.restrict_repos(path, [], NOW)
    assert watch_store.heads(path)[0]["status"] == "closed"
    assert watch_store.claim(path, NOW + timedelta(hours=1)) is None


def test_restart_and_disabled_switch_do_not_backfill(path, monkeypatch):
    records = discovery(monkeypatch)
    assert asyncio.run(watcher.poll_once(path)) == 0
    records[0]["pushed_at"] = NOW.isoformat()
    monkeypatch.setattr(watcher, "_repo_pushed_at", {})
    assert asyncio.run(watcher.poll_once(path)) == 0
    monkeypatch.setenv("ARGUS_WATCH_FAST_MERGES", "0")
    records[0]["pushed_at"] = (NOW + timedelta(seconds=1)).isoformat()
    assert asyncio.run(watcher.poll_once(path)) == 0
    assert watch_store.heads(path) == []


def test_denied_owner_is_refused_before_request(path, monkeypatch):
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "acme-corp")
    with pytest.raises((RepoDenied, github.GitHubDenied)):
        watcher._recently_closed_prs("acme-corp/project")


def test_column_migration_preserves_existing_rows_twice(path):
    watch_store.record_listing(path, REPO, [merged()], NOW)
    with watch_store.connection(path) as conn:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(pr_heads)")}
        if "merged_before_seen" in columns:
            conn.execute("ALTER TABLE pr_heads DROP COLUMN merged_before_seen")
    db.init_db(path)
    db.init_db(path)
    assert watch_store.heads(path)[0]["merged_before_seen"] == 0
    assert watch_store.heads(path)[0]["head_sha"] == SHA


def test_fast_merge_coverage_uses_merge_time_without_changing_existing_rules(path):
    prs = [merged(n, merged_at=(NOW - timedelta(minutes=age)).isoformat())
           for n, age in [(1, 2), (2, 20), (3, 4)]]
    watch_store.record_fast_merges(path, REPO, prs, NOW)
    for pr in prs[:2]:
        rid = db.create_review(path, REPO, pr["number"], "test-model", head_sha=SHA)
        with watch_store.connection(path) as conn:
            conn.execute("UPDATE reviews SET status='done', completed_at=? WHERE id=?",
                         ((NOW + timedelta(minutes=1)).isoformat(), rid))
    with watch_store.connection(path) as conn:
        result = watch_metrics.coverage(conn, NOW, NOW + timedelta(minutes=5))
        assert result["fast_merged"] == {
            "seen": 3, "done": 2, "done_within_15_minutes_of_merge": 1}
        early = watch_metrics.coverage(conn, NOW, NOW + timedelta(seconds=1))
        assert early["fast_merged"]["done"] == 0
        outside = watch_metrics.coverage(conn, NOW + timedelta(seconds=1), NOW + timedelta(hours=1))
        assert outside["fast_merged"]["seen"] == 0
        conn.execute("UPDATE pr_heads SET merged_before_seen=0")
        ordinary = watch_metrics.coverage(conn, NOW, NOW + timedelta(minutes=5))
    assert {k: v for k, v in result.items() if k != "fast_merged"} == {
        k: v for k, v in ordinary.items() if k != "fast_merged"}


def test_first_observation_is_independent_per_database(path, tmp_path, monkeypatch):
    records = discovery(monkeypatch)
    assert asyncio.run(watcher.poll_once(path)) == 0
    records[0]["pushed_at"] = NOW.isoformat()
    other = str(tmp_path / "other.db")
    db.init_db(other)
    assert asyncio.run(watcher.poll_once(other)) == 0
    monkeypatch.setattr(watcher, "_recently_closed_prs", lambda repo: [merged()])
    assert asyncio.run(watcher.poll_once(path)) == 1
    assert watch_store.heads(other) == []


def test_concurrent_closed_listings_queue_each_head_once(path):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(2) as pool:
        counts = list(pool.map(
            lambda _: watch_store.record_fast_merges(path, REPO, [merged()], NOW), range(2)))
    assert sum(counts) == 1
    assert len(watch_store.heads(path)) == 1


@pytest.mark.parametrize("failure", ["listing", "recording"])
def test_failed_closed_listing_is_retried(path, monkeypatch, failure):
    records = discovery(monkeypatch)
    asyncio.run(watcher.poll_once(path))
    records[0]["pushed_at"] = NOW.isoformat()
    calls = []
    record = watch_store.record_fast_merges

    def closed(repo):
        calls.append(repo)
        if failure == "listing" and len(calls) == 1:
            raise httpx.ConnectError("temporary")
        return [merged()]

    def save(*args):
        if failure == "recording" and len(calls) == 1:
            raise RuntimeError("temporary storage failure")
        return record(*args)

    monkeypatch.setattr(watcher, "_recently_closed_prs", closed)
    monkeypatch.setattr(watch_store, "record_fast_merges", save)
    assert asyncio.run(watcher.poll_once(path)) == 0
    assert asyncio.run(watcher.poll_once(path)) == 1
    assert asyncio.run(watcher.poll_once(path)) == 0
    assert calls == [REPO, REPO]
    assert watch_store.heads(path)[0]["status"] == "queued"


def test_quota_pause_keeps_unread_repo_advance(path, monkeypatch):
    records = discovery(monkeypatch)
    other = "example/b"
    records.append({**records[0], "full_name": other})
    asyncio.run(watcher.poll_once(path))
    for record in records:
        record["pushed_at"] = NOW.isoformat()
    calls = []

    def closed(repo):
        calls.append(repo)
        return [merged()]

    def opened(repo):
        github._pause_until = 9999999999
        return []

    monkeypatch.setattr(watcher, "_recently_closed_prs", closed)
    monkeypatch.setattr(watcher, "_open_prs", opened)
    asyncio.run(watcher.poll_once(path))
    assert other not in calls
    github._pause_until = 0
    monkeypatch.setattr(watcher, "_open_prs", lambda repo: [])
    asyncio.run(watcher.poll_once(path))
    asyncio.run(watcher.poll_once(path))
    assert calls.count(other) == 1
    assert {row["repo"] for row in watch_store.heads(path)} == {REPO, other}


@pytest.mark.parametrize("seconds", [2, 300])
def test_future_merge_within_clock_skew_is_queued(path, seconds):
    pr = merged(merged_at=(NOW + timedelta(seconds=seconds)).isoformat())
    assert watch_store.record_fast_merges(path, REPO, [pr], NOW) == 1
    assert watch_store.heads(path)[0]["eligible_pr_updated_at"] == pr["merged_at"]


def test_merge_push_activating_dormant_repo_is_listed(path, monkeypatch):
    records = discovery(monkeypatch)
    records[0]["pushed_at"] = (NOW - timedelta(days=100)).isoformat()
    asyncio.run(watcher.poll_once(path))
    assert watcher._watched_repos(path) == []
    records[0]["pushed_at"] = NOW.isoformat()
    monkeypatch.setattr(watcher, "_recently_closed_prs", lambda repo: [merged()])
    assert asyncio.run(watcher.poll_once(path)) == 1
    assert watch_store.heads(path)[0]["repo"] == REPO


@pytest.mark.parametrize("pushed_at", [None, "not-a-date", 42])
def test_bad_activity_does_not_stop_open_listings(path, monkeypatch, pushed_at):
    records = discovery(monkeypatch)
    records[0]["pushed_at"] = pushed_at
    db.save_config(path, repos=[{"name": REPO, "watched": True}], model="reviewer",
                   bug_cap=5, min_confidence=0.5, watch_mode="auto", owned_repos=records)
    monkeypatch.setattr(watcher, "_open_prs", lambda repo: [merged()])
    assert asyncio.run(watcher.poll_once(path)) == 1
    assert watch_store.heads(path)[0]["is_open"] == 1


def test_detection_failure_warns_once_and_lists_open_prs(path, monkeypatch, caplog):
    discovery(monkeypatch)

    def fail(*args):
        raise RuntimeError("detection failed")

    monkeypatch.setattr(watcher, "_advanced_repos", fail)
    monkeypatch.setattr(watcher, "_open_prs", lambda repo: [merged()])
    with caplog.at_level(logging.WARNING, logger="argus.watcher"):
        assert asyncio.run(watcher.poll_once(path)) == 1
    warnings = {id(r): r for r in caplog.records if r.levelno >= logging.WARNING}
    assert len(warnings) == 1
    assert "fast merge detection failed" in next(iter(warnings.values())).message


def test_disabled_switch_skips_detection(path, monkeypatch):
    discovery(monkeypatch)
    monkeypatch.setenv("ARGUS_WATCH_FAST_MERGES", "0")
    monkeypatch.setattr(watcher, "_advanced_repos", lambda *a: pytest.fail("detection disabled"))
    monkeypatch.setattr(watcher, "_open_prs", lambda repo: [merged()])
    assert asyncio.run(watcher.poll_once(path)) == 1


def test_failed_first_discovery_does_not_seed_from_disk(path, monkeypatch):
    records = discovery(monkeypatch)
    db.save_config(path, repos=[], model="reviewer", bug_cap=5, min_confidence=0.5,
                   watch_mode="auto", owned_repos=records)

    def fail():
        raise httpx.ConnectError("first discovery failed")

    monkeypatch.setattr(github, "owned_repo_records", fail)
    monkeypatch.setattr(watcher, "_recently_closed_prs", lambda repo: pytest.fail("no backfill"))
    assert asyncio.run(watcher.poll_once(path)) == 0
    records[0]["pushed_at"] = NOW.isoformat()
    monkeypatch.setattr(github, "owned_repo_records", lambda: records)
    assert asyncio.run(watcher.poll_once(path)) == 0
    records[0]["pushed_at"] = (NOW + timedelta(seconds=1)).isoformat()
    monkeypatch.setattr(watcher, "_recently_closed_prs", lambda repo: [merged()])
    assert asyncio.run(watcher.poll_once(path)) == 1


def test_merged_head_closed_before_claim_is_requeued_once(path):
    watch_store.record_listing(path, REPO, [merged()], NOW - timedelta(minutes=1))
    watch_store.record_listing(path, REPO, [], NOW)
    assert watch_store.heads(path)[0]["status"] == "closed"
    assert watch_store.record_fast_merges(path, REPO, [merged()], NOW) == 1
    row = watch_store.heads(path)[0]
    assert row["status"] == "queued"
    assert row["merged_before_seen"] == 1
    assert row["eligible_at"] == NOW.isoformat()
    assert row["eligible_pr_updated_at"] == merged()["merged_at"]
    watch_store.record_listing(path, REPO, [], NOW)
    assert watch_store.record_fast_merges(path, REPO, [merged()], NOW) == 0
    assert watch_store.heads(path)[0] == row
    assert watch_store.claim(path, NOW)["id"] == row["id"]


@pytest.mark.parametrize("status", ["done", "error", "parked", "skipped", "superseded"])
def test_other_merged_head_statuses_are_untouched(path, status):
    watch_store.record_listing(path, REPO, [merged()], NOW)
    with watch_store.connection(path) as conn:
        conn.execute("UPDATE pr_heads SET status=?", (status,))
    before = watch_store.heads(path)[0]
    assert watch_store.record_fast_merges(path, REPO, [merged()], NOW) == 0
    assert watch_store.heads(path)[0] == before


@pytest.mark.parametrize("linked", [False, True])
def test_closed_head_with_done_review_is_untouched(path, linked):
    watch_store.record_listing(path, REPO, [merged()], NOW)
    rid = db.create_review(path, REPO, 1, "reviewer", head_sha=SHA)
    db.save_result(path, rid, ReviewResult(raw_count=0))
    with watch_store.connection(path) as conn:
        conn.execute("UPDATE pr_heads SET status='closed', review_id=?", (rid if linked else None,))
    before = watch_store.heads(path)[0]
    assert watch_store.record_fast_merges(path, REPO, [merged()], NOW) == 0
    assert watch_store.heads(path)[0] == before


@pytest.mark.parametrize("retry", [False, True])
def test_queued_merge_survives_failed_open_listing_and_is_reviewed(path, monkeypatch, retry):
    records = discovery(monkeypatch)
    monkeypatch.setattr(watcher, "_open_prs", lambda repo: [merged()])
    assert asyncio.run(watcher.poll_once(path)) == 1
    if retry:
        head = watch_store.claim(path, NOW)
        rid = db.create_review(path, REPO, 1, "reviewer", head_sha=SHA)
        db.set_error(path, rid, "temporary", "RuntimeError")
        watch_store.finish(path, head, rid, RuntimeError("temporary"), NOW)
    before = watch_store.heads(path)[0]

    def fail(repo):
        raise httpx.ConnectError("open listing failed")

    records[0]["pushed_at"] = NOW.isoformat()
    monkeypatch.setattr(watcher, "_open_prs", fail)
    monkeypatch.setattr(watcher, "_recently_closed_prs", lambda repo: [merged()])
    assert asyncio.run(watcher.poll_once(path)) == 0
    assert watch_store.heads(path)[0] == {**before, "merged_before_seen": 1,
                                        "merged_at": merged()["merged_at"]}
    monkeypatch.setattr(watcher, "_open_prs", lambda repo: [])
    assert asyncio.run(watcher.poll_once(path)) == 0
    row = watch_store.heads(path)[0]
    assert row == {**before, "merged_before_seen": 1, "is_open": 0,
                   "merged_at": merged()["merged_at"]}
    if retry:
        assert watch_store.claim(path, NOW) is None
    monkeypatch.setattr(watch_store, "utcnow", lambda: NOW + timedelta(minutes=2))

    def review(repo, pr, model, cap, **kwargs):
        assert (repo, pr, kwargs["expected_head_sha"]) == (REPO, 1, SHA)
        return ReviewResult(raw_count=0)

    monkeypatch.setattr(main, "run_review", review)
    monkeypatch.setattr(main, "_publish_completed_review", lambda *a: None)
    assert watcher.process_one(path)
    row = watch_store.heads(path)[0]
    assert row["status"] == "done"
    assert row["attempts"] == int(retry)
    assert db.get_review(path, row["review_id"])["status"] == "done"


@pytest.mark.parametrize("open_first", [False, True])
def test_running_merge_preserves_claim_and_finishes(path, open_first):
    watch_store.record_listing(path, REPO, [merged()], NOW)
    head = watch_store.claim(path, NOW)
    rid = db.create_review(path, REPO, 1, "reviewer", head_sha=SHA)
    with watch_store.connection(path) as conn:
        conn.execute("UPDATE pr_heads SET review_id=? WHERE id=?", (rid, head["id"]))
    if open_first:
        watch_store.record_listing(path, REPO, [], NOW)
    before = watch_store.heads(path)[0]
    assert watch_store.record_fast_merges(path, REPO, [merged()], NOW) == 0
    assert watch_store.heads(path)[0] == {**before, "merged_before_seen": 1,
                                        "merged_at": merged()["merged_at"]}
    assert watch_store.record_fast_merges(path, REPO, [merged()], NOW) == 0
    if not open_first:
        watch_store.record_listing(path, REPO, [], NOW)
    db.save_result(path, rid, ReviewResult(raw_count=0))
    watch_store.finish(path, head, rid, now=NOW + timedelta(minutes=1))
    row = watch_store.heads(path)[0]
    assert row["status"] == "done"
    assert row["review_id"] == rid
    assert row["attempts"] == 0


@pytest.mark.parametrize("other_done", [None, "repo", "pr", "head_sha"])
def test_closed_errored_head_requeues_once_with_attempts_kept(path, monkeypatch, other_done):
    monkeypatch.setenv("ARGUS_WATCH_MAX_ATTEMPTS", "2")
    watch_store.record_listing(path, REPO, [merged()], NOW - timedelta(minutes=3))
    head = watch_store.claim(path, NOW - timedelta(minutes=3))
    rid = db.create_review(path, REPO, 1, "reviewer", head_sha=SHA)
    db.set_error(path, rid, "temporary", "RuntimeError")
    watch_store.finish(path, head, rid, RuntimeError("temporary"), NOW - timedelta(minutes=3))
    watch_store.record_listing(path, REPO, [], NOW - timedelta(minutes=2))
    if other_done:
        target = {"repo": REPO, "pr": 1, "head_sha": SHA}
        target[other_done] = {"repo": "example/b", "pr": 2, "head_sha": "b" * 40}[other_done]
        done_id = db.create_review(path, model="reviewer", **target)
        db.save_result(path, done_id, ReviewResult(raw_count=0))
    before = watch_store.heads(path)[0]
    assert before["status"] == "closed"
    assert before["review_id"] == rid
    assert watch_store.record_fast_merges(path, REPO, [merged()], NOW) == 1
    row = watch_store.heads(path)[0]
    assert row == {**before, "status": "queued", "merged_before_seen": 1, "baseline": 0,
                   "eligible_at": NOW.isoformat(), "eligible_pr_updated_at": merged()["merged_at"],
                   "ended_at": None, "merged_at": merged()["merged_at"]}
    assert row["attempts"] == 1
    assert watch_store.record_fast_merges(path, REPO, [merged()], NOW + timedelta(seconds=1)) == 0
    assert watch_store.heads(path)[0] == row
    claim = watch_store.claim(path, NOW)
    watch_store.finish(path, claim, rid, RuntimeError("temporary"), NOW)
    assert watch_store.heads(path)[0]["status"] == "parked"


def test_closed_baseline_merge_enters_fair_and_strict_coverage(path):
    watch_store.record_listing(path, REPO, [merged(updated_at=(NOW - timedelta(hours=2)).isoformat())],
                               NOW - timedelta(minutes=3))
    assert watch_store.heads(path)[0]["baseline"] == 1
    watch_store.record_listing(path, REPO, [], NOW - timedelta(minutes=2))
    assert watch_store.record_fast_merges(path, REPO, [merged()], NOW) == 1
    row = watch_store.heads(path)[0]
    assert row["baseline"] == 0
    head = watch_store.claim(path, NOW)
    rid = db.create_review(path, REPO, 1, "reviewer", head_sha=SHA)
    db.save_result(path, rid, ReviewResult(raw_count=0))
    completed = NOW + timedelta(minutes=1)
    with watch_store.connection(path) as conn:
        conn.execute("UPDATE reviews SET completed_at=? WHERE id=?", (completed.isoformat(), rid))
    watch_store.finish(path, head, rid, now=completed)
    with watch_store.connection(path) as conn:
        result = watch_metrics.coverage(conn, NOW, NOW + timedelta(minutes=5))
    assert result["baseline_heads"] == 0
    assert result["eligible_heads"] == result["done_heads"] == 1
    assert result["strict"]["eligible_heads"] == result["strict"]["done_heads"] == 1
    assert result["push_to_review_seconds"]["median"] == 180
    assert result["strict"]["share_within_15_minutes"] == 1
    assert result["fast_merged"] == {
        "seen": 1, "done": 1, "done_within_15_minutes_of_merge": 1}


def test_watch_reentry_restores_only_recent_unfinished_merges_once(path, monkeypatch, caplog):
    discovery(monkeypatch)
    watched = [REPO]
    monkeypatch.setattr(watcher, "_watched_repos", lambda path: list(watched))
    monkeypatch.setattr(watcher, "_recently_closed_prs", lambda repo: pytest.fail("no new push"))
    watch_store.record_fast_merges(path, REPO, [merged(1), merged(2), merged(3)],
                                   NOW - timedelta(minutes=1))
    watch_store.record_listing(path, REPO, [merged(4)], NOW)
    rid = db.create_review(path, REPO, 2, "reviewer", head_sha=SHA)
    db.save_result(path, rid, ReviewResult(raw_count=0))
    with watch_store.connection(path) as conn:
        conn.execute("UPDATE pr_heads SET attempts=2 WHERE pr=1")
        conn.execute("UPDATE pr_heads SET merged_at=? WHERE pr=3",
                     ((NOW - timedelta(minutes=61)).isoformat(),))
    assert asyncio.run(watcher.poll_once(path)) == 0
    watched.clear()
    assert asyncio.run(watcher.poll_once(path)) == 0
    before = watch_store.heads(path)
    assert all(row["status"] == "closed" for row in before)
    watched.append(REPO)
    with caplog.at_level(logging.INFO, logger="argus.watcher"):
        assert asyncio.run(watcher.poll_once(path)) == 1
        after = watch_store.heads(path)
        assert asyncio.run(watcher.poll_once(path)) == 0
    assert after[0] == {**before[0], "status": "queued", "eligible_at": NOW.isoformat(),
                        "ended_at": None}
    assert after[0]["attempts"] == 2
    assert after[1:] == before[1:]
    assert watch_store.heads(path) == after
    records = {id(r): r for r in caplog.records if "requeued fast merge" in r.getMessage()}
    assert len(records) == 1
    record = next(iter(records.values()))
    assert record.levelno == logging.INFO
    assert REPO + "#1" in record.getMessage()
    assert SHA in record.getMessage()
    assert watch_store.claim(path, NOW)["id"] == after[0]["id"]


@pytest.mark.parametrize(("switch", "queued", "status"), [
    ("0", 0, "closed"),
    ("1", 1, "queued"),
])
def test_reentry_requeue_obeys_fast_merges_switch_and_updates_baseline(
    path, monkeypatch, switch, queued, status
):
    discovery(monkeypatch)
    watched = [REPO]
    monkeypatch.setattr(watcher, "_watched_repos", lambda path: list(watched))
    assert asyncio.run(watcher.poll_once(path)) == 0
    watch_store.record_fast_merges(path, REPO, [merged()], NOW)
    watched.clear()
    assert asyncio.run(watcher.poll_once(path)) == 0

    monkeypatch.setenv("ARGUS_WATCH_FAST_MERGES", switch)
    watched.append(REPO)
    assert asyncio.run(watcher.poll_once(path)) == queued
    assert watch_store.heads(path)[0]["status"] == status

    monkeypatch.setenv("ARGUS_WATCH_FAST_MERGES", "1")
    assert asyncio.run(watcher.poll_once(path)) == 0
    assert watch_store.heads(path)[0]["status"] == status


def test_first_tick_does_not_restore_closed_merges(path, monkeypatch):
    discovery(monkeypatch)
    watch_store.record_fast_merges(path, REPO, [merged()], NOW)
    watch_store.restrict_repos(path, [], NOW)
    before = watch_store.heads(path)
    assert asyncio.run(watcher.poll_once(path)) == 0
    assert asyncio.run(watcher.poll_once(path)) == 0
    assert watch_store.heads(path) == before


@pytest.mark.parametrize("age,expected", [(60, 1), (60 + 1 / 60, 0)])
@pytest.mark.parametrize("other_done", ["repo", "pr", "head_sha"])
def test_reentry_merge_window_and_done_review_identity(path, age, expected, other_done):
    watch_store.record_fast_merges(path, REPO, [merged()], NOW)
    watch_store.restrict_repos(path, [], NOW)
    with watch_store.connection(path) as conn:
        conn.execute("UPDATE pr_heads SET merged_at=?",
                     ((NOW - timedelta(minutes=age)).isoformat(),))
    target = {"repo": REPO, "pr": 1, "head_sha": SHA}
    target[other_done] = {"repo": "example/b", "pr": 2, "head_sha": "b" * 40}[other_done]
    rid = db.create_review(path, model="reviewer", **target)
    db.save_result(path, rid, ReviewResult(raw_count=0))
    assert watch_store.requeue_fast_merges(path, ["example/b"], NOW) == 0
    assert watch_store.requeue_fast_merges(path, [REPO], NOW) == expected
    assert watch_store.requeue_fast_merges(path, [REPO], NOW) == 0


def test_reentry_requeues_future_merge_within_clock_skew(path):
    pr = merged(merged_at=(NOW + timedelta(minutes=2)).isoformat())
    assert watch_store.record_fast_merges(path, REPO, [pr], NOW) == 1
    watch_store.restrict_repos(path, [], NOW)
    assert watch_store.heads(path)[0]["status"] == "closed"

    assert watch_store.requeue_fast_merges(path, [REPO], NOW) == 1
    assert watch_store.heads(path)[0]["status"] == "queued"


def test_watch_reentry_is_independent_per_database(path, tmp_path, monkeypatch):
    discovery(monkeypatch)
    watched = [REPO]
    monkeypatch.setattr(watcher, "_watched_repos", lambda path: list(watched))
    asyncio.run(watcher.poll_once(path))
    watched.clear()
    asyncio.run(watcher.poll_once(path))
    other = str(tmp_path / "other.db")
    db.init_db(other)
    watch_store.record_fast_merges(other, REPO, [merged()], NOW)
    watch_store.restrict_repos(other, [], NOW)
    before = watch_store.heads(other)
    watched.append(REPO)
    assert asyncio.run(watcher.poll_once(other)) == 0
    assert watch_store.heads(other) == before


def test_failed_requeue_keeps_polling_and_retries_both_reentries(path, monkeypatch, caplog):
    discovery(monkeypatch)
    other = "example/b"
    watched = [REPO]
    monkeypatch.setattr(watcher, "_watched_repos", lambda path: list(watched))
    asyncio.run(watcher.poll_once(path))
    watch_store.record_fast_merges(path, REPO, [merged()], NOW)
    watch_store.record_fast_merges(path, other, [merged()], NOW)
    watch_store.restrict_repos(path, [REPO], NOW)
    real_requeue = watch_store.requeue_fast_merges
    calls = []

    def requeue(path, repos):
        calls.append(set(repos))
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_requeue(path, repos, NOW)

    monkeypatch.setattr(watch_store, "requeue_fast_merges", requeue)
    monkeypatch.setattr(watcher, "_open_prs", lambda repo: [merged(2)])
    watched[:] = [other]
    with caplog.at_level(logging.WARNING, logger="argus.watcher"):
        assert asyncio.run(watcher.poll_once(path)) == 1
    assert any(row["repo"] == other and row["pr"] == 2 and row["is_open"]
               for row in watch_store.heads(path))
    warnings = {id(r): r for r in caplog.records if r.levelno >= logging.WARNING}
    assert len(warnings) == 1
    assert next(iter(warnings.values())).levelno == logging.WARNING
    assert "requeue" in next(iter(warnings.values())).getMessage()

    monkeypatch.setattr(watcher, "_open_prs", lambda repo: [])
    watched[:] = [REPO, other]
    assert asyncio.run(watcher.poll_once(path)) == 2
    assert calls == [{other}, {REPO, other}]
    assert all(row["status"] == "queued" for row in watch_store.heads(path) if row["pr"] == 1)
    assert asyncio.run(watcher.poll_once(path)) == 0
    assert len(calls) == 2


@pytest.mark.parametrize("running", [False, True])
def test_reentry_window_uses_merge_time_for_head_first_seen_open(path, monkeypatch, running):
    discovery(monkeypatch)
    watched = [REPO]
    monkeypatch.setattr(watcher, "_watched_repos", lambda path: list(watched))
    asyncio.run(watcher.poll_once(path))
    updated = (NOW - timedelta(minutes=70)).isoformat()
    pr = merged(updated_at=updated, merged_at=(NOW - timedelta(minutes=20)).isoformat())
    watch_store.record_listing(path, REPO, [pr], NOW - timedelta(minutes=60))
    head = watch_store.claim(path, NOW) if running else None
    assert watch_store.record_fast_merges(path, REPO, [pr], NOW) == 0
    row = watch_store.heads(path)[0]
    assert row["eligible_pr_updated_at"] == updated
    assert row["merged_at"] == pr["merged_at"]
    watched.clear()
    asyncio.run(watcher.poll_once(path))
    if running:
        watch_store.close_claim(path, head, NOW)
    assert watch_store.heads(path)[0]["status"] == "closed"
    watched.append(REPO)
    assert asyncio.run(watcher.poll_once(path)) == 1
    row = watch_store.heads(path)[0]
    assert row["status"] == "queued"
    assert row["eligible_pr_updated_at"] == updated


def test_merge_time_migration_backfills_legacy_fast_merges_once(path):
    watch_store.record_listing(path, REPO, [merged(2)], NOW)
    watch_store.record_fast_merges(path, REPO, [merged()], NOW)
    watch_store.restrict_repos(path, [], NOW)
    with watch_store.connection(path) as conn:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(pr_heads)")}
        if "merged_at" in columns:
            conn.execute("ALTER TABLE pr_heads DROP COLUMN merged_at")
    db.init_db(path)
    after_first_init = watch_store.heads(path)
    db.init_db(path)
    assert watch_store.heads(path) == after_first_init
    fast_merge = next(row for row in after_first_init if row["merged_before_seen"])
    ordinary = next(row for row in after_first_init if not row["merged_before_seen"])
    assert fast_merge["merged_at"] == fast_merge["eligible_pr_updated_at"] == merged()["merged_at"]
    assert ordinary["merged_at"] is None
    assert watch_store.requeue_fast_merges(path, [REPO], NOW) == 1
    with watch_store.connection(path) as conn:
        columns = {r["name"]: r["type"] for r in conn.execute("PRAGMA table_info(pr_heads)")}
    assert columns["merged_at"] == "TEXT"


def test_worker_unwatch_between_ticks_does_not_hide_reentry(path, monkeypatch):
    discovery(monkeypatch)
    watched = [REPO]
    monkeypatch.setattr(watcher, "_watched_repos", lambda path: list(watched))
    monkeypatch.setattr(watcher, "trigger_review", lambda *a, **k: pytest.fail("unwatched repo"))
    asyncio.run(watcher.poll_once(path))
    watch_store.record_fast_merges(path, REPO, [merged()], NOW)
    watched.clear()
    assert watcher.process_one(path)
    assert watch_store.heads(path)[0]["status"] == "closed"
    watched.append(REPO)
    assert asyncio.run(watcher.poll_once(path)) == 1
    assert watch_store.heads(path)[0]["status"] == "queued"
    assert asyncio.run(watcher.poll_once(path)) == 0
