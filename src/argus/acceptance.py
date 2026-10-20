"""Collect author outcomes and measure acceptance of published findings."""

import logging
import math
import os
import re
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import github, publish, replies
from .repo_policy import validate_repo

NOTE = (
    "Strict acceptance means a first trusted non-bot reply opening with a fixed verb, or a "
    "reviewer re-check confirming a fix at a later head. A resolved thread alone "
    "does not count. Resolved threads get record-only re-checks until a fix is confirmed. "
    "The report labels every row with the current rule, including older rows. "
    "Pull requests closed more than 7 days ago get no new re-check. "
    "The re-check substitutes for the "
    "reference corpus publisher's own Addressed in commit marker. The re-check is stricter: "
    "deletions, fixes outside the flagged lines, and fixes that keep an evidence line "
    "do not count as strict. It mostly under-counts, but a model that misjudges can still over-count. "
    "A helper extracted with renamed arguments can keep the defect in the same file or another file with a patch. "
    "A moved short call under 8 characters can escape the move check. "
    "A moved call can also escape the move check when its destination reuses unchanged lines for its arguments or closing parenthesis. "
    "A cosmetic edit of the first flagged line can pass with empty or signature-only evidence. "
    "An in-place edit can keep the defect, for example an added comment or a .strip(). "
    "The anchored range includes lines that replace deleted flagged lines. "
    "A long replacement block is in place for the move check. The model still sees that block in its window. "
    "A defect call or line that also occurs elsewhere in the reviewed file, including in a comment or a string, "
    "is not a signature, so its move is left to the model. "
    "Loose acceptance "
    "also counts addressed regions unless the first reply rejects or defers the finding. "
    "Rates divide by all posted findings, including unconfirmed findings, as the "
    "reference corpus does. Compare inline findings only. Summary findings have no "
    "reply thread. Unverified items are review-body items and are excluded from "
    "the inline comparison like other body items. The final group holds closed pull requests only."
)
log = logging.getLogger("argus.acceptance")
_last_runs = {}

_THREADS = """query($owner: String!, $name: String!, $pr: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      reviewThreads(first:100) {
        nodes { id isResolved comments(first:1) { nodes { databaseId } } }
      }
    }
  }
}"""


def first_line(text):
    return next((line.strip() for line in (text or '').splitlines() if line.strip()), '')


def _reply_opening(text):
    # Only the opening of the first non-empty line. Never inspect subsequent prose/code.
    opening = re.sub(r'^[\W_]+', '', first_line(text), flags=re.UNICODE)
    patterns = [
        ('fixed', r'(?:fix(?:ed)?|done|addressed|resolved|applied|implemented|added|updated|changed)\b'),
        ('rejected', r'(?:reject(?:ed|ing)?|not changing|won[’\x27]t fix|wontfix|declin\w*|disagree\w*|no change|keeping|intentional|by design|false positive)\b'),
        ('deferred', r'(?:defer\w*|follow-up|followup|tracked in|out of scope|later)\b'),
    ]
    for verdict, pattern in patterns:
        match = re.match(f"(?P<{verdict}>{pattern})", opening, re.I)
        if match:
            return opening, match
    return opening, None


def classify_reply(text):
    if text is None:
        return 'none'
    _, match = _reply_opening(text)
    return match.lastgroup if match else 'other'


def hunk_overlaps(hunk, start, end, padding=0):
    match = re.match(r'^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@', hunk)
    if not match:
        return False
    a, b = int(match[1]), int(match[2]) if match[2] is not None else 1
    old_end = a + b - 1 if b else a
    return a <= end + padding and old_end >= start - padding


