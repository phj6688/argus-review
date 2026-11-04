"""Resolve stable GitLab identities before applying live repository policy."""

import logging
from contextlib import closing
from pathlib import Path

from argus import repo_policy
from argus.api import db
from argus.forge.keys import canonical_host, gitlab_key, parse_entry, parse_repo_key
from argus.forge.types import ChangeMeta, RepoRecord, RepoRef
from argus.gitlab import config
from argus.gitlab.client import (
    GitLabAuthError, GitLabClient, GitLabDenied, GitLabRateLimited, client as shared_client,
)
from argus.repo_policy import GitLabDecision

log = logging.getLogger(__name__)
_LOOKUP_REASONS = frozenset({
    "forbidden", "not_found", "redirect_refused", "guard", "http", "transport",
    "response_too_large", "decoding", "too_many_pages",
})


class GitLabProjectError(ValueError):
    """Project metadata or its configured identity cannot be trusted."""


class GitLabInputError(ValueError):
    """A project input cannot be resolved safely."""


def _host() -> str:
    base = config.base_url()
    if base is None:
        raise GitLabProjectError("GitLab is not configured")
    return canonical_host(base.host, base.port)


def _client(client: GitLabClient | None) -> GitLabClient:
    selected = client or shared_client()
    if selected is None:
        raise GitLabProjectError("GitLab client is unavailable")
    return selected


def _record(data: object, project_id: int) -> RepoRecord:
    if not isinstance(data, dict) or type(data.get("id")) is not int or data["id"] != project_id:
        raise GitLabProjectError("Invalid GitLab project response")
    path, archived = data.get("path_with_namespace"), data.get("archived")
    branch, visibility = data.get("default_branch"), data.get("visibility")
    if (not isinstance(path, str) or not path.strip() or type(archived) is not bool
            or (branch is not None and not isinstance(branch, str))
            or (visibility is not None and not isinstance(visibility, str))):
        raise GitLabProjectError("Invalid GitLab project response")
    return RepoRecord(path, archived, branch, visibility)


def resolve(ref: RepoRef, *, client: GitLabClient | None = None,
            db_path: str | None = None) -> RepoRecord:
    if (not isinstance(ref, RepoRef) or ref.forge != "gitlab" or ref.host != _host()
            or type(ref.project_id) is not int or not 1 <= ref.project_id < 10**18):
        raise GitLabProjectError("Invalid GitLab project identity")
    response = _client(client).get("project", project_id=ref.project_id)
    try:
        data = response.json()
    except ValueError:
        raise GitLabProjectError("Invalid GitLab project response") from None
    record = _record(data, ref.project_id)
    with closing(db.connect(db_path)) as conn, conn:
        db.upsert_forge_project(conn, ref, record, visibility=record.visibility)
    return record


def resolve_input(text: str, *, client: GitLabClient | None = None) -> str:
    try:
        host = _host()
        entry = parse_entry(text if ":" in text else f"gitlab:{host}/{text}")
        if entry.host != host:
            raise ValueError
        if entry.kind == "id":
            parse_repo_key(text)
            return text
        if entry.kind != "path" or len(entry.segments) < 2:
            raise ValueError
        path = text[7:].partition("/")[2] if ":" in text else text
        data = _client(client).get("project_by_path", project_path=path).json()
        if not isinstance(data, dict):
            raise ValueError
        key = gitlab_key(host, data.get("id"))
        parse_repo_key(key)
        return key
    except Exception as error:
        if isinstance(error, GitLabDenied) and error.reason in {"redirect_refused", "not_found"}:
            raise GitLabInputError("GitLab project path is unavailable; use the new path or the numeric id") from None
        raise GitLabInputError("GitLab project input could not be resolved") from None


def live_deny(key: str, *, client: GitLabClient | None = None,
              db_path: str | None = None) -> GitLabDecision:
    try:
        ref = parse_repo_key(key)
        path = db_path if db_path is not None else db.db_path()
        record = resolve(ref, client=client, db_path=path)
        stored = db.get_config(path) if Path(path).exists() else None
        deny_entries = stored["deny"] if stored is not None else ()
        return repo_policy.gitlab_decision(ref, record, deny_entries, repo_policy.gitlab_allow_entries())
    except Exception as error:
        reason = getattr(error, "reason", None)
        # Only known classifications may leave this boundary, never exception text.
        if not isinstance(reason, str) or reason not in _LOOKUP_REASONS:
            reason = {
                GitLabAuthError: "auth", GitLabRateLimited: "rate_limited",
                GitLabProjectError: "invalid_project", repo_policy.GitLabPolicyError: "policy_invalid",
            }.get(type(error), "failed")
        log.debug("GitLab project lookup denied: %s", reason)
        return GitLabDecision(False, f"lookup failed: {reason}")


def live_deny_change(key: str, meta: ChangeMeta, *, client: GitLabClient | None = None,
                     db_path: str | None = None) -> GitLabDecision:
    target = live_deny(key, client=client, db_path=db_path)
    if not target.allowed:
        return target
    source = meta.source_repo
    if isinstance(source, RepoRef) and source.forge == "gitlab":
        ref = parse_repo_key(key)
        if (source.host, source.project_id) != (ref.host, ref.project_id):
            try:
                source_key = gitlab_key(source.host, source.project_id)
            except ValueError:
                return GitLabDecision(False, "fork_source_denied")
            if not live_deny(source_key, client=client, db_path=db_path).allowed:
                return GitLabDecision(False, "fork_source_denied")
    return target
