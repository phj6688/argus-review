"""Discover permitted projects and map merge requests into the watch store."""

import logging
import re
from datetime import datetime, timezone

from argus import repo_policy
from argus.api import watch_store
from argus.forge.keys import gitlab_key, parse_repo_key
from argus.forge.types import ChangeListing
from argus.gitlab import projects, trust
from argus.gitlab.client import GitLabAuthError, GitLabDenied, GitLabHTTPError, GitLabRateLimited

log = logging.getLogger(__name__)


def _project_key(host, data):
    if not isinstance(data, dict) or type(data.get("id")) is not int or not 1 <= data["id"] < 10**18:
        return None
    return gitlab_key(host, data["id"])


def expand_allow(*, client=None, db_path=None) -> tuple[str, ...]:
    entries = repo_policy.gitlab_allow_entries()
    host = projects._host()
    selected = projects._client(client)
    result = {}
    for entry in entries:
        if entry.host != host:
            continue
        text = f"gitlab:{entry.host}" + (f"#{entry.project_id}" if entry.kind == "id"
                                         else "/" + "/".join(entry.segments))
        try:
            candidates = []
            if entry.kind == "id":
                candidates.append(gitlab_key(entry.host, entry.project_id))
            else:
                try:
                    for page in selected.pages("group_projects", group="/".join(entry.segments), query={
                        "with_shared": False, "include_subgroups": True, "archived": False,
                        "simple": True, "per_page": 100,
                    }):
                        for data in page.json():
                            key = _project_key(entry.host, data)
                            path = data.get("path_with_namespace") if isinstance(data, dict) else None
                            if key and isinstance(path, str):
                                segments = tuple(part.casefold() for part in path.split("/"))
                                if segments[:len(entry.segments)] == entry.segments:
                                    candidates.append(key)
                except (GitLabDenied, GitLabHTTPError) as error:
                    # The client classifies group 404s differently from project 404s.
                    if not ((isinstance(error, GitLabDenied) and error.reason == "not_found")
                            or (isinstance(error, GitLabHTTPError) and error.status_code == 404)):
                        raise
                    data = selected.get("project_by_path", project_path="/".join(entry.segments)).json()
                    key = _project_key(entry.host, data)
                    if key:
                        candidates.append(key)
            for key in candidates:
                decision = projects.live_deny(key, client=selected, db_path=db_path)
                if decision.reason == "lookup failed: auth":
                    raise GitLabAuthError("GitLab authentication failed")
                if decision.reason == "lookup failed: rate_limited":
                    raise GitLabRateLimited(selected.pause_until())
                if decision.allowed:
                    result[key] = None
        except (GitLabAuthError, GitLabRateLimited):
            raise
        except Exception:
            log.warning("GitLab allow entry skipped: %s", text)
    return tuple(result)


def _utc(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _row(repo, item, updated, client, cache):
    author = item.get("author")
    author = author if isinstance(author, dict) else {}
    username = author.get("username")
    username = username if isinstance(username, str) and username else ""
    user_id = author.get("id") if type(author.get("id")) is int else None
    trusted = trust.author_trusted(repo, user_id, client=client, cache=cache)
    try:
        merged_at = _utc(item["merged_at"]).isoformat()
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        merged_at = None
    source = item.get("source_project_id")
    return {
        "number": item["iid"], "head": {"sha": item.get("sha")}, "updated_at": updated.isoformat(),
        "draft": bool(item.get("draft", item.get("work_in_progress", False))),
        "user": {"login": username, "type": "Bot" if trust.is_bot(author) else "User"},
        "base": {"ref": item.get("target_branch")}, "author_trusted": int(trusted) if trusted is not None else None,
        "author_id": user_id, "merged_at": merged_at,
        "source_project_id": source if type(source) is int else None,
    }


def _listing(repo, since, complete, client, cache):
    selected = projects._client(client)
    ref = parse_repo_key(repo)
    if ref.forge != "gitlab" or ref.host != projects._host():
        raise ValueError("Invalid GitLab repository")
    cache = cache if cache is not None else trust.TrustCache()
    query = {"state": "opened" if complete else "all", "per_page": 100,
             "order_by": "updated_at", "sort": "asc"}
    if not complete:
        query["updated_after"] = watch_store.poll_window_start(since, overlap_seconds=300)
    opened, closed, max_seen, pending = [], [], None, None
    for page in selected.pages("mr_list", project_id=ref.project_id, query=query):
        for item in page.json():
            try:
                if not isinstance(item, dict) or type(item.get("iid")) is not int:
                    raise ValueError
                updated = _utc(item.get("updated_at"))
            except (ValueError, TypeError, AttributeError, OverflowError):
                log.debug("GitLab listing row skipped")
                continue
            max_seen = max(max_seen, updated) if max_seen is not None else updated
            is_open = complete or item.get("state") in {"opened", "locked"}
            sha = item.get("sha")
            if is_open and (not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", sha)):
                pending = min(pending, updated) if pending is not None else updated
                log.debug("GitLab listing head preparing")
                continue
            if is_open or item.get("state") in {"closed", "merged"}:
                (opened if is_open else closed).append(_row(repo, item, updated, selected, cache))
    cursor = min(max_seen, pending) if pending is not None else max_seen
    return ChangeListing(tuple(opened), tuple(closed), cursor.isoformat() if cursor else since,
                         complete and pending is None)


def list_changes(repo, since, *, client=None, cache=None) -> ChangeListing:
    if since is None:
        raise ValueError("cursor required; use full_listing")
    return _listing(repo, since, False, client, cache)


def full_listing(repo, *, client=None, cache=None) -> ChangeListing:
    """A preparing open head makes the listing incomplete to preserve stored work."""
    return _listing(repo, None, True, client, cache)