def _change_blocks(patch):
    old = None
    start = None
    deleted = 0
    for line in [*patch.split("\n"), ""]:
        if line.startswith("\\"):
            continue
        if old is not None and line.startswith(("-", "+")):
            if start is None:
                start = old
            if line.startswith("-"):
                deleted += 1
                old += 1
            continue
        if start is not None:
            yield f"@@ -{start if deleted else start - 1},{deleted} +0,0 @@"
            start, deleted = None, 0
        match = re.match(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@", line)
        if match:
            # Empty old ranges name the line before the insertion point.
            old = int(match[1]) + (match[2] == "0")
        elif line.startswith(" ") and old is not None:
            old += 1
        else:
            old = None


def region_changed(finding, files):
    # A capped comparison cannot prove an omitted file stayed unchanged.
    if len(files) >= 300 and not any(
        finding["path"] in {file.get("filename"), file.get("previous_filename")} for file in files
    ):
        return None
    for file in files:
        if (file.get("status") == "renamed" and file.get("previous_filename") == finding["path"]
                and file["filename"] != finding["path"]):
            return True
        if file["filename"] != finding["path"]:
            continue
        if file.get("status") == "removed":
            return True
        if not file.get("patch"):
            return None
        if finding["line"] > 0 and any(
            hunk_overlaps(hunk, finding["line"], finding["end_line"] or finding["line"], padding=2)
            for hunk in _change_blocks(file["patch"])
        ):
            return True
    return False


def labels(reply, addressed, resolved=False, rechecked=False):
    strict = reply == "fixed" or rechecked
    if strict:
        return "strict_accepted", True
    if reply in {"rejected", "deferred"}:
        return reply, False
    if addressed:
        return "loose_accepted", True
    return "unconfirmed", False


def _stamp(value):
    from .api.db import _parse_created_at

    return _parse_created_at(value)


def _record(conn, finding_id, kind, value, evidence_id, now, occurred_at=None, body=None,
            author_association=None, author_login=None):
    conn.execute(
        "INSERT INTO finding_outcomes(finding_id,kind,value,evidence_id,observed_at,occurred_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(finding_id, kind, evidence_id) DO UPDATE SET "
        "value=excluded.value, observed_at=excluded.observed_at, occurred_at=excluded.occurred_at",
        (finding_id, kind, value, str(evidence_id), now, occurred_at),
    )
    if kind == "reply" and body is not None:
        conn.execute(
            "INSERT INTO reply_texts(finding_id,evidence_id,body,author_association,author_login) "
            "VALUES (?,?,?,?,?) ON CONFLICT(finding_id,evidence_id) DO UPDATE SET "
            "body=excluded.body,author_association=excluded.author_association,author_login=excluded.author_login",
            (finding_id, str(evidence_id), body, author_association, author_login),
        )


def _signals(outcomes, actions=(), original_head=None):
    trusted_replies = sorted((o for o in outcomes if o["kind"] == "reply" and replies.trusted(o, {})),
                     key=lambda o: (_stamp(o["occurred_at"] or o["observed_at"]), int(o["evidence_id"])))
    resolutions = sorted((o for o in outcomes if o["kind"] == "resolved"),
                         key=lambda o: _stamp(o["observed_at"]))
    reply = trusted_replies[0]["value"] if trusted_replies else "none"
    addressed = any(o["kind"] == "addressed" and o["value"] == "true" for o in outcomes)
    resolved = bool(resolutions and resolutions[-1]["value"] == "true")
    rechecked = any(a["kind"] == "recheck" and a["value"] == "fixed"
                    and a["evidence_id"] != original_head for a in actions)
    label, loose = labels(reply, addressed, resolved, rechecked)
    return label, loose, trusted_replies[0] if trusted_replies else None


def _sync_rejections(conn, findings, now):
    from .jarvis.live import _norm, invalidate_live_fps

    for finding in findings:
        finding = dict(finding)
        outcomes = conn.execute(
            "SELECT o.*,t.author_association,t.author_login FROM finding_outcomes o "
            "LEFT JOIN reply_texts t ON o.kind='reply' AND o.finding_id=t.finding_id AND o.evidence_id=t.evidence_id "
            "WHERE o.finding_id=?", (finding["id"],),
        ).fetchall()
        actions = conn.execute("SELECT * FROM bot_actions WHERE finding_id=?", (finding["id"],)).fetchall()
        label, _, reply = _signals(outcomes, actions, finding["head_sha"])
        changed_resolved = (any(o["kind"] == "addressed" and o["value"] == "true" for o in outcomes)
                            and any(o["kind"] == "resolved" and o["value"] == "true" for o in outcomes))
        fixed = any(a["kind"] == "recheck" and a["value"] == "fixed"
                    and a["evidence_id"] != finding["head_sha"] for a in actions)
        if fixed:
            # A later fix also invalidates suppression seeded by an earlier rejection.
            conn.execute("DELETE FROM known_fps_live WHERE repo=? AND file=? AND title=? AND fingerprint=?",
                         (finding["repo"], finding["path"], _norm(finding["title"]), finding["fingerprint"]))
        elif label == "rejected" and not changed_resolved and replies.trusted(reply, finding):
            conn.execute(
                "INSERT OR IGNORE INTO known_fps_live VALUES (?, ?, ?, ?, ?, ?)",
                (finding["repo"], finding["path"], _norm(finding["title"]), finding["fingerprint"],
                 reply["evidence_id"], now),
            )
    invalidate_live_fps()


def _replies(conn, repo, findings, since, now):
    identities = {row[0].casefold() for row in conn.execute(
        "SELECT bot_login FROM publishing_identities WHERE repo=?", (repo,)
    )}
    # Without the publishing identities, a reply could be mistaken for author acceptance.
    if not identities:
        raise RuntimeError("Publishing identity is missing")
    comments = github.list_cached(f"/repos/{repo}/pulls/comments", {
        "sort": "updated", "direction": "asc", "since": since, "per_page": 100,
    })
    by_comment = {f["gh_comment_id"]: f for f in findings
                  if f["placement"] == "inline" and f["gh_comment_id"]}
    # Timestamp precision is one second; replay the boundary instead of losing ties.
    cursor = max([_stamp(since)] + [
        _stamp(c["updated_at"]) - timedelta(seconds=1) for c in comments
    ]).isoformat()
    with conn:
        for comment in comments:
            finding = by_comment.get(comment.get("in_reply_to_id"))
            user = comment.get("user") or {}
            if not finding or not user.get("login"):
                continue
            marker = replies.MARKER.search(comment.get("body") or "")
            if user["login"].casefold() in identities:
                if marker and marker["evidence"]:
                    replies.recover(conn, finding, comment, marker, now)
                continue
            if user.get("type") == "Bot" or marker:
                continue
            _record(conn, finding["id"], "reply", classify_reply(comment.get("body") or ""),
                    comment["id"], now, comment["created_at"], comment.get("body") or "",
                    comment.get("author_association"), user["login"])
        conn.execute(
            "INSERT INTO outcome_cursors(repo, since) VALUES (?, ?) "
            "ON CONFLICT(repo) DO UPDATE SET since=excluded.since", (repo, cursor),
        )


def _resolutions(conn, repo, pr, findings, pull, now, run):
    recent = pull.get("state") == "open" or (
        pull.get("closed_at") and _stamp(pull["closed_at"]) >= _stamp(now) - timedelta(days=7)
    )
    inline = {f["gh_comment_id"]: f for f in findings if f["placement"] == "inline" and f["gh_comment_id"]}
    if not inline or not recent or run["graphql_paused"]:
        return
    try:
        owner, name = repo.split("/")
        data = github.graphql_read(_THREADS, {"owner": owner, "name": name, "pr": pr})
        threads = data["repository"]["pullRequest"]["reviewThreads"]["nodes"]
        with conn:
            for thread in threads:
                for comment in thread["comments"]["nodes"]:
                    finding = inline.get(comment["databaseId"])
                    if finding:
                        finding["thread_resolved"] = thread["isResolved"]
                        finding["gh_thread_id"] = thread.get("id")
                        conn.execute("UPDATE published_findings SET gh_thread_id=? WHERE id=?",
                                     (thread.get("id"), finding["id"]))
                        _record(conn, finding["id"], "resolved", str(thread["isResolved"]).lower(),
                                comment["databaseId"], now)
    except github.GraphQLRateLimited:
        run["graphql_paused"] = True
    except github.GitHubRateLimited:
        raise
    except Exception as error:
        _failure(run, repo, error, pr)


def _comparisons(conn, repo, pr, findings, head, comparisons, now, run):
    by_head = defaultdict(list)
    for finding in findings:
        if not conn.execute(
            "SELECT 1 FROM finding_outcomes WHERE finding_id=? AND kind='addressed' AND evidence_id=?",
            (finding["id"], head),
        ).fetchone():
            by_head[finding["head_sha"]].append(finding)
    for old, items in by_head.items():
        try:
            comparison = {"status": "identical"}
            if head != old:
                key = (repo, old, head)
                if key not in comparisons:
                    response = github.get(f"/repos/{repo}/compare/{old}...{head}")
                    response.raise_for_status()
                    comparisons[key] = response.json()
                    if comparisons[key].get("status") in {"behind", "diverged"}:
                        run["compares_not_ancestor"] += 1
                comparison = comparisons[key]
            with conn:
                for finding in items:
                    changed = None
                    if comparison.get("status") == "identical":
                        changed = False
                    elif comparison.get("status") == "ahead":
                        changed = region_changed(finding, comparison["files"])
                    value = "unknown" if changed is None else str(changed).lower()
                    _record(conn, finding["id"], "addressed", value, head, now)
        except github.GitHubRateLimited:
            raise
        except Exception as error:
            _failure(run, repo, error, pr)


def _pr_outcomes(conn, repo, pr, findings, comparisons, now, run, *, replies_ready=True):
    try:
        pull, _ = github.get_json_cached(f"/repos/{repo}/pulls/{pr}")
        with conn:
            conn.execute(
                "INSERT INTO outcome_prs VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(repo, pr) DO UPDATE SET state=excluded.state, merged=excluded.merged, "
                "closed_at=excluded.closed_at, head_sha=excluded.head_sha, checked_at=excluded.checked_at",
                (repo, pr, pull["state"], int(pull.get("merged_at") is not None),
                 pull.get("closed_at"), pull["head"]["sha"], now),
            )
        for finding in findings:
            finding["pr_author"] = (pull.get("user") or {}).get("login", "")
        _resolutions(conn, repo, pr, findings, pull, now, run)
        _comparisons(conn, repo, pr, findings, pull["head"]["sha"], comparisons, now, run)
        if replies_ready:
            replies.run(conn, repo, findings, pull["head"]["sha"], comparisons, now, run)
    finally:
        with conn:
            _sync_rejections(conn, findings, now)


def _recover_comment_ids(conn, repo, findings, run):
    missing = defaultdict(list)
    for finding in findings:
        if (finding["placement"] == "inline" and finding["gh_review_id"]
                and not finding["gh_comment_id"] and finding["comment_lookups"] < 3):
            missing[(finding["pr"], finding["gh_review_id"])].append(finding)
    for (pr, review_id), items in missing.items():
        try:
            comments = github.list_cached(f"/repos/{repo}/pulls/{pr}/reviews/{review_id}/comments",
                                          {"per_page": 100})
            ids = {marker[1]: c["id"] for c in comments
                   if (marker := publish.inline_fingerprint(c.get("body") or "")) and marker[0] == "fp"}
            with conn:
                for finding in items:
                    comment_id = ids.get(finding["fingerprint"])
                    if comment_id:
                        conn.execute("UPDATE published_findings SET gh_comment_id=? WHERE id=?",
                                     (comment_id, finding["id"]))
                        finding["gh_comment_id"] = comment_id
                    else:
                        conn.execute("UPDATE published_findings SET comment_lookups=comment_lookups+1 WHERE id=?",
                                     (finding["id"],))
                        finding["comment_lookups"] += 1
        except github.GitHubRateLimited:
            raise
        except Exception as error:
            _failure(run, repo, error, pr)


def _failure(run, repo, error, pr=None):
    run["failures"] += 1
    log.warning("outcomes: repo=%s pr=%s failed: %s", repo, pr, type(error).__name__)


def _reconcile(conn, path, now, run):
    from . import verdict
    from .api import db

    if os.environ.get("ARGUS_PUBLISH") != "1":
        return
    candidates = conn.execute(
        "SELECT * FROM reviews r WHERE pr>0 AND status='done' "
        "AND (publish_status IS NULL OR publish_status='error') "
        "AND julianday(created_at)>=julianday(?) AND julianday(created_at)<=julianday(?) "
        "AND NOT EXISTS (SELECT 1 FROM published_findings f WHERE f.review_id=r.id AND mode='posted') "
        "AND NOT EXISTS (SELECT 1 FROM publish_reconciliations p WHERE p.review_id=r.id)",
        ((now - timedelta(days=7)).isoformat(), (now - timedelta(minutes=2)).isoformat()),
    ).fetchall()
    for row in candidates:
        review = dict(row)
        repo, pr, rid = review["repo"], review["pr"], review["id"]
        if not publish.enabled_for(repo) or not re.fullmatch(r"[0-9a-fA-F]{40}", review["head_sha"] or ""):
            continue
        try:
            validate_repo(repo)
            bot_login = publish._bot_login()
            reviews = github.list_cached(f"/repos/{repo}/pulls/{pr}/reviews", {"per_page": 100})
            remote = next((r for r in reviews if publish.authored_by(r, bot_login)
                           and publish.review_marker(r.get("body") or "") == (rid, review["head_sha"])), None)
            outcome = None
            if remote:
                comments = github.list_cached(f"/repos/{repo}/pulls/{pr}/comments", {"per_page": 100})
                comments = [c for c in comments if c.get("pull_request_review_id") == remote["id"]
                            and publish.authored_by(c, bot_login)]
                document = verdict.document(review, db.get_findings(path, rid),
                                            unverified=db.get_unverified_findings(path, rid))
                payload = {"body": remote["body"], "comments": comments}
                items = publish._placed_items(document, payload, comments)
                outcome = publish.PublishOutcome(
                    "posted", comments_posted=sum(i["placement"] == "inline" for i in items),
                    reason="recovered an existing review", items=items, gh_review_id=remote["id"],
                    posted_at=remote["submitted_at"], bot_login=bot_login,
                )
            # Keep recovered items and the one-time check in the same transaction.
            with conn:
                if outcome:
                    db._save_published_items(conn, rid, outcome)
                    conn.execute(
                        "UPDATE reviews SET publish_status='posted', published_at=?, "
                        "published_comments=?, publish_error=? WHERE id=?",
                        (outcome.posted_at, outcome.comments_posted, outcome.reason, rid),
                    )
                conn.execute("INSERT INTO publish_reconciliations VALUES (?, ?, ?)",
                             (rid, now.isoformat(), int(remote is not None)))
            run["reconciled_found" if remote is not None else "reconciled_missing"] += 1
        except github.GitHubRateLimited:
            raise
        except Exception as error:
            _failure(run, repo, error, pr)


def collect(path=None):
    """Commit cursors before checking PRs so quota pauses do not lose progress."""
    from .api import db

    now = datetime.now(timezone.utc)
    since_default = (now - timedelta(days=30)).isoformat()
    run = {"started_at": now.isoformat(), "finished_at": None, "repos": 0,
           "prs_checked": 0, "prs_skipped_final": 0, "failures": 0,
           "stopped_by_rate_limit": False, "graphql_paused": False,
           "compares_not_ancestor": 0, "reconciled_found": 0, "reconciled_missing": 0,
           "bot": replies.counts(), "rechecks_attempted": 0, "writes_attempted": 0}
    _last_runs[str(Path(path or db.db_path()).resolve())] = run
    comparisons = {}
    try:
        with closing(db.connect(path)) as conn:
            _reconcile(conn, path, now, run)
            by_repo = defaultdict(list)
            for row in conn.execute("SELECT * FROM published_findings WHERE mode='posted'"):
                by_repo[row["repo"]].append(dict(row))
            for repo, items in by_repo.items():
                run["repos"] += 1
                try:
                    validate_repo(repo)
                    by_pr = defaultdict(list)
                    for finding in items:
                        by_pr[finding["pr"]].append(finding)
                    active = {}
                    for pr, pr_items in by_pr.items():
                        stored = conn.execute("SELECT state, closed_at FROM outcome_prs WHERE repo=? AND pr=?",
                                              (repo, pr)).fetchone()
                        if (stored and stored["state"] == "closed" and stored["closed_at"]
                                and _stamp(stored["closed_at"]) < now - timedelta(days=7)
                                and not conn.execute(
                                    "SELECT 1 FROM pr_heads WHERE repo=? AND pr=? AND is_open=1", (repo, pr),
                                ).fetchone()):
                            run["prs_skipped_final"] += 1
                        else:
                            active[pr] = pr_items
                    if not active:
                        continue
                    items = [item for pr_items in active.values() for item in pr_items]
                    cursor = conn.execute("SELECT since FROM outcome_cursors WHERE repo=?", (repo,)).fetchone()
                    _recover_comment_ids(conn, repo, items, run)
                    since = max(since_default, cursor[0], key=_stamp) if cursor else since_default
                    # Missing identities need replies from posting time, including the boundary second.
                    pending = [(_stamp(f["posted_at"]) - timedelta(seconds=1)).isoformat() for f in items
                               if f["placement"] == "inline" and not f["gh_comment_id"]
                               and f["comment_lookups"] < 3]
                    if pending:
                        since = min([since, *pending], key=_stamp)
                except github.GitHubRateLimited:
                    raise
                except Exception as error:
                    _failure(run, repo, error)
                    continue
                replies_ready = False
                try:
                    _replies(conn, repo, items, since, now.isoformat())
                    replies_ready = True
                except github.GitHubRateLimited:
                    raise
                except Exception as error:
                    _failure(run, repo, error)
                for pr, pr_items in active.items():
                    run["prs_checked"] += 1
                    try:
                        _pr_outcomes(conn, repo, pr, pr_items, comparisons, now.isoformat(), run,
                                     replies_ready=replies_ready)
                    except github.GitHubRateLimited:
                        raise
                    except Exception as error:
                        _failure(run, repo, error, pr)
                with conn:
                    conn.execute(
                        "INSERT INTO outcome_cursors(repo, since, last_collected_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(repo) DO UPDATE SET last_collected_at=excluded.last_collected_at",
                        (repo, since_default, now.isoformat()),
                    )
    except github.GitHubRateLimited:
        run["stopped_by_rate_limit"] = True
        raise
    finally:
        run["finished_at"] = datetime.now(timezone.utc).isoformat()


def wilson(successes, total):
    if not total:
        return [None, None]
    z = 1.959963984540054
    rate = successes / total
    denominator = 1 + z * z / total
    center = (rate + z * z / (2 * total)) / denominator
    radius = z * math.sqrt(rate * (1 - rate) / total + z * z / (4 * total * total)) / denominator
    return [max(0.0, center - radius), min(1.0, center + radius)]


def _counts(items):
    result = dict.fromkeys(("posted", "strict_accepted", "loose_accepted", "rejected", "deferred", "unconfirmed"), 0)
    for label, loose in items:
        result["posted"] += 1
        if label != "loose_accepted":
            result[label] += 1
        result["loose_accepted"] += int(loose)
    posted = result["posted"]
    for key in ("strict", "loose"):
        result[f"{key}_rate"] = result[f"{key}_accepted"] / posted if posted else None
        result[f"{key}_wilson_95"] = wilson(result[f"{key}_accepted"], posted)
    return result


def report(conn, since=None):
    if since:
        since = _stamp(since).isoformat()
    findings = conn.execute(
        "SELECT * FROM published_findings WHERE mode='posted' "
        "AND (? IS NULL OR julianday(posted_at)>=julianday(?))", (since, since),
    ).fetchall()
    outcomes = defaultdict(list)
    has_reply_texts = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reply_texts'").fetchone()
    outcome_query = (
        "SELECT o.*,t.author_association FROM finding_outcomes o "
        "LEFT JOIN reply_texts t ON o.kind='reply' AND o.finding_id=t.finding_id AND o.evidence_id=t.evidence_id"
        if has_reply_texts else "SELECT *,NULL AS author_association FROM finding_outcomes"
    )
    for row in conn.execute(outcome_query):
        outcomes[row["finding_id"]].append(row)
    actions = defaultdict(list)
    bot = replies.counts()
    selected_ids = {f["id"] for f in findings}
    has_actions = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bot_actions'").fetchone()
    for row in conn.execute("SELECT * FROM bot_actions") if has_actions else ():
        actions[row["finding_id"]].append(row)
        if row["finding_id"] not in selected_ids:
            continue
        if row["kind"] == "recheck":
            bot["rechecks"][row["value"]] += 1
        else:
            bot["replies" if row["kind"] == "reply" else "resolutions"] += 1
    closed = {(row["repo"], row["pr"]) for row in conn.execute(
        "SELECT repo, pr FROM outcome_prs WHERE state='closed'"
    )}

    def grouped(selected):
        repos, severities = defaultdict(list), defaultdict(list)
        placements = {"inline": [], "summary": []}
        unverified = []
        all_items = []
        for finding in selected:
            signals = outcomes[finding["id"]]
            if finding["placement"] == "summary":
                signals = [o for o in signals if o["kind"] == "addressed"]
            checks = actions[finding["id"]] if finding["placement"] == "inline" else []
            label, loose, _ = _signals(signals, checks, finding["head_sha"])
            item = (label, loose)
            all_items.append(item)
            repos[finding["repo"]].append(item)
            severities[finding["severity"]].append(item)
            placements[finding["placement"]].append(item)
            if "unverified" in finding.keys() and finding["unverified"]:
                unverified.append(item)
        return {"totals": _counts(all_items), "by_repo": {k: _counts(v) for k, v in repos.items()},
                "by_severity": {k: _counts(v) for k, v in severities.items()},
                "by_placement": {k: _counts(v) for k, v in placements.items()},
                "unverified": _counts(unverified)}

    identities = defaultdict(list)
    for repo, login in conn.execute("SELECT repo, bot_login FROM publishing_identities ORDER BY repo, bot_login"):
        identities[repo].append(login)
    last = conn.execute("SELECT MAX(last_collected_at) FROM outcome_cursors").fetchone()[0]
    return {**grouped(findings),
            "final": grouped(f for f in findings if (f["repo"], f["pr"]) in closed),
            "bot": bot, "bot_logins": dict(identities), "last_collection_at": last, "note": NOTE}


def stats(path=None, since=None):
    from .api import db

    with closing(sqlite3.connect(Path(path or db.db_path()).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        run = _last_runs.get(str(Path(path or db.db_path()).resolve()))
        return {**report(conn, since), "last_run": dict(run) if run else None}
