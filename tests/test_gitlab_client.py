"""Exercise GitLab transport boundaries without making network requests."""

import gzip
import importlib
import io
import logging
import os
import pickle
import threading
import tracemalloc
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from argus import github
from argus.errors import HeadMoved, classify_error
from argus.forge.errors import ForgeAuthError, ForgeDenied, ForgeError, ForgeRateLimited
from argus.gitlab import guard
from argus.gitlab.client import (
    GitLabAuthError,
    GitLabClient,
    GitLabDenied,
    GitLabHTTPError,
    GitLabRateLimited,
)

module = importlib.import_module("argus.gitlab.client")
FAKE_PREFIX = "gl" + "pat-"
SENTINEL = FAKE_PREFIX + "s" * 24
ORIGIN = "https://gitlab.example.test"
NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    for name in list(os.environ):
        if name.startswith("ARGUS_GITLAB_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("ARGUS_GITLAB_URL", ORIGIN)
    monkeypatch.setenv("ARGUS_GITLAB_TOKEN", SENTINEL)
    monkeypatch.setattr(GitLabClient, "retry_delay", 0)
    module.close_client()
    yield
    module.close_client()


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


@pytest.mark.parametrize("status", [300, 301, 302, 303, 305, 307, 308, 399])
@pytest.mark.parametrize("location", [ORIGIN + "/elsewhere", "https://other.test/", "https://["])
def test_redirects_are_refused_without_reading_location(make_client, status, location):
    client, requests = make_client(lambda _: httpx.Response(status, headers={"Location": location}))
    with pytest.raises(GitLabDenied) as caught:
        client.get("project", project_id=7)
    assert caught.value.reason == "redirect_refused"
    assert len(requests) == 1
    assert requests[0].url.host == "gitlab.example.test"


def test_304_is_returned(make_client):
    client, requests = make_client(lambda _: httpx.Response(304, headers={"ETag": '"v1"'}))
    response = client.get("project", project_id=7)
    assert response.status_code == 304
    assert response.headers["etag"] == '"v1"'
    assert response.content == b""
    assert len(requests) == 1
    assert response.request is requests[0]


def test_success_response_preserves_request(make_client):
    client, requests = make_client(lambda _: httpx.Response(200, json={"id": 7}))
    response = client.get("project", project_id=7)
    assert response.url == httpx.URL(ORIGIN + "/api/v4/projects/7")
    assert response.request is requests[0]
    assert response.raise_for_status() is response


@pytest.mark.parametrize("page,expected", [("2", 2), ("abc", 1), ("../x", 1), ("0", 1),
                                         ("02", 1), ("1000000", 1), ("2&page=3", 1)])
def test_link_pagination_rebuilds_the_configured_route(make_client, page, expected):
    def handle(request):
        headers = {} if request.url.params.get("page") else {
            "Link": f'<https://other.test/wrong?state=closed&per_page=1&page={page}>; rel="next"',
        }
        return httpx.Response(200, json=[], headers=headers)

    client, requests = make_client(handle)
    query = {"state": "opened", "per_page": 20}
    assert len(list(client.pages("mr_list", project_id=7, query=query))) == expected
    assert query == {"state": "opened", "per_page": 20}
    for request in requests:
        assert request.url.host == "gitlab.example.test"
        assert request.url.path == "/api/v4/projects/7/merge_requests"
        assert request.url.params["state"] == "opened"
        assert request.url.params["per_page"] == "20"
    if expected == 2:
        assert requests[-1].url.params["page"] == "2"


@pytest.mark.parametrize("next_page,expected", [("3", 2), ("2", 1), ("1", 1), ("", 1), ("abc", 1)])
def test_next_page_header_wins_and_stops_at_empty_or_non_increasing(make_client, next_page, expected):
    def handle(request):
        value = next_page if request.url.params["page"] == "2" else ""
        return httpx.Response(200, json=[], headers={
            "X-Next-Page": value, "Link": '<https://other.test/?page=4>; rel="next"',
        })

    client, requests = make_client(handle)
    assert len(list(client.pages("mr_list", project_id=7, query={"page": 2}))) == expected
    if expected == 2:
        assert requests[-1].url.params["page"] == "3"


def test_pagination_has_a_hard_cap(make_client):
    def handle(request):
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(200, json=[], headers={"X-Next-Page": str(page + 1)})

    client, requests = make_client(handle)
    with pytest.raises(GitLabHTTPError) as caught:
        list(client.pages("mr_list", project_id=7))
    assert caught.value.reason == "too_many_pages"
    assert len(requests) == 1000


@pytest.mark.parametrize("case", [200, 401, 403, 404, 429, 500, 422, "transport", "large", "redirect"])
def test_tokens_stay_in_headers_and_out_of_diagnostics(make_client, monkeypatch, caplog, case):
    monkeypatch.setenv("ARGUS_GITLAB_MAX_RESPONSE_BYTES", "1024")
    for logger in ("argus", "httpx", "httpcore"):
        caplog.set_level(logging.DEBUG, logger=logger)

    def handle(request):
        if case == "transport":
            raise httpx.ConnectError("connection " + SENTINEL, request=request)
        if case == "large":
            return httpx.Response(200, content=(SENTINEL * 100).encode())
        if case == "redirect":
            return httpx.Response(302, headers={"Location": "https://other.test/" + SENTINEL})
        return httpx.Response(case, json={"error": SENTINEL})

    client, requests = make_client(handle)
    if case == 200:
        assert client.get("project", project_id=7).json() == {"error": SENTINEL}
    else:
        with pytest.raises(ForgeError) as caught:
            client.get("project", project_id=7)
        assert SENTINEL not in str(caught.value)
        assert SENTINEL not in repr(vars(caught.value))
        assert not hasattr(caught.value, "request")
        assert not hasattr(caught.value, "response")
    assert SENTINEL not in repr(client)
    assert SENTINEL not in str(client)
    assert all(SENTINEL not in record.getMessage() for record in caplog.records)
    assert requests
    for request in requests:
        assert request.headers["PRIVATE-TOKEN"] == SENTINEL
        assert request.headers["Accept"] == "application/json"
        assert SENTINEL not in str(request.url)
        assert request.url.host == "gitlab.example.test"


@pytest.mark.parametrize("bundle", [None, "/custom/ca.pem"])
def test_tls_and_environment_configuration(monkeypatch, bundle):
    if bundle:
        monkeypatch.setenv("ARGUS_GITLAB_CA_BUNDLE", bundle)
    monkeypatch.setenv("ARGUS_GITLAB_TIMEOUT_S", "12.5")
    real_client = httpx.Client
    options = []

    def recording_client(**kwargs):
        options.append(kwargs.copy())
        return real_client(**(kwargs | {"verify": True}))

    monkeypatch.setattr(httpx, "Client", recording_client)
    client = GitLabClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    client.close()
    assert len(options) == 1
    assert options[0]["verify"] == (bundle if bundle else True)
    assert options[0]["trust_env"] is False
    assert options[0]["follow_redirects"] is False
    assert options[0]["timeout"] == 12.5


def test_tls_verification_cannot_be_disabled_in_package():
    for path in Path(guard.__file__).parent.rglob("*.py"):
        source = path.read_text()
        for forbidden in ("verify=False", "verify = False", "ssl._create_unverified_context", "CERT_NONE"):
            assert forbidden not in source, path


@pytest.mark.parametrize("headers,reset", [
    ({"Retry-After": "60", "RateLimit-Reset": str(int(NOW + 500)), "RateLimit-Name": "notes_create"}, NOW + 60),
    ({"Retry-After": "Fri, 15 Jan 2027 08:02:00 GMT"}, NOW + 120),
    ({"RateLimit-Reset": str(int(NOW + 90))}, NOW + 90),
    ({}, NOW + 60),
    ({"Retry-After": "nan", "RateLimit-Reset": "inf"}, NOW + 60),
    ({"Retry-After": "-1", "RateLimit-Reset": str(int(NOW + 90))}, NOW + 90),
    ({"RateLimit-Reset": "-1"}, NOW + 60),
    ({"Retry-After": "invalid", "RateLimit-Reset": "-1"}, NOW + 60),
    ({"RateLimit-Reset": "0"}, NOW + 60),
    ({"Retry-After": "9" * 400, "RateLimit-Reset": "9" * 400}, NOW + 60),
])
def test_rate_limit_pauses_only_this_client_and_fails_fast(make_client, monkeypatch, headers, reset):
    monkeypatch.setattr(module.time, "time", lambda: NOW)
    github_pause = github.pause_until()
    client, requests = make_client(lambda _: httpx.Response(429, headers=headers))
    for _ in range(2):
        with pytest.raises(GitLabRateLimited) as caught:
            client.get("user")
        assert caught.value.reset_at == reset
    assert client.pause_until() == reset
    assert len(requests) == 1
    assert github.pause_until() == github_pause
    fresh, _ = make_client(lambda _: httpx.Response(200, json={}))
    assert fresh.pause_until() == 0.0
    assert fresh.quota_snapshot() == {}
    assert fresh.get("user").status_code == 200
    monkeypatch.setattr(module.time, "time", lambda: reset + 1)
    with pytest.raises(GitLabRateLimited):
        client.get("user")
    assert len(requests) == 2


def test_quota_retains_only_valid_last_seen_headers(make_client):
    headers = iter([
        {"RateLimit-Limit": "100", "RateLimit-Remaining": "90", "RateLimit-Reset": "1800000060",
         "RateLimit-Observed": "10", "RateLimit-Name": "authenticated"},
        {"RateLimit-Limit": "nan", "RateLimit-Remaining": "80", "RateLimit-Reset": "inf",
         "RateLimit-Observed": "1.5"}, {},
    ])
    client, _ = make_client(lambda _: httpx.Response(200, headers=next(headers)))
    client.get("user")
    expected = {"limit": 100, "remaining": 90, "reset": 1800000060, "observed": 10, "name": "authenticated"}
    assert client.quota_snapshot() == expected
    snapshot = client.quota_snapshot()
    snapshot["remaining"] = 0
    assert client.quota_snapshot() == expected
    client.get("user")
    client.get("user")
    assert client.quota_snapshot() == expected | {"remaining": 80}


@pytest.mark.parametrize("key", ["limit", "remaining", "reset", "observed"])
def test_quota_ignores_negative_values_and_accepts_zero(make_client, key):
    values = iter(["10", "-5", "0"])
    client, _ = make_client(lambda _: httpx.Response(200, headers={"RateLimit-" + key: next(values)}))
    client.get("user")
    assert client.quota_snapshot() == {key: 10}
    client.get("user")
    assert client.quota_snapshot() == {key: 10}
    client.get("user")
    assert client.quota_snapshot() == {key: 0}


class TrackedStream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.read_count = 0
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk

    def close(self):
        self.closed = True


@pytest.mark.parametrize("length,chunks,reads", [
    ("1025", [b"x" * 1025], 0),
    (None, [b"x" * 1024, b"x", b"unread"], 2),
    ("1", [b"x" * 1025, b"unread"], 1),
])
def test_response_cap_closes_stream_without_reading_more(make_client, monkeypatch, length, chunks, reads):
    monkeypatch.setenv("ARGUS_GITLAB_MAX_RESPONSE_BYTES", "1024")
    stream = TrackedStream(chunks)
    headers = {"Content-Length": length} if length else {}
    client, _ = make_client(lambda _: httpx.Response(200, headers=headers, stream=stream))
    with pytest.raises(GitLabHTTPError) as caught:
        client.get("user")
    assert caught.value.reason == "response_too_large"
    assert stream.closed
    assert stream.read_count == reads


def test_response_exactly_at_cap_succeeds(make_client, monkeypatch):
    monkeypatch.setenv("ARGUS_GITLAB_MAX_RESPONSE_BYTES", "1024")
    stream = TrackedStream([b"x" * 512, b"x" * 512])
    client, _ = make_client(lambda _: httpx.Response(200, headers={"Content-Length": "1024"}, stream=stream))
    assert client.get("user").content == b"x" * 1024
    assert stream.closed


def test_compressed_metadata_counts_toward_response_cap(make_client, monkeypatch):
    monkeypatch.setenv("ARGUS_GITLAB_MAX_RESPONSE_BYTES", "1024")
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="x" * 2048, mode="wb", fileobj=buffer) as compressed:
        compressed.write(b"ok")
    body = buffer.getvalue()
    assert len(body) > 1024
    assert gzip.decompress(body) == b"ok"
    stream = TrackedStream([body[:512], body[512:1025], body[1025:]])
    client, requests = make_client(lambda _: httpx.Response(
        200, headers={"Content-Encoding": "gzip"}, stream=stream,
    ))
    with pytest.raises(GitLabHTTPError) as caught:
        client.get("user")
    assert caught.value.reason == "response_too_large"
    assert len(requests) == 1
    assert stream.read_count == 2
    assert stream.closed


