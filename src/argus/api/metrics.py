"""Read-only aggregation queries over the reviews/findings tables.

The console's overview, report, and benchmark screens need rollups the engine
never persists pre-aggregated. Kept out of db.py (writes) and main.py (HTTP)
so each layer stays single-purpose. Parameterized queries only.
"""

from datetime import datetime, timedelta, timezone

from . import db

SEVERITIES = ("critical", "major", "minor", "nit")
_DONE = db.STATUS_DONE


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def done_review_count(path: str | None = None) -> int:
    conn = db.connect(path)
    try:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM reviews WHERE status = ?", (_DONE,)
        ).fetchone()["n"]
    finally:
        conn.close()


def distinct_pr_count(path: str | None = None) -> int:
    """Distinct (repo, pr) pairs with at least one done review.

    The watcher re-reviews a PR on every push, so done-review COUNT overstates how
    many PRs were actually reviewed (146 done reviews were 87 distinct PRs). This
    is the honest 'PRs reviewed' number for the overview.
    """
    conn = db.connect(path)
    try:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM (SELECT DISTINCT repo, pr FROM reviews WHERE status = ?)",
            (_DONE,),
        ).fetchone()["n"]
    finally:
        conn.close()


def mark_precision(path: str | None = None, *, include_auto: bool = False) -> dict:
    """Operator-label rollup over findings: the only ground-truth precision number.

    Uses the latest mark per finding (newest id wins). 'located' findings are the
    judged ones (real or fp); precision = real / located. stale/cant_locate/unsure
    are reported but excluded from the denominator, since they are not a verdict on
    correctness. Zero labels -> precision None (nothing to claim yet).

    Machine marks (namespaced markers: automark:* from the nightly labeller,
    agent:* from MCP callers) are excluded by default so this stays human ground
    truth; a bare marker is a human identity and counts. The latest human mark per
    finding is used; include_auto=True folds machine marks in for a combined view.
    The filter is applied before picking the latest, so a human re-label after a
    machine mark still wins.
    """
    # Machine markers are namespaced (automark:*, agent:*); a bare marker is a
    # human identity (e.g. 'reviewer'). The colon is the namespace convention, so
    # any future machine lane is excluded without another filter change.
    auto_filter = "" if include_auto else (
        "WHERE marker IS NULL OR marker NOT LIKE '%:%' ")
    conn = db.connect(path)
    try:
        rows = conn.execute(
            "SELECT m.mark FROM marks m JOIN ("
            f"  SELECT finding_id, MAX(id) AS max_id FROM marks {auto_filter}"  # nosec B608 - constant filter
            "  GROUP BY finding_id"
            ") latest ON latest.max_id = m.id"
        ).fetchall()
    finally:
        conn.close()
    tally = {v: 0 for v in db.MARK_VALUES}
    for row in rows:
        if row["mark"] in tally:
            tally[row["mark"]] += 1
    located = tally["real"] + tally["fp"]
    labeled = sum(tally.values())
    precision = round(tally["real"] / located, 3) if located else None
    return {"labeled": labeled, "located": located, "precision": precision, **tally}


def verified_severity_counts(path: str | None = None) -> dict[str, int]:
    """Confirmed findings (verdict_is_real=1) grouped by severity, zero-filled."""
    conn = db.connect(path)
    try:
        rows = conn.execute(
            "SELECT severity, COUNT(*) AS n FROM findings "
            "WHERE verdict_is_real = 1 GROUP BY severity"
        ).fetchall()
    finally:
        conn.close()
    counts = {s: 0 for s in SEVERITIES}
    for row in rows:
        if row["severity"] in counts:
            counts[row["severity"]] = row["n"]
    return counts


def verified_bug_split(path: str | None = None) -> dict[str, int]:
    """is_bug split among confirmed findings -> {"bug": n, "nit": n}."""
    conn = db.connect(path)
    try:
        row = conn.execute(
            "SELECT "
            "SUM(CASE WHEN is_bug = 1 THEN 1 ELSE 0 END) AS bug, "
            "SUM(CASE WHEN is_bug = 0 THEN 1 ELSE 0 END) AS nit "
            "FROM findings WHERE verdict_is_real = 1"
        ).fetchone()
    finally:
        conn.close()
    return {"bug": row["bug"] or 0, "nit": row["nit"] or 0}


