# Self-hosting Argus

Argus runs as two containers: a FastAPI API (`argus-api`) and a Next.js console
(`argus-web`). This page covers running them for real use: authentication,
a reverse proxy, data, upgrades and optional files. The quick start is in the
[README](README.md).

## Before you start

- Docker with the compose plugin.
- A model provider (see "Choosing a model provider" in the README and `.env.example`).
  The Docker image cannot use the `claude-cli` or `codex-cli` providers. Run the API
  on the host with uv for those.
- A GitHub token with read access to the repositories you want reviewed
  (Contents: read, Pull requests: read). A fine-grained token scoped to those
  repositories is preferred over a classic `repo` token, which also grants write.

## Run it

```bash
cp .env.example .env     # set ARGUS_API_TOKEN, GITHUB_TOKEN and one model provider
docker compose up -d --build
curl -fsS http://127.0.0.1:8350/health
```

The compose file publishes the API on `127.0.0.1:8350` and the console on
`127.0.0.1:3350`. Keep both bound to loopback: nothing should reach them except
your own reverse proxy.

## Authentication

Two modes, set with `ARGUS_AUTH_MODE`:

- `token` (default). Every API route needs `Authorization: Bearer $ARGUS_API_TOKEN`,
  except the shallow `/health`. The console shows a login page and asks for the same
  token. The token must be at least 32 characters. Generate one with
  `openssl rand -hex 32`. The API will not start without it.
- `proxy`. Argus checks nothing and trusts a reverse proxy that authenticates every
  request. Use it only when such a proxy is the sole way to reach the containers. A
  mistake here leaves the API open, so test the gate (see below).

## Put it behind a reverse proxy

Token mode is enough for a single user on a trusted network. For anything wider,
put a reverse proxy in front (Caddy, nginx, Traefik, a Cloudflare Tunnel with
Access, or similar), terminate TLS there, and keep both ports on loopback. In
proxy mode the proxy must require authentication on every route.

Set `ARGUS_PUBLIC_API_URL` to the external URL and `ARGUS_CORS_ORIGINS` to the
console origin. If your proxy serves the API under a path prefix such as `/api/v1`,
build the console with `NEXT_PUBLIC_ARGUS_CLIENT_API=/api/v1` so browser calls use it.

- `/health` is a shallow liveness check. `/health?deep=1` makes a real model call.
  The API allows `deep=1` for loopback callers or with the `ARGUS_HEALTH_TOKEN` header
  after API authentication. A reverse proxy on the same host connects over loopback, so
  in proxy mode an anonymous `deep=1` request through that proxy would pass. Either keep
  `/health` behind auth, or make the proxy match `/health` exactly and reject or strip
  any query string on the unauthenticated route.
- Do not use an IP allowlist as the gate. Behind a proxy the socket peer is the
  proxy, so a rule that trusts private ranges becomes a public bypass.

Then confirm the gate from outside your network, with no cookies:

```bash
ARGUS_HOST=argus.example.com ./verify-gate.sh   # expects 302/401/403 on the root and on /reviews and /config
```

The script also probes the data routes `/reviews` and `/config` anonymously. A
gate that covers only the root page is not a gate. If the API sits on a different
host or under a path prefix, set `ARGUS_API_URL` (for example
`https://api.example.com` or `https://argus.example.com/api/v1`) and the same
routes are checked there.

## Data and volumes

SQLite lives in the `argus-data` named volume, mounted at `/data`
(`ARGUS_DB_PATH=/data/argus.db`). It survives `docker compose up --build` and
`docker compose down`. `docker compose down -v` deletes it. Back it up with the
SQLite online backup API or by copying the file while the API is stopped.

Optional files mount read-only through a `docker-compose.override.yml` (it stays out
of version control), each selected by an environment variable:

| Variable | File | Default |
| --- | --- | --- |
| `ARGUS_RULES_PATH` | JSON list of `{id, summary, source}` rule entries merged over the built-in rules | built-in rules |
| `ARGUS_KNOWN_FPS_PATH` | JSONL of known false positives to suppress (format: `data/fixtures/known_fps.example.jsonl`) | suppression off |
| `ARGUS_BENCHMARK_BASELINES_PATH` | JSON list of per-repo baseline rows for the benchmark screen | empty |
| `ARGUS_CR_THREADS_PATH` | Review-thread corpus from another reviewer (JSONL) for the benchmark screen | none |

Example override:

```yaml
services:
  argus-api:
    environment:
      ARGUS_KNOWN_FPS_PATH: /private/known_fps.jsonl
    volumes:
      - /srv/argus-private/known_fps.jsonl:/private/known_fps.jsonl:ro
```

The same override file is the place for site-specific compose changes: an existing
volume or external network, extra mounts, or `extra_hosts` for a model server on the
host. For example, to keep the database in a volume that already exists and join a
shared network:

```yaml
services:
  argus-api:
    networks: [default, shared-net]
volumes:
  argus-data:
    name: my-existing-volume
    external: true
networks:
  shared-net:
    external: true
```

## Upgrade

```bash
git pull
docker compose up -d --build --force-recreate
docker compose logs --since=1m argus-api
```

Rebuild rather than restart so the image carries the new code. The API creates
its SQLite schema on start.

## Roll back

```bash
git checkout <previous-tag-or-sha>
docker compose up -d --build --force-recreate
```

## Optional pieces

- `ARGUS_JUDGE_LIVE=1` turns on the live second-opinion lane. It uses the
  `ARGUS_JUDGE_*` variables from `.env.example` and adds one model call per
  surviving bug-class finding.
- The MCP server is a stdio command. Run it on the host (`uv sync`, then register
  `.venv/bin/argus-mcp` with your MCP client, with `ARGUS_API_TOKEN` in its
  environment), or inside the API container with
  `docker exec -i -e ARGUS_API=http://127.0.0.1:8000 argus-api /opt/venv/bin/argus-mcp`.
  `ARGUS_API` overrides the default `http://127.0.0.1:8350`.
- Scheduled evaluation wrappers live in `deploy/cron/`. They read their paths and
  notification settings from environment variables; see the comments at the top
  of each script. The weekly replay runs in a container on `ARGUS_EVAL_NETWORK`
  (default `bridge`). If your model endpoint is a container hostname on a compose
  network, set `ARGUS_EVAL_NETWORK` to that network, or the hostname will not resolve.
