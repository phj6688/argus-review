# Contributing

Thank you for helping. This page is short on purpose.

## Set up

You need Python 3.11 or newer, [uv](https://docs.astral.sh/uv/), and Node 22 for the console.

```bash
uv sync --extra dev
cd web && npm ci
```

## Check your change

```bash
uv run pytest -q
uv run ruff check src tests
cd web
npm run lint && npx tsc --noEmit && npm run build
npx playwright install chromium && npx playwright test && npm run test:auth
```

CI runs the same checks. Tests must not call a real model or the real GitHub API. Patch the client, as the existing tests do.

## Send a change

- Branch from `dev`, and open the pull request against `dev`. `master` receives merges from `dev` only.
- Keep a PR to one topic. Add or update a test for each behavior change.
- Write a short imperative commit subject, for example `fix timeout on graph queries`. Add a body only when the reason is not obvious.
- Comments explain why, not what.
- Update `README.md`, `.env.example` and `CHANGELOG.md` when you change a setting or a behavior users can see.
- Do not put secrets, tokens, private repository content or real review output in code, tests, issues or PRs. Use `example/app`-style names in fixtures.

Report a security problem through the process in [SECURITY.md](SECURITY.md), not in an issue.

## Add a model provider

1. Add the name and its default model to `src/argus/providers.py`. Extend `auto_provider` if it can be chosen from credentials alone.
2. Implement the request in `src/argus/client.py`. It must return one structured tool result, as the other providers do. A subprocess provider belongs in `src/argus/cli_providers.py` and must run with no tools and a stripped environment.
3. Add a check to `src/argus/doctor.py`.
4. Add tests next to `tests/test_doctor.py` and the provider tests, then document the variables in `.env.example` and the README provider table.

## Add a rule

Rules give a finding something to cite. The built-in set is in `src/argus/rules.py`, and `data/rules/index.json` extends it. An entry has an `id`, a one-line `summary` and a `source`. Keep ids lowercase with hyphens. Add a rule only if a reviewer can check it against a diff. Users can also add their own file through `ARGUS_RULES_PATH` without a code change.

For a deterministic check, see the lanes in `src/argus/jarvis/`: `ruff_lane.py`, `config_lane.py` and `probes.py`. A new check needs a test that shows it fires on a bad example and stays quiet on a good one.
