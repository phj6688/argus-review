"use client";

import { useState } from "react";
import { Reveal, RevealItem } from "@/components/ui/reveal";
import { ApiBrackets, GitHubMark, McpGear, WarnTriangle } from "@/components/ui/icons";
import type { Integrations } from "@/lib/types";
import { ScreenHeader } from "./screen-header";

const STATE_COLOR = {
  ok: "var(--color-sage)",
  warn: "var(--color-warn)",
  error: "var(--color-critical)",
} as const;

function CardHeading({
  icon,
  title,
  meta,
}: {
  icon: React.ReactNode;
  title: string;
  meta: React.ReactNode;
}) {
  return (
    <div className="mb-4 flex items-center gap-[10px]">
      <span className="text-heading">{icon}</span>
      <span className="text-[15px] font-semibold text-heading">{title}</span>
      <span className="ml-auto text-[11.5px] text-faint">{meta}</span>
    </div>
  );
}

function Label({ children }: { children: React.ReactNode }) {
  return (
    <div className="mb-[7px] text-[11px] font-semibold uppercase tracking-[0.08em] text-faint">
      {children}
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center gap-[11px] border-t border-[var(--line-05)] py-[9px]">
      <span className="text-xs text-dim">{label}</span>
      <span className="ml-auto text-right text-[12.5px] text-text">{children}</span>
    </div>
  );
}

