"""Keep the MCP contract stable while the API accepts GitLab keys."""

import asyncio
import hashlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from argus import mcp_server
from argus.api import db, main
from argus.forge.github_adapter import github_forge
from argus.review import ReviewResult


# Hashes from origin/dev normalize only description whitespace, which differs
# between Python versions. Every other schema field stays exact.
PARENT_TOOL_SCHEMAS = {
    "review_diff": "c298fc73c59303e099fd88a3e2555b9413bb7dd01730fb498a6dff946f6e5d15",
    "review_pr": "91e636fb6fcd0836fe84ce8cfeff7f79151da46c0fcf40c625757ba9b0aabb70",
    "get_verdict": "99d5eaeccc82e80c3c63b7774a8be53dc900f7c4f577605c7629063b9a936889",
    "mark_finding": "85ae1504b72bd8a20b011a59a988e599fbe2df9325ae2665078928920a7009f1",
}


@pytest.fixture
def api_path(gitlab_event, monkeypatch):
    monkeypatch.setattr(main, "run_review", lambda *a, **kw: ReviewResult(0, [], [], 0))
    monkeypatch.setattr(github_forge, "head_sha", lambda *a: "a" * 40)
    client = TestClient(main.app)
    calls = []
    def through_api(path, method="GET", body=None):
        calls.append((path, method, body))
        response = client.request(method, path, json=body)
        if response.status_code >= 400:
            raise RuntimeError(f"argus-api {response.status_code} on {path}: {response.text}")
        return response.json()
    monkeypatch.setattr(mcp_server, "_api", through_api)
    return calls


@pytest.mark.parametrize("repo_form", ["path_key", "key", "github"])
def test_review_pr_forms_return_same_result_keys(gitlab_event, api_path, repo_form):
    repo = "owner/repo" if repo_form == "github" else getattr(gitlab_event, repo_form)
    result = json.loads(mcp_server.review_pr(repo, 7))
    assert set(result) == {"id", "status"}
    assert result["status"] == "running"
    stored = db.get_review(gitlab_event.path, result["id"])
    assert stored["repo"] == ("owner/repo" if repo_form == "github" else gitlab_event.key)
    assert api_path == [("/reviews", "POST", {"repo": repo, "pr": 7})]


@pytest.mark.parametrize("kind", ["denied", "not_allowlisted", "hard_denied", "fork"])
def test_mcp_path_denies_before_diff(gitlab_event, api_path, kind):
    expected = gitlab_event.deny(kind)
    with pytest.raises(RuntimeError) as caught:
        mcp_server.review_pr(gitlab_event.key, 7)
    assert f'403 on /reviews: {{"detail":"repository denied: {gitlab_event.key}"}}' in str(caught.value)
    assert [r.url.path for r in gitlab_event.requests] == expected


def test_review_pr_path_lookup_failure_reports_502(gitlab_event, api_path):
    event = gitlab_event
    lookup = event.prefix + "/projects/allowed/sub/app"
    event.responses[lookup] = httpx.Response(500, text=event.token)
    with pytest.raises(RuntimeError) as caught:
        mcp_server.review_pr(event.path_key, 7)
    assert "502 on /reviews" in str(caught.value)
    assert "gitlab lookup failed: gitlab_http" in str(caught.value)
    assert event.token not in str(caught.value)
    assert {r.url.path for r in event.requests} == {lookup}


def test_review_diff_refused_before_http(gitlab_event, monkeypatch):
    monkeypatch.setattr(mcp_server, "_api", lambda *a, **kw: pytest.fail("Unexpected API request"))
    with pytest.raises(ValueError, match="^review_diff supports GitHub repositories only$"):
        mcp_server.review_diff("x", repo=gitlab_event.key)
    assert gitlab_event.requests == []


def test_gitlab_off_returns_neutral_api_refusal(gitlab_event, api_path, monkeypatch):
    monkeypatch.delenv("ARGUS_GITLAB_URL")
    with pytest.raises(RuntimeError, match="GitLab is not configured"):
        mcp_server.review_pr(gitlab_event.key, 7)
    assert gitlab_event.requests == []


def test_parent_tool_schemas_are_byte_identical(monkeypatch):
    monkeypatch.delenv("ARGUS_GITLAB_URL", raising=False)
    schemas = asyncio.run(mcp_server.server.list_tools())
    actual = {}
    for tool in schemas:
        schema = tool.model_dump(mode="json")
        schema["description"] = " ".join(schema["description"].split())
        actual[tool.name] = hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()
    assert actual == PARENT_TOOL_SCHEMAS
    assert set(mcp_server.TOOL_NAMES) == set(actual)
