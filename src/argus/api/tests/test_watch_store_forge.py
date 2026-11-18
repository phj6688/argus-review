"""Incremental discovery preserves queue state and isolates forge recovery."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from argus import github
from argus.api import db, watch_store
from argus.forge import keys
from argus.forge.errors import ForgeRateLimited
from argus.github import GitHubRateLimited
from argus.gitlab import client as gitlab_client
from argus.gitlab.client import GitLabRateLimited

NOW = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
GL = "gitlab:gitlab.example#42"
GH = "example/app"


@pytest.fixture
def path(tmp_path, monkeypatch):
    path = str(tmp_path / "argus.db")
    monkeypatch.setenv("ARGUS_DB_PATH", path)
    monkeypatch.setenv("ARGUS_WATCH_TRUSTED_ONLY", "1")
    monkeypatch.delenv("ARGUS_WATCH_INCLUDE_BOTS", raising=False)
    monkeypatch.delenv("ARGUS_WATCH_INCLUDE_DRAFTS", raising=False)
    monkeypatch.delenv("ARGUS_GITLAB_URL", raising=False)
    monkeypatch.setattr(github, "_pause_until", 0)
    monkeypatch.setattr(github, "_quota", {})
    monkeypatch.setattr(watch_store, "_listed_repos", set())
    db.init_db(path)
    return path


def mr(number=1, sha="a" * 40, **extra):
    return {
        "number": number, "head": {"sha": sha}, "updated_at": NOW.isoformat(),
        "draft": False, "user": {"login": "human", "type": "User"},
        "base": {"ref": "main"}, "author_trusted": 1, "author_id": 23,
        "merged_at": None, **extra,
    }


def gh_pr(**extra):
    pr = mr(**{"author_association": "OWNER", **extra})
    for field in ("author_trusted", "author_id", "merged_at"):
        if field not in extra:
            pr.pop(field)
    return pr


def comparable(row):
    return {k: v for k, v in row.items() if k not in {"id", "repo"}}


@pytest.mark.parametrize("column", ["repo", "h.repo"])
def test_gitlab_key_sql_matches_python_predicate(path, column):
    with watch_store.connection(path) as conn:
        for key, expected in [
            (GL, True), ("gitlab:", True), ("gitlab:invalid", True),
            (GH, False), ("GITLAB:example#42", False), ("other:example#42", False),
            ("xgitlab:example#42", False), ("", False),
        ]:
            row = conn.execute(
                f"SELECT {keys.gitlab_key_sql(column)} FROM (SELECT ? AS repo) AS h", (key,),  # nosec B608 - validated column name
            ).fetchone()
            assert bool(row[0]) == expected == keys.is_gitlab_key(key)


@pytest.mark.parametrize("column", ["", "repo; DROP TABLE pr_heads", "h.repo.extra", "repo --", "repo\n"])
def test_gitlab_key_sql_rejects_invalid_columns(column):
    with pytest.raises(ValueError, match="invalid sql column name"):
        keys.gitlab_key_sql(column)


def test_incremental_listing_leaves_other_open_mrs_queued(path):
    assert watch_store.record_changes(path, GL, [mr(), mr(2, "b" * 40)], [], True, NOW) == 2
    assert watch_store.record_changes(path, GL, [mr(sha="c" * 40)], [], False, NOW) == 1
    rows = watch_store.heads(path)
    assert [(r["pr"], r["status"]) for r in rows] == [
        (1, "superseded"), (2, "queued"), (1, "queued"),
    ]
    assert rows[1]["is_open"] == 1
    assert all(r["status"] != "closed" for r in rows)


def test_push_without_updated_at_change_supersedes_old_head(path):
    watch_store.record_changes(path, GL, [mr()], [], False, NOW)
    assert watch_store.record_changes(path, GL, [mr(sha="b" * 40)], [], False, NOW) == 1
    old, new = watch_store.heads(path)
    assert old["status"] == "superseded"
    assert (new["head_sha"], new["status"], new["pr_updated_at"]) == (
        "b" * 40, "queued", NOW.isoformat(),
    )


@pytest.mark.parametrize("state", ["backoff", "running", "done"])
def test_overlap_relisting_is_idempotent(path, state):
    watch_store.record_changes(path, GL, [mr()], [], False, NOW)
    head = watch_store.claim(path, NOW)
    if state == "backoff":
        watch_store.finish(path, head, None, RuntimeError("failed"), NOW)
    elif state == "done":
        watch_store.finish(path, head, None, now=NOW)
    before = watch_store.heads(path)[0]
    assert watch_store.record_changes(path, GL, [mr()], [], False, NOW + timedelta(seconds=20)) == 0
    after = watch_store.heads(path)
    assert len(after) == 1
    assert {k: v for k, v in after[0].items() if k != "last_listed_at"} == {
        k: v for k, v in before.items() if k != "last_listed_at"
    }


def test_merged_before_seen_matches_github_row_and_requeue(path):
    merged = mr(merged_at=(NOW - timedelta(minutes=2)).isoformat())
    watch_store.record_listing(path, GH, [], NOW)
    assert watch_store.record_fast_merges(path, GH, [{**merged, "author_association": "OWNER"}], NOW) == 1
    assert watch_store.record_changes(path, GL, [], [merged], False, NOW) == 1
    gh, gl = watch_store.heads(path)
    ignore = {"author_association", "author_trusted", "author_id"}
    assert {k: v for k, v in comparable(gh).items() if k not in ignore} == {
        k: v for k, v in comparable(gl).items() if k not in ignore
    }
    assert (gl["status"], gl["is_open"], gl["merged_before_seen"]) == ("queued", 0, 1)
    watch_store.restrict_repos(path, [], NOW)
    watch_store.restrict_repos(path, [], NOW, forge="gitlab")
    assert watch_store.requeue_fast_merges(path, [GH], NOW) == 1
    assert watch_store.requeue_fast_merges(path, [GL], NOW) == 1
    assert watch_store.requeue_fast_merges(path, [GH, GL], NOW) == 0


def test_merged_seen_open_preserves_queue_and_backoff(path):
    watch_store.record_changes(path, GL, [mr()], [], False, NOW)
    head = watch_store.claim(path, NOW)
    watch_store.finish(path, head, None, RuntimeError("failed"), NOW)
    before = watch_store.heads(path)[0]
    assert watch_store.record_changes(path, GL, [], [mr(merged_at=NOW.isoformat())], False, NOW) == 1
    after = watch_store.heads(path)[0]
    assert (after["status"], after["merged_before_seen"], after["merged_at"]) == (
        "queued", 1, NOW.isoformat(),
    )
    for key in ("attempts", "next_attempt_at", "eligible_at"):
        assert after[key] == before[key]


def test_incremental_merge_closes_unlisted_obsolete_revision(path):
    watch_store.record_changes(path, GL, [mr()], [], False, NOW)
    merged = mr(sha="b" * 40, merged_at=NOW.isoformat())
    assert watch_store.record_changes(path, GL, [], [merged], False, NOW) == 1
    old, new = watch_store.heads(path)
    assert (old["status"], old["is_open"], old["ended_at"]) == ("closed", 0, NOW.isoformat())
    assert (new["status"], new["is_open"], new["merged_before_seen"]) == ("queued", 0, 1)
    assert watch_store.open_head_shas(path, GL) == {}
    assert watch_store.claim(path, NOW)["head_sha"] == "b" * 40
    assert watch_store.claim(path, NOW) is None


def test_incremental_merge_of_queued_sha_matches_github_requeue(path):
    watch_store.record_listing(path, GH, [gh_pr()], NOW)
    watch_store.record_changes(path, GL, [mr()], [], False, NOW)
    later = NOW + timedelta(seconds=20)
    merged = mr(merged_at=later.isoformat())
    watch_store.record_listing(path, GH, [], later)
    assert watch_store.record_fast_merges(
        path, GH, [{**merged, "author_association": "OWNER"}], later,
    ) == 1
    assert watch_store.record_changes(path, GL, [], [merged], False, later) == 1
    gh, gl = watch_store.heads(path)
    ignore = {"author_association", "author_trusted", "author_id"}
    assert {k: v for k, v in comparable(gh).items() if k not in ignore} == {
        k: v for k, v in comparable(gl).items() if k not in ignore
    }
    assert (gl["status"], gl["is_open"], gl["merged_before_seen"]) == ("queued", 0, 1)
    watch_store.restrict_repos(path, [], later)
    watch_store.restrict_repos(path, [], later, forge="gitlab")
    assert watch_store.requeue_fast_merges(path, [GH], later) == 1
    assert watch_store.requeue_fast_merges(path, [GL], later) == 1
    assert [watch_store.claim(path, later)["repo"] for _ in range(2)] == [GH, GL]


def test_closed_unmerged_closes_only_that_mr_before_fast_merge(path):
    watch_store.record_changes(path, GL, [mr(), mr(2, "b" * 40)], [], False, NOW)
    assert watch_store.record_changes(
        path, GL, [], [mr(), mr(merged_at=NOW.isoformat())], False, NOW,
    ) == 1
    first, other = watch_store.heads(path)
    assert (first["status"], first["is_open"], first["merged_before_seen"]) == ("queued", 0, 1)
    assert (other["status"], other["is_open"]) == ("queued", 1)
    watch_store.record_changes(path, GL, [], [mr(2, "b" * 40)], False, NOW)
    first, other = watch_store.heads(path)
    assert first["status"] == "queued"
    assert (other["status"], other["is_open"], other["ended_at"]) == ("closed", 0, NOW.isoformat())


@pytest.mark.parametrize("complete", [False, True])
def test_closed_unmerged_does_not_override_complete_opened(path, complete):
    watch_store.record_changes(path, GL, [mr()], [mr()], complete, NOW)
    row = watch_store.heads(path)[0]
    assert (row["status"], row["is_open"]) == (("queued", 1) if complete else ("closed", 0))


@pytest.mark.parametrize("repo,pr", [(GH, gh_pr()), (GL, mr())])
def test_complete_changes_match_record_listing(path, tmp_path, repo, pr):
    other = str(tmp_path / "other.db")
    db.init_db(other)
    for opened in ([pr], [{**pr, "head": {"sha": "b" * 40}}], []):
        expected = watch_store.record_listing(path, repo, opened, NOW)
        assert watch_store.record_changes(other, repo, opened, [pr], True, NOW) == expected
        assert [comparable(r) for r in watch_store.heads(path)] == [
            comparable(r) for r in watch_store.heads(other)
        ]


def test_first_incremental_listing_baselines_only_old_first_seen_mrs(path):
    old = (NOW - timedelta(minutes=16)).isoformat()
    watch_store.record_changes(path, GL, [mr(updated_at=old)], [], False, NOW)
    watch_store.record_changes(path, GL, [mr(2, "b" * 40, updated_at=old)], [], False, NOW)
    assert [r["baseline"] for r in watch_store.heads(path)] == [1, 0]


def test_incremental_failure_rolls_back_opened_and_closed_together(path):
    with pytest.raises(ValueError):
        watch_store.record_changes(path, GL, [mr()], [mr(2, merged_at="invalid")], False, NOW)
    assert watch_store.heads(path) == []
    old = (NOW - timedelta(minutes=16)).isoformat()
    watch_store.record_changes(path, GL, [mr(updated_at=old)], [], False, NOW)
    assert watch_store.heads(path)[0]["baseline"] == 1


@pytest.mark.parametrize("trusted,reason", [
    (1, None), (0, "untrusted author (gitlab membership)"), (None, "trust_unknown"),
])
@pytest.mark.parametrize("at_claim", [False, True])
def test_gitlab_trust_is_strict_at_listing_and_claim(path, monkeypatch, trusted, reason, at_claim):
    monkeypatch.setenv("ARGUS_WATCH_TRUSTED_ONLY", "0" if at_claim else "1")
    watch_store.record_listing(path, GL, [mr(author_trusted=trusted, author_association="OWNER")], NOW)
    assert watch_store.heads(path)[0]["author_association"] is None
    if at_claim:
        with watch_store.connection(path) as conn:
            conn.execute("UPDATE pr_heads SET author_association='OWNER' WHERE repo=?", (GL,))
    monkeypatch.setenv("ARGUS_WATCH_TRUSTED_ONLY", "1")
    head = watch_store.claim(path, NOW)
    assert (head is not None) == (trusted == 1)
    row = watch_store.heads(path)[0]
    assert row["skip_reason"] == reason
    assert row["status"] == ("running" if trusted == 1 else "skipped")
    watch_store.claim(path, NOW)
    assert watch_store.heads(path)[0]["skip_reason"] == reason


@pytest.mark.parametrize("trusted,reason", [
    (0, "untrusted author (gitlab membership)"), (None, "trust_unknown"),
])
@pytest.mark.parametrize("incremental", [False, True])
def test_fast_merge_refreshes_queued_gitlab_trust(path, trusted, reason, incremental):
    merged = mr(merged_at=NOW.isoformat())
    assert watch_store.record_fast_merges(path, GL, [merged], NOW) == 1
    changed = {**merged, "author_trusted": trusted, "author_id": None}
    if incremental:
        assert watch_store.record_changes(path, GL, [], [changed], False, NOW) == 0
    else:
        assert watch_store.record_fast_merges(path, GL, [changed], NOW) == 0
    assert watch_store.claim(path, NOW) is None
    row = watch_store.heads(path)[0]
    assert (row["author_trusted"], row["author_id"], row["status"], row["skip_reason"]) == (
        trusted, None, "skipped", reason,
    )


def test_fast_merge_skip_preserves_running_gitlab_head(path):
    merged = mr(merged_at=NOW.isoformat())
    watch_store.record_fast_merges(path, GL, [merged], NOW)
    head = watch_store.claim(path, NOW)
    assert watch_store.record_fast_merges(
        path, GL, [{**merged, "author_trusted": 0, "author_id": None}], NOW,
    ) == 0
    assert watch_store.heads(path)[0] == head


def test_github_claim_keeps_untrusted_association_reason(path, monkeypatch):
    monkeypatch.setenv("ARGUS_WATCH_TRUSTED_ONLY", "0")
    watch_store.record_listing(path, GH, [gh_pr(author_trusted=1, author_association="NONE")], NOW)
    monkeypatch.setenv("ARGUS_WATCH_TRUSTED_ONLY", "1")
    assert watch_store.claim(path, NOW) is None
    assert watch_store.heads(path)[0]["skip_reason"] == "untrusted author association"


def test_trust_disabled_allows_all_states(path, monkeypatch):
    monkeypatch.setenv("ARGUS_WATCH_TRUSTED_ONLY", "0")
    for number, trusted in enumerate((1, 0, None), 1):
        watch_store.record_changes(path, GL, [mr(number, author_trusted=trusted)], [], False, NOW)
    watch_store.record_listing(path, GH, [gh_pr(author_association="NONE")], NOW)
    assert [watch_store.claim(path, NOW)["repo"] for _ in range(4)] == [GL, GL, GL, GH]


@pytest.mark.parametrize("extra,reason", [
    ({"draft": True}, "draft"),
    ({"user": {"login": "robot[bot]", "type": "Bot"}}, "bot"),
])
def test_gitlab_draft_and_bot_checks_precede_trust(path, extra, reason):
    assert watch_store.skip_reason(mr(author_trusted=None, **extra), GL) == reason


@pytest.mark.parametrize("fast", [False, True])
def test_gitlab_stores_author_fields_and_github_leaves_them_null(path, fast):
    prs = [mr(merged_at=NOW.isoformat())] if fast else [mr()]
    record = watch_store.record_fast_merges if fast else watch_store.record_listing
    record(path, GL, prs, NOW)
    record(path, GH, [{**prs[0], "author_association": "OWNER"}], NOW)
    gl, gh = watch_store.heads(path)
    assert (gl["author_trusted"], gl["author_id"], gl["author_association"]) == (1, 23, None)
    assert (gh["author_trusted"], gh["author_id"], gh["author_association"]) == (None, None, "OWNER")
    record(path, GL, [{**prs[0], "author_id": 24}], NOW)
    assert watch_store.heads(path)[0]["author_id"] == (23 if fast else 24)


def test_fast_merge_requeue_updates_gitlab_author_fields(path):
    watch_store.record_listing(path, GL, [mr(author_trusted=None)], NOW)
    with watch_store.connection(path) as conn:
        conn.execute("UPDATE pr_heads SET status='closed', is_open=0 WHERE repo=?", (GL,))
    assert watch_store.record_changes(
        path, GL, [], [mr(author_id=24, merged_at=NOW.isoformat())], False, NOW,
    ) == 1
    row = watch_store.heads(path)[0]
    assert (row["author_trusted"], row["author_id"], row["status"], row["merged_before_seen"]) == (1, 24, "queued", 1)


@pytest.mark.parametrize("minutes,sha,extra", [
    (-61, "a" * 40, {}), (6, "a" * 40, {}), (0, "invalid", {}),
    (0, "a" * 40, {"author_trusted": None}), (0, "a" * 40, {"draft": True}),
])
def test_incremental_fast_merge_rejects_outside_window_invalid_sha_and_skips(path, minutes, sha, extra):
    merged = mr(sha=sha, merged_at=(NOW + timedelta(minutes=minutes)).isoformat(), **extra)
    assert watch_store.record_changes(path, GL, [], [merged], False, NOW) == 0
    assert watch_store.heads(path) == []


@pytest.mark.parametrize("error_class,repo,category,minutes,attempts", [
    (GitLabRateLimited, GL, "gitlab_rate_limited", 5, 0),
    (GitHubRateLimited, GH, "github_rate_limited", 5, 0),
    (RuntimeError, GL, "internal", 2, 1),
])
def test_finish_rate_limits_do_not_count_attempts_plain_errors_back_off(path, error_class, repo, category, minutes, attempts):
    watch_store.record_listing(path, repo, [mr() if repo == GL else gh_pr()], NOW)
    head = watch_store.claim(path, NOW)
    reset = NOW + timedelta(minutes=5)
    error = error_class("failed") if error_class is RuntimeError else error_class(reset.timestamp())
    if error_class is not RuntimeError:
        assert isinstance(error, ForgeRateLimited)
    watch_store.finish(path, head, None, error, NOW)
    row = watch_store.heads(path)[0]
    assert (row["status"], row["attempts"], row["last_error_class"], row["next_attempt_at"]) == (
        "queued", attempts, category, (NOW + timedelta(minutes=minutes)).isoformat(),
    )


@pytest.mark.parametrize("paused,expected", [
    ({"gitlab": 60}, [GH]), ({"github": 60}, [GL]),
    ({}, [GL, GH]), ({"gitlab": 60, "github": 60}, []),
    ({"gitlab": 0, "github": 0}, [GL, GH]),
])
def test_claim_pauses_are_per_forge_and_keep_order(path, paused, expected):
    watch_store.record_listing(path, GL, [mr()], NOW)
    watch_store.record_listing(path, GH, [gh_pr()], NOW)
    pauses = {forge: NOW.timestamp() + seconds for forge, seconds in paused.items()}
    assert [watch_store.claim(path, NOW, pauses=pauses)["repo"] for _ in expected] == expected
    assert watch_store.claim(path, NOW, pauses=pauses) is None


@pytest.mark.parametrize("paused,expected", [("gitlab", GH), ("github", GL)])
def test_claim_derives_pause_from_shared_clients(path, monkeypatch, paused, expected):
    monkeypatch.setenv("ARGUS_GITLAB_URL", "https://gitlab.example")
    monkeypatch.setattr(gitlab_client.config, "token", lambda: "test")
    client = gitlab_client.GitLabClient(transport=httpx.MockTransport(
        lambda request: pytest.fail("unexpected network request"),
    ))
    monkeypatch.setattr(gitlab_client, "_shared", client)
    try:
        monkeypatch.setattr(github, "_pause_until", NOW.timestamp() + 60 if paused == "github" else 0)
        client._pause = NOW.timestamp() + 60 if paused == "gitlab" else 0
        watch_store.record_listing(path, GL, [mr()], NOW)
        watch_store.record_listing(path, GH, [gh_pr()], NOW)
        assert watch_store.claim(path, NOW)["repo"] == expected
        assert watch_store.claim(path, NOW) is None
    finally:
        client.close()


def test_gitlab_client_failure_does_not_block_github_claim(path, monkeypatch, caplog):
    monkeypatch.setenv("ARGUS_GITLAB_URL", "https://gitlab.example")
    monkeypatch.delenv("ARGUS_GITLAB_TOKEN", raising=False)

    def missing_token():
        raise gitlab_client.GitLabAuthError("GitLab token is not set")

    monkeypatch.setattr(gitlab_client, "client", missing_token)
    watch_store.record_listing(path, GH, [gh_pr()], NOW)
    with caplog.at_level("DEBUG", logger="argus.watcher"):
        assert watch_store.claim(path, NOW)["repo"] == GH
    assert len(caplog.records) == 1
    assert caplog.records[0].levelname == "DEBUG"


def test_open_head_shas_returns_latest_non_superseded_open_head(path):
    watch_store.record_listing(path, GL, [mr(), mr(2, "b" * 40)], NOW)
    watch_store.record_listing(path, GH, [gh_pr()], NOW)
    watch_store.record_changes(path, GL, [mr(sha="c" * 40)], [mr(2, "b" * 40)], False, NOW)
    assert watch_store.open_head_shas(path, GL) == {1: "c" * 40}
    assert watch_store.open_head_shas(path, GH) == {1: "a" * 40}


def test_open_head_shas_prefers_new_head_over_running_old_head(path):
    watch_store.record_listing(path, GL, [mr()], NOW)
    assert watch_store.claim(path, NOW)["head_sha"] == "a" * 40
    watch_store.record_changes(path, GL, [mr(sha="b" * 40)], [], False, NOW)
    assert [r["status"] for r in watch_store.heads(path)] == ["running", "queued"]
    assert watch_store.open_head_shas(path, GL) == {1: "b" * 40}


def test_incremental_new_sha_closes_done_head(path):
    watch_store.record_changes(path, GL, [mr()], [], False, NOW)
    head = watch_store.claim(path, NOW)
    watch_store.finish(path, head, None, now=NOW)
    assert watch_store.open_head_shas(path, GL) == {1: "a" * 40}
    watch_store.record_changes(path, GL, [mr(sha="b" * 40)], [], False, NOW)
    old, new = watch_store.heads(path)
    assert (old["status"], old["is_open"]) == ("done", 0)
    assert (new["status"], new["is_open"]) == ("queued", 1)
    assert watch_store.open_head_shas(path, GL) == {1: "b" * 40}


def test_open_head_shas_detects_return_to_earlier_sha_during_running_claim(path):
    watch_store.record_changes(path, GL, [mr()], [], False, NOW)
    head = watch_store.claim(path, NOW)
    watch_store.finish(path, head, None, now=NOW)
    later = NOW + timedelta(seconds=1)
    watch_store.record_changes(path, GL, [mr(sha="b" * 40)], [], False, later)
    assert watch_store.claim(path, later)["head_sha"] == "b" * 40
    watch_store.record_changes(path, GL, [mr()], [], False, NOW + timedelta(seconds=2))
    assert [r["status"] for r in watch_store.heads(path)] == ["done", "running"]
    assert watch_store.open_head_shas(path, GL) == {1: "a" * 40}


@pytest.mark.parametrize("cursor,overlap,expected", [
    (None, 300, None), ("2026-11-03T12:00:00Z", 300, "2026-11-03T11:55:00+00:00"),
    ("2026-11-03T13:00:00+01:00", 60, "2026-11-03T11:59:00+00:00"),
    ("2026-11-03T00:02:00", 300, "2026-11-02T23:57:00+00:00"),
])
def test_poll_window_start_uses_utc_overlap(cursor, overlap, expected):
    assert watch_store.poll_window_start(cursor, overlap) == expected


def test_poll_window_start_defaults_to_five_minutes():
    assert watch_store.poll_window_start("2026-11-03T12:00:00Z") == "2026-11-03T11:55:00+00:00"


def test_poll_cursor_fresh_db_and_partial_updates_clear_none(path):
    with watch_store.connection(path) as conn:
        assert db.gitlab_poll_cursor(conn, GL) is None
        db.set_gitlab_poll_cursor(conn, GL, updated_after=NOW.isoformat(), last_error="failed")
        db.set_gitlab_poll_cursor(conn, GL, last_polled_at=NOW.isoformat())
        assert db.gitlab_poll_cursor(conn, GL) == {
            "repo_key": GL, "updated_after": NOW.isoformat(), "last_error": "failed",
            "last_polled_at": NOW.isoformat(), "last_full_reconcile": None,
        }
        db.set_gitlab_poll_cursor(conn, GL, last_error=None, last_full_reconcile=NOW.isoformat())
        row = db.gitlab_poll_cursor(conn, GL)
        assert row["last_error"] is None
        assert row["updated_after"] == NOW.isoformat()
        assert row["last_full_reconcile"] == NOW.isoformat()
        db.set_gitlab_poll_cursor(conn, GL)
        assert db.gitlab_poll_cursor(conn, GL) == row


def test_poll_cursor_leaves_transaction_to_caller(path):
    with watch_store.connection(path) as conn:
        db.set_gitlab_poll_cursor(conn, GL)
        assert conn.in_transaction
        conn.rollback()
        assert db.gitlab_poll_cursor(conn, GL) is None


@pytest.mark.parametrize("key", [GH, "other:example#42"])
def test_poll_cursor_rejects_non_gitlab_keys(path, key):
    with watch_store.connection(path) as conn:
        with pytest.raises(ValueError):
            db.gitlab_poll_cursor(conn, key)
        with pytest.raises(ValueError):
            db.set_gitlab_poll_cursor(conn, key, last_error=None)


def test_poll_cursor_rejects_unknown_fields(path):
    with watch_store.connection(path) as conn:
        with pytest.raises(ValueError):
            db.set_gitlab_poll_cursor(conn, GL, typo="failed")


def test_poll_cursor_migration_is_additive_and_preserves_existing_tables(path):
    watch_store.record_listing(path, GH, [gh_pr()], NOW)
    with watch_store.connection(path) as conn:
        conn.execute("DROP TABLE gitlab_poll_cursors")
        before = list(conn.iterdump())
    db.init_db(path)
    db.init_db(path)
    with watch_store.connection(path) as conn:
        assert conn.execute("SELECT * FROM gitlab_poll_cursors").fetchall() == []
        conn.execute("DROP TABLE gitlab_poll_cursors")
        assert list(conn.iterdump()) == before
