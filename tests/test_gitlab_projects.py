"""Check live project identity and policy without network access."""

import logging
import os
import sqlite3
from contextlib import closing

import httpx
import pytest

from argus import repo_policy
from argus.api import db
from argus.forge.keys import parse_repo_key
from argus.forge.types import ChangeMeta, RepoRecord, RepoRef
from argus.gitlab import projects
from argus.gitlab.client import GitLabClient, GitLabDenied

HOST = "gitlab.example.test"
KEY = f"gitlab:{HOST}#42"
REF = parse_repo_key(KEY)
TOKEN = "gl" + "pat-" + "x" * 20


@pytest.fixture(autouse=True)
def configured(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.startswith("ARGUS_GITLAB_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("ARGUS_GITLAB_URL", f"https://{HOST}")
    monkeypatch.setenv("ARGUS_GITLAB_TOKEN", TOKEN)
    monkeypatch.setenv("ARGUS_GITLAB_ALLOW", f"gitlab:{HOST}/allowed")
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "")
    path = str(tmp_path / "projects.db")
    monkeypatch.setenv("ARGUS_DB_PATH", path)
    monkeypatch.setattr(GitLabClient, "retry_delay", 0)
    db.init_db(path)
    return path


@pytest.fixture
def make_client():
    clients = []

    def make(handler):
        requests = []

        def handle(request):
            requests.append(request)
            return handler(request)

        client = GitLabClient(transport=httpx.MockTransport(handle))
        clients.append(client)
        return client, requests

    yield make
    for client in clients:
        client.close()


def payload(path="allowed/app", **fields):
    return {"id": 42, "path_with_namespace": path, "archived": False,
            "default_branch": "main", "visibility": "private"} | fields


def save_policy(path, deny=()):
    db.save_config(path, repos=[{"name": KEY}], model="test", bug_cap=5,
                   min_confidence=0.5, deny=list(deny))


def meta(source=None):
    return ChangeMeta(None, None, "opened", False, False, None, None,
                      False, "Test change", None, source)


def test_transfer_into_db_denied_group_denies_next_event(make_client, configured):
    data = payload()
    client, requests = make_client(lambda _: httpx.Response(200, json=data))
    save_policy(configured, ["github-owner", f"gitlab:{HOST}/blocked"])
    assert projects.live_deny(KEY, client=client).allowed
    data["path_with_namespace"] = "blocked/app"
    assert projects.live_deny(KEY, client=client) == repo_policy.GitLabDecision(False, "deny entry")
    assert [r.url.path for r in requests] == ["/api/v4/projects/42"] * 2


@pytest.mark.parametrize("allow", [KEY, f"gitlab:{HOST}/BlockedOrg", f"{KEY},gitlab:{HOST}/BlockedOrg"])
def test_hard_deny_wins_every_event(make_client, monkeypatch, allow):
    monkeypatch.setenv("ARGUS_GITLAB_ALLOW", allow)
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "blockedorg")
    client, requests = make_client(lambda _: httpx.Response(200, json=payload("BlockedOrg/app")))
    for _ in range(2):
        assert not projects.live_deny(KEY, client=client).allowed
    assert len(requests) == 2


def test_mixed_case_hard_deny_overrides_exact_id(make_client, monkeypatch):
    monkeypatch.setenv("ARGUS_GITLAB_ALLOW", KEY)
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "BlOcKeD")
    client, requests = make_client(lambda _: httpx.Response(200, json=payload("bLoCkEd/sub/app")))
    for _ in range(2):
        assert projects.live_deny(KEY, client=client) == repo_policy.GitLabDecision(False, "hard deny owner")
    assert len(requests) == 2


@pytest.mark.parametrize("allow", [KEY, f"gitlab:{HOST}/allowed"])
def test_transfer_hard_deny_overrides_db_allow(make_client, monkeypatch, configured, allow):
    monkeypatch.setenv("ARGUS_GITLAB_ALLOW", allow)
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "blockedorg")
    save_policy(configured)
    data = payload()
    client, requests = make_client(lambda _: httpx.Response(200, json=data))
    assert projects.live_deny(KEY, client=client).allowed
    data["path_with_namespace"] = "BlockedOrg/app"
    assert projects.live_deny(KEY, client=client) == repo_policy.GitLabDecision(False, "hard deny owner")
    assert len(requests) == 2


