import json
from dataclasses import FrozenInstanceError

import httpx
import pytest

from argus.gitlab.body import UnsafeBodyError, marker
from argus.gitlab.client import GitLabAuthError, GitLabDenied
from argus.gitlab.pacing import NotePacer, pacing_for
from argus.gitlab.writer import GitLabWriter, WriteResult
from test_gitlab_client import SENTINEL, configured as configured, make_client as make_client
from test_gitlab_pacing import FakeClock


DISCUSSION = "a" * 40
BODY = "Finding\n" + marker("finding-1")
POSITION = {"position_type": "text", "base_sha": "b" * 40, "start_sha": "c" * 40,
            "head_sha": "d" * 40, "old_path": "app.py", "new_path": "app.py", "new_line": 3}


@pytest.fixture
def writer_factory(make_client, monkeypatch):
    def make(handler):
        clock = FakeClock()
        monkeypatch.setattr("time.time", lambda: 1_800_000_000.0 + clock.now)
        client, requests = make_client(handler)
        client.pacing = NotePacer(30, clock=clock, sleep=clock.sleep)
        return GitLabWriter(client, 7), requests, clock
    return make


def write(writer, method, body=BODY):
    if method == "post_discussion":
        return writer.post_discussion(42, 3, body, POSITION)
    if method == "reply":
        return writer.reply(42, 3, DISCUSSION, body)
    return writer.post_note(42, 3, body)


@pytest.mark.parametrize("method", ["post_discussion", "post_note", "reply"])
@pytest.mark.parametrize("body", ["/approve", "Check @someone", "Check &#64;someone"])
def test_unsafe_body_spends_no_token_and_sends_no_request(writer_factory, method, body):
    writer, requests, clock = writer_factory(lambda _: httpx.Response(201, json={"id": 123}))
    with pytest.raises(UnsafeBodyError):
        write(writer, method, body)
    assert requests == []
    for _ in range(5):
        pacing_for(writer.client).acquire(7)
    assert clock.waits == []


@pytest.mark.parametrize("method,suffix", [("post_discussion", "discussions"), ("post_note", "notes"),
                                         ("reply", f"discussions/{DISCUSSION}/notes")])
def test_created_ids_and_payload(writer_factory, method, suffix):
    payload = {"id": DISCUSSION, "notes": [{"id": 123}]} if method == "post_discussion" else {"id": 123}
    writer, requests, _ = writer_factory(lambda _: httpx.Response(201, json=payload))
    result = write(writer, method)
    assert result == WriteResult("posted", 123, None if method == "post_note" else DISCUSSION, None)
    assert len(requests) == 1 and requests[0].method == "POST"
    assert requests[0].url.path == f"/api/v4/projects/42/merge_requests/3/{suffix}"
    assert json.loads(requests[0].content) == ({"body": BODY, "position": POSITION}
                                             if method == "post_discussion" else {"body": BODY})


def test_bad_position_fails_without_retry(writer_factory):
    writer, requests, _ = writer_factory(lambda _: httpx.Response(400))
    result = write(writer, "post_discussion")
    assert result.outcome == "failed" and "400" in result.reason
    assert len(requests) == 1


@pytest.mark.parametrize("twice", [False, True])
def test_rate_limit_retries_once_after_pause(writer_factory, twice):
    def handle(request):
        if twice or len(requests) == 1:
            return httpx.Response(429, headers={"Retry-After": "60", "RateLimit-Name": "notes_create"})
        return httpx.Response(201, json={"id": 123})

    writer, requests, clock = writer_factory(handle)
    result = write(writer, "post_note")
    assert (result.outcome, result.reason) == (("failed", "rate_limited") if twice else ("posted", None))
    assert len(requests) == 2 and clock.waits == [60.0]


@pytest.mark.parametrize("status,error", [(401, GitLabAuthError), (403, GitLabDenied), (404, GitLabDenied)])
def test_auth_and_denial_propagate(writer_factory, status, error):
    writer, requests, _ = writer_factory(lambda _: httpx.Response(status))
    with pytest.raises(error):
        write(writer, "post_note")
    assert len(requests) == 1


