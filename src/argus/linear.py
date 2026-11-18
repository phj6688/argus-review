"""Minimal Linear GraphQL client: fetch one issue by its ENG identifier.

Read-only, token from LINEAR_API_KEY. Returns None (never raises) on a missing
token, a network/API error, a malformed id, or an unknown issue, so the intent
adapter degrades to 'issue unavailable' rather than breaking a review. The id is
resolved by team key + number (ENG-449 -> team key ENG, number 449) because the
GraphQL `issue(id:)` arg expects the UUID, not the human identifier.
"""

import os

import httpx

ENV_TOKEN = "LINEAR_API_KEY"
_API = "https://api.linear.app/graphql"
_QUERY = ("query($key:String!,$number:Float!){issues(filter:{team:{key:{eq:$key}},"
          "number:{eq:$number}}){nodes{identifier title description}}}")
_TIMEOUT = 15


def fetch_issue(identifier: str, *, token: str | None = None) -> dict | None:
    token = token or os.environ.get(ENV_TOKEN)
    prefix, _, number = identifier.partition("-")
    if not token or not prefix or not number.isdigit():
        return None
    try:
        response = httpx.post(
            _API, headers={"Authorization": token, "Content-Type": "application/json"},
            json={"query": _QUERY, "variables": {"key": prefix, "number": float(number)}},
            timeout=_TIMEOUT)
        response.raise_for_status()
        nodes = response.json().get("data", {}).get("issues", {}).get("nodes", [])
    except Exception:
        return None
    if not nodes:
        return None
    node = nodes[0]
    return {"identifier": node.get("identifier", identifier),
            "title": node.get("title", ""), "description": node.get("description") or ""}
