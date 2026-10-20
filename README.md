<p align="center"><img src="docs/assets/argus-hero.gif" alt="Argus" width="360"></p>

# Argus

Argus is a self-hosted code reviewer for GitHub pull requests. It reads a PR, asks a model for candidate bugs, and has a second skeptical pass try to refute each one. It then checks the survivors against your rules and posts only a few findings that held up.

You run it on your own machine and bring your own model: an API key, a local OpenAI-compatible server, or a Claude or ChatGPT subscription. Your code goes to the model you choose and nowhere else.

## How a review runs

1. Argus fetches the PR and renders each changed file with its post-change line numbers. Files up to 600 lines appear in full. Larger files appear as windows around the changes. The PR title, description and commit subjects come first.
2. The reviewer pass reports every candidate issue as structured JSON, with a category, severity, confidence, a verbatim evidence line and a suggested fix. `ARGUS_REVIEW_PASSES=2` or `3` adds gap-filling passes. They are off by default because they add many findings and few real ones.
3. A skeptical verifier checks each finding against the diff and drops what it cannot confirm.
4. Deterministic lanes run beside the model: ruff on changed Python files, a config lane for compose and YAML mistakes, and a small catalogue of known failure patterns. Ruff rules count only on lines the PR changed. Syntax errors and undefined names count anywhere in a changed file.
5. Findings are matched to a rule index (`data/rules/index.json`). A finding can cite a rule. It cannot invent one.
6. Argus ranks the findings and applies a cap. Critical and major findings are the bug-class output. Minor findings and nits are counted in the summary.

If the evidence line sits elsewhere in the file, Argus moves the finding to it.

## Quickstart with Docker

You need Docker with the compose plugin, a GitHub token and one model provider.

1. Clone the repository and create your settings file.

   ```bash
   git clone https://github.com/phj6688/argus-review.git
   cd argus-review
   cp .env.example .env
   ```

2. Generate a token and put it in `.env` as `ARGUS_API_TOKEN`.

   ```bash
   openssl rand -hex 32
   ```

