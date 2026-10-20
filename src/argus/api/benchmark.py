"""Benchmark rows: Argus vs CodeRabbit on the persisted review-thread corpus.

CodeRabbit-side volume/action numbers come from data/cr_threads.jsonl (its own
persisted threads: a precision oracle of what it surfaced, not ground truth).
Argus-side numbers come from the live reviews/findings DB where derivable.
Catch-rate and false-positive-rate stay pending until the injected-bug and
verified-finding corpora are scored locally.
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("argus.api.benchmark")

ENV_BASELINES_PATH = "ARGUS_BENCHMARK_BASELINES_PATH"

DASH = "—"  # em dash; the console renders this literal as "no data yet"

# Documented baselines used only when the corpus file is absent, so the screen
# still renders. These mirror web/lib/fixtures.ts; not live-computed.
_FALLBACK_CR = {
    "comments_per_pr": "11.3",
    "action_rate": "19%",
    "merged_past": "81%",
    "bug_share": "7%",
    "cites_guideline": "10%",  # documented 23/234
}

NOTE = (
    "Volume and action metrics come from CodeRabbit's persisted review threads "
    "(a precision oracle, what it found, not what was true). Catch-rate and "
    "false-positive-rate stay pending until the injected-bug and verified-finding "
    "corpora are scored locally."
)


@dataclass(frozen=True)
class _Side:
    comments_per_pr: str
    action_rate: str
    merged_past: str
    bug_share: str
    cites_guideline: str


def _pct(num: int, denom: int) -> str:
    return f"{round(100 * num / denom)}%" if denom else DASH


def _ratio(num: int, denom: int) -> str:
    return f"{num / denom:.1f}" if denom else DASH


def coderabbit_side(path: Path) -> _Side:
    """Aggregate the CR thread corpus; fall back to documented baselines if absent."""
    try:
        recs = _load(path)
    except FileNotFoundError:
        return _Side(**_FALLBACK_CR)
    if not recs:
        return _Side(**_FALLBACK_CR)

    total = len(recs)
    prs = {(r.get("repo"), r.get("pr")) for r in recs}
    resolved = sum(1 for r in recs if r.get("resolved"))
    merged_unresolved = sum(1 for r in recs if r.get("state") == "MERGED" and not r.get("resolved"))
    bug_class = sum(1 for r in recs if r.get("sev") in ("major", "critical"))
    cites = sum(1 for r in recs if r.get("cites_guideline"))
    return _Side(
        comments_per_pr=_ratio(total, len(prs)),
        action_rate=_pct(resolved, total),
        merged_past=_pct(merged_unresolved, total),
        bug_share=_pct(bug_class, total),
        cites_guideline=_pct(cites, total),
    )


def argus_side(verified_findings: int, done_reviews: int, bug_findings: int) -> _Side:
    """Argus volume/action numbers from the DB; action/merged/guideline-grounding
    need GitHub + rule-index data we do not have yet, so those stay DASH."""
    return _Side(
        comments_per_pr=_ratio(verified_findings, done_reviews),
        action_rate=DASH,   # acted/merged signal needs GitHub PR state (not wired)
        merged_past=DASH,   # same source as action_rate
        bug_share=_pct(bug_findings, verified_findings),
        cites_guideline=DASH,  # guideline-grounded fraction needs the rule index (not wired)
    )


def build_rows(cr: _Side, argus: _Side) -> list[dict]:
    def winner_lower(metric: str, a: str, c: str) -> str | None:
        return _winner(metric, a, c)

    return [
        _row("Comments / PR", "lower is calmer", argus.comments_per_pr, cr.comments_per_pr,
             winner_lower("lower", argus.comments_per_pr, cr.comments_per_pr)),
        _row("Action rate (acted-on)", "higher is better", argus.action_rate, cr.action_rate,
             _winner("higher", argus.action_rate, cr.action_rate)),
        _row("Merged-past (ignored)", "lower is better", argus.merged_past, cr.merged_past,
             _winner("lower", argus.merged_past, cr.merged_past)),
        _row("Bug-class share", "signal vs nit-noise", argus.bug_share, cr.bug_share,
             _winner("higher", argus.bug_share, cr.bug_share)),
        _row("Cites guideline", "grounded in docs", argus.cites_guideline, cr.cites_guideline,
             _winner("higher", argus.cites_guideline, cr.cites_guideline)),
        # Recall / FP-rate need the local scoring corpora; left explicitly pending.
        _row("Catch rate (recall)", "injected-bug corpus", DASH, DASH, None, pending=True),
        _row("False-positive rate", "verified-finding corpus", DASH, DASH, None, pending=True),
    ]


def per_repo_rows(path: Path) -> list[dict]:
    """CodeRabbit's per-repo breakdown, busiest first. Groups the thread corpus by
    repo; uses optional ARGUS_BENCHMARK_BASELINES_PATH rows if absent or empty.

    Each row: repo, crComments, resolved, mergedPast, critMajor, citesGuideline (counts).
    """
    try:
        recs = _load(path)
    except FileNotFoundError:
        return _baseline_rows()
    if not recs:
        return _baseline_rows()

    agg: dict[str, dict] = {}
    for r in recs:
        repo = r.get("repo")
        if repo is None:
            continue
        row = agg.setdefault(
            repo,
            {"repo": repo, "crComments": 0, "resolved": 0, "mergedPast": 0,
             "critMajor": 0, "citesGuideline": 0},
        )
        row["crComments"] += 1
        if r.get("resolved"):
            row["resolved"] += 1
        if r.get("state") == "MERGED" and not r.get("resolved"):
            row["mergedPast"] += 1
        if r.get("sev") in ("major", "critical"):
            row["critMajor"] += 1
        if r.get("cites_guideline"):
            row["citesGuideline"] += 1
    return sorted(agg.values(), key=lambda row: (-row["crComments"], row["repo"]))


def _baseline_rows() -> list[dict]:
    path = os.environ.get(ENV_BASELINES_PATH)
    if not path:
        return []
    try:
        rows = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            raise ValueError("expected a JSON list")
    except (OSError, ValueError) as error:
        log.warning("Could not load benchmark baselines from %s: %s", path, error)
        return []
    counts = ("crComments", "resolved", "mergedPast", "critMajor", "citesGuideline")
    valid = [{"repo": row["repo"], **{key: row[key] for key in counts}}
             for row in rows
             if isinstance(row, dict) and isinstance(row.get("repo"), str) and row["repo"].strip()
             and all(type(row.get(key)) is int and row[key] >= 0 for key in counts)]
    return sorted(valid, key=lambda row: (-row["crComments"], row["repo"]))


def _row(metric, note, argus, cr, winner, *, pending=False):
    out = {"metric": metric, "note": note, "argus": argus, "codeRabbit": cr, "winner": winner}
    if pending:
        out["pending"] = True
    return out


def _winner(direction: str, argus: str, cr: str) -> str | None:
    a, c = _num(argus), _num(cr)
    if a is None or c is None:
        return None
    if a == c:
        return None
    if direction == "lower":
        return "argus" if a < c else "coderabbit"
    return "argus" if a > c else "coderabbit"


def _num(value: str) -> float | None:
    if value == DASH:
        return None
    try:
        return float(value.rstrip("%"))
    except ValueError:
        return None


def _load(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


# --- Bake-off gate --------------------------------------------------------
# The adopt/kill gate, not a leaderboard. A verdict needs both axes; the cheap
# proxies above never decide it. Floors and the ceiling are the locked numbers.
CEILING_DAYS = 28
AXIS_A_BUG_FLOOR = 30
LIVE_PR_FLOOR = 20
MARK_FLOOR = 60
CRASHFREE_FLOOR_DAYS = 7
ACTION_RATE_FLOOR = 50
_DOMINANT_REPO_PCT = 40  # one repo at/above this share makes the sample one-sided


def _to_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def build_gate(
    *,
    status_counts: dict[str, int],
    first_review_at: str | None,
    last_error_at: str | None,
    verified_findings: int,
    bug_findings: int,
    severity: dict[str, int],
    repo_counts: list[dict],
    mark_count: int = 0,
    axis_a_bugs: int = 0,
    error_kinds: list[dict] | None = None,
    now: datetime | None = None,
) -> dict:
    """Compute the live gate state the benchmark page renders.

    Verdict is INSUFFICIENT-DATA until the Axis-A dual corpus is scored: nothing
    else can promote, and the ceiling only forces KILL once the corpus is built
    but ambiguous. action-rate is 0 until the ✓/✗ triage UI exists.
    """
    now = now or datetime.now(timezone.utc)
    done = status_counts.get("done", 0)
    errors = status_counts.get("error", 0)
    running = status_counts.get("running", 0)

    started_at = _to_utc(first_review_at)
    started = started_at is not None
    elapsed = (now - started_at).days if started_at else 0
    day = elapsed + 1 if started else 0
    days_remaining = max(0, CEILING_DAYS - elapsed) if started else CEILING_DAYS
    ceiling = (started_at + timedelta(days=CEILING_DAYS)).date().isoformat() if started_at else None

    # Crash-free runs from the last engine error; any error in-window fails the gate.
    error_at = _to_utc(last_error_at)
    crashfree_days = (now - error_at).days if error_at else elapsed
    crashfree_met = errors == 0 and elapsed >= CRASHFREE_FLOOR_DAYS

    live_met = done >= LIVE_PR_FLOOR
    action_pct = 0  # operator ✓/✗ marks UI not built yet
    action_met = action_pct >= ACTION_RATE_FLOOR and mark_count >= MARK_FLOOR

    axis_a_status = "not-started" if axis_a_bugs == 0 else "in-progress"
    axis_b_status = "passing" if (live_met and crashfree_met and action_met) else "blocked"

    comments_per_pr = f"{verified_findings / done:.1f}" if done else DASH
    bug_share = f"{round(100 * bug_findings / verified_findings)}%" if verified_findings else DASH

    floors = [
        {"key": "corpus", "label": "Axis-A corpus bugs", "value": axis_a_bugs,
         "floor": AXIS_A_BUG_FLOOR, "met": axis_a_bugs >= AXIS_A_BUG_FLOOR,
         "detail": "escaped ∪ CR-caught-pre-merge, not mined yet" if axis_a_bugs == 0 else None},
        {"key": "livePRs", "label": "Live PRs reviewed", "value": done,
         "floor": LIVE_PR_FLOOR, "met": live_met, "detail": None},
        {"key": "marks", "label": "Operator ✓/✗ marks", "value": mark_count,
         "floor": MARK_FLOOR, "met": mark_count >= MARK_FLOOR,
         "detail": "triage UI not built yet" if mark_count == 0 else None},
        {"key": "crashFree", "label": "Crash-free days", "value": max(0, crashfree_days),
         "floor": CRASHFREE_FLOOR_DAYS, "met": crashfree_met, "failing": errors > 0,
         "detail": f"{errors} engine errors" if errors else None},
    ]

    total_reviews = done + errors + running
    skew = [
        {"repo": r["repo"].split("/")[-1], "count": r["count"],
         "pct": round(100 * r["count"] / total_reviews) if total_reviews else 0}
        for r in repo_counts[:6]
    ]
    dominant = None
    if skew and skew[0]["pct"] >= _DOMINANT_REPO_PCT:
        dominant = {"repo": skew[0]["repo"], "pct": skew[0]["pct"]}

    return {
        "verdict": "INSUFFICIENT-DATA",
        "verdictWhy": (
            "Axis A (the recall corpus) is not built, so no verdict can ship. The "
            "proxies below are diagnostics, they never decide who wins."
        ),
        "clock": {
            "started": started,
            "day": day,
            "totalDays": CEILING_DAYS,
            "daysRemaining": days_remaining,
            "firstReviewAt": first_review_at,
            "ceiling": ceiling,
        },
        "floors": floors,
        "axisA": {
            "title": "Comparison",
            "status": axis_a_status,
            "rule": (
                "Paired McNemar on a dual corpus (escaped ∪ CR-caught-pre-merge). "
                "Promote when Argus-only catches beat CR-only at p < 0.10, ≥ 3 net."
            ),
            "bugsScored": axis_a_bugs,
            "floor": AXIS_A_BUG_FLOOR,
        },
        "axisB": {
            "title": "Deployment",
            "status": axis_b_status,
            "metrics": [
                {"label": "Live PRs", "value": str(done), "floor": "≥ 20", "met": live_met,
                 "detail": None},
                {"label": "Crash-free", "value": f"{max(0, crashfree_days)}d", "floor": "≥ 7d",
                 "met": crashfree_met, "detail": f"{errors} errors" if errors else None},
                {"label": "Action-rate", "value": f"{action_pct}%", "floor": "≥ 50%",
                 "met": action_met, "detail": "no marks UI yet"},
            ],
        },
        "axisC": {
            "title": "Procurement",
            "status": "awaiting",
            "rule": (
                "MCV = Σ severity·(CR-caught, Argus-missed) ÷ CR price. Below 0.3 makes "
                "CodeRabbit cancel-eligible. Decided separately from the Argus verdict."
            ),
        },
        "severity": severity,
        "repoSkew": skew,
        "dominantRepo": dominant,
        "proxies": {"commentsPerPr": comments_per_pr, "bugShare": bug_share},
        "reviews": {
            "done": done, "error": errors, "running": running,
            "errorKinds": error_kinds or [],
        },
    }