@pytest.mark.parametrize("method,status", [("GET", 304), ("GET", 204), ("HEAD", 200),
                                          ("GET", 100), ("GET", 199)])
@pytest.mark.parametrize("headers", [{"Content-Length": "2048"}, {"Content-Encoding": "gzip"},
                                   {"Content-Encoding": "unsupported"}])
def test_bodyless_responses_skip_body_validation(make_client, monkeypatch, method, status, headers):
    monkeypatch.setenv("ARGUS_GITLAB_MAX_RESPONSE_BYTES", "1024")
    stream = TrackedStream([b""])
    client, requests = make_client(lambda _: httpx.Response(status, headers=headers, stream=stream))
    if status < 200:
        with pytest.raises(GitLabHTTPError) as caught:
            client.request(method, "user")
        assert caught.value.status_code == status
        assert caught.value.reason == "http"
    else:
        response = client.request(method, "user")
        assert response.status_code == status
        assert response.content == b""
        for name, value in headers.items():
            assert response.headers[name] == value
    assert len(requests) == 1
    assert stream.read_count == 0
    assert stream.closed


@pytest.mark.parametrize("size", [1024, 1025])
@pytest.mark.parametrize("encoding,compress", [("gzip", gzip.compress), ("deflate", zlib.compress)])
def test_compressed_responses_are_decoded_once_and_capped(make_client, monkeypatch, size, encoding, compress):
    monkeypatch.setenv("ARGUS_GITLAB_MAX_RESPONSE_BYTES", "1024")
    body = b"x" * size
    compressed = compress(body)
    stream = TrackedStream([compressed])
    headers = {"Content-Encoding": encoding, "Content-Length": str(len(compressed))}
    client, _ = make_client(lambda _: httpx.Response(200, headers=headers, stream=stream))
    if size == 1024:
        response = client.get("user")
        assert response.content == body
        assert response.headers["Content-Encoding"] == encoding
        assert response.headers["Content-Length"] == str(len(compressed))
    else:
        with pytest.raises(GitLabHTTPError) as caught:
            client.get("user")
        assert caught.value.reason == "response_too_large"
    assert stream.closed