export function IntegrationsScreen({ data }: { data: Integrations }) {
  const [copied, setCopied] = useState<string | null>(null);
  const copy = (field: string, text: string) => {
    try {
      navigator.clipboard?.writeText(text);
    } catch {
      /* clipboard unavailable */
    }
    setCopied(field);
    setTimeout(() => setCopied((c) => (c === field ? null : c)), 1300);
  };
  const copyLabel = (field: string) => (copied === field ? "Copied" : "Copy");
  const { watcher, publishing } = data.github;

  return (
    <div>
      <ScreenHeader
        title="Integrations"
        subtitle="How Argus reaches GitHub and your model provider, and how clients reach Argus."
      />

      <div className="mb-4 flex flex-wrap gap-3">
        {data.health.map((h) => (
          <div
            key={h.label}
            className="flex items-center gap-[9px] rounded-[11px] border border-[var(--line)] bg-surface px-[15px] py-[10px]"
          >
            <span
              className="h-2 w-2 rounded-full"
              style={{ background: STATE_COLOR[h.state], boxShadow: "0 0 8px var(--sage-30)" }}
            />
            <span className="text-[13px] text-text">{h.label}</span>
            <span className="text-xs text-faint">{h.status}</span>
          </div>
        ))}
        {data.warning && (
          <div
            className="flex min-w-[250px] flex-1 items-center gap-[10px] rounded-[11px] px-[15px] py-[10px]"
            style={{ background: "var(--warn-06)", border: "1px solid var(--warn-22)" }}
          >
            <WarnTriangle className="shrink-0 text-warn" />
            <span className="text-[12.5px]" style={{ color: "var(--color-warn-text)" }}>
              {data.warning}
            </span>
          </div>
        )}
      </div>

      <Reveal className="mb-[14px] grid grid-cols-1 items-start gap-[14px] lg:grid-cols-2">
        <RevealItem className="panel px-[22px] py-5">
          <CardHeading
            icon={<GitHubMark size={20} />}
            title="GitHub"
            meta={`token ${data.github.token}`}
          />
          <Label>
            PR watcher{" "}
            <span
              className="font-normal normal-case tracking-normal"
              data-tip="Polls GitHub for open pull requests. Set ARGUS_WATCH_ENABLED and ARGUS_WATCH_OWNERS."
            >
              · polling
            </span>
          </Label>
          <div className="mb-4 flex flex-col">
            <Row label="Status">{watcher.enabled ? "on" : "off"}</Row>
            <Row label="Owners">
              {watcher.owners.length ? (
                <span className="inline-flex flex-wrap justify-end gap-[6px]">
                  {watcher.owners.map((o) => (
                    <span
                      key={o}
                      className="rounded-[5px] px-2 py-[3px] font-mono text-[11.5px] text-accent-dim"
                      style={{ background: "var(--accent-08)" }}
                    >
                      {o}
                    </span>
                  ))}
                </span>
              ) : (
                <span className="text-faint">none set</span>
              )}
            </Row>
            <Row label="Poll interval">{watcher.intervalSeconds}s</Row>
            <Row label="Watched repos">{watcher.watchedRepos}</Row>
          </div>
          <Label>
            Publishing{" "}
            <span
              className="font-normal normal-case tracking-normal"
              data-tip="Posts review comments to pull requests. Set ARGUS_PUBLISH, ARGUS_PUBLISH_REPOS and ARGUS_PUBLISH_TOKEN."
            >
              · review comments
            </span>
          </Label>
          <div className="flex flex-col">
            <Row label="Mode">{publishing.mode}</Row>
            <Row label="Repo allowlist">{publishing.repoCount}</Row>
            <Row label="Publish token">
              {publishing.tokenConfigured ? "configured" : "missing"}
            </Row>
          </div>
        </RevealItem>

        <RevealItem className="panel px-[22px] py-5">
          <CardHeading
            icon={<ApiBrackets size={19} />}
            title="REST API"
            meta={`FastAPI · ${data.api.auth} auth`}
          />
          <Label>Base URL</Label>
          <div className="mb-[14px] rounded-[8px] border border-[var(--line-10)] bg-inset px-3 py-[9px]">
            <span className="font-mono text-[12.5px] text-accent-dim">{data.api.baseUrl}</span>
          </div>
          <Label>Authentication</Label>
          <div className="mb-4 rounded-[8px] border border-[var(--line-10)] bg-inset px-3 py-[9px]">
            <span className="font-mono text-[12.5px] text-dim">
              {data.api.auth === "token"
                ? "Authorization: Bearer <ARGUS_API_TOKEN>"
                : "Handled by the reverse proxy in front of the API"}
            </span>
          </div>
          <Label>Endpoints</Label>
          <div className="flex flex-col">
            {data.api.endpoints.map((e, i) => (
              <div
                key={i}
                className="grid grid-cols-[44px_minmax(0,1fr)] items-start gap-x-[11px] gap-y-[3px] border-t border-[var(--line-05)] py-[9px]"
              >
                <span
                  className="w-[44px] shrink-0 rounded-[5px] py-[3px] text-center text-[10px] font-bold"
                  style={
                    e.method === "GET"
                      ? { color: "var(--color-accent-dim)", background: "var(--accent-10)" }
                      : { color: "var(--color-major)", background: "var(--major-14)" }
                  }
                >
                  {e.method}
                </span>
                <span className="break-all pt-[2px] font-mono text-xs text-text">{e.path}</span>
                {e.description && (
                  <span
                    className="col-start-2 truncate text-[11px] text-faint"
                    title={e.description}
                  >
                    {e.description}
                  </span>
                )}
              </div>
            ))}
          </div>
        </RevealItem>
      </Reveal>

      <Reveal>
        <RevealItem className="panel px-[22px] py-5">
          <div className="mb-[6px] flex items-center gap-[10px]">
            <span className="text-heading">
              <McpGear size={19} />
            </span>
            <span className="text-[15px] font-semibold text-heading">MCP server</span>
            <span className="ml-1 inline-flex items-center gap-[6px] text-[11.5px] text-sage">
              <span className="h-[6px] w-[6px] rounded-full bg-sage" />
              {data.mcp.status}
            </span>
          </div>
          <p className="mb-[18px] max-w-[660px] text-[13px] leading-[1.55] text-dim">
            A local stdio server. Your MCP client starts it with the command below, so there is no
            network port to open. It reviews a diff or a pull request and reads or marks verdicts.
          </p>
          <div className="grid grid-cols-1 gap-5 lg:grid-cols-[1.25fr_1fr]">
            <div>
              <div className="mb-2 flex items-center justify-between">
                <span className="text-[11px] font-semibold uppercase tracking-[0.08em] text-faint">
                  .mcp.json
                </span>
                <button
                  type="button"
                  onClick={() => copy("mcp", data.mcp.config)}
                  className="text-[11.5px] text-dim transition-colors hover:text-accent"
                >
                  {copyLabel("mcp")}
                </button>
              </div>
              <pre
                className="overflow-x-auto rounded-[9px] border border-[var(--line-10)] bg-inset px-4 py-[14px] font-mono text-xs leading-[1.65]"
                style={{ color: "var(--color-prose)" }}
              >
                {data.mcp.config}
              </pre>
            </div>
            <div>
              <Label>Exposed tools</Label>
              <div className="mb-5 flex flex-wrap gap-[7px]">
                {data.mcp.tools.map((t) => (
                  <span
                    key={t}
                    className="rounded-[6px] px-[9px] py-1 font-mono text-[11.5px] text-accent-dim"
                    style={{ background: "var(--accent-08)", border: "1px solid var(--accent-18)" }}
                  >
                    {t}
                  </span>
                ))}
              </div>
              {data.mcp.resources.length > 0 && (
                <>
                  <Label>Resources</Label>
                  <div className="flex flex-col gap-2">
                    {data.mcp.resources.map((r) => (
                      <span key={r} className="font-mono text-xs text-sage">
                        {r}
                      </span>
                    ))}
                  </div>
                </>
              )}
            </div>
          </div>
        </RevealItem>
      </Reveal>
    </div>
  );
}
