/* The data access layer. Today it resolves the demo fixtures; when the
   FastAPI backend is live, set NEXT_PUBLIC_ARGUS_API and the
   guarded `live()` branch fetches the same shapes — no screen changes. */

import {
  benchmark,
  config,
  demoDetails,
  integrations,
  liveReview,
  reports,
  reviews,
  stats,
} from "./fixtures";
import type {
  Benchmark,
  ConfigData,
  Integrations,
  LiveReview,
  PublishConfig,
  Report,
  ReportPeriod,
  ReviewDetail,
  ReviewSummary,
  Stats,
} from "./types";

const API_BASE = process.env.NEXT_PUBLIC_ARGUS_API;

/* Browser calls use the console proxy; deployments behind an authenticating
   reverse proxy can explicitly keep their existing API route. */
const CLIENT_API_BASE =
  (process.env.NEXT_PUBLIC_ARGUS_CLIENT_API?.trim() || "/api/argus").replace(/\/+$/, "");

async function live<T>(
  path: string,
  notFoundAsNull = false,
): Promise<T | null> {
  if (!API_BASE) return null;
  const server = typeof window === "undefined";
  const headers: HeadersInit = {};
  if (server && process.env.ARGUS_API_TOKEN) {
    headers.Authorization = `Bearer ${process.env.ARGUS_API_TOKEN}`;
  }
  const res = await fetch(`${server ? API_BASE : CLIENT_API_BASE}${path}`, {
    cache: "no-store", headers,
  });
  // A 404 means the id is valid but no such row — let callers render notFound()
  // instead of a 500. Any other non-OK (incl. 5xx) still throws.
  if (notFoundAsNull && res.status === 404) return null;
  if (!res.ok) throw new Error(`argus api ${path}: ${res.status}`);
  return (await res.json()) as T;
}

export async function getStats(): Promise<Stats> {
  return (await live<Stats>("/stats")) ?? stats;
}

export async function getReviews(): Promise<{
  live: LiveReview | null;
  reviews: ReviewSummary[];
}> {
  const remote = await live<{ live: LiveReview | null; reviews: ReviewSummary[] }>(
    "/reviews",
  );
  return remote ?? { live: liveReview, reviews };
}

export async function getReview(id: string): Promise<ReviewDetail | null> {
  const remote = await live<ReviewDetail>(`/reviews/${id}`, true);
  if (remote) return remote;
  // Demo: the detail screen showcases one fully-authored PR and its run-truth variants.
  return demoDetails[id] ?? null;
}

/* The trigger flow talks to the run lifecycle directly from the browser.
   Server fetches above use the internal absolute base; these browser fetches
   go through the console proxy or an explicitly configured API route. */

export interface CreatedReview {
  id: string;
  status: RunStatus;
}

export type RunStatus = "running" | "done" | "error";

export interface RunState {
  id: string;
  status: RunStatus;
  findings?: unknown[];
  error?: string;
}

/** The API refused to start a review. detail is its own explanation, when it gave one. */
export class ReviewRefused extends Error {
  constructor(
    readonly status: number,
    readonly detail: string | null,
  ) {
    super(detail ?? `argus api /reviews: ${status}`);
  }
}

export async function createReview(input: {
  repo: string;
  pr: number;
}): Promise<CreatedReview> {
  const res = await fetch(`${CLIENT_API_BASE}/reviews`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    cache: "no-store",
    body: JSON.stringify(input),
  });
  if (!res.ok) {
    let detail: string | null = null;
    try {
      const refusal = (await res.json()) as { detail?: unknown; error?: unknown };
      const text = refusal.detail ?? refusal.error;
      if (typeof text === "string" && text) detail = text;
    } catch {
      // No JSON body: the status is all there is to report.
    }
    throw new ReviewRefused(res.status, detail);
  }
  const body = (await res.json()) as { id: string | number; status: RunStatus };
  return { id: String(body.id), status: body.status };
}

export async function getRunState(
  id: string,
  signal?: AbortSignal,
): Promise<RunState> {
  // Poll the machine-facing verdict document, not ReviewDetail: a running review
  // returns {status:"running"}; a finished one carries engine.status (the real
  // done/error) and counts.confirmed. ReviewDetail has none of these, which is
  // why the old poll always resolved to "done · 0 findings".
  const res = await fetch(`${CLIENT_API_BASE}/reviews/${id}/verdict`, {
    cache: "no-store",
    signal,
  });
  if (!res.ok) throw new Error(`argus api /reviews/${id}/verdict: ${res.status}`);
  const body = (await res.json()) as {
    status?: RunStatus;
    engine?: { status?: string; error?: string };
    counts?: { confirmed?: number };
    findings?: unknown[];
  };
  if (body.status === "running") return { id, status: "running" };
  if (body.engine?.status === "error") {
    return { id, status: "error", error: body.engine?.error };
  }
  const findings = body.findings ?? Array.from({ length: body.counts?.confirmed ?? 0 });
  return { id, status: "done", findings };
}

export async function getReport(period: ReportPeriod): Promise<Report> {
  return (await live<Report>(`/reports/${period}`)) ?? reports[period];
}

export async function getBenchmark(): Promise<Benchmark> {
  return (await live<Benchmark>("/benchmark")) ?? benchmark;
}

export async function getConfig(): Promise<ConfigData> {
  return (await live<ConfigData>("/config")) ?? config;
}

function deepModelId(data: ConfigData): string {
  const deep =
    data.models.find((m) => m.tier === "deep") ?? data.models[0];
  return deep?.model ?? "";
}

/* Persist operator config. With a live backend it PUTs the camelCase contract and
   returns the stored shape; without one it echoes the input so the demo is a no-op. */
export async function putConfig(data: ConfigData): Promise<ConfigData> {
  if (!API_BASE) return data;
  // Browser-invoked (config screen is a client component): must ride the
  // same-origin proxy like the trigger flow; API_BASE is the
  // container-network address only the server side can resolve.
  const res = await fetch(`${CLIENT_API_BASE}/config`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      repos: data.repos,
      model: deepModelId(data),
      bugCap: data.bugCap,
      minConfidence: data.minConfidence,
    }),
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`argus api /config: ${res.status}`);
  return (await res.json()) as ConfigData;
}

export async function putPublishRepos(repos: string[]): Promise<PublishConfig> {
  if (!API_BASE) {
    await new Promise((r) => setTimeout(r, 120));
    return {
      ...config.publish,
      source: "dashboard",
      repos: repos.map((name) => ({
        name,
        access: config.publish.repos.find(
          (repo) => repo.name.toLowerCase() === name.toLowerCase(),
        )?.access ?? {
          state: "ok",
          detail: "bot can push",
          settingsUrl: `https://github.com/${name}/settings/access`,
          grant: null,
        },
      })),
    };
  }

  const res = await fetch(`${CLIENT_API_BASE}/config/publish-repos`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ repos }),
    cache: "no-store",
  });
  if (!res.ok) {
    let message = `argus api /config/publish-repos: ${res.status}`;
    try {
      const body = (await res.json()) as { detail?: unknown } | null;
      if (typeof body?.detail === "string") {
        message = body.detail;
      } else if (Array.isArray(body?.detail) && typeof body.detail[0]?.msg === "string") {
        message = body.detail[0].msg;
      }
    } catch {
      // Proxies may return a non-JSON error, so retain the status fallback.
    }
    throw new Error(message);
  }
  return (await res.json()) as PublishConfig;
}

export async function getIntegrations(): Promise<Integrations> {
  return (await live<Integrations>("/integrations")) ?? integrations;
}
