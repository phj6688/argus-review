"""Publish GitLab findings while retaining only confirmed remote placements."""

import hashlib
import logging
import re
import threading
from collections import Counter
from dataclasses import asdict

from argus import publish as shared
from argus.errors import HeadMoved
from argus.forge.errors import ForgeDenied
from argus.gitlab import config, projects
from argus.gitlab.body import (
    assert_body_safe, neutralize_mentions, neutralize_quick_actions, parse_marker_kind,
    render_gitlab_comment,
)
from argus.gitlab.client import GitLabDenied, client as shared_client
from argus.gitlab.pacing import pacing_for
from argus.gitlab.positions import position_for
from argus.gitlab.reader import GitLabForge
from argus.gitlab.writer import GitLabWriter
from argus.publish import PublishOutcome
from argus.redact import redact

log = logging.getLogger(__name__)
_identities = {}
_identity_lock = threading.Lock()


def bot_identity(client=None) -> tuple[int, str]:
    client = client if client is not None else shared_client()
    if client is None or client._base is None:
        raise config.GitLabConfigError("GitLab is not configured")
    key = (client._base.url, hashlib.sha256(client._token.encode()).hexdigest())
    with _identity_lock:
        if key not in _identities:
            data = client.get("user").json()
            if (not isinstance(data, dict) or type(data.get("id")) is not int or data["id"] <= 0
                    or not isinstance(data.get("username"), str)
                    or not re.fullmatch(r"[A-Za-z0-9_.-]{1,255}", data["username"])):
                raise ValueError("Invalid GitLab publishing identity")
            _identities[key] = (data["id"], data["username"])
        return _identities[key]


def check_access(repo, client=None) -> dict:
    try:
        if config.base_url() is None:
            return {"state": "unconfigured", "detail": "GitLab is not configured"}
        client, ref = GitLabForge(client)._connection(repo)
        bot_id, _ = bot_identity(client)
        data = client.get("members_all", project_id=ref.project_id, user_id=bot_id).json()
        level = data.get("access_level") if isinstance(data, dict) else None
        if type(level) is not int:
            return {"state": "error", "detail": "Invalid GitLab membership"}
        if level >= 20:
            return {"state": "ok", "detail": "Reporter access or higher"}
    except config.GitLabConfigError:
        return {"state": "unconfigured", "detail": "GitLab configuration invalid"}
    except GitLabDenied as error:
        if error.reason not in {"not_found", "forbidden"}:
            return {"state": "error", "detail": "GitLab access check failed"}
    except Exception:
        return {"state": "error", "detail": "GitLab access check failed"}
    return {"state": "no_access", "detail": "Reporter access required"}


def _existing(client, project_id, pr, bot_id):
    notes, summaries, posted, unverified = {}, [], set(), set()
    for response in client.pages("mr_discussions", project_id=project_id, iid=pr, query={"per_page": 100}):
        for discussion in response.json():
            for note in discussion["notes"]:
                if (note.get("author") or {}).get("id") != bot_id:
                    continue
                if type(note.get("id")) is not int or note["id"] <= 0:
                    raise ValueError("Invalid GitLab note identity")
                body = note.get("body") or ""
                if discussion.get("individual_note") and shared.review_marker(body):
                    summaries.append(note)
                    fp, ufp = shared.summary_fingerprints(body)
                    posted.update(fp)
                    unverified.update(ufp)
                elif marker := parse_marker_kind(body):
                    if not isinstance(discussion.get("id"), str) or not discussion["id"]:
                        raise ValueError("Invalid GitLab discussion identity")
                    notes[marker] = (note, discussion["id"])
                    (posted if marker[0] == "fp" else unverified).add(marker[1])
    return notes, summaries, posted, unverified


def _item(finding, placement="summary", anchor=None, note_id=None, thread_id=None):
    return {**finding, "placement": placement, "anchor_status": anchor,
            "gh_comment_id": note_id, "gh_thread_id": thread_id}


