"""Queue discovery and worker contracts with local fakes."""

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from argus import github
from argus.api import db, main, watch_config, watch_store, watcher
from argus.review import ReviewResult


@pytest.fixture
def path(tmp_path, monkeypatch):
    path = str(tmp_path / "argus.db")
    monkeypatch.setenv("ARGUS_DB_PATH", path)
    monkeypatch.setenv("ARGUS_WATCH_OWNERS", "example")
    monkeypatch.setattr(github, "_quota", {})
    monkeypatch.setattr(github, "_pause_until", 0)
    monkeypatch.setattr(watcher, "_ticks", {})
    monkeypatch.setattr(watcher, "_repo_pushed_at", {})
    monkeypatch.setattr(watcher, "_recently_closed_prs", lambda repo: [])
    monkeypatch.setattr(github, "owned_repo_records", lambda: [])
    db.init_db(path)
    return path


def repo(name="example/a", age=0, **extra):
    return {
        "full_name": name,
        "pushed_at": (datetime.now(timezone.utc) - timedelta(days=age)).isoformat(),
        **extra,
    }


def pr(number=1, sha="abc", **extra):
    return {
        "author_association": "OWNER",
        "number": number,
        "head": {"sha": sha},
        "user": {"login": "human", "type": "User"},
        **extra,
    }


def config(path, repos=None, owned=None, **kwargs):
    db.save_config(
        path,
        repos=repos or [],
        model="test-model",
        bug_cap=5,
        min_confidence=0.5,
        owned_repos=owned or [],
        **kwargs,
    )


def test_activity_overrides_scope_and_denial(path):
    records = [
        repo(),
        repo("example/old", 91),
        repo("example/forced", 100),
        repo("example/off"),
        repo("example/frozen", archived=True),
        repo("other/a"),
        repo("example/denied"),
    ]
    config(
        path,
        [{"name": r["full_name"], "watched": None} for r in records]
        + [
            {"name": "example/forced", "watched": True},
            {"name": "example/off", "watched": False},
            {"name": "example/denied", "watched": True},
        ],
        records,
        deny=["EXAMPLE/DENIED"],
    )
    assert watcher._watched_repos(path) == ["example/a", "example/forced"]


def test_migration_once(path, caplog):
    config(path, [{"name": "example/a", "watched": False}])
    with watch_store.connection(path) as conn:
        conn.execute("UPDATE config SET watch_mode=NULL")
    db.init_db(path)
    assert db.get_config(path)["repos"][0]["watched"] is None
    config(path, [{"name": "example/a", "watched": False}])
    db.init_db(path)
    assert db.get_config(path)["repos"][0]["watched"] is False


def test_console_round_trip_keeps_auto(path):
    config(path, [{"name": "example/a", "watched": None}], [repo()])
    payload = main.get_config()
    assert payload["repos"][0]["watched"] is True
    assert payload["repos"][0]["effective_watched"] is True
    assert payload["repos"][0]["watched_source"] == "auto"
    main.put_config(main.ConfigUpdate(**payload, model=payload["models"][0]["model"]))
    assert db.get_config(path)["repos"][0]["watched"] is None
    payload["repos"][0]["watched"] = False
    main.put_config(main.ConfigUpdate(**payload, model=payload["models"][0]["model"]))
    assert db.get_config(path)["repos"][0]["watched"] is False


def test_canonical_listing_removes_stale_catalogue(path, monkeypatch):
    config(
        path,
        [
            {"name": name, "watched": True}
            for name in ["example/app", "example/service"]
        ],
    )
    monkeypatch.setattr(
        github, "owned_repo_records", lambda: [repo("example/service"), repo("other/x")]
    )
    watcher.discover_repos(path)
    assert watcher._watched_repos(path) == ["example/service"]
    assert not any(r["name"] == "other/x" for r in db.get_config(path)["repos"])


def test_manual_discovery_stays_opt_in(path, monkeypatch):
    config(path, watch_mode="manual")
    monkeypatch.setattr(github, "owned_repo_records", lambda: [repo()])
    watcher.discover_repos(path)
    assert db.get_config(path)["repos"] == []
    monkeypatch.setenv("ARGUS_AUTODISCOVER", "1")
    watcher.discover_repos(path)
    assert db.get_config(path)["repos"] == [{"name": "example/a", "watched": False}]
    assert watcher._watched_repos(path) == []