3. Open `.env` and set three things:
   - `ARGUS_API_TOKEN`, the value from step 2. It must be at least 32 characters.
   - `GITHUB_TOKEN`, a fine-grained token with Contents:read and Pull requests:read on the repositories you want reviewed.
   - One provider. For example, `OPENROUTER_API_KEY=...`. See [Choosing a model provider](#choosing-a-model-provider).

4. Start the stack.

   ```bash
   docker compose up -d --build
   curl -fsS http://127.0.0.1:8350/health
   ```

   The health call returns `{"status":"ok", ...}` with the model Argus will use.

5. Open <http://127.0.0.1:3350> and sign in with `ARGUS_API_TOKEN`.

6. Run a first review. In the console, open Trigger and enter `owner/repo#123`. Or call the API:

   ```bash
   TOKEN=$(grep '^ARGUS_API_TOKEN=' .env | cut -d= -f2)
   curl -fsS -X POST http://127.0.0.1:8350/reviews \
     -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"repo": "owner/repo", "pr": 123}'
   ```

   The response holds a review `id`. A review takes a few minutes. Read the result with the verdict route:

   ```bash
   curl -fsS http://127.0.0.1:8350/reviews/1/verdict -H "Authorization: Bearer $TOKEN"
   ```

If the API container stops at startup, read `docker compose logs argus-api`. A missing or short `ARGUS_API_TOKEN` is the usual cause, and the message says so. If a review fails with "No model provider configured", no provider variable is set. Run `docker compose exec argus-api argus doctor` to see what Argus found.

Data lives in the `argus-data` volume. `docker compose down` keeps it. `docker compose down -v` deletes it.

## Quickstart without Docker

You need Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/phj6688/argus-review.git
cd argus-review
uv sync
cp .env.example .env     # then edit it, and export the values in your shell
```

Load the settings into your shell. This reads `.env` as plain `KEY=value` lines:

```bash
set -a; . ./.env; set +a
```

Check the provider, then review a PR from the command line:

```bash
uv run argus doctor
uv run argus review owner/repo 123
```

The CLI has these commands:

| Command | What it does |
| --- | --- |
| `argus doctor` | Checks the configured provider and makes one small model call. |
| `argus review OWNER/REPO PR` | Runs the full pipeline and prints the findings. |
| `argus compress OWNER/REPO PR` | Fetches a PR diff and shows how it fits the token budget. |
| `argus publish-preview REVIEW_ID` | Renders a stored review as the GitHub payload, without posting. |
| `argus publish-placement REVIEW_ID` | Lists where each finding would go and whether the line can take a comment. |

To run the API and the console on the host:

```bash
ARGUS_DB_PATH=./data/argus.db uv run uvicorn argus.api.main:app --host 127.0.0.1 --port 8350
```

```bash
cd web
npm ci
NEXT_PUBLIC_ARGUS_API=http://127.0.0.1:8350 npm run dev   # http://localhost:3000
```

The console reads `ARGUS_API_TOKEN` from its own environment. Without `NEXT_PUBLIC_ARGUS_API`, the console shows demo data and calls no API.

## Choosing a model provider

Set `ARGUS_PROVIDER` to pick a provider. With it empty, Argus chooses by the credentials it finds, in this order: gateway, anthropic, openai, openrouter. The two CLI providers always need `ARGUS_PROVIDER`.

| Provider | Select with | Variables | Default model | Notes |
| --- | --- | --- | --- | --- |
| `openrouter` | `OPENROUTER_API_KEY` | `OPENROUTER_API_KEY` | `openai/gpt-6.1-sol` | One key, many vendors. Model ids have a slash. |
| `anthropic` | `ANTHROPIC_API_KEY` | `ANTHROPIC_API_KEY` | `claude-sonnet-5-5` | Direct Anthropic API. |
| `openai` | `OPENAI_API_KEY` or `OPENAI_BASE_URL` | `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `ARGUS_OPENAI_TOKEN_PARAM` | `gpt-6.1-sol` | OpenAI, vLLM, Ollama, LM Studio. |
| `gateway` | `ANTHROPIC_BASE_URL` and `ANTHROPIC_AUTH_TOKEN` | the same two | `gpt-6.1-sol` | Your own Anthropic-compatible gateway. |
| `claude-cli` | `ARGUS_PROVIDER=claude-cli` | `CLAUDE_CODE_OAUTH_TOKEN`, `ARGUS_CLAUDE_CONFIG_DIR`, `ARGUS_CLAUDE_BIN` | `claude-sonnet-5-5` | Claude Pro or Max login. Host only. |
| `codex-cli` | `ARGUS_PROVIDER=codex-cli` | `ARGUS_CODEX_HOME`, `ARGUS_CODEX_BIN` | `gpt-6.1-sol` | ChatGPT login. Host only. |

Set `ARGUS_MODEL` to override the default. A model id with a slash always goes to OpenRouter, unless `ARGUS_PROVIDER=openai` is set. A model must support tool calls. Argus asks for its output through one forced tool call.

Run `argus doctor` after every change. It checks the credentials, calls the models endpoint where there is one, and makes one small model call. For OpenRouter it also checks that the model supports tools.

### OpenRouter (API key)

```bash
OPENROUTER_API_KEY=sk-or-...
ARGUS_MODEL=openai/gpt-6.1-sol
```

Any OpenRouter model that supports tool calls works. Pick the model id from the model page on openrouter.ai and keep the vendor prefix. Without `ARGUS_MODEL`, Argus uses the default in the table. If `ARGUS_MODEL` has no slash and OpenRouter is your only provider, Argus stops with an error that asks for a `vendor/model` id.

### Anthropic API key

```bash
ANTHROPIC_API_KEY=sk-ant-...
# ARGUS_MODEL=claude-sonnet-5-5
```

### OpenAI or an OpenAI-compatible server

```bash
OPENAI_API_KEY=sk-...
# OPENAI_BASE_URL=https://api.openai.com/v1
ARGUS_MODEL=gpt-6.1-sol
```

For a local server, set `OPENAI_BASE_URL` to its `/v1` address. Inside Docker, `localhost` is the container, so use `http://host.docker.internal:11434/v1` for Ollama on the host. Add `extra_hosts: ["host.docker.internal:host-gateway"]` to the service in a `docker-compose.override.yml` on Linux. Set `ARGUS_MODEL` to the name the server uses. Some older servers reject `max_completion_tokens`. For those, set `ARGUS_OPENAI_TOKEN_PARAM=max_tokens`. Small local models often fail at forced tool calls. Test with `argus doctor` first.

### Self-hosted Anthropic-compatible gateway

```bash
ANTHROPIC_BASE_URL=http://your-gateway:3000
ANTHROPIC_AUTH_TOKEN=...
ARGUS_MODEL=your-model-id
```

Both variables are required. The address must be reachable from where Argus runs. Inside Docker, it must be reachable from the container.

### Claude Pro or Max subscription (claude-cli)

Argus can call the official Claude Code CLI with your subscription login. It runs on the host, because the Docker image does not contain the CLI. Run the API with uv on the host.

1. Install the Claude Code CLI as its documentation describes.
2. Log in once with `claude`. Or create a long-lived token with `claude setup-token` and set `CLAUDE_CODE_OAUTH_TOKEN`.
3. Set the provider in `.env`:

   ```bash
   ARGUS_PROVIDER=claude-cli
   # ARGUS_CLAUDE_CONFIG_DIR=/home/you/.argus/claude   # a separate config directory
   ```

4. Run `uv run argus doctor`.

Every call runs in a sandbox. The CLI starts in an empty temporary working directory with safe mode on, so it loads no `CLAUDE.md`, hooks, skills or MCP servers. All tools are off. The prompt arrives on stdin. The subprocess sees only `HOME`, `PATH`, `CLAUDE_CODE_OAUTH_TOKEN` and the config directory. Your API keys and gateway settings are stripped from it.

### ChatGPT subscription (codex-cli)

Argus can call the official Codex CLI with your ChatGPT login. It also runs on the host only.

1. Install the Codex CLI as its documentation describes.
2. Create a dedicated Codex home and log in to it:

   ```bash
   mkdir -p ~/.argus/codex && CODEX_HOME=~/.argus/codex codex login
   ```

3. Set the provider in `.env`:

   ```bash
   ARGUS_PROVIDER=codex-cli
   ARGUS_CODEX_HOME=/home/you/.argus/codex
   ```

4. Run `uv run argus doctor`.

Use a dedicated home so your personal `AGENTS.md`, skills and settings stay out of review prompts. Doctor warns if it finds such files in `ARGUS_CODEX_HOME`. Each call runs with a read-only sandbox, no session state, user config and rules ignored, shell, plugin and browser tools disabled, and web search off. The working directory is empty.

### Terms and limits of subscription mode

- Anthropic's Claude Code legal page says that subscription login is for ordinary individual use of the unmodified Claude Code binary, and that products and services should use API keys: <https://code.claude.com/docs/en/legal-and-compliance>.
- OpenAI recommends API keys for automation: <https://learn.chatgpt.com/docs/auth>.

Use subscription mode for personal use on your own repositories. Use an API key for a team or an always-on deployment. Subscription rate windows can block reviews for hours. Set `ARGUS_FALLBACK_MODEL` to an API-key model so reviews continue. A model id with a slash goes to OpenRouter. `argus doctor` warns when the fallback would use the same subscription.

### Fallback, model list and judges

- `ARGUS_FALLBACK_MODEL` runs a second attempt when a review fails on the primary model. The switch is recorded on the review. It covers `POST /reviews` and the watcher. Choose a model from a different provider, or both fail together.
- `ARGUS_MODEL_OPTIONS` is a comma-separated list of models that the console config screen offers. `PUT /config` rejects any model that is not in the list, the default, or the stored model. Once you save the config screen, the stored model wins over `ARGUS_MODEL`.
- Judges are an optional second opinion. When `ARGUS_JUDGE_LIVE=1`, Argus asks a model from the other family (Claude or GPT) to judge each surviving bug-class finding. The finding then carries an evidence tier: `deterministic`, `consensus`, `single_critic` or `disputed`. A disputed finding is kept and never blocks a change on its own. Judges use a chat completions URL. An OpenRouter example:

  ```bash
  ARGUS_JUDGE_LIVE=1
  ARGUS_JUDGE_BASE_URL=https://openrouter.ai/api/v1/chat/completions
  ARGUS_JUDGE_ANTHROPIC_MODEL=anthropic/claude-sonnet-5-5
  ARGUS_JUDGE_ANTHROPIC_KEY=sk-or-...
  ARGUS_JUDGE_OPENAI_MODEL=openai/gpt-6.1-sol
  ARGUS_JUDGE_OPENAI_KEY=sk-or-...
  ```

  Each judge call adds cost. Configure both families, so a judge exists whatever the reviewer is.

## GitHub setup

Argus needs a read token. It can also post reviews, and then it needs a second token.

**Read token.** Create a fine-grained personal access token with Contents:read and Pull requests:read on the repositories you review. Put it in `GITHUB_TOKEN`. Argus uses it to fetch diffs, files and each repository's `CLAUDE.md` house rules.

**Posting.** Publishing is off by default. To turn it on:

1. Create a separate GitHub account for the bot, or a GitHub App. A review written by the PR author's own account reads as self-review.
2. Give its token Pull requests:write on the allowlisted repositories. Set it as `ARGUS_PUBLISH_TOKEN`. Argus never uses `GITHUB_TOKEN` to write.
3. Set `ARGUS_PUBLISH_REPOS=owner/repo,owner/other`. Empty means no posting, dry-run included.
4. Set `ARGUS_PUBLISH=dry-run`. Complete a review. Read it with `argus publish-preview REVIEW_ID`, or read the stored `publish_payload`.
5. Set `ARGUS_PUBLISH=1` when the output looks right.

Argus posts with `event=COMMENT`. The verdict appears in the summary. `ARGUS_PUBLISH_MIN_SEVERITY` (default `minor`) and `ARGUS_PUBLISH_MAX_COMMENTS` (default `4`) limit the noise. Findings over the cap go into the review body. Fingerprint markers stop duplicate comments across commits. Set `ARGUS_PUBLISH_BOT_LOGIN` to the bot's login, or leave it empty to read it from the token.

With publishing live, the reply loop is on. It re-checks a finding when the flagged lines change. It replies when a fix is confirmed and resolves the thread. It replies to author requests that start with "recheck", "re-check" or "check again", or that mention the bot. Set `ARGUS_REPLY_LOOP=0` to stop it. Details are in [docs/publishing.md](docs/publishing.md).

**PR watcher.** The watcher polls GitHub and queues every new PR head for review. Set `ARGUS_WATCH_ENABLED=1` and `ARGUS_WATCH_OWNERS=your-user-or-org` to start it.

- Auto mode (default): the watcher covers the owner's repositories that were pushed in the last `ARGUS_WATCH_ACTIVE_DAYS` days (90). Archived repositories are excluded. In the console, you can switch a repository on or off.
- Manual mode (`watch_mode: "manual"` through `PUT /config`): only repositories you mark as watched are covered. `ARGUS_AUTODISCOVER=1` adds newly found repositories to the list as unwatched.
- Only PRs from an OWNER, MEMBER or COLLABORATOR are reviewed. Set `ARGUS_WATCH_TRUSTED_ONLY=0` to change that. Keep the default on public repositories.
- Drafts and bot authors are skipped unless `ARGUS_WATCH_INCLUDE_DRAFTS` or `ARGUS_WATCH_INCLUDE_BOTS` is truthy.
- The watcher runs `ARGUS_REVIEW_WORKERS` reviews in parallel (2). `ARGUS_LLM_MAX_CONCURRENCY` (3) caps model requests across all of them.
- A failed head is retried after 2, 10 and 30 minutes, then parked after `ARGUS_WATCH_MAX_ATTEMPTS` (4) failures. A new commit gets a new budget. The watcher never reviews a finished head twice.
- A PR merged between two polls can get a post-merge review (`ARGUS_WATCH_FAST_MERGES=1`, window 60 minutes).
- Argus pauses GitHub reads when fewer than `ARGUS_GITHUB_MIN_REMAINING` (500) core API requests remain.
- `GET /stats` shows queue, quota and coverage state under `ops`.

## Security model

Read [SECURITY.md](SECURITY.md) for the full policy and a hardening checklist.

- **Token auth by default.** `ARGUS_AUTH_MODE=token` needs `ARGUS_API_TOKEN` of 32 characters or more. The API refuses to start without it. The console has a login page that uses the same token.
- **Proxy mode** (`ARGUS_AUTH_MODE=proxy`) turns the token check off. Use it only behind a reverse proxy that authenticates every request. See [DEPLOY.md](DEPLOY.md).
- **Loopback ports.** Compose binds the API to `127.0.0.1:8350` and the console to `127.0.0.1:3350`.
- **PR content is untrusted.** A PR can contain text that tries to steer the model. Comment output is escaped. The CLI providers run with no tools. Tokens never enter prompts.
- **Trusted authors.** The watcher skips PRs from outsiders by default.
- **Hard exclusions.** `ARGUS_DENY_OWNERS` lists owners or `owner/repo` entries that Argus never reviews or watches. The console cannot remove them.
- **Spend.** Argus makes several model calls per review. Set a spend cap at your provider.

## MCP server

`argus-mcp` is a stdio MCP server that lets a coding agent call Argus as a tool. It is a thin client of the running API and holds no state. It exposes four tools:

| Tool | Purpose |
| --- | --- |
| `review_diff` | Review a unified diff without a PR. `mode=full` runs the whole pipeline. `mode=fast` runs only the deterministic probes and never returns `approve`. |
| `review_pr` | Start a review of an open PR. |
| `get_verdict` | Fetch the verdict document for a review: findings, fingerprints, coverage and evidence tiers. |
| `mark_finding` | Label a finding `real`, `fp`, `stale` or `unsure` after you act on it. |

A verdict is `insufficient_evidence` when a run found nothing but did not read the code. Treat that as "not checked", not as a pass.

Register it with an MCP client. `uv sync` installs the `argus-mcp` command:

```json
{
  "mcpServers": {
    "argus": {
      "command": "/path/to/argus-review/.venv/bin/argus-mcp",
      "env": {
        "ARGUS_API": "http://127.0.0.1:8350",
        "ARGUS_API_TOKEN": "<your token>"
      }
    }
  }
}
```

With Claude Code, the equivalent is `claude mcp add argus -e ARGUS_API_TOKEN=<token> -- /path/to/argus-review/.venv/bin/argus-mcp`. To run the server inside the API container instead, use `docker exec -i -e ARGUS_API=http://127.0.0.1:8000 argus-api /opt/venv/bin/argus-mcp` as the command. The container already holds the token.

## Configuration reference

[`.env.example`](.env.example) lists every setting with a short comment. These matter most:

| Variable | Default | Purpose |
| --- | --- | --- |
| `ARGUS_AUTH_MODE` | `token` | `token` or `proxy`. |
| `ARGUS_API_TOKEN` | none | Shared secret, 32 characters or more. |
| `ARGUS_PROVIDER` | auto | Model provider. |
| `ARGUS_MODEL` | provider default | Reviewer model. |
| `ARGUS_FALLBACK_MODEL` | none | Second attempt on failure. |
| `ARGUS_MODEL_OPTIONS` | none | Models the console offers. |
| `GITHUB_TOKEN` | none | Read token. |
| `ARGUS_WATCH_ENABLED` | off | Start the PR watcher. |
| `ARGUS_WATCH_OWNERS` | none | Owners the watcher may cover. |
| `ARGUS_WATCH_TRUSTED_ONLY` | `1` | Review only trusted authors. |
| `ARGUS_DENY_OWNERS` | none | Owners or repos that are never reviewed. |
| `ARGUS_PUBLISH` | `0` | `0`, `dry-run` or `1`. |
| `ARGUS_PUBLISH_REPOS` | none | Repositories Argus may post to. |
| `ARGUS_PUBLISH_TOKEN` | none | Write token of the bot account. |
| `ARGUS_PUBLISH_MAX_COMMENTS` | `4` | Inline comments per review. |
| `ARGUS_DB_PATH` | `./data/argus.db` | SQLite file. |
| `ARGUS_PUBLIC_API_URL` | `http://localhost:8000` | Public URL of the API. |

## Development

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check src tests
```

```bash
cd web
npm ci
npm run lint
npx tsc --noEmit
npm run build
npx playwright install chromium   # once
npx playwright test               # console tests against demo data
npm run test:auth                 # login tests
```

GitHub Actions runs these checks on every PR to `dev` and `master`:

- ruff on `src` and `tests`
- pytest on Python 3.11, 3.12 and 3.13
- `pip-audit` on the locked dependencies and `bandit` on `src`
- web lint, type check and build, plus the Playwright tests
- gitleaks on the full history
- a sanity job, and one aggregate job named "CI passed"

More docs:

- [ARCHITECTURE.md](ARCHITECTURE.md): how the pipeline is built.
- [DEPLOY.md](DEPLOY.md): self-hosting behind a reverse proxy, upgrades and backups.
- [docs/publishing.md](docs/publishing.md): posting, the reply loop and acceptance tracking.
- [docs/evaluation.md](docs/evaluation.md): offline replay and scoring.
- [CONTRIBUTING.md](CONTRIBUTING.md): how to send a change.

## License

MIT. See [LICENSE](LICENSE).
