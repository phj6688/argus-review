# Changelog

## Unreleased

- Moved the finding-trace and auto-label modules into the main argus package; their commands are now argus subcommands (explain, why-quiet, automark); the corpus database setting is ARGUS_CORPUS_DB_PATH and its default file is data/corpus.db.

- Trusted GitLab rejections now suppress the same finding in later reviews when the reply loop is enabled. A confirmed fix at a later commit removes the suppression.
- Acceptance reports show GitLab separately, counting only confirmed inline diff notes and membership-trusted replies. Rejected and downgraded anchors stay outside the rates. GitHub baseline figures stay unchanged, and the precision and verified-finding metrics can select GitLab explicitly.
- GitLab replies and confirmed fix resolution run behind their own opt-in flag, `ARGUS_GITLAB_REPLY_LOOP` (default off), and the live publishing gates. Replies share the publisher's pacing, recover uncertain writes by marker and keep GitHub limits separate. A refused resolution leaves the reply posted and appears in integrations.
- GitLab comparisons supply finding re-checks and track ancestry. Incomplete comparisons stay unknown. Acceptance collection and the reply helpers now read comparisons and files through the forge adapter.
- Acceptance collection records GitLab merge request state, human replies with membership trust, and discussion resolution. Missing discussions record no outcomes, and GitLab failures or rate limits do not interrupt GitHub collection.
- GitLab thread reader: reads every discussion page, drops system notes, and recognises bot notes by numeric user id, with a username fallback counted in `bot_id_missing` only when the id is not recorded. Nothing calls it yet; the reply loop for GitLab is not part of this change.
- GitLab watching now feeds the shared review queue with incremental polling, periodic full reconciliation and retries for unknown membership. Only complete listings close missing requests. Discovery skips permanently unavailable groups and projects, stale requests stay excluded on incremental polls, and poll logs include push counts. GitHub and GitLab polling remain independent. Integrations report the watch state.
- GitHub polling preserves queued GitLab reviews. Each forge checks its own watched set and quota pause. GitLab workers recheck live policy, retain denial reasons and retry temporary lookup failures.
- GitLab listing expands permitted groups without shared projects, rechecks live project policy and reads every merge request page with a five minute cursor overlap. Membership determines author trust, unknown results stay skipped until a later sweep, and integrations show the unresolved count and a health warning. Unresolved lookups rotate so later heads can recover. Preparing heads hold the cursor and keep full listings incomplete to preserve queued work.
- Added incremental GitLab watch storage, strict membership trust, separate forge pauses and persistent poll cursors. Overlapping polls track head shas because pushes may not move the update timestamp.
- GitLab support is documented in `docs/gitlab.md` and the README: self-managed and gitlab.com setup, token choice and scopes, roles, minimum version, settings, limits and pacing, the hard-deny and fork rules and known limits. Merge request reviews post inline discussions and one summary note. Watching and replies for GitLab are not available yet. GitHub behaviour is unchanged.
- Added GitLab console trigger forms, project labels, merge request links and an integrations card. GitHub rows keep their existing presentation. The `/integrations` GitLab block now includes `baseUrl` so console links preserve the configured path prefix.
- GitLab reviews can now post inline discussions and one summary note, with partial posts recorded only where creation was confirmed. Retrying a partially posted review posts its own missing summary note even when another review of the same head already posted one. Attempts that write nothing preserve the partial state for a later retry. A new review of an unchanged head still posts nothing. GitHub publishing is unchanged.
- Added GitLab note pacing and a lock per merge request. Writes report posted, already_posted, failed or unknown. Partial posts record only confirmed findings. GitHub behavior is unchanged.
- Added GitLab body helpers that neutralize quick actions and mentions, plus inline positions that preserve the correct old and new line numbers. Multi-line positions remain disabled by default.
- Added GitLab reviews through the API, CLI and MCP with stable project keys, live deny checks and cached integration identity. GitHub entry points stay unchanged. GitHub only commands refuse GitLab keys before any request. GitLab path lookup failures from timeouts, upstream errors, rate limits and authentication return 502 with a safe classification, unknown, renamed and malformed paths return 422, and denied projects return 403. The API logs whether GitLab is configured at startup.
- A golden snapshot suite under `tests/golden/` pins GitHub requests, prompts, return values and database rows before the forge refactor. `ARGUS_GOLDEN_UPDATE=1` regenerates it.
- Added a GitLab reader with head checks, paginated diffs, explicit incomplete coverage, target project file reads and default branch house rules. GitLab failures no longer trigger a model fallback. GitLab auth, denial and rate-limit errors are no longer swallowed during context reads.
- Added live GitLab project resolution by id, deny checks that follow renames and transfers, hard group denies and fork source checks. Configured GitLab integrations now report policy refusal reasons.
- Added a GitLab HTTP client with guarded token headers, redirect refusal, bounded responses, safe pagination, per-client rate limits, GET-only retries and distinct error categories.
- Added a pure GitLab URL guard with fixed route builders, strict path and query validation, and checks against the configured HTTPS origin and API prefix.
- A review whose file listing was incomplete (more than 3000 files, an unsafe path, or an unparseable diff section) no longer reports `full` coverage. The reviewer lane records `files_unseen: true`, and when that lane ran the coverage level is `partial` instead of `full`. A skipped reviewer lane keeps its previous coverage level. `files_omitted` and the posted line "Files reviewed: N; omitted: M" are unchanged, and lane records of complete listings are byte-identical. Stored reviews are not rewritten.
- Measurement readers now count GitHub keys only. Operational counters stay forge-wide. The monthly report skip count includes findings plus posted reviews. GitHub-only data produces unchanged measurements.
- Route GitHub publishing through the forge adapter and explicitly skip GitLab keys in outcome collection, replies, automarking and evaluation, with skip counters when GitLab is configured. GitHub behavior is unchanged.
- Read the review path through a GitHub forge adapter and treat forge auth, denial and rate-limit errors as non-retryable by class (GitHub behaviour unchanged).
- Added GitLab project storage, nullable forge columns and shared trust helpers. GitHub behavior is unchanged.
- Added a repository key parser and forge aware deny and allow policy. `is_denied` stays pure and denies every GitLab key. GitHub behavior is unchanged.
- GitLab token prefixes are redacted. The `ARGUS_GITLAB_*` settings and a single base URL parser add configuration only, with GitLab features off by default and no GitLab calls yet.
- Added `argus.forge` with neutral types and an error hierarchy. GitHub exceptions gain neutral bases with no behavior change.
- The posting list is rechecked immediately before each review POST (and the 422 retry), and the deny policy is read strictly and revalidated before the auto-invite collaborator PUT. An unreadable deny list blocks the invite. A small window remains between the PUT and the invitation accept, which is not rechecked.
- Four off-by-default settings add context blocks to the reviewer prompt for offline evaluation: `ARGUS_CONTEXT_PAIRING`, `ARGUS_CONTEXT_CALLEES`, `ARGUS_CONTEXT_CALLERS`, `ARGUS_CONTEXT_COCHANGE`. The three mirror blocks need a replay mirror and skip in production. `argus.evalkit.replay --reviewer-only` skips the verifier.
- Every completed review carries a deterministic `safe` or `risky` path and size category in the console, verdict, MCP response and published body. The category is not a correctness verdict. `ARGUS_RISK_TRIPWIRES` and `ARGUS_RISK_MAX_LINES` configure the rule.
- GitHub API calls reuse one HTTP client, so each call no longer opens a new TLS connection.
- The `action` column of the reviews list shows the real GitHub posting state of each finished review: `posted` (with `posted_comments`), `dry_run`, `not_posted` or `post_failed`. A failed run has `action: null` and shows a dash. Before, every row said `pending`.
- The Config page has a "GitHub posting" section. Drag a repository between "Review only" and "Review and post to GitHub" (or use the arrow button) to change where Argus posts. The list is stored in the database. `ARGUS_PUBLISH_REPOS` seeds it until the first save, and `ARGUS_PUBLISH` stays an environment setting. New API: `PUT /config/publish-repos`. `GET /config` adds a `publish` object and `GET /integrations` adds `github.publishing.source`. On add, Argus checks the bot's write access. With the new opt-in `ARGUS_PUBLISH_AUTO_INVITE=1` (default 0, needs a `GITHUB_TOKEN` with Administration:write) it also invites the bot for watched owners and accepts the invitation. Without the opt-in `GITHUB_TOKEN` is never used to write. When the bot has no access the repository is still saved and the console shows a warning with a link to the access settings. Denied owners are refused with `422`.
- Compose sets no project, container or image names, and the host ports come from `ARGUS_API_PORT` and `ARGUS_WEB_PORT`. A second copy no longer collides with the first. Add the old names in `docker-compose.override.yml` if a script needs them.
- `POST /reviews` (and `POST /review-diff` in full mode) answers `503` with the `argus doctor` message when no model provider is configured. Before, it created a review that failed in the background.
- `GET /reviews/{id}` returns the real branch, author and diff stats when the run recorded them, and leaves them out otherwise. It also returns `model_used`, `fallback_from`, `fallback_reason` and, for a failed run, a sanitised `error`. The console review page shows them.
- `/health` adds `provider` and `provider_configured`. The status stays `ok`.
- `argus review` honours `ARGUS_FALLBACK_MODEL`, prints the model that ran, and exits with one line and status 1 when `GITHUB_TOKEN` is missing. `argus doctor` prints each failure once.
- The default database for a host install is `./.argus/argus.db`, outside the tracked `data/` folder. An existing `./data/argus.db` is still used.
- The Docker default for `ARGUS_PUBLIC_API_URL` follows `ARGUS_API_PORT`.

