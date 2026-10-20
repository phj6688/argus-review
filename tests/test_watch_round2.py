"""Regression cases for deadline accounting and resilient local watch work."""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from argus import context, github, review
from argus.api import db, main, watch_metrics, watch_store, watcher
from argus.diff import FilePatch
from argus.jarvis import live

NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
REPO = "example/a"


@pytest.fixture
def path(tmp_path, monkeypatch):
    path = str(tmp_path / "argus.db")
    monkeypatch.setenv("ARGUS_DB_PATH", path)
    monkeypatch.setenv("ARGUS_WATCH_OWNERS", "example")
    monkeypatch.setenv("GITHUB_TOKEN", "fake")
    monkeypatch.delenv("ARGUS_FALLBACK_MODEL", raising=False)
    monkeypatch.delenv("ARGUS_WATCH_DISCOVERY_TICKS", raising=False)
    monkeypatch.setattr(github, "_quota", {})
    monkeypatch.setattr(github, "_pause_until", 0)
    monkeypatch.setattr(github, "_cache", github.OrderedDict())
    monkeypatch.setattr(watcher, "_ticks", {})
    monkeypatch.setattr(httpx, "get", lambda *a, **k: pytest.fail("unexpected HTTP request"))
    monkeypatch.setattr(review, "complete_tool", lambda *a, **k: pytest.fail("unexpected model call"))
    db.init_db(path)
    return path


def pr(number=1, age=0):
    return {"author_association": "OWNER", "number": number, "head": {"sha": "abcdef0"},
            "updated_at": (NOW - timedelta(seconds=age)).isoformat()}


def test_coverage_uses_measurement_deadline(path):
    end = NOW + timedelta(minutes=5)
    watch_store.record_listing(path, REPO, [pr()], NOW)
    rid = db.create_review(path, REPO, 1, "test-model", head_sha="abcdef0")
    with watch_store.connection(path) as conn:
        conn.execute("UPDATE reviews SET status='done', completed_at=? WHERE id=?",
                     ((end + timedelta(minutes=5)).isoformat(), rid))
        pending = watch_metrics.coverage(conn, NOW, end, as_of=end)
        resolved = watch_metrics.coverage(conn, NOW, end, as_of=end + timedelta(minutes=15))
    assert pending["pending_heads"] == 1
    assert pending["eligible_heads"] == pending["strict"]["eligible_heads"] == 0
    assert resolved["pending_heads"] == 0
    assert resolved["done_heads"] == resolved["eligible_heads"] == 1
    assert resolved["push_to_review_seconds"]["share_within_15_minutes"] == 1
    assert resolved["strict"]["share_within_15_minutes"] == 1


def test_pending_becomes_miss_at_deadline(path):
    watch_store.record_listing(path, REPO, [pr()], NOW)
    with watch_store.connection(path) as conn:
        report = watch_metrics.coverage(conn, NOW, NOW + timedelta(minutes=15))
    assert report["pending_heads"] == 0
    assert report["eligible_heads"] == report["strict"]["eligible_heads"] == 1
    assert report["coverage_share"] == report["strict"]["coverage_share"] == 0


def test_completion_at_measurement_counts(path):
    watch_store.record_listing(path, REPO, [pr()], NOW)
    end = NOW + timedelta(minutes=15)
    rid = db.create_review(path, REPO, 1, "test-model", head_sha="abcdef0")
    with watch_store.connection(path) as conn:
        conn.execute("UPDATE reviews SET status='done', completed_at=? WHERE id=?", (end.isoformat(), rid))
        report = watch_metrics.coverage(conn, NOW, end)
    assert report["done_heads"] == 1
    assert report["push_to_review_seconds"]["share_within_15_minutes"] == 1


