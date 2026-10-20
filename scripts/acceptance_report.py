#!/usr/bin/env python3
"""Print acceptance counts and intervals from a read-only database copy."""

import argparse
import json

from argus.acceptance import stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db", help="SQLite database copy")
    parser.add_argument("--since", help="Inclusive posting date or ISO timestamp")
    args = parser.parse_args()
    try:
        result = stats(args.db, args.since)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
