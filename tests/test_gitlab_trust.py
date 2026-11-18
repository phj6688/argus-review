"""Exercise membership decisions and recovery without external services."""

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from argus.api import watch_store
from argus.gitlab import client as client_module, status, trust
from test_gitlab_projects import (
    HOST, KEY, TOKEN, configured as configured, make_client as make_client,
)

NOW = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def watch_settings(monkeypatch):
    monkeypatch.setenv("ARGUS_WATCH_TRUSTED_ONLY", "1")
    monkeypatch.delenv("ARGUS_WATCH_INCLUDE_DRAFTS", raising=False)
    monkeypatch.delenv("ARGUS_WATCH_INCLUDE_BOTS", raising=False)
    monkeypatch.setattr(watch_store, "_listed_repos", set())


def stored_mr(number=1, **extra):
    return {"number": number, "head": {"sha": "a" * 40},
            "updated_at": NOW.isoformat(), "draft": False,
            "user": {"login": "argus-dev", "type": "User"},
            "base": {"ref": "main"}, "author_id": number,
            "author_trusted": None, "head_committed_at": NOW.isoformat(), **extra}


@pytest.mark.parametrize("author,expected", [
    ({"username": "project_5_bot_abc123"}, True),
    ({"username": "group_7_bot_deadbeef"}, True),
    ({"username": "service-account", "bot": True}, True),
    ({"username": "argus-dev"}, False), ({"username": "project_bot_x"}, False),
    ({"username": "project_5_bot"}, True), ({"username": 5}, False),
    ({"bot": 1}, False), ({}, False), (None, False), ([], False),
])
def test_bot_detection(author, expected):
    assert trust.is_bot(author) is expected


@pytest.mark.parametrize("outcome,expected", [
    (30, True), (20, False), (404, False), (403, None), (500, None),
    ("timeout", None), (429, None), (401, None), (301, None), ("garbage", None),
    ("list", None), ("missing", None), ("bool", None), ("string", None),
])
def test_membership_outcomes(make_client, outcome, expected):
    def handle(request):
        if outcome == "timeout":
            raise httpx.ConnectTimeout(TOKEN, request=request)
        if outcome == "garbage":
            return httpx.Response(200, text=TOKEN)
        if outcome in {"list", "missing", "bool", "string"}:
            return httpx.Response(200, json={
                "list": [], "missing": {}, "bool": {"access_level": True},
                "string": {"access_level": "30"},
            }[outcome])
        return (httpx.Response(200, json={"access_level": outcome}) if outcome < 100
                else httpx.Response(outcome, text=TOKEN))

    client, requests = make_client(handle)
    assert trust.author_trusted(KEY, 7, client=client) is expected
    assert {r.url.path for r in requests} == {"/api/v4/projects/42/members/all/7"}


@pytest.mark.parametrize("user_id", [None, True, False, 0, -1, 10**18, "7", 7.0])
def test_invalid_user_id_makes_no_request(make_client, user_id):
    client, requests = make_client(lambda _: pytest.fail("Unexpected request"))
    assert trust.author_trusted(KEY, user_id, client=client) is None
    assert requests == []


def test_membership_threshold_and_shared_client(make_client, monkeypatch):
    client, _ = make_client(lambda _: httpx.Response(200, json={"access_level": 30}))
    monkeypatch.setattr(trust, "shared_client", lambda: client)
    monkeypatch.setenv("ARGUS_GITLAB_TRUSTED_LEVEL", "40")
    assert trust.author_trusted(KEY, 7) is False
    monkeypatch.setattr(trust, "shared_client", lambda: None)
    assert trust.author_trusted(KEY, 7) is None


def test_only_successful_membership_response_grants_trust(make_client):
    client, _ = make_client(lambda _: httpx.Response(201, json={"access_level": 30}))
    assert trust.author_trusted(KEY, 7, client=client) is None


def test_foreign_repository_cannot_reuse_cached_membership(make_client):
    client, requests = make_client(lambda _: httpx.Response(200, json={"access_level": 30}))
    cache = trust.TrustCache()
    assert trust.author_trusted(KEY, 7, client=client, cache=cache) is True
    assert trust.author_trusted("gitlab:other.test#42", 7, client=client, cache=cache) is None
    assert len(requests) == 1


@pytest.mark.parametrize("code,expected", [(200, True), (404, False), (403, None)])
def test_cache_keeps_all_outcomes_for_one_tick(make_client, code, expected):
    client, requests = make_client(lambda _: httpx.Response(code, json={"access_level": 30}))
    cache = trust.TrustCache()
    for _ in range(2):
        assert trust.author_trusted(KEY, 7, client=client, cache=cache) is expected
    assert len(requests) == 1
    assert trust.author_trusted(KEY, 7, client=client, cache=trust.TrustCache()) is expected
    assert len(requests) == 2
    trust.author_trusted(f"gitlab:{HOST}#43", 7, client=client, cache=cache)
    assert len(requests) == 3


