"""Deployment policy uses explicit scope and fails closed for unknown authors."""

import asyncio
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from argus import github, repo_policy
from argus.api import db, main, watch_config, watch_store, watcher
from argus.jarvis import live


@pytest.fixture
def path(tmp_path, monkeypatch, caplog):
    target = str(tmp_path / "argus.db")
    monkeypatch.setenv("ARGUS_DB_PATH", target)
    for key in ("ARGUS_DENY_OWNERS", "ARGUS_WATCH_OWNERS", "ARGUS_WATCH_TRUSTED_ONLY",
                "ARGUS_LINEAR_REPOS", "ARGUS_LINEAR_KEY_PREFIX"):
        monkeypatch.delenv(key, raising=False)
    db.init_db(target)
    monkeypatch.setattr(github, "_quota", {})
    monkeypatch.setattr(github, "_pause_until", 0)
    monkeypatch.setattr(github, "_cache", github.OrderedDict())
    monkeypatch.setattr(watcher, "_ticks", {})
    logger = logging.getLogger("argus.watcher")
    logger.addHandler(caplog.handler)
    caplog.set_level(logging.INFO, logger="argus.watcher")
    try:
        yield target
    finally:
        logger.removeHandler(caplog.handler)


def pull(association="OWNER"):
    return {"number": 1, "head": {"sha": "a" * 40}, "author_association": association}


@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_trusted_authors_are_queued(path, association):
    assert watch_store.record_listing(path, "example/repo", [pull(association)]) == 1
    assert watch_store.claim(path)["author_association"] == association


@pytest.mark.parametrize("association", [None, "", "NONE", "CONTRIBUTOR", "FIRST_TIMER", "UNKNOWN"])
@pytest.mark.parametrize("merged", [False, True])
def test_untrusted_authors_are_skipped_and_logged(path, association, merged, caplog):
    pr = pull(association)
    if association is None:
        pr.pop("author_association")
    if merged:
        pr["merged_at"] = datetime.now(timezone.utc).isoformat()
    record = watch_store.record_fast_merges if merged else watch_store.record_listing
    assert record(path, "example/repo", [pr]) == 0
    assert watch_store.claim(path) is None
    assert "untrusted author association" in caplog.text


@pytest.mark.parametrize("value", ["0", "false", "no", "off", " FALSE ", "Off"])
def test_trust_filter_can_be_explicitly_disabled(path, monkeypatch, value):
    monkeypatch.setenv("ARGUS_WATCH_TRUSTED_ONLY", value)
    assert watch_store.record_listing(path, "example/repo", [pull(None)]) == 1
    assert watch_store.claim(path) is not None


@pytest.mark.parametrize("value", ["", "1", "true", "unexpected"])
def test_trust_filter_stays_on_unless_explicitly_disabled(path, monkeypatch, value):
    monkeypatch.setenv("ARGUS_WATCH_TRUSTED_ONLY", value)
    assert watch_store.record_listing(path, "example/repo", [pull(None)]) == 0


@pytest.mark.parametrize("merged", [False, True])
def test_enabling_trust_filter_rechecks_queued_work(path, monkeypatch, merged):
    monkeypatch.setenv("ARGUS_WATCH_TRUSTED_ONLY", "off")
    pr = pull("CONTRIBUTOR")
    pr["merged_at"] = datetime.now(timezone.utc).isoformat()
    record = watch_store.record_fast_merges if merged else watch_store.record_listing
    assert record(path, "example/repo", [pr]) == 1
    monkeypatch.delenv("ARGUS_WATCH_TRUSTED_ONLY")
    assert watch_store.claim(path) is None
    assert watch_store.heads(path)[0]["status"] == "skipped"


def test_trusted_author_can_be_reconsidered_on_same_head(path):
    assert watch_store.record_listing(path, "example/repo", [pull(None)]) == 0
    assert watch_store.record_listing(path, "example/repo", [pull("MEMBER")]) == 1
    assert watch_store.claim(path)["author_association"] == "MEMBER"


@pytest.mark.parametrize("listing", ["_open_prs", "_recently_closed_prs"])
def test_rest_listing_preserves_association(path, monkeypatch, listing):
    monkeypatch.setenv("GITHUB_TOKEN", "fake")
    monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Response(
        200, json=[pull("COLLABORATOR")], request=httpx.Request("GET", url)))
    assert getattr(watcher, listing)("example/repo")[0]["author_association"] == "COLLABORATOR"


