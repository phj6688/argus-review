#!/usr/bin/env python3
"""Weekly bake-off recompute: read-only heartbeat for the Argus vs CodeRabbit gate.

Curls the live argus-api (ARGUS_API, default 127.0.0.1:8350), prints the two-number
view (emits + labeled-precision lower-CI), the error-class
mix, latency, and where we are on the 28-day clock with the matching checkpoint
reminder. It NEVER writes prod data and NEVER flips a gate: it only measures and
reports. The trust decisions (shadow->enforce, the week-8 go/no-go, posting) stay
with the operator.

Run by cron weekly; safe to run by hand any time. Stdlib only (no venv needed).
Degrades gracefully against the pre-deploy API shape (no 'labeling' key yet).
"""

import json
import math
import os
import sys
import urllib.request
from datetime import datetime, timezone

API = os.environ.get("ARGUS_API", "http://127.0.0.1:8350")
# Bake-off clock day 0 = watcher-on. Overridden by the API's first review when present.
BAKEOFF_START = datetime(2026, 6, 20, tzinfo=timezone.utc)
CEILING_DAYS = 28


def _get(path: str) -> dict | None:
    try:
        with urllib.request.urlopen(f"{API}{path}", timeout=15) as resp:
            return json.load(resp)
    except Exception as error:  # noqa: BLE001 - a heartbeat reports failure, never raises
        print(f"  ! could not reach {API}{path}: {type(error).__name__}: {error}")
        return None


def _wilson_low(successes: int, n: int, z: float = 1.96) -> float:
    """Lower bound of the Wilson 95% interval for a proportion (the honest floor)."""
    if n == 0:
        return 0.0
    p = successes / n
    denom = 1 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (center - margin) / denom)


def _checkpoint(day: int) -> str:
    if day < 13:
        return f"day {day}/28: gathering. First hard check is day 14 (label count)."
    if day < 16:
        return (
            f"day {day}/28: DAY-14 CHECK. If < 20 findings are labeled, cancel the "
            "retrospective-140 and the posting trial in writing."
        )
    if day < 23:
        return (
            f"day {day}/28: WEEK-3. Review a 20-finding would-drop sample; decide whether "
            "to flip the grounding gate shadow->enforce (operator judgment)."
        )
    if day < 28:
        return f"day {day}/28: WEEK-4. Verifier A/B interim read (single-pass vs verified)."
    if day < 60:
        return (
            f"day {day} (past the 28-day ceiling): WEEK-8 GO/NO-GO is due. Three verdicts: "
            "continue / narrow / background-only-and-reassess. Verifier: delete it if "
            "single-pass >= verified - 3pp lower-CI."
        )
    return f"day {day}: past week-8. Re-scope or shut the bake-off down."


def main() -> int:
    now = datetime.now(timezone.utc)
    print(f"=== Argus bake-off weekly recompute @ {now.isoformat(timespec='seconds')} ===")

    stats = _get("/stats")
    bench = _get("/benchmark")
    if stats is None:
        print("  API unreachable; nothing to report this run.")
        return 1

    prs = stats.get("prsReviewed", {})
    print(f"PRs reviewed (distinct): {prs.get('value', '?')}  (raw reviews: {prs.get('reviews', '?')})")

    labeling = stats.get("labeling")
    if labeling is None:
        print("Labeled precision: PENDING (deploy the measurement-foundation branch; "
              "no marks table on the running API yet).")
    else:
        located = labeling.get("located", 0)
        real = labeling.get("real", 0)
        if located == 0:
            print(f"Labeled precision: PENDING (0 findings labeled of "
                  f"{labeling.get('labeled', 0)} marks). Label ~20 to get a real number.")
        else:
            low = _wilson_low(real, located)
            print(f"Labeled precision: {real}/{located} = {real / located:.0%} "
                  f"(Wilson 95% low {low:.0%}) over {labeling.get('labeled')} marks. "
                  f"Soft-promote needs low >= 75% and >= 80 labels.")

    gate = (bench or {}).get("gate") or {}
    status = gate.get("statusCounts") or gate.get("status_counts") or {}
    if status:
        done, err = status.get("done", 0), status.get("error", 0)
        total = done + err + status.get("running", 0)
        rate = f"{err / total:.0%}" if total else "?"
        print(f"Reviews: {done} done, {err} error ({rate} error rate).")
    errs = (bench or {}).get("gate", {}).get("errorKinds") or stats.get("errorKinds")
    if errs:
        print("Error classes:", ", ".join(f"{e.get('kind')}={e.get('count')}" for e in errs))

    # Bake-off clock + the matching checkpoint reminder.
    start = BAKEOFF_START
    first = (gate.get("firstReviewAt") or gate.get("first_review_at"))
    if first:
        try:
            start = datetime.fromisoformat(first.replace("Z", "+00:00"))
        except ValueError:
            pass
    day = (now - start).days
    print(f"Clock: {_checkpoint(day)}")
    print("Reminder: this is measurement only. shadow->enforce, posting, and the "
          "go/no-go are operator decisions, never automated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