def test_transfer_out_of_denied_group_allows_and_keeps_identity(make_client, monkeypatch, configured):
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "blocked")
    data = payload("blocked/app")
    client, requests = make_client(lambda _: httpx.Response(200, json=data))
    assert not projects.live_deny(KEY, client=client).allowed
    data["path_with_namespace"] = "allowed/app"
    assert projects.live_deny(KEY, client=client).allowed
    with closing(db.connect(configured)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM forge_projects").fetchone()[0] == 1
        assert db.forge_project(conn, KEY)["path_with_namespace"] == "allowed/app"
    assert len(requests) == 2


@pytest.mark.parametrize("source_path,status,allowed", [
    ("allowed/fork", 200, True), ("outside/fork", 200, False),
    ("blocked/fork", 200, False), ("allowed/fork", 404, False),
])
def test_fork_source_must_pass_live_policy(make_client, monkeypatch, source_path, status, allowed):
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "blocked")

    def handle(request):
        if request.url.path.endswith("/43"):
            return httpx.Response(status, json=payload(source_path, id=43))
        return httpx.Response(200, json=payload())

    client, requests = make_client(handle)
    decision = projects.live_deny_change(KEY, meta(RepoRef("gitlab", HOST, None, 43)), client=client)
    assert decision == repo_policy.GitLabDecision(allowed, "allowed" if allowed else "fork_source_denied")
    assert [r.url.path for r in requests] == ["/api/v4/projects/42", "/api/v4/projects/43"]


def test_fork_host_mismatch_denies_without_source_request(make_client):
    client, requests = make_client(lambda _: httpx.Response(200, json=payload()))
    result = projects.live_deny_change(KEY, meta(RepoRef("gitlab", "other.test", None, 42)), client=client)
    assert result == repo_policy.GitLabDecision(False, "fork_source_denied")
    assert len(requests) == 1


@pytest.mark.parametrize("source", [None, REF, RepoRef("gitlab", HOST, "old/path", 42)])
def test_non_fork_resolves_only_target(make_client, source):
    client, requests = make_client(lambda _: httpx.Response(200, json=payload()))
    assert projects.live_deny_change(KEY, meta(source), client=client).allowed
    assert len(requests) == 1


def test_denied_target_does_not_resolve_fork(make_client):
    client, requests = make_client(lambda _: httpx.Response(404))
    result = projects.live_deny_change(KEY, meta(RepoRef("gitlab", HOST, None, 43)), client=client)
    assert result == repo_policy.GitLabDecision(False, "lookup failed: not_found")
    assert len(requests) == 1


@pytest.mark.parametrize("status,reason", [
    (401, "auth"), (403, "forbidden"), (404, "not_found"), (500, "http"),
    (301, "redirect_refused"), (429, "rate_limited"),
])
def test_live_deny_http_failure(make_client, status, reason):
    client, _ = make_client(lambda _: httpx.Response(status, text=TOKEN, headers={"Location": "https://private.test/"}))
    assert projects.live_deny(KEY, client=client) == repo_policy.GitLabDecision(False, f"lookup failed: {reason}")


def test_live_deny_timeout(make_client):
    def handle(request):
        raise httpx.ReadTimeout(TOKEN, request=request)

    client, _ = make_client(handle)
    assert projects.live_deny(KEY, client=client) == repo_policy.GitLabDecision(False, "lookup failed: transport")


@pytest.mark.parametrize("data", [[], None, {}, payload(id=True), payload(id=42.0), payload(id="42"), payload(id=43),
    payload(path_with_namespace=None), payload(path_with_namespace=""), payload(archived=0),
    payload(default_branch=12), payload(visibility=False),
    {k: v for k, v in payload().items() if k != "path_with_namespace"},
    {k: v for k, v in payload().items() if k != "archived"},
])
def test_invalid_project_never_persists_or_allows(make_client, configured, data):
    client, _ = make_client(lambda _: httpx.Response(200, json=data))
    with pytest.raises(projects.GitLabProjectError):
        projects.resolve(REF, client=client)
    assert not projects.live_deny(KEY, client=client).allowed
    with closing(db.connect(configured)) as conn:
        assert db.forge_project(conn, KEY) is None


