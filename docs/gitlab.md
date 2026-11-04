# GitLab support notes

## Measurement filter

Baseline readers count GitHub keys only: acceptance reports and stats, head coverage
and coverage stats, review and finding metrics, benchmark inputs, monthly reports,
acceptance labels, corpus finding and mark readers, and known false-positive exports.
Weekly API reports and the overview's measurement counters inherit these filters.

Operational readers stay forge-wide: the watcher queue, claims and worker health,
store operations and console lists, and the `reviews_window`, `top_error_class` and
`review_health` functions used by the watchdog, health endpoint and stats operations block.
Lookups by review id and suppression by repository keep their existing scope.
Acceptance collection and reconciliation and per-repository replies already skip GitLab.

Use `is_github_key` in Python and `github_key_sql` in SQL. A key with a colon never
feeds a GitHub baseline number, including a future forge prefix.
The configured-only monthly `skipped_gitlab` count includes findings plus posted reviews.
With GitHub-only data, measurements and output shapes stay unchanged.
