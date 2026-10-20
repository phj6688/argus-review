"""PR head coverage, independent of per-review lane coverage."""

import json
import logging
import math
from datetime import timedelta
from statistics import median

from . import db, watch_store

log = logging.getLogger("argus.watcher")


def _percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def _latency(values, denominator):
    return {
        "median": median(values) if values else None,
        "p90": _percentile(values, 0.9),
        "within_15_minutes": sum(v <= 900 for v in values),
        "share_within_15_minutes": sum(v <= 900 for v in values) / denominator
        if denominator
        else None,
    }


def _completed_at(row):
    # Parse the stored text, not julianday(): SQLite rounds julianday() to the millisecond,
    # which moved a review at exactly 900 s to 900.001 s. The earliest is taken here because
    # two completions in one millisecond tie in SQL, and text order is not chronological
    # for every timestamp format the parser accepts.
    if not row["done_at"]:
        return None
    return min(db._parse_created_at(text) for text in row["done_at"].split(","))


def _seconds_between(earlier, later):
    return max(0, round((later - earlier).total_seconds(), 3))


def coverage(conn, start, end, *, as_of=None):
    as_of = as_of or end
    rows = [
        dict(r)
        for r in conn.execute(
            """SELECT h.*,
        EXISTS(SELECT 1 FROM reviews r WHERE r.repo=h.repo AND r.pr=h.pr AND r.head_sha=h.head_sha
               AND r.status='done' AND julianday(COALESCE(r.completed_at,r.created_at))<=julianday(?)) has_done,
        (SELECT group_concat(r.completed_at, ',') FROM reviews r WHERE r.repo=h.repo AND r.pr=h.pr
         AND r.head_sha=h.head_sha AND r.status='done' AND julianday(r.completed_at)<=julianday(?)) done_at
        FROM pr_heads h WHERE
        (julianday(eligible_at)>=julianday(?) AND julianday(eligible_at)<julianday(?)) OR
        (julianday(first_seen_at)>=julianday(?) AND julianday(first_seen_at)<julianday(?))""",
            (as_of.isoformat(), as_of.isoformat(), start.isoformat(), end.isoformat(),
             start.isoformat(), end.isoformat()),
        )
    ]
    candidates = [
        r for r in rows
        if not r["baseline"] and r["eligible_at"] and start <= db._parse_created_at(r["eligible_at"]) < end
    ]
    fast_merged = [
        r for r in rows if r["merged_before_seen"] and r["eligible_at"]
        and start <= db._parse_created_at(r["eligible_at"]) < end
    ]
    eligible, strict, strict_latencies = [], [], []
    excluded = pending = 0
    for row in candidates:
        first = db._parse_created_at(row["eligible_at"])
        pushed = (
            min(first, db._parse_created_at(row["eligible_pr_updated_at"]))
            if row["eligible_pr_updated_at"] else first
        )
        if not row["has_done"] and pushed + timedelta(seconds=900) > as_of:
            pending += 1
            continue
        strict.append(row)
        completed = _completed_at(row)
        latency = _seconds_between(pushed, completed) if completed else None
        if latency is not None:
            strict_latencies.append(latency)
        ended = db._parse_created_at(row["ended_at"]) if row["ended_at"] else None
        # A head replaced before the deadline gave the worker no fair opportunity.
        if (
            row["status"] in {"superseded", "closed"}
            and ended is not None and ended <= as_of
            and (ended - pushed).total_seconds() <= 900
            and (latency is None or latency > 900)
        ):
            excluded += 1
            continue
        eligible.append((row, completed, latency))
    latencies, commits = [], []
    done_count = 0
    commit_available = 0
    for row, completed, latency in eligible:
        commit_available += bool(row["head_committed_at"])
        done_count += row["has_done"]
        if latency is None:
            continue
        latencies.append(latency)
        if row["head_committed_at"]:
            commits.append(_seconds_between(db._parse_created_at(row["head_committed_at"]), completed))
    listed = [r for r in rows if start <= db._parse_created_at(r["first_seen_at"]) < end]
    attempts = conn.execute(
        """SELECT error_class, COUNT(*) n FROM watch_attempts
        WHERE julianday(finished_at)>=julianday(?) AND julianday(finished_at)<julianday(?)
        GROUP BY error_class""",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    errors = {r["error_class"]: r["n"] for r in attempts if r["error_class"]}
    total = sum(r["n"] for r in attempts)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "as_of": as_of.isoformat(),
        "pending_heads": pending,
        "baseline_heads": sum(bool(r["baseline"]) for r in rows),
        "eligible_heads": len(eligible),
        "done_heads": done_count,
        "coverage_share": done_count / len(eligible) if eligible else None,
        "push_to_review_seconds": _latency(latencies, len(eligible)),
        "commit_to_review_seconds": {
            **_latency(commits, commit_available),
            "available_heads": commit_available,
        },
        "excluded_ended_within_15_minutes": excluded,
        "strict": {
            "eligible_heads": len(strict),
            "done_heads": sum(r["has_done"] for r in strict),
            "coverage_share": sum(r["has_done"] for r in strict) / len(strict) if strict else None,
            "share_within_15_minutes": _latency(strict_latencies, len(strict))["share_within_15_minutes"],
        },
        "fast_merged": {
            "seen": len(fast_merged),
            "done": sum(r["has_done"] for r in fast_merged),
            "done_within_15_minutes_of_merge": sum(
                r["done_at"] is not None and r["eligible_pr_updated_at"] is not None
                and round(
                    (_completed_at(r) - db._parse_created_at(r["eligible_pr_updated_at"])).total_seconds(), 3
                ) <= 900
                for r in fast_merged
            ),
        },
        "finished_attempts": total,
        "errored_attempts": sum(errors.values()),
        "error_share": sum(errors.values()) / total if total else None,
        "errors_by_class": errors,
        "superseded": sum(r["status"] == "superseded" for r in listed),
        "skipped": sum(r["skip_reason"] is not None for r in listed),
    }


