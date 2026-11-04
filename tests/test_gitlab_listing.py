"""Verify guarded discovery, incremental cursors and membership mapping."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from argus.api import watch_store
from argus.forge.keys import gitlab_key
from argus.gitlab import listing, trust
from argus.gitlab.client import GitLabAuthError, GitLabHTTPError, GitLabRateLimited
from test_gitlab_projects import (
    HOST, KEY, TOKEN, configured as configured, make_client as make_client, payload,
)

CURSOR = "2026-11-04T12:00:00.123000+00:00"


def mr(number=1, **extra):
    return {"iid": number, "sha": "a" * 40, "draft": False,
            "author": {"id": 7, "username": "argus-dev", "bot": False},
            "updated_at": "2026-11-04T12:00:00.123Z", "target_branch": "main",
            "merged_at": None, "source_project_id": 42, "state": "opened", **extra}


def mr_client(make_client, rows):
    return make_client(lambda r: httpx.Response(200, json=(
        {"access_level": 30} if "/members/all/" in r.url.path else rows
    )))


def test_nested_group_expansion_filters_shared_prefixes_and_live_denies(make_client, monkeypatch, configured):
    monkeypatch.setenv("ARGUS_GITLAB_ALLOW", f"gitlab:{HOST}/argus-test/sub")
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "acme-labs")
    entries = [payload("ARGUS-test/Sub/app"), payload("argus-test-other/sub/app", id=43),
               payload("argus-testcorp/sub/app", id=44), payload("acme-labs/app", id=45),
               payload("argus-test/sub/transferred", id=46), payload("argus-test/sub/second", id=47),
               {"id": True, "path_with_namespace": "argus-test/sub/invalid"},
               {"id": 10**18, "path_with_namespace": "argus-test/sub/invalid"}]

    def handle(request):
        if "/groups/" in request.url.path:
            assert dict(request.url.params) == {
                "include_subgroups": "true", "with_shared": "false", "archived": "false",
                "simple": "true", "per_page": "100", **({"page": "2"} if request.url.params.get("page") else {}),
            }
            second = request.url.params.get("page") == "2"
            return httpx.Response(200, json=entries[5:] if second else entries[:5],
                                  headers={"X-Next-Page": "" if second else "2"})
        project_id = int(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(200, json=payload(
            "acme-labs/app" if project_id == 46 else "argus-test/sub/app", id=project_id,
        ))

    client, requests = make_client(handle)
    assert listing.expand_allow(client=client, db_path=configured) == (KEY, f"gitlab:{HOST}#47")
    assert {r.url.path for r in requests if "/projects/" in r.url.path} == {
        "/api/v4/projects/42", "/api/v4/projects/46", "/api/v4/projects/47",
    }


@pytest.mark.parametrize("path,archived", [("allowed/app", True), ("acme-labs/app", False)])
def test_id_expansion_runs_live_deny(make_client, monkeypatch, configured, path, archived):
    monkeypatch.setenv("ARGUS_GITLAB_ALLOW", KEY)
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "acme-labs")
    client, requests = make_client(lambda _: httpx.Response(200, json=payload(path, archived=archived)))
    assert listing.expand_allow(client=client, db_path=configured) == ()
    assert [r.url.path for r in requests] == ["/api/v4/projects/42"]


def test_expansion_deduplicates_in_order_and_skips_foreign_hosts(make_client, monkeypatch, configured):
    monkeypatch.setenv("ARGUS_GITLAB_ALLOW", f"{KEY},gitlab:other.test#43,gitlab:{HOST}/allowed")

    def handle(request):
        return httpx.Response(200, json=[payload(), payload(id=43)] if "/groups/" in request.url.path
                              else payload(id=int(request.url.path.rsplit("/", 1)[1])))

    client, requests = make_client(handle)
    assert listing.expand_allow(client=client, db_path=configured) == (KEY, f"gitlab:{HOST}#43")
    assert all(r.url.host == HOST for r in requests)


@pytest.mark.parametrize("project_status", [200, 301, 404])
def test_project_path_fallback_and_renames(make_client, monkeypatch, configured, caplog, project_status):
    monkeypatch.setenv("ARGUS_GITLAB_ALLOW", f"gitlab:{HOST}/allowed/app")

    def handle(request):
        if "/groups/" in request.url.path:
            return httpx.Response(404, text=TOKEN)
        return httpx.Response(project_status, json=payload())

    client, requests = make_client(handle)
    assert listing.expand_allow(client=client, db_path=configured) == ((KEY,) if project_status == 200 else ())
    assert requests[1].url.raw_path == b"/api/v4/projects/allowed%2Fapp"
    assert TOKEN not in caplog.text


@pytest.mark.parametrize("by_id", [False, True])
@pytest.mark.parametrize("code,error", [(401, GitLabAuthError), (429, GitLabRateLimited)])
def test_expansion_auth_and_rate_limits_propagate(make_client, monkeypatch, code, error, by_id):
    if by_id:
        monkeypatch.setenv("ARGUS_GITLAB_ALLOW", KEY)
    client, _ = make_client(lambda _: httpx.Response(code, text=TOKEN))
    with pytest.raises(error):
        listing.expand_allow(client=client)


def test_expansion_entry_failure_does_not_drop_other_entries(make_client, monkeypatch, configured, caplog):
    monkeypatch.setenv("ARGUS_GITLAB_ALLOW", f"gitlab:{HOST}/missing,{KEY}")
    client, _ = make_client(lambda r: httpx.Response(500, text=TOKEN) if "/groups/" in r.url.path
                            else httpx.Response(200, json=payload()))
    assert listing.expand_allow(client=client, db_path=configured) == (KEY,)
    assert f"gitlab:{HOST}/missing" in caplog.text
    assert TOKEN not in caplog.text


@pytest.mark.parametrize("code", [403, 404, 301])
def test_strict_expansion_skips_permanent_project_failure(make_client, monkeypatch, configured, code):
    other = gitlab_key(HOST, 43)
    monkeypatch.setenv("ARGUS_GITLAB_ALLOW", f"{KEY},{other}")
    client, _ = make_client(lambda r: httpx.Response(code) if r.url.path.endswith("/42")
                            else httpx.Response(200, json=payload(id=43)))
    assert listing.expand_allow(client=client, db_path=configured, strict=True) == (other,)


def test_strict_expansion_raises_on_transient_project_failure(make_client, monkeypatch, configured):
    monkeypatch.setenv("ARGUS_GITLAB_ALLOW", KEY)
    client, _ = make_client(lambda _: httpx.Response(503))
    with pytest.raises(GitLabHTTPError):
        listing.expand_allow(client=client, db_path=configured, strict=True)


@pytest.mark.parametrize("by_path", [False, True], ids=["group", "path"])
@pytest.mark.parametrize("code", [403, 404, 301])
def test_strict_expansion_skips_permanent_group_or_path_failure(
        make_client, monkeypatch, configured, caplog, by_path, code):
    monkeypatch.setenv("ARGUS_GITLAB_ALLOW", f"gitlab:{HOST}/missing,{KEY}")

    def handle(request):
        if request.url.path == "/api/v4/projects/42":
            return httpx.Response(200, json=payload())
        if by_path and "/groups/" in request.url.path:
            return httpx.Response(404, text=TOKEN)
        return httpx.Response(code, text=TOKEN)

    client, requests = make_client(handle)
    assert listing.expand_allow(client=client, db_path=configured, strict=True) == (KEY,)
    assert requests[-1].url.path == "/api/v4/projects/42"
    if by_path or code == 404:
        assert requests[1].url.path == "/api/v4/projects/missing"
    assert f"gitlab:{HOST}/missing" in caplog.text
    assert TOKEN not in caplog.text


@pytest.mark.parametrize("by_path", [False, True], ids=["group", "path"])
@pytest.mark.parametrize("code,error", [
    (503, GitLabHTTPError), (401, GitLabAuthError), (429, GitLabRateLimited),
])
def test_strict_expansion_raises_on_transient_group_or_path_failure(
        make_client, monkeypatch, configured, by_path, code, error):
    monkeypatch.setenv("ARGUS_GITLAB_ALLOW", f"gitlab:{HOST}/missing,{KEY}")

    def handle(request):
        if by_path and "/groups/" in request.url.path:
            return httpx.Response(404)
        return httpx.Response(code, text=TOKEN)

    client, requests = make_client(handle)
    with pytest.raises(error):
        listing.expand_allow(client=client, db_path=configured, strict=True)
    assert all(r.url.path != "/api/v4/projects/42" for r in requests)


def test_pagination_lists_250_mrs_and_maximum_cursor(make_client):
    start = datetime(2026, 11, 4, tzinfo=timezone.utc)

    def handle(request):
        if "/members/all/" in request.url.path:
            return httpx.Response(200, json={"access_level": 30})
        page = int(request.url.params.get("page", "1"))
        rows = [mr(i, updated_at=(start + timedelta(seconds=i)).isoformat())
                for i in range((page - 1) * 100 + 1, min(page * 100, 250) + 1)]
        return httpx.Response(200, json=rows, headers={"X-Next-Page": str(page + 1) if page < 3 else ""})

    client, requests = make_client(handle)
    result = listing.list_changes(KEY, CURSOR, client=client)
    assert len(result.changes) == 250
    assert result.cursor == (start + timedelta(seconds=250)).isoformat()
    assert (result.closed, result.complete) == ((), False)
    assert len([r for r in requests if r.url.path.endswith("/merge_requests")]) == 3


def test_overlap_relists_cursor_row_idempotently(make_client, configured):
    client, requests = mr_client(make_client, [mr()])
    for _ in range(2):
        result = listing.list_changes(KEY, CURSOR, client=client)
        watch_store.record_changes(configured, KEY, result.changes, result.closed, result.complete)
    assert len(watch_store.heads(configured)) == 1
    for request in requests:
        if request.url.path.endswith("/merge_requests"):
            assert request.url.params["updated_after"] == "2026-11-04T11:55:00.123000+00:00"
            assert request.url.params["state"] == "all"


def test_closed_and_locked_rows_contribute_to_cursor(make_client):
    client, _ = mr_client(make_client, [
        mr(1), mr(2, state="locked"), mr(3, state="closed", sha=None),
        mr(4, state="merged", updated_at="2026-11-04T13:00:00.456Z",
           merged_at="2026-11-04T14:00:00.123+01:00"),
    ])
    result = listing.list_changes(KEY, CURSOR, client=client)
    assert [r["number"] for r in result.changes] == [1, 2]
    assert [r["number"] for r in result.closed] == [3, 4]
    assert all(r["author_trusted"] == 1 for r in result.closed)
    assert result.cursor == "2026-11-04T13:00:00.456000+00:00"
    assert result.closed[1]["merged_at"] == "2026-11-04T13:00:00.123000+00:00"


@pytest.mark.parametrize("sha", [None, "preparing", "a" * 39, "g" * 40])
def test_incremental_listing_pins_cursor_to_preparing_head(make_client, sha):
    client, requests = mr_client(make_client, [
        mr(1, updated_at="2026-11-04T12:00:00Z"),
        mr(2, sha=sha, updated_at="2026-11-04T13:00:00Z"),
        mr(3, updated_at="2026-11-04T14:00:00Z"),
        mr(4, sha=None, updated_at="2026-11-04T13:30:00Z"),
    ])
    result = listing.list_changes(KEY, CURSOR, client=client)
    assert [row["number"] for row in result.changes] == [1, 3]
    assert result.cursor == "2026-11-04T13:00:00+00:00"
    assert result.complete is False
    listing.list_changes(KEY, result.cursor, client=client)
    listing_requests = [r for r in requests if r.url.path.endswith("/merge_requests")]
    assert listing_requests[-1].url.params["updated_after"] == "2026-11-04T12:55:00+00:00"


@pytest.mark.parametrize("sha", [None, "preparing", "a" * 39, "g" * 40])
def test_full_listing_with_preparing_head_keeps_stored_head_open(configured, make_client, sha):
    client, _ = mr_client(make_client, [mr()])
    initial = listing.full_listing(KEY, client=client)
    assert initial.complete is True
    watch_store.record_changes(configured, KEY, initial.changes, initial.closed, initial.complete)
    before = watch_store.heads(configured)[0]
    assert (before["status"], before["is_open"]) == ("queued", 1)

    client, _ = mr_client(make_client, [mr(sha=sha)])
    result = listing.full_listing(KEY, client=client)
    watch_store.record_changes(configured, KEY, result.changes, result.closed, result.complete)
    assert watch_store.heads(configured) == [before]
    assert result.complete is False
    assert result.changes == ()
    assert result.cursor == CURSOR


def test_full_listing_maps_fork_without_source_lookup_and_uses_guarded_routes(make_client):
    client, requests = mr_client(make_client, [mr(source_project_id=99)])
    result = listing.full_listing(KEY, client=client)
    assert result.complete is True
    assert result.closed == ()
    assert result.cursor == CURSOR
    assert result.changes == ({
        "number": 1, "head": {"sha": "a" * 40}, "updated_at": CURSOR, "draft": False,
        "user": {"login": "argus-dev", "type": "User"}, "base": {"ref": "main"},
        "author_trusted": 1, "author_id": 7, "merged_at": None, "source_project_id": 99,
    },)
    for request in requests:
        assert str(request.url).startswith(f"https://{HOST}/api/v4/")
        assert request.url.path in {"/api/v4/projects/42/merge_requests", "/api/v4/projects/42/members/all/7"}
    assert dict(requests[0].url.params) == {
        "state": "opened", "per_page": "100", "order_by": "updated_at", "sort": "asc",
    }


def test_listing_cache_is_scoped_to_tick(make_client):
    client, requests = mr_client(make_client, [mr(1), mr(2)])
    cache = trust.TrustCache()
    listing.full_listing(KEY, client=client, cache=cache)
    listing.list_changes(KEY, CURSOR, client=client, cache=cache)
    assert len([r for r in requests if "/members/all/" in r.url.path]) == 1
    listing.full_listing(KEY, client=client, cache=trust.TrustCache())
    assert len([r for r in requests if "/members/all/" in r.url.path]) == 2
    listing.full_listing(KEY, client=client)
    assert len([r for r in requests if "/members/all/" in r.url.path]) == 3


def test_invalid_rows_do_not_drop_page_and_timestamps_use_utc(make_client, caplog):
    rows = [None, [], mr(iid=True), mr(iid="1"), mr(updated_at=None), mr(updated_at="bad"),
            mr(sha="preparing"), mr(10, updated_at="2026-11-04T14:00:00.001+02:00"),
            mr(11, updated_at="2026-11-04T12:00:00.002")]
    client, _ = mr_client(make_client, rows)
    with caplog.at_level("DEBUG", logger="argus.gitlab.listing"):
        result = listing.full_listing(KEY, client=client)
    assert [r["number"] for r in result.changes] == [10, 11]
    assert result.changes[0]["updated_at"] == "2026-11-04T12:00:00.001000+00:00"
    assert result.cursor == CURSOR
    assert caplog.records


@pytest.mark.parametrize("invalid", [{"iid": "bad"}, {"updated_at": "bad"}, {"sha": None}],
                         ids=["bad_iid", "bad_updated_at", "head_preparing"])
def test_full_listing_with_skipped_row_is_incomplete(make_client, invalid):
    client, _ = mr_client(make_client, [mr(**invalid), mr(2)])
    result = listing.full_listing(KEY, client=client)
    assert [row["number"] for row in result.changes] == [2]
    assert result.cursor == CURSOR
    assert result.complete is False


@pytest.mark.parametrize("author", [{}, None, {"id": True}, {"id": "7"}, {"id": 7.0}])
def test_invalid_author_does_not_lookup_membership(make_client, author):
    client, requests = mr_client(make_client, [mr(author=author)])
    row = listing.full_listing(KEY, client=client).changes[0]
    assert row["author_id"] is None
    assert row["author_trusted"] is None
    assert len(requests) == 1


@pytest.mark.parametrize("username", [None, "", 7, [], {}])
def test_listing_stores_missing_and_malformed_authors(make_client, configured, monkeypatch, username):
    monkeypatch.setenv("ARGUS_WATCH_TRUSTED_ONLY", "1")
    client, requests = mr_client(make_client, [
        mr(1), mr(2, author=None), mr(3, author={}), mr(4, author={"username": username}),
    ])
    result = listing.full_listing(KEY, client=client)
    assert len(result.changes) == 4
    assert [row["user"]["login"] for row in result.changes] == ["argus-dev", "", "", ""]
    assert watch_store.record_changes(
        configured, KEY, result.changes, result.closed, result.complete,
    ) == 1
    rows = watch_store.heads(configured)
    assert len(rows) == 4
    assert rows[0]["status"] == "queued"
    assert [(row["author"], row["status"], row["skip_reason"]) for row in rows[1:]] == [
        ("", "skipped", "trust_unknown"),
        ("", "skipped", "trust_unknown"),
        ("", "skipped", "trust_unknown"),
    ]
    assert len([request for request in requests if "/members/all/" in request.url.path]) == 1


def test_merged_rows_and_draft_fallback(make_client):
    rows = [mr(1, state="merged", merged_at="2026-11-04T12:00:01.123Z"),
            mr(2, state="merged", merged_at="2026-11-04T12:00:02.123Z")]
    client, _ = mr_client(make_client, rows)
    result = listing.list_changes(KEY, CURSOR, client=client)
    assert len(result.changes) + len(result.closed) == len(rows)
    row = mr(work_in_progress=True, author={"id": 7, "username": "project_5_bot_abc123"})
    row.pop("draft")
    client, _ = mr_client(make_client, [row])
    mapped = listing.full_listing(KEY, client=client).changes[0]
    assert mapped["draft"] is True
    assert mapped["user"]["type"] == "Bot"


def test_empty_listings_and_missing_cursor(make_client):
    client, requests = mr_client(make_client, [])
    with pytest.raises(ValueError, match="cursor required; use full_listing"):
        listing.list_changes(KEY, None, client=client)
    assert requests == []
    assert listing.list_changes(KEY, CURSOR, client=client).cursor == CURSOR
    assert listing.full_listing(KEY, client=client).cursor is None
