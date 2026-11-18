"""Resolve review inputs and enforce live policy before reading a change."""

import re
from urllib.parse import urlsplit

from argus.errors import HeadMoved, classify_error
from argus.forge import forge_for
from argus.forge.keys import PolicyEntry, canonical_host, parse_entry, parse_repo_key
from argus.gitlab import config, projects
from argus.gitlab.client import GitLabAuthError, GitLabDenied, GitLabHTTPError, GitLabRateLimited
from argus.repo_policy import validate_repo


class EntryRefused(ValueError):
    """A review input cannot be used safely."""


class EntryDenied(Exception):
    """Live repository policy refuses this review."""


class EntryUnavailable(Exception):
    """A lookup failed without a policy decision."""

    def __init__(self, classification: str):
        self.classification = classification
        super().__init__(classification)


def _base():
    try:
        base = config.base_url()
    except config.GitLabConfigError:
        raise EntryRefused("Invalid GitLab configuration") from None
    if base is None:
        raise EntryRefused("GitLab is not configured")
    return base


def parse_mr_url(text: str) -> tuple[str, int]:
    base = _base()
    try:
        # URL parsers discard some controls and normalize traversal.
        if re.search(r"[\s%\\\x00-\x1f\x7f-\x9f]", text):
            raise ValueError
        url = urlsplit(text)
        host = canonical_host(url.hostname or "", url.port)
        if (url.scheme != "https" or host != canonical_host(base.host, base.port)
                or not re.fullmatch(r"(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(?::[0-9]+)?", url.netloc)):
            raise ValueError
        segments = url.path.removesuffix("/").split("/")[1:]
        prefix = base.path_prefix.split("/")[1:]
        if (segments[:len(prefix)] != prefix or len(segments) < len(prefix) + 5
                or any(s in {"", ".", ".."} for s in segments)
                or segments[-3:-1] != ["-", "merge_requests"]
                or not re.fullmatch(r"[0-9]+", segments[-1]) or int(segments[-1]) <= 0):
            raise ValueError
        path = "/".join(segments[len(prefix):-3])
        entry = parse_entry(f"gitlab:{host}/{path}")
        if entry.kind != "path" or len(entry.segments) < 2:
            raise ValueError
        return path, int(segments[-1])
    except ValueError:
        raise EntryRefused("Invalid GitLab merge request URL") from None


def mr_url_args(text: str) -> tuple[str, str]:
    path, iid = parse_mr_url(text)
    base = _base()
    return f"gitlab:{canonical_host(base.host, base.port)}/{path}", str(iid)


def check_repo_syntax(text: str) -> PolicyEntry:
    """Validate a configured GitLab key without resolving its project."""
    base = _base()
    try:
        parsed = parse_entry(text)
        if parsed.host != canonical_host(base.host, base.port):
            raise EntryRefused("GitLab repository host does not match configuration")
        if parsed.kind == "id":
            parse_repo_key(text)
        elif parsed.kind != "path" or len(parsed.segments) < 2:
            raise ValueError
        return parsed
    except EntryRefused:
        raise
    except ValueError:
        raise EntryRefused("Invalid GitLab repository input") from None


def canonical_repo(text: str) -> str:
    parsed = check_repo_syntax(text)
    try:
        key = text if parsed.kind == "id" else projects.resolve_input(text)
        return validate_repo(key, allow_gitlab=True)
    except EntryRefused:
        raise
    except projects.GitLabInputError as error:
        raise EntryRefused(str(error)) from None
    except (GitLabHTTPError, GitLabRateLimited, GitLabAuthError) as error:
        raise EntryUnavailable(_classification(error)) from None
    except ValueError:
        raise EntryRefused("Invalid GitLab repository input") from None


def _classification(error: Exception) -> str:
    classification = classify_error(error)
    return classification if classification in {
        "gitlab_auth", "gitlab_denied", "gitlab_rate_limited", "gitlab_http",
    } else "gitlab_http"


def _check_decision(repo, decision):
    if decision.allowed:
        return
    if decision.reason.startswith("lookup failed: "):
        reason = decision.reason.removeprefix("lookup failed: ")
        if reason in {"auth", "rate_limited"}:
            raise EntryUnavailable("gitlab_" + reason)
        if reason not in {"forbidden", "not_found", "redirect_refused", "guard", "policy_invalid"}:
            raise EntryUnavailable("gitlab_http")
    raise EntryDenied(f"repository denied: {repo}")


def check_change(repo: str, pr: int) -> None:
    try:
        _check_decision(repo, projects.live_deny(repo))
        meta = forge_for(repo).change_meta(repo, pr)
        _check_decision(repo, projects.live_deny_change(repo, meta))
    except HeadMoved:
        raise EntryUnavailable("head_moved") from None
    except (EntryDenied, EntryUnavailable):
        raise
    except GitLabDenied:
        raise EntryDenied(f"repository denied: {repo}") from None
    except Exception as error:
        raise EntryUnavailable(_classification(error)) from None
