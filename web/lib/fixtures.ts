/* Demo fixtures, shown when no API is configured.
   These mirror the exact shapes the FastAPI backend will serve, so
   lib/api.ts can flip from fixtures to live fetch with no UI change. */

import type {
  Stats,
  ReviewSummary,
  LiveReview,
  ReviewDetail,
  Report,
  ReportPeriod,
  Benchmark,
  ConfigData,
  Integrations,
} from "./types";

/* The one fully-authored PR the detail screen showcases. Every review
   row opens it until the backend serves per-PR detail. */
export const SHOWCASE_ID = "acme-web-142";

export const stats: Stats = {
  prsReviewed: { value: 62, deltaPct: 9 },
  openFindings: { total: 148, critical: 4, major: 17, minor: 39, nit: 88 },
  bugVsNit: { bug: 21, nit: 127 },
  actionRate: { pct: 35, prevPct: 38 },
  avgComments: { value: 3.9, prev: 5.2 },
  trends: {
    reviewsPerDay: {
      value: 8.9,
      deltaPct: 14,
      series: [4, 6, 5, 9, 8, 12, 10, 14, 11, 16, 13, 17, 15, 18],
    },
    findingsPerDay: {
      value: 21,
      deltaPct: -5,
      series: [19, 15, 22, 13, 18, 11, 17, 9, 16, 12, 20, 14, 21, 18],
    },
  },
};

export const liveReview: LiveReview = {
  repo: "acme/worker",
  pr: 88,
  title: "Stream large exports without buffering",
  model: "gpt-6.1-sol",
  stage: "deep review",
  elapsed: "18s",
};

type RawRow = [
  repo: string,
  pr: number,
  title: string,
  model: string,
  age: string,
  critical: number,
  major: number,
  minor: number,
  nit: number,
  action: ReviewSummary["action"],
];

const rawReviews: RawRow[] = [
  ["acme/infra", 218, "Add auth rate-limit cache", "gpt-6.1-sol", "3m", 0, 1, 0, 7, "acted"],
  ["acme/web", 142, "Bind metrics endpoint to loopback only", "gpt-6.1-sol", "22m", 1, 1, 0, 4, "acted"],
  ["acme/worker", 87, "Refactor job queue backpressure", "claude-sonnet-5-5", "1h", 0, 0, 0, 9, "merged"],
  ["acme/billing", 54, "Fix off-by-one in invoice proration", "gpt-6.1-sol", "2h", 0, 1, 0, 3, "acted"],
  ["acme/auth", 31, "Rotate signing keys on schedule", "gpt-6.1-sol", "3h", 0, 2, 0, 1, "merged"],
  ["acme/tooling", 209, "shell: guard against unset PATH", "gpt-5.6-terra", "5h", 0, 0, 0, 2, "merged"],
  ["acme/docs", 76, "Migrate docs site to static export", "claude-sonnet-5-5", "6h", 0, 0, 2, 5, "pending"],
  ["acme/api", 19, "Cache config lookups per request", "gpt-6.1-sol", "8h", 0, 1, 1, 2, "acted"],
  ["acme/worker", 85, "Add retry/backoff to webhook delivery", "gpt-6.1-sol", "11h", 0, 0, 1, 4, "merged"],
  ["acme/web", 140, "Tighten proxy TLS cipher suite", "gpt-6.1-sol", "1d", 1, 0, 1, 2, "acted"],
  ["acme/infra", 63, "Alert on cert expiry under 7 days", "claude-sonnet-5-5", "1d", 0, 1, 0, 3, "merged"],
  ["acme/tooling", 207, "editor: lazy-load language servers", "gpt-5.6-terra", "2d", 0, 0, 0, 6, "merged"],
];

export const reviews: ReviewSummary[] = rawReviews.map((r) => ({
  id: `${r[0].replace("/", "-")}-${r[1]}`,
  repo: r[0],
  pr: r[1],
  title: r[2],
  model: r[3],
  age: r[4],
  counts: { critical: r[5], major: r[6], minor: r[7], nit: r[8] },
  action: r[9],
}));