def test_tick_only_queues_and_refreshes_every_ten_ticks(path, monkeypatch):
    monkeypatch.setenv("ARGUS_WATCH_DISCOVERY_TICKS", "10")
    calls = []
    monkeypatch.setattr(github, "owned_repo_records", lambda: calls.append(1) or [repo()])
    monkeypatch.setattr(watcher, "_open_prs", lambda _: [pr()])
    monkeypatch.setattr(watcher, "trigger_review", lambda *a, **k: pytest.fail("tick ran a review"))
    for _ in range(11):
        asyncio.run(watcher.poll_once(path))
    assert len(calls) == 2
    assert watch_store.queue_sizes(path)["queued"] == 1
    assert db.list_reviews(path) == []


@pytest.mark.parametrize(
    "user",
    [
        {"type": "Bot", "login": "a"},
        {"login": "a[bot]"},
        {"login": "dependabot"},
        {"login": "renovate"},
        {"login": "dependabot-preview"},
    ],
)
def test_bots_reconsidered_on_same_sha(path, user):
    watch_store.record_listing(path, "example/a", [pr(user=user)])
    assert watch_store.heads(path)[0]["skip_reason"] == "bot"
    watch_store.record_listing(path, "example/a", [pr()])
    assert watch_store.heads(path)[0]["status"] == "queued"


def test_include_flags(path, monkeypatch):
    monkeypatch.setenv("ARGUS_WATCH_INCLUDE_DRAFTS", "yes")
    monkeypatch.setenv("ARGUS_WATCH_INCLUDE_BOTS", "true")
    watch_store.record_listing(path, "example/a", [pr(draft=True, user={"type": "Bot"})])
    assert watch_store.heads(path)[0]["status"] == "queued"


def test_old_errors_get_fresh_budget_but_old_done_is_deduped(path):
    for _ in range(7):
        rid = db.create_review(path, "example/a", 1, "test-model", head_sha="abc")
        db.set_error(path, rid, "overloaded", "RuntimeError")
    done = db.create_review(path, "example/a", 2, "test-model", head_sha="done")
    db.set_status(path, done, "done")
    watch_store.record_listing(path, "example/a", [pr(), pr(2, "done")])
    rows = watch_store.heads(path)
    assert (rows[0]["status"], rows[0]["attempts"]) == ("queued", 0)
    assert rows[1]["status"] == "done"


def test_two_real_workers_concurrent_without_duplicate_claims(path, monkeypatch):
    config(path, owned=[repo()])
    watch_store.record_listing(path, "example/a", [pr(), pr(2, "def")])
    barrier = threading.Barrier(2)
    calls = []

    def review(*args, **kwargs):
        calls.append(args[1])
        barrier.wait(timeout=3)
        time.sleep(0.03)
        return ReviewResult(raw_count=0)

    monkeypatch.setattr(main, "run_review", review)
    with ThreadPoolExecutor(2) as pool:
        assert all(pool.map(lambda _: watcher.process_one(path), range(2)))
    assert sorted(calls) == [1, 2]
    assert watch_store.queue_sizes(path)["done"] == 2
    assert not watcher.process_one(path)
    assert all(r["review_id"] for r in watch_store.heads(path))


def test_reclaim_and_old_completion_is_fenced(path):
    now = datetime.now(timezone.utc)
    watch_store.record_listing(path, "example/a", [pr()], now)
    head = watch_store.claim(path, now - timedelta(hours=1))
    assert watch_store.reclaim(path, now=now) == 1
    row = watch_store.heads(path)[0]
    assert (row["status"], row["attempts"], row["last_error_class"]) == ("queued", 1, "interrupted")
    watch_store.finish(path, head, None, now=now)
    assert watch_store.heads(path)[0]["status"] == "queued"
    watch_store.claim(path, now + timedelta(minutes=2))
    assert watch_store.reclaim(path, startup=True, now=now + timedelta(minutes=2)) == 1
    assert watch_store.heads(path)[0]["attempts"] == 2


def test_quota_pause_then_resume(path, monkeypatch):
    now = time.time()
    monkeypatch.setattr(
        github, "_quota", {"core": {"remaining": 20, "limit": 5000, "reset": now + 100}}
    )
    called = []
    monkeypatch.setattr(github, "owned_repo_records", lambda: called.append(1) or [repo()])
    monkeypatch.setattr(watcher, "_open_prs", lambda _: [])
    asyncio.run(watcher.poll_once(path))
    assert not called
    assert not watcher.process_one(path)
    github._quota["core"]["reset"] = now - 1
    asyncio.run(watcher.poll_once(path))
    assert called == [1]


