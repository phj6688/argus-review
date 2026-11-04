"""Exercise review boundaries with a guarded mock transport."""

from contextlib import closing

import httpx
import pytest

from argus import review
from argus.api import db
from argus.errors import HeadMoved
from argus.gitlab import entry, status
from argus.schema import Review


@pytest.mark.parametrize("suffix", ["", "/", "?diff_id=4#note_1", "/#note_1"])
def test_mr_url_configured_origin_and_prefix(gitlab_event, suffix):
    assert entry.parse_mr_url(gitlab_event.url + suffix) == ("allowed/sub/app", 7)
    assert gitlab_event.requests == []


@pytest.mark.parametrize("url", [
    "https://other.test/gitlab/allowed/app/-/merge_requests/7",
    "https://gitlab.example.test/gitlab/allowed/app/-/merge_requests/7",
    "https://gitlab.example.test:8443/gitlab2/allowed/app/-/merge_requests/7",
    "https://gitlab.example.test:8443/gitlab/allowed%2Fapp/-/merge_requests/7",
    "https://user:password@gitlab.example.test:8443/gitlab/allowed/app/-/merge_requests/7",
    "http://gitlab.example.test:8443/gitlab/allowed/app/-/merge_requests/7",
    "https://gitlab.example.test:8443/gitlab/allowed/../app/-/merge_requests/7",
    "https://gitlab.example.test:8443/gitlab/allowed/./app/-/merge_requests/7",
    "https://gitlab.example.test:8443/gitlab/allowed/app\\x/-/merge_requests/7",
    "https://gitlab.example.test:8443/gitlab/allowed/app\n/-/merge_requests/7",
    "https://gitlab.example.test:8443/gitlab/app/-/merge_requests/7",
    "https://gitlab.example.test:8443/gitlab/allowed//app/-/merge_requests/7",
    "https://gitlab.example.test:8443/gitlab/allowed/app/-/merge_requests/0",
    "https://gitlab.example.test:8443/gitlab/allowed/app/-/merge_requests/-1",
    "https://gitlab.example.test:8443/gitlab/allowed/app/-/merge_requests/١",
    "https://gitlab.example.test:8443/gitlab/allowed/app/-/merge_requests/1/extra",
])
def test_mr_url_refuses_unsafe_input_without_echo(gitlab_event, url):
    with pytest.raises(entry.EntryRefused) as caught:
        entry.parse_mr_url(url)
    assert url not in str(caught.value)
    assert gitlab_event.requests == []


def test_url_canonical_host_and_default_port(gitlab_event, monkeypatch):
    monkeypatch.setenv("ARGUS_GITLAB_URL", "https://gitlab.example.test")
    assert entry.parse_mr_url("https://GITLAB.example.test:443/group/app/-/merge_requests/7") == ("group/app", 7)
    monkeypatch.setenv("ARGUS_GITLAB_URL", "https://[::1]:8443/gitlab")
    assert entry.parse_mr_url("https://[::1]:8443/gitlab/group/app/-/merge_requests/7") == ("group/app", 7)


def test_canonicalization_resolves_path_once(gitlab_event):
    event = gitlab_event
    assert entry.canonical_repo(event.path_key) == event.key
    assert entry.canonical_repo(event.key) == event.key
    assert [r.url.path for r in event.requests] == [event.prefix + "/projects/allowed/sub/app"]


@pytest.mark.parametrize("failure,classification", [
    ("timeout", "gitlab_http"), (500, "gitlab_http"),
    (429, "gitlab_rate_limited"), (401, "gitlab_auth"),
])
def test_path_lookup_failure_is_unavailable(gitlab_event, failure, classification):
    event = gitlab_event
    lookup = event.prefix + "/projects/allowed/sub/app"

    def fail(request):
        raise httpx.ReadTimeout(event.token, request=request)

    event.responses[lookup] = fail if failure == "timeout" else httpx.Response(failure, text=event.token)
    with pytest.raises(entry.EntryUnavailable) as caught:
        entry.canonical_repo(event.path_key)
    assert caught.value.classification == classification
    assert event.token not in str(caught.value)
    assert {r.url.path for r in event.requests} == {lookup}


