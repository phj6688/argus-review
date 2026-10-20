from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

import pytest

from argus.api import db, watch_store
from argus.github import GitHubRateLimited


@pytest.fixture
def path(tmp_path):
    path = str(tmp_path / "db.sqlite")
    db.init_db(path)
    return path


def listing(sha="abc", **kwargs):
    return {"author_association": "OWNER", "number": 1, "head": {"sha": sha}, "user": {"login": "human"}, **kwargs}


def test_transitions(path):
    now = datetime.now(timezone.utc)
    watch_store.record_listing(path, "example/a", [listing(draft=True)], now)
    assert watch_store.heads(path)[0]["status"] == "skipped"
    watch_store.record_listing(path, "example/a", [listing()], now)
    assert watch_store.heads(path)[0]["status"] == "queued"
    watch_store.record_listing(path, "example/a", [listing("def")], now)
    assert [r["status"] for r in watch_store.heads(path)] == ["superseded", "queued"]
    watch_store.record_listing(path, "example/a", [], now)
    assert watch_store.heads(path)[1]["status"] == "closed"


def test_atomic_claim_and_priority(path):
    now = datetime.now(timezone.utc)
    watch_store.record_listing(path, "example/old", [listing()], now - timedelta(hours=1))
    watch_store.record_listing(path, "example/new", [listing()], now)
    assert watch_store.claim(path, now)["repo"] == "example/new"
    with ThreadPoolExecutor(8) as pool:
        claimed = list(pool.map(lambda _: watch_store.claim(path, now), range(10)))
    assert sum(r is not None for r in claimed) == 1


def test_backoff_and_rate_limit(path):
    now = datetime.now(timezone.utc)
    watch_store.record_listing(path, "example/a", [listing()], now)
    head = watch_store.claim(path, now)
    reset = now + timedelta(minutes=20)
    watch_store.finish(path, head, None, GitHubRateLimited(reset.timestamp()), now)
    row = watch_store.heads(path)[0]
    assert row["attempts"] == 0
    assert row["next_attempt_at"] == reset.isoformat()
    now = reset
    for attempt, minutes in enumerate([2, 10, 30, 30], 1):
        head = watch_store.claim(path, now)
        watch_store.finish(path, head, None, RuntimeError("overloaded"), now)
        row = watch_store.heads(path)[0]
        assert row["attempts"] == attempt
        assert row["status"] == ("parked" if attempt == 4 else "queued")
        now += timedelta(minutes=minutes)


def test_commit_timestamp_saved_with_review(path):
    from argus.review import ReviewResult

    now = datetime.now(timezone.utc)
    watch_store.record_listing(path, "example/a", [listing()], now)
    head = watch_store.claim(path, now)
    rid = db.create_review(path, "example/a", 1, "test-model", head_sha="abc")
    with watch_store.connection(path) as conn:
        conn.execute("UPDATE pr_heads SET review_id=? WHERE id=?", (rid, head["id"]))
    db.save_result(path, rid, ReviewResult(raw_count=0, head_committed_at=now.isoformat()))
    assert watch_store.heads(path)[0]["head_committed_at"] == now.isoformat()


def test_commit_date_uses_matching_head_without_extra_fetch(monkeypatch):
    import httpx
    from argus.jarvis import live

    calls = []

    def get(path, params=None):
        calls.append(path)
        body = (
            [
                {
                    "sha": "head",
                    "commit": {"message": "change", "committer": {"date": "2026-10-14T01:00:00Z"}},
                }
            ]
            if path.endswith("/commits")
            else {"head": {"sha": "head"}}
        )
        return httpx.Response(
            200, json=body, request=httpx.Request("GET", "https://api.github.com" + path)
        )

    monkeypatch.setattr(live.github, "get", get)
    assert live._pr_meta("example/a", 1)["head_committed_at"] == "2026-10-14T01:00:00Z"
    assert len(calls) == 2