def test_malformed_json_denies_and_has_safe_error(make_client, caplog):
    caplog.set_level(logging.DEBUG)
    client, _ = make_client(lambda _: httpx.Response(200, text=TOKEN + " https://private.test/"))
    with pytest.raises(projects.GitLabProjectError) as error:
        projects.resolve(REF, client=client)
    assert not projects.live_deny(KEY, client=client).allowed
    assert TOKEN not in str(error.value) + caplog.text
    assert "https://private.test/" not in str(error.value) + caplog.text


@pytest.mark.parametrize("key", ["owner/repo", "gitlab:broken", f"gitlab:{HOST}#0", "gitlab:other.test#42"])
def test_bad_key_denies_without_request(make_client, key):
    client, requests = make_client(lambda _: pytest.fail("Unexpected request"))
    assert not projects.live_deny(key, client=client).allowed
    assert requests == []


def test_archived_project_denied(make_client):
    client, _ = make_client(lambda _: httpx.Response(200, json=payload(archived=True)))
    assert projects.live_deny(KEY, client=client) == repo_policy.GitLabDecision(False, "project archived")


def test_unconfigured_gitlab_denies_and_resolve_raises_typed_error(monkeypatch):
    monkeypatch.delenv("ARGUS_GITLAB_URL")
    with pytest.raises(projects.GitLabProjectError):
        projects.resolve(REF)
    assert not projects.live_deny(KEY).allowed


def test_missing_client_denies_and_resolve_raises_typed_error(monkeypatch):
    monkeypatch.setattr(projects, "shared_client", lambda: None)
    with pytest.raises(projects.GitLabProjectError, match="client is unavailable"):
        projects.resolve(REF)
    assert not projects.live_deny(KEY).allowed


def test_missing_token_denies(monkeypatch):
    monkeypatch.delenv("ARGUS_GITLAB_TOKEN")
    monkeypatch.setattr(projects, "shared_client", lambda: GitLabClient())
    assert not projects.live_deny(KEY).allowed


def test_resolve_uses_shared_client(make_client, monkeypatch):
    client, requests = make_client(lambda _: httpx.Response(200, json=payload()))
    monkeypatch.setattr(projects, "shared_client", lambda: client)
    assert projects.resolve(REF) == RepoRecord("allowed/app", False, "main", "private")
    assert len(requests) == 1


@pytest.mark.parametrize("failure", ["config", "upsert"])
def test_database_error_denies(make_client, monkeypatch, failure):
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError(TOKEN)

    monkeypatch.setattr(db, "get_config" if failure == "config" else "upsert_forge_project", fail)
    client, _ = make_client(lambda _: httpx.Response(200, json=payload()))
    assert not projects.live_deny(KEY, client=client).allowed


def test_invalid_allow_policy_denies(make_client, monkeypatch):
    monkeypatch.setenv("ARGUS_GITLAB_ALLOW", "invalid")
    client, requests = make_client(lambda _: httpx.Response(200, json=payload()))
    assert not projects.live_deny(KEY, client=client).allowed
    assert len(requests) == 1


def test_db_policy_is_read_again_each_event(make_client, configured):
    client, requests = make_client(lambda _: httpx.Response(200, json=payload()))
    assert projects.live_deny(KEY, client=client).allowed
    save_policy(configured, [KEY])
    assert not projects.live_deny(KEY, client=client).allowed
    save_policy(configured)
    assert projects.live_deny(KEY, client=client).allowed
    assert len(requests) == 3


def test_explicit_database_is_used_for_records_and_policy(make_client, configured, tmp_path):
    path = str(tmp_path / "override.db")
    db.init_db(path)
    save_policy(path, [KEY])
    client, _ = make_client(lambda _: httpx.Response(200, json=payload()))
    assert not projects.live_deny(KEY, client=client, db_path=path).allowed
    with closing(db.connect(path)) as conn:
        assert db.forge_project(conn, KEY) is not None
    with closing(db.connect(configured)) as conn:
        assert db.forge_project(conn, KEY) is None


@pytest.mark.parametrize("text", ["allowed/sub/app", f"gitlab:{HOST}/allowed/sub/app"])
def test_resolve_nested_path_to_id(make_client, text):
    client, requests = make_client(lambda _: httpx.Response(200, json={"id": 42}))
    assert projects.resolve_input(text, client=client) == KEY
    assert len(requests) == 1
    assert requests[0].url.raw_path == b"/api/v4/projects/allowed%2Fsub%2Fapp"