@pytest.mark.parametrize("code", [301, 404])
def test_path_lookup_refuses_unknown_or_renamed_path(gitlab_event, code):
    event = gitlab_event
    lookup = event.prefix + "/projects/allowed/sub/app"
    event.responses[lookup] = httpx.Response(code, text=event.token)
    with pytest.raises(entry.EntryRefused, match="use the new path or the numeric id"):
        entry.canonical_repo(event.path_key)
    assert [r.url.path for r in event.requests] == [lookup]


@pytest.mark.parametrize("text", ["gitlab:other.test#42", "gitlab:other.test/group/app", "gitlab:gitlab.example.test:8443#0"])
def test_canonicalization_refuses_foreign_or_invalid_keys(gitlab_event, text):
    with pytest.raises(entry.EntryRefused):
        entry.canonical_repo(text)
    assert gitlab_event.requests == []


def test_canonicalization_refuses_malformed_path_without_request(gitlab_event):
    with pytest.raises(entry.EntryRefused, match="^Invalid GitLab repository input$"):
        entry.canonical_repo(f"gitlab:{gitlab_event.host}/allowed//app")
    assert gitlab_event.requests == []


def test_gitlab_off_refuses_without_request(gitlab_event, monkeypatch):
    monkeypatch.delenv("ARGUS_GITLAB_URL")
    for text in [gitlab_event.key, gitlab_event.path_key]:
        with pytest.raises(entry.EntryRefused, match="^GitLab is not configured$"):
            entry.canonical_repo(text)
    with pytest.raises(entry.EntryRefused, match="^GitLab is not configured$"):
        entry.parse_mr_url(gitlab_event.url)
    assert gitlab_event.requests == []


def test_allowed_event_order(gitlab_event):
    entry.check_change(gitlab_event.key, 7)
    assert [r.url.path for r in gitlab_event.requests] == [gitlab_event.project, gitlab_event.mr, gitlab_event.project]


@pytest.mark.parametrize("kind", ["denied", "not_allowlisted", "hard_denied", "fork"])
def test_denial_precedes_all_diff_reads(gitlab_event, kind):
    expected = gitlab_event.deny(kind)
    with pytest.raises(entry.EntryDenied):
        entry.check_change(gitlab_event.key, 7)
    assert [r.url.path for r in gitlab_event.requests] == expected


@pytest.mark.parametrize("route", ["project", "mr"])
@pytest.mark.parametrize("code,classification", [(401, "gitlab_auth"), (429, "gitlab_rate_limited"), (500, "gitlab_http")])
def test_lookup_failure_has_safe_classification(gitlab_event, route, code, classification):
    event = gitlab_event
    event.responses[getattr(event, route)] = httpx.Response(code, text=event.token)
    with pytest.raises(entry.EntryUnavailable) as caught:
        entry.check_change(event.key, 7)
    assert caught.value.classification == classification
    assert event.token not in str(caught.value)
    assert all(r.url.path in {event.project, event.mr} for r in event.requests)


@pytest.mark.parametrize("source", ["live_deny", "change_meta", "live_deny_change"])
def test_head_moved_is_unavailable(gitlab_event, monkeypatch, source):
    def fail(*args):
        raise HeadMoved(gitlab_event.token + "\nprivate lookup details")

    target = entry.forge_for(gitlab_event.key) if source == "change_meta" else entry.projects
    monkeypatch.setattr(target, source, fail)
    with pytest.raises(entry.EntryUnavailable) as caught:
        entry.check_change(gitlab_event.key, 7)
    assert caught.value.classification == "head_moved"
    assert str(caught.value) == "head_moved"
    assert all(r.url.path in {gitlab_event.project, gitlab_event.mr} for r in gitlab_event.requests)


@pytest.mark.parametrize("code", [403, 404])
def test_meta_denial_is_a_denial(gitlab_event, code):
    gitlab_event.responses[gitlab_event.mr] = httpx.Response(code)
    with pytest.raises(entry.EntryDenied):
        entry.check_change(gitlab_event.key, 7)
    assert [r.url.path for r in gitlab_event.requests] == [gitlab_event.project, gitlab_event.mr]


