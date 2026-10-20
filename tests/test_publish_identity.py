"""Only the publishing identity can supply retry and dedupe markers."""

import json

import httpx
import pytest

from argus import publish
from test_publish import FILES, HEAD, document, finding


@pytest.fixture(autouse=True)
def publishing(monkeypatch):
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    monkeypatch.setenv("ARGUS_PUBLISH_REPOS", "owner/repo")
    monkeypatch.setenv("ARGUS_PUBLISH_TOKEN", "identity-secret")
    monkeypatch.delenv("ARGUS_PUBLISH_BOT_LOGIN", raising=False)
    monkeypatch.setattr(publish, "_bot_logins", {}, raising=False)


def run(http, doc=None):
    return publish.publish_review("owner/repo", 42, HEAD, 7, doc or document(finding()), FILES, http=http)


def transport(*, comments=(), reviews=(), identity="publisher", user_status=200):
    calls, posts = [], []
    def handler(request):
        calls.append(request)
        if request.url.path == "/user":
            return httpx.Response(user_status, json={"login": identity})
        if request.method == "POST":
            posts.append(json.loads(request.content))
            return httpx.Response(200, json={"user": {"login": identity}})
        if request.url.path.endswith("/42"):
            return httpx.Response(200, json={"head": {"sha": HEAD}})
        return httpx.Response(200, json=list(reviews if request.url.path.endswith("/reviews") else comments))
    return httpx.Client(transport=httpx.MockTransport(handler)), calls, posts


@pytest.mark.parametrize("login", ["visitor", None, "publisher-other"])
@pytest.mark.parametrize("marker", ["fp=fp-one", "ufp=fp-one", f"review=7 head={HEAD}"])
def test_foreign_markers_cannot_suppress_or_recover_review(login, marker):
    entry = {"id": 90, "body": f"<!-- argus:{marker} -->", "user": {"login": login}}
    http, calls, posts = transport(comments=[entry], reviews=[entry])
    doc = document() if marker.startswith("ufp") else document(finding())
    if marker.startswith("ufp"):
        doc["unverified"] = [finding()]
    with http:
        result = run(http, doc)
    assert result.status == "posted" and result.reason is None
    assert result.already_posted == 0 and len(posts) == 1
    assert [r.headers["Authorization"] for r in calls if r.url.path == "/user"] == ["Bearer identity-secret"]


def test_own_markers_dedupe_case_insensitively():
    body = ("Verify the issue against the current code before changing anything.\n</details>\n\n"
            "<!-- argus:fp=fp-one -->\n\nBelow posting threshold: 0\n\nAlready posted: 0\n\n"
            f"<!-- argus:review=6 head={HEAD} -->")
    entry = {"id": 90, "body": body,
             "user": {"login": "PUBLISHER"}}
    http, _, posts = transport(reviews=[entry])
    with http:
        result = run(http)
    assert result.status == "skipped" and result.already_posted == 1
    assert posts == []


def test_login_is_cached_per_token_and_override_wins(monkeypatch):
    http, calls, _ = transport()
    with http:
        assert run(http).status == "posted"
        assert run(http).status == "posted"
        monkeypatch.setenv("ARGUS_PUBLISH_TOKEN", "second-secret")
        assert run(http).status == "posted"
        monkeypatch.setenv("ARGUS_PUBLISH_BOT_LOGIN", "configured-bot")
        assert run(http).status == "posted"
    assert [r.headers["Authorization"] for r in calls if r.url.path == "/user"] == [
        "Bearer identity-secret", "Bearer second-secret"]


@pytest.mark.parametrize("identity,status", [(None, 200), ("", 200), ("publisher", 403), ("publisher", 500)])
def test_unresolved_identity_skips_publishing(identity, status, caplog):
    http, _, posts = transport(identity=identity, user_status=status)
    with http:
        result = run(http)
    assert result.status == "skipped"
    assert "identity" in result.reason.lower() and "identity" in caplog.text.lower()
    assert "identity-secret" not in result.reason + caplog.text
    assert posts == []


def test_failed_identity_lookup_can_recover_next_run():
    bad, _, posts = transport(user_status=503)
    with bad:
        assert run(bad).status == "skipped"
    good, calls, _ = transport()
    with good:
        assert run(good).status == "posted"
    assert posts == [] and any(r.url.path == "/user" for r in calls)
