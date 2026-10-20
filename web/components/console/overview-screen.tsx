"use client";

import Link from "next/link";
import { useState } from "react";
import { Reveal, RevealItem } from "@/components/ui/reveal";
import { Segmented } from "@/components/ui/segmented";
import { Sparkline } from "@/components/ui/sparkline";
import type { LiveReview, ReviewSummary, Stats } from "@/lib/types";
import { sevVar } from "@/lib/utils";
import { LiveStrip, ReviewRow } from "./review-bits";
import { ScreenHeader } from "./screen-header";

type Range = "7d" | "30d" | "all";

function StatCard({ children }: { children: React.ReactNode }) {
  return <RevealItem className="panel p-[17px] pb-4">{children}</RevealItem>;
}

function TrendPanel({
  label,
  value,
  delta,
  deltaClass,
  series,
  color,
}: {
  label: string;
  value: string;
  delta: string;
  deltaClass: string;
  series: number[];
  color: string;
}) {
  return (
    <div className="flex items-center gap-5">
      <div className="min-w-[92px]">
        <div className="mb-[6px] text-xs text-dim">{label}</div>
        <div className="flex items-baseline gap-2">
          <span className="text-[26px] font-semibold tabular-nums text-text">{value}</span>
          <span className={`text-xs ${deltaClass}`}>{delta}</span>
        </div>
      </div>
      <Sparkline data={series} color={color} />
    </div>
  );
}

