"""HTTP policy and coverage contracts over a temporary database."""

import json
import logging
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from argus.api import db, main, watch_metrics, watch_store
from argus.review import ReviewResult


@pytest.fixture
def path(tmp_path, monkeypatch):
    path = str(tmp_path / "argus.db")
    monkeypatch.setenv("ARGUS_DB_PATH", path)
    monkeypatch.setenv("ARGUS_WATCH_OWNERS", "example")
    monkeypatch.delenv("ARGUS_WATCH_ENABLED", raising=False)
    db.init_db(path)
    monkeypatch.setattr(main, "run_review", lambda *a, **k: pytest.fail("unexpected review"))
    return path


@pytest.mark.parametrize("endpoint", ["/reviews", "/review-diff"])
@pytest.mark.parametrize(
    "repo,status",
    [
        ("x/..", 422),
        ("x/%2e", 422),
        ("x/y/z", 422),
        ("x/\\y", 422),
        ("/repositories/1", 422),
        ("AcMe-CoRp/secret-repo", 403),
        ("example/blocked", 403),
    ],
)
def test_api_denial_precedes_review_or_github(path, endpoint, repo, status, monkeypatch):
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "acme-corp")
    db.save_config(
        path, repos=[], model="test-model", bug_cap=5, min_confidence=0.5, deny=["EXAMPLE/blocked"]
    )
    monkeypatch.setattr(main.github, "get", lambda *a, **k: pytest.fail("unexpected GitHub read"))
    response = TestClient(main.app).post(endpoint, json={"repo": repo, "pr": 1, "diff": "x"})
    assert response.status_code == status
    assert db.list_reviews(path) == []


def test_repo_watch_validation_and_read_only_hard_deny(path, monkeypatch):
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "acme-corp")
    client = TestClient(main.app)
    payload = {
        "repos": [{"name": "acme-corp/secret-repo", "watched": True}],
        "model": main.DEFAULT_MODEL,
        "bugCap": 5,
        "minConfidence": 0.5,
    }
    assert client.put("/config", json=payload).status_code == 403
    payload["repos"] = [{"name": "bad/path/extra", "watched": None}]
    assert client.put("/config", json=payload).status_code == 422
    payload["repos"] = []
    payload["deny"] = []
    payload["hard_denied_owners"] = []
    assert client.put("/config", json=payload).json()["hard_denied_owners"] == ["acme-corp"]


def test_http_console_round_trip(path):
    now = datetime.now(timezone.utc).isoformat()
    db.save_config(
        path,
        repos=[{"name": "example/a", "watched": None}],
        model=main.DEFAULT_MODEL,
        bug_cap=5,
        min_confidence=0.5,
        owned_repos=[{"full_name": "example/a", "pushed_at": now}],
    )
    client = TestClient(main.app)
    body = client.get("/config").json()
    assert body["repos"][0]["watched"] is True
    payload = {
        "repos": [{"name": r["name"], "watched": r["watched"]} for r in body["repos"]],
        "model": body["models"][0]["model"],
        "bugCap": body["bugCap"],
        "minConfidence": body["minConfidence"],
    }
    assert client.put("/config", json=payload).status_code == 200
    assert db.get_config(path)["repos"][0]["watched"] is None


def seed(path, now):
    start = now - timedelta(hours=2)
    for number, delay in enumerate([300, 900, 1200, None], 1):
        watch_store.record_listing(
            path,
            f"example/r{number}",
            [
                {
                    "author_association": "OWNER",
                    "number": number,
                    "head": {"sha": "abc"},
                    "updated_at": start.isoformat(),
                    "head_committed_at": (start - timedelta(seconds=100)).isoformat(),
                }
            ],
            start + timedelta(seconds=30),
        )
        head = watch_store.claim(path, start + timedelta(seconds=30))
        rid = db.create_review(path, head["repo"], number, "test-model", head_sha="abc")
        if delay is None:
            db.set_error(path, rid, "overloaded", "RuntimeError")
            watch_store.finish(
                path, head, rid, RuntimeError("overloaded"), start + timedelta(seconds=300)
            )
        else:
            db.save_result(path, rid, ReviewResult(raw_count=0))
            with watch_store.connection(path) as conn:
                conn.execute(
                    "UPDATE reviews SET completed_at=? WHERE id=?",
                    ((start + timedelta(seconds=delay)).isoformat(), rid),
                )
            watch_store.finish(path, head, rid, now=start + timedelta(seconds=delay))
    return start