def _anchored(note, position):
    actual = note.get("position") or {}
    return note.get("type") == "DiffNote" and bool(position) and all(
        actual.get(key) == position.get(key) for key in ("new_path", "old_path", "new_line", "old_line"))


def _position(finding, files, refs):
    patch = next((f for f in files if f.filename == finding["file"]), None)
    return position_for(finding, patch, asdict(refs)) if patch else None


def _recover(outcome, document, note, notes, files, refs):
    confirmed, unverified = shared.summary_fingerprints(note["body"])
    findings = document["findings"] + [{**f, "unverified": True} for f in document.get("unverified", [])]
    for finding in findings:
        kind = "ufp" if finding.get("unverified") else "fp"
        fp = finding.get("fingerprint")
        existing = notes.get((kind, fp))
        if fp in (unverified if kind == "ufp" else confirmed):
            anchor = "downgraded" if existing and not _anchored(existing[0], _position(finding, files, refs)) else None
            outcome.items.append(_item(finding, anchor=anchor))
        elif existing and _anchored(existing[0], _position(finding, files, refs)):
            outcome.items.append(_item(finding, "inline", "anchored", existing[0]["id"], existing[1]))
    outcome.status, outcome.reason = "posted", "recovered an existing review"
    outcome.gh_review_id = note["id"]
    outcome.payload = {"body": note["body"]}
    return outcome


def _plan(document, files, refs, posted, unverified, options):
    comments, outside, overflow, disputed = [], [], [], []
    for finding in shared._new_findings(document, options["min_severity"], posted, unverified):
        if finding.get("unverified"):
            disputed.append(finding)
        elif not (position := _position(finding, files, refs)):
            outside.append(_item(finding))
        elif len(comments) >= options["max_comments"]:
            overflow.append(_item(finding))
        else:
            body = render_gitlab_comment(finding)
            assert_body_safe(body)
            comments.append((finding, {"body": body, "position": position}))
    return comments, outside, overflow, disputed


def _summary(head, review_id, document, files, outside, overflow, disputed, already, minimum):
    findings = document["findings"]
    coverage = document.get("coverage") or {}
    lanes = [lane for lane in coverage.get("lanes", []) if lane.get("lane") == "llm"]
    reviewed = sum(lane.get("files_reviewed") or 0 for lane in lanes)
    omitted = sum(lane.get("files_omitted") or 0 for lane in lanes)
    severities, tiers = Counter(f["severity"] for f in findings), Counter(f["evidence_tier"] for f in findings)
    body = [f"**Argus review** for {head[:7]}",
            f"Verdict: {document['verdict']}\n\nCoverage: {coverage.get('level', 'unknown')}\n\n"
            f"Files reviewed: {reviewed}; omitted: {omitted}",
            "Severity counts: " + "; ".join(f"{s.capitalize()}: {severities[s]}" for s in shared._SEVERITIES),
            "Tier counts: " + "; ".join(f"{tier}: {tiers[tier]}" for tier in
                                        ("deterministic", "consensus", "single_critic", "disputed"))]
    if getattr(files, "unseen", False):
        body.append("Some changed files were not visible to this review.")
    if risk := document.get("risk"):
        line = risk["label"]
        if risk["tier"] == "risky" and risk.get("reasons"):
            line += " Reasons: " + "; ".join(shared._risk_reason(r) for r in risk["reasons"])
        body.append(line)
    for title, entries in (("Outside the diff", outside), ("Comment limit", overflow)):
        if entries:
            body.append(shared._summary_findings(title, entries))
    below = sum(shared._below_threshold(f, minimum) for f in findings + document.get("unverified", []))
    confirmed = ",".join(f["fingerprint"] for f in outside + overflow if f.get("fingerprint"))

    def finish(count, section):
        ufp = ",".join(f["fingerprint"] for f in disputed[:count] if f.get("fingerprint"))
        footer = [f"Below posting threshold: {below}", f"Already posted: {already}",
                  f"<!-- argus:review={review_id} head={head} -->\n<!-- argus:manifest fp={confirmed} ufp={ufp} -->"]
        return neutralize_quick_actions(neutralize_mentions("\n\n".join(body + section + footer)))

    listed = 0
    section = [f"{len(disputed)} more unverified findings are not listed."] if disputed else []
    rendered = finish(0, section)
    for count in range(1, min(len(disputed), shared._MAX_UNVERIFIED) + 1):
        section = [shared._summary_findings("Unverified (the verifier disagreed)", disputed[:count],
                   "These findings were refuted by a second check. They are listed for a human "
                   "decision and do not affect the verdict.")]
        if remaining := len(disputed) - count:
            section.append(f"{remaining} more unverified findings are not listed.")
        candidate = finish(count, section)
        if len(candidate) > shared._MAX_BODY:
            break
        rendered, listed = candidate, count
    if len(rendered) > shared._MAX_BODY:
        raise ValueError("GitLab summary exceeds body limit")
    assert_body_safe(rendered)
    return rendered, outside + overflow + [_item(f) for f in disputed[:listed]]