def test_draft_toggle_does_not_reset_parked_budget(path):
    now = datetime.now(timezone.utc)
    watch_store.record_listing(path, "example/a", [listing()], now)
    with watch_store.connection(path) as conn:
        conn.execute("UPDATE pr_heads SET status='parked', attempts=4")
    watch_store.record_listing(path, "example/a", [listing(draft=True)], now)
    watch_store.record_listing(path, "example/a", [listing(draft=False)], now)
    assert watch_store.claim(path, now) is None
    assert watch_store.heads(path)[0]["status"] == "parked"


def test_moved_head_is_rejected_before_review(monkeypatch):
    from argus import review
    from argus.jarvis import live

    monkeypatch.setattr(review, "fetch_pr_files", lambda *a: [])
    monkeypatch.setattr(live, "_pr_meta", lambda *a: {"head_sha": "new"})
    monkeypatch.setattr(review, "_deep_review", lambda *a: pytest.fail("review started"))
    from argus.errors import HeadMoved

    with pytest.raises(HeadMoved):
        review.run_review("example/a", 1, expected_head_sha="old")



def test_moved_head_is_not_a_failed_attempt(path):
    from argus.errors import HeadMoved, classify_error
    from argus.api import watch_metrics

    now = datetime.now(timezone.utc)
    watch_store.record_listing(path, "example/a", [listing()], now)
    with watch_store.connection(path) as conn:
        conn.execute("UPDATE pr_heads SET attempts=2")
    head = watch_store.claim(path, now)
    ended = now + timedelta(seconds=60)
    error = HeadMoved("PR head changed after listing")
    assert classify_error(error) == "head_moved"
    watch_store.finish(path, head, None, error, ended)
    row = watch_store.heads(path)[0]
    assert row["status"] == "superseded"
    assert row["ended_at"] == ended.isoformat()
    assert row["attempts"] == 2
    with watch_store.connection(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM watch_attempts").fetchone()[0] == 0
        report = watch_metrics.coverage(conn, now, ended + timedelta(seconds=1))
    assert report["errored_attempts"] == 0
    assert report["finished_attempts"] == 0
    assert report["error_share"] is None


@pytest.mark.parametrize("ending", ["superseded", "closed", "restricted"])
def test_listing_records_end_time(path, ending):
    now = datetime.now(timezone.utc)
    watch_store.record_listing(path, "example/a", [listing()], now)
    ended = now + timedelta(seconds=60)
    if ending == "restricted":
        watch_store.restrict_repos(path, [], now=ended)
    else:
        watch_store.record_listing(
            path, "example/a", [listing("new")] if ending == "superseded" else [], ended
        )
    row = watch_store.heads(path)[0]
    assert row["ended_at"] == ended.isoformat()
    assert row["status"] == ("closed" if ending == "restricted" else ending)


def test_eligibility_backfill_is_idempotent(path):
    now = datetime.now(timezone.utc)
    # Drop the new columns to reproduce a database from before this migration.
    with watch_store.connection(path) as conn:
        for name in ("ended_at", "eligible_at", "eligible_pr_updated_at"):
            columns = {r["name"] for r in conn.execute("PRAGMA table_info(pr_heads)")}
            if name in columns:
                conn.execute(f"ALTER TABLE pr_heads DROP COLUMN {name}")
        conn.execute(
            "INSERT INTO pr_heads(repo,pr,head_sha,first_seen_at,pr_updated_at,status) "
            "VALUES(?,?,?,?,?,'queued')",
            ("example/a", 1, "old", now.isoformat(), now.isoformat()),
        )
        conn.execute(
            "INSERT INTO pr_heads(repo,pr,head_sha,first_seen_at,status,skip_reason) "
            "VALUES(?,?,?,?,'skipped','draft')",
            ("example/a", 2, "draft", now.isoformat()),
        )
    db.init_db(path)
    db.init_db(path)
    rows = watch_store.heads(path)
    assert rows[0]["eligible_at"] == now.isoformat()
    assert rows[0]["eligible_pr_updated_at"] == now.isoformat()
    assert rows[1]["eligible_at"] is None
