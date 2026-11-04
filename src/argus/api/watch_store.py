"""Durable head queue and attempt history."""

import logging
import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from .. import github
from ..errors import HeadMoved, classify_error
from ..forge.errors import ForgeRateLimited
from ..forge.keys import github_key_sql, gitlab_key_sql, is_gitlab_key
from ..forge.trust_sql import trusted_sql
from ..gitlab import client as gitlab_client
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


def skip_reason(pr, repo=None):
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
    if trusted_only() and repo is not None and is_gitlab_key(repo):
        if pr.get("author_trusted") is None:
            return "trust_unknown"
        if pr.get("author_trusted") != 1:
            return "untrusted author (gitlab membership)"
    elif trusted_only() and pr.get("author_association") not in TRUSTED_ASSOCIATIONS:
        reason = f"untrusted author association: {pr.get('author_association') or 'missing'}"
        log.info("watcher: skipped PR #%s: %s", pr.get("number"), reason)
        return reason
    return None


def heads(path):
    with connection(path) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM pr_heads ORDER BY id")]


def _record_listed_head(conn, repo, pr, now, stamp, first_listing):
    queued = 0
    sha = (pr.get("head") or {}).get("sha")
    if not sha or pr.get("number") is None:
        return 0
    number, reason = int(pr["number"]), skip_reason(pr, repo)
    user = pr.get("user") or {}
    gitlab = is_gitlab_key(repo)
    columns = ",author_trusted,author_id" if gitlab else ""
    values = ",?,?" if gitlab else ""
    update = ",author_trusted=excluded.author_trusted,author_id=excluded.author_id" if gitlab else ""
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
        f"""INSERT INTO pr_heads
        (repo,pr,head_sha,first_seen_at,pr_updated_at,draft,author,author_type,author_association,base_ref,
         status,skip_reason,last_listed_at,review_id,completed_at,head_committed_at,
         eligible_at,eligible_pr_updated_at,baseline{columns})
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?{values})
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
          head_committed_at=COALESCE(excluded.head_committed_at,pr_heads.head_committed_at){update}""",  # nosec B608 - fixed column fragments
        (
            repo,
            number,
            sha,
            stamp,
            pr.get("updated_at"),
            int(bool(pr.get("draft"))),
            user.get("login"),
            user.get("type"),
            None if gitlab else pr.get("author_association"),
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
        ) + ((pr.get("author_trusted"), pr.get("author_id")) if gitlab else ()),
    )
    return queued


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
            queued += _record_listed_head(conn, repo, pr, now, stamp, first_listing)
        conn.execute(
            "UPDATE pr_heads SET status='closed', ended_at=? "
            "WHERE repo=? AND is_open=0 AND status='queued' AND merged_before_seen=0",
            (stamp, repo),
        )
    _listed_repos.add(listing_key)
    return queued


def _record_fast_merge(conn, repo, pr, now, stamp):
    sha = (pr.get("head") or {}).get("sha") or ""
    merged_at = pr.get("merged_at")
    if not merged_at or pr.get("number") is None:
        return 0
    if skip_reason(pr, repo):
        if is_gitlab_key(repo):
            # A rejected refresh must revoke stale trust before work can be claimed.
            conn.execute(
                "UPDATE pr_heads SET author_trusted=?, author_id=? "
                "WHERE repo=? AND pr=? AND head_sha=? AND status='queued'",
                (pr.get("author_trusted"), pr.get("author_id"), repo, int(pr["number"]), sha),
            )
        return 0
    if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        return 0
    merged = db._parse_created_at(merged_at)
    if not now - timedelta(minutes=60) <= merged <= now + timedelta(minutes=5):
        return 0
    user = pr.get("user") or {}
    gitlab = is_gitlab_key(repo)
    columns = ",author_trusted,author_id" if gitlab else ""
    values = ",?,?" if gitlab else ""
    update = ",author_trusted=excluded.author_trusted,author_id=excluded.author_id" if gitlab else ""
    # A merge must survive later open listings without disturbing a claim or backoff.
    conn.execute(
        "UPDATE pr_heads SET merged_before_seen=1, merged_at=? "
        "WHERE repo=? AND pr=? AND head_sha=? AND status IN ('queued','running')",
        (merged_at, repo, int(pr["number"]), sha),
    )
    added = conn.execute(
        f"""INSERT INTO pr_heads
        (repo,pr,head_sha,first_seen_at,pr_updated_at,draft,author,author_type,author_association,base_ref,
         status,last_listed_at,eligible_at,eligible_pr_updated_at,is_open,baseline,
         merged_before_seen,merged_at{columns})
        VALUES(?,?,?,?,?,?,?,?,?,?,'queued',?,?,?,0,0,1,?{values})
        ON CONFLICT(repo,pr,head_sha) DO UPDATE SET
          status='queued', merged_before_seen=1, eligible_at=excluded.eligible_at,
          eligible_pr_updated_at=excluded.eligible_pr_updated_at, ended_at=NULL, baseline=0,
          merged_at=excluded.merged_at, author_association=excluded.author_association{update}
        WHERE pr_heads.status='closed' AND pr_heads.merged_before_seen=0
          AND NOT EXISTS(SELECT 1 FROM reviews r WHERE r.repo=pr_heads.repo
            AND r.pr=pr_heads.pr AND r.head_sha=pr_heads.head_sha AND r.status='done')""",  # nosec B608 - fixed column fragments
        (repo, int(pr["number"]), sha, stamp, pr.get("updated_at"),
         int(bool(pr.get("draft"))), user.get("login"), user.get("type"),
         None if gitlab else pr.get("author_association"),
         (pr.get("base") or {}).get("ref"), stamp, stamp, merged_at, merged_at)
        + ((pr.get("author_trusted"), pr.get("author_id")) if gitlab else ()),
    ).rowcount
    return added


