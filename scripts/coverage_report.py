#!/usr/bin/env python3
"""Print PR coverage for a UTC window without modifying the database."""

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from argus.api.db import _parse_created_at
from argus.api.watch_metrics import coverage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db", help="SQLite database path")
    parser.add_argument("--start", required=True, help="Inclusive ISO timestamp")
    parser.add_argument("--end", required=True, help="Exclusive ISO timestamp")
    parser.add_argument("--as-of", help="Measurement ISO timestamp (default: now; at least --end)")
    args = parser.parse_args()
    try:
        start, end = _parse_created_at(args.start), _parse_created_at(args.end)
        as_of = _parse_created_at(args.as_of) if args.as_of else datetime.now(timezone.utc)
    except ValueError as exc:
        parser.error(f"invalid timestamp: {exc}")
    if start >= end:
        parser.error("--end must be after --start")
    if as_of < end:
        parser.error("--as-of must not be earlier than --end")
    conn = sqlite3.connect(Path(args.db).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        print(json.dumps(coverage(conn, start, end, as_of=as_of), indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
