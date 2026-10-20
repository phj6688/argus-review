"""argus-mcp tools: thin wrappers whose contract is the HTTP call they make."""

import json

import pytest

pytest.importorskip("mcp")

from argus import mcp_server


class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


@pytest.fixture()
def calls(monkeypatch):
    seen = []

    class _Client:
        def __init__(self, timeout=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def request(self, method, url, json=None, headers=None):
            seen.append({"method": method, "url": url, "body": json})
            return _Response(payload={"ok": True})

    monkeypatch.setattr(mcp_server.httpx, "Client", _Client)
    return seen


def test_review_diff_posts_source_and_baseline(calls):
    mcp_server.review_diff("diff --git ...", repo="o/r", mode="fast",
                           baseline=["argusfp:aa"])
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"].endswith("/review-diff")
    assert calls[0]["body"] == {"diff": "diff --git ...", "mode": "fast",
                                "repo": "o/r", "baseline": ["argusfp:aa"]}


def test_mark_finding_carries_agent_provenance(calls):
    mcp_server.mark_finding(42, "fp", note="constant is fine", agent="ship-worker")
    assert calls[0]["url"].endswith("/findings/42/mark")
    assert calls[0]["body"] == {"mark": "fp", "marker": "agent:ship-worker",
                                "note": "constant is fine"}


def test_get_verdict_and_review_pr_routes(calls):
    mcp_server.get_verdict(7)
    mcp_server.review_pr("o/r", 12)
    assert calls[0]["url"].endswith("/reviews/7/verdict")
    assert calls[1]["body"] == {"repo": "o/r", "pr": 12}


def test_api_errors_surface_as_runtime_errors(monkeypatch):
    class _Client:
        def __init__(self, timeout=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def request(self, method, url, json=None, headers=None):
            return _Response(status_code=422, payload={"detail": "bad diff"})

    monkeypatch.setattr(mcp_server.httpx, "Client", _Client)
    with pytest.raises(RuntimeError, match="422"):
        mcp_server.review_diff("x")


def test_tools_are_registered():
    expected = {"review_diff", "review_pr", "get_verdict", "mark_finding"}
    import asyncio
    registered = {tool.name for tool in asyncio.run(mcp_server.server.list_tools())}
    assert expected <= registered


def test_review_diff_defaults_to_the_reviewing_mode(calls):
    """The agent-facing default must match the API's. Defaulting the MCP surface to
    the probe-only lane handed every no-mode caller a document that said approve
    with nothing behind it."""
    mcp_server.review_diff("diff --git ...")
    assert calls[0]["body"]["mode"] == "full"