def stats(path, now=None):
    now = now or watch_store.utcnow()
    with watch_store.connection(path) as conn:
        result = {
            f"{hours}h": coverage(conn, now - timedelta(hours=hours), now, as_of=now) for hours in (24, 48)
        }
        opened = conn.execute("""SELECT COUNT(*) total, COALESCE(SUM(EXISTS(
            SELECT 1 FROM reviews r WHERE r.repo=h.repo AND r.pr=h.pr AND r.head_sha=h.head_sha
            AND r.status='done')),0) done FROM pr_heads h WHERE is_open=1 AND skip_reason IS NULL""").fetchone()
        result["open_eligible_heads"] = opened["total"]
        result["open_done_heads"] = opened["done"]
        result["daily_reports"] = [
            json.loads(r[0])
            for r in conn.execute(
                "SELECT report_json FROM coverage_reports ORDER BY day DESC LIMIT 7"
            )
        ]
    return result


def daily_report(path, now=None):
    now = now or watch_store.utcnow()
    if now.hour == 0 and now.minute < 20:
        return
    end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=1)
    day = start.date().isoformat()
    with watch_store.connection(path) as conn:
        if conn.execute("SELECT 1 FROM coverage_reports WHERE day=?", (day,)).fetchone():
            return
        report = {"day": day, **coverage(conn, start, end, as_of=end + timedelta(minutes=15))}
        added = conn.execute(
            "INSERT OR IGNORE INTO coverage_reports VALUES(?,?,?)",
            (day, now.isoformat(), json.dumps(report)),
        ).rowcount
    if added:
        log.info(
            "coverage: day=%s eligible=%d done=%d within_15m=%s error_share=%s",
            day,
            report["eligible_heads"],
            report["done_heads"],
            report["push_to_review_seconds"]["share_within_15_minutes"],
            report["error_share"],
        )
