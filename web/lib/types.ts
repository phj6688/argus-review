/* Argus data shapes. Finding / Verdict / VerifiedFinding mirror the
   backend engine (src/argus/schema.py) exactly — do not drift. */

export type Severity = "critical" | "major" | "minor" | "nit";

export interface Finding {
  file: string;
  line: number; // >= 1
  severity: Severity;
  confidence: number; // 0.0–1.0, model self-reported at finding time
  title: string;
  description: string;
  proposed_fix: string; // a unified diff
  rule_id?: string | null; // grounded in a repo house rule; never invented
}

export interface Verdict {
  is_real: boolean; // CONFIRMED if true, else REFUTED
  confidence: number; // 0.0–1.0
  reasoning: string;
}

export interface VerifiedFinding {
  finding: Finding;
  verdict: Verdict;
}

/* ---- derived / view shapes returned by the API ---- */

export type ActionState = "acted" | "merged" | "pending";
export type Decision = "approve" | "comment" | "changes_requested" | "insufficient_evidence";

export interface SeverityCounts {
  critical: number;
  major: number;
  minor: number;
  nit: number;
}

export interface ReviewSummary {
  id: string;
  repo: string;
  pr: number;
  title: string;
  model: string;
  counts: SeverityCounts;
  action: ActionState;
  age: string;
}

export interface LiveReview {
  repo: string;
  pr: number;
  title: string;
  model: string;
  stage: string;
  elapsed: string;
}

export interface Stats {
  prsReviewed: { value: number; deltaPct: number };
  openFindings: SeverityCounts & { total: number };
  bugVsNit: { bug: number; nit: number };
  actionRate: { pct: number; prevPct: number };
  avgComments: { value: number; prev: number };
  trends: {
    reviewsPerDay: { value: number; deltaPct: number; series: number[] };
    findingsPerDay: { value: number; deltaPct: number; series: number[] };
  };
}

export interface Comment {
  author: string;
  role: "bot" | "operator";
  time: string;
  body: string;
  badge?: string;
  resolved?: boolean;
}

export interface ReviewDetail {
  id: string;
  repo: string;
  pr: number;
  title: string;
  branch: string;
  author: string;
  model: string;
  /** Run lifecycle, independent of the verdict: a poller must not have to
   *  recognise a decision value to know the run finished. */
  status: string;
  time: string;
  filesChanged: number;
  added: number;
  removed: number;
  decision: Decision;
  bugs: VerifiedFinding[];
  nits: VerifiedFinding[];
  thread: Comment[];
}

export type ReportPeriod = "daily" | "weekly" | "monthly";

export interface Report {
  period: ReportPeriod;
  title: string;
  range: string;
  reviews: number;
  findings: number;
  acted: number;
  merged: number;
  actedPct: number;
  severity: { label: string; value: number; severity: Severity }[];
  category: { label: string; value: number; color: string }[];
  volume: { label: string; value: number }[];
  volumeLabel: string;
  topRepos: { repo: string; value: number }[];
}

export interface BenchRow {
  metric: string;
  note: string;
  argus: string;
  codeRabbit: string;
  winner: "argus" | "coderabbit" | null;
  pending?: boolean;
}

export interface PerRepoRow {
  repo: string;
  crComments: number;
  resolved: number;
  mergedPast: number;
  critMajor: number;
  citesGuideline: number;
}

export type GateVerdict =
  | "PROMOTE"
  | "KEEP-GATHERING"
  | "KILL"
  | "INSUFFICIENT-DATA";

export interface GateFloor {
  key: string;
  label: string;
  value: number;
  floor: number;
  met: boolean;
  failing?: boolean;
  detail?: string | null;
}

export interface GateAxisMetric {
  label: string;
  value: string;
  floor: string;
  met: boolean;
  detail?: string | null;
}

export interface Gate {
  verdict: GateVerdict;
  verdictWhy: string;
  clock: {
    started: boolean;
    day: number;
    totalDays: number;
    daysRemaining: number;
    firstReviewAt: string | null;
    ceiling: string | null;
  };
  floors: GateFloor[];
  axisA: { title: string; status: string; rule: string; bugsScored: number; floor: number };
  axisB: { title: string; status: string; metrics: GateAxisMetric[] };
  axisC: { title: string; status: string; rule: string };
  severity: SeverityCounts;
  repoSkew: { repo: string; count: number; pct: number }[];
  dominantRepo: { repo: string; pct: number } | null;
  proxies: { commentsPerPr: string; bugShare: string };
  reviews: { done: number; error: number; running: number };
}

export interface Benchmark {
  rows: BenchRow[];
  note: string;
  perRepo: PerRepoRow[];
  argusEmpty: boolean;
  gate?: Gate;
}

export interface ModelStage {
  name: string;
  caption: string;
  model: string;
  tier: "deep" | "triage";
}

export interface RepoWatch {
  name: string;
  watched: boolean;
}

export interface ConfigData {
  repos: RepoWatch[];
  hiddenCount: number;
  totalRepos: number;
  models: ModelStage[];
  /** Reviewer ids the operator may pick (env-owned via ARGUS_MODEL_OPTIONS). */
  modelOptions: string[];
  bugCap: number;
  minConfidence: number;
}

export interface Endpoint {
  method: "GET" | "POST";
  path: string;
  description: string;
}

export interface Integrations {
  health: { label: string; status: string; state: "ok" | "warn" | "error" }[];
  warning: string | null;
  github: {
    token: "configured" | "missing";
    watcher: {
      enabled: boolean;
      owners: string[];
      intervalSeconds: number;
      watchedRepos: number;
    };
    publishing: {
      mode: "off" | "dry-run" | "on";
      repoCount: number;
      tokenConfigured: boolean;
    };
  };
  api: {
    baseUrl: string;
    auth: "token" | "proxy";
    endpoints: Endpoint[];
  };
  mcp: {
    status: string;
    config: string;
    tools: string[];
    resources: string[];
  };
}
