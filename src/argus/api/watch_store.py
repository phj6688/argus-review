"""Durable head queue and attempt history."""

import logging
import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from ..errors import HeadMoved, classify_error
from ..github import GitHubRateLimited
from ..repo_policy import TRUSTED_ASSOCIATIONS, env_int
from . import db

log = logging.getLogger("argus.watcher")
STATUSES = ("queued", "running", "done", "error", "parked", "skipped", "superseded", "closed")
_listed_repos = set()


@contextmanager
def connection(path):
    conn = db.connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def utcnow():
    return datetime.now(timezone.utc)


def truthy(name):
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def trusted_only():
    return os.getenv("ARGUS_WATCH_TRUSTED_ONLY", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def skip_reason(pr):
    user = pr.get("user") or {}
    login = user.get("login", "").lower()
    if pr.get("draft") and not truthy("ARGUS_WATCH_INCLUDE_DRAFTS"):
        return "draft"
    if (
        user.get("type") == "Bot"
        or login.endswith("[bot]")
        or login in {"dependabot", "renovate", "dependabot-preview"}
    ):
        if not truthy("ARGUS_WATCH_INCLUDE_BOTS"):
            return "bot"
    if trusted_only() and pr.get("author_association") not in TRUSTED_ASSOCIATIONS:
        reason = f"untrusted author association: {pr.get('author_association') or 'missing'}"
        log.info("watcher: skipped PR #%s: %s", pr.get("number"), reason)
        return reason
    return None


def heads(path):
    with connection(path) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM pr_heads ORDER BY id")]


def record_listing(path, repo, prs, now=None):
    now = now or utcnow()
    stamp = now.isoformat()
    listing_key = (os.path.realpath(path or db.db_path()), repo.casefold())
    first_listing = listing_key not in _listed_repos
    queued = 0
    with connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE pr_heads SET is_open=0 WHERE repo=?", (repo,))
        for pr in prs:
            sha = (pr.get("head") or {}).get("sha")
            if not sha or pr.get("number") is None:
                continue
            number, reason = int(pr["number"]), skip_reason(pr)
            user = pr.get("user") or {}
            pushed = min(now, db._parse_created_at(pr["updated_at"])) if pr.get("updated_at") else now
            baseline = first_listing and (now - pushed).total_seconds() > 900
            conn.execute(
                "UPDATE pr_heads SET status='superseded', ended_at=? "
                "WHERE repo=? AND pr=? AND head_sha<>? AND status='queued'",
                (stamp, repo, number, sha),
            )
            old = conn.execute(
                "SELECT * FROM pr_heads WHERE repo=? AND pr=? AND head_sha=?", (repo, number, sha)
            ).fetchone()
            done = conn.execute(
                "SELECT id, completed_at FROM reviews WHERE repo=? AND pr=? "
                "AND head_sha=? AND status='done' ORDER BY id LIMIT 1",
                (repo, number, sha),
            ).fetchone()
            status = old["status"] if old else "queued"
            if done:
                status = "done"
            elif status != "running":
                if reason:
                    status = "skipped"
                elif status in {"skipped", "closed", "superseded"}:
                    status = (
                        "parked"
                        if old["attempts"] >= env_int("ARGUS_WATCH_MAX_ATTEMPTS", 4)
                        else "queued"
                    )
            if status == "queued" and (not old or old["status"] != "queued"):
                queued += 1
            conn.execute(
                """INSERT INTO pr_heads
                (repo,pr,head_sha,first_seen_at,pr_updated_at,draft,author,author_type,author_association,base_ref,
                 status,skip_reason,last_listed_at,review_id,completed_at,head_committed_at,
                 eligible_at,eligible_pr_updated_at,baseline)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(repo,pr,head_sha) DO UPDATE SET
                  draft=excluded.draft, author=excluded.author, author_type=excluded.author_type,
                  author_association=excluded.author_association,
                  base_ref=excluded.base_ref, status=excluded.status, skip_reason=excluded.skip_reason,
                  last_listed_at=excluded.last_listed_at, is_open=1, ended_at=NULL,
                  eligible_pr_updated_at=CASE WHEN pr_heads.eligible_at IS NULL
                    THEN excluded.eligible_pr_updated_at ELSE pr_heads.eligible_pr_updated_at END,
                  eligible_at=COALESCE(pr_heads.eligible_at,excluded.eligible_at),
                  review_id=COALESCE(excluded.review_id,pr_heads.review_id),
                  completed_at=COALESCE(excluded.completed_at,pr_heads.completed_at),
                  head_committed_at=COALESCE(excluded.head_committed_at,pr_heads.head_committed_at)""",
                (
                    repo,
                    number,
                    sha,
                    stamp,
                    pr.get("updated_at"),
                    int(bool(pr.get("draft"))),
                    user.get("login"),
                    user.get("type"),
                    pr.get("author_association"),
                    (pr.get("base") or {}).get("ref"),
                    status,
                    reason,
                    stamp,
                    done["id"] if done else None,
                    done["completed_at"] if done else None,
                    pr.get("head_committed_at"),
                    stamp if reason is None else None,
                    pr.get("updated_at") if reason is None else None,
                    int(baseline),
                ),
            )
        conn.execute(
            "UPDATE pr_heads SET status='closed', ended_at=? "
            "WHERE repo=? AND is_open=0 AND status='queued' AND merged_before_seen=0",
            (stamp, repo),
        )
    _listed_repos.add(listing_key)
    return queued


def record_fast_merges(path, repo, prs, now=None):
    now = now or utcnow()
    stamp = now.isoformat()
    queued = []
    with connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for pr in prs:
            sha = (pr.get("head") or {}).get("sha") or ""
            merged_at = pr.get("merged_at")
            if not merged_at or pr.get("number") is None or skip_reason(pr):
                continue
            if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
                continue
            merged = db._parse_created_at(merged_at)
            if not now - timedelta(minutes=60) <= merged <= now + timedelta(minutes=5):
                continue
            user = pr.get("user") or {}
            # A merge must survive later open listings without disturbing a claim or backoff.
            conn.execute(
                "UPDATE pr_heads SET merged_before_seen=1, merged_at=? "
                "WHERE repo=? AND pr=? AND head_sha=? AND status IN ('queued','running')",
                (merged_at, repo, int(pr["number"]), sha),
            )
            added = conn.execute(
                """INSERT INTO pr_heads
                (repo,pr,head_sha,first_seen_at,pr_updated_at,draft,author,author_type,author_association,base_ref,
                 status,last_listed_at,eligible_at,eligible_pr_updated_at,is_open,baseline,
                 merged_before_seen,merged_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,'queued',?,?,?,0,0,1,?)
                ON CONFLICT(repo,pr,head_sha) DO UPDATE SET
                  status='queued', merged_before_seen=1, eligible_at=excluded.eligible_at,
                  eligible_pr_updated_at=excluded.eligible_pr_updated_at, ended_at=NULL, baseline=0,
                  merged_at=excluded.merged_at, author_association=excluded.author_association
                WHERE pr_heads.status='closed' AND pr_heads.merged_before_seen=0
                  AND NOT EXISTS(SELECT 1 FROM reviews r WHERE r.repo=pr_heads.repo
                    AND r.pr=pr_heads.pr AND r.head_sha=pr_heads.head_sha AND r.status='done')""",
                (repo, int(pr["number"]), sha, stamp, pr.get("updated_at"),
                 int(bool(pr.get("draft"))), user.get("login"), user.get("type"),
                 pr.get("author_association"),
                 (pr.get("base") or {}).get("ref"), stamp, stamp, merged_at, merged_at),
            ).rowcount
            if added:
                queued.append(pr["number"])
    for number in queued:
        log.info("watcher: queued fast merge %s#%s", repo, number)
    return len(queued)


def restrict_repos(path, repos, now=None):
    stamp = (now or utcnow()).isoformat()
    with connection(path) as conn:
        placeholders = ",".join("?" for _ in repos) or "''"
        conn.execute(
            f"UPDATE pr_heads SET is_open=0 WHERE repo NOT IN ({placeholders})",  # nosec B608 - placeholders only
            repos,
        )
        conn.execute(
            f"UPDATE pr_heads SET status='closed', ended_at=? WHERE repo NOT IN ({placeholders}) AND status='queued'",  # nosec B608 - placeholders only
            (stamp, *repos),
        )


def requeue_fast_merges(path, repos, now=None):
    now = now or utcnow()
    stamp = now.isoformat()
    queued = []
    with connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for repo in repos:
            # Both bounds must match record_fast_merges' clock-skew allowance.
            queued.extend(conn.execute(
                """UPDATE pr_heads SET status='queued', eligible_at=?, ended_at=NULL
                WHERE repo=? AND merged_before_seen=1 AND status='closed'
                  AND julianday(merged_at) BETWEEN julianday(?) AND julianday(?)
                  AND NOT EXISTS(SELECT 1 FROM reviews r WHERE r.repo=pr_heads.repo
                    AND r.pr=pr_heads.pr AND r.head_sha=pr_heads.head_sha AND r.status='done')
                RETURNING repo, pr, head_sha""",
                (stamp, repo, (now - timedelta(minutes=60)).isoformat(),
                 (now + timedelta(minutes=5)).isoformat()),
            ).fetchall())
    for row in queued:
        log.info("watcher: requeued fast merge %s#%s @ %s", row["repo"], row["pr"], row["head_sha"])
    return len(queued)


def claim(path, now=None, *, exclude=()):
    now = now or utcnow()
    with connection(path) as conn:
        # Queued work can outlive a deployment with a less restrictive policy.
        if trusted_only():
            trusted = sorted(TRUSTED_ASSOCIATIONS)
            marks = ",".join("?" for _ in trusted)
            skipped = conn.execute(
                f"UPDATE pr_heads SET status='skipped', skip_reason='untrusted author association' "
                f"WHERE status='queued' AND COALESCE(author_association,'') NOT IN ({marks}) "
                "RETURNING repo, pr",  # nosec B608 - placeholders only
                trusted,
            ).fetchall()
            for head in skipped:
                log.info("watcher: skipped %s#%s: untrusted author association", head["repo"], head["pr"])
        placeholders = ",".join("?" for _ in exclude) or "-1"
        row = conn.execute(
            f"""UPDATE pr_heads SET status='running', claimed_at=?
            WHERE id=(SELECT id FROM pr_heads WHERE status='queued'
                AND (is_open=1 OR merged_before_seen=1)
                AND (next_attempt_at IS NULL OR julianday(next_attempt_at)<=julianday(?))
                AND id NOT IN ({placeholders})
                ORDER BY baseline,
                  (MIN(julianday(first_seen_at),COALESCE(julianday(pr_updated_at),julianday(first_seen_at)))<julianday(?)),
                  MIN(julianday(first_seen_at),COALESCE(julianday(pr_updated_at),julianday(first_seen_at))),
                  id LIMIT 1)
            AND status='queued' RETURNING *""",  # nosec B608 - placeholders only
            (now.isoformat(), now.isoformat(), *exclude, (now - timedelta(minutes=30)).isoformat()),
        ).fetchone()
        return dict(row) if row else None


def _finish(conn, head, review_id, error, now):
    # The claim timestamp fences completions from an expired worker.
    current = conn.execute(
        "SELECT * FROM pr_heads WHERE id=? AND status='running' AND claimed_at=?",
        (head["id"], head["claimed_at"]),
    ).fetchone()
    if not current:
        return
    if isinstance(error, HeadMoved):
        conn.execute(
            "UPDATE pr_heads SET status='superseded', ended_at=?, next_attempt_at=NULL WHERE id=?",
            (now.isoformat(), head["id"]),
        )
        return
    category = classify_error(error) if error else None
    attempts = current["attempts"] + int(error is not None and category != "github_rate_limited")
    status, next_at = "done", None
    if error:
        status = "queued"
        if isinstance(error, GitHubRateLimited):
            next_at = datetime.fromtimestamp(error.reset_at, timezone.utc).isoformat()
        else:
            next_at = (now + timedelta(minutes=(2, 10, 30)[min(attempts - 1, 2)])).isoformat()
            if attempts >= env_int("ARGUS_WATCH_MAX_ATTEMPTS", 4):
                status = "parked"
                log.warning(
                    "watcher: parking %s#%s @ %s after %s failed attempts",
                    head["repo"],
                    head["pr"],
                    head["head_sha"][:12],
                    attempts,
                )
    conn.execute(
        """UPDATE pr_heads SET status=?, attempts=?, last_error_class=?, next_attempt_at=?,
        completed_at=?, review_id=COALESCE(?,review_id) WHERE id=?""",
        (
            status,
            attempts,
            category,
            next_at,
            now.isoformat() if not error else None,
            review_id,
            head["id"],
        ),
    )
    conn.execute(
        "INSERT INTO watch_attempts(head_id,review_id,finished_at,error_class) VALUES(?,?,?,?)",
        (head["id"], review_id, now.isoformat(), category),
    )


def finish(path, head, review_id, error=None, now=None):
    with connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _finish(conn, head, review_id, error, now or utcnow())


def close_claim(path, head, now=None):
    with connection(path) as conn:
        # An expired worker must not close a newer claim.
        conn.execute(
            "UPDATE pr_heads SET status='closed', ended_at=?, is_open=0, next_attempt_at=NULL "
            "WHERE id=? AND status='running' AND claimed_at=?",
            ((now or utcnow()).isoformat(), head["id"], head["claimed_at"]),
        )


def reclaim(path, *, startup=False, now=None):
    now = now or utcnow()
    cutoff = (now - timedelta(seconds=env_int("ARGUS_REVIEW_TIMEOUT_S", 1800))).isoformat()
    with connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT * FROM pr_heads WHERE status='running' "
            "AND (? OR julianday(claimed_at)<julianday(?))",
            (startup, cutoff),
        ).fetchall()
        for row in rows:
            review = conn.execute(
                "SELECT status FROM reviews WHERE id=?", (row["review_id"],)
            ).fetchone()
            if review and review["status"] == "done":
                _finish(conn, row, row["review_id"], None, now)
            else:
                _finish(conn, row, row["review_id"], InterruptedError("review interrupted"), now)
        return len(rows)


def queue_sizes(path):
    with connection(path) as conn:
        counts = dict(
            conn.execute("SELECT status,COUNT(*) FROM pr_heads GROUP BY status").fetchall()
        )
    return {s: counts.get(s, 0) for s in STATUSES}
