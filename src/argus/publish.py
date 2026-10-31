"""Opt-in GitHub review publishing and transport-free payload rendering."""

import logging
import os
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from . import repo_policy
from .diff import LANGUAGE_BY_EXTENSION, FilePatch, split_lines
from .github import API_VERSION, GITHUB_API, guard_url, observe_response
from .markdown import code_span, escape_block, escape_inline
from .repo_policy import validate_repo

log = logging.getLogger("argus.publish")
_SEVERITIES = ("critical", "major", "minor", "nit")
_CATEGORIES = {
    "contracts": "Contracts", "error_paths": "Error paths", "input_handling": "Input handling",
    "boundaries": "Boundaries", "resources": "Resources", "concurrency": "Concurrency",
    "security": "Security", "tests": "Tests", "data_integrity": "Data integrity",
}
_INLINE_FINGERPRINT = re.compile(r"\n<!-- argus:(fp|ufp)=([^\s>]+) -->\n?\Z")
_LEGACY_SUMMARY_FINGERPRINT = re.compile(r"^<!-- argus:(fp|ufp)=([^\s>]+) -->$", re.M)
# The publisher's own sentence closes the prompt block right before each legacy marker.
_LEGACY_MARKER_LEAD = "Verify the issue against the current code before changing anything.\n</details>\n\n"
_FENCE_LINE = re.compile(r"^(`{3,}|~{3,})")
_MAX_UNVERIFIED = 10
_MAX_BODY = 60_000
_REVIEW_MARKER = re.compile(r"(?:\A|\n)<!-- argus:review=(\d+) head=([0-9a-fA-F]{40}) -->\Z")
_MANIFEST = re.compile(r"\n<!-- argus:manifest fp=([^\s>]*) ufp=([^\s>]*) -->\n?\Z")
_bot_logins: dict[str, str] = {}
_identity_lock = threading.Lock()


def inline_fingerprint(body: str) -> tuple[str, str] | None:
    match = _INLINE_FINGERPRINT.search(body)
    return (match[1], match[2]) if match else None


def review_marker(body: str) -> tuple[int, str] | None:
    manifest = _MANIFEST.search(body)
    match = _REVIEW_MARKER.search(body[:manifest.start()] if manifest else body.removesuffix("\n"))
    return (int(match[1]), match[2]) if match else None


def summary_fingerprints(body: str) -> tuple[set[str], set[str]]:
    if review_marker(body) is None:
        return set(), set()
    if manifest := _MANIFEST.search(body):
        return set(filter(None, manifest[1].split(","))), set(filter(None, manifest[2].split(",")))
    # Older reviews have no manifest, but still require the review footer.
    legacy = [(m[1], m[2]) for m in _LEGACY_SUMMARY_FINGERPRINT.finditer(body)
              if body[:m.start()].endswith(_LEGACY_MARKER_LEAD) and not _inside_fence(body[:m.start()])]
    return ({fp for kind, fp in legacy if kind == "fp"},
            {fp for kind, fp in legacy if kind == "ufp"})


def _inside_fence(text: str) -> bool:
    """True when text ends inside an unclosed fenced block (evidence is model-authored)."""
    opener = None
    for line in text.split("\n"):
        match = _FENCE_LINE.match(line)
        if not match:
            continue
        run = match[1]
        if opener is None:
            opener = run
        elif run[0] == opener[0] and len(run) >= len(opener) and not line[len(run):].strip():
            opener = None
    return opener is not None


@dataclass
class PublishOutcome:
    status: str
    comments_posted: int = 0
    already_posted: int = 0
    reason: str | None = None
    payload: dict | None = None
    items: list[dict] = field(default_factory=list)
    gh_review_id: int | None = None
    bot_login: str | None = None
    posted_at: str | None = None


def enabled_for(repo: str) -> bool:
    """Read the opt-in at call time so an unset flag adds no GitHub reads."""
    return os.environ.get("ARGUS_PUBLISH") in {"1", "dry-run"} and repo.casefold() in repo_allowlist()


def repo_allowlist() -> set[str]:
    repos, deny = _dashboard_state()
    if repos is not None:
        return {name.casefold() for name in repos}
    # Env entries get the same screening as stored ones, so a denied or malformed name is
    # never shown, never sent to GitHub, and cannot make the first dashboard save fail.
    return {name for name in env_repo_allowlist() if _well_formed_and_allowed(name, deny)}


def _well_formed_and_allowed(name: str, deny) -> bool:
    try:
        repo_policy.validate_repo(name, deny)
    except ValueError:  # RepoDenied is a ValueError
        return False
    return True


