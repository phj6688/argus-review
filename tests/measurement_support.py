"""Helpers that freeze the clock and run the measurement readers on a database copy."""

import contextlib
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


class ParityError(Exception):
    """A usage problem that stops the run before it reads or writes anything."""


def _parse_stamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)



def _mask(name):
    return "id:" + hashlib.sha256(str(name).encode()).hexdigest()[:10]



def open_readonly(path):
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    # Second guard: even a statement that slipped through cannot write.
    conn.execute("PRAGMA query_only = ON")
    return conn




def _frozen_datetime(moment):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

        @classmethod
        def utcnow(cls):
            return moment.replace(tzinfo=None)

    return Frozen


@contextlib.contextmanager
def frozen_clock(moment):
    """Make every `datetime.now()` in the measurement modules return MOMENT."""
    from argus import acceptance
    from argus.api import db, metrics, watch_metrics, watch_store
    from argus.evalkit import labels, monthly

    frozen = _frozen_datetime(moment)
    with contextlib.ExitStack() as stack:
        for module in (acceptance, db, metrics, watch_metrics, watch_store, labels, monthly):
            if getattr(module, "datetime", None) is datetime:
                stack.enter_context(mock.patch.object(module, "datetime", frozen))
        stack.enter_context(mock.patch.object(watch_store, "utcnow", lambda: moment))
        yield


def _mask_keys(mapping):
    return {_mask(k): v for k, v in sorted(mapping.items())}


def measure(db_path, since, until, month):
    from argus import acceptance
    from argus.api import db, metrics, watch_metrics
    from argus.evalkit import labels, monthly

    start, end = _parse_stamp(since), _parse_stamp(until)
    if start >= end:
        raise ParityError("--until must be after --since")

    def readonly_connect(path=None):
        return open_readonly(path or db_path)

    out = {}

    def guarded(build):
        # A schema break can crash a measurement query. Record that as the value, so
        # the break shows up in compare instead of capture stopping without a fingerprint.
        try:
            return build()
        except sqlite3.Error as error:
            return {"error": f"{type(error).__name__}: {error}"}

    def section(name, build):
        out[name] = guarded(build)

    def acceptance_section():
        with contextlib.closing(open_readonly(db_path)) as conn:
            report = acceptance.report(conn, since)
        report["by_repo"] = _mask_keys(report["by_repo"])
        report["final"]["by_repo"] = _mask_keys(report["final"]["by_repo"])
        report["bot_logins"] = _mask_keys(report["bot_logins"])
        return report

    def coverage_section():
        with contextlib.closing(open_readonly(db_path)) as conn:
            return {
                "window": watch_metrics.coverage(conn, start, end, as_of=end),
                "last_24h": watch_metrics.coverage(conn, end - timedelta(hours=24), end, as_of=end),
                "last_48h": watch_metrics.coverage(conn, end - timedelta(hours=48), end, as_of=end),
            }

    def labelled_section():
        counts = {}
        with contextlib.closing(open_readonly(db_path)) as conn:
            for finding, label, loose in labels.labelled_findings(conn, start.isoformat(), end.isoformat()):
                key = f"{finding['placement']}:{label}:{int(bool(loose))}"
                counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items()))

    def metrics_section():
        calls = {
            "distinct_prs": lambda: metrics.distinct_pr_count(db_path),
            "done_reviews": lambda: metrics.done_review_count(db_path),
            "mark_precision_human": lambda: metrics.mark_precision(db_path),
            "mark_precision_all": lambda: metrics.mark_precision(db_path, include_auto=True),
            "verified_severity": lambda: metrics.verified_severity_counts(db_path),
            "verified_bug_split": lambda: metrics.verified_bug_split(db_path),
            "verified_findings": lambda: metrics.verified_finding_count(db_path),
            "status_counts": lambda: metrics.review_status_counts(db_path),
            "window_6h": lambda: metrics.reviews_window(6, db_path),
            "top_error_class_24h": lambda: metrics.top_error_class(24, db_path),
            "first_review_at": lambda: metrics.first_review_at(db_path),
            "last_error_at": lambda: metrics.last_error_at(db_path),
            "error_reasons": lambda: metrics.error_reason_counts(db_path),
            "repo_counts": lambda: [{"repo": _mask(r["repo"]), "count": r["count"]}
                                    for r in metrics.review_repo_counts(db_path)],
            "daily_reviews_7d": lambda: metrics.daily_buckets("reviews", 7, db_path),
            "daily_findings_7d": lambda: metrics.daily_buckets("findings", 7, db_path),
            "rollup_7d": lambda: _rollup(metrics.window_rollup(7, db_path)),
        }
        return {name: guarded(call) for name, call in calls.items()}

    def monthly_section():
        report = monthly.build(db_path, {}, month)
        return {"month": report["month"], "date": report["date"],
                "rows": report["rows"], "gate": report["gate"]}

    with frozen_clock(end), mock.patch.object(db, "connect", readonly_connect):
        section("acceptance", acceptance_section)
        section("coverage", coverage_section)
        section("labelled_findings", labelled_section)
        section("metrics", metrics_section)
        section("monthly", monthly_section)
    return json.loads(json.dumps(out, default=str))


def _rollup(rollup):
    rollup = dict(rollup)
    rollup["top_repos"] = [{"repo": _mask(r["repo"]), "value": r["value"]} for r in rollup["top_repos"]]
    rollup["review_timestamps"] = [hashlib.sha256("".join(rollup["review_timestamps"]).encode()).hexdigest(),
                                   len(rollup["review_timestamps"])]
    return rollup


# ---- capture ---------------------------------------------------------------

