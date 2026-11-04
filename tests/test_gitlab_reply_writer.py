"""Reply writes use guarded routes and preserve uncertain outcomes."""

import json

import httpx
import pytest

from argus.forge.types import ChangeRef, RepoRef
from argus.gitlab import body, publisher, threads
from argus.gitlab.client import GitLabHTTPError, GitLabRateLimited
from argus.gitlab.pacing import pacing_for
from test_gitlab_client import configured as configured, make_client as make_client
from test_gitlab_writer import writer_factory as writer_factory
import test_gitlab_publisher as publishing_tests

publisher_setup = publishing_tests.setup

CHANGE = ChangeRef(RepoRef("gitlab", "gitlab.example.test", None, 42), 3)
THREAD = threads.GitLabThread("a" * 40, (), True, False, None, "b" * 40)
MARKER = "<!-- argus:reply=recheck evidence=123 -->"


@pytest.fixture
def setup(writer_factory, monkeypatch):
    monkeypatch.setattr(publisher, "_identities", {})
    monkeypatch.setattr(threads, "COUNTERS", threads.Counter())

    def make(handler):
        def handle(request):
            if request.url.path.endswith("/user"):
                return httpx.Response(200, json={"id": 7, "username": "bot"})
            return handler(request)
        writer, requests, clock = writer_factory(handle)
        return writer.client, requests, clock
    return make


def test_reply_neutralizes_commands_and_mentions_before_posting(setup):
    client, requests, _ = setup(lambda _: httpx.Response(201, json={"id": 9}))
    result = threads.reply(CHANGE, THREAD, "Hello @dev\n/close\n\n" + MARKER, client=client)
    assert (result.id, result.thread_id, result.anchored, result.placement) == (9, THREAD.id, False, "reply")
    post = requests[-1]
    assert post.method == "POST"
    assert post.url.path == f"/api/v4/projects/42/merge_requests/3/discussions/{THREAD.id}/notes"
    assert json.loads(post.content) == {"body": "Hello @\u200bdev\n\\/close\n\n" + MARKER}
    assert threads.COUNTERS["reply_posted"] == 1


def test_unsafe_reply_is_refused_before_any_request(setup, monkeypatch):
    client, requests, _ = setup(lambda _: pytest.fail("Unsafe request"))
    monkeypatch.setattr(threads, "neutralize_quick_actions", lambda text: text)
    with pytest.raises(body.UnsafeBodyError):
        threads.reply(CHANGE, THREAD, "/close\n\n" + MARKER, client=client)
    assert requests == []


@pytest.mark.parametrize("kind,evidence", [("Bad", "1"), ("reply", "a b"), ("", "1"), ("reply", "")])
def test_reply_marker_rejects_invalid_fields(kind, evidence):
    with pytest.raises(ValueError):
        body.reply_marker(kind, evidence)


def test_reply_marker_uses_only_last_newline_delimited_line():
    assert body.reply_marker("recheck", "123") == MARKER
    assert body.parse_reply_marker("Text\r\n" + MARKER + "\n ") == ("recheck", "123")
    assert body.parse_reply_marker(MARKER + "\nText") is None
    assert body.parse_reply_marker("Text\v" + MARKER) is None
    assert body.parse_reply_marker("<!-- argus:fp=123 -->") is None


@pytest.mark.parametrize("status,match", [(502, "reply"), (502, "root"), (503, "none"), (504, "other_bot")])
def test_ambiguous_reply_reconciles_only_its_bot_reply_marker(setup, status, match):
    def handle(request):
        if request.method == "POST":
            return httpx.Response(status)
        notes = [] if match == "none" else [{
            "id": 9, "author": {"id": 8 if match == "other_bot" else 7},
            "body": "Fixed\n\n" + ("<!-- argus:fp=123 -->" if match == "root" else MARKER),
        }]
        return httpx.Response(200, json=[{"id": THREAD.id, "notes": notes}])
    client, requests, _ = setup(handle)
    if match == "reply":
        assert threads.reply(CHANGE, THREAD, "Fixed\n\n" + MARKER, client=client).id == 9
    else:
        with pytest.raises(GitLabHTTPError) as caught:
            threads.reply(CHANGE, THREAD, "Fixed\n\n" + MARKER, client=client)
        assert caught.value.reason == "unknown_outcome"
    assert [r.method for r in requests] == ["GET", "POST", "GET"]


@pytest.mark.parametrize("operation", ["reply", "resolve"])
def test_deleted_discussion_is_missing(setup, operation):
    client, requests, _ = setup(lambda _: httpx.Response(404))
    args = ("Fixed\n\n" + MARKER,) if operation == "reply" else ()
    with pytest.raises(threads.ThreadMissing):
        getattr(threads, operation)(CHANGE, THREAD, *args, client=client)
    assert len(requests) == 2


@pytest.mark.parametrize("operation", ["reply", "resolve"])
def test_rate_limit_pauses_shared_pacer(setup, operation):
    client, requests, clock = setup(lambda _: httpx.Response(429, headers={"Retry-After": "60"}))
    args = ("Fixed\n\n" + MARKER,) if operation == "reply" else ()
    with pytest.raises(GitLabRateLimited):
        getattr(threads, operation)(CHANGE, THREAD, *args, client=client)
    before = clock.now
    pacing_for(client).acquire(7)
    assert clock.now >= before + 60
    assert sum(r.method == "PUT" for r in requests) == (operation == "resolve")


@pytest.mark.parametrize("status", [200, 403])
def test_resolve_confirms_discussion_or_reports_forbidden(setup, status):
    client, requests, clock = setup(lambda _: httpx.Response(status, json={
        "id": THREAD.id, "notes": [{"resolvable": True, "resolved": True}],
    }))
    assert threads.resolve(CHANGE, THREAD, client=client) == ("resolved" if status == 200 else "forbidden")
    assert requests[-1].method == "PUT"
    assert requests[-1].url.path.endswith(f"/discussions/{THREAD.id}")
    assert json.loads(requests[-1].content) == {"resolved": True}
    assert threads.COUNTERS["resolve_forbidden"] == (status == 403)
    for _ in range(5):
        pacing_for(client).acquire(7)
    assert clock.waits == []


@pytest.mark.parametrize("payload", [{}, {"id": "other", "notes": []},
    {"id": THREAD.id, "notes": [{"resolvable": True, "resolved": False}]},
    {"id": THREAD.id, "notes": []}])
def test_resolve_requires_confirmation(setup, payload):
    client, requests, _ = setup(lambda _: httpx.Response(200, json=payload))
    with pytest.raises(GitLabHTTPError):
        threads.resolve(CHANGE, THREAD, client=client)
    assert len(requests) == 2


def test_reply_and_publisher_share_rate_budget(publisher_setup, monkeypatch):
    from argus.gitlab import client as client_module

    setup = publisher_setup
    monkeypatch.setattr(client_module, "client", lambda: setup.client)
    times = []
    setup.server.before_write = lambda request: times.append(setup.clock.now)
    outcome = publishing_tests.run(setup, publishing_tests.document(4))
    assert outcome.status == "posted"
    assert len(times) == 5
    assert threads.reply(CHANGE, THREAD, "Fixed\n\n" + MARKER).placement == "reply"
    assert publisher.pacing_for(setup.client) is pacing_for(setup.client)
    assert times == [0, 0, 0, 0, 0, 2]