def test_unknown_trust_logs_only_exception_class(make_client, caplog, monkeypatch):
    client, _ = make_client(lambda _: httpx.Response(200, text=TOKEN + " https://private.test/"))
    monkeypatch.setattr(logging.root.manager, "disable", logging.NOTSET)
    monkeypatch.setattr(trust.log, "disabled", False)
    monkeypatch.setattr(trust.log, "propagate", True)
    monkeypatch.setattr(logging.getLogger("argus"), "propagate", True)
    caplog.set_level(logging.DEBUG, logger="argus.gitlab.trust")
    assert trust.author_trusted(KEY, 7, client=client) is None
    assert "JSONDecodeError" in caplog.text
    assert TOKEN not in caplog.text
    assert "https://private.test/" not in caplog.text


def test_sweep_unknown_requeues_rejects_and_preserves_unknown(configured, make_client):
    watch_store.record_changes(configured, KEY, [stored_mr(n) for n in range(1, 4)], [], False, NOW)
    before = watch_store.heads(configured)

    def handle(request):
        user = int(request.url.path.rsplit("/", 1)[1])
        return httpx.Response({1: 200, 2: 404, 3: 403}[user], json={"access_level": 30})

    client, _ = make_client(handle)
    assert trust.sweep_unknown(configured, 3, client=client) == {
        "checked": 3, "requeued": 1, "untrusted": 1, "unknown": 1,
    }
    queued, rejected, unknown = watch_store.heads(configured)
    assert (queued["status"], queued["author_trusted"], queued["skip_reason"]) == ("queued", 1, None)
    assert (queued["pr_updated_at"], queued["head_committed_at"], queued["author_id"]) == (
        NOW.isoformat(), NOW.isoformat(), 1,
    )
    assert (rejected["status"], rejected["author_trusted"], rejected["skip_reason"]) == (
        "skipped", 0, "untrusted author (gitlab membership)",
    )
    assert unknown["last_listed_at"] != before[2]["last_listed_at"]
    assert unknown == before[2] | {"last_listed_at": unknown["last_listed_at"]}


def test_sweep_unknown_rotates_failed_lookups(configured, make_client, monkeypatch):
    for number in range(1, 4):
        watch_store.record_changes(
            configured, KEY, [stored_mr(number)], [], False, NOW - timedelta(days=4 - number),
        )
    client, requests = make_client(lambda _: httpx.Response(403))
    for tick in range(3):
        monkeypatch.setattr(watch_store, "utcnow", lambda stamp=NOW + timedelta(seconds=tick): stamp)
        assert trust.sweep_unknown(configured, 1, client=client) == {
            "checked": 1, "requeued": 0, "untrusted": 0, "unknown": 1,
        }
        assert [int(r.url.path.rsplit("/", 1)[1]) for r in requests] == list(range(1, tick + 2))
    assert watch_store.unknown_trust_count(configured) == 3


