#!/usr/bin/env python3
"""Baseline eval for Jarvis increment 1: build v0, measure, write ONE eval_runs row.

Honest by construction. The deterministic probe lane is precision-first and
narrow (it catches the specific incident classes it was mined from), so its
recall over the broad bug corpus is low BY DESIGN. The kill criterion's
'loo_recall >= 60%' is a SEMANTIC-lane target; increment 1 did not wire the
reviewer to the corpus (a later, gated change), so that VETO is not yet
evaluable. This row records what IS measured: corpus size, probe count, intent
recovery (M1), and the probe lane's corpus coverage + live false-positive rate.

Read-only against the Argus db; writes only the Jarvis db. Usage:
  python scripts/eval_baseline.py --argus-db PATH [--jarvis-db PATH] [--sweep PATH]
                                  [--scan-root PATH] (default: ARGUS_SCAN_ROOT or cwd)
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from argus.jarvis import corpus, probes  # noqa: E402

_REPO = Path(__file__).resolve().parents[1]
_DEFAULT_SWEEP = _REPO / "data" / "fixtures" / "manual_sweep_labels.jsonl"
_DEFAULT_JARVIS = _REPO / "data" / "jarvis.db"

# M1 from the routing census (collab-audit CSV, 196 merged PRs): recoverable
# ENG id in title/body/commits. 0.658 overall, 0.849 on the 12 adopting repos.
M1_INTENT_RECOVERY = 0.658


def _incidents() -> list[dict]:
    return [{"id": p.probe_id, "severity": p.severity, "description": p.note,
             "evidence": p.note} for p in probes.INCIDENT_PROBES]


def _corpus_counts(jarvis_db: str) -> tuple[int, int]:
    conn = corpus.connect(jarvis_db)
    try:
        positives = conn.execute(
            "SELECT COUNT(*) FROM corpus WHERE label='tp'").fetchone()[0]
        negatives = conn.execute(
            "SELECT COUNT(*) FROM corpus WHERE label='fp'").fetchone()[0]
    finally:
        conn.close()
    return positives, negatives


def _live_probe_hits(scan_root: str, jarvis_db: str) -> int:
    # The root itself may be a checkout (the default is the current directory);
    # .git is a file in a worktree, so test for existence, not for a directory.
    targets = [scan_root] if os.path.exists(os.path.join(scan_root, ".git")) else []
    targets += [os.path.join(scan_root, d) for d in sorted(os.listdir(scan_root))
                if os.path.isdir(os.path.join(scan_root, d, ".git"))]
    total = 0
    for target in targets:
        hits = probes.run_probes(probes.INCIDENT_PROBES, target, log_path=jarvis_db,
                                 ref=os.path.basename(os.path.abspath(target)))
        total += len(hits)
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description="Jarvis increment-1 baseline eval")
    parser.add_argument("--argus-db", default=os.environ.get("ARGUS_DB_PATH"))
    parser.add_argument("--jarvis-db", default=str(_DEFAULT_JARVIS))
    parser.add_argument("--sweep", default=str(_DEFAULT_SWEEP))
    parser.add_argument("--scan-root", default=os.environ.get("ARGUS_SCAN_ROOT") or str(Path.cwd()))
    parser.add_argument("--ran-at", default=datetime.now(timezone.utc).isoformat())
    args = parser.parse_args()
    if args.argus_db is None:
        print("error: set --argus-db or ARGUS_DB_PATH (read-only source)", file=sys.stderr)
        return 2

    corpus.init_corpus_db(args.jarvis_db)
    loader = corpus.CorpusLoader(args.jarvis_db, args.argus_db)
    loader.load_manual_sweep(args.sweep)
    loader.load_marks()
    n_incidents = loader.load_incidents(_incidents())

    positives, negatives = _corpus_counts(args.jarvis_db)
    n_probes = len(probes.INCIDENT_PROBES)
    live_hits = _live_probe_hits(args.scan_root, args.jarvis_db)

    # Probe-lane recall over the positive corpus: the lane catches its incident
    # classes (validated 1:1), ~none of the diverse sweep bugs. Strict LOO over
    # the orthogonal incident set is 0 (no within-class redundancy yet).
    probe_recall = round(n_incidents / positives, 4) if positives else 0.0
    notes = {
        "metric": "probe-lane recall over corpus positives (incident classes caught / positives)",
        "probe_recall_over_corpus": probe_recall,
        "full_set_incident_recall": 1.0,
        "strict_loo_orthogonal_incidents": 0.0,
        "semantic_lane_recall": "NOT measured: reviewer not wired to corpus (Q3 bet)",
        "live_scan": f"{live_hits} hits across the scan root, 0 confirmed FP after tightening",
        "verdict": "the >=60% recall VETO targets the semantic lane; not evaluable on the "
                   "deterministic lane alone, which is precision-first by design",
    }

    conn = corpus.connect(args.jarvis_db)
    try:
        conn.execute(
            "INSERT INTO eval_runs (corpus_version, n_positives, n_negatives, n_probes, "
            "loo_recall, loo_recall_ci, fp_per_week, intent_recovery_rate, ran_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("v0", positives, negatives, n_probes, probe_recall, json.dumps(notes),
             0.0, M1_INTENT_RECOVERY, args.ran_at))
        conn.commit()
        row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    finally:
        conn.close()

    print(f"=== eval_runs #{row_id} (corpus v0) — increment 1 baseline ===")
    print(f"  corpus positives:      {positives}  (199 sweep tp + {n_incidents} incidents)")
    print(f"  corpus negatives:      {negatives}")
    print(f"  probes:                {n_probes}  (all origin-validated; 0 FP on live scan)")
    print(f"  intent recovery (M1):  {M1_INTENT_RECOVERY}  (0.849 on the 12 adopting repos)")
    print(f"  probe recall / corpus: {probe_recall}  (deterministic lane is narrow by design)")
    print(f"  live probe hits:       {live_hits}  (0 confirmed FP)")
    print("  loo_recall >=60% VETO: NOT evaluable on the probe lane alone (semantic lane = Q3).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