def test_daily_report_waits_until_twenty_past(path):
    end = NOW.replace(hour=0)
    watch_metrics.daily_report(path, end + timedelta(minutes=19))
    with watch_store.connection(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM coverage_reports").fetchone()[0] == 0
    watch_metrics.daily_report(path, end + timedelta(minutes=20))
    with watch_store.connection(path) as conn:
        report = json.loads(conn.execute("SELECT report_json FROM coverage_reports").fetchone()[0])
    assert report["as_of"] == (end + timedelta(minutes=15)).isoformat()


def test_baseline_priority_and_coverage(path):
    watch_store.record_listing(path, REPO, [pr(1, 86400), pr(2, 120)], NOW)
    rows = watch_store.heads(path)
    assert [r["baseline"] for r in rows] == [1, 0]
    assert watch_store.claim(path, NOW)["pr"] == 2
    assert watch_store.claim(path, NOW)["pr"] == 1
    with watch_store.connection(path) as conn:
        report = watch_metrics.coverage(conn, NOW, NOW + timedelta(hours=1))
    assert report["baseline_heads"] == 1
    assert report["eligible_heads"] == report["strict"]["eligible_heads"] == 1


def test_later_listing_never_marks_new_head_baseline(path):
    watch_store.record_listing(path, REPO, [], NOW)
    watch_store.record_listing(path, REPO, [pr(1, 86400)], NOW)
    assert watch_store.heads(path)[0]["baseline"] == 0


def test_claim_orders_by_push_estimate(path):
    watch_store.record_listing(path, REPO, [], NOW)
    watch_store.record_listing(path, REPO, [pr(1, 60), pr(2, 600), pr(3, 3600)], NOW)
    assert [watch_store.claim(path, NOW)["pr"] for _ in range(3)] == [2, 1, 3]


def test_numeric_pagination_reuses_original_path(path, monkeypatch):
    calls = []

    def get(url, **kwargs):
        req = httpx.Request("GET", url, params=kwargs.get("params"))
        calls.append(req.url)
        page = req.url.params.get("page", "1")
        link = '<https://api.github.com/repositories/123/pulls?page=2>; rel="next"' if page == "1" else ""
        return httpx.Response(200, json=[{"number": int(page)}], headers={"Link": link}, request=req)

    monkeypatch.setattr(httpx, "get", get)
    assert github.list_cached(f"/repos/{REPO}/pulls", {"state": "open", "per_page": 100}) == [
        {"number": 1}, {"number": 2}]
    assert [url.path for url in calls] == [f"/repos/{REPO}/pulls"] * 2
    assert dict(calls[1].params) == {"state": "open", "per_page": "100", "page": "2"}


def test_pagination_loop_detected(path, monkeypatch):
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return httpx.Response(200, json=[], request=httpx.Request("GET", url), headers={
            "Link": '<https://api.github.com/repositories/123/pulls?page=2>; rel="next"'})

    monkeypatch.setattr(httpx, "get", get)
    with pytest.raises(RuntimeError, match="pagination loop"):
        github.list_cached(f"/repos/{REPO}/pulls")
    assert len(calls) == 2


@pytest.mark.parametrize("suffix", [
    "//repositories/1/pulls", "/repos//acme-corp/x/pulls", "/repos/example/a\\b/pulls",
    "/repos/example/a/compare/acme-corp:main...abcdef0", "/repos/example/a/repositories/1/pulls",
])
def test_owner_guard_bypasses_rejected(suffix, monkeypatch):
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "acme-corp")
    with pytest.raises(github.GitHubDenied):
        github.guard_url("https://api.github.com" + suffix)


@pytest.mark.parametrize("field", ["base_sha", "head_sha"])
@pytest.mark.parametrize("sha", ["owner:branch", "abcdef", "a" * 41, "ggggggg", "abcdef0/path", "abcdef0\n"])
def test_diff_sha_rejected_over_http(path, field, sha):
    response = TestClient(main.app).post("/review-diff", json={
        "repo": REPO, "base_sha": "a" * 40, "head_sha": "ABCDEF0", field: sha})
    assert response.status_code == 422
    assert db.list_reviews(path) == []


def test_failed_head_resolution_counts_and_backs_off(path, monkeypatch):
    def fail(*args):
        raise RuntimeError("lookup failed")

    monkeypatch.setattr(review, "fetch_pr_files", lambda *a: [])
    monkeypatch.setattr(live, "_pr_meta", fail)
    monkeypatch.setattr(live, "_pr_head_sha", fail)
    watch_store.record_listing(path, REPO, [pr()], NOW)
    head = watch_store.claim(path, NOW)
    with pytest.raises(RuntimeError) as caught:
        review.run_review(REPO, 1, expected_head_sha="abcdef0")
    watch_store.finish(path, head, None, caught.value, NOW)
    row = watch_store.heads(path)[0]
    assert row["attempts"] == 1
    assert row["status"] == "queued"
    assert db._parse_created_at(row["next_attempt_at"]) == NOW + timedelta(minutes=2)
    assert watch_store.claim(path, NOW) is None