def _partial(outcome):
    outcome.status, outcome.reason = "error", "partial_post"
    outcome.payload["counts"]["confirmed_inline"] = outcome.comments_posted
    return outcome


def _flow(forge, client, project_id, repo, pr, head, review_id, document, files, mode, outcome, prior):
    live = "unavailable"
    try:
        live = forge.head_sha(repo, pr)
        if live != head:
            raise HeadMoved("MR head changed")
        refs = forge.diff_refs(repo, pr)
        if refs.head_sha != head:
            live = refs.head_sha
            raise HeadMoved("MR head changed")
    except HeadMoved:
        live = live[:7] if re.fullmatch(r"[0-9a-fA-F]{40}", live) else "unavailable"
        outcome.reason = f"MR head moved since the review ({head[:7]} -> {live})"
        return outcome
    notes, summaries, posted, unverified = _existing(client, project_id, pr, outcome.bot_user_id)
    outcome.already_posted = shared._already_posted(document, posted, unverified)
    for note in summaries:
        if mode != "dry-run" and shared.review_marker(note["body"]) == (review_id, head):
            return _recover(outcome, document, note, notes, files, refs)
    options = shared.render_options()
    comments, outside, overflow, disputed = _plan(document, files, refs, posted, unverified, options)
    summarized = set().union(*(shared.summary_fingerprints(n["body"])[0] for n in summaries))
    for finding in document["findings"]:
        existing = notes.get(("fp", finding.get("fingerprint")))
        if (existing and finding.get("fingerprint") not in summarized
                and not _anchored(existing[0], _position(finding, files, refs))):
            outside.append(_item(finding, anchor="downgraded"))
    body, summary_items = _summary(head, review_id, document, files, outside, overflow, disputed,
                                   outcome.already_posted, options["min_severity"])
    outcome.payload = {"body": body, "comments": [payload for _, payload in comments],
                       "counts": {"attempted": 0, "confirmed_inline": 0, "unresolved": 0}}
    # Another review's summary does not complete this partial review.
    owes_summary = isinstance(prior, dict) and prior.get("status") == "error" and prior.get("reason") == "partial_post"
    if mode != "dry-run" and not comments and not summary_items and summaries and not owes_summary:
        outcome.reason = "nothing new"
        return outcome
    if mode == "dry-run":
        outcome.status = "dry_run"
        outcome.items = [_item(f, "inline") for f, _ in comments] + summary_items
        for item in outcome.items:
            item["anchor_status"] = None
        return outcome
    recovered = set()
    for finding in document["findings"]:
        fp = finding.get("fingerprint")
        existing = notes.get(("fp", fp))
        if fp not in recovered and existing and _anchored(existing[0], _position(finding, files, refs)):
            outcome.items.append(_item(finding, "inline", "anchored", existing[0]["id"], existing[1]))
            recovered.add(fp)
    writer = GitLabWriter(client, outcome.bot_user_id)
    for finding, payload in comments:
        assert_body_safe(payload["body"])
        outcome.payload["counts"]["attempted"] += 1
        result = writer.post_discussion(project_id, pr, **payload)
        if result.outcome in {"posted", "already_posted"}:
            note = result.first_note or {}
            # Refetched notes can legitimately move to a newer head after a push.
            head_matches = (result.outcome == "already_posted"
                            or (note.get("position") or {}).get("head_sha") == payload["position"]["head_sha"])
            if _anchored(note, payload["position"]) and head_matches:
                outcome.items.append(_item(finding, "inline", "anchored", result.note_id, result.discussion_id))
                outcome.comments_posted += 1
            else:
                outside.append(_item(finding, anchor="downgraded"))
        elif result.outcome == "failed" and result.status_code in {400, 500}:
            outside.append(_item(finding, anchor="rejected_400" if result.status_code == 400 else "rejected_500"))
        else:
            # A completion marker would make an unresolved attempt look successful on recovery.
            outcome.payload["counts"]["unresolved"] += 1
            return _partial(outcome)
    body, summary_items = _summary(head, review_id, document, files, outside, overflow, disputed,
                                   outcome.already_posted, options["min_severity"])
    outcome.payload["body"] = body
    assert_body_safe(body)
    outcome.payload["counts"]["attempted"] += 1
    result = writer.post_note(project_id, pr, body)
    if result.outcome not in {"posted", "already_posted"}:
        outcome.payload["counts"]["unresolved"] += 1
        return _partial(outcome)
    outcome.items.extend(summary_items)
    outcome.gh_review_id = result.note_id
    outcome.status = "posted"
    outcome.payload["counts"]["confirmed_inline"] = outcome.comments_posted
    return outcome