def test_tick_survives_failure_and_recovers(path, monkeypatch):
    monkeypatch.setattr(github, "owned_repo_records", lambda: [repo()])
    monkeypatch.setattr(
        watcher, "_watched_repos", lambda _: (_ for _ in ()).throw(RuntimeError("broken"))
    )
    assert asyncio.run(watcher.poll_once(path)) == 0
    monkeypatch.setattr(watcher, "_watched_repos", lambda _: ["example/a"])
    monkeypatch.setattr(watcher, "_open_prs", lambda _: [pr()])
    assert asyncio.run(watcher.poll_once(path)) == 1


def test_repo_500_does_not_close_heads_or_block_next_repo(path, monkeypatch):
    watch_store.record_listing(path, "example/a", [pr()])
    monkeypatch.setattr(github, "owned_repo_records", lambda: [repo(), repo("example/b")])

    def listing(name):
        if name.endswith("/a"):
            raise RuntimeError("500")
        return [pr()]

    monkeypatch.setattr(watcher, "_open_prs", listing)
    assert asyncio.run(watcher.poll_once(path)) == 1
    assert watch_store.queue_sizes(path)["queued"] == 2


def test_park_warning_only_at_transition(path, caplog, monkeypatch):
    logger = logging.getLogger("argus")
    monkeypatch.setattr(logger, "propagate", False)
    logger.addHandler(caplog.handler)
    try:
        now = datetime.now(timezone.utc)
        watch_store.record_listing(path, "example/a", [pr()], now)
        for minutes in (0, 2, 10, 30):
            now += timedelta(minutes=minutes)
            head = watch_store.claim(path, now)
            watch_store.finish(path, head, None, RuntimeError("overloaded"), now)
        for _ in range(3):
            watch_store.record_listing(path, "example/a", [pr()], now)
            assert watch_store.claim(path, now) is None
        assert sum("parking" in r.message for r in caplog.records) == 1
    finally:
        logger.removeHandler(caplog.handler)


def test_defaults_and_bounds(monkeypatch):
    monkeypatch.delenv("ARGUS_WATCH_ENABLED", raising=False)
    assert not watcher.is_enabled()
    monkeypatch.setenv("ARGUS_WATCH_ENABLED", "TRUE")
    assert watcher.is_enabled()
    monkeypatch.setenv("ARGUS_WATCH_INTERVAL", "junk")
    assert watcher.poll_interval() == 60
    monkeypatch.setenv("ARGUS_WATCH_INTERVAL", "1")
    assert watcher.poll_interval() == 30
    monkeypatch.setenv("ARGUS_REVIEW_WORKERS", "20")
    assert watcher.worker_count() == 8


def test_stored_auto_state_is_exposed(path):
    config(path, [{"name": "example/a", "watched": None}], [repo()])
    item = watch_config.public_repos(db.get_config(path))[0]
    assert item["stored_watched"] is None