def test_compressed_expansion_is_bounded_before_allocation(make_client, monkeypatch):
    cap = 64 * 1024
    monkeypatch.setenv("ARGUS_GITLAB_MAX_RESPONSE_BYTES", str(cap))
    compressed = gzip.compress(b"\x00" * (16 * 1024 * 1024))
    assert len(compressed) < cap
    stream = TrackedStream([compressed])
    decompressobj = zlib.decompressobj
    decoded_sizes = []

    class TrackingDecoder:
        def __init__(self, *args, **kwargs):
            self.decoder = decompressobj(*args, **kwargs)

        def decompress(self, *args, **kwargs):
            decoded = self.decoder.decompress(*args, **kwargs)
            decoded_sizes.append(len(decoded))
            return decoded

        def __getattr__(self, name):
            return getattr(self.decoder, name)

    monkeypatch.setattr(zlib, "decompressobj", TrackingDecoder)
    client, _ = make_client(lambda _: httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=stream))
    tracemalloc.start()
    try:
        with pytest.raises(GitLabHTTPError) as caught:
            client.get("user")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert caught.value.reason == "response_too_large"
    assert decoded_sizes
    assert sum(decoded_sizes) <= cap + 1
    assert peak < 2 * 1024 * 1024
    assert stream.closed