def publish(repo, pr, head_sha, review_id, document, files, *, client=None, prior=None) -> PublishOutcome:
    outcome = PublishOutcome("skipped")
    try:
        mode = config.effective_publish_mode()
        if mode == "0":
            return PublishOutcome("skipped", reason="publishing disabled")
        if repo not in config.publish_projects():
            return PublishOutcome("skipped", reason="repo not allowlisted")
        if type(pr) is not int or pr <= 0 or not isinstance(head_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", head_sha):
            return PublishOutcome("skipped", reason="Publishing requires an MR and a 40-hex head SHA")
        forge = GitLabForge(client)
        client, ref = forge._connection(repo)
        meta = forge.change_meta(repo, pr)
        if not projects.live_deny_change(repo, meta, client=client).allowed:
            return PublishOutcome("skipped", reason="repository denied")
        try:
            outcome.bot_user_id, outcome.bot_login = bot_identity(client)
        except Exception:
            return PublishOutcome("skipped", reason="Publishing identity could not be resolved")
        if check_access(repo, client)["state"] != "ok":
            outcome.reason = "GitLab publishing access unavailable"
            return outcome
        with pacing_for(client).write_lock(outcome.bot_user_id, ref.project_id, pr):
            meta = forge.change_meta(repo, pr)
            if not projects.live_deny_change(repo, meta, client=client).allowed:
                outcome.reason = "repository denied"
                return outcome
            return _flow(forge, client, ref.project_id, repo, pr, head_sha, review_id,
                         document, files, mode, outcome, prior)
    except Exception as error:
        reason = redact(f"{type(error).__name__}: {error}")
        log.warning("GitLab publishing failed: %s", reason)
        if outcome.payload and outcome.payload["counts"]["attempted"]:
            outcome.payload["error"] = reason
            outcome.payload["counts"]["unresolved"] += 1
            return _partial(outcome)
        if isinstance(error, (config.GitLabConfigError, ForgeDenied)):
            outcome.reason = "GitLab publishing configuration unavailable"
        else:
            outcome.status, outcome.reason = "error", reason
        return outcome
