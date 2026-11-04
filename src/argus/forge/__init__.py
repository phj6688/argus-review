"""Share forge contracts without loading provider implementations."""

from .errors import ForgeAuthError, ForgeDenied, ForgeError, ForgeRateLimited
from .keys import parse_repo_key
from .types import (
    ChangeListing,
    ChangeMeta,
    ChangeRef,
    Comparison,
    DiffRefs,
    Note,
    PostedComment,
    RepoRecord,
    RepoRef,
    Thread,
)

__all__ = [
    "RepoRef",
    "ChangeRef",
    "DiffRefs",
    "ChangeMeta",
    "Comparison",
    "PostedComment",
    "Note",
    "Thread",
    "RepoRecord",
    "ChangeListing",
    "ForgeError",
    "ForgeAuthError",
    "ForgeDenied",
    "ForgeRateLimited",
    "forge_for",
]

_REGISTRY: dict[str, object] = {}


def forge_for(key: str):
    try:
        ref = parse_repo_key(key)
    except ValueError:
        if ":" in key and not key.startswith("gitlab:"):
            raise ForgeDenied("unknown forge") from None
        raise
    if ref.forge == "github":
        from .github_adapter import github_forge

        return github_forge
    if ref.forge in _REGISTRY:
        return _REGISTRY[ref.forge]
    if ref.forge == "gitlab":
        from argus.gitlab import config

        try:
            configured = config.base_url() is not None
        except config.GitLabConfigError:
            configured = False
        if configured:
            from argus.gitlab.reader import gitlab_forge

            return gitlab_forge
        raise ForgeDenied("gitlab forge is not enabled")
    raise ForgeDenied("unknown forge")
