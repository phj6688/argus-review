"""Repository boundaries shared by entry points and discovery."""

import os
import re

TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
_OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}\Z")
_NAME = re.compile(r"[A-Za-z0-9._-]{1,100}\Z")


class RepoDenied(ValueError):
    pass


def hard_deny_owners() -> set[str]:
    return {
        entry.strip().casefold()
        for entry in os.getenv("ARGUS_DENY_OWNERS", "").split(",")
        if entry.strip()
    }


def is_denied(name: str, extra_deny=()) -> bool:
    name = name.casefold()
    owner = name.split("/", 1)[0]
    denied = hard_deny_owners() | {entry.casefold() for entry in extra_deny}
    return owner in denied or name in denied


def validate_repo(name: str, extra_deny=None) -> str:
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
