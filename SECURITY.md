# Security policy

## Report a vulnerability

Use GitHub private vulnerability reporting. Open the repository's Security tab and choose "Report a vulnerability". Do not open a public issue or a pull request for a vulnerability, and do not post exploit details in a discussion.

Include the version or commit, the steps to reproduce, and what an attacker gains. A short proof of concept helps. Do not include real tokens or private repository content.

Argus is maintained by one person, so replies are best effort. Fixes for confirmed issues ship on `dev`, then `master`. The advisory credits you unless you ask otherwise.

## Supported versions

Argus has no versioned releases yet. Only the latest commit on `master` receives fixes.

## In scope

- Authentication bypass on the API or the console, in `token` or `proxy` mode.
- A way to read or write data without the API token.
- Prompt injection from PR content that makes Argus leak a secret, run a command, post to a repository outside the allowlist, or write attacker-controlled markup into a GitHub comment.
- Path traversal or request forgery through a diff, a repository name, or any API field.
- Secrets in logs, error messages, prompts, stored payloads or comments.
- Weak defaults in `docker-compose.yml` or `.env.example` that expose a service.
- Supply chain problems in the build: unpinned actions, unlocked dependencies.

## Out of scope

- Findings that need an attacker to hold your `ARGUS_API_TOKEN`, your GitHub tokens, or shell access to the host.
- A deployment that sets `ARGUS_AUTH_MODE=proxy` without an authenticating proxy, or that publishes the ports on a public interface.
- Wrong or missing review findings. These are quality bugs. Open a normal issue.
- Denial of service through large PRs or through your own provider quota.
- Vulnerabilities in a model provider, the Claude Code CLI, the Codex CLI, or GitHub itself.

## How Argus limits the damage

- Token auth is the default. The API refuses to start without a token of 32 characters or more.
- Compose binds both ports to `127.0.0.1`.
- PR text is untrusted. Model output is rendered as literal text with inert links. The CLI providers run with no tools, an empty working directory and a stripped environment. Tokens never enter prompts.
- The watcher reviews only trusted authors by default. `ARGUS_DENY_OWNERS` excludes owners and repositories for good.
- Posting needs its own write token, an allowlist of repositories, and an opt-in switch. It starts in dry-run.
- CI runs `pip-audit`, `bandit` and `gitleaks`. Third-party actions are pinned to commit hashes.

## Hardening checklist for operators

1. Keep `ARGUS_AUTH_MODE=token` unless an authenticating reverse proxy is the only way to reach the containers.
2. Generate `ARGUS_API_TOKEN` with `openssl rand -hex 32`. Rotate it if it leaks.
3. Never publish the API and console ports (8350 and 3350 by default, `ARGUS_API_PORT` and `ARGUS_WEB_PORT`) on a public interface. Keep the `127.0.0.1` bindings and put a reverse proxy with TLS and authentication in front.
4. Use fine-grained GitHub tokens. `GITHUB_TOKEN` needs Contents:read and Pull requests:read on the repositories you review, nothing more.
5. Use a separate bot account for posting, with Pull requests:write on the allowlisted repositories only.
6. Start with `ARGUS_PUBLISH=dry-run` and read the stored payloads before you set `ARGUS_PUBLISH=1`.
7. Keep `ARGUS_WATCH_TRUSTED_ONLY=1`, especially on public repositories.
8. Set `ARGUS_DENY_OWNERS` for any organization whose code must never reach a model provider.
9. Set a spend cap and alerts at your model provider. One review makes several model calls.
10. For the CLI providers, use a dedicated Codex home (`ARGUS_CODEX_HOME`) and a separate Claude config directory (`ARGUS_CLAUDE_CONFIG_DIR`), so personal instructions and skills stay out of review prompts.
11. Do not store `.env` in version control. Back up the `argus-data` volume. It holds review text from your code.
12. After you put the console behind a proxy, run `ARGUS_HOST=your.host ./verify-gate.sh` from outside your network. It expects 302, 401 or 403 on the root, `/reviews` and `/config`.
13. Keep images and dependencies current. Dependabot is configured for pip, npm and GitHub Actions.
