"""MCP surface over the Argus API, so coding agents call reviews as tools.

Thin by design: every tool is one HTTP call to the running argus-api (the
container owns the DB and the engine); this process holds no state. Runs on
stdio for the local Claude Code registration:

    claude mcp add argus -- <repo>/.venv/bin/argus-mcp

ARGUS_API points it at a non-default API (default http://127.0.0.1:8350).

Marks written here carry marker "agent:<name>" so they land as machine
provenance: corpus and precision math count only human (marker-less) labels,
and the agent outcome stream stays separately measurable, exactly like the
nightly automark lane.
"""

import json
import os
from typing import Any

import httpx

from mcp.server.fastmcp import FastMCP

API_BASE_ENV = "ARGUS_API"
DEFAULT_API_BASE = "http://127.0.0.1:8350"
_TIMEOUT = httpx.Timeout(30.0, read=900.0)  # a full review holds the socket for minutes

server = FastMCP("argus")

# The tools this server actually exposes. Single source of truth so the console's
# /integrations page imports it and can never drift from the real surface.
TOOL_NAMES = ("review_diff", "review_pr", "get_verdict", "mark_finding")


def _api(path: str, method: str = "GET", body: dict | None = None) -> Any:
    base = os.environ.get(API_BASE_ENV, DEFAULT_API_BASE).rstrip("/")
    token = os.environ.get("ARGUS_API_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with httpx.Client(timeout=_TIMEOUT) as client:
        response = client.request(method, f"{base}{path}", json=body, headers=headers)
    if response.status_code >= 400:
        detail = response.text[:500]
        raise RuntimeError(f"argus-api {response.status_code} on {path}: {detail}")
    return response.json()


@server.tool()
def review_diff(diff: str, repo: str = "", mode: str = "full",
                baseline: list[str] | None = None) -> str:
    """Review a unified diff (git diff output) without needing a GitHub PR.

    mode="full" (the default): the whole reviewer pipeline in the background;
    returns {id, status} - poll get_verdict(id). This is the only mode that reads
    the code.

    mode="fast": deterministic incident probes only, no reviewer, returns the
    verdict document synchronously in about a second. The probe catalogue is a
    small set of regression checks globbed to specific filenames, so on most diffs
    it executes ZERO checks. It can therefore surface a known incident pattern but
    can never clear a change: a finding-free fast run reports
    verdict="insufficient_evidence", never "approve". Read the `coverage` block to
    see which lanes ran. Use it as a cheap tripwire, never as a gate.

    Pass repo ("owner/name") when known: it unlocks the repo's house rules,
    known-FP suppression, and (with head_sha) the full-fidelity probe set.
    baseline: fingerprints from a prior verdict, so the response marks findings
    new/unchanged and reports resolved ones - the "did my fix clear finding X"
    answer.
    """
    body: dict = {"diff": diff, "mode": mode}
    if repo:
        body["repo"] = repo
    if baseline:
        body["baseline"] = baseline
    return json.dumps(_api("/review-diff", "POST", body))


@server.tool()
def review_pr(repo: str, pr: int) -> str:
    """Start a full review of an open GitHub PR (owner/name, number).

    Returns {id, status} immediately; poll get_verdict(id) for the result.
    """
    return json.dumps(_api("/reviews", "POST", {"repo": repo, "pr": pr}))


@server.tool()
def get_verdict(review_id: int) -> str:
    """Fetch the machine verdict for a review (PR or diff).

    Still-running reviews return {"status": "running"} - poll again. Done
    reviews return the verdict document: decision (approve/comment/
    changes_requested), findings with fingerprints, severities, confidence,
    lanes and evidence tiers (deterministic > consensus > single_critic >
    disputed), plus baseline resolution when a baseline was given.
    """
    return json.dumps(_api(f"/reviews/{review_id}/verdict"))


@server.tool()
def mark_finding(finding_id: int, mark: str, note: str = "", agent: str = "claude") -> str:
    """Record an outcome label on a finding after acting on it.

    mark: "real" (confirmed and fixed), "fp" (wrong - say why in note), "stale",
    or "unsure". Labels feed the reviewer's corpus as machine provenance
    (marker agent:<agent>); they never pollute human precision. Marking findings
    you fixed or rejected is what makes the reviewer improve.
    """
    body = {"mark": mark, "marker": f"agent:{agent}", "note": note or None}
    return json.dumps(_api(f"/findings/{finding_id}/mark", "POST", body))


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