@pytest.mark.parametrize("body", [b"broken", gzip.compress(b"x")[:-3]])
def test_invalid_compression_raises_a_safe_http_error(make_client, body):
    stream = TrackedStream([body])
    client, requests = make_client(lambda _: httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=stream))
    with pytest.raises(GitLabHTTPError) as caught:
        client.get("user")
    assert classify_error(caught.value) == "gitlab_http"
    assert not hasattr(caught.value, "request")
    assert not hasattr(caught.value, "response")
    assert SENTINEL not in str(caught.value)
    assert len(requests) == 1
    assert stream.closed


@pytest.mark.parametrize("status", [200, 302])
def test_http_traces_hide_reflected_secrets_without_muting_other_threads(make_client, caplog, status):
    for logger in ("httpx", "httpcore"):
        caplog.set_level(logging.DEBUG, logger=logger)

    def handle(request):
        logging.getLogger("httpcore.http11").debug("Location: https://other.test/%s", SENTINEL)
        thread = threading.Thread(target=lambda: logging.getLogger("httpx").info("unrelated request"))
        thread.start()
        thread.join()
        return httpx.Response(status, extensions={"reason_phrase": SENTINEL.encode()})

    client, _ = make_client(handle)
    if status == 200:
        assert client.get("user").status_code == 200
    else:
        with pytest.raises(GitLabDenied):
            client.get("user")
    logging.getLogger("httpx").info("after request")
    messages = [record.getMessage() for record in caplog.records]
    assert all(SENTINEL not in message for message in messages)
    assert all("Location:" not in message for message in messages)
    assert "unrelated request" in messages
    assert "after request" in messages