def save_config(path, repos=None):
    db.save_config(path, repos=repos or [], model=main.DEFAULT_MODEL, bug_cap=5,
                   min_confidence=0.5, owned_repos=[{"full_name": REPO, "pushed_at": NOW.isoformat()}])


def test_legacy_catalogue_console_round_trip(path, monkeypatch):
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "acme-corp")
    save_config(path, [{"name": name, "watched": True} for name in [REPO, "acme-corp/secret-repo", "bad/name/extra"]])
    client = TestClient(main.app)
    body = client.get("/config").json()
    assert [r["name"] for r in body["repos"]] == [REPO]
    body["model"] = body["models"][0]["model"]
    assert client.put("/config", json=body).status_code == 200


def test_migration_prunes_invalid_catalogue(path, monkeypatch):
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "acme-corp")
    save_config(path, [{"name": name, "watched": True} for name in [REPO, "acme-corp/secret-repo", "bad/name/extra"]])
    with watch_store.connection(path) as conn:
        conn.execute("UPDATE config SET watch_mode=NULL")
    messages = []
    monkeypatch.setattr(logging.getLogger("argus.db"), "info", lambda *a: messages.append(a))
    db.init_db(path)
    db.init_db(path)
    assert db.get_config(path)["repos"] == [{"name": REPO, "watched": None}]
    assert [args[1] for args in messages if "pruned" in args[0]] == [2]


@pytest.mark.parametrize("step", ["reclaim", "daily_report", "discover_repos"])
def test_tick_step_failure_still_lists_cached_repos(path, monkeypatch, step):
    save_config(path)
    calls = []
    warnings = []
    monkeypatch.setattr(watcher.log, "warning", lambda *a, **k: warnings.append(k))
    monkeypatch.setattr(github, "owned_repo_records", lambda: db.get_config(path)["owned_repos"])
    monkeypatch.setattr(watcher, "_open_prs", lambda repo: calls.append(repo) or [])
    module = {"reclaim": watch_store, "daily_report": watch_metrics, "discover_repos": watcher}[step]

    def fail(*args):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(module, step, fail)
    for _ in range(2):
        asyncio.run(watcher.poll_once(path))
    assert calls == [REPO, REPO]
    assert len(warnings) == 2
    assert all(warning.get("exc_info") for warning in warnings)


@pytest.mark.parametrize("step", ["reclaim", "reset"])
def test_startup_failure_still_starts_ticks(path, monkeypatch, step):
    records = []
    monkeypatch.setattr(watcher.log, "warning", lambda *a, **k: records.append((a, k)))

    def fail(*args, **kwargs):
        raise RuntimeError("startup failure")

    if step == "reclaim":
        monkeypatch.setattr(watch_store, "reclaim", fail)
    else:
        monkeypatch.setattr(watch_store, "connection", fail)

    ticks = []

    async def tick(_):
        ticks.append(1)
        if len(ticks) == 2:
            raise asyncio.CancelledError

    async def worker(*args):
        await asyncio.Event().wait()

    monkeypatch.setattr(watcher, "poll_once", tick)
    monkeypatch.setattr(watcher, "poll_interval", lambda: 0.01)
    monkeypatch.setattr(watcher, "_worker", worker)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(watcher.run_forever(path))
    assert any(kwargs.get("exc_info") for _, kwargs in records)
    assert len(ticks) == 2


def test_secondary_limit_uses_retry_after(path, monkeypatch):
    monkeypatch.setattr(github.time, "time", lambda: NOW.timestamp())
    response = httpx.Response(403, json={}, request=httpx.Request("GET", "https://api.github.com/user/repos"),
                              headers={"Retry-After": "60", "X-RateLimit-Remaining": "4000",
                                       "X-RateLimit-Reset": str(int(NOW.timestamp()) + 3000)})
    with pytest.raises(github.GitHubRateLimited) as caught:
        github.observe_response(response)
    assert caught.value.reset_at == github.pause_until() == NOW.timestamp() + 60


