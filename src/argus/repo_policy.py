"""Repository boundaries shared by entry points and discovery."""

import os
from dataclasses import dataclass

from .forge import keys
from .forge.keys import PolicyEntry, parse_entry
from .forge.types import RepoRef

TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
_OWNER = keys.GITHUB_OWNER
_NAME = keys.GITHUB_NAME


class RepoDenied(ValueError):
    pass


def hard_deny_owners() -> set[str]:
    return {
        entry.strip().casefold()
        for entry in os.getenv("ARGUS_DENY_OWNERS", "").split(",")
        if entry.strip()
    }


def is_denied(name: str, extra_deny=()) -> bool:
    if ":" in name:
        return True
    name = name.casefold()
    owner = name.split("/", 1)[0]
    denied = hard_deny_owners() | {entry.casefold() for entry in extra_deny}
    return owner in denied or name in denied


def validate_repo(name: str, extra_deny=None, *, allow_gitlab=False) -> str:
    if allow_gitlab and keys.is_gitlab_key(name):
        ref = keys.parse_repo_key(name)
        from .gitlab.config import base_url

        base = base_url()
        if base is None:
            raise ValueError("GitLab is not configured")
        if ref.host != keys.canonical_host(base.host, base.port):
            raise RepoDenied("repository denied")
        return name
    parts = name.split("/")
    if (
        len(parts) != 2
        or not _OWNER.fullmatch(parts[0])
        or not _NAME.fullmatch(parts[1])
        or parts[1] in {".", ".."}
    ):
        raise ValueError("repo must be owner/name with a valid owner and repository name")
    if is_denied(name, ()):
        raise RepoDenied(f"repository denied: {name}")
    if extra_deny is None:
        # CLI entry points share the operator's persisted policy.
        from .api import db
        from pathlib import Path

        config = db.get_config() if Path(db.db_path()).is_file() else None
        extra_deny = (config or {}).get("deny", [])
    if is_denied(name, extra_deny):
        raise RepoDenied(f"repository denied: {name}")
    return name


@dataclass(frozen=True)
class GitLabDecision:
    allowed: bool
    reason: str


def _policy_entry(item) -> PolicyEntry:
    if isinstance(item, str):
        return parse_entry(item)
    if not isinstance(item, PolicyEntry):
        raise ValueError("invalid policy entry")
    if item.kind == "owner":
        return item
    # Dataclasses can be constructed directly, bypassing the boundary parser.
    if item.kind == "id":
        text = keys.gitlab_key(item.host, item.project_id)
    elif item.kind == "path":
        text = f"gitlab:{item.host}/{'/'.join(item.segments)}"
    else:
        raise ValueError("invalid policy entry")
    if parse_entry(text) != item:
        raise ValueError("invalid policy entry")
    return item


def _matches(entry, ref, segments) -> bool:
    return entry.host == ref.host and (
        (entry.kind == "id" and entry.project_id == ref.project_id)
        or (entry.kind == "path" and entry.segments == segments[:len(entry.segments)])
    )


def _hard_gitlab_entries(hard):
    return [parse_entry(entry) for entry in hard if entry.startswith("gitlab:")]


def gitlab_decision(ref, record, deny_entries, allow_entries) -> GitLabDecision:
    try:
        if not isinstance(ref, RepoRef) or ref.forge != "gitlab" or type(ref.project_id) is not int or ref.project_id <= 0:
            return GitLabDecision(False, "not a gitlab ref")
        try:
            from .gitlab.config import base_url

            base = base_url()
        except Exception:
            return GitLabDecision(False, "gitlab not configured")
        if base is None:
            return GitLabDecision(False, "gitlab not configured")
        if ref.host != keys.canonical_host(base.host, base.port):
            return GitLabDecision(False, "host not configured")
        if record is None:
            return GitLabDecision(False, "project not resolved")
        if not isinstance(record.path, str) or not record.path or "" in record.path.split("/"):
            return GitLabDecision(False, "project path unknown")
        if record.archived is None or record.archived:
            return GitLabDecision(False, "project archived")
        segments = tuple(segment.casefold() for segment in record.path.split("/"))
        hard = hard_deny_owners()
        if segments[0] in hard or "/".join(segments) in hard:
            return GitLabDecision(False, "hard deny owner")
        try:
            hard_entries = _hard_gitlab_entries(hard)
        except ValueError:
            return GitLabDecision(False, "hard deny entry invalid")
        if isinstance(deny_entries, (str, bytes)) or isinstance(allow_entries, (str, bytes)):
            return GitLabDecision(False, "policy invalid")
        for item in (*hard_entries, *deny_entries):
            try:
                entry = _policy_entry(item)
            except ValueError:
                if isinstance(item, str) and item[:7].casefold() != "gitlab:":
                    continue
                return GitLabDecision(False, "deny entry invalid")
            if _matches(entry, ref, segments):
                return GitLabDecision(False, "deny entry")
        for item in allow_entries:
            try:
                entry = _policy_entry(item)
            except (ValueError, TypeError, AttributeError):
                continue
            if _matches(entry, ref, segments):
                return GitLabDecision(True, "allowed")
        return GitLabDecision(False, "not allowlisted")
    except Exception:
        # Unexpected records or broken iterables must never open the boundary.
        return GitLabDecision(False, "policy invalid")


class GitLabPolicyError(ValueError):
    pass


def gitlab_allow_entries() -> tuple[PolicyEntry, ...]:
    from .gitlab.config import allow

    hard = hard_deny_owners()
    try:
        _hard_gitlab_entries(hard)
    except ValueError:
        raise GitLabPolicyError("ARGUS_DENY_OWNERS: invalid gitlab entry") from None
    entries = []
    for text in allow():
        try:
            entry = parse_entry(text)
        except ValueError:
            raise GitLabPolicyError("ARGUS_GITLAB_ALLOW: invalid entry") from None
        if entry.kind == "owner":
            raise GitLabPolicyError("ARGUS_GITLAB_ALLOW: entries must be gitlab:HOST/group or gitlab:HOST#ID")
        if entry.kind == "path" and (entry.segments[0] in hard or "/".join(entry.segments) in hard):
            raise GitLabPolicyError("ARGUS_GITLAB_ALLOW: an entry overlaps a hard-deny owner")
        entries.append(entry)
    return tuple(entries)


@dataclass(frozen=True)
class GitLabPolicy:
    allow: tuple[PolicyEntry, ...]
    refusal: str | None

    @property
    def enabled(self) -> bool:
        return self.refusal is None


def gitlab_policy() -> GitLabPolicy:
    try:
        return GitLabPolicy(gitlab_allow_entries(), None)
    except GitLabPolicyError as error:
        return GitLabPolicy((), str(error))


def watch_owners() -> set[str]:
    return {
        s.strip().casefold()
        for s in os.getenv("ARGUS_WATCH_OWNERS", "").split(",")
        if s.strip()
    } - hard_deny_owners()


def valid_catalogue_name(name: str) -> bool:
    """Catalogue entries may be operator-denied, but must pass the hard boundary."""
    try:
        validate_repo(name, ())
    except ValueError:
        return False
    return True


def env_int(name: str, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum) if maximum is not None else value)
