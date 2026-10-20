"""Shared report values and read-only database access."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from argus.acceptance import wilson


def rate(hits, n):
    return {'hits': hits, 'n': n, 'value': hits / n if n else None, 'wilson95': wilson(hits, n)}


def average(total, n):
    # Item counts are not Bernoulli trials, so Wilson intervals do not apply.
    return {'total': total, 'n': n, 'value': total / n if n else None, 'wilson95': None}


def display(value):
    if value is None or value.get('value') is None:
        return 'not available'
    interval = value.get('wilson95')
    if interval and interval[0] is not None:
        return (f"{value['hits']} / {value['n']} ({value['value']:.1%}; "
                f"Wilson 95% {interval[0]:.1%} to {interval[1]:.1%})")
    return f"{value['value']:.2f} (n = {value['n']}; Wilson 95% not applicable)"


def write_report(out_dir, report, markdown):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / 'report.json').write_text(json.dumps(report, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    (out / 'report.md').write_text(markdown + '\n', encoding='utf-8')


@contextmanager
def readonly(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()