@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR", "NONE", None, "UNKNOWN"])
def test_linear_fetch_and_injection_require_trusted_metadata(path, monkeypatch, association):
    monkeypatch.setenv("ARGUS_LINEAR_REPOS", "example/repo")
    monkeypatch.setenv("ARGUS_LINEAR_KEY_PREFIX", "TASK")
    monkeypatch.setenv("ARGUS_WATCH_TRUSTED_ONLY", "off")
    def get(url, params=None):
        body = [] if url.endswith("/commits") else {
            "title": "TASK-12 fix", "author_association": association}
        return httpx.Response(200, json=body, request=httpx.Request("GET", github.GITHUB_API + url))
    monkeypatch.setattr(github, "get", get)
    fetched = []
    monkeypatch.setattr(live.linear, "fetch_issue", lambda key: fetched.append(key) or {
        "identifier": key, "title": "Private intent", "description": "Sensitive details"})
    section = live.intent_section("example/repo", 1)
    if association in {"OWNER", "MEMBER", "COLLABORATOR"}:
        assert fetched == ["TASK-12"]
        assert "Sensitive details" in section
    else:
        assert fetched == []
        assert section is None


@pytest.mark.parametrize("missing", ["ARGUS_LINEAR_REPOS", "ARGUS_LINEAR_KEY_PREFIX"])
def test_linear_lane_defaults_off(path, monkeypatch, missing):
    monkeypatch.setenv("ARGUS_LINEAR_REPOS", "example/repo")
    monkeypatch.setenv("ARGUS_LINEAR_KEY_PREFIX", "TASK")
    monkeypatch.delenv(missing)
    monkeypatch.setattr(live.linear, "fetch_issue", lambda key: pytest.fail("Linear read"))
    assert live.intent_section("example/repo", 1, {
        "title": "TASK-1", "body": "", "commits": [], "author_association": "OWNER"}) is None


def test_hard_denials_are_dynamic_and_default_empty(path, monkeypatch):
    assert not repo_policy.is_denied("acme-corp/secret-repo")
    monkeypatch.setenv("ARGUS_DENY_OWNERS", " Acme-Corp , Example/Secret-Repo, ,")
    assert repo_policy.is_denied("ACME-CORP/anything")
    assert repo_policy.is_denied("EXAMPLE/SECRET-REPO")
    assert not repo_policy.is_denied("example/public-repo")
    assert repo_policy.is_denied("other/repo", ["OTHER"])
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "")
    assert not repo_policy.is_denied("acme-corp/secret-repo")


def test_hard_repo_denial_applies_before_http_transport(path, monkeypatch):
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "Acme-Corp/Secret-Repo")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: pytest.fail("request sent"))
    with pytest.raises(github.GitHubDenied):
        github.get("/repos/ACME-CORP/secret-repo/pulls")
    github.guard_url("https://api.github.com/repos/acme-corp/public-repo/pulls")


@pytest.mark.parametrize("denied,route", [
    ("acme-corp/contents", "/repos/acme-corp/contents/pulls"),
    ("contents", "/repos/contents/secret-repo/pulls"),
    ("contents", "/orgs/contents"),
])
def test_contents_name_cannot_hide_a_denied_target(path, monkeypatch, denied, route):
    monkeypatch.setenv("ARGUS_DENY_OWNERS", denied)
    with pytest.raises(github.GitHubDenied):
        github.guard_url(github.GITHUB_API + route)


@pytest.mark.parametrize("query,variables", [
    ('query { repository(owner: "acme-corp", name: "secret-repo") { id } }', {}),
    ('query($owner:String!,$name:String!) { repository(owner:$owner,name:$name) { id } }',
     {"owner": "ACME-CORP", "name": "SECRET-REPO"}),
    ('query { organization(login:"acme-corp") { repository(name:"secret-repo") { id } } }', {}),
    ('query($owner:String="acme-corp",$name:String="secret-repo") '
     '{ repository(owner:$owner,name:$name) { id } }', {}),
    ('query { repository(owner: # comment\n "acme-corp", name:"secret-repo") { id } }', {}),
    ('query($name:String!) { repository(owner:"acme-corp",name:$name) { id } }',
     {"name": "secret-repo"}),
])
def test_hard_repo_denial_applies_to_graphql(path, monkeypatch, query, variables):
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "acme-corp/secret-repo")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("request sent"))
    with pytest.raises(github.GitHubDenied):
        github.graphql_read(query, variables)


def test_hard_repo_denial_allows_other_repos_in_graphql(path, monkeypatch):
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "acme-corp/secret-repo")
    monkeypatch.setenv("GITHUB_TOKEN", "fake")
    monkeypatch.setattr(github, "_graphql_pause_until", 0)
    monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Response(
        200, json={"data": {"repository": {"id": "public"}}}, request=httpx.Request("POST", url)))
    result = github.graphql_read(
        'query($owner:String!,$name:String!) { repository(owner:$owner,name:$name) { id } }',
        {"owner": "acme-corp", "name": "public-repo"},
    )
    assert result == {"repository": {"id": "public"}}