def _dashboard_state() -> tuple[list[str] | None, list[str]]:
    try:
        from .api import db

        repos, deny = db.get_publish_state()
    except Exception:
        # Fail closed: an unreadable dashboard list must not fall back to the env list, which
        # could re-enable posting that the operator removed from the dashboard.
        log.warning("publish: dashboard repo list is unreadable, posting is blocked until it is")
        return [], []
    if repos is None:
        return None, deny
    return [name for name in repos if _well_formed_and_allowed(name, deny)], deny


def _dashboard_repos() -> list[str] | None:
    return _dashboard_state()[0]


def repo_allowlist_source() -> str:
    return "dashboard" if _dashboard_repos() is not None else "env"


def env_repo_allowlist() -> set[str]:
    return {name.strip().casefold() for name in os.environ.get("ARGUS_PUBLISH_REPOS", "").split(",")
            if name.strip()}


def mode() -> str:
    """Publishing mode as the operator set it: off, dry-run or on."""
    return {"1": "on", "dry-run": "dry-run"}.get(os.environ.get("ARGUS_PUBLISH", ""), "off")


def render_options() -> dict:
    minimum = os.environ.get("ARGUS_PUBLISH_MIN_SEVERITY", "minor")
    maximum = int(os.environ.get("ARGUS_PUBLISH_MAX_COMMENTS", "4"))
    if minimum not in _SEVERITIES or maximum < 0:
        raise ValueError("Invalid publishing severity or comment limit")
    return {"min_severity": minimum, "max_comments": maximum}


def commentable_lines(patch: str | None) -> set[int]:
    """Post-image context and additions, excluding deletions and patch metadata."""
    numbers: set[int] = set()
    number = None
    remaining = 0
    for line in split_lines(patch or ""):
        header = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
        if header:
            number = int(header[1])
            remaining = int(header[2]) if header[2] is not None else 1
        elif number is not None and remaining > 0 and (not line or line[0] in {" ", "+"}):
            numbers.add(number)
            number += 1
            remaining -= 1
    return numbers


def render_comment(finding: dict) -> str:
    marker = "ufp" if finding.get("unverified") else "fp"
    category = _CATEGORIES.get(finding.get("category"), "Correctness")
    severity = finding["severity"].capitalize()
    tier = finding["evidence_tier"]
    title = escape_inline(finding["title"])
    fix = finding.get("proposed_fix") or ""
    end = finding.get("end_line")
    span = f"{finding['line']}-{end}" if end and end != finding["line"] else str(finding["line"])
    parts = [f"_{category}_ | _{severity}_ | _{tier}_", f"**{title}**", escape_block(finding["description"])]
    evidence = finding.get("evidence")
    if evidence:
        language = LANGUAGE_BY_EXTENSION.get(Path(finding["file"]).suffix.lower(), "")
        # A snippet containing a fence must stay inside the evidence block.
        fence = "`" * max(3, max((len(m[0]) + 1 for m in re.finditer(r"`+", evidence)), default=3))
        evidence = evidence.replace("<!--", "<!\u200b--").replace("argus:", "argus\u200b:")
        parts.append(f"{fence}{language}\n{evidence}\n{fence}")
    parts.extend([
        f"**Suggested fix:** {escape_block(fix)}",
        "<details>\n<summary>Prompt for agents</summary>\n\n"
        f"In {code_span(finding['file'])} around line {span}: {title}. {escape_inline(fix)} "
        "Verify the issue against the current code before changing anything.\n</details>",
        f"<!-- argus:{marker}={finding.get('fingerprint') or ''} -->",
    ])
    return "\n\n".join(parts)


def _below_threshold(finding: dict, minimum: str) -> bool:
    # Nits stay summary-only even if an operator explicitly selects nit.
    return (finding["severity"] == "nit"
            or _SEVERITIES.index(finding["severity"]) > _SEVERITIES.index(minimum))


def _already_posted(document: dict, posted: set[str], posted_unverified: set[str]) -> int:
    return (sum(bool(f.get("fingerprint") and f["fingerprint"] in posted)
                for f in document["findings"])
            + sum(bool(f.get("fingerprint") and f["fingerprint"] in posted | posted_unverified)
                  for f in document.get("unverified", [])))


def _new_findings(document: dict, minimum: str, posted: set[str],
                  posted_unverified: set[str] | None = None) -> list[dict]:
    candidates = [f for f in document["findings"]
                  if not (f.get("fingerprint") and f["fingerprint"] in posted)]
    seen = posted | (posted_unverified or set())
    candidates.extend({**f, "unverified": True} for f in document.get("unverified", [])
                      if not (f.get("fingerprint") and f["fingerprint"] in seen))
    return sorted(
        [f for f in candidates if not _below_threshold(f, minimum)],
        key=lambda f: (_SEVERITIES.index(f["severity"]), -(f.get("confidence") or 0)),
    )


