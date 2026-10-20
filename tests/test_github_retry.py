"""GET retries never repeat writes or bypass quota and owner checks."""

import logging
from unittest.mock import Mock

import httpx
import pytest

from argus import github


@pytest.fixture(autouse=True)
def state(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake")
    monkeypatch.setattr(github, "_quota", {})
    monkeypatch.setattr(github, "_pause_until", 0)
    monkeypatch.setattr(github, "_graphql_pause_until", 0)
    monkeypatch.setattr(github.time, "sleep", Mock())


def response(code):
    return httpx.Response(code, request=httpx.Request("GET", "https://api.github.com/user"))


@pytest.mark.parametrize("first", [
    httpx.ReadTimeout("private"), httpx.ConnectTimeout("private"),
    httpx.ConnectError("private"), httpx.RemoteProtocolError("private"),
    response(500), response(502), response(503), response(504),
])
def test_transient_get_retries_once(monkeypatch, caplog, first):
    get = Mock(side_effect=[first, response(200)])
    monkeypatch.setattr(httpx, "get", get)
    with caplog.at_level(logging.INFO, logger="argus.github"):
        assert github.get("/user").status_code == 200
    assert get.call_count == 2
    github.time.sleep.assert_called_once_with(1)
    assert len({id(r) for r in caplog.records}) == 1
    assert "private" not in caplog.text
    cause = str(first.status_code) if isinstance(first, httpx.Response) else type(first).__name__
    assert cause in caplog.text


def test_second_transport_error_is_raised(monkeypatch):
    second = httpx.ReadTimeout("second")
    get = Mock(side_effect=[httpx.ConnectError("first"), second])
    monkeypatch.setattr(httpx, "get", get)
    with pytest.raises(httpx.TransportError) as caught:
        github.get("/user")
    assert get.call_count == 2
    assert caught.value is second
    github.time.sleep.assert_called_once_with(1)


@pytest.mark.parametrize("first", [response(503), httpx.ConnectError("first")])
@pytest.mark.parametrize("code", [500, 502, 503, 504])
def test_second_server_error_is_returned(monkeypatch, first, code):
    second = response(code)
    get = Mock(side_effect=[first, second])
    monkeypatch.setattr(httpx, "get", get)
    assert github.get("/user") is second
    assert get.call_count == 2
    github.time.sleep.assert_called_once_with(1)


def test_review_diff_repeated_compare_failure_returns_422(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from argus.api import db, main

    monkeypatch.setenv("ARGUS_DB_PATH", str(tmp_path / "argus.db"))
    db.init_db()
    url = "https://api.github.com/repos/o/r/compare/aaaaaaa...bbbbbbb"
    get = Mock(side_effect=[
        httpx.Response(502, request=httpx.Request("GET", url)),
        httpx.Response(502, request=httpx.Request("GET", url)),
    ])
    monkeypatch.setattr(httpx, "get", get)
    with TestClient(main.app) as client:
        result = client.post("/review-diff", json={
            "repo": "o/r", "base_sha": "aaaaaaa", "head_sha": "bbbbbbb", "mode": "fast",
        })
    assert result.status_code == 422
    assert result.json()["detail"] == "compare fetch failed (502)"
    assert get.call_count == 2
    github.time.sleep.assert_called_once_with(1)


def test_fetch_file_at_repeated_server_error_returns_none(monkeypatch):
    from argus.jarvis import live

    get = Mock(side_effect=[response(500), response(500)])
    monkeypatch.setattr(httpx, "get", get)
    assert live._fetch_file_at("o/r", "src/app.py", "head") is None
    assert get.call_count == 2
    github.time.sleep.assert_called_once_with(1)


def test_pr_meta_repeated_commits_error_returns_empty_messages(monkeypatch):
    from argus.jarvis import live

    pull_url = "https://api.github.com/repos/o/r/pulls/1"
    commits_url = f"{pull_url}/commits"
    get = Mock(side_effect=[
        httpx.Response(200, json={"title": "Fix", "body": "Details", "head": {"sha": "head"}},
                       request=httpx.Request("GET", pull_url)),
        httpx.Response(503, request=httpx.Request("GET", commits_url)),
        httpx.Response(503, request=httpx.Request("GET", commits_url)),
    ])
    monkeypatch.setattr(httpx, "get", get)
    assert live._pr_meta("o/r", 1) == {
        "title": "Fix", "body": "Details", "head_sha": "head",
        "commits": [], "head_committed_at": None, "author_association": None,
    }
    assert [call.args[0] for call in get.call_args_list] == [pull_url, commits_url, commits_url]
    github.time.sleep.assert_called_once_with(1)


def test_house_rules_repeated_server_error_returns_none(monkeypatch):
    from argus.review import fetch_house_rules

    get = Mock(side_effect=[response(503), response(503)])
    monkeypatch.setattr(httpx, "get", get)
    assert fetch_house_rules("o/r") is None
    assert get.call_count == 2
    github.time.sleep.assert_called_once_with(1)


@pytest.mark.parametrize("code,limited", [(403, True), (429, True), (401, False), (403, False)])
def test_rate_and_auth_errors_are_not_retried(monkeypatch, code, limited):
    answer = response(code)
    if limited:
        answer.headers["retry-after"] = "60"
    get = Mock(return_value=answer)
    monkeypatch.setattr(httpx, "get", get)
    with pytest.raises(github.GitHubRateLimited if limited else github.GitHubAuthError):
        github.get("/user")
    assert get.call_count == 1
    github.time.sleep.assert_not_called()


def test_retry_rechecks_pause(monkeypatch):
    get = Mock(return_value=response(503))
    monkeypatch.setattr(httpx, "get", get)
    pause = Mock(side_effect=[0, 9999999999])
    monkeypatch.setattr(github, "pause_until", pause)
    with pytest.raises(github.GitHubRateLimited):
        github.get("/user")
    assert get.call_count == 1
    assert pause.call_count == 2


def test_retry_rechecks_guard(monkeypatch):
    get = Mock(side_effect=[httpx.ConnectError("first"), response(200)])
    monkeypatch.setattr(httpx, "get", get)
    guard = Mock(side_effect=[httpx.URL("https://api.github.com/user"),
                             httpx.URL("https://api.github.com/user"), github.GitHubDenied("denied")])
    monkeypatch.setattr(github, "guard_url", guard)
    with pytest.raises(github.GitHubDenied):
        github.get("/user")
    assert get.call_count == 1


@pytest.mark.parametrize("failure", [httpx.ConnectError("private"), response(503)])
def test_post_is_not_retried(monkeypatch, failure):
    post = Mock(side_effect=[failure])
    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises((httpx.TransportError, httpx.HTTPStatusError)):
        github.graphql_read("query Status { viewer { login } }", {})
    assert post.call_count == 1
    github.time.sleep.assert_not_called()


@pytest.mark.parametrize("code", [500, 502, 503, 504])
def test_server_error_then_transport_error_returns_original(monkeypatch, code):
    first = response(code)
    get = Mock(side_effect=[first, httpx.ReadTimeout("temporary")])
    monkeypatch.setattr(httpx, "get", get)
    assert github.get("/user") is first
    assert get.call_count == 2
    github.time.sleep.assert_called_once_with(1)