@pytest.mark.parametrize("error", [github.GitHubRateLimited(123), github.GitHubDenied("denied"),
                                   github.GitHubAuthError("auth")])
def test_context_propagates_github_control_errors(error):
    def read(_):
        raise error

    file = FilePatch("app.py", "modified", "@@ -1 +1 @@\n-old\n+new")
    with pytest.raises(type(error)):
        list(context._file_views(file, read, context.RenderedContext()))


def test_normal_read_error_uses_hunks():
    def read(_):
        raise RuntimeError("read failed")

    file = FilePatch("app.py", "modified", "@@ -1 +1 @@\n-old\n+new")
    result = context.RenderedContext()
    assert list(context._file_views(file, read, result))
    assert result.files_read_failed == 1


def test_review_hits_quota_floor_and_defers_without_attempt(path, monkeypatch):
    reset = NOW.timestamp() + 3600
    monkeypatch.setattr(github.time, "time", lambda: NOW.timestamp())
    monkeypatch.setattr(review, "fetch_pr_files", lambda *a: [FilePatch("app.py", "modified", "@@ -1 +1 @@\n+x = 1")])

    def meta(*args):
        github._quota["core"] = {"remaining": 499, "reset": reset}
        return {"head_sha": "abcdef0"}

    monkeypatch.setattr(live, "_pr_meta", meta)
    watch_store.record_listing(path, REPO, [pr()], NOW)
    head = watch_store.claim(path, NOW)
    with pytest.raises(github.GitHubRateLimited) as caught:
        review.run_review(REPO, 1, expected_head_sha="abcdef0")
    watch_store.finish(path, head, None, caught.value, NOW)
    row = watch_store.heads(path)[0]
    assert row["attempts"] == 0
    assert db._parse_created_at(row["next_attempt_at"]).timestamp() == reset
    assert watch_store.claim(path, NOW) is None


def test_discovery_runs_every_tick_by_default(path, monkeypatch):
    calls = []
    monkeypatch.setattr(github, "owned_repo_records", lambda: calls.append(1) or [])
    for _ in range(3):
        asyncio.run(watcher.poll_once(path))
    assert len(calls) == 3


@pytest.mark.parametrize("seconds,baseline", [(900, 0), (901, 1), (-60, 0)])
def test_baseline_age_boundary(path, seconds, baseline):
    watch_store.record_listing(path, REPO, [pr(age=seconds)], NOW)
    assert watch_store.heads(path)[0]["baseline"] == baseline


def test_restart_only_baselines_new_rows(path, monkeypatch):
    watch_store.record_listing(path, REPO, [pr()], NOW)
    monkeypatch.setattr(watch_store, "_listed_repos", set())
    watch_store.record_listing(path, REPO, [pr(), pr(2)], NOW + timedelta(days=1))
    assert [r["baseline"] for r in watch_store.heads(path)] == [0, 1]


def test_failed_listing_does_not_consume_first_listing(path):
    with pytest.raises(ValueError):
        watch_store.record_listing(path, REPO, [pr(1, 3600), {"author_association": "OWNER", "number": "bad", "head": {"sha": "bad"}}], NOW)
    assert watch_store.heads(path) == []
    watch_store.record_listing(path, REPO, [pr(1, 3600)], NOW)
    assert watch_store.heads(path)[0]["baseline"] == 1


def test_baseline_column_migrates_existing_rows(path):
    watch_store.record_listing(path, REPO, [pr()], NOW)
    with watch_store.connection(path) as conn:
        conn.execute("ALTER TABLE pr_heads DROP COLUMN baseline")
    db.init_db(path)
    db.init_db(path)
    assert watch_store.heads(path)[0]["baseline"] == 0


def test_rate_limit_endpoint_can_refresh_while_paused(path, monkeypatch):
    monkeypatch.setattr(github, "_pause_until", NOW.timestamp() + 60)
    monkeypatch.setattr(github.time, "time", lambda: NOW.timestamp())
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return httpx.Response(200, json={}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)
    with pytest.raises(github.GitHubRateLimited):
        github.get(f"/repos/{REPO}/pulls")
    assert calls == []
    github.get("/rate_limit")
    assert calls == ["https://api.github.com/rate_limit"]