export const reviewDetail: ReviewDetail = {
  id: SHOWCASE_ID,
  repo: "acme/web",
  pr: 142,
  title: "Bind metrics endpoint to loopback only",
  branch: "fix/metrics-bind",
  author: "acme-dev",
  model: "gpt-6.1-sol",
  model_used: "gpt-6.1-sol",
  fallback_from: null,
  fallback_reason: null,
  status: "done",
  time: "22m ago",
  filesChanged: 3,
  added: 47,
  removed: 12,
  decision: "changes_requested",
  bugs: [
    {
      finding: {
        file: "src/metrics/server.py",
        line: 38,
        severity: "critical",
        confidence: 0.92,
        title: "Metrics endpoint binds to all interfaces",
        description:
          "The Prometheus exporter binds 0.0.0.0, exposing internal metrics on the LAN. House rule requires loopback binding behind the auth gate; nothing in this PR re-fronts the port.",
        rule_id: "loopback-binding",
        proposed_fix: [
          '  def serve(self):',
          '-     app.run(host="0.0.0.0", port=9090)',
          '+     app.run(host="127.0.0.1", port=9090)',
        ].join("\n"),
      },
      verdict: {
        is_real: true,
        confidence: 0.94,
        reasoning:
          "Checked against the repo loopback rule and the proxy route map: the bind address is reachable on the LAN as written. Fix is necessary.",
      },
    },
    {
      finding: {
        file: "src/metrics/server.py",
        line: 52,
        severity: "major",
        confidence: 0.81,
        title: "Unauthenticated /metrics scrape path",
        description:
          "The scrape handler returns metrics with no auth check. Even on loopback, the auth middleware is bypassed for this internal route.",
        rule_id: null,
        proposed_fix: [
          '  @app.route("/metrics")',
          "  def metrics():",
          "-     return generate_latest()",
          "+     require_auth(request)",
          "+     return generate_latest()",
        ].join("\n"),
      },
      verdict: {
        is_real: true,
        confidence: 0.78,
        reasoning:
          "No upstream auth wrapper covers this route in the diff; the counterfactual fix is needed unless the route is removed from the public mux.",
      },
    },
  ],
  nits: [
    {
      finding: {
        file: "src/metrics/server.py",
        line: 71,
        severity: "minor",
        confidence: 0.6,
        title: "Prefer f-string over %-formatting",
        description: "",
        rule_id: null,
        proposed_fix: "",
      },
      verdict: {
        is_real: false,
        confidence: 0.7,
        reasoning: "Matches the existing module style; not a defect.",
      },
    },
    {
      finding: {
        file: "src/metrics/server.py",
        line: 4,
        severity: "nit",
        confidence: 0.42,
        title: "Unused import `socket`",
        description: "",
        rule_id: null,
        proposed_fix: "",
      },
      verdict: {
        is_real: true,
        confidence: 0.9,
        reasoning: "Import is never referenced after the bind change.",
      },
    },
    {
      finding: {
        file: "src/metrics/server.py",
        line: 30,
        severity: "nit",
        confidence: 0.38,
        title: "`serve()` missing docstring",
        description: "",
        rule_id: null,
        proposed_fix: "",
      },
      verdict: {
        is_real: true,
        confidence: 0.66,
        reasoning: "Public method, no docstring.",
      },
    },
    {
      finding: {
        file: "src/metrics/server.py",
        line: 38,
        severity: "nit",
        confidence: 0.3,
        title: "Magic number 9090 (port)",
        description: "",
        rule_id: null,
        proposed_fix: "",
      },
      verdict: {
        is_real: false,
        confidence: 0.62,
        reasoning: "Documented in the deploy compose file; intentional.",
      },
    },
  ],
  thread: [
    {
      role: "bot",
      author: "argus",
      time: "22m ago",
      badge: "Request changes",
      body: "Reviewed 3 files · +47 −12. 1 critical, 1 major — requesting changes on the loopback bind. 4 nits folded.",
    },
    {
      role: "operator",
      author: "acme-dev",
      time: "14m ago",
      resolved: true,
      body: "Good catch — the exporter should never have been public. Pushing the loopback fix now.",
    },
    {
      role: "bot",
      author: "argus",
      time: "9m ago",
      body: "Re-reviewed the delta (1 file). Loopback bind confirmed resolved. Major (auth on /metrics) still open.",
    },
  ],
};

const C = {
  critical: "var(--color-critical)",
  major: "var(--color-major)",
  minor: "var(--color-minor)",
  refactor: "var(--color-refactor)",
  nit: "var(--color-nit)",
};

/* Detail variants for the run-truth states the live API can return. They are
   reachable by direct URL only, so the review list stays the same. */
export const FALLBACK_ID = "acme-web-fallback";
export const ERROR_ID = "acme-web-error";
export const BARE_ID = "acme-web-bare";

/** The same review as the API returns it when the run never recorded the PR header. */
function withoutHeader(detail: ReviewDetail): ReviewDetail {
  const copy = { ...detail };
  delete copy.branch;
  delete copy.author;
  delete copy.filesChanged;
  delete copy.added;
  delete copy.removed;
  return copy;
}

