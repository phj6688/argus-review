"""Intent adapter: recover a PR's Linear issue so the reviewer sees its purpose.

Argus today judges a diff against house rules alone; it never sees why the PR
opened. This recovers the issue key from the PR's title, body, and commit messages,
scoped to the repos that actually file Linear issues. Repos that do not file
issues yield an explicit 'intent unknown' rather than a guessed goal.

The Linear fetch itself is an injected seam: this module decides which issue to
fetch and turns a fetched issue into a prompt slot, but does not own the client.
The 84.9% recoverability that justifies title+body+commit parsing was measured
across the 12 adopting repos (see JARVIS_INCREMENT_1.md, M1).
"""

import os
import re
from dataclasses import dataclass, field

from ..repo_policy import TRUSTED_ASSOCIATIONS

STATUS_RECOVERED = "recovered"
STATUS_UNKNOWN = "intent_unknown"


@dataclass(frozen=True)
class PullRequest:
    repo: str
    title: str = ""
    body: str = ""
    commit_messages: list[str] = field(default_factory=list)
    author_association: str | None = None


@dataclass(frozen=True)
class Intent:
    repo: str
    status: str
    issue_ids: list[str]
    reason: str


def extract_issue_ids(*texts: object) -> list[str]:
    """Every configured issue key, upper-cased, in first-seen order.

    Accepts strings and lists of strings (a PR's commit messages are a list), so
    callers pass title, body, commits without flattening first.
    """
    prefix = os.getenv("ARGUS_LINEAR_KEY_PREFIX", "").strip()
    if not prefix:
        return []
    pattern = re.compile(rf"(?<![\w-]){re.escape(prefix)}-\d+\b", re.IGNORECASE)
    found: list[str] = []
    for chunk in _flatten(texts):
        for match in pattern.findall(chunk):
            identifier = match.upper()
            if identifier not in found:
                found.append(identifier)
    return found


def recover(pull_request: PullRequest) -> Intent:
    repo = pull_request.repo.casefold()
    repos = {name.strip().casefold() for name in os.getenv("ARGUS_LINEAR_REPOS", "").split(",")
             if name.strip().count("/") == 1 and all(name.strip().split("/"))}
    if repo not in repos:
        return Intent(repo, STATUS_UNKNOWN, [],
                      f"{repo} is not in scope for Linear intent")
    if pull_request.author_association not in TRUSTED_ASSOCIATIONS:
        return Intent(repo, STATUS_UNKNOWN, [], "untrusted PR author association")
    ids = extract_issue_ids(
        pull_request.title, pull_request.body, pull_request.commit_messages)
    if not ids:
        return Intent(repo, STATUS_UNKNOWN, [],
                      "no Linear issue key in title, body, or commit messages")
    return Intent(repo, STATUS_RECOVERED, ids, f"recovered {', '.join(ids)}")


def intent_slot(recovered: Intent, issue: dict | None) -> str:
    """The labelled prompt section to inject ahead of the diff.

    Three states, never conflated: no id was recoverable; an id was recovered but
    its issue could not be fetched; the issue is in hand. Each instructs the
    reviewer not to fabricate the parts it does not have.
    """
    if recovered.status == STATUS_UNKNOWN:
        return ("# PR intent\n"
                f"Intent unknown ({recovered.reason}). Judge the diff on the house "
                "rules and the code alone; do not invent a goal for this PR.")
    if issue is None:
        ids = ", ".join(recovered.issue_ids)
        return (f"# PR intent (Linear {ids} referenced, issue text unavailable)\n"
                "The issue body could not be fetched. Judge the diff on the house "
                "rules and the code alone; do not invent the issue's contents.")
    identifier = issue.get("identifier") or issue.get("id") or recovered.issue_ids[0]
    return (f"# PR intent (recovered from Linear {identifier})\n"
            f"Title: {issue.get('title', '')}\n\n"
            f"{issue.get('description', '')}\n\n"
            "Flag changes in this diff that contradict or fail this intent.")


def _flatten(texts: tuple) -> list[str]:
    flat: list[str] = []
    for item in texts:
        if isinstance(item, str):
            flat.append(item)
        elif isinstance(item, (list, tuple)):
            flat.extend(str(part) for part in item)
    return flat
