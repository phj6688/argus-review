"""Review API policy and identity behavior for configured GitLab projects."""

import asyncio
from contextlib import closing

import httpx
import pytest
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient

from argus.api import db, main
from argus.gitlab import status
from argus.review import ReviewResult


@pytest.fixture
def api(gitlab_event, monkeypatch):
    monkeypatch.setattr(main, "run_review", lambda *a, **kw: ReviewResult(0, [], [], 0))
    monkeypatch.setattr(status, "_identity_cache", None)
    return TestClient(main.app)


def test_path_validation_defers_lookup_to_create_review(gitlab_event, monkeypatch):
    event = gitlab_event
    runs = []

    def run_review(repo, pr, *args, **kwargs):
        runs.append((repo, pr))
        return ReviewResult(0, [], [], 0)

    monkeypatch.setattr(main, "run_review", run_review)
    monkeypatch.setenv("ARGUS_FALLBACK_MODEL", "fallback-model")
    request = main.ReviewRequest(repo=event.path_key, pr=7)
    assert request.repo == event.path_key
    assert event.requests == []

    background = BackgroundTasks()
    result = main.create_review(request, background)

    assert event.requests[0].url.path == event.prefix + "/projects/allowed/sub/app"
    assert sum("%2F" in str(r.url.raw_path) for r in event.requests) == 1
    row = db.get_review(event.path, result.id)
    assert row["repo"] == event.key
    assert row["head_sha"] == event.head
    task = background.tasks[0]
    assert task.args[2:4] == (event.key, 7)
    task.args[4]()
    task.kwargs["fallback_runner"]()
    assert runs == [(event.key, 7), (event.key, 7)]


def test_path_lookup_runs_without_an_event_loop(api, gitlab_event):
    def resolve(request):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        return httpx.Response(200, json={"id": 42})

    gitlab_event.responses[gitlab_event.prefix + "/projects/allowed/sub/app"] = resolve
    response = api.post("/reviews", json={"repo": gitlab_event.path_key, "pr": 7})
    assert response.status_code == 201, response.text


@pytest.mark.parametrize("failure,status,detail", [
    ("timeout", 502, "gitlab lookup failed: gitlab_http"),
    (500, 502, "gitlab lookup failed: gitlab_http"),
    (429, 502, "gitlab lookup failed: gitlab_rate_limited"),
    (401, 502, "gitlab lookup failed: gitlab_auth"),
    (403, 422, "GitLab project input could not be resolved"),
    (404, 422, "GitLab project path is unavailable; use the new path or the numeric id"),
    (301, 422, "GitLab project path is unavailable; use the new path or the numeric id"),
])
def test_path_lookup_failure_precedes_provider_and_review_storage(
    api, gitlab_event, monkeypatch, failure, status, detail,
):
    event = gitlab_event
    lookup = event.prefix + "/projects/allowed/sub/app"

    def fail(request):
        raise httpx.ReadTimeout(event.token, request=request)

    event.responses[lookup] = fail if failure == "timeout" else httpx.Response(failure, text=event.token)
    monkeypatch.setattr(main, "require_provider", lambda *a, **kw: pytest.fail("Provider checked before refusal"))

    response = api.post("/reviews", json={"repo": event.path_key, "pr": 7})

    assert response.status_code == status
    assert response.json() == {"detail": detail}
    assert event.token not in response.text
    assert {r.url.path for r in event.requests} == {lookup}
    assert db.list_reviews(event.path) == []


@pytest.mark.parametrize("suffix,message", [
    ("other.test#42", "GitLab repository host does not match configuration"),
    ("other.test/group/app", "GitLab repository host does not match configuration"),
    ("gitlab.example.test:8443#0", "Invalid GitLab repository input"),
    ("gitlab.example.test:8443/group", "Invalid GitLab repository input"),
    ("gitlab.example.test:8443/allowed//app", "Invalid GitLab repository input"),
])
def test_invalid_key_validation_makes_no_request(api, gitlab_event, suffix, message):
    repo = gitlab_event.key.split(":", 1)[0] + ":" + suffix
    response = api.post("/reviews", json={"repo": repo, "pr": 7})
    assert response.status_code == 422
    assert message in response.text
    assert gitlab_event.requests == []


def test_nested_path_and_id_store_same_key(api, gitlab_event):
    event = gitlab_event
    ids = []
    for repo in [event.path_key, event.key]:
        response = api.post("/reviews", json={"repo": repo, "pr": 7})
        assert response.status_code == 201, response.text
        ids.append(response.json()["id"])
    assert [db.get_review(event.path, i)["repo"] for i in ids] == [event.key, event.key]
    assert [db.get_review(event.path, i)["head_sha"] for i in ids] == [event.head, event.head]
    assert sum("%2F" in str(r.url.raw_path) for r in event.requests) == 1


@pytest.mark.parametrize("kind", ["denied", "not_allowlisted", "hard_denied", "fork"])
@pytest.mark.parametrize("repo_form", ["key", "path_key"])
def test_policy_denial_matches_github_before_provider_or_diff(api, gitlab_event, monkeypatch, kind, repo_form):
    event = gitlab_event
    expected = event.deny(kind)
    monkeypatch.setattr(main, "require_provider", lambda *a, **kw: pytest.fail("Provider checked before denial"))
    if repo_form == "path_key":
        expected = [event.prefix + "/projects/allowed/sub/app", *expected]
    response = api.post("/reviews", json={"repo": getattr(event, repo_form), "pr": 7})
    github = api.post("/reviews", json={"repo": "blockedorg/app", "pr": 7})
    assert response.status_code == github.status_code == 403
    assert response.json() == {"detail": f"repository denied: {event.key}"}
    assert github.json() == {"detail": "repository denied: blockedorg/app"}
    assert [r.url.path for r in event.requests] == expected
    assert db.list_reviews(event.path) == []