def test_config_cannot_remove_env_denials(path, monkeypatch):
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "Acme-Corp,Example/Secret-Repo")
    client = TestClient(main.app)
    response = client.put("/config", json={
        "repos": [], "model": main.DEFAULT_MODEL, "bugCap": 5, "minConfidence": 0.5,
        "deny": ["other/repo"], "hard_denied_owners": [],
    })
    assert response.status_code == 200
    assert response.json()["hard_denied_owners"] == ["acme-corp", "example/secret-repo"]
    assert response.json()["deny"] == ["other/repo"]
    for repo in ("acme-corp/anything", "example/secret-repo", "other/repo"):
        assert client.post("/reviews", json={"repo": repo, "pr": 1}).status_code == 403
    assert watcher.stats(path)["hard_denied_owners"] == ["acme-corp", "example/secret-repo"]
    assert "hard_deny_refusals" in watcher.stats(path)


def test_empty_watch_scope_warns_and_watches_nothing(path, monkeypatch, caplog):
    monkeypatch.setenv("ARGUS_WATCH_ENABLED", "1")
    monkeypatch.setattr(github, "owned_repo_records", lambda: pytest.fail("owner listing without scope"))
    monkeypatch.setattr(watcher, "_open_prs", lambda repo: pytest.fail("PR listing"))
    assert repo_policy.watch_owners() == set()
    assert asyncio.run(watcher.poll_once(path)) == 0
    assert watcher._watched_repos(path) == []
    assert "ARGUS_WATCH_OWNERS" in caplog.text
    assert "watching nothing" in caplog.text


def test_hard_denials_filter_discovery_at_call_time(path, monkeypatch):
    monkeypatch.setenv("ARGUS_WATCH_OWNERS", " ACME-CORP, Example ")
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "acme-corp, example/secret-repo")
    monkeypatch.setattr(github, "owned_repo_records", lambda: [
        {"full_name": name, "pushed_at": datetime.now(timezone.utc).isoformat()}
        for name in ("acme-corp/anything", "example/secret-repo", "example/public-repo")
    ])
    assert repo_policy.watch_owners() == {"example"}
    watcher.discover_repos(path)
    assert watcher._watched_repos(path) == ["example/public-repo"]
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "example/public-repo")
    assert watcher._watched_repos(path) == []


def test_legacy_queue_association_migration_fails_closed(path):
    with watch_store.connection(path) as conn:
        conn.execute("ALTER TABLE pr_heads DROP COLUMN author_association")
        conn.execute(
            "INSERT INTO pr_heads(repo,pr,head_sha,first_seen_at,status) VALUES(?,?,?,?,'queued')",
            ("example/repo", 1, "a" * 40, datetime.now(timezone.utc).isoformat()),
        )
    db.init_db(path)
    db.init_db(path)
    assert watch_store.heads(path)[0]["author_association"] is None
    assert watch_store.claim(path) is None


def test_explicit_repo_can_be_watched_without_owner_scope(path, monkeypatch):
    db.save_config(path, repos=[{"name": "example/repo", "watched": True}],
                   model=main.DEFAULT_MODEL, bug_cap=5, min_confidence=0.5)
    monkeypatch.setattr(github, "owned_repo_records", lambda: [{
        "full_name": "example/repo", "pushed_at": datetime.now(timezone.utc).isoformat()}])
    watcher.discover_repos(path)
    assert watcher._watched_repos(path) == ["example/repo"]
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "example/repo")
    assert watch_config.effective(db.get_config(path)) == []


@pytest.mark.parametrize("overrides,expected", [
    ({}, ["http://localhost:8000", ["http://localhost:3000"]]),
    ({"ARGUS_PUBLIC_API_URL": "https://api.example.com",
      "ARGUS_CORS_ORIGINS": "https://example.com, https://console.example.com"},
     ["https://api.example.com", ["https://example.com", "https://console.example.com"]]),
])
def test_api_url_defaults_and_overrides(overrides, expected):
    import json

    env = {k: v for k, v in os.environ.items()
           if k not in {"ARGUS_PUBLIC_API_URL", "ARGUS_CORS_ORIGINS"}}
    output = subprocess.check_output([sys.executable, "-c",
        "import json; from argus.api.main import API_BASE_URL, _cors_origins; "
        "print(json.dumps([API_BASE_URL, _cors_origins]))"], env={**env, **overrides}, text=True)
    assert json.loads(output) == expected


def test_env_example_documents_safe_defaults():
    lines = Path(".env.example").read_text().splitlines()
    for entry in ("ARGUS_WATCH_TRUSTED_ONLY=1", "ARGUS_LINEAR_REPOS=",
                  "ARGUS_LINEAR_KEY_PREFIX=", "ARGUS_DENY_OWNERS=", "ARGUS_WATCH_OWNERS=",
                  "ARGUS_PUBLIC_API_URL=http://localhost:8000",
                  "ARGUS_CORS_ORIGINS=http://localhost:3000"):
        assert entry in lines