const bareBase = withoutHeader(reviewDetail);

export const demoDetails: Record<string, ReviewDetail> = {
  [SHOWCASE_ID]: reviewDetail,
  [FALLBACK_ID]: {
    ...reviewDetail,
    id: FALLBACK_ID,
    model: "claude-sonnet-5-5",
    model_used: "gpt-6.1-sol",
    fallback_from: "claude-sonnet-5-5",
    fallback_reason: "RuntimeError: provider returned 529 overloaded",
  },
  [ERROR_ID]: {
    ...bareBase,
    id: ERROR_ID,
    status: "error",
    decision: "insufficient_evidence",
    model_used: null,
    bugs: [],
    nits: [],
    error: "BackendNotConfigured: ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN not set",
  },
  [BARE_ID]: { ...bareBase, id: BARE_ID },
};

export const reports: Record<ReportPeriod, Report> = {
  daily: {
    period: "daily",
    title: "Daily report",
    range: "Today · Jun 16, 2026",
    reviews: 9,
    findings: 21,
    acted: 7,
    merged: 14,
    actedPct: 33,
    severity: [
      { label: "Critical", value: 1, severity: "critical" },
      { label: "Major", value: 3, severity: "major" },
      { label: "Minor", value: 6, severity: "minor" },
      { label: "Nit", value: 11, severity: "nit" },
    ],
    category: [
      { label: "Bug", value: 4, color: C.major },
      { label: "Security", value: 2, color: C.critical },
      { label: "Performance", value: 3, color: C.minor },
      { label: "Refactor", value: 4, color: C.refactor },
      { label: "Nit", value: 8, color: C.nit },
    ],
    volume: [1, 2, 0, 2, 1, 2, 1, 0].map((v, i) => ({ label: `${i}`, value: v })),
    volumeLabel: "per 3-hour block",
    topRepos: [
      { repo: "acme/web", value: 3 },
      { repo: "acme/infra", value: 2 },
      { repo: "acme/worker", value: 2 },
      { repo: "acme/api", value: 1 },
      { repo: "acme/tooling", value: 1 },
    ],
  },
  weekly: {
    period: "weekly",
    title: "Weekly report",
    range: "Jun 10 – Jun 16, 2026",
    reviews: 62,
    findings: 148,
    acted: 52,
    merged: 96,
    actedPct: 35,
    severity: [
      { label: "Critical", value: 4, severity: "critical" },
      { label: "Major", value: 17, severity: "major" },
      { label: "Minor", value: 39, severity: "minor" },
      { label: "Nit", value: 88, severity: "nit" },
    ],
    category: [
      { label: "Bug", value: 27, color: C.major },
      { label: "Security", value: 12, color: C.critical },
      { label: "Performance", value: 15, color: C.minor },
      { label: "Refactor", value: 26, color: C.refactor },
      { label: "Nit", value: 68, color: C.nit },
    ],
    volume: [7, 9, 6, 11, 8, 12, 9].map((v, i) => ({ label: `${i}`, value: v })),
    volumeLabel: "per day",
    topRepos: [
      { repo: "acme/web", value: 15 },
      { repo: "acme/infra", value: 9 },
      { repo: "acme/worker", value: 8 },
      { repo: "acme/billing", value: 6 },
      { repo: "acme/auth", value: 4 },
    ],
  },
  monthly: {
    period: "monthly",
    title: "Monthly report",
    range: "May 17 – Jun 16, 2026",
    reviews: 246,
    findings: 571,
    acted: 200,
    merged: 371,
    actedPct: 35,
    severity: [
      { label: "Critical", value: 15, severity: "critical" },
      { label: "Major", value: 61, severity: "major" },
      { label: "Minor", value: 152, severity: "minor" },
      { label: "Nit", value: 343, severity: "nit" },
    ],
    category: [
      { label: "Bug", value: 112, color: C.major },
      { label: "Security", value: 49, color: C.critical },
      { label: "Performance", value: 63, color: C.minor },
      { label: "Refactor", value: 108, color: C.refactor },
      { label: "Nit", value: 239, color: C.nit },
    ],
    volume: [48, 56, 67, 75].map((v, i) => ({ label: `${i}`, value: v })),
    volumeLabel: "per week",
    topRepos: [
      { repo: "acme/web", value: 61 },
      { repo: "acme/infra", value: 39 },
      { repo: "acme/worker", value: 33 },
      { repo: "acme/billing", value: 24 },
      { repo: "acme/auth", value: 20 },
    ],
  },
};

