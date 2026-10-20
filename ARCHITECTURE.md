# Architecture

Argus is a polling service and a review engine. It reads pull requests from GitHub, runs a model-backed pipeline plus a few deterministic lanes, stores the result in SQLite, and can post a short review back. A console and a stdio MCP server sit on top of the same API.

## Design principles

1. **Generate broadly, verify hard, post few.** The common failure of an LLM reviewer is noise: it flags correct code. So the reviewer pass reports every candidate with severity and confidence, and a separate skeptical verifier does the filtering. One pass is never asked to be both thorough and quiet.
2. **Ground every finding.** Output follows a strict JSON schema. A finding may cite a rule from the rule index. It cannot invent one. An unknown rule id reads as ungrounded.
3. **Say what was looked at.** A review that found nothing and a review that read nothing are different results. The verdict carries a coverage record next to the findings.
4. **Treat PR content as hostile.** Titles, descriptions, diffs and file contents can carry prompt injection. They never get tools, tokens or markup that renders.
5. **Measure before you change.** Prompt, context and verifier changes are scored offline on a fixed case set (see [docs/evaluation.md](docs/evaluation.md)).

## The pipeline

```
GitHub PR (or raw diff)
        |
        v
[1] Context       diff.py, context.py
    fetch patches, render numbered post-change views, pack to a token budget
        |
        v
[2] Review        review.py, client.py, providers.py
    reviewer pass -> structured findings (optional extra passes)
        |
        v
[3] Verify        review.py
    skeptical pass tries to refute each finding; unconfirmed ones are dropped
        |
        v
[4] Lanes         jarvis/ (ruff_lane, config_lane, probes, judges)
    deterministic checks, optional second-opinion judge
        |
        v
[5] Ground, rank, cap     rules.py, review.py
    rule index check, dedupe, order by severity then confidence, bug cap
        |
        v
[6] Store         api/db.py (SQLite)
        |
        v
[7] Publish       publish.py, replies.py, acceptance.py   (off by default)
    one GitHub review, replies to authors, outcome tracking
```

**Context.** Files up to 600 lines appear whole. Larger files appear as windows around the changes. Files without a patch appear as hunks. The PR title, description and commit subjects come first. House rules come from the repository's `CLAUDE.md`, capped at 6000 characters.

**Review.** The client speaks to six providers (`gateway`, `anthropic`, `openrouter`, `openai`, `claude-cli`, `codex-cli`). `providers.py` chooses the provider and the default model. `cli_providers.py` runs the two subscription CLIs in a sandbox. Every call asks for one structured result through one forced tool call.

**Verify.** The verifier sees the compressed diff and each finding. A finding it cannot confirm is dropped. The verifier variant is set by `ARGUS_VERIFY_VARIANT`.

**Lanes.**
- The ruff lane runs ruff on changed Python files. A rule counts only when it touches a changed line. Syntax errors and undefined names count anywhere in a changed file.
- The config lane checks compose and YAML files for a few failure classes, for example an environment variable that shadows a Dockerfile default.
- The probe lane is a small catalogue of known failure patterns, matched by file name. On most diffs it runs zero checks. It can surface a known pattern but can never clear a change.
- The judge lane (`ARGUS_JUDGE_LIVE=1`) asks a model from a different family than the reviewer's for a second opinion on each surviving bug-class finding. Same-family judges tend to repeat the reviewer's mistakes.

**Rank and cap.** Critical and major findings are the bug class, capped by the bug cap (default 10). Minor findings and nits are counted in the summary.

## Processes

| Part | Where | Role |
| --- | --- | --- |
| API | `src/argus/api/main.py` | FastAPI routes: reviews, verdicts, stats, config, reports, benchmark. Runs reviews as background tasks. |
| Watcher | `src/argus/api/watcher.py` | Polls GitHub, queues PR heads durably, runs a bounded pool of review workers. Off unless `ARGUS_WATCH_ENABLED` is set. |
| Console | `web/` | Next.js app with a token login. Reads the API on the server side. Shows demo data when no API is configured. |
| MCP server | `src/argus/mcp_server.py` | Stdio server. Each tool is one HTTP call to the API. Holds no state. |
| CLI | `src/argus/cli.py` | `doctor`, `review`, `compress`, `publish-preview`, `publish-placement`. |

State lives in one SQLite file (`ARGUS_DB_PATH`) with WAL mode. The design assumes one API instance per database.

The watcher keeps a durable queue of PR heads with an attempt history. It retries a failed head with backoff, parks it after a set number of failures, and never reviews a finished head twice. A shared semaphore caps model requests across reviews and judges. A quota-aware GitHub client pauses reads when the rate limit runs low.

## Agent-facing evidence surface

Merge gates and coding agents read reviews as evidence. The surface has three parts.

**`POST /review-diff`** reviews a change that need not belong to a PR. Send unified `git diff` text, or `repo` with `base_sha` and `head_sha`. `mode=full` (default) runs the whole pipeline in the background. `GET /reviews/{id}/verdict` serves the result and also works for PR reviews. `mode=fast` runs only the deterministic probe lane, synchronously, with no model. Read it as a tripwire, never as a gate.

**The verdict document** answers two independent questions: what was found and what was looked at.
- `coverage` has one record per lane (`ran`, `skipped` or `error`, the checks run and the reason for a skip) and a derived level. `full` needs the reviewer lane to have run and to have read every changed file. Any omitted file caps the level at `partial`. A deterministic lane that ran zero checks does not lift the level off `none`.
- `verdict` is `approve`, `comment`, `changes_requested` or `insufficient_evidence`. A run with no findings returns `insufficient_evidence` unless coverage supports a pass. The value sits outside the old three on purpose, so a consumer that only knows those fails closed. Findings count in the other direction without condition: a probe hit returns `changes_requested` however thin the lane set.
- `engine.reviewer_model` names the model that actually ran. It differs from the configured model when the fallback fired.
- Every finding has a stable `fingerprint`, a hash of the rule or normalized title, the path and the flagged line content, never the line number, so identity survives rebases. Each finding also has a `lane` (`probe` or `llm`), an optional judge opinion and an `evidence_tier`: `deterministic` (probe) over `consensus` (reviewer and judge agree) over `single_critic` (reviewer alone) over `disputed` (judge says false positive; kept, and does not request changes alone).
- `baseline` fingerprints in a request come back as per-finding `baseline_state` (`new` or `unchanged`) plus a `resolved` list. That answers "did my fix clear the finding".

**`argus-mcp`** exposes `review_diff`, `review_pr`, `get_verdict` and `mark_finding` to a local agent. Agent marks carry `marker=agent:<name>`, so they stay out of human precision numbers.

## Threat model in short

- The API is for one trusted operator. Token mode checks a shared secret on every route except shallow `/health`. Proxy mode trusts the network in front of it.
- The model sees untrusted text. It has no tools through the CLI providers, and Argus puts no secret in a prompt.
- Comment output is escaped before it reaches GitHub. URLs in model text are made inert.
- Paths from diffs and API input are checked before they touch the file system or a GitHub URL.
- Posting uses a separate token with write access only on allowlisted repositories.

See [SECURITY.md](SECURITY.md) for the reporting policy and the operator checklist.