def test_transport_decoding_failure_drops_the_original_exception(make_client):
    def handle(request):
        raise httpx.DecodingError(SENTINEL, request=request)

    client, requests = make_client(handle)
    with pytest.raises(GitLabHTTPError) as caught:
        client.get("user")
    assert caught.value.reason == "decoding"
    assert caught.value.__context__ is None
    assert SENTINEL not in str(caught.value)
    assert len(requests) == 1


@pytest.mark.parametrize("error", [GitLabAuthError, GitLabDenied, GitLabHTTPError])
def test_exception_messages_redact_supplied_secrets(error):
    assert SENTINEL not in str(error("rejected " + SENTINEL))


@pytest.mark.parametrize("error,base,category", [
    (GitLabRateLimited(1.0), ForgeRateLimited, "gitlab_rate_limited"),
    (GitLabAuthError("x"), ForgeAuthError, "gitlab_auth"),
    (GitLabDenied("x", "guard"), ForgeDenied, "gitlab_denied"),
    (GitLabHTTPError("x", 422, "http"), ForgeError, "gitlab_http"),
])
def test_errors_are_classified_picklable_and_separate_from_github(error, base, category):
    restored = pickle.loads(pickle.dumps(error))
    for instance in (error, restored):
        assert isinstance(instance, base)
        assert instance.forge == "gitlab"
        assert classify_error(instance) == category
        assert not isinstance(instance, (github.GitHubRateLimited, github.GitHubAuthError, github.GitHubDenied))
    assert type(restored) is type(error)
    assert vars(restored) == vars(error)
    assert str(restored) == str(error)


def test_github_and_head_moved_classifications_are_unchanged():
    error = RuntimeError("x")
    error.request = httpx.Request("GET", "https://api.github.com/repos/a/b")
    for exception, expected in [
        (github.GitHubRateLimited(1.0), "github_rate_limited"),
        (github.GitHubAuthError("x"), "github_auth"),
        (github.GitHubDenied("x"), "github_denied"),
        (error, "github_http"), (HeadMoved(), "head_moved"),
    ]:
        assert classify_error(exception) == expected


def test_disabled_accessor_does_not_construct_http_client(monkeypatch):
    monkeypatch.delenv("ARGUS_GITLAB_URL")

    def forbidden(**kwargs):
        pytest.fail("Disabled GitLab constructed an HTTP client")

    monkeypatch.setattr(httpx, "Client", forbidden)
    assert module.client() is None


def test_accessor_is_shared_under_threads_and_close_resets_it(monkeypatch):
    real_client = httpx.Client
    constructed = []

    def build(**kwargs):
        client = real_client(**(kwargs | {"transport": httpx.MockTransport(lambda _: httpx.Response(200))}))
        constructed.append(client)
        return client

    monkeypatch.setattr(httpx, "Client", build)
    with ThreadPoolExecutor(max_workers=8) as pool:
        clients = list(pool.map(lambda _: module.client(), range(20)))
    assert all(client is clients[0] for client in clients)
    assert len(constructed) == 1
    module.close_client()
    assert constructed[0].is_closed
    assert module.client() is not clients[0]
    assert len(constructed) == 2


