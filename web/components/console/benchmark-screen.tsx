"use client";

import type { ReactNode } from "react";
import { Reveal, RevealItem } from "@/components/ui/reveal";
import { CheckIcon, EyeMark, InfoCircle, MinusIcon, WarnTriangle } from "@/components/ui/icons";
import type { BenchRow, Gate, GateAxisMetric, GateFloor, GateVerdict, PerRepoRow } from "@/lib/types";
import { ScreenHeader } from "./screen-header";

const GRID = "grid grid-cols-[1fr_140px_1fr] items-center sm:grid-cols-[1fr_220px_1fr]";

const REPO_GRID =
  "grid grid-cols-[1.4fr_repeat(5,minmax(0,1fr))] items-center gap-x-2";

// Cheap proxies that a quiet-or-noisy reviewer can game. The gate keeps these as
// diagnostics only, never as the thing that decides a verdict.
const GAMEABLE = new Set(["Comments / PR", "Merged-past (ignored)", "Bug-class share"]);

function severityTint(critMajor: number, comments: number): {
  background: string;
  borderColor: string;
  color: string;
} {
  const share = comments > 0 ? critMajor / comments : 0;
  if (share >= 0.6) {
    return { background: "var(--crit-10)", borderColor: "var(--crit-28)", color: "var(--sev-critical)" };
  }
  if (share >= 0.35) {
    return { background: "var(--major-14)", borderColor: "var(--warn-22)", color: "var(--sev-major)" };
  }
  return { background: "var(--accent-08)", borderColor: "var(--accent-18)", color: "var(--sev-minor)" };
}

// State -> token. Amber accent is the "good/met" register in this palette; crit
// red is failure; faint is not-yet-met. No neon green; stays on Amber Terminal.
function stateColor(met: boolean, failing?: boolean): string {
  if (failing) return "var(--color-critical)";
  if (met) return "var(--color-accent)";
  return "var(--color-faint)";
}

const VERDICT_TONE: Record<GateVerdict, { color: string; bg: string; border: string; label: string }> = {
  PROMOTE: { color: "var(--color-accent)", bg: "var(--accent-08)", border: "var(--accent-35)", label: "PROMOTE" },
  "KEEP-GATHERING": { color: "var(--color-warn-text)", bg: "var(--warn-06)", border: "var(--warn-22)", label: "KEEP GATHERING" },
  KILL: { color: "var(--color-critical)", bg: "var(--crit-08)", border: "var(--crit-28)", label: "KILL" },
  "INSUFFICIENT-DATA": { color: "var(--color-accent-dim)", bg: "var(--accent-04)", border: "var(--accent-25)", label: "INSUFFICIENT DATA" },
};

function StatusGlyph({ met, failing }: { met: boolean; failing?: boolean }) {
  if (failing) return <WarnTriangle className="text-[color:var(--color-critical)]" />;
  if (met) return <CheckIcon className="text-[color:var(--color-accent)]" />;
  return <MinusIcon className="text-faint" />;
}

function VerdictBanner({ gate }: { gate: Gate }) {
  const tone = VERDICT_TONE[gate.verdict];
  const { day, totalDays, daysRemaining, ceiling, started } = gate.clock;
  return (
    <div
      className="mb-[18px] max-w-[920px] rounded-[12px] px-[20px] py-[16px]"
      style={{ background: tone.bg, border: `1px solid ${tone.border}`, contain: "layout style" }}
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-[11px]">
          <span className="text-[10px] font-semibold uppercase tracking-[0.14em] text-faint">verdict</span>
          <span
            className="rounded-[6px] px-[11px] py-[4px] text-[15px] font-bold tracking-[0.04em]"
            style={{ color: tone.color, border: `1px solid ${tone.border}`, background: "var(--color-ground)" }}
          >
            {tone.label}
          </span>
        </div>
        <div className="text-right">
          <div className="font-mono text-[13px] tabular-nums" style={{ color: "var(--color-prose)" }}>
            {started ? `Day ${day} / ${totalDays}` : "clock not started"}
          </div>
          <div className="text-[11px] text-faint">
            {started ? `${daysRemaining}d to ceiling · ${ceiling}` : "awaiting first review"}
          </div>
        </div>
      </div>
      <p className="mt-[10px] max-w-[680px] text-[12.5px] leading-[1.55] text-dim">{gate.verdictWhy}</p>
    </div>
  );
}

