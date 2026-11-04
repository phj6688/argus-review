"""Post safe bodies and reconcile writes whose responses cannot confirm creation."""

import time
from dataclasses import dataclass, field

from argus.forge.errors import ForgeError
from argus.gitlab.body import assert_body_safe, parse_marker
from argus.gitlab.client import GitLabHTTPError, GitLabRateLimited
from argus.gitlab.pacing import pacing_for
from argus.redact import redact


@dataclass(frozen=True)
class WriteResult:
    outcome: str
    note_id: int | None = None
    discussion_id: str | None = None
    reason: str | None = None
    status_code: int | None = None
    first_note: dict | None = field(default=None, compare=False, repr=False)

    def __post_init__(self):
        object.__setattr__(self, "reason", redact(self.reason))


class GitLabWriter:
    def __init__(self, client, bot_user_id: int, *, pacer=None):
        self.client, self.bot_user_id = client, bot_user_id
        self.pacer = pacing_for(client) if pacer is None else pacer

    def post_discussion(self, project_id, iid, body, position) -> WriteResult:
        return self._write("mr_discussions", project_id, iid, body, {"body": body, "position": position})

    def post_note(self, project_id, iid, body) -> WriteResult:
        return self._write("mr_notes", project_id, iid, body, {"body": body})

    def reply(self, project_id, iid, discussion_id, body) -> WriteResult:
        return self._write("mr_discussion_notes", project_id, iid, body, {"body": body},
                           discussion_id=discussion_id)

    def _write(self, route, project_id, iid, body, payload, **parts) -> WriteResult:
        assert_body_safe(body)
        with self.pacer.write_lock(self.bot_user_id, project_id, iid):
            for attempt in range(2):
                self.pacer.acquire(self.bot_user_id)
                try:
                    response = self.client.request("POST", route, json=payload,
                                                   project_id=project_id, iid=iid, **parts)
                except GitLabRateLimited as exc:
                    self.pacer.on_rate_limited(self.bot_user_id, max(0.0, exc.reset_at - time.time()))
                    if attempt:
                        return WriteResult("failed", reason="rate_limited")
                    continue
                except GitLabHTTPError as exc:
                    if exc.reason == "transport" or 500 <= (exc.status_code or 0) <= 599:
                        return self._reconcile(project_id, iid, body, str(exc),
                                               target_discussion_id=parts.get("discussion_id"),
                                               transport_failure=exc.reason == "transport",
                                               status_code=exc.status_code)
                    return WriteResult("failed" if 400 <= (exc.status_code or 0) < 500 else "unknown",
                                       reason=str(exc), status_code=exc.status_code)
                try:
                    data = response.json()
                    discussion_id = parts.get("discussion_id")
                    if route == "mr_discussions":
                        discussion_id, note_id = data["id"], data["notes"][0]["id"]
                        if not isinstance(discussion_id, str) or not discussion_id:
                            raise ValueError
                    else:
                        note_id = data["id"]
                    if not 200 <= response.status_code < 300 or type(note_id) is not int or note_id <= 0:
                        raise ValueError
                    return WriteResult("posted", note_id, discussion_id,
                                       first_note=data["notes"][0] if route == "mr_discussions" else data)
                except (ValueError, KeyError, TypeError, IndexError):
                    return WriteResult("unknown", reason="unparseable_response")

    def _reconcile(self, project_id, iid, body, reason, *, target_discussion_id, transport_failure,
                   status_code=None) -> WriteResult:
        wanted = parse_marker(body)
        if wanted is None:
            return WriteResult("unknown", reason=reason, status_code=status_code)
        try:
            for page in self.client.pages("mr_discussions", project_id=project_id, iid=iid,
                                          query={"per_page": 100}):
                discussions = page.json()
                if not isinstance(discussions, list):
                    raise ValueError
                for discussion in discussions:
                    if target_discussion_id is not None and discussion["id"] != target_discussion_id:
                        continue
                    for note in discussion["notes"]:
                        if note["author"]["id"] == self.bot_user_id and parse_marker(note["body"]) == wanted:
                            if (type(note["id"]) is not int or note["id"] <= 0
                                    or not isinstance(discussion["id"], str) or not discussion["id"]):
                                raise ValueError
                            return WriteResult("already_posted", note["id"], discussion["id"], first_note=note)
        except (ForgeError, ValueError, KeyError, TypeError, AttributeError):
            return WriteResult("unknown", reason=reason, status_code=status_code)
        # Transport and gateway failures can still commit after the completed read.
        uncertain = transport_failure or status_code in {502, 503, 504}
        return WriteResult("unknown" if uncertain else "failed", reason=reason, status_code=status_code)
