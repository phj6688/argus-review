"""Keep shared review data independent of provider payloads."""

from dataclasses import dataclass


@dataclass(frozen=True)
class RepoRef:
    """Preserve identity before a repository path is resolved."""

    forge: str
    host: str
    path: str | None
    project_id: int | None


@dataclass(frozen=True)
class ChangeRef:
    """Keep change numbers scoped to their repository."""

    repo: RepoRef
    number: int


@dataclass(frozen=True)
class DiffRefs:
    """Retain the revisions needed for stable diff anchors."""

    base_sha: str
    start_sha: str
    head_sha: str


@dataclass(frozen=True)
class ChangeMeta:
    """Share review decisions without exposing provider payloads."""

    head_sha: str | None
    base_sha: str | None
    state: str
    merged: bool
    draft: bool
    author_id: str | None
    author_login: str | None
    author_is_bot: bool
    title: str
    updated_at: str | None
    source_repo: RepoRef | None


@dataclass(frozen=True)
class Comparison:
    """Distinguish partial comparisons from complete file sets."""

    status: str
    files: tuple
    complete: bool


@dataclass(frozen=True)
class PostedComment:
    """Preserve where a comment actually landed."""

    id: int | str
    thread_id: str | None
    anchored: bool
    placement: str


@dataclass(frozen=True)
class Note:
    """Retain authorship and reply context for trust decisions."""

    id: int | str
    thread_id: str | None
    in_reply_to: int | str | None
    author_login: str | None
    author_is_bot: bool
    trusted: bool | None
    body: str
    created_at: str | None
    updated_at: str | None


@dataclass(frozen=True)
class Thread:
    """Keep discussion state with the revision it refers to."""

    id: str
    resolved: bool
    resolvable: bool
    root_comment_id: int | str
    head_sha: str | None
    notes: tuple[Note, ...]


@dataclass(frozen=True)
class RepoRecord:
    """Support discovery without loading full repository metadata."""

    path: str
    archived: bool
    default_branch: str | None


@dataclass(frozen=True)
class ChangeListing:
    """Keep explicit closures distinct from incomplete listings."""

    changes: tuple
    closed: tuple
    cursor: str | None
    complete: bool