def test_missing_token_refuses_before_constructing_http_client(monkeypatch):
    monkeypatch.delenv("ARGUS_GITLAB_TOKEN")

    def forbidden(**kwargs):
        pytest.fail("Missing token constructed an HTTP client")

    monkeypatch.setattr(httpx, "Client", forbidden)
    with pytest.raises(GitLabAuthError, match="GitLab token is not set"):
        GitLabClient()


@pytest.mark.parametrize("status,route,body,headers,error,reason", [
    (401, "user", {}, {}, GitLabAuthError, None),
    (403, "user", {"error": "insufficient_scope"}, {}, GitLabAuthError, None),
    (403, "user", {"message": {"error": "insufficient_scope"}}, {}, GitLabAuthError, None),
    (403, "user", {}, {"WWW-Authenticate": 'Bearer error="insufficient_scope"'}, GitLabAuthError, None),
    (403, "user", {}, {}, GitLabDenied, "forbidden"),
    (404, "project", {}, {}, GitLabDenied, "not_found"),
    (404, "mr_list", {}, {}, GitLabDenied, "not_found"),
    (404, "user", {}, {}, GitLabHTTPError, "http"),
    (400, "user", {}, {}, GitLabHTTPError, "http"),
    (405, "user", {}, {}, GitLabHTTPError, "http"),
    (409, "user", {}, {}, GitLabHTTPError, "http"),
    (422, "user", {}, {}, GitLabHTTPError, "http"),
])
def test_status_error_mapping(make_client, status, route, body, headers, error, reason):
    client, _ = make_client(lambda _: httpx.Response(status, json=body, headers=headers))
    with pytest.raises(error) as caught:
        client.get(route, **({"project_id": 7} if route != "user" else {}))
    if reason:
        assert caught.value.reason == reason
    if error is GitLabHTTPError:
        assert caught.value.status_code == status


@pytest.mark.parametrize("status,route,scope,error,reason", [
    (401, "user", False, GitLabAuthError, None),
    (403, "user", True, GitLabAuthError, None),
    (403, "user", False, GitLabDenied, "forbidden"),
    (404, "project", False, GitLabDenied, "not_found"),
    (404, "user", False, GitLabHTTPError, "http"),
    (422, "user", False, GitLabHTTPError, "http"),
    (429, "user", False, GitLabRateLimited, None),
])
@pytest.mark.parametrize("body,headers", [
    (b"broken", {"Content-Encoding": "gzip"}),
    (b"unsupported", {"Content-Encoding": "br"}),
    (b"x" * 2048, {"Content-Length": "2048"}),
    (b"x" * 2048, {}),
], ids=["malformed_gzip", "unsupported_encoding", "oversized_length", "oversized_stream"])
def test_error_status_mapping_survives_unreadable_body(
    make_client, monkeypatch, status, route, scope, error, reason, body, headers,
):
    monkeypatch.setenv("ARGUS_GITLAB_MAX_RESPONSE_BYTES", "1024")
    monkeypatch.setattr(module.time, "time", lambda: NOW)
    headers = dict(headers)
    if scope:
        headers["WWW-Authenticate"] = 'Bearer error="insufficient_scope"'
    stream = TrackedStream([body])
    client, requests = make_client(lambda _: httpx.Response(status, headers=headers, stream=stream))
    with pytest.raises(error) as caught:
        client.get(route, **({"project_id": 7} if route == "project" else {}))
    if reason:
        assert caught.value.reason == reason
    if error is GitLabHTTPError:
        assert caught.value.status_code == status
    if error is GitLabRateLimited:
        assert caught.value.reset_at == NOW + 60
        assert client.pause_until() == NOW + 60
    assert len(requests) == 1
    assert stream.closed
    if status != 403 or scope:
        assert stream.read_count == 0


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ReadError, httpx.DecodingError])
def test_forbidden_body_read_failure_does_not_retry(make_client, failure):
    def chunks(request):
        yield b'{"message":'
        raise failure("Unreadable denial body", request=request)

    client, requests = make_client(lambda request: httpx.Response(
        403, stream=TrackedStream(chunks(request)),
    ))
    with pytest.raises(GitLabDenied) as caught:
        client.get("user")
    assert caught.value.reason == "forbidden"
    assert len(requests) == 1