def record_fast_merges(path, repo, prs, now=None):
    now = now or utcnow()
    stamp = now.isoformat()
    queued = []
    with connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for pr in prs:
            if _record_fast_merge(conn, repo, pr, now, stamp):
                queued.append(pr["number"])
    for number in queued:
        log.info("watcher: queued fast merge %s#%s", repo, number)
    return len(queued)


def record_changes(path, repo, opened, closed, complete, now=None):
    """Record a full listing or only changed MRs, without closing unlisted MRs.

    GitLab dicts use the PR shape: number (iid), head.sha, updated_at (ISO),
    draft (bool), user.login/type, base.ref, author_trusted (1/0/None),
    author_id (int/None), merged_at (ISO/None), optional head_committed_at.
    GitLab dicts omit author_association. Closed entries with merged_at use
    the fast-merge path; other closed entries affect incremental listings only.
    """
    now = now or utcnow()
    stamp = now.isoformat()
    queued = record_listing(path, repo, opened, now) if complete else 0
    listing_key = (os.path.realpath(path or db.db_path()), repo.casefold())
    first_listing = listing_key not in _listed_repos
    closed = list(closed)
    fast_merges = []
    with connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not complete:
            for pr in opened:
                sha = (pr.get("head") or {}).get("sha")
                if not sha or pr.get("number") is None:
                    continue
                conn.execute(
                    "UPDATE pr_heads SET is_open=0 WHERE repo=? AND pr=? AND head_sha<>?",
                    (repo, int(pr["number"]), sha),
                )
                queued += _record_listed_head(conn, repo, pr, now, stamp, first_listing)
            for pr in closed:
                if pr.get("number") is None:
                    continue
                conn.execute("UPDATE pr_heads SET is_open=0 WHERE repo=? AND pr=?",
                             (repo, int(pr["number"])))
                conn.execute(
                    "UPDATE pr_heads SET status='closed', ended_at=? WHERE repo=? AND pr=? "
                    "AND status='queued' AND merged_before_seen=0", (stamp, repo, int(pr["number"])),
                )
        for pr in closed:
            if _record_fast_merge(conn, repo, pr, now, stamp):
                fast_merges.append(pr["number"])
    _listed_repos.add(listing_key)
    for number in fast_merges:
        log.info("watcher: queued fast merge %s#%s", repo, number)
    return queued + len(fast_merges)


def open_head_shas(path, repo):
    with connection(path) as conn:
        return {r["pr"]: r["head_sha"] for r in conn.execute(
            "SELECT pr, head_sha FROM pr_heads "
            "WHERE repo=? AND is_open=1 AND status<>'superseded' "
            "ORDER BY julianday(COALESCE(last_listed_at,first_seen_at)), id", (repo,),
        )}