def test_stats_numbers_and_daily_report_once(path):
    now = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
    start = seed(path, now)
    data = watch_metrics.stats(path, now)
    window = data["24h"]
    assert window["eligible_heads"] == 4
    assert window["done_heads"] == 3
    assert window["coverage_share"] == 0.75
    assert window["excluded_ended_within_15_minutes"] == 0
    assert window["strict"] == {
        "eligible_heads": 4, "done_heads": 3, "coverage_share": 0.75,
        "share_within_15_minutes": 0.5,
    }
    assert window["push_to_review_seconds"]["median"] == 900
    assert window["push_to_review_seconds"]["p90"] == 1140
    assert window["push_to_review_seconds"]["share_within_15_minutes"] == 0.5
    assert window["commit_to_review_seconds"]["share_within_15_minutes"] == 0.25
    assert window["error_share"] == 0.25
    assert window["errors_by_class"] == {"gateway_overloaded": 1}
    assert data["open_eligible_heads"] == 4
    assert data["open_done_heads"] == 3
    tomorrow = now.replace(hour=0, minute=4) + timedelta(days=1)
    watch_metrics.daily_report(path, tomorrow)
    with watch_store.connection(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM coverage_reports").fetchone()[0] == 0
    for _ in range(3):
        watch_metrics.daily_report(path, tomorrow + timedelta(minutes=16))
    data = watch_metrics.stats(path, tomorrow)
    assert len(data["daily_reports"]) == 1
    assert data["daily_reports"][0]["eligible_heads"] == 4
    assert data["daily_reports"][0]["strict"] == window["strict"]
    assert data["daily_reports"][0]["excluded_ended_within_15_minutes"] == 0
    with watch_store.connection(path) as conn:
        assert watch_metrics.coverage(conn, start, start + timedelta(minutes=10))["done_heads"] == 1


def test_stats_additive_watch_surface(path, monkeypatch):
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "acme-corp")
    body = TestClient(main.app).get("/stats").json()
    assert {"watch", "coverage"} <= body["ops"].keys()
    assert {"24h", "48h", "open_eligible_heads", "open_done_heads", "daily_reports"} <= body["ops"][
        "coverage"
    ].keys()
    watch = body["ops"]["watch"]
    assert watch["workers"] == 2
    assert watch["llm_cap"] == 3
    assert watch["tick_interval"] == 60
    assert watch["queue_sizes"]["queued"] == 0
    assert watch["hard_denied_owners"] == ["acme-corp"]