def test_forbidden_deeply_nested_json_does_not_retry(make_client):
    body = b"[" * 2000 + b"0" + b"]" * 2000
    client, requests = make_client(lambda _: httpx.Response(403, content=body))
    with pytest.raises(GitLabDenied) as caught:
        client.get("user")
    assert caught.value.reason == "forbidden"
    assert len(requests) == 1


def test_forbidden_json_recursion_error_does_not_retry(make_client, monkeypatch):
    def json_with_recursion_error(self):
        raise RecursionError("JSON nesting exceeds the parser limit")

    # Newer Python versions can parse the nested fixture without reaching a limit.
    monkeypatch.setattr(httpx.Response, "json", json_with_recursion_error)
    client, requests = make_client(lambda _: httpx.Response(403, content=b"[]"))
    with pytest.raises(GitLabDenied) as caught:
        client.get("user")
    assert caught.value.reason == "forbidden"
    assert len(requests) == 1


def test_forbidden_readable_scope_error_remains_an_auth_error(make_client):
    client, requests = make_client(lambda _: httpx.Response(
        403, json={"error": "insufficient_scope"},
    ))
    with pytest.raises(GitLabAuthError):
        client.get("user")
    assert len(requests) == 1


@pytest.mark.parametrize("failure", [500, 599, httpx.ConnectError, httpx.ReadTimeout])
@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"])
@pytest.mark.parametrize("recovers", [True, False])
def test_only_get_retries_once(make_client, failure, method, recovers):
    def handle(request):
        if recovers and len(requests) == 2:
            return httpx.Response(200, json={"ok": True})
        if isinstance(failure, int):
            return httpx.Response(failure)
        raise failure("unsafe " + SENTINEL, request=request)

    client, requests = make_client(handle)
    if method == "GET" and recovers:
        assert client.request(method, "mr_discussions", project_id=7, iid=2).json() == {"ok": True}
    else:
        with pytest.raises(GitLabHTTPError) as caught:
            client.request(method, "mr_discussions", project_id=7, iid=2, json={"body": "note"})
        assert caught.value.status_code == (failure if isinstance(failure, int) else None)
        assert caught.value.reason == ("http" if isinstance(failure, int) else "transport")
        assert SENTINEL not in str(caught.value)
    assert len(requests) == (2 if method == "GET" else 1)


@pytest.mark.parametrize("status", [500, 599])
@pytest.mark.parametrize("method,recovers", [("GET", True), ("GET", False), ("POST", False)])
def test_server_error_retry_ignores_malformed_compression(make_client, status, method, recovers):
    streams = []

    def handle(request):
        if recovers and len(requests) == 2:
            return httpx.Response(200, json={"ok": True})
        stream = TrackedStream([b"broken"])
        streams.append(stream)
        return httpx.Response(status, headers={"Content-Encoding": "gzip"}, stream=stream)

    client, requests = make_client(handle)
    if recovers:
        response = client.request(method, "mr_discussions", project_id=7, iid=2)
        assert response.status_code == 200
        assert response.json() == {"ok": True}
    else:
        with pytest.raises(GitLabHTTPError) as caught:
            client.request(method, "mr_discussions", project_id=7, iid=2, json={"body": "note"})
        assert caught.value.status_code == status
        assert caught.value.reason == "http"
        assert str(caught.value) == f"GitLab returned HTTP {status}"
    assert len(requests) == (2 if method == "GET" else 1)
    assert all(stream.closed and stream.read_count == 0 for stream in streams)


@pytest.mark.parametrize("final_check", [False, True])
def test_guard_rejects_bad_parts_and_rechecks_final_url(make_client, monkeypatch, final_check):
    client, requests = make_client(lambda _: httpx.Response(200))
    if final_check:
        monkeypatch.setattr(guard, "build", lambda *a, **kw: httpx.URL("https://other.test/api/v4/projects/7"))
    with pytest.raises(GitLabDenied) as caught:
        client.get("project", project_id=7 if final_check else "../x")
    assert caught.value.reason == "guard"
    assert requests == []


def test_changed_origin_cannot_receive_the_existing_token(make_client, monkeypatch):
    client, requests = make_client(lambda _: httpx.Response(200))
    monkeypatch.setenv("ARGUS_GITLAB_URL", "https://other.test")
    with pytest.raises(GitLabDenied) as caught:
        client.get("user")
    assert caught.value.reason == "guard"
    assert requests == []