def test_run_forever_starts_dedicated_workers_and_survives_tick_failure(path, monkeypatch):
    pools = []
    started = []
    original = ThreadPoolExecutor

    def pool(**kwargs):
        pools.append(kwargs)
        return original(**kwargs)

    async def run():
        second = asyncio.Event()
        calls = 0

        async def tick(_):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("tick failed")
            second.set()

        async def worker(_, executor):
            started.append(executor)
            await asyncio.Event().wait()

        monkeypatch.setattr(watcher, "ThreadPoolExecutor", pool)
        monkeypatch.setattr(watcher, "poll_once", tick)
        monkeypatch.setattr(watcher, "_worker", worker)
        monkeypatch.setattr(watcher, "poll_interval", lambda: 0.01)
        task = asyncio.create_task(watcher.run_forever(path))
        await asyncio.wait_for(second.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert len(started) == 2
    assert started[0] is started[1]
    assert pools == [{"max_workers": 2, "thread_name_prefix": "argus-review"}]


def test_rate_limited_review_preserves_budget_even_with_fallback(path, monkeypatch):
    config(path, owned=[repo()])
    watch_store.record_listing(path, "example/a", [pr()])
    reset = time.time() + 120
    monkeypatch.setenv("ARGUS_FALLBACK_MODEL", "fallback")
    calls = []

    def limited(*args, **kwargs):
        calls.append(args)
        raise github.GitHubRateLimited(reset)

    monkeypatch.setattr(main, "run_review", limited)
    assert watcher.process_one(path)
    row = watch_store.heads(path)[0]
    assert row["attempts"] == 0
    assert row["last_error_class"] == "github_rate_limited"
    assert len(calls) == 1
    assert datetime.fromisoformat(row["next_attempt_at"]).timestamp() == pytest.approx(
        reset, abs=1e-6, rel=0
    )


def test_ready_draft_runs_once_same_sha(path, monkeypatch):
    config(path, owned=[repo()])
    watch_store.record_listing(path, "example/a", [pr(draft=True)])
    assert not watcher.process_one(path)
    assert db.list_reviews(path) == []
    watch_store.record_listing(path, "example/a", [pr(draft=False)])
    monkeypatch.setattr(main, "run_review", lambda *a, **k: ReviewResult(raw_count=0))
    assert watcher.process_one(path)
    watch_store.record_listing(path, "example/a", [pr()])
    assert not watcher.process_one(path)
    assert len(db.list_reviews(path)) == 1


def test_timeout_does_not_start_same_head_while_old_thread_is_alive(path, monkeypatch):
    config(path, owned=[repo()])
    watch_store.record_listing(path, "example/a", [pr()])
    started = threading.Event()
    release = threading.Event()

    def review(*args, **kwargs):
        started.set()
        assert release.wait(3)
        return ReviewResult(raw_count=0)

    monkeypatch.setattr(main, "run_review", review)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(watcher.process_one, path)
        assert started.wait(3)
        now = datetime.now(timezone.utc)
        with watch_store.connection(path) as conn:
            conn.execute(
                "UPDATE pr_heads SET claimed_at=?", ((now - timedelta(hours=1)).isoformat(),)
            )
        watch_store.reclaim(path, now=now)
        with watch_store.connection(path) as conn:
            conn.execute("UPDATE pr_heads SET next_attempt_at=NULL")
        try:
            assert not pool.submit(watcher.process_one, path).result(timeout=1)
        finally:
            release.set()
        assert first.result(timeout=2)



def test_moved_head_never_tries_fallback(path, monkeypatch):
    from argus.errors import HeadMoved

    config(path, owned=[repo()])
    watch_store.record_listing(path, "example/a", [pr()])
    monkeypatch.setenv("ARGUS_FALLBACK_MODEL", "fallback")
    calls = []

    def moved(*args, **kwargs):
        calls.append(args)
        raise HeadMoved("PR head changed after listing")

    monkeypatch.setattr(main, "run_review", moved)
    assert watcher.process_one(path)
    assert len(calls) == 1
    assert watch_store.heads(path)[0]["status"] == "superseded"
    review = db.list_reviews(path)[0]
    assert review["status"] == "error"
    assert review["error_class"] == "HeadMoved"


def test_idle_worker_does_not_restrict_repos(path, monkeypatch):
    monkeypatch.setattr(watch_store, "restrict_repos", lambda *a: pytest.fail("idle write"))
    assert not watcher.process_one(path)


def test_claimed_unwatched_repo_closes_without_attempt(path, monkeypatch):
    config(path, owned=[repo()])
    watch_store.record_listing(path, "example/a", [pr()])
    original = watch_store.claim

    def claim(*args, **kwargs):
        head = original(*args, **kwargs)
        config(path, owned=[repo()], deny=["example/a"])
        return head

    monkeypatch.setattr(watch_store, "claim", claim)
    monkeypatch.setattr(watcher, "trigger_review", lambda *a, **k: pytest.fail("review started"))
    before = datetime.now(timezone.utc)
    assert watcher.process_one(path)
    row = watch_store.heads(path)[0]
    assert row["status"] == "closed"
    assert datetime.fromisoformat(row["ended_at"]) >= before
    assert row["is_open"] == 0
    assert row["attempts"] == 0
    with watch_store.connection(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM watch_attempts").fetchone()[0] == 0


def test_idle_worker_sleeps_five_seconds(path, monkeypatch):
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(watcher, "process_one", lambda _: False)
    monkeypatch.setattr(asyncio, "sleep", sleep)

    async def run():
        with ThreadPoolExecutor(1) as pool:
            with pytest.raises(asyncio.CancelledError):
                await watcher._worker(path, pool)

    asyncio.run(run())
    assert sleeps == [5]


def test_poll_sqlite_work_runs_off_event_loop(path, monkeypatch):
    from argus.api import watch_metrics

    config(path, owned=[repo()])
    monkeypatch.setattr(watcher, "_open_prs", lambda _: [pr()])
    monkeypatch.setattr(github, "owned_repo_records", lambda: [repo()])
    event_thread = threading.get_ident()
    called = []
    for module, name in [
        (watch_store, "reclaim"), (watch_metrics, "daily_report"),
        (watch_store, "restrict_repos"), (watch_store, "record_listing"),
        (watch_store, "queue_sizes"),
    ]:
        original = getattr(module, name)

        def checked(*args, _name=name, _original=original, **kwargs):
            assert threading.get_ident() != event_thread, _name
            called.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(module, name, checked)
    assert asyncio.run(watcher.poll_once(path)) == 1
    assert set(called) == {"reclaim", "daily_report", "restrict_repos", "record_listing", "queue_sizes"}