@pytest.mark.parametrize("status", [301, 404])
def test_old_path_fails_with_new_path_or_id_guidance(make_client, status):
    client, requests = make_client(lambda _: httpx.Response(status, text=TOKEN))
    with pytest.raises(projects.GitLabInputError, match="use the new path or the numeric id"):
        projects.resolve_input("old/app", client=client)
    assert len(requests) == 1


def test_canonical_input_passes_through_without_lookup(make_client):
    client, requests = make_client(lambda _: pytest.fail("Unexpected request"))
    assert projects.resolve_input(KEY, client=client) == KEY
    assert requests == []


@pytest.mark.parametrize("text", ["", "group", "42", "group//app", "group/../app", "group/app?x=1",
    "group/app#42", "group/app%2Fother", "group/app\\other", "group/app\n", "group/app ",
    "https://gitlab.example.test/group/app", "gitlab:other.test/group/app", "gitlab:other.test#42",
    f"gitlab:{HOST}#True", f"gitlab:{HOST}/group", "group/" + "a" * 256,
])
def test_invalid_input_never_requests(make_client, text):
    client, requests = make_client(lambda _: pytest.fail("Unexpected request"))
    with pytest.raises(projects.GitLabInputError):
        projects.resolve_input(text, client=client)
    assert requests == []


@pytest.mark.parametrize("data", [[], {}, {"id": True}, {"id": 0}, {"id": "42"}, {"id": 10**18}])
def test_input_invalid_response_id_raises(make_client, data):
    client, _ = make_client(lambda _: httpx.Response(200, json=data))
    with pytest.raises(projects.GitLabInputError):
        projects.resolve_input("allowed/app", client=client)


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_input_other_failures_have_fixed_safe_message(make_client, status):
    client, _ = make_client(lambda _: httpx.Response(status, text=TOKEN))
    with pytest.raises(projects.GitLabInputError, match="^GitLab project input could not be resolved$"):
        projects.resolve_input("allowed/app", client=client)


def test_rename_updates_one_row_and_checked_at(make_client, monkeypatch, configured):
    stamps = iter(["2026-11-01T12:00:00+00:00", "2026-11-02T12:00:00+00:00"])
    monkeypatch.setattr(db, "_now_iso", lambda: next(stamps))
    data = payload()
    client, requests = make_client(lambda _: httpx.Response(200, json=data))
    assert projects.resolve(REF, client=client).visibility == "private"
    with closing(db.connect(configured)) as conn:
        first = db.forge_project(conn, KEY)
    data.update(path_with_namespace="allowed/renamed", default_branch=None, visibility=None)
    record = projects.resolve(REF, client=client)
    assert record == RepoRecord("allowed/renamed", False, None)
    with closing(db.connect(configured)) as conn:
        row = db.forge_project(conn, KEY)
        assert conn.execute("SELECT COUNT(*) FROM forge_projects").fetchone()[0] == 1
        assert row["repo_key"] == first["repo_key"] == KEY
        assert row["path_with_namespace"] == "allowed/renamed"
        assert row["checked_at"] == "2026-11-02T12:00:00+00:00"
        assert row["checked_at"] != first["checked_at"]
    assert len(requests) == 2


def test_is_denied_still_has_no_io(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("Pure policy must not perform I/O")

    monkeypatch.setenv("ARGUS_DENY_OWNERS", "blockedorg")
    monkeypatch.setattr(db, "connect", fail)
    monkeypatch.setattr(GitLabClient, "get", fail)
    monkeypatch.setattr(projects, "resolve", fail)
    assert repo_policy.is_denied(KEY)


def test_untrusted_exception_reason_never_reaches_logs(make_client, monkeypatch, caplog):
    def fail(*args, **kwargs):
        raise GitLabDenied(TOKEN, reason="https://private.test/" + TOKEN)

    monkeypatch.setattr(projects, "resolve", fail)
    caplog.set_level(logging.DEBUG)
    decision = projects.live_deny(KEY)
    assert not decision.allowed
    assert TOKEN not in decision.reason + caplog.text
    assert "https://private.test/" not in decision.reason + caplog.text
