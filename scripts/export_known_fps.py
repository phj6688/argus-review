#!/usr/bin/env python3
"""Export confirmed false positives as one JSON object per line."""

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from argus.api.db import db_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db", nargs="?", default=db_path(), help="SQLite database copy")
    args = parser.parse_args()
    with closing(sqlite3.connect(Path(args.db).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        for row in conn.execute("SELECT * FROM known_fps_live ORDER BY repo, file, title"):
            print(json.dumps({**dict(row), "verdict": "FALSE_POSITIVE",
                              "reason": f"Author rejected the published finding (reply {row['reply_id']})"}))


if __name__ == "__main__":
    main()