export const benchmark: Benchmark = {
  rows: [
    { metric: "Comments / PR", note: "lower is calmer · gameable", argus: "4.8", codeRabbit: "9.7", winner: "argus" },
    { metric: "Action rate (acted-on)", note: "higher is better", argus: "—", codeRabbit: "22%", winner: null },
    { metric: "Merged-past (ignored)", note: "lower is better · gameable", argus: "—", codeRabbit: "78%", winner: null },
    { metric: "Bug-class share", note: "signal vs nit-noise · gameable", argus: "21%", codeRabbit: "8%", winner: "argus" },
    { metric: "Cites guideline", note: "grounded in docs", argus: "—", codeRabbit: "12%", winner: null },
    { metric: "Catch rate (recall)", note: "dual corpus (escaped ∪ CR-caught)", argus: "—", codeRabbit: "—", winner: null, pending: true },
    { metric: "False-positive rate", note: "operator-marked corpus", argus: "—", codeRabbit: "—", winner: null, pending: true },
  ],
  note: "Demo data: every figure and the gate state on this page are synthetic, not measured comparisons. In a live setup, volume and action metrics come from CodeRabbit's persisted review threads (a precision oracle, what it found, not what was true). Catch-rate and false-positive-rate stay pending until the injected-bug and verified-finding corpora are scored locally.",
  perRepo: [
    { repo: "acme/web", crComments: 48, resolved: 19, mergedPast: 29, critMajor: 28, citesGuideline: 6 },
    { repo: "acme/api", crComments: 36, resolved: 12, mergedPast: 24, critMajor: 22, citesGuideline: 4 },
    { repo: "acme/billing", crComments: 27, resolved: 10, mergedPast: 17, critMajor: 16, citesGuideline: 5 },
    { repo: "acme/infra", crComments: 21, resolved: 7, mergedPast: 14, critMajor: 13, citesGuideline: 1 },
    { repo: "acme/docs", crComments: 17, resolved: 9, mergedPast: 8, critMajor: 6, citesGuideline: 2 },
    { repo: "acme/worker", crComments: 12, resolved: 5, mergedPast: 7, critMajor: 5, citesGuideline: 0 },
    { repo: "acme/auth", crComments: 8, resolved: 4, mergedPast: 4, critMajor: 4, citesGuideline: 1 },
    { repo: "acme/tooling", crComments: 5, resolved: 1, mergedPast: 4, critMajor: 2, citesGuideline: 0 },
  ],
  argusEmpty: false,
  // Synthetic gate state in the same shape the live /benchmark returns. The
  // numbers are invented demo data; the live endpoint recomputes them from the DB.
  gate: {
    verdict: "INSUFFICIENT-DATA",
    verdictWhy:
      "Axis A (the recall corpus) is not built, so no verdict can ship. The proxies below are diagnostics, they never decide who wins.",
    clock: {
      started: true,
      day: 9,
      totalDays: 28,
      daysRemaining: 19,
      firstReviewAt: "2026-06-01T09:00:00+00:00",
      ceiling: "2026-06-29",
    },
    floors: [
      { key: "corpus", label: "Axis-A corpus bugs", value: 0, floor: 30, met: false, detail: "escaped ∪ CR-caught-pre-merge, not mined yet" },
      { key: "livePRs", label: "Live PRs reviewed", value: 41, floor: 20, met: true },
      { key: "marks", label: "Operator ✓/✗ marks", value: 0, floor: 60, met: false, detail: "triage UI not built yet" },
      { key: "crashFree", label: "Crash-free days", value: 0, floor: 7, met: false, failing: true, detail: "3 engine errors" },
    ],
    axisA: {
      title: "Comparison",
      status: "not-started",
      rule: "Paired McNemar on a dual corpus (escaped ∪ CR-caught-pre-merge). Promote when Argus-only catches beat CR-only at p < 0.10, ≥ 3 net.",
      bugsScored: 0,
      floor: 30,
    },
    axisB: {
      title: "Deployment",
      status: "blocked",
      metrics: [
        { label: "Live PRs", value: "41", floor: "≥ 20", met: true },
        { label: "Crash-free", value: "0d", floor: "≥ 7d", met: false, detail: "3 errors" },
        { label: "Action-rate", value: "0%", floor: "≥ 50%", met: false, detail: "no marks UI yet" },
      ],
    },
    axisC: {
      title: "Procurement",
      status: "awaiting",
      rule: "MCV = Σ severity·(CR-caught, Argus-missed) ÷ CR price. Below 0.3 makes CodeRabbit cancel-eligible. Decided separately from the Argus verdict.",
    },
    severity: { critical: 3, major: 39, minor: 120, nit: 35 },
    repoSkew: [
      { repo: "acme/api", count: 24, pct: 59 },
      { repo: "acme/web", count: 8, pct: 20 },
      { repo: "acme/billing", count: 4, pct: 10 },
      { repo: "acme/infra", count: 3, pct: 7 },
      { repo: "acme/worker", count: 1, pct: 2 },
      { repo: "acme/docs", count: 1, pct: 2 },
    ],
    dominantRepo: { repo: "acme/api", pct: 59 },
    proxies: { commentsPerPr: "4.8", bugShare: "21%" },
    reviews: { done: 41, error: 3, running: 0 },
  },
};