def poll_window_start(updated_after, overlap_seconds=300):
    # A push may change the head sha without moving updated_at.
    if updated_after is None:
        return None
    return (db._parse_created_at(updated_after) - timedelta(seconds=overlap_seconds)).isoformat()


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


def claim(path, now=None, *, exclude=(), pauses=None):
    now = now or utcnow()
    if pauses is None:
        gitlab_pause = 0
        try:
            client = gitlab_client.client()
            gitlab_pause = client.pause_until() if client else 0
        except Exception:
            log.debug("watcher: GitLab pause lookup failed", exc_info=True)
        pauses = {"github": github.pause_until(), "gitlab": gitlab_pause}
    pause_filter = ""
    if pauses.get("github", 0) > now.timestamp():
        pause_filter += f" AND NOT ({github_key_sql('repo')})"
    if pauses.get("gitlab", 0) > now.timestamp():
        pause_filter += f" AND NOT ({gitlab_key_sql('repo')})"
    with connection(path) as conn:
        # Queued work can outlive a deployment with a less restrictive policy.
        if trusted_only():
            skipped = conn.execute(
                "UPDATE pr_heads SET status='skipped', skip_reason=CASE "
                f"WHEN {gitlab_key_sql('repo')} THEN CASE WHEN author_trusted IS NULL "
                "THEN 'trust_unknown' ELSE 'untrusted author (gitlab membership)' END "
                "ELSE 'untrusted author association' END "
                f"WHERE status='queued' AND NOT ({trusted_sql()}) "
                "RETURNING repo, pr, skip_reason",  # nosec B608 - shared predicate
            ).fetchall()
            for head in skipped:
                log.info("watcher: skipped %s#%s: %s", head["repo"], head["pr"], head["skip_reason"])
        placeholders = ",".join("?" for _ in exclude) or "-1"
        row = conn.execute(
            f"""UPDATE pr_heads SET status='running', claimed_at=?
            WHERE id=(SELECT id FROM pr_heads WHERE status='queued'
                AND (is_open=1 OR merged_before_seen=1)
                AND (next_attempt_at IS NULL OR julianday(next_attempt_at)<=julianday(?))
                AND id NOT IN ({placeholders}){pause_filter}
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
    attempts = current["attempts"] + int(
        error is not None and category not in {"github_rate_limited", "gitlab_rate_limited"}
    )
    status, next_at = "done", None
    if error:
        status = "queued"
        if isinstance(error, ForgeRateLimited):
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


def unknown_trust_heads(path, limit):
    if limit < 1:
        return []
    with connection(path) as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM pr_heads WHERE is_open=1 AND status='skipped' "
            f"AND skip_reason='trust_unknown' AND {gitlab_key_sql('repo')} "
            "ORDER BY julianday(COALESCE(last_listed_at, first_seen_at)), id LIMIT ?",  # nosec B608 - shared predicate
            (limit,),
        )]


def apply_trust(path, head, trusted, now=None):
    now = now or utcnow()
    with connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT * FROM pr_heads WHERE id=?", (head["id"],)).fetchone()
        if (current is None or not current["is_open"] or current["status"] != "skipped"
                or current["skip_reason"] != "trust_unknown" or current["head_sha"] != head["head_sha"]):
            return None
        head = current
        pr = {
            "number": head["pr"], "head": {"sha": head["head_sha"]},
            "updated_at": head["pr_updated_at"], "draft": head["draft"],
            "user": {"login": head["author"] or "", "type": head["author_type"]},
            "base": {"ref": head["base_ref"]}, "author_id": head["author_id"],
            "head_committed_at": head["head_committed_at"], "author_trusted": 1 if trusted else 0,
        }
        _record_listed_head(conn, head["repo"], pr, now, now.isoformat(), False)
        return conn.execute("SELECT status FROM pr_heads WHERE id=?", (head["id"],)).fetchone()[0]


def unknown_trust_count(path):
    with connection(path) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM pr_heads WHERE is_open=1 AND status='skipped' "
            f"AND skip_reason='trust_unknown' AND {gitlab_key_sql('repo')}",  # nosec B608 - shared predicate
        ).fetchone()[0]


def defer_unknown_trust(path, head_id, now=None):
    with connection(path) as conn:
        conn.execute(
            "UPDATE pr_heads SET last_listed_at=? WHERE id=? AND is_open=1 "
            "AND status='skipped' AND skip_reason='trust_unknown'",
            ((now or utcnow()).isoformat(), head_id),
        )
