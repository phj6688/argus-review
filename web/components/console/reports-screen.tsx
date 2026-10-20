"use client";

import { useState } from "react";
import { Reveal, RevealItem } from "@/components/ui/reveal";
import { Segmented } from "@/components/ui/segmented";
import { DownloadIcon } from "@/components/ui/icons";
import type { Report, ReportPeriod } from "@/lib/types";
import { sevVar } from "@/lib/utils";
import { ScreenHeader } from "./screen-header";

function HeroStat({ label, value }: { label: string; value: number }) {
  return (
    <div className="panel px-5 py-[18px]">
      <div className="eyebrow mb-3">{label}</div>
      <div className="text-[36px] font-semibold leading-none tabular-nums text-text">{value}</div>
    </div>
  );
}

function Bar({ width, color }: { width: string; color: string }) {
  return (
    <div className="h-[6px] overflow-hidden rounded-[4px]" style={{ background: "var(--bone-07)" }}>
      <span className="block h-full" style={{ width, background: color }} />
    </div>
  );
}

export function ReportsScreen({ reports }: { reports: Record<ReportPeriod, Report> }) {
  const [period, setPeriod] = useState<ReportPeriod>("weekly");
  const rep = reports[period];
  const maxCat = Math.max(...rep.category.map((c) => c.value));
  const maxVol = Math.max(...rep.volume.map((v) => v.value));

  const onExport = () => {
    const blob = new Blob([JSON.stringify(rep, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `argus-${rep.period}-report.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div>
      <ScreenHeader
        title={rep.title}
        subtitle={rep.range}
        controls={
          <>
            <Segmented<ReportPeriod>
              options={[
                { value: "daily", label: "Daily" },
                { value: "weekly", label: "Weekly" },
                { value: "monthly", label: "Monthly" },
              ]}
              value={period}
              onChange={setPeriod}
            />
            <button
              type="button"
              onClick={onExport}
              className="inline-flex items-center gap-[7px] rounded-[9px] border border-[var(--line)] bg-surface px-[13px] py-2 text-[12.5px] text-dim transition-colors hover:text-accent"
            >
              <DownloadIcon />
              Export
            </button>
          </>
        }
      />

      <Reveal className="mb-[14px] grid grid-cols-1 gap-[14px] md:grid-cols-[1fr_1fr_1.4fr]">
        <RevealItem>
          <HeroStat label="Reviews" value={rep.reviews} />
        </RevealItem>
        <RevealItem>
          <HeroStat label="Findings" value={rep.findings} />
        </RevealItem>
        <RevealItem className="panel px-5 py-[18px]">
          <div
            className="eyebrow mb-[14px]"
            data-tip="Acted-on vs. merged-past: how many findings you addressed against how many merged without being touched."
          >
            The action gap
          </div>
          <div className="mb-3 flex items-baseline gap-[10px]">
            <span className="text-[28px] font-semibold leading-none tabular-nums text-accent">
              {rep.acted}
            </span>
            <span className="text-[13px] text-faint">
              acted-on · {rep.merged} merged past ({rep.actedPct}%)
            </span>
          </div>
          <div className="flex h-2 overflow-hidden rounded-[5px]" style={{ background: "var(--line)" }}>
            <span className="h-full bg-accent" style={{ width: `${rep.actedPct}%` }} />
          </div>
        </RevealItem>
      </Reveal>

      <Reveal className="mb-[14px] grid grid-cols-1 gap-[14px] md:grid-cols-2">
        <RevealItem className="panel px-5 py-[18px]">
          <div className="eyebrow mb-[18px]">Findings by severity</div>
          <div className="flex flex-col gap-[14px]">
            {rep.severity.map((s) => (
              <div key={s.label}>
                <div className="mb-[6px] flex justify-between text-[13px]">
                  <span style={{ color: sevVar(s.severity) }}>{s.label}</span>
                  <span className="tabular-nums text-dim">{s.value}</span>
                </div>
                <Bar width={`${(s.value / rep.findings) * 100}%`} color={sevVar(s.severity)} />
              </div>
            ))}
          </div>
        </RevealItem>

        <RevealItem className="panel px-5 py-[18px]">
          <div className="eyebrow mb-[18px]">Findings by category</div>
          <div className="flex flex-col gap-[14px]">
            {rep.category.map((c) => (
              <div key={c.label}>
                <div className="mb-[6px] flex justify-between text-[13px]">
                  <span className="text-text">{c.label}</span>
                  <span className="tabular-nums text-dim">{c.value}</span>
                </div>
                <Bar width={`${(c.value / maxCat) * 100}%`} color={c.color} />
              </div>
            ))}
          </div>
        </RevealItem>
      </Reveal>

      <Reveal className="grid grid-cols-1 gap-[14px] md:grid-cols-[1.4fr_1fr]">
        <RevealItem className="panel px-5 py-[18px]">
          <div className="mb-[18px] flex items-center justify-between">
            <span className="eyebrow">Review volume</span>
            <span className="text-xs text-faint">{rep.volumeLabel}</span>
          </div>
          <div className="flex h-[120px] items-end gap-2">
            {rep.volume.map((b, i) => (
              <div key={i} className="flex h-full flex-1 items-end">
                <span
                  className="w-full rounded-t-[4px]"
                  style={{
                    height: `${(b.value / maxVol) * 100}%`,
                    background: "linear-gradient(180deg, var(--color-accent), var(--accent-35))",
                  }}
                />
              </div>
            ))}
          </div>
        </RevealItem>

        <RevealItem className="panel px-5 py-[18px]">
          <div className="eyebrow mb-4">Top repos</div>
          <div className="flex flex-col gap-[13px]">
            {rep.topRepos.map((r) => (
              <div key={r.repo} className="flex items-center justify-between gap-3">
                <span className="truncate font-mono text-[12.5px]" style={{ color: "var(--color-prose)" }}>
                  {r.repo}
                </span>
                <span className="tabular-nums text-[13px] text-dim">{r.value}</span>
              </div>
            ))}
          </div>
        </RevealItem>
      </Reveal>
    </div>
  );
}
