# GitHub golden snapshots

This suite pins the current GitHub behavior before a refactor. Each synthetic
scenario records ordered requests, complete model prompts, return values and
non-empty database tables. It runs without network or model access.

Run `uv run pytest tests/golden -q` to compare. Regenerate with
`ARGUS_GOLDEN_UPDATE=1 uv run pytest tests/golden`. A later pull request may
regenerate a snapshot only when its body explains the reason for that change.
All fixtures and snapshots must contain synthetic data only.

One strict recording transport covers the module client used by `github.get`,
`graphql_read` and `list_cached`, plus the bare `httpx.get`, `httpx.post` and
`httpx.request` paths. Records distinguish `github.get` from `httpx`. The model
stub captures `build_client(...).messages.create` after provider resolution,
including complete messages, schemas and cache parameters. Unregistered or
exhausted requests and model calls fail, even when production catches the error.

The test database, environment, clocks and caches are isolated for every test.
Database timestamps and run identifiers are normalized; raw request bytes are
hashed. API tests use the real `TestClient` and background review pipeline.
Watcher cases call the real `poll_once` and `process_one` serially.

| Scenario | Behavior pinned |
| --- | --- |
| `review` | Full review and verification, coverage, unsafe paths and file-list limits. |
| `diff-review` | Full and fast review of parsed diffs. |
| `publish` | Payload placement, deduplication, recovery, retries and head changes. |
| `publish-access` | Permission checks and the complete invitation flow. |
| `cli-publish-placement` | Placement diagnostics and publish preview output. |
| `api` | Review creation, publishing, integrations and configuration routes. |
| `replies` | Trusted reply rechecks, reply writes and thread resolution. |
| `acceptance` | Posted finding collection, recovery and acceptance reports. |
| `automark` | Finding selection, judge decisions, persisted marks and the human queue. |
| `labels-monthly` | Labels, monthly coverage and retirement decisions. |
| `watch-store` | Listing, durable claims, trust checks, fast merges and retry state. |
| `watcher` | One polling tick and one serial review worker step. |
| `policy` | Repository validation, error categories and coverage status combinations. |
| `schema` | Tables, indexes, column metadata, journal mode and repeat initialization. |

See [GAPS.md](GAPS.md) for constraints imposed by existing production output.