function FloorBar({ floor }: { floor: GateFloor }) {
  const pct = floor.floor > 0 ? Math.min(100, (floor.value / floor.floor) * 100) : 0;
  const color = stateColor(floor.met, floor.failing);
  return (
    <div style={{ contain: "layout style" }}>
      <div className="mb-[5px] flex items-baseline justify-between">
        <span className="text-[12px]" style={{ color: "var(--color-prose)" }}>{floor.label}</span>
        <span className="font-mono text-[12px] tabular-nums" style={{ color }}>
          {floor.value} / {floor.floor}
        </span>
      </div>
      <div className="h-[7px] w-full overflow-hidden rounded-full" style={{ background: "var(--color-inset)" }}>
        <div
          className="h-full rounded-full transition-[width]"
          style={{ width: `${pct}%`, background: color, boxShadow: floor.met ? `0 0 7px ${color}` : "none" }}
        />
      </div>
      {floor.detail && (
        <div className="mt-[4px] text-[10.5px]" style={{ color: floor.failing ? "var(--color-critical)" : "var(--color-faint)" }}>
          {floor.detail}
        </div>
      )}
    </div>
  );
}

function AxisCard({
  tag,
  title,
  status,
  rule,
}: {
  tag: string;
  title: string;
  status: string;
  rule: string;
}) {
  const blocked = status === "blocked" || status === "not-started";
  return (
    <div
      className="rounded-[10px] px-[15px] py-[13px]"
      style={{ background: "var(--color-surface)", border: "1px solid var(--line-07)", contain: "layout style" }}
    >
      <div className="flex items-center justify-between">
        <span className="font-mono text-[11px] font-semibold tracking-[0.08em] text-accent">{tag}</span>
        <span
          className="rounded-[4px] px-[7px] py-[2px] text-[9px] font-semibold uppercase tracking-[0.1em]"
          style={{
            color: blocked ? "var(--color-faint)" : "var(--color-accent)",
            border: `1px solid ${blocked ? "var(--line-09)" : "var(--accent-25)"}`,
          }}
        >
          {status.replace("-", " ")}
        </span>
      </div>
      <div className="mt-[6px] text-[13px] font-semibold" style={{ color: "var(--color-heading)" }}>{title}</div>
      <p className="mt-[5px] text-[11.5px] leading-[1.5] text-dim">{rule}</p>
    </div>
  );
}

function TelemetryChip({ m }: { m: GateAxisMetric }) {
  const color = stateColor(m.met);
  return (
    <div
      className="flex min-w-[120px] flex-1 items-center gap-[10px] rounded-[9px] px-[13px] py-[10px]"
      style={{ background: "var(--color-surface)", border: "1px solid var(--line-07)", contain: "layout style" }}
    >
      <StatusGlyph met={m.met} />
      <div>
        <div className="flex items-baseline gap-[6px]">
          <span className="font-mono text-[15px] font-semibold tabular-nums" style={{ color }}>{m.value}</span>
          <span className="text-[10px] text-faint">{m.floor}</span>
        </div>
        <div className="text-[10.5px] text-faint">{m.label}{m.detail ? ` · ${m.detail}` : ""}</div>
      </div>
    </div>
  );
}

function AwaitingPanel({ title, caption, children }: { title: string; caption: string; children: ReactNode }) {
  return (
    <RevealItem className="panel max-w-[920px] overflow-hidden p-[16px]">
      <div className="mb-[10px] flex items-center justify-between">
        <span className="eyebrow">{title}</span>
        <span className="text-[10px] font-semibold uppercase tracking-[0.1em] text-faint">awaiting corpus</span>
      </div>
      {children}
      <p className="mt-[10px] text-[11px] leading-[1.5] text-faint">{caption}</p>
    </RevealItem>
  );
}

