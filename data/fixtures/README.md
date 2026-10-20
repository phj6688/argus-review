# Fixtures

## Known false positives

`known_fps.example.jsonl` is a synthetic example of the known-false-positive format:
one JSON object per line.

```json
{"repo": "example/app", "tier": "bug-class", "file": "app/main.py", "line": 35,
 "severity": "major", "title": "...", "verdict": "FALSE_POSITIVE",
 "suggested_mark": "fp", "reason": "..."}
```

The live review drops a finding whose `(repo, file, normalised title)` matches a row.
The match is exact on purpose, so a real finding is never lost to a fuzzy collision.

Suppression from a file is off by default. To turn it on, keep your own file outside
the repository (it describes code in your repos) and set:

```
ARGUS_KNOWN_FPS_PATH=/path/to/known_fps.jsonl
```

In a container, mount the file read-only and point the variable at the mounted path.
`scripts/export_known_fps.py <database-copy>` exports the rejections that Argus
learned from author replies as JSONL. `repo`, `file` and `title` are the match keys.

## Manual-sweep labels

`manual_sweep_labels.jsonl` (not committed) is the output of a one-time verification
sweep: `{repo, tier, file, line, severity, title, verdict, suggested_mark, reason}`,
with `verdict` one of `REAL_BUG | LIKELY_REAL | FALSE_POSITIVE | STALE_FIXED | CANT_LOCATE`.

These verdicts come from an automated verification pass, not from the operator. The
`marks` table is meant to hold operator ground truth, so:

- Do not bulk-insert sweep verdicts into `marks`.
- `suggested_mark` pre-fills a label for the operator to confirm or override.
- A known-false-positive set is safe as a suppression set, because the signal is
  "Argus was wrong here", which holds regardless of who graded it.
