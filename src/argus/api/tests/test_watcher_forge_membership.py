"""Watcher membership and quota pauses stay isolated by provider."""

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from argus import github
from argus.api import db, main, watch_store, watcher
from argus.forge.types import ChangeMeta, RepoRecord, RepoRef
from argus.gitlab import client as gitlab_client
from argus.gitlab import projects
from argus.gitlab.client import GitLabRateLimited
from argus.repo_policy import GitLabDecision
from argus.review import ReviewResult

NOW = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)
GH = "example/app"
GL = "gitlab:gitlab.example#42"
ISO_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:\+00:00|Z)"
)


def unexpected(*args, **kwargs):
    pytest.fail("Unexpected network, client or review call")


@pytest.fixture
def path(tmp_path, monkeypatch):
    path = str(tmp_path / "argus.db")
    monkeypatch.setenv("ARGUS_DB_PATH", path)
    monkeypatch.setenv("ARGUS_WATCH_OWNERS", "example")
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "acme-labs")
    monkeypatch.setenv("ARGUS_WATCH_TRUSTED_ONLY", "1")
    monkeypatch.setenv("ARGUS_PUBLISH", "0")
    monkeypatch.setenv("ARGUS_WATCH_DISCOVERY_TICKS", "1")
    monkeypatch.setenv("ARGUS_OUTCOME_INTERVAL_TICKS", "100")
    monkeypatch.setenv("ARGUS_WATCH_FAST_MERGES", "1")
    for name in ("ARGUS_GITLAB_WATCH", "ARGUS_GITLAB_URL", "ARGUS_FALLBACK_MODEL"):
        monkeypatch.delenv(name, raising=False)
    for name in ("_ticks", "_last_tick", "_repo_pushed_at", "_previous_watched_repos"):
        monkeypatch.setattr(watcher, name, {})
    monkeypatch.setattr(watcher, "_active_heads", set())
    monkeypatch.setattr(watcher, "_gitlab_expanded", {}, raising=False)
    monkeypatch.setattr(watcher, "_pause_logged", 0)
    monkeypatch.setattr(watcher, "_gitlab_pause_logged", 0, raising=False)
    monkeypatch.setattr(watch_store, "_listed_repos", set())
    monkeypatch.setattr(watch_store, "utcnow", lambda: NOW)
    monkeypatch.setattr(github, "_quota", {})
    monkeypatch.setattr(github, "_pause_until", 0)
    monkeypatch.setattr(github, "owned_repo_records", lambda: [repo()])
    monkeypatch.setattr(watcher, "_open_prs", lambda name: [pr()])
    monkeypatch.setattr(watcher, "_recently_closed_prs", unexpected)
    monkeypatch.setattr(gitlab_client, "client", unexpected)
    monkeypatch.setattr(main, "run_review", unexpected)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", unexpected)
    db.init_db(path)
    db.save_config(
        path,
        repos=[{"name": GH, "watched": True}],
        model="test-model",
        bug_cap=5,
        min_confidence=0.5,
        owned_repos=[repo()],
    )
    return path


def repo():
    return {"full_name": GH, "pushed_at": NOW.isoformat()}


def pr():
    return {
        "number": 1,
        "head": {"sha": "abc"},
        "author_association": "OWNER",
        "user": {"login": "human", "type": "User"},
    }


def normalise(value):
    return ISO_TIMESTAMP.sub("<timestamp>", value)


def queue_gitlab(path):
    change = {k: v for k, v in pr().items() if k != "author_association"}
    change.update(author_trusted=1, author_id=23)
    assert watch_store.record_changes(path, GL, [change], [], True) == 1


def future_reset():
    return max(time.time(), NOW.timestamp()) + 300


@pytest.fixture
def reviews(monkeypatch):
    calls = []

    def review(repo, number, model, bug_cap, **kwargs):
        calls.append((repo, number, kwargs["expected_head_sha"]))
        return ReviewResult(raw_count=0)

    monkeypatch.setattr(main, "run_review", review)
    return calls