def test_sweep_unknown_requeues_later_head_after_failure(configured, make_client, monkeypatch):
    watch_store.record_changes(configured, KEY, [stored_mr(1), stored_mr(2)], [], False, NOW)
    resolved = set()

    def handle(request):
        user_id = int(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(200 if user_id in resolved else 403, json={"access_level": 30})

    client, requests = make_client(handle)
    monkeypatch.setattr(watch_store, "utcnow", lambda: NOW + timedelta(seconds=1))
    assert trust.sweep_unknown(configured, 1, client=client)["unknown"] == 1
    resolved.add(2)
    assert trust.sweep_unknown(configured, 1, client=client)["requeued"] == 1
    assert [int(r.url.path.rsplit("/", 1)[1]) for r in requests] == [1, 2]
    first, second = watch_store.heads(configured)
    assert (first["status"], first["skip_reason"], first["author_trusted"]) == (
        "skipped", "trust_unknown", None,
    )
    assert (second["status"], second["author_trusted"]) == ("queued", 1)


def test_sweep_unknown_defers_null_author(configured, make_client, monkeypatch):
    watch_store.record_changes(
        configured, KEY, [stored_mr(1, author_id=None), stored_mr(2)], [], False, NOW,
    )
    before = watch_store.heads(configured)[0]
    stamp = NOW + timedelta(seconds=1)
    monkeypatch.setattr(watch_store, "utcnow", lambda: stamp)
    client, requests = make_client(lambda _: httpx.Response(200, json={"access_level": 30}))
    assert trust.sweep_unknown(configured, 1, client=client)["unknown"] == 1
    assert requests == []
    assert watch_store.heads(configured)[0] == before | {"last_listed_at": stamp.isoformat()}
    assert trust.sweep_unknown(configured, 1, client=client)["requeued"] == 1
    assert [r.url.path for r in requests] == ["/api/v4/projects/42/members/all/2"]


@pytest.mark.parametrize("update", [
    "UPDATE pr_heads SET is_open=0 WHERE pr=1",
    "UPDATE pr_heads SET status='queued' WHERE pr=1",
    "UPDATE pr_heads SET skip_reason='draft' WHERE pr=1",
    "DELETE FROM pr_heads WHERE pr=1",
])
def test_defer_unknown_trust_ignores_ineligible_head(configured, update):
    watch_store.record_changes(configured, KEY, [stored_mr(1), stored_mr(2)], [], False, NOW)
    head = watch_store.heads(configured)[0]
    with watch_store.connection(configured) as conn:
        conn.execute(update)
    before = watch_store.heads(configured)
    watch_store.defer_unknown_trust(configured, head["id"], NOW + timedelta(seconds=1))
    assert watch_store.heads(configured) == before


@pytest.mark.parametrize("code", [200, 404])
def test_sweep_unknown_does_not_replay_replaced_head(configured, make_client, code):
    watch_store.record_changes(configured, KEY, [stored_mr()], [], False, NOW)
    refreshed = []

    def handle(request):
        watch_store.record_changes(configured, KEY, [stored_mr(
            head={"sha": "b" * 40}, author_trusted=1,
        )], [], False, NOW + timedelta(seconds=1))
        refreshed.extend(watch_store.heads(configured))
        return httpx.Response(code, json={"access_level": 30})

    client, _ = make_client(handle)
    assert trust.sweep_unknown(configured, 1, client=client) == {
        "checked": 1, "requeued": 0, "untrusted": 0, "unknown": 0,
    }
    assert watch_store.heads(configured) == refreshed
    assert (refreshed[0]["status"], refreshed[0]["is_open"]) == ("skipped", 0)
    assert (refreshed[1]["head_sha"], refreshed[1]["status"]) == ("b" * 40, "queued")


def test_sweep_unknown_preserves_draft_refresh_during_lookup(configured, make_client):
    watch_store.record_changes(configured, KEY, [stored_mr()], [], False, NOW)
    refreshed = []

    def handle(request):
        watch_store.record_changes(configured, KEY, [stored_mr(draft=True)], [], False, NOW)
        refreshed.extend(watch_store.heads(configured))
        return httpx.Response(200, json={"access_level": 30})

    client, _ = make_client(handle)
    assert trust.sweep_unknown(configured, 1, client=client) == {
        "checked": 1, "requeued": 0, "untrusted": 0, "unknown": 0,
    }
    assert watch_store.heads(configured) == refreshed
    assert (refreshed[0]["draft"], refreshed[0]["status"], refreshed[0]["skip_reason"]) == (
        1, "skipped", "draft",
    )


def test_sweep_unknown_uses_current_draft_during_lookup(configured, make_client):
    watch_store.record_changes(configured, KEY, [stored_mr()], [], False, NOW)

    def handle(request):
        with watch_store.connection(configured) as conn:
            conn.execute("UPDATE pr_heads SET draft=1")
        return httpx.Response(200, json={"access_level": 30})

    client, _ = make_client(handle)
    assert trust.sweep_unknown(configured, 1, client=client)["requeued"] == 0
    row = watch_store.heads(configured)[0]
    assert (row["draft"], row["status"], row["skip_reason"], row["author_trusted"]) == (
        1, "skipped", "draft", 1,
    )


@pytest.mark.parametrize("update", [
    "UPDATE pr_heads SET status='queued'",
    "UPDATE pr_heads SET head_sha='changed'",
    "DELETE FROM pr_heads",
])
def test_apply_trust_ignores_ineligible_snapshot(configured, update):
    watch_store.record_changes(configured, KEY, [stored_mr()], [], False, NOW)
    snapshot = watch_store.heads(configured)[0]
    with watch_store.connection(configured) as conn:
        conn.execute(update)
    current = watch_store.heads(configured)
    assert watch_store.apply_trust(configured, snapshot, True, NOW) is None
    assert watch_store.heads(configured) == current


def test_apply_trust_handles_null_author(configured):
    watch_store.record_changes(configured, KEY, [stored_mr()], [], False, NOW)
    with watch_store.connection(configured) as conn:
        conn.execute("UPDATE pr_heads SET author=NULL, author_type=NULL")
    head = watch_store.heads(configured)[0]
    assert watch_store.apply_trust(configured, head, True, NOW) == "queued"
    row = watch_store.heads(configured)[0]
    assert (row["author"], row["author_type"], row["author_trusted"]) == ("", None, 1)


def test_sweep_limit_checks_oldest_open_gitlab_head(configured, make_client):
    for n, age in [(1, 0), (2, 2), (3, 1)]:
        watch_store.record_changes(configured, KEY, [stored_mr(n)], [], False, NOW - timedelta(days=age))
    watch_store.record_changes(configured, "owner/repo", [stored_mr(4)], [], False, NOW)
    with watch_store.connection(configured) as conn:
        conn.execute("UPDATE pr_heads SET skip_reason='trust_unknown' WHERE repo='owner/repo'")
    client, requests = make_client(lambda _: httpx.Response(200, json={"access_level": 30}))
    assert trust.sweep_unknown(configured, 0, client=client)["checked"] == 0
    assert watch_store.unknown_trust_heads(configured, -1) == []
    assert requests == []
    assert trust.sweep_unknown(configured, 1, client=client)["requeued"] == 1
    assert requests[0].url.path.endswith("/members/all/2")
    assert watch_store.unknown_trust_count(configured) == 2
    watch_store.record_changes(configured, KEY, [], [stored_mr(1)], False, NOW)
    assert watch_store.unknown_trust_count(configured) == 1


@pytest.mark.parametrize("extra,reason", [
    ({"draft": True}, "draft"),
    ({"user": {"login": "service-account", "type": "Bot"}}, "bot"),
])
def test_resolved_trust_keeps_other_skip_reasons(configured, make_client, extra, reason):
    watch_store.record_changes(configured, KEY, [stored_mr()], [], False, NOW)
    with watch_store.connection(configured) as conn:
        conn.execute("UPDATE pr_heads SET draft=?, author_type=?", (
            int(extra.get("draft", False)), extra.get("user", {}).get("type", "User"),
        ))
    client, _ = make_client(lambda _: httpx.Response(200, json={"access_level": 30}))
    assert trust.sweep_unknown(configured, 1, client=client)["requeued"] == 0
    row = watch_store.heads(configured)[0]
    assert (row["status"], row["skip_reason"], row["author_trusted"]) == ("skipped", reason, 1)


def test_apply_trust_preserves_attempt_limit_and_completed_review(configured):
    watch_store.record_changes(configured, KEY, [stored_mr(1), stored_mr(2)], [], False, NOW)
    with watch_store.connection(configured) as conn:
        conn.execute("UPDATE pr_heads SET attempts=4 WHERE pr=1")
        conn.execute("INSERT INTO reviews(repo,pr,head_sha,status,model,created_at) "
                     "VALUES(?,?,?,'done','test',?)", (KEY, 2, "a" * 40, NOW.isoformat()))
    first, second = watch_store.heads(configured)
    assert watch_store.apply_trust(configured, first, True, NOW) == "parked"
    assert watch_store.apply_trust(configured, second, True, NOW) == "done"


def test_unknown_trust_count_and_integration_status(configured, make_client, monkeypatch):
    watch_store.record_changes(configured, KEY, [stored_mr()], [], False, NOW)
    row = watch_store.heads(configured)[0]
    assert (row["status"], row["skip_reason"]) == ("skipped", "trust_unknown")
    assert watch_store.unknown_trust_count(configured) == 1
    client, _ = make_client(lambda _: httpx.Response(200, json={"username": "review-bot", "bot": True}))
    monkeypatch.setattr(client_module, "client", lambda: client)
    monkeypatch.setattr(status, "_identity_cache", None)
    assert status.integration_status()["trustUnknown"] == 1


@pytest.mark.parametrize("missing", [True, False])
def test_status_count_falls_back_to_zero(configured, make_client, monkeypatch, tmp_path, missing):
    client, _ = make_client(lambda _: httpx.Response(200, json={"username": "review-bot", "bot": True}))
    monkeypatch.setattr(client_module, "client", lambda: client)
    monkeypatch.setattr(status, "_identity_cache", None)
    path = tmp_path / "absent.db"
    if missing:
        monkeypatch.setenv("ARGUS_DB_PATH", str(path))
    else:
        def fail(_):
            raise sqlite3.OperationalError("unavailable")
        monkeypatch.setattr(watch_store, "unknown_trust_count", fail)
    assert status.integration_status()["trustUnknown"] == 0
    assert not path.exists()