def verified_finding_count(path: str | None = None) -> int:
    conn = db.connect(path)
    try:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM findings WHERE verdict_is_real = 1"
        ).fetchone()["n"]
    finally:
        conn.close()


def review_status_counts(path: str | None = None) -> dict[str, int]:
    """reviews grouped by status -> {"done","error","running"}, zero-filled.

    Feeds the bake-off gate: done is the live-PR sample, error drives crash-free.
    """
    conn = db.connect(path)
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM reviews GROUP BY status"
        ).fetchall()
    finally:
        conn.close()
    counts = {db.STATUS_DONE: 0, db.STATUS_ERROR: 0, db.STATUS_RUNNING: 0}
    for row in rows:
        if row["status"] in counts:
            counts[row["status"]] = row["n"]
    return {
        "done": counts[db.STATUS_DONE],
        "error": counts[db.STATUS_ERROR],
        "running": counts[db.STATUS_RUNNING],
    }


def reviews_window(hours: int, path: str | None = None) -> dict[str, int]:
    """done/error review counts over the last `hours` (for /stats.ops + health)."""
    since = _iso(datetime.now(timezone.utc) - timedelta(hours=hours))
    conn = db.connect(path)
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM reviews WHERE created_at >= ? "
            "GROUP BY status", (since,)
        ).fetchall()
    finally:
        conn.close()
    out = {"done": 0, "error": 0}
    for row in rows:
        if row["status"] in out:
            out[row["status"]] = row["n"]
    return out


def top_error_class(hours: int, path: str | None = None) -> str | None:
    """Most common error_class among errored reviews in the window, or None."""
    since = _iso(datetime.now(timezone.utc) - timedelta(hours=hours))
    conn = db.connect(path)
    try:
        row = conn.execute(
            "SELECT COALESCE(error_class, 'unknown') AS c, COUNT(*) AS n FROM reviews "
            "WHERE status = ? AND created_at >= ? GROUP BY c ORDER BY n DESC LIMIT 1",
            (db.STATUS_ERROR, since),
        ).fetchone()
    finally:
        conn.close()
    return row["c"] if row else None