def _summary_findings(title: str, findings: list[dict], description: str = "") -> str:
    heading = [f"### {title}"] + ([description] if description else [])
    return "\n\n".join(heading + [
        f"{code_span(str(f['file']) + ':' + str(f['line']))}\n\n{render_comment(f)}" for f in findings])


def render_review(
    head_sha: str, review_id: int, document: dict, files: list[FilePatch], *,
    min_severity: str = "minor", max_comments: int = 4,
    posted_fingerprints: set[str] | None = None, outside_only: bool = False,
    posted_unverified_fingerprints: set[str] | None = None,
) -> dict:
    """Build the exact GitHub payload without environment reads or HTTP."""
    if min_severity not in _SEVERITIES or max_comments < 0:
        raise ValueError("Invalid publishing severity or comment limit")
    posted = posted_fingerprints or set()
    posted_unverified = posted_unverified_fingerprints or set()
    findings = document["findings"]
    already = _already_posted(document, posted, posted_unverified)
    below = sum(_below_threshold(f, min_severity)
                for f in findings + document.get("unverified", []))
    lines = {f.filename: commentable_lines(f.patch) for f in files if f.status != "removed"}
    outside, overflow, comments, unverified = [], [], [], []
    for finding in _new_findings(document, min_severity, posted, posted_unverified):
        if finding.get("unverified"):
            unverified.append(finding)
            continue
        start = finding["line"]
        end = finding.get("end_line") if finding.get("end_line") is not None else start
        allowed = lines.get(finding["file"], set())
        # Every line must be in a hunk, so a span cannot bridge an unseen gap.
        commentable = (start > 0 and end >= start and end - start < len(allowed)
                       and all(line in allowed for line in range(start, end + 1)))
        if outside_only or not commentable:
            outside.append(finding)
        elif len(comments) >= max_comments:
            overflow.append(finding)
        else:
            comment = {"path": finding["file"], "line": end, "side": "RIGHT",
                       "body": render_comment(finding)}
            if end > start:
                comment.update(start_line=start, start_side="RIGHT")
            comments.append(comment)
    coverage = document.get("coverage") or {}
    lanes = [lane for lane in coverage.get("lanes", []) if lane.get("lane") == "llm"]
    reviewed = sum(lane.get("files_reviewed") or 0 for lane in lanes)
    omitted = sum(lane.get("files_omitted") or 0 for lane in lanes)
    severities = Counter(f["severity"] for f in findings)
    tiers = Counter(f["evidence_tier"] for f in findings)
    body = [
        f"**Argus review** for {head_sha[:7]}",
        f"Verdict: {document['verdict']}\n\nCoverage: {coverage.get('level', 'unknown')}\n\n"
        f"Files reviewed: {reviewed}; omitted: {omitted}",
        "Severity counts: " + "; ".join(f"{s.capitalize()}: {severities[s]}" for s in _SEVERITIES),
        "Tier counts: " + "; ".join(f"{tier}: {tiers[tier]}" for tier in
                                    ("deterministic", "consensus", "single_critic", "disputed")),
    ]
    if outside:
        body.append(_summary_findings("Outside the diff", outside))
    if overflow:
        body.append(_summary_findings("Comment limit", overflow))
    confirmed = ",".join(f["fingerprint"] for f in outside + overflow if f.get("fingerprint"))

    def footer(count):
        disputed = ",".join(f["fingerprint"] for f in unverified[:count] if f.get("fingerprint"))
        return [f"Below posting threshold: {below}", f"Already posted: {already}",
                f"<!-- argus:review={review_id} head={head_sha} -->\n"
                f"<!-- argus:manifest fp={confirmed} ufp={disputed} -->"]

    listed = 0
    if unverified:
        # Include each candidate's manifest before spending space on disputed items.
        section = f"{len(unverified)} more unverified findings are not listed."
        for count in range(1, min(len(unverified), _MAX_UNVERIFIED) + 1):
            candidate = _summary_findings(
                "Unverified (the verifier disagreed)", unverified[:count],
                "These findings were refuted by a second check. They are listed for a human "
                "decision and do not affect the verdict.",
            )
            remaining = len(unverified) - count
            if remaining:
                candidate += f"\n\n{remaining} more unverified findings are not listed."
            if len("\n\n".join(body + [candidate] + footer(count))) > _MAX_BODY:
                break
            section = candidate
            listed = count
        body.append(section)
    body.extend(footer(listed))
    return {"commit_id": head_sha, "event": "COMMENT", "body": "\n\n".join(body), "comments": comments}


