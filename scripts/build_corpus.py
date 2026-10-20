#!/usr/bin/env python3
"""Build (freeze) a Jarvis corpus version from the sweep fixture + an Argus db.

v0 = the frozen 239-row manual sweep (positive backbone) + any operator marks.
Read-only against the Argus db; writes only the separate Jarvis db. Re-running is
idempotent per source, so this IS the freeze: same inputs reproduce the same v0.

Usage:
  python scripts/build_corpus.py [--argus-db PATH] [--jarvis-db PATH]
                                 [--sweep PATH] [--version v0]

Defaults: ARGUS_DB_PATH (or the repo default), data/jarvis.db, the sweep fixture.
The sweep file must hold the real rows; it is committed empty on some branches,
so point --sweep at a populated copy when the working tree's is empty.
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from argus.jarvis import corpus, probes  # noqa: E402


def _incidents_from_catalog() -> list[dict]:
    return [{"id": p.probe_id, "severity": p.severity, "description": p.note,
             "evidence": p.note} for p in probes.INCIDENT_PROBES]

_REPO = Path(__file__).resolve().parents[1]
_DEFAULT_SWEEP = _REPO / "data" / "fixtures" / "manual_sweep_labels.jsonl"
_DEFAULT_JARVIS = _REPO / "data" / "jarvis.db"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a frozen Jarvis corpus version")
    parser.add_argument("--argus-db", default=os.environ.get("ARGUS_DB_PATH"))
    parser.add_argument("--jarvis-db", default=str(_DEFAULT_JARVIS))
    parser.add_argument("--sweep", default=str(_DEFAULT_SWEEP))
    parser.add_argument("--version", default=corpus.DEFAULT_VERSION)
    args = parser.parse_args()

    if args.argus_db is None:
        print("error: set --argus-db or ARGUS_DB_PATH (read-only source)", file=sys.stderr)
        return 2
    if os.path.getsize(args.sweep) == 0:
        print(f"error: sweep file {args.sweep} is empty; point --sweep at the real rows",
              file=sys.stderr)
        return 2

    corpus.init_corpus_db(args.jarvis_db)
    loader = corpus.CorpusLoader(args.jarvis_db, args.argus_db, version=args.version)
    n_sweep = loader.load_manual_sweep(args.sweep)
    n_marks = loader.load_marks()
    n_incidents = loader.load_incidents(_incidents_from_catalog())

    print(f"corpus {args.version} -> {args.jarvis_db}")
    print(f"  manual_sweep: {n_sweep}   marks: {n_marks}   incidents: {n_incidents}")
    print(f"  by source:    {loader.counts_by_source()}")
    print(f"  labeled (tp|fp): {loader.labeled_count()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