export function OverviewScreen({
  stats,
  recent,
  live,
}: {
  stats: Stats;
  recent: ReviewSummary[];
  live: LiveReview | null;
}) {
  const [range, setRange] = useState<Range>("7d");
  const of = stats.openFindings;
  const w = (n: number) => `${(of.total > 0 ? (n / of.total) * 100 : 0).toFixed(1)}%`;
  const bug = stats.bugVsNit.bug;
  const nit = stats.bugVsNit.nit;
  // An empty database has no findings: show 0% rather than NaN, and draw an empty bar.
  const hasFindings = bug + nit > 0;
  const bugPct = hasFindings ? Math.round((bug / (bug + nit)) * 100) : 0;
  const nitPct = hasFindings ? 100 - bugPct : 0;

  return (
    <div>
      <ScreenHeader
        className="mb-7"
        title="Overview"
        subtitle="Fleet health"
        controls={
          <Segmented<Range>
            options={[
              { value: "7d", label: "7D" },
              { value: "30d", label: "30D" },
              { value: "all", label: "All" },
            ]}
            value={range}
            onChange={setRange}
          />
        }
      />

      <Reveal className="mb-[22px] grid grid-cols-2 gap-[14px] md:grid-cols-3 lg:grid-cols-5">
        <StatCard>
          <div
            className="eyebrow mb-3"
            data-tip="Pull requests Argus reviewed in this period, across the watched repos."
          >
            PRs reviewed
          </div>
          <div className="stat-num">{stats.prsReviewed.value}</div>
          <div className="mt-[9px] text-xs text-accent">
            ↑ {stats.prsReviewed.deltaPct}% <span className="text-faint">vs last week</span>
          </div>
        </StatCard>

        <StatCard>
          <div
            className="eyebrow mb-3"
            data-tip="Findings not yet resolved, split by severity — critical, major, minor, nit."
          >
            Open findings
          </div>
          <div className="stat-num">{of.total}</div>
          <div className="mt-[11px] flex h-[5px] gap-[2px] overflow-hidden rounded-[3px]">
            <span style={{ width: w(of.critical), background: sevVar("critical") }} />
            <span style={{ width: w(of.major), background: sevVar("major") }} />
            <span style={{ width: w(of.minor), background: sevVar("minor") }} />
            <span style={{ width: w(of.nit), background: sevVar("nit") }} />
          </div>
          <div className="mt-[9px] text-xs">
            <span style={{ color: sevVar("critical") }}>{of.critical} critical</span>
            <span className="text-faint"> · {of.major} major</span>
          </div>
        </StatCard>

        <StatCard>
          <div
            className="eyebrow mb-3"
            data-tip="Bug-class = critical + major (real defects). Nit = minor + nit (style / low-signal)."
          >
            Bug-class vs nit
          </div>
          <div className="flex items-baseline gap-[6px]">
            <span className="stat-num">{bug}</span>
            <span className="text-[15px] text-faint">/ {nit}</span>
          </div>
          <div className="mt-[11px] flex h-[5px] gap-[2px] overflow-hidden rounded-[3px]">
            <span style={{ width: `${bugPct}%`, background: sevVar("major") }} />
            <span style={{ width: `${nitPct}%`, background: sevVar("nit") }} />
          </div>
          <div className="mt-[9px] text-xs text-faint">
            <span style={{ color: sevVar("major") }}>{bugPct}%</span> bug-class
          </div>
        </StatCard>

        <StatCard>
          <div
            className="eyebrow mb-3"
            data-tip="Share of findings you acted on vs. merged past without addressing. Higher means the reviewer is earning its keep."
          >
            Action rate
          </div>
          <div className="stat-num">
            {stats.actionRate.pct}
            <span className="text-[18px] text-dim">%</span>
          </div>
          <div
            className="mt-[11px] h-[5px] overflow-hidden rounded-[3px]"
            style={{ background: "var(--line)" }}
          >
            <span
              className="block h-full bg-accent"
              style={{ width: `${stats.actionRate.pct}%` }}
            />
          </div>
          <div className="mt-[9px] text-xs" style={{ color: "var(--color-critical)" }}>
            ↓ from {stats.actionRate.prevPct}%{" "}
            <span className="text-faint">acted vs merged-past</span>
          </div>
        </StatCard>

        <StatCard>
          <div
            className="eyebrow mb-3"
            data-tip="Average inline comments Argus posts per PR. Lower is calmer — less noise to triage."
          >
            Avg comments / PR
          </div>
          <div className="stat-num">{stats.avgComments.value}</div>
          <div className="mt-[9px] text-xs text-accent">
            ↓ from {stats.avgComments.prev} <span className="text-faint">· calmer</span>
          </div>
        </StatCard>
      </Reveal>

      <Reveal className="mb-[22px]">
        <RevealItem className="panel px-[22px] py-5">
          <div className="mb-[18px] flex items-center justify-between">
            <span className="eyebrow">Trends · 14 days</span>
            <span className="text-xs text-faint">this week vs last</span>
          </div>
          <div className="grid grid-cols-1 gap-7 sm:grid-cols-2">
            <TrendPanel
              label="Reviews / day"
              value={stats.trends.reviewsPerDay.value.toString()}
              delta={`↑ ${stats.trends.reviewsPerDay.deltaPct}%`}
              deltaClass="text-accent"
              series={stats.trends.reviewsPerDay.series}
              color="var(--color-accent)"
            />
            <div className="sm:border-l sm:border-[var(--line-07)] sm:pl-7">
              <TrendPanel
                label="Findings / day"
                value={stats.trends.findingsPerDay.value.toString()}
                delta={`↓ ${Math.abs(stats.trends.findingsPerDay.deltaPct)}%`}
                deltaClass="text-faint"
                series={stats.trends.findingsPerDay.series}
                color="var(--color-nit)"
              />
            </div>
          </div>
        </RevealItem>
      </Reveal>

      <Reveal>
        <RevealItem className="panel px-[22px] pt-[6px] pb-3">
          <div className="flex items-center justify-between pt-4 pb-[6px]">
            <span className="eyebrow">Recent reviews</span>
            <Link href="/reviews" className="text-xs text-accent hover:underline">
              View all reviews →
            </Link>
          </div>
          <LiveStrip live={live} variant="feed" />
          {recent.map((r) => (
            <ReviewRow key={r.id} review={r} variant="feed" />
          ))}
        </RevealItem>
      </Reveal>
    </div>
  );
}