export function BenchmarkScreen({
  rows,
  note,
  perRepo,
  argusEmpty,
  gate,
}: {
  rows: BenchRow[];
  note: string;
  perRepo: PerRepoRow[];
  argusEmpty: boolean;
  gate?: Gate;
}) {
  return (
    <div>
      <ScreenHeader
        title="Benchmark"
        subtitle={
          <>
            Argus vs CodeRabbit · the bake-off gate, not a leaderboard
          </>
        }
      />

      {gate ? (
        <>
          <VerdictBanner gate={gate} />

          {/* Floors: the milestone gate the verdict waits on. */}
          <Reveal>
            <RevealItem className="panel mb-[18px] max-w-[920px] overflow-hidden p-[18px]">
              <div className="mb-[14px] flex items-center justify-between">
                <span className="eyebrow">Gate progress · floors</span>
                <span className="text-[11px] text-faint">verdict ships when every floor is met (or at the 28-day ceiling)</span>
              </div>
              <div className="grid gap-[15px] sm:grid-cols-2">
                {gate.floors.map((f) => (
                  <FloorBar key={f.key} floor={f} />
                ))}
              </div>
            </RevealItem>
          </Reveal>

          {/* The three axes that define winning. */}
          <Reveal>
            <RevealItem className="mb-[18px] grid max-w-[920px] gap-[12px] sm:grid-cols-3">
              <AxisCard tag="AXIS A" title={gate.axisA.title} status={gate.axisA.status} rule={gate.axisA.rule} />
              <AxisCard
                tag="AXIS B"
                title={gate.axisB.title}
                status={gate.axisB.status}
                rule="Operator agrees with ≥ 50% of findings (within 10pp of CR), latency stays in SLO, and the engine runs 7 days crash-free."
              />
              <AxisCard tag="AXIS C" title={gate.axisC.title} status={gate.axisC.status} rule={gate.axisC.rule} />
            </RevealItem>
          </Reveal>

          {/* Axis B telemetry, the deployment gate, live. */}
          <Reveal>
            <RevealItem className="panel mb-[18px] max-w-[920px] overflow-hidden p-[16px]">
              <div className="mb-[11px] flex items-center justify-between">
                <span className="eyebrow">Axis B · deployment telemetry</span>
                <span className="text-[11px] text-faint">red/green vs threshold</span>
              </div>
              <div className="flex flex-wrap gap-[10px]">
                {gate.axisB.metrics.map((m) => (
                  <TelemetryChip key={m.label} m={m} />
                ))}
              </div>
              {gate.dominantRepo && (
                <div
                  className="mt-[12px] flex items-center gap-[9px] rounded-[8px] px-[13px] py-[9px]"
                  style={{ background: "var(--warn-06)", border: "1px solid var(--warn-22)" }}
                >
                  <WarnTriangle className="text-[color:var(--color-warn-text)]" />
                  <span className="text-[11.5px] leading-[1.5]" style={{ color: "var(--color-warn-text)" }}>
                    Sample is one-repo-dominated: <span className="font-semibold">{gate.dominantRepo.repo}</span> is{" "}
                    {gate.dominantRepo.pct}% of reviews. Stratify or weight by repo before reading any verdict.
                  </span>
                </div>
              )}
            </RevealItem>
          </Reveal>
        </>
      ) : (
        argusEmpty && (
          <div
            className="mb-[18px] flex max-w-[920px] items-center gap-3 rounded-[11px] px-[18px] py-[12px]"
            style={{ background: "var(--accent-04)", border: "1px solid var(--accent-25)" }}
          >
            <EyeMark size={18} strokeWidth={1.8} />
            <div className="text-[12.5px] leading-[1.55] text-dim">
              <span className="font-semibold text-accent">No Argus runs yet</span>: the gate has nothing to score.
            </div>
          </div>
        )
      )}

      {/* Tale of the tape: real numbers, but the cheap rows are tagged gameable. */}
      <Reveal>
        <RevealItem className="panel max-w-[920px] overflow-hidden">
          <div className={GRID}>
            <div className="px-4 py-5 text-right sm:px-6">
              <div className="inline-flex items-center gap-[9px]">
                <EyeMark size={20} strokeWidth={1.8} />
                <span className="text-[17px] font-semibold tracking-[0.03em] text-accent">ARGUS</span>
              </div>
            </div>
            <div className="text-center text-[11px] font-semibold uppercase tracking-[0.12em] text-faint">
              metric
            </div>
            <div className="px-4 py-5 sm:px-6">
              <span className="text-[17px] font-semibold text-dim">CodeRabbit</span>
            </div>
          </div>

          {rows.map((b) => {
            const gameable = GAMEABLE.has(b.metric);
            return (
              <div key={b.metric} className={`${GRID} border-t border-[var(--line-07)]`}>
                <div className="px-4 py-4 text-right sm:px-6">
                  {b.argus === "—" ? (
                    <span className="text-[13px] font-semibold uppercase tracking-[0.08em] text-faint">
                      {b.pending ? "—" : "no data"}
                    </span>
                  ) : (
                    <span className="inline-flex items-center gap-2">
                      <span className="h-[6px] w-[6px] rounded-full bg-accent" style={{ boxShadow: "0 0 7px var(--accent-70)" }} />
                      <span className="text-[24px] font-semibold tabular-nums text-text">{b.argus}</span>
                    </span>
                  )}
                </div>
                <div className="px-3 py-4 text-center">
                  <div className="text-[13px]" style={{ color: "var(--color-prose)" }}>{b.metric}</div>
                  <div className="mt-[3px] text-[11px] text-faint">{b.note}</div>
                  {gameable && (
                    <div className="mt-[6px]">
                      <span
                        data-tip="Diagnostic only: a quiet or cherry-picking reviewer can game this. It never decides the verdict."
                        className="rounded-[5px] px-[8px] py-[2px] text-[9px] font-bold tracking-[0.08em]"
                        style={{ color: "var(--color-warn-text)", border: "1px solid var(--warn-22)" }}
                      >
                        GAMEABLE
                      </span>
                    </div>
                  )}
                  {b.pending && (
                    <div className="mt-[6px]">
                      <span
                        data-tip="Recall and false-positive rate need the dual corpus (escaped ∪ CR-caught-pre-merge) scored locally, not built yet."
                        className="rounded-[5px] px-[9px] py-[3px] text-[9px] font-bold tracking-[0.1em] text-accent-dim"
                        style={{ border: "1px solid var(--accent-25)" }}
                      >
                        THE REAL GATE · PENDING
                      </span>
                    </div>
                  )}
                </div>
                <div className="px-4 py-4 sm:px-6">
                  <span
                    className="text-[24px] font-semibold tabular-nums"
                    style={{ color: b.pending ? "var(--color-faint)" : "var(--color-dim)" }}
                  >
                    {b.codeRabbit}
                  </span>
                </div>
              </div>
            );
          })}
        </RevealItem>
      </Reveal>

      {/* What the recall verdict will look like once the corpus exists. */}
      <Reveal>
        <RevealItem className="mt-[18px] grid max-w-[920px] gap-[12px] sm:grid-cols-2">
          <AwaitingPanel title="Axis A · McNemar 2×2" caption="Each escaped/CR-caught bug scored for both reviewers; promote when Argus-only beats CR-only at p < 0.10.">
            <div className="grid grid-cols-2 gap-[6px] font-mono text-[11px]">
              {["both", "Argus only (b)", "CR only (c)", "neither"].map((cell) => (
                <div
                  key={cell}
                  className="flex h-[52px] items-center justify-center rounded-[7px] text-center text-faint"
                  style={{ background: "var(--color-inset)", border: "1px dashed var(--line-09)" }}
                >
                  {cell}
                </div>
              ))}
            </div>
          </AwaitingPanel>
          <AwaitingPanel title="Axis A · dominance plot" caption="Recall vs false-positive rate, Wilson confidence rectangles; Argus wins inside the shaded quadrant.">
            <div
              className="flex h-[116px] items-center justify-center rounded-[7px] text-[11px] text-faint"
              style={{ background: "var(--color-inset)", border: "1px dashed var(--line-09)" }}
            >
              recall ↑ / false-positive →
            </div>
          </AwaitingPanel>
        </RevealItem>
      </Reveal>

      <Reveal>
        <RevealItem className="panel mt-[18px] max-w-[920px] overflow-hidden">
          <div className="flex items-center justify-between px-[18px] pb-[10px] pt-[16px]">
            <span className="eyebrow">CodeRabbit snapshot corpus · by repo</span>
            <span className="text-[11px] text-faint">2026-06-15 mined threads · crit+major tinted</span>
          </div>
          <div className={`${REPO_GRID} px-[18px] pb-[8px] text-[10px] font-semibold uppercase tracking-[0.1em] text-faint`}>
            <span>repo</span>
            <span className="text-right">CR&nbsp;comments</span>
            <span className="text-right">resolved</span>
            <span className="text-right">merged-past</span>
            <span className="text-right">crit+major</span>
            <span className="text-right">cites&nbsp;guideline</span>
          </div>
          {perRepo.map((r) => {
            const tint = severityTint(r.critMajor, r.crComments);
            return (
              <div
                key={r.repo}
                className={`${REPO_GRID} border-t border-[var(--line-07)] px-[18px] py-[10px] text-[13px]`}
                style={{ contain: "layout style" }}
              >
                <span className="truncate font-medium" style={{ color: "var(--color-prose)" }}>{r.repo}</span>
                <span className="text-right tabular-nums text-text">{r.crComments}</span>
                <span className="text-right tabular-nums text-dim">{r.resolved}</span>
                <span className="text-right tabular-nums text-dim">{r.mergedPast}</span>
                <span className="text-right">
                  <span
                    className="inline-block min-w-[34px] rounded-[5px] px-[8px] py-[2px] text-center text-[12.5px] font-semibold tabular-nums"
                    style={{ background: tint.background, border: `1px solid ${tint.borderColor}`, color: tint.color }}
                  >
                    {r.critMajor}
                  </span>
                </span>
                <span className="text-right tabular-nums text-dim">{r.citesGuideline}</span>
              </div>
            );
          })}
        </RevealItem>
      </Reveal>

      {/* What this page is NOT: kills the old "fake leaderboard" reading. */}
      <div
        className="mt-[18px] flex max-w-[920px] gap-3 rounded-[11px] px-[18px] py-[14px]"
        style={{ background: "var(--accent-04)", border: "1px solid var(--accent-14)" }}
      >
        <InfoCircle className="mt-px shrink-0 text-accent-dim" />
        <div className="text-[12.5px] leading-[1.6] text-dim">
          <span className="font-semibold" style={{ color: "var(--color-prose)" }}>What this page is not:</span>{" "}
          not a verdict yet (Axis A recall is unscored), not a leaderboard (the bold rows are gameable diagnostics), and
          no longer a demo. {note}
        </div>
      </div>
    </div>
  );
}
