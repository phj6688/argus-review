# Changelog

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