## 0.1.0 - initial public release

First public release of Argus, a self-hosted AI code reviewer for GitHub pull requests.

### Review engine

- A reviewer pass reports candidate defects as structured findings with a category, severity, confidence, a verbatim evidence line and a suggested fix.
- A skeptical verifier pass checks each finding against the diff and drops what it cannot confirm. Verifier variants are selectable with `ARGUS_VERIFY_VARIANT`.
- Deterministic lanes run beside the model: ruff on changed Python files, a config lane for compose and YAML mistakes, and a catalogue of known failure patterns.
- Findings can cite entries of a rule index (`data/rules/index.json`). A finding cannot invent a rule.
- A verdict reports `insufficient_evidence` when a run did not read the code, so a skipped check never reads as a pass.
- Findings are ranked and capped. Critical and major findings are the bug-class output.

### Model providers

- OpenRouter, Anthropic API key, OpenAI or any OpenAI-compatible server, a self-hosted Anthropic-compatible gateway, and Claude or ChatGPT subscriptions through their command-line tools.
- A fallback model and a model picker for the console.

### Interfaces

- A CLI (`argus review`, `compress`, `doctor`, and the evaluation commands).
- A FastAPI service with SQLite storage.
- A Next.js console with overview, reviews, reports, benchmark, config and integrations screens. It runs on demo fixtures when no API is configured.
- The integrations screen shows live watcher, publishing, model provider and auth status.
- An MCP server (`argus-mcp`) with `review_diff`, `review_pr`, `get_verdict` and `mark_finding`.

### Watcher, publishing and replies

- A watcher that reviews new PR heads on the repositories you choose. It reviews trusted authors only by default, and `ARGUS_DENY_OWNERS` excludes owners and repositories for good.
- Posting inline review comments, off by default and in dry-run first. It needs its own write token and a repository allowlist.
- A reply loop that reads author replies and learns which findings were rejected.
- Acceptance tracking for posted findings.

### Evaluation

- An offline harness (`argus.evalkit`): replay historical PRs through the review code, score findings against known outcomes, and rerun the verifier alone on stored findings.
- Cron wrappers for weekly replay and monthly reports.

### Security

- Token auth by default. The API refuses to start without a token of 32 characters or more.
- Compose binds the API and the console to `127.0.0.1`.
- PR text is treated as untrusted. Model output is escaped. CLI providers run with no tools and a stripped environment.
- CI runs ruff, pytest, pip-audit, bandit, a web build with Playwright tests, and gitleaks. Third-party actions are pinned to commit hashes.