const baseRepos = [
  "acme/api",
  "acme/web",
  "acme/billing",
  "acme/infra",
  "acme/worker",
  "acme/auth",
  "acme/docs",
  "acme/tooling",
  "acme/mobile",
  "acme/archive",
];


export const config: ConfigData = {
  repos: baseRepos.map((name) => ({ name, watched: name !== "acme/archive" })),
  publish: {
    mode: "dry-run",
    source: "env",
    botLogin: "argus-review-bot",
    tokenConfigured: true,
    repos: [
      {
        name: "acme/api",
        access: {
          state: "ok",
          detail: "bot can push",
          settingsUrl: "https://github.com/acme/api/settings/access",
          grant: null,
        },
      },
      {
        name: "acme/auth",
        access: {
          state: "missing",
          detail: "bot has no write access",
          settingsUrl: "https://github.com/acme/auth/settings/access",
          grant: "failed",
        },
      },
    ],
  },
  hiddenCount: 10,
  totalRepos: 20,
  models: [
    { name: "Deep review", caption: "reports every candidate", model: "gpt-6.1-sol", tier: "deep" },
    { name: "Verification", caption: "skeptical adjudicator", model: "gpt-6.1-sol", tier: "deep" },
  ],
  modelOptions: ["gpt-6.1-sol", "~x-ai/grok-latest"],
  bugCap: 10,
  minConfidence: 0.55,
};

export const integrations: Integrations = {
  health: [
    { label: "GitHub token", status: "configured", state: "ok" },
    { label: "PR watcher", status: "on · 1 owner · every 60s", state: "ok" },
    { label: "Publishing", status: "dry-run · 2 repos", state: "ok" },
    { label: "Model provider", status: "openrouter · openai/gpt-6.1-sol", state: "ok" },
    { label: "REST API", status: "healthy", state: "ok" },
    { label: "Auth", status: "token", state: "ok" },
  ],
  warning: null,
  github: {
    token: "configured",
    watcher: { enabled: true, owners: ["acme"], intervalSeconds: 60, watchedRepos: 20 },
    publishing: { mode: "dry-run", repoCount: 2, tokenConfigured: true, source: "env", autoInvite: false },
  },
  api: {
    baseUrl: "https://argus.example.com/api/v1",
    auth: "token",
    endpoints: [
      { method: "GET", path: "/reviews", description: "List reviews, paginated" },
      { method: "GET", path: "/reviews/{id}", description: "One review: findings + verdicts + thread" },
      { method: "GET", path: "/findings?severity=&repo=", description: "Filter findings across all repos" },
      { method: "GET", path: "/reports/{period}", description: "daily · weekly · monthly rollup" },
      { method: "GET", path: "/stats", description: "Overview counters and trends" },
      { method: "POST", path: "/reviews/{id}/rerun", description: "Re-run the pipeline on a PR" },
      { method: "GET", path: "/benchmark", description: "Argus vs CodeRabbit: volume and action numbers for the comparison screen" },
      { method: "GET", path: "/integrations", description: "Integration status from the running config: credentials are reported as set or missing" },
      { method: "POST", path: "/findings/{finding_id}/label", description: "Record the operator's ground-truth label on a finding" },
    ],
  },
  mcp: {
    status: "stdio",
    config: `{
  "mcpServers": {
    "argus": {
      "command": "docker",
      "args": ["compose", "-f", "/path/to/argus/docker-compose.yml",
               "exec", "-T", "-e", "ARGUS_API=http://127.0.0.1:8000",
               "argus-api", "/opt/venv/bin/argus-mcp"]
    }
  }
}`,
    tools: ["review_diff", "review_pr", "get_verdict", "mark_finding"],
    resources: [],
  },
};