@pytest.fixture
def gitlab(monkeypatch):
    monkeypatch.setenv("ARGUS_GITLAB_WATCH", "1")
    monkeypatch.setenv("ARGUS_GITLAB_URL", "https://gitlab.example")
    state = SimpleNamespace(
        reset=0,
        decision=GitLabDecision(True, "allowed"),
        calls=[],
        meta_error=None,
        deny_error=None,
    )
    meta = ChangeMeta(
        "abc",
        "base",
        "open",
        False,
        False,
        "23",
        "human",
        False,
        "Change",
        NOW.isoformat(),
        None,
    )

    def change_meta(repo, number):
        assert not watcher._watched_repos_lock.locked()
        assert not watcher._active_lock.locked()
        state.calls.append(("change_meta", repo, number))
        if state.meta_error:
            raise state.meta_error
        return meta

    def deny(repo, change, *, db_path=None):
        assert not watcher._watched_repos_lock.locked()
        assert not watcher._active_lock.locked()
        assert change is meta
        state.calls.append(("live_deny_change", repo))
        if state.deny_error:
            raise state.deny_error
        if state.decision.reason == "lookup failed: rate_limited":
            state.reset = future_reset()
        return state.decision

    def forge(repo):
        assert repo == GL
        return SimpleNamespace(change_meta=change_meta)

    monkeypatch.setattr(
        gitlab_client,
        "client",
        lambda: SimpleNamespace(pause_until=lambda: state.reset),
    )
    monkeypatch.setattr(watcher, "forge_for", forge, raising=False)
    monkeypatch.setattr(projects, "live_deny_change", deny)
    return state


