"""Compare recorded GitLab ranges through the guarded HTTP client."""

import json
import os
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from argus import github
from argus.forge.github_adapter import GitHubForge
from argus.gitlab import reader
from argus.gitlab.client import (
    GitLabAuthError, GitLabClient, GitLabDenied, GitLabHTTPError, GitLabRateLimited,
)

FIXTURES = Path(__file__).parent / "fixtures/gitlab/spike_b"
HOST = "gitlab.example.test"
KEY = f"gitlab:{HOST}#1"
PROJECT = "/api/v4/projects/1"
COMPARE = PROJECT + "/repository/compare"
MERGE_BASE = PROJECT + "/repository/merge_base"
OLD = "a" * 40
HEAD = "b" * 40


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())["response"]["body"]


def entry(**fields):
    return fixture("b14_heads_True")["diffs"][0] | {
        "new_path": "app.py", "old_path": "app.py", "new_file": False,
        "deleted_file": False, "renamed_file": False, "collapsed": False,
        "too_large": False, "generated_file": False,
        "diff": "@@ -10 +10 @@\n-old_value\n+return value\n",
    } | fields


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    for name in list(os.environ):
        if name.startswith("ARGUS_GITLAB_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("ARGUS_GITLAB_URL", f"https://{HOST}")
    monkeypatch.setenv("ARGUS_GITLAB_TOKEN", "gl" + "pat-" + "x" * 20)
    monkeypatch.setattr(GitLabClient, "retry_delay", 0)


@pytest.fixture
def make_compare(monkeypatch):
    clients = []

    def make(*, payload=None, base=None, routes=None):
        table = {
            COMPARE: httpx.Response(200, json=(
                fixture("b14_heads_True") | {"diffs": [entry()]}
                if payload is None else payload)),
            MERGE_BASE: httpx.Response(200, json={"id": OLD} if base is None else base),
        } | (routes or {})
        requests = []

        def handle(request):
            requests.append(request)
            assert request.method == "GET"
            assert "/changes" not in request.url.path
            result = table[request.url.path]
            return result(request) if callable(result) else result

        client = GitLabClient(transport=httpx.MockTransport(handle))
        clients.append(client)
        monkeypatch.setattr(reader.gitlab_forge, "_client", client)
        return reader.GitLabForge(client), requests, table

    yield make
    for client in clients:
        client.close()


def test_normal_push_files_counts_and_rename(make_compare):
    payload = fixture("b14_heads_True") | {"diffs": [
        entry(diff="--- a/app.py\n+++ b/app.py\n@@ -10 +10,2 @@\n-old\n+new\n+extra\n\\ No newline at end of file\n"),
        entry(new_path="new.py", old_path="old.py", renamed_file=True),
    ]}
    forge, requests, _ = make_compare(payload=payload)
    result = forge.compare(KEY, OLD, HEAD)
    assert result == {"status": "ahead", "complete": True, "files": [
        {"filename": "app.py", "previous_filename": None, "status": "modified",
         "patch": "@@ -10 +10,2 @@\n-old\n+new\n+extra\n\\ No newline at end of file\n",
         "additions": 2, "deletions": 1},
        {"filename": "new.py", "previous_filename": "old.py", "status": "renamed",
         "patch": entry()["diff"], "additions": 1, "deletions": 1},
    ]}
    assert [request.url.path for request in requests] == [COMPARE, MERGE_BASE]
    assert dict(requests[0].url.params) == {"from": OLD, "to": HEAD}
    assert requests[1].url.params.multi_items() == [("refs[]", OLD), ("refs[]", HEAD)]


def test_force_push_is_diverged(make_compare):
    old = fixture("b14_old_head_readable")["id"]
    payload, base = fixture("b14_heads_False"), fixture("b14_heads_merge_base")
    assert base["id"] != old
    forge, requests, _ = make_compare(payload=payload, base=base)
    result = forge.compare(KEY, old, payload["commit"]["id"])
    assert result["status"] == "diverged"
    assert result["files"]
    assert [request.url.path for request in requests] == [COMPARE, MERGE_BASE]


def test_identical_heads_make_no_requests(make_compare):
    forge, requests, _ = make_compare()
    assert forge.compare(KEY, OLD, OLD) == {"status": "identical", "files": [], "complete": True}
    assert requests == []


@pytest.mark.parametrize("name", ["b14_large", "b14_too_large"])
def test_recorded_limits_are_incomplete(make_compare, name):
    payload = fixture(name)
    if name == "b14_large":
        recorded = json.loads((FIXTURES / f"{name}.json").read_text())
        derived = recorded["derived"]
        visible = derived["diffs"] - derived["flags"]["collapsed"]
        sample = payload["diffs"][0]
        # The fixture retains sample diffs and records every path separately.
        payload["diffs"] = [sample | {
            "new_path": path, "old_path": path,
            "collapsed": index >= visible,
            "diff": sample["diff"] if index < visible else "",
        } for index, path in enumerate(derived["paths"])]
        assert len(payload["diffs"]) == 1000
        assert sum(item["collapsed"] for item in payload["diffs"]) == 900
        assert payload["compare_timeout"] is True
    else:
        assert payload["diffs"][0]["collapsed"] is True
    forge, _, _ = make_compare(payload=payload)
    assert forge.compare(KEY, OLD, HEAD)["complete"] is False


@pytest.mark.parametrize("updates", [
    {"compare_timeout": True},
    {"diffs": [entry()] * 1000},
    {"diffs": [entry(too_large=True)]},
    {"diffs": [entry(new_path="../unsafe.py")]},
    {"diffs": [entry(generated_file=True)]},
])
def test_each_incomplete_signal(make_compare, updates):
    forge, _, _ = make_compare(payload={"diffs": [entry()], "compare_timeout": False} | updates)
    assert forge.compare(KEY, OLD, HEAD)["complete"] is False


def test_binary_only_compare_is_complete(make_compare):
    forge, _, _ = make_compare(payload={"diffs": [entry(diff="Binary files a/app.py and b/app.py differ\n")]})
    result = forge.compare(KEY, OLD, HEAD)
    assert result["complete"] is True
    assert result["files"][0]["patch"] is None
    assert (result["files"][0]["additions"], result["files"][0]["deletions"]) == (0, 0)


@pytest.mark.parametrize("route", [COMPARE, MERGE_BASE])
def test_missing_ref_or_ancestor_is_incomplete_divergence(make_compare, route):
    forge, requests, _ = make_compare(routes={route: httpx.Response(404)})
    assert forge.compare(KEY, OLD, HEAD) == {"status": "diverged", "files": [], "complete": False}
    assert {request.url.path for request in requests} <= {COMPARE, MERGE_BASE}


@pytest.mark.parametrize("route", [COMPARE, MERGE_BASE])
@pytest.mark.parametrize("status,error", [
    (429, GitLabRateLimited), (401, GitLabAuthError),
    (403, GitLabDenied), (500, GitLabHTTPError),
])
def test_other_http_errors_propagate(make_compare, route, status, error):
    forge, _, _ = make_compare(routes={route: httpx.Response(status)})
    with pytest.raises(error):
        forge.compare(KEY, OLD, HEAD)


@pytest.mark.parametrize("route,payload", [
    (COMPARE, []), (COMPARE, {}), (COMPARE, {"diffs": {}}),
    (COMPARE, {"diffs": [None]}), (COMPARE, {"diffs": ["bad"]}),
    (MERGE_BASE, []), (MERGE_BASE, {}), (MERGE_BASE, {"id": 1}),
])
def test_bad_payload_shapes_raise_typed_http_error(make_compare, route, payload):
    forge, _, _ = make_compare(routes={route: httpx.Response(200, json=payload)})
    with pytest.raises(GitLabHTTPError) as raised:
        forge.compare(KEY, OLD, HEAD)
    assert raised.value.reason == "http"


@pytest.mark.parametrize("route", [COMPARE, MERGE_BASE])
def test_invalid_json_raises_typed_http_error(make_compare, route):
    forge, _, _ = make_compare(routes={route: httpx.Response(200, content=b"invalid")})
    with pytest.raises(GitLabHTTPError) as raised:
        forge.compare(KEY, OLD, HEAD)
    assert raised.value.reason == "http"


@pytest.mark.parametrize("fields,unseen,hidden", [
    ({"diff": "Binary files a/app.py and b/app.py differ\n"}, True, False),
    ({"collapsed": True}, True, True),
    ({"too_large": True}, True, True),
    ({"generated_file": True}, True, True),
    ({"new_path": "../bad.py"}, True, True),
    ({"diff": "", "renamed_file": True}, False, False),
])
def test_mapping_distinguishes_unseen_and_hidden(fields, unseen, hidden):
    files, actual_unseen, actual_hidden = reader.map_diff_entries([entry(**fields)])
    assert actual_unseen is unseen
    assert actual_hidden is hidden
    assert not files or files[0].patch is None


def test_legacy_empty_mapping_is_hidden():
    item = entry(diff="", a_mode="100644", b_mode="100644")
    del item["collapsed"], item["too_large"]
    files, unseen, hidden = reader.map_diff_entries([item])
    assert files[0].patch is None
    assert unseen is hidden is True


def test_github_compare_preserves_request_and_response(monkeypatch):
    payload = {"status": "ahead", "files": []}
    response = Mock()
    response.json.return_value = payload
    get = Mock(return_value=response)
    monkeypatch.setattr(github, "get", get)
    assert GitHubForge().compare("owner/repo", OLD, HEAD) is payload
    get.assert_called_once_with(f"/repos/owner/repo/compare/{OLD}...{HEAD}")
    assert response.method_calls == [("raise_for_status", (), {}), ("json", (), {})]


def test_github_compare_propagates_status_error_before_json(monkeypatch):
    response = Mock()
    response.raise_for_status.side_effect = RuntimeError("failed response")
    monkeypatch.setattr(github, "get", Mock(return_value=response))
    with pytest.raises(RuntimeError, match="failed response"):
        GitHubForge().compare("owner/repo", OLD, HEAD)
    response.json.assert_not_called()