def review_health(hours: int = 6, path: str | None = None) -> str:
    """"degraded" when the recent review error-rate is high, else "ok". Same
    threshold the watchdog pages on: >=5 errors and >=50% of the window."""
    window = reviews_window(hours, path)
    total = window["done"] + window["error"]
    if window["error"] >= 5 and total > 0 and (window["error"] * 100 // total) >= 50:
        return "degraded"
    return "ok"


def first_review_at(path: str | None = None) -> str | None:
    """created_at of the earliest review -- the bake-off clock's day 0 -- or None.

    Rows are minted with one canonical tz-aware format, so ascending lexical order
    is chronological here.
    """
    conn = db.connect(path)
    try:
        row = conn.execute(
            "SELECT created_at FROM reviews ORDER BY created_at ASC, id ASC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return row["created_at"] if row else None


def last_error_at(path: str | None = None) -> str | None:
    """created_at of the most recent errored review (drives crash-free days), or None.

    Chronological max by parsed instant, not lexical, so a row written in another
    offset cannot shorten the crash-free window.
    """
    conn = db.connect(path)
    try:
        rows = conn.execute(
            "SELECT created_at FROM reviews WHERE status = ?", (db.STATUS_ERROR,)
        ).fetchall()
    finally:
        conn.close()
    stamps = [row["created_at"] for row in rows if row["created_at"]]
    if not stamps:
        return None
    return max(stamps, key=lambda s: _parse(s) or datetime.min.replace(tzinfo=timezone.utc))


def error_reason_counts(path: str | None = None) -> list[dict]:
    """Errored reviews grouped by exception type, most frequent first.

    Turns the crash-free floor from a bare count into a diagnosis: transient
    backend faults (timeout / overload) read differently from a code bug. Rows
    that predate error capture carry a NULL reason, bucketed as 'unrecorded'.
    """
    conn = db.connect(path)
    try:
        rows = conn.execute(
            "SELECT error FROM reviews WHERE status = ?", (db.STATUS_ERROR,)
        ).fetchall()
    finally:
        conn.close()
    tally: dict[str, int] = {}
    for row in rows:
        reason = row["error"] or ""
        # The stored reason is "ExceptionType: message"; the type alone is the
        # useful bucket. Empty -> a pre-capture row.
        kind = reason.split(":", 1)[0].strip() if reason else "unrecorded"
        tally[kind] = tally.get(kind, 0) + 1
    return [
        {"kind": kind, "count": count}
        for kind, count in sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def review_repo_counts(path: str | None = None) -> list[dict]:
    """All-status review counts per repo, busiest first -> [{"repo","count"}].

    Surfaces the bake-off's repo skew so a one-repo-dominated sample is visible.
    """
    conn = db.connect(path)
    try:
        rows = conn.execute(
            "SELECT repo, COUNT(*) AS n FROM reviews GROUP BY repo ORDER BY n DESC, repo"
        ).fetchall()
    finally:
        conn.close()
    return [{"repo": row["repo"], "count": row["n"]} for row in rows]


def daily_buckets(column_table: str, days: int, path: str | None = None) -> list[int]:
    """Per-day counts for the last `days` days, oldest first, zero-filled.

    column_table is "reviews" (one bucket per row) or "findings" (joined to the
    parent review's created_at, since findings carry no timestamp of their own).
    """
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if column_table == "reviews":
        sql = "SELECT created_at FROM reviews WHERE created_at >= ?"
    else:
        sql = (
            "SELECT r.created_at FROM findings f "
            "JOIN reviews r ON r.id = f.review_id WHERE r.created_at >= ?"
        )
    conn = db.connect(path)
    try:
        rows = conn.execute(sql, (_iso(start),)).fetchall()
    finally:
        conn.close()

    buckets = [0] * days
    for row in rows:
        ts = _parse(row["created_at"])
        if ts is None:
            continue
        idx = (ts - start).days
        if 0 <= idx < days:
            buckets[idx] += 1
    return buckets


def window_rollup(period_days: int, path: str | None = None) -> dict:
    """Reviews/findings/severity/topRepos/volume for done reviews in the window."""
    now = datetime.now(timezone.utc)
    start = _iso(now - timedelta(days=period_days))
    conn = db.connect(path)
    try:
        review_rows = conn.execute(
            "SELECT id, repo, created_at FROM reviews "
            "WHERE status = ? AND created_at >= ?",
            (_DONE, start),
        ).fetchall()
        ids = [r["id"] for r in review_rows]
        sev_counts = {s: 0 for s in SEVERITIES}
        findings_total = 0
        if ids:
            placeholders = ",".join("?" * len(ids))
            # placeholders is only "?,?,..."; the ids are bound as query
            # parameters, so this is not user-controlled SQL (bandit B608 FP).
            for row in conn.execute(
                f"SELECT severity, COUNT(*) AS n FROM findings "  # nosec B608
                f"WHERE review_id IN ({placeholders}) GROUP BY severity",
                ids,
            ).fetchall():
                if row["severity"] in sev_counts:
                    sev_counts[row["severity"]] = row["n"]
                findings_total += row["n"]
    finally:
        conn.close()

    repo_counts: dict[str, int] = {}
    for r in review_rows:
        repo_counts[r["repo"]] = repo_counts.get(r["repo"], 0) + 1
    top = sorted(repo_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]

    return {
        "reviews": len(review_rows),
        "findings": findings_total,
        "severity_counts": sev_counts,
        "top_repos": [{"repo": name, "value": n} for name, n in top],
        "review_timestamps": [r["created_at"] for r in review_rows],
        "window_start": start,
    }


def _parse(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