def test_script_readonly_and_same_numbers(path):
    # 500 microseconds is the value at which SQLite julianday() rounding used to push the
    # 900 second review to 900.001 seconds.
    now = datetime(2026, 10, 14, 12, 0, 0, 500, tzinfo=timezone.utc)
    start = seed(path, now)
    before = open(path, "rb").read()
    result = subprocess.run(
        [
            sys.executable,
            "scripts/coverage_report.py",
            path,
            "--start",
            start.isoformat(),
            "--end",
            now.isoformat(),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    report = json.loads(result.stdout)
    assert report["eligible_heads"] == 4
    assert report["excluded_ended_within_15_minutes"] == 0
    assert report["strict"] == {
        "eligible_heads": 4, "done_heads": 3, "coverage_share": 0.75,
        "share_within_15_minutes": 0.5,
    }
    assert open(path, "rb").read() == before
    with pytest.raises(Exception):
        import sqlite3

        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            conn.execute("DELETE FROM pr_heads")
        finally:
            conn.close()


def test_logging_setup_idempotent_and_access_logger_unchanged(path):
    access = logging.getLogger("uvicorn.access")
    original = list(access.handlers)
    main.configure_logging()
    main.configure_logging()
    logger = logging.getLogger("argus")
    assert logger.isEnabledFor(logging.INFO)
    assert sum(getattr(h, "_argus_stdout", False) for h in logger.handlers) == 1
    assert not logger.propagate
    assert access.handlers == original


def test_legacy_done_without_completion_counts_without_inventing_latency(path):
    now = datetime.now(timezone.utc)
    rid = db.create_review(path, "example/a", 1, "test-model", head_sha="abc")
    db.set_status(path, rid, "done")
    watch_store.record_listing(path, "example/a", [{"author_association": "OWNER", "number": 1, "head": {"sha": "abc"}}], now)
    data = watch_metrics.stats(path, now + timedelta(seconds=1))["24h"]
    assert data["done_heads"] == 1
    assert data["push_to_review_seconds"]["median"] is None



def test_denied_repo_can_stay_in_catalogue(path, monkeypatch):
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "acme-corp")
    from argus.api import watcher

    db.save_config(
        path, repos=[], model=main.DEFAULT_MODEL, bug_cap=5, min_confidence=0.5,
        owned_repos=[{"full_name": "example/foo", "pushed_at": datetime.now(timezone.utc).isoformat()}],
    )
    client = TestClient(main.app)
    payload = {
        "repos": [], "model": main.DEFAULT_MODEL, "bugCap": 5,
        "minConfidence": 0.5, "deny": ["example/foo"],
    }
    assert client.put("/config", json=payload).status_code == 200
    payload["repos"] = [{"name": "example/foo", "watched": True}]
    assert client.put("/config", json=payload).status_code == 200
    assert db.get_config(path)["deny"] == ["example/foo"]
    assert watcher._watched_repos(path) == []
    assert client.post("/reviews", json={"repo": "example/foo", "pr": 1}).status_code == 403
    payload["repos"] = [{"name": "acme-corp/x", "watched": True}]
    assert client.put("/config", json=payload).status_code == 403


@pytest.mark.parametrize("seconds,excluded", [(60, 1), (900, 1), (1200, 0)])
@pytest.mark.parametrize("ending", ["superseded", "closed"])
def test_fair_coverage_excludes_only_short_lived_heads(path, seconds, excluded, ending):
    start = datetime(2026, 10, 14, 10, tzinfo=timezone.utc)
    watch_store.record_listing(path, "example/a", [{"author_association": "OWNER", "number": 1, "head": {"sha": "old"}}], start)
    watch_store.record_listing(
        path, "example/a",
        [{"author_association": "OWNER", "number": 1, "head": {"sha": "new"}, "draft": True}] if ending == "superseded" else [],
        start + timedelta(seconds=seconds),
    )
    with watch_store.connection(path) as conn:
        report = watch_metrics.coverage(conn, start, start + timedelta(hours=1))
    assert report["eligible_heads"] == 1 - excluded
    assert report["done_heads"] == 0
    assert report["coverage_share"] == (None if excluded else 0)
    assert report["excluded_ended_within_15_minutes"] == excluded
    assert report["strict"] == {
        "eligible_heads": 1, "done_heads": 0, "coverage_share": 0,
        "share_within_15_minutes": 0,
    }
    assert report["superseded"] == int(ending == "superseded")
    assert report["skipped"] == int(ending == "superseded")


def test_draft_coverage_starts_when_ready_and_keeps_first_eligibility(path):
    draft = datetime(2026, 10, 14, 9, tzinfo=timezone.utc)
    ready = draft + timedelta(hours=1)
    pr = {"author_association": "OWNER", "number": 1, "head": {"sha": "abc"}, "draft": True, "updated_at": draft.isoformat()}
    watch_store.record_listing(path, "example/a", [pr], draft)
    assert watch_store.heads(path)[0]["eligible_at"] is None
    pr.update(draft=False, updated_at=ready.isoformat())
    watch_store.record_listing(path, "example/a", [pr], ready)
    pr["updated_at"] = (ready + timedelta(minutes=1)).isoformat()
    watch_store.record_listing(path, "example/a", [pr], ready + timedelta(minutes=1))
    row = watch_store.heads(path)[0]
    assert row["eligible_at"] == ready.isoformat()
    assert row["eligible_pr_updated_at"] == ready.isoformat()
    rid = db.create_review(path, "example/a", 1, "test-model", head_sha="abc")
    db.save_result(path, rid, ReviewResult(raw_count=0))
    with watch_store.connection(path) as conn:
        conn.execute("UPDATE reviews SET completed_at=? WHERE id=?", ((ready + timedelta(minutes=5)).isoformat(), rid))
        assert watch_metrics.coverage(conn, draft, ready)["eligible_heads"] == 0
        report = watch_metrics.coverage(conn, ready, ready + timedelta(hours=1))
    assert report["eligible_heads"] == report["done_heads"] == 1
    assert report["push_to_review_seconds"]["median"] == 300
    assert report["strict"]["share_within_15_minutes"] == 1


def test_short_lived_reviewed_head_stays_in_fair_coverage(path):
    start = datetime(2026, 10, 14, 10, tzinfo=timezone.utc)
    watch_store.record_listing(path, "example/a", [{"author_association": "OWNER", "number": 1, "head": {"sha": "abc"}}], start)
    watch_store.record_listing(path, "example/a", [], start + timedelta(seconds=60))
    rid = db.create_review(path, "example/a", 1, "test-model", head_sha="abc")
    db.save_result(path, rid, ReviewResult(raw_count=0))
    with watch_store.connection(path) as conn:
        conn.execute("UPDATE reviews SET completed_at=? WHERE id=?", ((start + timedelta(seconds=30)).isoformat(), rid))
        report = watch_metrics.coverage(conn, start, start + timedelta(hours=1))
    assert report["eligible_heads"] == report["done_heads"] == 1
    assert report["excluded_ended_within_15_minutes"] == 0


@pytest.mark.parametrize("micro", [0, 499, 500, 510, 519, 999])
@pytest.mark.parametrize("delta,within", [(timedelta(seconds=900), 1), (timedelta(seconds=900, milliseconds=1), 0)])
def test_review_at_exactly_fifteen_minutes_counts_as_within(path, micro, delta, within):
    start = datetime(2026, 10, 14, 10, 0, 0, micro, tzinfo=timezone.utc)
    watch_store.record_listing(
        path, "example/a",
        [{"author_association": "OWNER", "number": 1, "head": {"sha": "abc"}, "updated_at": start.isoformat()}], start,
    )
    rid = db.create_review(path, "example/a", 1, "test-model", head_sha="abc")
    db.save_result(path, rid, ReviewResult(raw_count=0))
    with watch_store.connection(path) as conn:
        conn.execute(
            "UPDATE reviews SET completed_at=? WHERE id=?", ((start + delta).isoformat(), rid)
        )
        report = watch_metrics.coverage(conn, start - timedelta(hours=1), start + timedelta(hours=1))
    assert report["push_to_review_seconds"]["within_15_minutes"] == within
    assert report["push_to_review_seconds"]["share_within_15_minutes"] == within
    assert report["strict"]["share_within_15_minutes"] == within


def test_two_done_reviews_in_one_millisecond_use_the_earlier_exact_time(path):
    # With a start at 500 microseconds, 900.0004 s and 900.0006 s after the push both round to
    # the same julianday() millisecond, yet their latencies round to 900.000 and 900.001.
    start = datetime(2026, 10, 14, 10, 0, 0, 500, tzinfo=timezone.utc)
    early = start + timedelta(seconds=900, microseconds=400)
    late = start + timedelta(seconds=900, microseconds=600)
    watch_store.record_listing(
        path, "example/a",
        [{"author_association": "OWNER", "number": 1, "head": {"sha": "abc"}, "updated_at": start.isoformat()}], start,
    )
    # The later review is created first so row order cannot pick the right one by luck.
    ids = []
    for _ in range(2):
        rid = db.create_review(path, "example/a", 1, "test-model", head_sha="abc")
        db.save_result(path, rid, ReviewResult(raw_count=0))
        ids.append(rid)
    with watch_store.connection(path) as conn:
        for rid, moment in zip(ids, (late, early)):
            conn.execute("UPDATE reviews SET completed_at=? WHERE id=?", (moment.isoformat(), rid))
        tied = conn.execute(
            "SELECT julianday(?) = julianday(?)", (early.isoformat(), late.isoformat())
        ).fetchone()[0]
        report = watch_metrics.coverage(conn, start - timedelta(hours=1), start + timedelta(hours=1))
    assert tied == 1
    assert report["push_to_review_seconds"]["within_15_minutes"] == 1
    assert report["push_to_review_seconds"]["median"] == 900