@pytest.mark.parametrize("change", [
    lambda url: url.replace("gitlab.example.test", "other.test"),
    lambda url: url.replace("/gitlab/", "/gitlab2/"),
    lambda url: url.replace("allowed/sub", "allowed%2Fsub"),
    lambda url: url.replace("https://", "https://user:password@"),
    lambda url: url.replace("https://", "http://"),
])
def test_url_repo_is_refused_without_request(api, gitlab_event, change):
    response = api.post("/reviews", json={"repo": change(gitlab_event.url), "pr": 7})
    assert response.status_code == 422
    assert gitlab_event.requests == []


def test_unconfigured_key_has_neutral_validation_error(api, gitlab_event, monkeypatch):
    monkeypatch.delenv("ARGUS_GITLAB_URL")
    response = api.post("/reviews", json={"repo": gitlab_event.key, "pr": 7})
    assert response.status_code == 422
    assert "GitLab is not configured" in response.text
    assert gitlab_event.requests == []


@pytest.mark.parametrize("mode", ["full", "fast"])
@pytest.mark.parametrize("source", ["diff", "compare"])
def test_diff_route_is_github_only(api, gitlab_event, mode, source):
    body = {"repo": gitlab_event.key, "mode": mode}
    body.update({"diff": "x"} if source == "diff" else {"base_sha": "b" * 40, "head_sha": "a" * 40})
    response = api.post("/review-diff", json=body)
    assert response.status_code == 422
    assert "review-diff supports GitHub repositories only" in response.text
    assert gitlab_event.requests == []


def test_lookup_failure_returns_safe_502(api, gitlab_event):
    gitlab_event.responses[gitlab_event.mr] = httpx.Response(401, text=gitlab_event.token)
    response = api.post("/reviews", json={"repo": gitlab_event.key, "pr": 7})
    assert response.status_code == 502
    assert response.json() == {"detail": "gitlab lookup failed: gitlab_auth"}
    assert gitlab_event.token not in response.text
    assert [r.url.path for r in gitlab_event.requests] == [gitlab_event.project, gitlab_event.mr]


def test_integrations_identity_cached_and_safe(api, gitlab_event, monkeypatch):
    monkeypatch.setenv("ARGUS_GITLAB_PUBLISH", "dry-run")
    monkeypatch.setenv("ARGUS_GITLAB_PUBLISH_PROJECTS", gitlab_event.key)
    first = api.get("/integrations")
    assert api.get("/integrations").content == first.content
    assert gitlab_event.token not in first.text
    assert first.json()["gitlab"] == {
        "host": gitlab_event.host, "baseUrl": "https://gitlab.example.test:8443/gitlab", "token": "configured",
        "identity": {"status": "ok", "username": "review-bot", "bot": True},
        "publish": {"mode": "dry-run", "projectCount": 1}, "allowCount": 1, "refusal": None,
        "trustUnknown": 0,
        "replyLoop": {"enabled": False, "resolveForbidden": 0},
        "watch": {"enabled": False, "projectCount": 0, "lastPolledAt": None,
                  "lastError": None, "trustUnknown": 0, "forkSourceDenied": 0},
    }
    assert [r.url.path for r in gitlab_event.requests] == [gitlab_event.prefix + "/user"]


def test_title_uses_live_path_or_key_and_github_stays_unchanged(api, gitlab_event):
    event = gitlab_event
    created = api.post("/reviews", json={"repo": event.key, "pr": 7}).json()["id"]
    github_id = db.create_review(event.path, "owner/repo", 5, "test")
    db.set_error(event.path, github_id, "stopped", "test")
    rows = {r["id"]: r for r in api.get("/reviews").json()["reviews"]}
    assert rows[str(created)]["title"] == "allowed/sub/app #7"
    assert rows[str(github_id)]["title"] == "owner/repo #5"
    assert api.get(f"/reviews/{created}").json()["title"] == "allowed/sub/app #7"
    assert api.get(f"/reviews/{github_id}").json()["title"] == "owner/repo #5"
    with closing(db.connect(event.path)) as conn, conn:
        conn.execute("DELETE FROM forge_projects")
    assert api.get(f"/reviews/{created}").json()["title"] == f"{event.key} #7"
    rows = {r["id"]: r for r in api.get("/reviews").json()["reviews"]}
    assert rows[str(created)]["title"] == f"{event.key} #7"


def test_live_title_and_page_labels_share_one_connection(api, gitlab_event, monkeypatch):
    event = gitlab_event
    api.post("/reviews", json={"repo": event.key, "pr": 7})
    for _ in range(3):
        review_id = db.create_review(event.path, event.key, 8, "test")
        db.set_error(event.path, review_id, "stopped", "test")
    db.create_review(event.path, event.key, 9, "test")
    connect = db.connect
    count = []
    def counted(*a, **kw):
        count.append(1)
        return connect(*a, **kw)
    monkeypatch.setattr(db, "connect", counted)
    result = api.get("/reviews").json()
    assert result["live"]["title"] == "allowed/sub/app #9"
    assert len(count) == 3  # Two existing reads and one shared project lookup.