def test_transport_failure_is_unavailable(gitlab_event):
    def fail(request):
        raise httpx.ConnectError(gitlab_event.token, request=request)
    gitlab_event.responses[gitlab_event.mr] = fail
    with pytest.raises(entry.EntryUnavailable, match="^gitlab_http$"):
        entry.check_change(gitlab_event.key, 7)


def test_status_success_cache_and_token_sentinel(gitlab_event, monkeypatch):
    monkeypatch.setattr(status, "_clock", lambda: 0)
    monkeypatch.setattr(status, "_identity_cache", None)
    first = status.integration_status()
    assert status.integration_status() == first
    assert first == {"host": gitlab_event.host, "token": "configured",
                     "identity": {"status": "ok", "username": "review-bot", "bot": True},
                     "publish": {"mode": "0", "projectCount": 0}, "allowCount": 1, "refusal": None}
    assert gitlab_event.token not in str(first)
    assert len(gitlab_event.requests) == 1
    monkeypatch.setattr(status, "_clock", lambda: 299)
    status.integration_status()
    assert len(gitlab_event.requests) == 1
    monkeypatch.setattr(status, "_clock", lambda: 300)
    status.integration_status()
    assert len(gitlab_event.requests) == 2


def test_status_failure_cache_expires_after_sixty_seconds(gitlab_event, monkeypatch):
    monkeypatch.setattr(status, "_clock", lambda: 0)
    monkeypatch.setattr(status, "_identity_cache", None)
    gitlab_event.responses[gitlab_event.prefix + "/user"] = httpx.Response(401, text=gitlab_event.token)
    first = status.integration_status()
    assert first["identity"] == {"status": "failed", "reason": "gitlab_auth"}
    assert status.integration_status() == first
    monkeypatch.setattr(status, "_clock", lambda: 59)
    status.integration_status()
    assert len(gitlab_event.requests) == 1
    monkeypatch.setattr(status, "_clock", lambda: 60)
    status.integration_status()
    assert len(gitlab_event.requests) == 2
    assert gitlab_event.token not in str(first)


def test_status_missing_token_skips_request_and_bad_publish_is_safe(gitlab_event, monkeypatch):
    monkeypatch.delenv("ARGUS_GITLAB_TOKEN")
    monkeypatch.setenv("ARGUS_GITLAB_PUBLISH", gitlab_event.token)
    monkeypatch.setenv("ARGUS_GITLAB_PUBLISH_PROJECTS", "*")
    block = status.integration_status()
    assert block["identity"] == {"status": "skipped", "reason": "token_missing"}
    assert block["publish"] == {"mode": "invalid", "projectCount": 0}
    assert gitlab_event.token not in str(block)
    assert gitlab_event.requests == []


def test_status_token_change_invalidates_cache(gitlab_event, monkeypatch):
    monkeypatch.setattr(status, "_identity_cache", None)
    status.integration_status()
    monkeypatch.setenv("ARGUS_GITLAB_TOKEN", "gl" + "pat-" + "z" * 20)
    status.integration_status()
    assert len(gitlab_event.requests) == 2


def test_run_review_reads_only_the_selected_forge(gitlab_event, monkeypatch):
    monkeypatch.setattr(review, "_deep_review", lambda *a, **kw: Review(findings=[]))
    entry.check_change(gitlab_event.key, 7)
    result = review.run_review(gitlab_event.key, 7, expected_head_sha=gitlab_event.head)
    assert result.head_sha == gitlab_event.head
    assert result.files_changed == 1
    assert any(r.url.path.endswith("/diffs") for r in gitlab_event.requests)
    assert any("/repository/files/" in r.url.path for r in gitlab_event.requests)
    with closing(db.connect()) as conn:
        assert db.forge_project(conn, gitlab_event.key)["path_with_namespace"] == "allowed/sub/app"


def test_diff_pipeline_refuses_gitlab_before_reads(gitlab_event):
    with pytest.raises(ValueError, match="^run_diff_review supports GitHub repositories only$"):
        review.run_diff_review([], gitlab_event.key, gitlab_event.head)
    assert gitlab_event.requests == []