def test_github_poll_effects_unchanged_with_gitlab_off(path, monkeypatch):
    calls, writes = [], []

    def owned():
        calls.append(("owned_repo_records", ()))
        return [repo()]

    def opened(name):
        calls.append(("_open_prs", (name,)))
        return [pr()]

    def closed(name):
        calls.append(("_recently_closed_prs", (name,)))
        return []

    def trace(statement):
        if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(normalise(" ".join(statement.split())))

    connect = db.connect

    def traced_connect(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(trace)
        return conn

    monkeypatch.setattr(github, "owned_repo_records", owned)
    monkeypatch.setattr(watcher, "_open_prs", opened)
    monkeypatch.setattr(watcher, "_recently_closed_prs", closed)
    monkeypatch.setattr(db, "connect", traced_connect)
    assert asyncio.run(watcher.poll_once(path)) == 1
    rows = json.loads(normalise(json.dumps(watch_store.heads(path))))
    assert calls == [("owned_repo_records", ()), ("_open_prs", ("example/app",))]
    expected_writes = [
        "INSERT OR IGNORE INTO coverage_reports VALUES('2026-11-03','<timestamp>','{\"day\": "
        '"2026-11-03", "start": "<timestamp>", "end": "<timestamp>", "as_of": "<timestamp>", '
        '"pending_heads": 0, "baseline_heads": 0, "eligible_heads": 0, "done_heads": 0, "coverage_share": '
        'null, "push_to_review_seconds": {"median": null, "p90": null, "within_15_minutes": 0, '
        '"share_within_15_minutes": null}, "commit_to_review_seconds": {"median": null, "p90": null, '
        '"within_15_minutes": 0, "share_within_15_minutes": null, "available_heads": 0}, '
        '"excluded_ended_within_15_minutes": 0, "strict": {"eligible_heads": 0, "done_heads": 0, '
        '"coverage_share": null, "share_within_15_minutes": null}, "fast_merged": {"seen": 0, "done": 0, '
        '"done_within_15_minutes_of_merge": 0}, "finished_attempts": 0, "errored_attempts": 0, '
        '"error_share": null, "errors_by_class": {}, "superseded": 0, "skipped": 0}\')',
        "INSERT INTO config (id,repo_allowlist,model,bug_cap,min_confidence,watch_mode,deny,owned_repos) "
        'VALUES(1,\'[{"name": "example/app", "watched": '
        "true}]','test-model',5,0.5,'auto','[]','[{\"full_name\": \"example/app\", \"pushed_at\": "
        '"<timestamp>"}]\') ON CONFLICT(id) DO UPDATE SET repo_allowlist=excluded.repo_allowlist, '
        "owned_repos=excluded.owned_repos",
        "UPDATE pr_heads SET is_open=0 WHERE repo NOT IN ('example/app')",
        "UPDATE pr_heads SET status='closed', ended_at='<timestamp>' WHERE repo NOT IN ('example/app') "
        "AND status='queued'",
        "UPDATE pr_heads SET is_open=0 WHERE repo='example/app'",
        "UPDATE pr_heads SET status='superseded', ended_at='<timestamp>' WHERE repo='example/app' AND "
        "pr=1 AND head_sha<>'abc' AND status='queued'",
        "INSERT INTO pr_heads "
        "(repo,pr,head_sha,first_seen_at,pr_updated_at,draft,author,author_type,author_association,base_ref, "
        "status,skip_reason,last_listed_at,review_id,completed_at,head_committed_at, "
        "eligible_at,eligible_pr_updated_at,baseline) VALUES "
        "('example/app',1,'abc','<timestamp>',NULL,0,'human','User','OWNER',NULL,'queued',NULL,'<timestamp>',NULL,NULL,NULL,'<timestamp>',NULL,0) "
        "ON CONFLICT(repo,pr,head_sha) DO UPDATE SET draft=excluded.draft, author=excluded.author, "
        "author_type=excluded.author_type, author_association=excluded.author_association, "
        "base_ref=excluded.base_ref, status=excluded.status, skip_reason=excluded.skip_reason, "
        "last_listed_at=excluded.last_listed_at, is_open=1, ended_at=NULL, eligible_pr_updated_at=CASE "
        "WHEN pr_heads.eligible_at IS NULL THEN excluded.eligible_pr_updated_at ELSE "
        "pr_heads.eligible_pr_updated_at END, "
        "eligible_at=COALESCE(pr_heads.eligible_at,excluded.eligible_at), "
        "review_id=COALESCE(excluded.review_id,pr_heads.review_id), "
        "completed_at=COALESCE(excluded.completed_at,pr_heads.completed_at), "
        "head_committed_at=COALESCE(excluded.head_committed_at,pr_heads.head_committed_at)",
        "UPDATE pr_heads SET status='closed', ended_at='<timestamp>' WHERE repo='example/app' AND "
        "is_open=0 AND status='queued' AND merged_before_seen=0",
    ]
    # These two provider guards are the only changes to the captured SQL baseline.
    expected_writes[2] += " AND instr(repo, ':') = 0"
    expected_writes[3] += " AND instr(repo, ':') = 0"
    assert writes == expected_writes
    assert rows == [
        {
            "attempts": 0,
            "author": "human",
            "author_association": "OWNER",
            "author_id": None,
            "author_trusted": None,
            "author_type": "User",
            "base_ref": None,
            "baseline": 0,
            "claimed_at": None,
            "completed_at": None,
            "draft": 0,
            "eligible_at": "<timestamp>",
            "eligible_pr_updated_at": None,
            "ended_at": None,
            "first_seen_at": "<timestamp>",
            "head_committed_at": None,
            "head_sha": "abc",
            "id": 1,
            "is_open": 1,
            "last_error_class": None,
            "last_listed_at": "<timestamp>",
            "merged_at": None,
            "merged_before_seen": 0,
            "next_attempt_at": None,
            "pr": 1,
            "pr_updated_at": None,
            "repo": "example/app",
            "review_id": None,
            "skip_reason": None,
            "status": "queued",
        }
    ]


def test_queued_gitlab_head_survives_github_tick_and_is_reviewed(
    path, monkeypatch, reviews
):
    queue_gitlab(path)
    assert asyncio.run(watcher.poll_once(path)) == 1
    row = watch_store.heads(path)[0]
    assert (row["repo"], row["status"], row["is_open"]) == (GL, "queued", 1)
    watcher.set_gitlab_expanded(path, {GL})
    monkeypatch.setenv("ARGUS_GITLAB_WATCH", "1")
    monkeypatch.setenv("ARGUS_GITLAB_URL", "https://gitlab.example")
    monkeypatch.setattr(
        gitlab_client, "client", lambda: SimpleNamespace(pause_until=lambda: 0)
    )
    meta = ChangeMeta(
        "abc",
        "base",
        "open",
        False,
        False,
        "23",
        "human",
        False,
        "Change",
        NOW.isoformat(),
        None,
    )
    monkeypatch.setattr(
        watcher,
        "forge_for",
        lambda repo: SimpleNamespace(change_meta=lambda *args: meta),
    )
    monkeypatch.setattr(
        projects, "live_deny_change", lambda *args, **kwargs: GitLabDecision(True, "allowed")
    )
    assert watcher.process_one(path)
    assert reviews == [(GL, 1, "abc")]
    assert watch_store.heads(path)[0]["status"] == "done"


def test_github_pause_does_not_stop_gitlab_claims(path, gitlab, monkeypatch, reviews):
    watch_store.record_listing(path, GH, [pr()])
    queue_gitlab(path)
    monkeypatch.setattr(github, "_pause_until", future_reset())
    assert watcher.process_one(path)
    assert reviews == [(GL, 1, "abc")]
    assert [(r["repo"], r["status"]) for r in watch_store.heads(path)] == [
        (GH, "queued"),
        (GL, "done"),
    ]


def test_gitlab_pause_does_not_stop_github_polling_or_claims(path, gitlab, reviews):
    queue_gitlab(path)
    gitlab.reset = future_reset()
    assert asyncio.run(watcher.poll_once(path)) == 1
    assert watcher.process_one(path)
    assert reviews == [(GH, 1, "abc")]
    assert [(r["repo"], r["status"]) for r in watch_store.heads(path)] == [
        (GL, "queued"),
        (GH, "done"),
    ]
    assert gitlab.calls == []


@pytest.mark.parametrize("repos", [[], ["gitlab:gitlab.example#99"]])
def test_gitlab_restrict_closes_only_gitlab_rows(path, repos):
    watch_store.record_listing(path, GH, [pr()])
    queue_gitlab(path)
    watch_store.restrict_repos(path, repos, NOW, forge="gitlab")
    gh, gl = watch_store.heads(path)
    assert (gh["status"], gh["is_open"]) == ("queued", 1)
    assert (gl["status"], gl["is_open"], gl["ended_at"]) == (
        "closed",
        0,
        NOW.isoformat(),
    )


@pytest.mark.parametrize("repos", [[], ["example/other"]])
def test_github_restrict_leaves_gitlab_rows(path, repos):
    watch_store.record_listing(path, GH, [pr()])
    queue_gitlab(path)
    watch_store.restrict_repos(path, repos, NOW, forge="github")
    gh, gl = watch_store.heads(path)
    assert (gh["status"], gh["is_open"]) == ("closed", 0)
    assert (gl["status"], gl["is_open"]) == ("queued", 1)


def test_restrict_rejects_unknown_forge_without_writes(path):
    queue_gitlab(path)
    before = watch_store.heads(path)
    with pytest.raises(ValueError):
        watch_store.restrict_repos(path, [], forge="other")
    assert watch_store.heads(path) == before


@pytest.mark.parametrize(
    "reason",
    [
        "hard deny owner",
        "lookup failed: forbidden",
        "lookup failed: not_found",
        "lookup failed: redirect_refused",
        "lookup failed: guard",
        "lookup failed: invalid_project",
        "lookup failed: policy_invalid",
    ],
)
def test_denied_gitlab_head_closed_at_claim_with_reason(path, gitlab, reason):
    queue_gitlab(path)
    gitlab.decision = GitLabDecision(False, reason)
    assert watcher.process_one(path)
    row = watch_store.heads(path)[0]
    assert (row["status"], row["skip_reason"], row["is_open"]) == ("closed", reason, 0)
    assert row["attempts"] == 0
    assert db.list_reviews(path) == []


@pytest.fixture
def live_gitlab(path, monkeypatch):
    monkeypatch.setenv("ARGUS_GITLAB_WATCH", "1")
    monkeypatch.setenv("ARGUS_GITLAB_URL", "https://gitlab.example")
    monkeypatch.setenv("ARGUS_GITLAB_ALLOW", "gitlab:gitlab.example/example")
    state = SimpleNamespace(source=None, source_limited=False, reset=0)

    def change_meta(repo, number):
        return ChangeMeta(
            "abc", "base", "open", False, False, "23", "human", False,
            "Change", NOW.isoformat(), state.source,
        )

    def resolve(ref, **kwargs):
        assert not watcher._watched_repos_lock.locked()
        assert not watcher._active_lock.locked()
        if ref.project_id == 99 and state.source_limited:
            state.reset = future_reset()
            raise GitLabRateLimited(state.reset)
        return RepoRecord(f"example/project-{ref.project_id}", False, "main")

    monkeypatch.setattr(
        gitlab_client, "client", lambda: SimpleNamespace(pause_until=lambda: state.reset)
    )
    monkeypatch.setattr(
        watcher, "forge_for", lambda repo: SimpleNamespace(change_meta=change_meta)
    )
    monkeypatch.setattr(projects, "resolve", resolve)
    return state


def test_gitlab_deny_uses_worker_database(path, live_gitlab, tmp_path, reviews):
    other_path = str(tmp_path / "other.db")
    db.init_db(other_path)
    db.save_config(
        other_path, repos=[], model="test-model", bug_cap=5, min_confidence=0.5,
        deny=[GL],
    )
    assert db.get_config(path)["deny"] == []
    queue_gitlab(other_path)
    assert watcher.process_one(other_path)
    row = watch_store.heads(other_path)[0]
    assert (row["status"], row["skip_reason"], row["is_open"]) == (
        "closed", "deny entry", 0,
    )
    assert reviews == []


def test_gitlab_rate_limited_fork_source_requeues(path, live_gitlab):
    live_gitlab.source = RepoRef("gitlab", "gitlab.example", None, 99)
    live_gitlab.source_limited = True
    queue_gitlab(path)
    assert watcher.process_one(path)
    row = watch_store.heads(path)[0]
    assert (row["status"], row["is_open"], row["skip_reason"]) == ("queued", 1, None)
    assert row["next_attempt_at"] == datetime.fromtimestamp(
        live_gitlab.reset, timezone.utc
    ).isoformat()
    assert row["attempts"] == 0
    assert row["last_error_class"] == "gitlab_rate_limited"
    assert db.list_reviews(path) == []


@pytest.mark.parametrize("project_id", [99, 0])
def test_gitlab_denied_fork_source_closes(path, live_gitlab, project_id):
    live_gitlab.source = RepoRef("gitlab", "gitlab.example", None, project_id)
    db.save_config(
        path, repos=[], model="test-model", bug_cap=5, min_confidence=0.5,
        deny=["gitlab:gitlab.example#99"],
    )
    queue_gitlab(path)
    assert watcher.process_one(path)
    row = watch_store.heads(path)[0]
    assert (row["status"], row["skip_reason"], row["is_open"]) == (
        "closed", "fork_source_denied", 0,
    )
    assert row["next_attempt_at"] is None
    assert row["attempts"] == 0
    assert db.list_reviews(path) == []


def test_gitlab_forbidden_fork_source_recheck_keeps_deny_reason(path, live_gitlab, monkeypatch):
    live_gitlab.source = RepoRef("gitlab", "gitlab.example", None, 99)
    monkeypatch.setattr(
        projects, "live_deny_change",
        lambda *args, **kwargs: GitLabDecision(False, "fork_source_denied"),
    )
    monkeypatch.setattr(
        projects, "live_deny",
        lambda *args, **kwargs: GitLabDecision(False, "lookup failed: forbidden"),
    )
    queue_gitlab(path)
    assert watcher.process_one(path)
    row = watch_store.heads(path)[0]
    assert (row["status"], row["skip_reason"], row["is_open"]) == (
        "closed", "fork_source_denied", 0,
    )
    assert row["next_attempt_at"] is None
    assert row["attempts"] == 0
    assert db.list_reviews(path) == []


def test_gitlab_fork_source_allowed_on_recheck_requeues(path, live_gitlab, monkeypatch):
    live_gitlab.source = RepoRef("gitlab", "gitlab.example", None, 99)
    monkeypatch.setattr(
        projects, "live_deny_change",
        lambda *args, **kwargs: GitLabDecision(False, "fork_source_denied"),
    )
    monkeypatch.setattr(
        projects, "live_deny", lambda *args, **kwargs: GitLabDecision(True, "allowed")
    )
    queue_gitlab(path)
    assert watcher.process_one(path)
    row = watch_store.heads(path)[0]
    assert (row["status"], row["is_open"], row["skip_reason"]) == ("queued", 1, None)
    assert row["next_attempt_at"] is not None
    assert datetime.fromisoformat(row["next_attempt_at"]) > NOW
    assert row["attempts"] == 1
    assert row["ended_at"] is None
    assert db.list_reviews(path) == []


@pytest.mark.parametrize("reason", ["rate_limited", "transport", "auth", "failed"])
def test_transient_gitlab_lookup_failure_requeues(path, gitlab, reason):
    queue_gitlab(path)
    gitlab.decision = GitLabDecision(False, "lookup failed: " + reason)
    assert watcher.process_one(path)
    row = watch_store.heads(path)[0]
    assert (row["status"], row["is_open"], row["skip_reason"]) == ("queued", 1, None)
    assert row["next_attempt_at"] is not None
    if reason == "rate_limited":
        assert (
            row["next_attempt_at"]
            == datetime.fromtimestamp(gitlab.reset, timezone.utc).isoformat()
        )
        assert row["attempts"] == 0
        assert row["last_error_class"] == "gitlab_rate_limited"
    else:
        assert row["attempts"] == 1
    assert db.list_reviews(path) == []


def test_rate_limited_lookup_without_future_reset_waits(path, gitlab, monkeypatch):
    queue_gitlab(path)
    gitlab.decision = GitLabDecision(False, "lookup failed: rate_limited")
    monkeypatch.setattr(gitlab_client, "client", lambda: SimpleNamespace(pause_until=lambda: 0.0))
    before = time.time()
    assert watcher.process_one(path)
    row = watch_store.heads(path)[0]
    assert row["status"] == "queued"
    assert row["attempts"] == 0
    assert datetime.fromisoformat(row["next_attempt_at"]).timestamp() > before


def test_rate_limit_error_with_expired_reset_waits(path, gitlab):
    queue_gitlab(path)
    gitlab.meta_error = GitLabRateLimited(0.0)
    before = time.time()
    assert watcher.process_one(path)
    row = watch_store.heads(path)[0]
    assert (row["status"], row["attempts"]) == ("queued", 0)
    assert datetime.fromisoformat(row["next_attempt_at"]).timestamp() > before


@pytest.mark.parametrize("boundary", ["meta_error", "deny_error"])
@pytest.mark.parametrize("limited", [False, True])
def test_gitlab_lookup_exception_requeues(path, gitlab, boundary, limited):
    queue_gitlab(path)
    error = (
        GitLabRateLimited(future_reset())
        if limited
        else RuntimeError("lookup unavailable")
    )
    setattr(gitlab, boundary, error)
    assert watcher.process_one(path)
    row = watch_store.heads(path)[0]
    assert row["status"] == "queued"
    assert row["next_attempt_at"] is not None
    assert row["attempts"] == (0 if limited else 1)
    if limited:
        assert (
            row["next_attempt_at"]
            == datetime.fromtimestamp(error.reset_at, timezone.utc).isoformat()
        )
    assert not watcher._active_heads


def test_gitlab_head_outside_cached_expanded_set_is_closed(path, gitlab):
    queue_gitlab(path)
    watcher.set_gitlab_expanded(Path(path).parent / "." / "argus.db", set())
    assert watcher.process_one(path)
    row = watch_store.heads(path)[0]
    assert (row["status"], row["skip_reason"]) == (
        "closed",
        "not in watched gitlab set",
    )
    assert gitlab.calls == []


def test_gitlab_head_without_cache_goes_to_live_check(path, gitlab, reviews):
    queue_gitlab(path)
    assert watcher.process_one(path)
    assert gitlab.calls == [("change_meta", GL, 1), ("live_deny_change", GL)]
    assert reviews == [(GL, 1, "abc")]
    assert watch_store.heads(path)[0]["status"] == "done"


@pytest.mark.parametrize("cached", [False, True])
def test_gitlab_set_change_does_not_mark_github_repos_dropped(path, gitlab, cached):
    queue_gitlab(path)
    key = str(Path(path).resolve())
    watcher._previous_watched_repos[key] = {GH, GL}
    watcher._previous_watched_repos[(key, "gitlab")] = {GL}
    if cached:
        watcher.set_gitlab_expanded(path, set())
    else:
        gitlab.decision = GitLabDecision(False, "hard deny owner")
    assert watcher.process_one(path)
    assert watcher._previous_watched_repos[key] == {GH, GL}
    assert watcher._previous_watched_repos[(key, "gitlab")] == set()
    assert asyncio.run(watcher.poll_once(path)) == 1
    assert watcher._previous_watched_repos[key] == {GH}
    assert watcher._previous_watched_repos[(key, "gitlab")] == set()


def test_denied_owner_stays_denied_on_both_forges(path, gitlab):
    db.save_config(
        path,
        repos=[{"name": "acme-labs/app", "watched": True}],
        model="test-model",
        bug_cap=5,
        min_confidence=0.5,
    )
    watch_store.record_listing(path, "acme-labs/app", [pr()])
    queue_gitlab(path)
    gitlab.decision = GitLabDecision(False, "hard deny owner")
    assert watcher.process_one(path)
    assert watcher.process_one(path)
    gh, gl = watch_store.heads(path)
    assert gh["status"] == "closed"
    assert (gl["status"], gl["skip_reason"]) == ("closed", "hard deny owner")
    assert db.list_reviews(path) == []


@pytest.mark.parametrize(
    ("watch", "url"), [("", ""), ("0", "https://gitlab.example"), ("1", "")]
)
@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("gitlab_queued", [False, True])
def test_gitlab_off_process_one_makes_no_gitlab_call(
    path, monkeypatch, reviews, watch, url, paused, gitlab_queued
):
    if gitlab_queued:
        queue_gitlab(path)
    watch_store.record_listing(path, GH, [pr()])
    monkeypatch.setenv("ARGUS_GITLAB_WATCH", watch)
    monkeypatch.setenv("ARGUS_GITLAB_URL", url)
    calls = []

    def quota():
        calls.append(1)
        return paused

    monkeypatch.setattr(watcher, "quota_paused", quota)
    assert watcher.process_one(path) is not paused
    assert calls == [1]
    assert reviews == ([] if paused else [(GH, 1, "abc")])
    if gitlab_queued:
        assert watch_store.heads(path)[0]["status"] == "queued"


@pytest.mark.parametrize(
    "failure", ["invalid_config", "missing_client", "client_error", "both_paused"]
)
def test_github_pause_stays_blocked_without_available_gitlab(
    path, gitlab, monkeypatch, failure
):
    queue_gitlab(path)
    monkeypatch.setattr(github, "_pause_until", future_reset())
    if failure == "invalid_config":
        monkeypatch.setenv("ARGUS_GITLAB_WATCH", "invalid")
    elif failure == "missing_client":
        monkeypatch.setattr(gitlab_client, "client", lambda: None)
    elif failure == "client_error":

        def failed_client():
            raise RuntimeError("client unavailable")

        monkeypatch.setattr(gitlab_client, "client", failed_client)
    else:
        gitlab.reset = future_reset()
    assert not watcher.process_one(path)
    assert watch_store.heads(path)[0]["status"] == "queued"


def test_quota_pauses_log_independently(path, gitlab, monkeypatch, caplog):
    reset = future_reset()
    monkeypatch.setattr(github, "_pause_until", reset)
    gitlab.reset = reset
    with caplog.at_level(logging.INFO, logger="argus.watcher"):
        assert watcher.quota_paused()
        assert watcher.quota_paused("gitlab")
        assert watcher.quota_paused()
        assert watcher.quota_paused("gitlab")
    messages = [
        r.getMessage() for r in caplog.records if "quota pause" in r.getMessage()
    ]
    assert messages == [
        f"watcher: quota pause forge=github remaining=? reset={reset}",
        f"watcher: quota pause forge=gitlab reset={reset}",
    ]


@pytest.mark.parametrize("failure", ["missing_client", "client_error", "pause_error"])
def test_gitlab_quota_lookup_failure_is_not_paused(path, monkeypatch, failure):
    def failed():
        raise RuntimeError("client unavailable")

    if failure == "missing_client":
        monkeypatch.setattr(gitlab_client, "client", lambda: None)
    elif failure == "client_error":
        monkeypatch.setattr(gitlab_client, "client", failed)
    else:
        monkeypatch.setattr(
            gitlab_client, "client", lambda: SimpleNamespace(pause_until=failed)
        )
    assert not watcher.quota_paused("gitlab")


def test_close_claim_without_reason_keeps_existing_skip_reason(path):
    queue_gitlab(path)
    head = watch_store.claim(path, pauses={})
    with watch_store.connection(path) as conn:
        conn.execute(
            "UPDATE pr_heads SET skip_reason=? WHERE id=?", ("existing reason", head["id"])
        )
    watch_store.close_claim(path, head, NOW)
    row = watch_store.heads(path)[0]
    assert (row["status"], row["skip_reason"]) == ("closed", "existing reason")


def test_close_claim_reason_cannot_close_newer_claim(path):
    queue_gitlab(path)
    head = watch_store.claim(path, pauses={})
    with watch_store.connection(path) as conn:
        conn.execute(
            "UPDATE pr_heads SET claimed_at=? WHERE id=?", ("new-claim", head["id"])
        )
    watch_store.close_claim(path, head, NOW, reason="hard deny owner")
    row = watch_store.heads(path)[0]
    assert (row["status"], row["skip_reason"]) == ("running", None)