@pytest.mark.parametrize("failure", [500, 599, "transport"])
@pytest.mark.parametrize("case,http_expected,transport_expected", [
    ("found", "already_posted", "already_posted"), ("absent", "failed", "unknown"),
    ("other_author", "failed", "unknown"), ("read_failed", "unknown", "unknown"),
    ("no_marker", "unknown", "unknown"), ("page_two", "already_posted", "already_posted"),
])
def test_ambiguous_write_rereads_without_reposting(writer_factory, failure, case, http_expected, transport_expected):
    def handle(request):
        if request.method == "POST":
            if failure == "transport":
                raise httpx.ReadTimeout(SENTINEL, request=request)
            return httpx.Response(failure)
        assert request.url.path.endswith("/discussions")
        assert request.url.params["per_page"] == "100"
        if case == "read_failed":
            return httpx.Response(502)
        if case == "page_two" and request.url.params.get("page") != "2":
            return httpx.Response(200, json=[], headers={"X-Next-Page": "2"})
        notes = [] if case == "absent" else [{"id": 123, "body": BODY,
                                            "author": {"id": 8 if case == "other_author" else 7}}]
        return httpx.Response(200, json=[{"id": DISCUSSION, "notes": notes}])

    writer, requests, _ = writer_factory(handle)
    result = write(writer, "post_discussion", "No marker" if case == "no_marker" else BODY)
    expected = transport_expected if failure == "transport" else http_expected
    assert result.outcome == expected
    assert sum(request.method == "POST" for request in requests) == 1
    assert SENTINEL not in (result.reason or "")
    if expected == "already_posted":
        assert (result.note_id, result.discussion_id) == (123, DISCUSSION)
    if case == "page_two":
        assert [r.url.params.get("page") for r in requests[1:]] == [None, "2"]
    if case == "no_marker":
        assert len(requests) == 1


@pytest.mark.parametrize("method", ["post_note", "reply"])
def test_note_and_reply_also_reconcile(writer_factory, method):
    def handle(request):
        if request.method == "POST":
            return httpx.Response(500)
        return httpx.Response(200, json=[{"id": DISCUSSION, "notes": [
            {"id": 123, "body": BODY, "author": {"id": 7}},
        ]}])
    writer, requests, _ = writer_factory(handle)
    assert write(writer, method) == WriteResult("already_posted", 123, DISCUSSION, None)
    assert [r.method for r in requests] == ["POST", "GET"]


@pytest.mark.parametrize("target_found", [False, True])
def test_reply_reconciliation_requires_marker_in_target_discussion(writer_factory, target_found):
    def handle(request):
        if request.method == "POST":
            return httpx.Response(500)
        return httpx.Response(200, json=[
            {"id": "b" * 40, "notes": [{"id": 123, "body": BODY, "author": {"id": 7}}]},
            {"id": DISCUSSION, "notes": [
                {"id": 456, "body": BODY, "author": {"id": 7}},
            ] if target_found else []},
        ])

    writer, requests, _ = writer_factory(handle)
    result = write(writer, "reply")
    assert result.outcome == ("already_posted" if target_found else "failed")
    assert (result.note_id, result.discussion_id) == ((456, DISCUSSION) if target_found else (None, None))
    assert [request.method for request in requests] == ["POST", "GET"]


@pytest.mark.parametrize("method", ["post_discussion", "post_note", "reply"])
@pytest.mark.parametrize("failure,expected", [(500, "failed"), (599, "failed"), ("transport", "unknown")])
def test_complete_read_without_marker_preserves_write_uncertainty(writer_factory, method, failure, expected):
    def handle(request):
        if request.method == "POST":
            if failure == "transport":
                raise httpx.ReadTimeout(SENTINEL, request=request)
            return httpx.Response(failure)
        return httpx.Response(200, json=[])

    writer, requests, _ = writer_factory(handle)
    result = write(writer, method)
    assert result.outcome == expected
    assert (result.note_id, result.discussion_id) == (None, None)
    assert [request.method for request in requests] == ["POST", "GET"]


@pytest.mark.parametrize("payload", [None, [], {}, {"id": "bad"}, {"id": 123, "notes": []}])
@pytest.mark.parametrize("method", ["post_discussion", "post_note", "reply"])
def test_unparseable_success_is_unknown(writer_factory, payload, method):
    if method != "post_discussion" and payload == {"id": 123, "notes": []}:
        payload = {"id": None}
    writer, requests, _ = writer_factory(lambda _: httpx.Response(201, json=payload))
    assert write(writer, method).outcome == "unknown"
    assert len(requests) == 1


def test_write_result_redacts_reason_and_is_frozen():
    result = WriteResult("failed", None, None, "Rejected " + SENTINEL)
    assert SENTINEL not in result.reason and "[redacted]" in result.reason
    with pytest.raises(FrozenInstanceError):
        result.reason = "changed"
