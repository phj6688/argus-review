"""Share forge contracts without loading provider implementations."""

from .errors import ForgeAuthError, ForgeDenied, ForgeError, ForgeRateLimited
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


def forge_for(key: str):
    raise NotImplementedError("forge_for is wired in a later change")