@pytest.mark.parametrize("retry", ["60", "Wed, 14 Oct 2026 12:01:00 GMT"])
def test_retry_after_precedes_primary_reset(path, monkeypatch, retry):
    monkeypatch.setattr(github.time, "time", lambda: NOW.timestamp())
    response = httpx.Response(403, json={}, request=httpx.Request("GET", "https://api.github.com/user/repos"),
                              headers={"Retry-After": retry, "X-RateLimit-Remaining": "4000",
                                       "X-RateLimit-Reset": str(int(NOW.timestamp()) + 3000)})
    with pytest.raises(github.GitHubRateLimited) as caught:
        github.observe_response(response)
    assert caught.value.reset_at == github.pause_until() == NOW.timestamp() + 60


@pytest.mark.parametrize("sha", ["abcdef0", "A" * 40])
def test_valid_diff_shas(sha):
    request = main.DiffReviewRequest(repo=REPO, base_sha=sha, head_sha=sha)
    assert request.base_sha == request.head_sha == sha


@pytest.mark.parametrize("error", [github.GitHubRateLimited(123), github.GitHubDenied("denied"),
                                   github.GitHubAuthError("auth")])
@pytest.mark.parametrize("reader", ["ruff", "config", "probe", "diff_probe", "reanchor", "head", "intent"])
def test_review_readers_propagate_control_errors(path, monkeypatch, error, reader):
    from argus.schema import Finding

    def fail(*args, **kwargs):
        raise error

    py = FilePatch("app.py", "modified", "@@ -1 +1 @@\n-old\n+new")
    compose = FilePatch("docker-compose.yml", "modified", "@@ -1 +1 @@\n+services: {}")
    with pytest.raises(type(error)):
        if reader == "ruff":
            review._ruff_lane([py], REPO, "abcdef0", fail)
        elif reader == "config":
            review._config_lane([compose], REPO, "abcdef0", fail)
        elif reader == "probe":
            monkeypatch.setattr(live, "probe_scan", fail)
            review._probe_lane(REPO, 1, [py], "abcdef0", fail)
        elif reader == "diff_probe":
            monkeypatch.setattr(live, "probe_scan_for_files", fail)
            review.run_diff_review([py], fast=True)
        elif reader == "reanchor":
            finding = Finding(file="app.py", line=1, severity="major", confidence=0.9,
                              title="Wrong result", description="Wrong result", proposed_fix="Fix it",
                              evidence="a_long_expression()")
            review._reanchor_findings([finding], fail)
        elif reader == "head":
            monkeypatch.setattr(live, "_pr_head_sha", fail)
            review._head_sha_for(REPO, 1)
        else:
            monkeypatch.setattr(live, "intent_section", fail)
            review._intent_for_prompt(REPO, 1)


def test_report_cli_as_of_resolves_deadline(path):
    import subprocess
    import sys

    watch_store.record_listing(path, REPO, [pr()], NOW)
    command = [sys.executable, "scripts/coverage_report.py", path,
               "--start", NOW.isoformat(), "--end", (NOW + timedelta(minutes=5)).isoformat()]
    result = subprocess.run(command + ["--as-of", (NOW + timedelta(minutes=15)).isoformat()],
                            capture_output=True, text=True, check=True)
    report = json.loads(result.stdout)
    assert report["pending_heads"] == 0
    assert report["eligible_heads"] == 1
    result = subprocess.run(command + ["--as-of", NOW.isoformat()], capture_output=True, text=True)
    assert result.returncode == 2
    assert "--as-of must not be earlier than --end" in result.stderr


@pytest.mark.parametrize("flag", ["--start", "--end", "--as-of"])
def test_report_cli_invalid_timestamp_is_usage_error(path, flag):
    import subprocess
    import sys

    args = {"--start": NOW.isoformat(), "--end": (NOW + timedelta(minutes=5)).isoformat(),
            "--as-of": (NOW + timedelta(minutes=15)).isoformat()}
    args[flag] = "not-a-timestamp"
    command = [sys.executable, "scripts/coverage_report.py", path]
    for key, value in args.items():
        command.extend([key, value])
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 2
    assert "invalid timestamp:" in result.stderr
    assert "Traceback" not in result.stderr
