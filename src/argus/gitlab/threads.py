"""Read discussions with stable authorship and revision context."""

import re
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass

from argus.api import db
from argus.forge.keys import gitlab_key
from argus.forge.types import ChangeRef, PostedComment
from argus.gitlab import client as client_module, publisher
from argus.gitlab.body import assert_body_safe, neutralize_mentions, neutralize_quick_actions, parse_reply_marker
from argus.gitlab.client import GitLabClient, GitLabDenied, GitLabHTTPError, GitLabRateLimited
from argus.gitlab.pacing import pacing_for
from argus.gitlab.reader import GitLabForge
from argus.gitlab.writer import GitLabWriter

COUNTERS: Counter[str] = Counter()


class ThreadMissing(Exception):
    """The discussion no longer exists."""


def reply(change: ChangeRef, thread: "GitLabThread", body: str, *, client=None) -> PostedComment:
    if parse_reply_marker(body) is None:
        raise ValueError("Missing reply marker")
    prose, _, marker = body.rstrip().rpartition("\n")
    body = neutralize_mentions(neutralize_quick_actions(prose)) + "\n" + marker
    assert_body_safe(body)
    client = client if client is not None else client_module.client()
    bot_id = publisher.bot_identity(client)[0]
    try:
        result = GitLabWriter(client, bot_id).reply(change.repo.project_id, change.number, thread.id, body)
    except GitLabDenied as error:
        if error.reason != "not_found":
            raise
        COUNTERS["reply_discussion_missing"] += 1
        raise ThreadMissing from error
    if result.outcome in {"posted", "already_posted"}:
        COUNTERS["reply_" + result.outcome] += 1
        return PostedComment(result.note_id, thread.id, False, "reply")
    if result.reason == "rate_limited":
        raise GitLabRateLimited(client.pause_until())
    raise GitLabHTTPError("GitLab reply was not confirmed", status_code=result.status_code,
                          reason="unknown_outcome" if result.outcome == "unknown" else result.reason)


def resolve(change: ChangeRef, thread: "GitLabThread", *, client=None) -> str:
    client = client if client is not None else client_module.client()
    bot_id = publisher.bot_identity(client)[0]
    pacer = pacing_for(client)
    with pacer.write_lock(bot_id, change.repo.project_id, change.number):
        try:
            response = client.request("PUT", "mr_discussion", json={"resolved": True},
                                      project_id=change.repo.project_id, iid=change.number, discussion_id=thread.id)
        except GitLabRateLimited as error:
            pacer.on_rate_limited(bot_id, max(0.0, error.reset_at - time.time()))
            raise
        except GitLabDenied as error:
            if error.reason == "forbidden":
                COUNTERS["resolve_forbidden"] += 1
                return "forbidden"
            if error.reason == "not_found":
                COUNTERS["resolve_discussion_missing"] += 1
                raise ThreadMissing from error
            raise
        try:
            data = response.json()
            notes = [n for n in data["notes"] if n.get("resolvable") is True]
            if data["id"] != thread.id or not notes or not all(n.get("resolved") is True for n in notes):
                raise ValueError
        except (ValueError, KeyError, TypeError, AttributeError):
            raise GitLabHTTPError("GitLab resolution was not confirmed", reason="http") from None
    COUNTERS["resolved"] += 1
    return "resolved"


@dataclass(frozen=True)
class ThreadNote:
    """Preserve numeric authorship even when a user changes their login."""

    id: int
    author_id: int
    username: str
    body: str
    created_at: str
    system: bool


@dataclass(frozen=True)
class GitLabThread:
    """Keep discussion state and API note order without system activity."""

    id: str
    notes: tuple[ThreadNote, ...]
    resolvable: bool
    resolved: bool
    resolved_by: int | None
    head_sha: str | None


def _note(data) -> ThreadNote:
    if not isinstance(data, dict):
        raise GitLabHTTPError("Invalid GitLab threads response", reason="http")
    author = data.get("author")
    if (type(data.get("id")) is not int or not isinstance(author, dict)
            or type(author.get("id")) is not int
            or not isinstance(author.get("username"), str)
            or any(not isinstance(data.get(key), str) for key in ("body", "created_at"))
            or type(data.get("system")) is not bool):
        raise GitLabHTTPError("Invalid GitLab threads response", reason="http")
    return ThreadNote(data["id"], author["id"], author["username"], data["body"],
                      data["created_at"], data["system"])


def _thread(data) -> GitLabThread | None:
    if (not isinstance(data, dict) or not isinstance(data.get("id"), str)
            or not re.fullmatch(r"[0-9a-fA-F]{40}", data["id"])
            or not isinstance(data.get("notes"), list)):
        raise GitLabHTTPError("Invalid GitLab threads response", reason="http")
    parsed = [(raw, _note(raw)) for raw in data["notes"]]
    kept = [raw for raw, note in parsed if not note.system]
    if not kept:
        return None
    resolvable = [raw for raw in kept if raw.get("resolvable") is True]
    resolved = bool(resolvable) and all(raw.get("resolved") is True for raw in resolvable)
    resolved_by = None
    if resolved:
        for raw in resolvable:
            resolver = raw.get("resolved_by")
            if isinstance(resolver, dict) and type(resolver.get("id")) is int:
                resolved_by = resolver["id"]
    position = kept[0].get("position")
    head_sha = position.get("head_sha") if isinstance(position, dict) else None
    return GitLabThread(
        id=data["id"], notes=tuple(note for _, note in parsed if not note.system),
        resolvable=bool(resolvable), resolved=resolved, resolved_by=resolved_by,
        head_sha=head_sha if isinstance(head_sha, str) else None,
    )


def list_threads(change: ChangeRef, *, client: GitLabClient | None = None) -> list[GitLabThread]:
    """Read every guarded discussion page, rejecting malformed note identities."""
    key = gitlab_key(change.repo.host, change.repo.project_id)
    client, repo = GitLabForge(client)._connection(key)
    result = []
    for response in client.pages("mr_discussions", project_id=repo.project_id,
                                 iid=change.number, query={"per_page": 100}):
        try:
            page = response.json()
        except ValueError:
            raise GitLabHTTPError("Invalid GitLab threads response", reason="http") from None
        if not isinstance(page, list):
            raise GitLabHTTPError("Invalid GitLab threads response", reason="http")
        for item in page:
            thread = _thread(item)
            if thread is not None:
                result.append(thread)
    return result


def is_outdated(thread: GitLabThread, current_head_sha: str) -> bool:
    """Compare position heads, as described in docs/gitlab.md.

    GitLab has no outdated, original_position or line_code field. The caller
    must read the MR first for its current diff_refs.head_sha or sha.
    """
    return isinstance(thread.head_sha, str) and thread.head_sha != current_head_sha


def is_bot_note(note: ThreadNote, repo: str, *, conn: sqlite3.Connection | None = None) -> bool:
    """Prefer recorded numeric identity, counting each legacy login fallback."""
    owned = conn is None
    conn = db.connect() if owned else conn
    try:
        bot_id = db.bot_user_id(conn, repo)
        if bot_id is not None:
            # A rename must not trigger replies to our own notes, and a new owner
            # of the old username must not be mistaken for the bot.
            return note.author_id == bot_id
        COUNTERS["bot_id_missing"] += 1
        login = db.bot_login(conn, repo)
        return login is not None and note.username.casefold() == login.casefold()
    finally:
        if owned:
            conn.close()