def _list_all(http, url: str, headers: dict) -> list[dict]:
    records = []
    page = 1
    while True:
        guard_url(url)
        response = http.get(url, params={"per_page": 100, "page": page}, headers=headers, timeout=15.0)
        observe_response(response, record_quota=False)
        response.raise_for_status()
        batch = response.json()
        records.extend(batch)
        if len(batch) < 100:
            return records
        page += 1


def _redacted_error(error):
    reason = f"{type(error).__name__}: {error}"
    for key in ("ARGUS_PUBLISH_TOKEN", "GITHUB_TOKEN"):
        token = os.environ.get(key, "").strip()
        if token:
            reason = reason.replace(token, "[redacted]")
    return reason


def error_outcome(error: Exception, *, payload: dict | None = None,
                  already_posted: int = 0) -> PublishOutcome:
    # Transport errors may include credentials in their text. Do not log a traceback.
    reason = _redacted_error(error)
    log.warning("GitHub publishing failed: %s", reason)
    return PublishOutcome("error", already_posted=already_posted, reason=reason, payload=payload)


def _placed_items(document, payload, comments=()):
    inline = {marker[1]: comment for comment in payload["comments"]
              if (marker := inline_fingerprint(comment.get("body") or "")) and marker[0] == "fp"}
    confirmed, unverified = summary_fingerprints(payload["body"])
    summary = confirmed | unverified
    ids = {marker[1]: comment.get("id") for comment in comments
           if (marker := inline_fingerprint(comment.get("body") or "")) and marker[0] == "fp"}
    items = []
    findings = document["findings"] + [{**f, "unverified": True} for f in document.get("unverified", [])]
    for finding in findings:
        fp = finding.get("fingerprint")
        if fp not in inline and fp not in summary:
            continue
        item = {**finding, "placement": "inline" if fp in inline else "summary",
                "gh_comment_id": ids.get(fp) if fp in inline else None}
        if fp in inline:
            comment = inline[fp]
            if comment.get("original_line") is not None:
                end_line = comment["original_line"]
                line = comment.get("original_start_line") or end_line
            elif comment.get("line") is not None:
                end_line = comment["line"]
                line = comment.get("start_line") or end_line
            else:
                line = finding["line"]
                end_line = finding.get("end_line") or line
            item.update(file=comment["path"], line=line, end_line=end_line)
        items.append(item)
    return items


def _posted_metadata(outcome, document, review, transport, url, headers):
    outcome.gh_review_id = review.get("id")
    outcome.posted_at = review.get("submitted_at")
    outcome.items = _placed_items(document, outcome.payload)
    if outcome.gh_review_id:
        try:
            comments = _list_all(transport, f"{url}/reviews/{outcome.gh_review_id}/comments", headers)
            comments = [c for c in comments if authored_by(c, outcome.bot_login)]
            outcome.items = _placed_items(document, outcome.payload, comments)
        except Exception as error:  # noqa: BLE001 - the review already exists remotely
            outcome.reason = _redacted_error(error)
            log.warning("review was posted; collector recovers comment ids later: %s", type(error).__name__)
    return outcome


def authored_by(entry, login):
    author = (entry.get("user") or {}).get("login")
    return isinstance(author, str) and author.casefold() == login.casefold()


def _bot_login(transport=None):
    override = os.environ.get("ARGUS_PUBLISH_BOT_LOGIN", "").strip()
    if override:
        return override
    token = os.environ.get("ARGUS_PUBLISH_TOKEN", "").strip()
    if not token:
        raise ValueError("ARGUS_PUBLISH_TOKEN is not set")
    transport = transport if transport is not None else httpx
    with _identity_lock:
        if token not in _bot_logins:
            url = str(guard_url(f"{GITHUB_API}/user"))
            response = transport.get(url, headers={
                "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
            }, timeout=15.0)
            observe_response(response, record_quota=False)
            response.raise_for_status()
            login = response.json().get("login")
            if not isinstance(login, str) or not login.strip():
                raise ValueError("Publishing identity is missing")
            _bot_logins[token] = login.strip()
        return _bot_logins[token]


