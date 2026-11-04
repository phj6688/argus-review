"""Pin discussion parsing and stable bot identity to recorded GitLab responses."""

import json
import os
import sqlite3
from contextlib import closing
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import httpx
import pytest

from argus.api import db
from argus.forge.errors import ForgeDenied
from argus.forge.keys import gitlab_key
from argus.forge.types import ChangeRef, RepoRef
from argus.gitlab import threads
from argus.gitlab.client import GitLabClient, GitLabHTTPError

FIXTURES = Path(__file__).parent / "fixtures/gitlab/spike_b"
HOST = "gitlab.example.test"
KEY = gitlab_key(HOST, 1)
CHANGE = ChangeRef(RepoRef("gitlab", HOST, None, 1), 107)
DISCUSSIONS = "/api/v4/projects/1/merge_requests/107/discussions"


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())["response"]["body"]


def discussion():
    return fixture("b13_changed_push")[0]


def bot_note():
    return threads.ThreadNote(241, 2, "argus-bot", "changed", "2026-11-02T22:26:52.270Z",
                              False)


@pytest.fixture(autouse=True)
def configured(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.startswith(("ARGUS_GITLAB_", "ARGUS_CONTEXT_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("ARGUS_GITLAB_URL", f"https://{HOST}")
    monkeypatch.setenv("ARGUS_GITLAB_TOKEN", "gl" + "pat-" + "x" * 20)
    monkeypatch.setenv("ARGUS_DB_PATH", str(tmp_path / "threads.db"))
    monkeypatch.setattr(GitLabClient, "retry_delay", 0)
    threads.COUNTERS.clear()
    yield
    threads.COUNTERS.clear()


@pytest.fixture
def make_client():
    clients = []

    def make(*pages, raw=None):
        requests = []

        def handle(request):
            requests.append(request)
            assert request.method == "GET"
            assert request.url.path.startswith("/api/v4/projects/")
            assert request.url.path == DISCUSSIONS
            assert set(request.url.params) <= {"page", "per_page"}
            assert request.url.params["per_page"] == "100"
            page = int(request.url.params.get("page", "1"))
            if raw is not None:
                return httpx.Response(200, content=raw)
            headers = {"X-Next-Page": str(page + 1) if page < len(pages) else ""}
            return httpx.Response(200, json=pages[page - 1], headers=headers)

        client = GitLabClient(transport=httpx.MockTransport(handle))
        clients.append(client)
        return client, requests

    yield make
    for client in clients:
        client.close()


@pytest.fixture
def database():
    db.init_db()
    with closing(db.connect()) as conn:
        yield conn


def identity(conn, *, login="argus-bot", user_id=2, repo=KEY, seen="2026-11-01T00:00:00Z"):
    conn.execute(
        "INSERT INTO publishing_identities (repo, bot_login, first_seen_at, bot_user_id) "
        "VALUES (?, ?, ?, ?)", (repo, login, seen, user_id),
    )
    conn.commit()


def test_spike_b_threads_drop_system_notes_and_keep_api_order(make_client):
    data = fixture("b13_changed_push")
    client, requests = make_client(data)
    actual = threads.list_threads(CHANGE, client=client)
    assert [thread.id for thread in actual] == [item["id"] for item in data[:2]]
    assert [[note.id for note in thread.notes] for thread in actual] == [[241, 243, 244], [242]]
    for thread, item in zip(actual, data):
        kept = [note for note in item["notes"] if not note["system"]]
        assert thread.notes == tuple(threads.ThreadNote(
            note["id"], note["author"]["id"], note["author"]["username"],
            note["body"], note["created_at"], False,
        ) for note in kept)
        assert thread.resolvable is True
        assert thread.resolved is False
        assert thread.resolved_by is None
        assert thread.head_sha == kept[0]["position"]["head_sha"]
    assert len(requests) == 1
    with pytest.raises(FrozenInstanceError):
        actual[0].id = "0" * 40
    with pytest.raises(FrozenInstanceError):
        actual[0].notes[0].username = "renamed"


def test_spike_b_resolved_thread_maps_resolver_user_id(make_client):
    data = fixture("b12b_outdated_argus-bot_by_argus-bot")
    client, _ = make_client([data])
    thread, = threads.list_threads(CHANGE, client=client)
    assert thread.id == data["id"]
    assert [note.id for note in thread.notes] == [317]
    assert thread.resolvable is True
    assert thread.resolved is True
    assert thread.resolved_by == 2
    assert type(thread.resolved_by) is int
    assert thread.head_sha == data["notes"][0]["position"]["head_sha"]


@pytest.mark.parametrize(("resolvable", "resolved", "resolver"), [
    ((True, True, False), (True, True, False), 4),
    ((True, True, True), (True, True, True), 4),
    ((True, True, True), (True, False, True), None),
    ((False, False, False), (True, True, True), None),
])
def test_resolution_uses_only_resolvable_kept_notes(make_client, resolvable, resolved, resolver):
    data = discussion()
    for index, note in enumerate(data["notes"][:3]):
        note.update(resolvable=resolvable[index], resolved=resolved[index],
                    resolved_by={"id": [2, 4, 9][index]})
    data["notes"][2].pop("resolved_by")
    data["notes"][3].update(resolvable=True, resolved=False, resolved_by={"id": 10})
    data.update(resolvable=not any(resolvable), resolved=not all(resolved))
    client, _ = make_client([data])
    thread, = threads.list_threads(CHANGE, client=client)
    assert thread.resolvable is any(resolvable)
    assert thread.resolved is (any(resolvable) and all(
        done for can_resolve, done in zip(resolvable, resolved) if can_resolve
    ))
    assert thread.resolved_by == resolver


@pytest.mark.parametrize("resolver", [None, {}, {"id": "2"}, {"id": True}])
def test_resolved_thread_without_numeric_resolver_has_no_resolved_by(make_client, resolver):
    data = fixture("b12b_outdated_argus-bot_by_argus-bot")
    data["notes"][0]["resolved_by"] = resolver
    client, _ = make_client([data])
    thread, = threads.list_threads(CHANGE, client=client)
    assert thread.resolved is True
    assert thread.resolved_by is None


@pytest.mark.parametrize("position", [None, {}, {"head_sha": 42}, "invalid"])
def test_head_sha_comes_only_from_first_kept_note(make_client, position):
    data = discussion()
    data["notes"].insert(0, data["notes"].pop())
    data["notes"][1]["position"] = position
    client, _ = make_client([data])
    thread, = threads.list_threads(CHANGE, client=client)
    assert thread.notes[0].id == 241
    assert thread.head_sha is None
    assert threads.is_outdated(thread, "current") is False


def test_empty_discussions_are_omitted(make_client):
    client, _ = make_client([], [{"id": "a" * 40, "notes": []}])
    assert threads.list_threads(CHANGE, client=client) == []


def test_bot_id_survives_username_change(database):
    identity(database, login="old-bot-name")
    assert threads.is_bot_note(replace(bot_note(), username="new-bot-name"), KEY,
                               conn=database) is True
    assert threads.COUNTERS["bot_id_missing"] == 0


def test_human_with_old_bot_username_is_not_bot(database):
    identity(database)
    assert threads.is_bot_note(replace(bot_note(), author_id=4), KEY, conn=database) is False
    assert threads.COUNTERS["bot_id_missing"] == 0


def test_missing_bot_id_falls_back_to_casefolded_login_and_counts(database):
    identity(database, login="ARGUS-BOT", user_id=None)
    assert threads.is_bot_note(bot_note(), KEY, conn=database) is True
    assert threads.COUNTERS["bot_id_missing"] == 1
    assert threads.is_bot_note(replace(bot_note(), username="human"), KEY, conn=database) is False
    assert threads.COUNTERS["bot_id_missing"] == 2


def test_missing_identity_returns_false_and_counts_fallback(database):
    assert threads.is_bot_note(bot_note(), KEY, conn=database) is False
    assert threads.COUNTERS["bot_id_missing"] == 1


def test_bot_login_uses_newest_identity_for_this_repo(database):
    identity(database, login="new-bot", user_id=None, seen="2026-11-03T00:00:00Z")
    identity(database, login="old-bot", user_id=None)
    identity(database, login="other-bot", user_id=None, repo=gitlab_key(HOST, 2),
             seen="2026-11-04T00:00:00Z")
    assert db.bot_login(database, KEY) == "new-bot"
    assert threads.is_bot_note(replace(bot_note(), username="NEW-BOT"), KEY,
                               conn=database) is True
    assert threads.is_bot_note(replace(bot_note(), username="old-bot"), KEY,
                               conn=database) is False


@pytest.mark.parametrize("user_id", [2, None])
def test_bot_note_closes_only_internally_opened_connection(database, monkeypatch, user_id):
    identity(database, user_id=user_id)
    owned = db.connect()
    monkeypatch.setattr(db, "connect", lambda: owned)
    assert threads.is_bot_note(bot_note(), KEY) is True
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        owned.execute("SELECT 1")
    assert threads.is_bot_note(bot_note(), KEY, conn=database) is True
    assert database.execute("SELECT 1").fetchone()[0] == 1


def test_bot_note_closes_owned_connection_on_lookup_error(database, monkeypatch):
    owned = db.connect()
    monkeypatch.setattr(db, "connect", lambda: owned)

    def fail(*args):
        raise sqlite3.OperationalError("lookup failed")

    monkeypatch.setattr(db, "bot_user_id", fail)
    with pytest.raises(sqlite3.OperationalError, match="lookup failed"):
        threads.is_bot_note(bot_note(), KEY)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        owned.execute("SELECT 1")


def test_pagination_reads_all_250_discussions_through_guarded_client(make_client):
    data = [discussion() | {"id": f"{index:040x}"} for index in range(250)]
    client, requests = make_client(data[:100], data[100:200], data[200:])
    actual = threads.list_threads(CHANGE, client=client)
    assert [thread.id for thread in actual] == [item["id"] for item in data]
    assert [request.url.params.get("page", "1") for request in requests] == ["1", "2", "3"]
    assert all(request.url.params["per_page"] == "100" for request in requests)
    assert all(set(request.url.params) <= {"page", "per_page"} for request in requests)
    assert all(request.url.path.startswith("/api/v4/projects/") for request in requests)


def test_host_mismatch_is_denied_before_request(make_client):
    client, requests = make_client([])
    change = ChangeRef(RepoRef("gitlab", "other.example.test", None, 1), 107)
    with pytest.raises(ForgeDenied, match="host"):
        threads.list_threads(change, client=client)
    assert requests == []


def test_missing_configuration_is_denied_before_request(make_client, monkeypatch):
    client, requests = make_client([])
    monkeypatch.delenv("ARGUS_GITLAB_URL")
    with pytest.raises(ForgeDenied, match="not configured"):
        threads.list_threads(CHANGE, client=client)
    assert requests == []


def test_default_client_uses_same_guarded_connection(make_client, monkeypatch):
    from argus.gitlab import client as client_module

    client, requests = make_client([discussion()])
    monkeypatch.setattr(client_module, "client", lambda: client)
    assert len(threads.list_threads(CHANGE)) == 1
    assert len(requests) == 1


@pytest.mark.parametrize("page", [None, {}, "invalid", 1, [None], [True],
                                     [{"id": "a" * 40}], [{"notes": []}],
                                     [{"id": 1, "notes": []}],
                                     [{"id": "a" * 39, "notes": []}],
                                     [{"id": "g" * 40, "notes": []}],
                                     [{"id": "a" * 40 + "\n", "notes": []}],
                                     [{"id": "a" * 40, "notes": {}}]])
def test_invalid_page_and_discussion_shapes_raise_http_error(make_client, page):
    client, _ = make_client(page)
    with pytest.raises(GitLabHTTPError, match="^Invalid GitLab threads response$") as error:
        threads.list_threads(CHANGE, client=client)
    assert error.value.reason == "http"


@pytest.mark.parametrize(("field", "value"), [
    ("id", None), ("id", "241"), ("id", True),
    ("author", None), ("author", []), ("author", {}),
    ("author.id", "2"), ("author.id", True), ("author.id", None),
    ("author.username", None), ("author.username", 42),
    ("body", None), ("body", 42), ("created_at", None), ("created_at", 42),
    ("system", None), ("system", 0), ("system", "false"),
])
def test_invalid_note_fields_raise_http_error(make_client, field, value):
    data = discussion()
    target = data["notes"][0]
    if field.startswith("author."):
        target = target["author"]
        field = field.split(".")[1]
    target[field] = value
    client, _ = make_client([data])
    with pytest.raises(GitLabHTTPError, match="^Invalid GitLab threads response$") as error:
        threads.list_threads(CHANGE, client=client)
    assert error.value.reason == "http"


@pytest.mark.parametrize("field", ["id", "author", "body", "created_at", "system"])
def test_missing_required_note_fields_raise_http_error(make_client, field):
    data = discussion()
    data["notes"][0].pop(field)
    client, _ = make_client([data])
    with pytest.raises(GitLabHTTPError, match="Invalid GitLab threads response"):
        threads.list_threads(CHANGE, client=client)


@pytest.mark.parametrize("note", [None, [], "invalid", True])
def test_non_object_notes_raise_http_error(make_client, note):
    client, _ = make_client([discussion() | {"notes": [note]}])
    with pytest.raises(GitLabHTTPError, match="Invalid GitLab threads response"):
        threads.list_threads(CHANGE, client=client)


def test_system_notes_are_validated_before_being_dropped(make_client):
    data = discussion()
    data["notes"][3]["author"]["id"] = True
    client, _ = make_client([data])
    with pytest.raises(GitLabHTTPError, match="Invalid GitLab threads response"):
        threads.list_threads(CHANGE, client=client)


def test_invalid_json_raises_threads_http_error(make_client):
    client, _ = make_client(raw=b"not json")
    with pytest.raises(GitLabHTTPError, match="^Invalid GitLab threads response$") as error:
        threads.list_threads(CHANGE, client=client)
    assert error.value.reason == "http"


def test_b13_outdated_contract_uses_recorded_untouched_head(make_client):
    data = fixture("b13_changed_push")
    forbidden = {"outdated", "original_position", "line_code", "change_position"}
    for item in data:
        for note in item["notes"]:
            assert not forbidden.intersection(note)
            assert not forbidden.intersection(note.get("position") or {})
    old_head = data[0]["notes"][0]["position"]["head_sha"]
    newer_head = data[1]["notes"][0]["position"]["head_sha"]
    assert old_head != newer_head
    assert all(note["position"]["head_sha"] == old_head for note in data[0]["notes"])
    client, _ = make_client(data)
    changed, untouched = threads.list_threads(CHANGE, client=client)
    assert changed.head_sha == old_head
    assert untouched.head_sha == newer_head
    assert threads.is_outdated(changed, newer_head) is True
    assert threads.is_outdated(untouched, newer_head) is False
