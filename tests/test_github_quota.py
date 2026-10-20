"""Quota and conditional listing tests use an in-memory transport."""

import time

import httpx
import pytest

from argus import github


@pytest.fixture(autouse=True)
def state(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake")
    monkeypatch.setattr(github, "_quota", {})
    monkeypatch.setattr(github, "_pause_until", 0)
    monkeypatch.setattr(github, "_cache", github.OrderedDict())


def response(code=200, headers=None, body=None):
    return httpx.Response(
        code,
        headers=headers,
        json=body or {},
        request=httpx.Request("GET", "https://api.github.com/user/repos"),
    )


def test_all_quota_headers_recorded(monkeypatch):
    headers = {
        f"X-RateLimit-{k}": v
        for k, v in {
            "Limit": "5000",
            "Remaining": "4500",
            "Reset": str(int(time.time()) + 3600),
            "Used": "500",
            "Resource": "core",
        }.items()
    }
    monkeypatch.setattr(httpx, "get", lambda *a, **k: response(headers=headers))
    github.get("/user/repos")
    snapshot = github.quota_snapshot()["core"]
    assert snapshot["limit"] == 5000
    assert snapshot["remaining"] == 4500
    assert snapshot["used"] == 500
    assert snapshot["read_at"]
    snapshot["remaining"] = 0
    assert github.quota_snapshot()["core"]["remaining"] == 4500


@pytest.mark.parametrize("code,error", [
    (200, None), (401, github.GitHubAuthError), (403, github.GitHubAuthError),
    (429, github.GitHubRateLimited),
])
def test_observe_without_quota_recording_preserves_read_state(monkeypatch, code, error):
    github.observe_response(response(headers={"X-RateLimit-Remaining": "4500"}))
    monkeypatch.setattr(github, "_pause_until", 123.0)
    before = github.quota_snapshot(), github.pause_until()
    headers = {"X-RateLimit-Remaining": "10", "X-RateLimit-Reset": "9999999999"}
    if code == 429:
        headers["Retry-After"] = "120"
    if error:
        with pytest.raises(error):
            github.observe_response(response(code, headers), record_quota=False)
    else:
        github.observe_response(response(code, headers), record_quota=False)
    assert (github.quota_snapshot(), github.pause_until()) == before


def test_observe_without_quota_recording_still_guards_url(monkeypatch):
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "acme-corp")
    denied = httpx.Response(200, request=httpx.Request(
        "GET", "https://api.github.com/repos/acme-corp/secret-repo/contents/x"))
    with pytest.raises(github.GitHubDenied):
        github.observe_response(denied, record_quota=False)


@pytest.mark.parametrize(
    "code,headers,body",
    [
        (403, {"X-RateLimit-Remaining": "0"}, {}),
        (403, {"Retry-After": "120"}, {}),
        (429, {"Retry-After": "120"}, {}),
        (403, {}, {"message": "API rate limit exceeded"}),
    ],
)
def test_limits_are_not_auth_errors(monkeypatch, code, headers, body):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: response(code, headers, body))
    with pytest.raises(github.GitHubRateLimited) as error:
        github.get("/user/repos")
    assert not isinstance(error.value, github.GitHubAuthError)
    assert error.value.reset_at > time.time()
    assert github.pause_until() == error.value.reset_at


def test_pr_pagination_and_304_reuse(monkeypatch):
    requests = []

    def get(url, **kwargs):
        req = httpx.Request("GET", url, params=kwargs.get("params"), headers=kwargs["headers"])
        requests.append(req)
        page = req.url.params.get("page", "1")
        if req.headers.get("If-None-Match"):
            return httpx.Response(304, request=req)
        link = (
            '<https://api.github.com/repos/example/a/pulls?page=2>; rel="next"'
            if page == "1"
            else ""
        )
        return httpx.Response(
            200,
            json=[{"number": int(page)}],
            headers={"ETag": f'"{page}"', "Link": link},
            request=req,
        )

    monkeypatch.setattr(httpx, "get", get)
    for _ in range(2):
        assert github.list_cached("/repos/example/a/pulls", {"per_page": 100}) == [
            {"number": 1},
            {"number": 2},
        ]
    assert len(requests) == 4
    assert requests[2].headers["If-None-Match"] == '"1"'
    assert requests[3].headers["If-None-Match"] == '"2"'


def test_redirect_is_checked_before_following(monkeypatch):
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "acme-corp")
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return httpx.Response(
            301, headers={"Location": "/repos/acme-corp/secret-repo"}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx, "get", get)
    before = github.refusal_count()
    with pytest.raises(github.GitHubDenied):
        github.get("/repos/example/a")
    assert len(calls) == 1
    assert github.refusal_count() == before + 1


def test_lru_is_bounded(monkeypatch):
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **kw: httpx.Response(
            200, json=[], headers={"ETag": "x"}, request=httpx.Request("GET", url)
        ),
    )
    for i in range(2002):
        github.get_json_cached(f"/repos/example/r{i}/pulls")
    assert len(github._cache) == 2000
    assert not any("/r0/" in key for key in github._cache)