def publish_review(
    repo: str, pr: int, head_sha: str, review_id: int, document: dict,
    files: list[FilePatch], *, http=None,
) -> PublishOutcome:
    if os.environ.get("ARGUS_PUBLISH") not in {"1", "dry-run"}:
        return PublishOutcome("skipped", reason="publishing disabled")
    if not enabled_for(repo):
        return PublishOutcome("skipped", reason="repo not allowlisted")
    payload = None
    already = 0
    reason = None
    try:
        url = str(guard_url(f"{GITHUB_API}/repos/{repo}/pulls/{pr}"))
        validate_repo(repo)
        options = render_options()
        payload = render_review(head_sha, review_id, document, files, **options)
        if os.environ.get("ARGUS_PUBLISH") == "dry-run":
            return PublishOutcome("dry_run", payload=payload, items=_placed_items(document, payload))
        token = os.environ.get("ARGUS_PUBLISH_TOKEN", "").strip()
        if not token:
            raise ValueError("ARGUS_PUBLISH_TOKEN is not set")
        if pr <= 0 or not re.fullmatch(r"[0-9a-fA-F]{40}", head_sha):
            raise ValueError("Publishing requires a PR and a 40-hex head SHA")
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": API_VERSION}
        transport = http if http is not None else httpx
        guard_url(url)
        response = transport.get(url, headers=headers, timeout=15.0)
        observe_response(response, record_quota=False)
        response.raise_for_status()
        live_head = response.json()["head"]["sha"]
        if live_head != head_sha:
            return PublishOutcome("skipped", reason=(
                f"PR head moved since the review ({head_sha[:7]} -> {live_head[:7]})"), payload=payload)
        try:
            bot_login = _bot_login(transport)
        except Exception as error:  # noqa: BLE001 - identity is required before any write
            # An unknown identity cannot safely dedupe. Skip instead of double-posting;
            # cache only successful lookups so a temporary failure can recover next run.
            reason = f"Publishing identity could not be resolved ({type(error).__name__})"
            log.warning("Skipping GitHub publishing: %s", reason)
            return PublishOutcome("skipped", reason=reason, payload=payload)

        existing = [c for c in _list_all(transport, f"{url}/comments", headers) if authored_by(c, bot_login)]
        reviews = [r for r in _list_all(transport, f"{url}/reviews", headers) if authored_by(r, bot_login)]
        inline = [marker for comment in existing if (marker := inline_fingerprint(comment.get("body") or ""))]
        posted = {fp for kind, fp in inline if kind == "fp"}
        posted_unverified = {fp for kind, fp in inline if kind == "ufp"}
        for review in reviews:
            confirmed, unverified = summary_fingerprints(review.get("body") or "")
            posted.update(confirmed)
            posted_unverified.update(unverified)
        already = _already_posted(document, posted, posted_unverified)
        for review in reviews:
            if review_marker(review.get("body") or "") == (review_id, head_sha):
                # Recover retries even when capped items have no remote fingerprint.
                comments = [c for c in existing if c.get("pull_request_review_id") == review["id"]]
                original = {"commit_id": head_sha, "event": "COMMENT",
                            "body": review["body"], "comments": comments}
                return PublishOutcome(
                    "posted", comments_posted=len(comments), already_posted=already,
                    reason="recovered an existing review", payload=original,
                    items=_placed_items(document, original, comments), gh_review_id=review["id"],
                    bot_login=bot_login, posted_at=review.get("submitted_at"),
                )
        options.update(posted_fingerprints=posted, posted_unverified_fingerprints=posted_unverified)
        payload = render_review(head_sha, review_id, document, files, **options)
        confirmed, unverified = summary_fingerprints(payload["body"])
        new_confirmed = confirmed - posted
        new_unverified = unverified - posted - posted_unverified
        if not (payload["comments"] or new_confirmed or new_unverified):
            if any(review_marker(review.get("body") or "") for review in reviews):
                return PublishOutcome("skipped", already_posted=already, reason="nothing new", payload=payload)
        guard_url(f"{url}/reviews")
        response = transport.post(f"{url}/reviews", json=payload, headers=headers, timeout=15.0)
        observe_response(response, record_quota=False)
        if response.status_code == 422:
            payload = render_review(head_sha, review_id, document, files,
                                    outside_only=True, **options)
            reason = "GitHub returned 422; moved inline comments to Outside the diff"
            log.info("review %s: %s", review_id, reason)
            guard_url(f"{url}/reviews")
            response = transport.post(f"{url}/reviews", json=payload, headers=headers, timeout=15.0)
            observe_response(response, record_quota=False)
        response.raise_for_status()
        return _posted_metadata(
            PublishOutcome("posted", len(payload["comments"]), already, reason, payload, bot_login=bot_login),
            document, response.json(), transport, url, headers,
        )
    except Exception as error:  # noqa: BLE001 - posting must never fail a completed review
        outcome = error_outcome(error, payload=payload, already_posted=already)
        if reason:
            outcome.reason = f"{reason}; {outcome.reason}"
        return outcome
