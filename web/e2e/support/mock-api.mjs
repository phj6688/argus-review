import http from "node:http";

export const gitlab = {
  host: "gitlab.test", baseUrl: "https://gitlab.test/gitlab", token: "configured",
  identity: { status: "ok", username: "review-bot", bot: true },
  publish: { mode: "dry-run", projectCount: 2 }, allowCount: 3, refusal: null,
};
const rows = [
  ["github-infra", "acme/infra", 218, "Add auth rate-limit cache"],
  ["github-web", "acme/web", 142, "Bind metrics endpoint to loopback only"],
  ["gitlab-app", "gitlab:gitlab.test#42", 7, "group/sub/app #7"],
  ["gitlab-api", "gitlab:gitlab.test#43", 8, "team/api #8"],
  ["gitlab-key", "gitlab:gitlab.test#44", 9, "gitlab:gitlab.test#44 #9"],
  ["gitlab-other", "gitlab:other.test#50", 3, "team/other #3"],
].map(([id, repo, pr, title]) => ({
  id, repo, pr, title, model: "review-model", model_used: "review-model", status: "done",
  counts: { critical: 0, major: 0, minor: 0, nit: 0 }, action: "dry_run", age: "2m",
}));
const details = Object.fromEntries(rows.map((row) => [row.id, {
  id: row.id, repo: row.repo, pr: row.pr, title: row.title,
  model: row.model, model_used: row.model_used, status: "done", time: "2m ago",
  decision: "comment", bugs: [], nits: [], thread: [],
}]));
const integrations = {
  health: [{ label: "REST API", status: "healthy", state: "ok" }], warning: null,
  github: { token: "configured", watcher: { enabled: false, owners: [], intervalSeconds: 60, watchedRepos: 0 }, publishing: { mode: "off", repoCount: 0, tokenConfigured: false } },
  gitlab,
  api: { baseUrl: "http://127.0.0.1:4318", auth: "proxy", endpoints: [{ method: "GET", path: "/reviews", description: "Review list" }] },
  mcp: { status: "stdio", config: "{}", tools: [], resources: [] },
};
const config = {
  repos: [], models: [{ name: "Reviewer", caption: "Deep review", tier: "deep", model: "review-model" }],
  publish: { mode: "off", source: "env", botLogin: null, tokenConfigured: false, repos: [] },
  hiddenCount: 0, totalRepos: 0, modelOptions: ["review-model"], bugCap: 5, minConfidence: 0.8,
};
http.createServer((request, response) => {
  const path = new URL(request.url, "http://127.0.0.1:4318").pathname;
  const body = request.method !== "GET" ? null : path === "/health" ? { status: "ok" }
    : path === "/reviews" ? { reviews: rows, live: null }
    : path === "/integrations" ? integrations
    : path === "/config" ? config
    : path.startsWith("/reviews/") ? details[path.slice(9)] : null;
  response.writeHead(body ? 200 : 404, { "content-type": "application/json" });
  response.end(JSON.stringify(body ?? { error: "Not found" }));
}).listen(4318, "127.0.0.1");
