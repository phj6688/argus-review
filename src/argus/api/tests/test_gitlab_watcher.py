"""Exercise GitLab ticks through the shared queue without external requests."""

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from argus import github
from argus.api import db, main, watch_store, watcher
from argus.forge.keys import gitlab_key
from argus.gitlab import client as gitlab_client
from argus.gitlab import listing, projects, status, trust
from argus.gitlab.client import GitLabAuthError, GitLabClient, GitLabHTTPError
from argus.review import ReviewResult

HOST = "gitlab.example.test"
KEY = gitlab_key(HOST, 42)
OTHER = gitlab_key(HOST, 43)
GH = "example/app"
NOW = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)
TOKEN = "gl" + "pat-" + "x" * 20


def unexpected(*args, **kwargs):
    pytest.fail("Unexpected network, client or review call")


def mr(number=1, **extra):
    return {"iid": number, "sha": "a" * 40, "state": "opened", "draft": False,
            "author": {"id": 7, "username": "human", "bot": False},
            "updated_at": NOW.isoformat(), "target_branch": "main",
            "source_project_id": 42, "merged_at": None, **extra}


@pytest.fixture
def world(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith(("ARGUS_GITLAB_", "ARGUS_WATCH_")):
            monkeypatch.delenv(name)
    path = str(tmp_path / "argus.db")
    for name, value in {
        "ARGUS_DB_PATH": path, "ARGUS_GITLAB_URL": f"https://{HOST}",
        "ARGUS_GITLAB_TOKEN": TOKEN, "ARGUS_GITLAB_ALLOW": KEY,
        "ARGUS_GITLAB_WATCH": "1", "ARGUS_WATCH_OWNERS": "example",
        "ARGUS_DENY_OWNERS": "blocked", "ARGUS_PUBLISH": "0",
        "ARGUS_WATCH_DISCOVERY_TICKS": "1", "ARGUS_OUTCOME_INTERVAL_TICKS": "1000",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("ARGUS_FALLBACK_MODEL", raising=False)
    for name in ("_ticks", "_last_tick", "_repo_pushed_at", "_previous_watched_repos",
                 "_gitlab_expanded", "_gitlab_ticks", "_gitlab_pass_errors", "_outcome_tasks"):
        monkeypatch.setattr(watcher, name, {}, raising=False)
    monkeypatch.setattr(watcher, "_active_heads", set())
    monkeypatch.setattr(watch_store, "_listed_repos", set())
    monkeypatch.setattr(watch_store, "utcnow", lambda: NOW)
    monkeypatch.setattr(github, "_pause_until", 0)
    monkeypatch.setattr(github, "_quota", {})
    monkeypatch.setattr(status, "_identity_cache", None)
    monkeypatch.setattr(GitLabClient, "retry_delay", 0)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", unexpected)
    monkeypatch.setattr(main, "run_review", unexpected)
    state = SimpleNamespace(path=path, rows=[mr()], gh_rows=[], requests=[],
                            responses={}, gh_calls=[], projects={})

    def handle(request):
        state.requests.append(request)
        route = request.url.path
        if route in state.responses:
            if callable(state.responses[route]):
                return state.responses[route](request)
            return httpx.Response(state.responses[route], text="private server detail " + TOKEN,
                                  headers={"Retry-After": "300"})
        if route == "/api/v4/user":
            return httpx.Response(200, json={"id": 99, "username": "reviewer", "bot": True})
        if "/members/all/" in route:
            return httpx.Response(200, json={"access_level": 30})
        if route.endswith("/merge_requests"):
            rows = state.rows
            if request.url.params["state"] == "opened":
                rows = [r for r in rows if r["state"] == "opened"]
            return httpx.Response(200, json=rows)
        if "/merge_requests/" in route:
            return httpx.Response(200, json=next(r for r in state.rows
                                               if r["iid"] == int(route.rsplit("/", 1)[1])))
        project_id = int(route.rsplit("/", 1)[1])
        return httpx.Response(200, json={"id": project_id, "archived": False,
            "path_with_namespace": state.projects.get(project_id, f"allowed/app-{project_id}"),
            "default_branch": "main", "visibility": "private"})

    client = GitLabClient(transport=httpx.MockTransport(handle))
    state.client = client
    monkeypatch.setattr(gitlab_client, "client", lambda: client)
    monkeypatch.setattr(projects, "shared_client", lambda: client)
    monkeypatch.setattr(trust, "shared_client", lambda: client)
    records = [{"full_name": GH, "pushed_at": NOW.isoformat()}]
    monkeypatch.setattr(github, "owned_repo_records", lambda: records)

    def gh_open(repo):
        state.gh_calls.append(repo)
        return state.gh_rows

    monkeypatch.setattr(watcher, "_open_prs", gh_open)
    monkeypatch.setattr(watcher, "_recently_closed_prs", unexpected)
    db.init_db(path)
    db.save_config(path, repos=[{"name": GH, "watched": True}], model="test-model",
                   bug_cap=5, min_confidence=0.5, owned_repos=records)
    yield state
    client.close()


def tick(world):
    return asyncio.run(watcher.poll_once(world.path))


def cursor(world, key=KEY):
    with watch_store.connection(world.path) as conn:
        return db.gitlab_poll_cursor(conn, key)


def rows(world, key=KEY):
    return [r for r in watch_store.heads(world.path) if r["repo"] == key]


def listings(world):
    return [r for r in world.requests if r.url.path.endswith("/merge_requests")]


def github_row():
    return {"number": 1, "head": {"sha": "abc"}, "author_association": "OWNER",
            "user": {"login": "human", "type": "User"}}


def test_new_head_worker_idempotence_and_push_without_timestamp_change(world, monkeypatch, caplog):
    caplog.set_level("INFO", logger="argus.watcher")
    review = Mock(return_value=ReviewResult(raw_count=0))
    monkeypatch.setattr(main, "run_review", review)
    assert tick(world) == 1
    assert "watcher: forge=gitlab projects=1 queued=1 pushed=0" in caplog.messages
    assert rows(world)[0]["status"] == "queued"
    assert watcher.process_one(world.path)
    assert rows(world)[0]["status"] == "done"
    assert review.call_count == 1
    assert review.call_args.kwargs["expected_head_sha"] == "a" * 40
    assert tick(world) == 0
    assert "watcher: forge=gitlab projects=1 queued=0 pushed=0" in caplog.messages
    assert not watcher.process_one(world.path)
    assert review.call_count == 1
    world.rows[0]["sha"] = "b" * 40
    assert tick(world) == 1
    assert "watcher: forge=gitlab projects=1 queued=1 pushed=1" in caplog.messages
    assert watcher.process_one(world.path)
    assert review.call_count == 2
    assert review.call_args.kwargs["expected_head_sha"] == "b" * 40
    assert len(db.list_reviews(world.path)) == 2
    assert listings(world)[-1].url.params["updated_after"] == "2026-11-04T11:55:00+00:00"
    assert all(not r["full_name"].startswith("gitlab")
               for r in db.get_config(world.path)["owned_repos"])


def test_incremental_close_and_push_supersedes_queued_head(world):
    assert tick(world) == 1
    world.rows[0]["sha"] = "b" * 40
    assert tick(world) == 1
    assert [(r["status"], r["is_open"]) for r in rows(world)] == [
        ("superseded", 0), ("queued", 1)]
    world.rows[0]["state"] = "closed"
    assert tick(world) == 0
    assert (rows(world)[-1]["status"], rows(world)[-1]["is_open"]) == ("closed", 0)
    assert listings(world)[-1].url.params["state"] == "all"


def test_gitlab_failure_preserves_github_result_and_rows(world, monkeypatch, tmp_path):
    assert tick(world) == 1
    reference = str(tmp_path / "reference.db")
    db.init_db(reference)
    db.save_config(reference, repos=[{"name": GH, "watched": True}], model="test-model",
                   bug_cap=5, min_confidence=0.5)
    world.gh_rows = [github_row()]
    with monkeypatch.context() as patch:
        patch.setenv("ARGUS_GITLAB_WATCH", "0")
        expected = asyncio.run(watcher.poll_once(reference))
    monkeypatch.setattr(listing, "list_changes", Mock(side_effect=RuntimeError("private detail " + TOKEN)))
    assert tick(world) == expected == 1
    actual = [{k: v for k, v in r.items() if k != "id"} for r in rows(world, GH)]
    assert actual == [{k: v for k, v in r.items() if k != "id"} for r in watch_store.heads(reference)]
    assert cursor(world)["last_error"] == "gitlab_http"


@pytest.mark.parametrize("github_state", ["paused", "failed"])
def test_gitlab_runs_after_github_pause_or_failure(world, monkeypatch, github_state):
    if github_state == "paused":
        monkeypatch.setattr(github, "_pause_until", time.time() + 300)
    else:
        monkeypatch.setattr(watcher, "_watched_repos", Mock(side_effect=RuntimeError("failure")))
    assert tick(world) == 1
    assert len(listings(world)) == 1
    assert rows(world)[0]["status"] == "queued"


def test_cursor_stays_put_when_recording_fails_then_retries_full(world, monkeypatch):
    assert tick(world) == 1
    before = cursor(world)
    world.rows = [mr(2, updated_at=(NOW + timedelta(minutes=1)).isoformat())]
    with monkeypatch.context() as patch:
        patch.setattr(watch_store, "record_changes", Mock(side_effect=RuntimeError("write failed")))
        assert tick(world) == 0
    assert cursor(world)["updated_after"] == before["updated_after"]
    assert cursor(world)["last_error"] == "gitlab_http"
    assert tick(world) == 1
    assert listings(world)[-1].url.params["state"] == "opened"
    assert rows(world)[-1]["pr"] == 2
    assert cursor(world)["last_error"] is None


def test_full_reconciliation_closes_missed_close(world, monkeypatch):
    monkeypatch.setenv("ARGUS_GITLAB_RECONCILE_TICKS", "2")
    assert tick(world) == 1
    world.rows = []
    assert tick(world) == 0
    assert [r.url.params["state"] for r in listings(world)] == ["opened", "opened"]
    assert (rows(world)[0]["status"], rows(world)[0]["is_open"]) == ("closed", 0)
    assert cursor(world)["last_full_reconcile"] is not None


@pytest.mark.parametrize("invalid", [{"iid": "bad"}, {"updated_at": "bad"}, {"sha": None}],
                         ids=["bad_iid", "bad_updated_at", "head_preparing"])
def test_incomplete_full_listing_preserves_queued_head_until_clean_reconcile(world, monkeypatch, invalid):
    monkeypatch.setenv("ARGUS_GITLAB_RECONCILE_TICKS", "1")
    assert tick(world) == 1
    last_full = cursor(world)["last_full_reconcile"]
    later = NOW + timedelta(minutes=1)
    monkeypatch.setattr(watch_store, "utcnow", lambda: later)
    world.rows = [mr(**invalid), mr(2, updated_at=later.isoformat())]
    assert tick(world) == 1
    assert [(r["pr"], r["status"], r["is_open"]) for r in rows(world)] == [
        (1, "queued", 1), (2, "queued", 1),
    ]
    # A head still being prepared holds the cursor at its own update time.
    held = NOW.isoformat() if invalid == {"sha": None} else later.isoformat()
    assert cursor(world)["updated_after"] == held
    assert cursor(world)["last_full_reconcile"] == last_full
    world.rows = [mr(2, updated_at=later.isoformat())]
    assert tick(world) == 0
    assert [(r["pr"], r["status"], r["is_open"]) for r in rows(world)] == [
        (1, "closed", 0), (2, "queued", 1),
    ]
    assert cursor(world)["last_full_reconcile"] == later.isoformat()
    assert [r.url.params["state"] for r in listings(world)] == ["opened"] * 3


@pytest.mark.parametrize("source_id,source_status,expected", [
    (99, 200, "skipped"), (99, 500, "queued"), (0, 200, "skipped"),
])
def test_fork_denial_is_distinct_from_lookup_failure(world, source_id, source_status, expected):
    world.rows = [mr(source_project_id=source_id), mr(2, source_project_id=source_id)]
    if source_status != 200:
        world.responses["/api/v4/projects/99"] = source_status
    assert tick(world) == (2 if expected == "queued" else 0)
    assert [r["status"] for r in rows(world)] == [expected, expected]
    assert [r["skip_reason"] for r in rows(world)] == [
        "fork_source_denied" if expected == "skipped" else None] * 2
    if expected == "skipped":
        assert not watcher.process_one(world.path)
        assert db.list_reviews(world.path) == []
        assert status.integration_status()["watch"]["forkSourceDenied"] == 2
    source_requests = [r for r in world.requests if r.url.path == "/api/v4/projects/99"]
    # Both MRs share the source lookup result within this pass.
    assert len(source_requests) == (0 if source_id == 0 else 4 if source_status == 500 else 2)


def test_unknown_trust_health_and_sweep_requeues_once(world):
    world.responses["/api/v4/projects/42/members/all/7"] = 500
    assert tick(world) == 0
    assert (rows(world)[0]["status"], rows(world)[0]["skip_reason"]) == ("skipped", "trust_unknown")
    assert not watcher.process_one(world.path)
    assert status.integration_status()["watch"]["trustUnknown"] == 1
    assert any(r["label"] == "GitLab trust" and r["state"] == "warn"
               for r in main.get_integrations()["health"])
    world.responses.clear()
    world.rows = []
    assert tick(world) == 1
    assert rows(world)[0]["status"] == "queued"
    assert tick(world) == 0
    assert trust.sweep_unknown(world.path, 5, client=world.client)["requeued"] == 0
    assert status.integration_status()["trustUnknown"] == 0


@pytest.mark.parametrize("disabled", ["off", "url", "invalid", "client"])
def test_disabled_gitlab_never_constructs_client_or_changes_github(world, monkeypatch, disabled):
    if disabled == "url":
        monkeypatch.delenv("ARGUS_GITLAB_URL")
    elif disabled == "invalid":
        monkeypatch.setenv("ARGUS_GITLAB_WATCH", "invalid")
    else:
        monkeypatch.setenv("ARGUS_GITLAB_WATCH", "0")
    world.gh_rows = [github_row()]
    monkeypatch.setattr(gitlab_client, "client", unexpected)
    assert tick(world) == 1
    before = rows(world, GH)
    if disabled == "client":
        monkeypatch.setenv("ARGUS_GITLAB_WATCH", "1")
        monkeypatch.setattr(gitlab_client, "client", lambda: None)
    assert tick(world) == 0
    assert rows(world, GH) == before
    assert world.requests == []
    assert rows(world) == []


def test_startup_reentry_reopens_heads_despite_stored_cursor(world):
    assert tick(world) == 1
    watcher._previous_watched_repos.clear()
    with watch_store.connection(world.path) as conn:
        conn.execute("UPDATE pr_heads SET is_open=0")
    assert tick(world) == 0
    assert listings(world)[-1].url.params["state"] == "opened"
    assert watch_store.open_head_shas(world.path, KEY) == {1: "a" * 40}


def test_rate_limit_pauses_only_gitlab_without_advancing_cursor(world):
    assert tick(world) == 1
    before = cursor(world)["updated_after"]
    world.responses["/api/v4/projects/42/merge_requests"] = 429
    world.gh_rows = [github_row()]
    assert tick(world) == 1
    assert watcher.quota_paused("gitlab")
    assert not watcher.quota_paused()
    assert cursor(world)["updated_after"] == before
    assert cursor(world)["last_error"] == "gitlab_rate_limited"
    count = len(world.requests)
    world.gh_rows = [github_row() | {"head": {"sha": "def"}}]
    assert tick(world) == 1
    assert len(world.requests) == count
    assert rows(world, GH)[-1]["head_sha"] == "def"
    world.client._pause = 0
    world.responses.clear()
    world.rows[0]["sha"] = "b" * 40
    assert tick(world) == 1
    assert listings(world)[-1].url.params["state"] == "opened"


def test_watch_status_shape_classified_error_and_health(world, monkeypatch, caplog):
    assert tick(world) == 1
    monkeypatch.setattr(listing, "list_changes", Mock(side_effect=RuntimeError("private server detail " + TOKEN)))
    assert tick(world) == 0
    watch = status.integration_status()["watch"]
    assert watch == {"enabled": True, "projectCount": 1,
                     "lastPolledAt": cursor(world)["last_polled_at"],
                     "lastError": "gitlab_http", "trustUnknown": 0, "forkSourceDenied": 0}
    assert watch["lastPolledAt"] is not None
    result = main.get_integrations()
    assert {"label": "GitLab watcher", "status": "last poll failed", "state": "warn"} in result["health"]
    for output in (json.dumps(result), caplog.text):
        assert TOKEN not in output
        assert "private server detail" not in output


@pytest.mark.parametrize("include", [False, True])
def test_active_days_drafts_and_bots(world, monkeypatch, include):
    monkeypatch.setenv("ARGUS_WATCH_ACTIVE_DAYS", "10")
    monkeypatch.setenv("ARGUS_WATCH_INCLUDE_DRAFTS", str(int(include)))
    monkeypatch.setenv("ARGUS_WATCH_INCLUDE_BOTS", str(int(include)))
    world.rows = [mr(updated_at=(NOW - timedelta(days=11)).isoformat()), mr(2, draft=True),
                  mr(3, author={"id": 8, "username": "robot", "bot": True})]
    assert tick(world) == (2 if include else 0)
    assert [r["pr"] for r in rows(world)] == [2, 3]
    assert [r["skip_reason"] for r in rows(world)] == ([None, None] if include else ["draft", "bot"])


def test_active_days_keeps_stale_mr_out_of_full_and_incremental_listings(world, monkeypatch):
    monkeypatch.setenv("ARGUS_WATCH_ACTIVE_DAYS", "10")
    stale = (NOW - timedelta(days=11)).isoformat()
    world.rows = [mr(updated_at=stale)]
    assert tick(world) == 0
    assert rows(world) == []
    assert cursor(world)["updated_after"] == stale
    assert tick(world) == 0
    assert rows(world) == []
    assert [r.url.params["state"] for r in listings(world)] == ["opened", "all"]
    assert not watcher.process_one(world.path)


def test_allow_refresh_cache_and_pass_error_recovery(world, monkeypatch):
    monkeypatch.setenv("ARGUS_WATCH_DISCOVERY_TICKS", "2")
    expand = Mock(wraps=listing.expand_allow)
    monkeypatch.setattr(listing, "expand_allow", expand)
    assert tick(world) == 1
    expand.side_effect = GitLabHTTPError("private server detail " + TOKEN)
    assert tick(world) == 0
    assert expand.call_count == 1
    world.rows[0]["sha"] = "b" * 40
    assert tick(world) == 1
    assert watcher.gitlab_pass_error(world.path) == "gitlab_http"
    assert status.integration_status()["watch"]["lastError"] == "gitlab_http"
    assert cursor(world)["last_error"] is None
    expand.side_effect = None
    assert tick(world) == 0
    assert tick(world) == 0
    assert watcher.gitlab_pass_error(world.path) is None


def test_expansion_failure_without_cache_records_safe_pass_error(world, monkeypatch):
    monkeypatch.setattr(listing, "expand_allow", Mock(side_effect=GitLabAuthError(TOKEN)))
    assert tick(world) == 0
    assert listings(world) == []
    assert status.integration_status()["watch"]["lastError"] == "gitlab_auth"


@pytest.mark.parametrize("code,classification", [(500, "gitlab_http"), (401, "gitlab_auth"),
                                                 (429, "gitlab_rate_limited")])
def test_project_error_continues_only_for_nonfatal_failures(world, monkeypatch, code, classification):
    monkeypatch.setenv("ARGUS_GITLAB_ALLOW", f"{KEY},{OTHER}")
    assert tick(world) == 2
    monkeypatch.setenv("ARGUS_WATCH_DISCOVERY_TICKS", "100")
    world.rows[0]["sha"] = "b" * 40
    world.responses["/api/v4/projects/42"] = code
    assert tick(world) == (1 if code == 500 else 0)
    assert cursor(world)["last_error"] == classification
    assert rows(world, OTHER)[-1]["head_sha"] == ("b" * 40 if code == 500 else "a" * 40)


def test_auth_failure_ends_pass_before_trust_sweep(world, monkeypatch):
    world.responses["/api/v4/projects/42/members/all/7"] = 500
    assert tick(world) == 0
    world.responses.clear()
    monkeypatch.setattr(listing, "list_changes", Mock(side_effect=GitLabAuthError(TOKEN)))
    assert tick(world) == 0
    assert rows(world)[0]["skip_reason"] == "trust_unknown"
    assert cursor(world)["last_error"] == "gitlab_auth"


def test_watch_status_never_exposes_unclassified_stored_error(world):
    with watch_store.connection(world.path) as conn:
        db.set_gitlab_poll_cursor(conn, KEY, last_error="private server detail " + TOKEN,
                                  last_polled_at=NOW.isoformat())
    assert status.integration_status()["watch"]["lastError"] == "gitlab_http"


def test_fork_lookup_recovers_during_policy_recheck(world, monkeypatch):
    monkeypatch.setenv("ARGUS_GITLAB_ALLOW", f"{KEY},{gitlab_key(HOST, 99)}")
    monkeypatch.setattr(listing, "expand_allow", lambda **kwargs: (KEY,))
    world.rows = [mr(source_project_id=99)]
    replies = iter([httpx.Response(500), httpx.Response(500), httpx.Response(200, json={
        "id": 99, "path_with_namespace": "allowed/fork", "archived": False,
        "default_branch": "main", "visibility": "private",
    })])
    world.responses["/api/v4/projects/99"] = lambda request: next(replies)
    assert tick(world) == 1
    assert (rows(world)[0]["status"], rows(world)[0]["skip_reason"]) == ("queued", None)


def test_trust_sweep_checks_at_most_five_heads_per_tick(world):
    world.rows = [mr(i, author={"id": i, "username": f"human-{i}", "bot": False})
                  for i in range(1, 7)]
    world.responses = {f"/api/v4/projects/42/members/all/{i}": 500 for i in range(1, 7)}
    assert tick(world) == 0
    assert watch_store.unknown_trust_count(world.path) == 6
    world.rows = []
    world.responses.clear()
    world.requests.clear()
    assert tick(world) == 5
    assert len([r for r in world.requests if "/members/all/" in r.url.path]) == 5
    assert watch_store.unknown_trust_count(world.path) == 1
    assert tick(world) == 1
    assert tick(world) == 0


def test_project_reentry_after_allow_removal_uses_full_listing(world, monkeypatch):
    assert tick(world) == 1
    monkeypatch.setenv("ARGUS_GITLAB_ALLOW", "")
    assert tick(world) == 0
    assert (rows(world)[0]["status"], rows(world)[0]["is_open"]) == ("closed", 0)
    monkeypatch.setenv("ARGUS_GITLAB_ALLOW", KEY)
    assert tick(world) == 1
    assert listings(world)[-1].url.params["state"] == "opened"
    assert (rows(world)[0]["status"], rows(world)[0]["is_open"]) == ("queued", 1)


@pytest.mark.parametrize("existing", [False, True])
def test_watch_status_missing_or_unreadable_database_defaults(world, monkeypatch, tmp_path, existing):
    path = tmp_path / "unavailable.db"
    if existing:
        path.write_text("not a database")
    monkeypatch.setenv("ARGUS_DB_PATH", str(path))
    monkeypatch.setenv("ARGUS_GITLAB_WATCH", "invalid")
    assert status.integration_status()["watch"] == {
        "enabled": False, "projectCount": 0, "lastPolledAt": None,
        "lastError": None, "trustUnknown": 0, "forkSourceDenied": 0,
    }
    assert path.exists() == existing


def test_watch_status_uses_latest_project_error_then_pass_error(world):
    with watch_store.connection(world.path) as conn:
        db.set_gitlab_poll_cursor(conn, KEY, last_polled_at="2026-11-04T10:00:00+00:00",
                                  last_error="gitlab_auth")
        db.set_gitlab_poll_cursor(conn, OTHER, last_polled_at="2026-11-04T11:00:00+00:00",
                                  last_error="gitlab_denied")
    watch = status.integration_status()["watch"]
    assert (watch["projectCount"], watch["lastPolledAt"], watch["lastError"]) == (
        2, "2026-11-04T11:00:00+00:00", "gitlab_denied")


def test_discovery_server_failure_preserves_cached_projects(world):
    assert tick(world) == 1
    before = cursor(world)["updated_after"]
    world.responses["/api/v4/projects/42"] = 500
    assert tick(world) == 0
    assert (rows(world)[0]["status"], rows(world)[0]["is_open"]) == ("queued", 1)
    assert watcher.gitlab_pass_error(world.path) == "gitlab_http"
    assert cursor(world)["updated_after"] == before
    assert cursor(world)["last_error"] == "gitlab_http"


def test_error_persistence_failure_preserves_pending_full_retries(world, monkeypatch):
    monkeypatch.setenv("ARGUS_GITLAB_ALLOW", f"{KEY},{OTHER}")
    assert tick(world) == 2
    watcher._previous_watched_repos.clear()
    with watch_store.connection(world.path) as conn:
        conn.execute("UPDATE pr_heads SET is_open=0")
    with monkeypatch.context() as patch:
        patch.setattr(listing, "full_listing", Mock(side_effect=GitLabHTTPError("unavailable")))
        patch.setattr(watcher, "_gitlab_project_error", Mock(side_effect=RuntimeError("database locked")))
        assert tick(world) == 0
    world.requests.clear()
    assert tick(world) == 0
    assert [r.url.params["state"] for r in listings(world)] == ["opened", "opened"]
    assert all(r["is_open"] == 1 for r in watch_store.heads(world.path))
