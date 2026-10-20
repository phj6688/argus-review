"use client";

import Link from "next/link";
import { useState } from "react";
import { Reveal, RevealItem } from "@/components/ui/reveal";
import {
  CheckIcon,
  ChevronLeft,
  ChevronRight,
  MinusIcon,
} from "@/components/ui/icons";
import type { Comment, ReviewDetail, VerifiedFinding } from "@/lib/types";
import { SEV_LABEL, SEV_TIP, sevVar } from "@/lib/utils";
import { ScreenHeader } from "./screen-header";

type FState = "accepted" | "suppressed" | undefined;

const DECISION = {
  changes_requested: { label: "Changes requested", color: "var(--color-critical)" },
  comment: { label: "Commented", color: "var(--color-accent)" },
  approve: { label: "Approved", color: "var(--color-sage)" },
  // Nothing found, but no reviewer read the whole change, so there is nothing to
  // approve. Amber, not green: a missing review must never look like a pass.
  insufficient_evidence: { label: "Not reviewed", color: "var(--color-accent)" },
} as const;

const UNKNOWN_DECISION = { label: "Unknown", color: "var(--color-dim)" } as const;

const VERDICT = {
  confirmed: {
    label: "CONFIRMED",
    color: "var(--color-sage)",
    border: "var(--sage-30)",
    bg: "var(--sage-10)",
    tip: "The skeptical verification pass tried to refute this finding and could not — the issue and its fix hold.",
  },
  refuted: {
    label: "REFUTED",
    color: "var(--color-nit)",
    border: "var(--line-14)",
    bg: "var(--bone-04)",
    tip: "Verification could not confirm this from the evidence — downgraded or discarded to keep false positives low.",
  },
} as const;

function vof(v: VerifiedFinding) {
  return v.verdict.is_real ? VERDICT.confirmed : VERDICT.refuted;
}

function DiffBlock({ file, diff }: { file: string; diff: string }) {
  const lines = diff.split("\n");
  return (
    <div
      className="mt-[10px] overflow-hidden rounded-[9px] border border-[var(--line-10)] bg-inset"
    >
      <div className="border-b border-[var(--line-07)] px-[14px] py-[7px] font-mono text-[11px] text-faint">
        {file}
      </div>
      {lines.map((ln, i) => {
        const kind = ln.startsWith("-") ? "del" : ln.startsWith("+") ? "add" : "ctx";
        const style =
          kind === "del"
            ? { background: "var(--crit-10)", color: "var(--color-diff-del)" }
            : kind === "add"
              ? { background: "var(--sage-13)", color: "var(--color-sage)" }
              : { color: "var(--color-diff-ctx)" };
        return (
          <div
            key={i}
            className="whitespace-pre px-[14px] py-px font-mono text-[12.5px] leading-[1.7]"
            style={style}
          >
            {ln}
          </div>
        );
      })}
    </div>
  );
}

function BugCard({
  vf,
  id,
  state,
  onMark,
}: {
  vf: VerifiedFinding;
  id: string;
  state: FState;
  onMark: (id: string, s: FState) => void;
}) {
  const f = vf.finding;
  const v = vof(vf);
  const confPct = `${Math.round(f.confidence * 100)}%`;
  return (
    <RevealItem
      className="panel card-contain overflow-hidden"
      style={{ opacity: state === "suppressed" ? 0.5 : 1, transition: "opacity .2s ease" }}
    >
      <div className="p-5">
        <div className="flex items-start gap-[14px]">
          <span
            data-tip={SEV_TIP[f.severity]}
            className="mt-[2px] shrink-0 rounded-[6px] px-[9px] py-1 text-[10px] font-bold uppercase tracking-[0.08em]"
            style={{ color: sevVar(f.severity), background: "var(--bone-04)", border: `1px solid ${sevVar(f.severity)}` }}
          >
            {SEV_LABEL[f.severity]}
          </span>
          <div className="min-w-0 flex-1">
            <div className="mb-[5px] text-base font-semibold text-text">{f.title}</div>
            <div className="flex items-center gap-3 font-mono text-xs">
              <span
                data-tip="File and line this finding anchors to — click to jump to it in the diff."
                className="cursor-pointer text-accent hover:underline"
              >
                {f.file}:{f.line}
              </span>
              {f.rule_id && (
                <span
                  data-tip="Grounded in a repo house rule (CLAUDE.md). Argus can cite a rule but never invent one."
                  className="text-faint"
                >
                  rule {f.rule_id}
                </span>
              )}
            </div>
          </div>
          <div className="w-[116px] shrink-0 text-right max-sm:hidden">
            <div
              className="mb-[5px] text-[10px] uppercase tracking-[0.08em] text-faint"
              data-tip="The model self-reported confidence the issue is real (0 to 1), recorded before verification."
            >
              confidence
            </div>
            <div className="flex items-center gap-2">
              <div className="h-[5px] flex-1 overflow-hidden rounded-[3px]" style={{ background: "var(--bone-10)" }}>
                <span className="block h-full" style={{ width: confPct, background: sevVar(f.severity) }} />
              </div>
              <span className="text-xs tabular-nums text-text">{confPct}</span>
            </div>
          </div>
        </div>

        {f.description && (
          <p className="mt-[14px] max-w-[760px] text-[13.5px] leading-[1.6]" style={{ color: "var(--color-prose)" }}>
            {f.description}
          </p>
        )}

        {f.proposed_fix && (
          <details className="mt-[14px]">
            <summary className="inline-flex cursor-pointer select-none items-center gap-[7px] py-[5px] text-[12.5px] text-dim hover:text-accent">
              <ChevronRight className="chev" />
              Proposed fix
            </summary>
            <DiffBlock file={f.file} diff={f.proposed_fix} />
          </details>
        )}
      </div>

      <div
        className="flex items-start gap-3 border-t border-[var(--line-07)] px-5 py-[13px]"
        style={{ background: v.bg }}
      >
        <span
          data-tip={v.tip}
          className="shrink-0 rounded-[6px] px-[9px] py-1 text-[10px] font-bold tracking-[0.1em]"
          style={{ color: v.color, border: `1px solid ${v.border}` }}
        >
          {v.label}
        </span>
        <div className="flex-1 text-[12.5px] leading-[1.55] text-dim">
          <span className="text-faint">verifier {vf.verdict.confidence.toFixed(2)} · </span>
          {vf.verdict.reasoning}
        </div>
      </div>

      <div className="flex items-center gap-[10px] border-t border-[var(--line-06)] px-5 py-3">
        {!state && (
          <>
            <button
              type="button"
              onClick={() => onMark(id, "accepted")}
              className="rounded-[8px] border px-[14px] py-[7px] text-[12.5px] font-medium transition-colors"
              style={{ borderColor: "var(--sage-35)", background: "var(--sage-08)", color: "var(--color-sage)" }}
            >
              Accept &amp; post
            </button>
            <button
              type="button"
              onClick={() => onMark(id, "suppressed")}
              className="rounded-[8px] border border-[var(--line-14)] bg-transparent px-[14px] py-[7px] text-[12.5px] font-medium text-dim transition-colors hover:text-text"
            >
              Suppress
            </button>
            <span className="ml-auto text-[11.5px] text-faint max-sm:hidden">
              posts an inline comment on the PR
            </span>
          </>
        )}
        {state === "accepted" && (
          <>
            <span className="inline-flex items-center gap-[7px] text-[12.5px] text-sage">
              <CheckIcon /> Accepted · inline comment posted
            </span>
            <UndoButton onClick={() => onMark(id, undefined)} />
          </>
        )}
        {state === "suppressed" && (
          <>
            <span className="inline-flex items-center gap-[7px] text-[12.5px] text-dim">
              <MinusIcon /> Suppressed · added to repo suppression list
            </span>
            <UndoButton onClick={() => onMark(id, undefined)} />
          </>
        )}
      </div>
    </RevealItem>
  );
}

function UndoButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="ml-auto rounded-[8px] border border-[var(--line-14)] bg-transparent px-3 py-[6px] text-xs text-faint transition-colors hover:text-text"
    >
      Undo
    </button>
  );
}

function NitRow({ vf }: { vf: VerifiedFinding }) {
  const f = vf.finding;
  const v = vof(vf);
  return (
    <div className="flex items-center gap-[14px] border-t border-[var(--line-05)] px-[18px] py-[13px] first:border-t-0">
      <span
        data-tip={SEV_TIP[f.severity]}
        className="w-[52px] shrink-0 rounded-[5px] px-0 py-[3px] text-center text-[9px] font-bold uppercase tracking-[0.06em]"
        style={{ color: sevVar(f.severity), background: "var(--bone-04)", border: `1px solid ${sevVar(f.severity)}` }}
      >
        {SEV_LABEL[f.severity]}
      </span>
      <span className="min-w-0 flex-1 truncate text-[13.5px] text-text">{f.title}</span>
      <span className="whitespace-nowrap font-mono text-[11.5px] text-faint max-sm:hidden">
        {f.file}:{f.line}
      </span>
      <span className="w-16 text-right text-[11px] text-faint max-sm:hidden">
        conf {Math.round(f.confidence * 100)}%
      </span>
      <span
        data-tip={v.tip}
        className="w-[92px] shrink-0 text-right text-[10px] font-bold tracking-[0.06em]"
        style={{ color: v.color }}
      >
        {v.label}
      </span>
    </div>
  );
}

function ThreadItem({ c }: { c: Comment }) {
  const bot = c.role === "bot";
  return (
    <div className="flex gap-[13px]">
      <span
        className="flex h-[30px] w-[30px] shrink-0 items-center justify-center rounded-[8px] text-xs font-semibold"
        style={{
          color: bot ? "var(--color-accent)" : "var(--color-text)",
          background: bot ? "var(--accent-12)" : "var(--bone-10)",
        }}
      >
        {c.author.charAt(0).toUpperCase()}
      </span>
      <div className="flex-1 rounded-[11px] border border-[var(--line)] bg-surface px-4 py-3">
        <div className="mb-[6px] flex items-center gap-[10px]">
          <span className="text-[13px] font-semibold text-text">{c.author}</span>
          <span className="text-xs text-faint">{c.time}</span>
          {c.badge && (
            <span
              className="rounded-[5px] px-2 py-[2px] text-[10px] font-semibold tracking-[0.04em]"
              style={{ color: "var(--color-critical)", border: "1px solid var(--color-critical)" }}
            >
              {c.badge}
            </span>
          )}
          {c.resolved && (
            <span
              className="rounded-[5px] px-2 py-[2px] text-[10px] font-semibold tracking-[0.04em]"
              style={{ color: "var(--color-sage)", border: "1px solid var(--sage-30)" }}
            >
              RESOLVED
            </span>
          )}
        </div>
        <div className="text-[13.5px] leading-[1.6]" style={{ color: "var(--color-prose)" }}>
          {c.body}
        </div>
      </div>
    </div>
  );
}

const CHIP_STYLE = {
  background: "var(--accent-08)",
  border: "1px solid var(--accent-18)",
  color: "var(--color-accent-dim)",
} as const;

/** Which model answered. The configured model is shown only when nothing ran, so a
 *  failed run never reads as if the configured model had produced it. */
function ModelUsed({ detail }: { detail: ReviewDetail }) {
  if (detail.model_used) {
    return (
      <span
        data-testid="model-used"
        data-tip="The model that actually answered this review."
        className="rounded-[6px] px-[9px] py-[3px] text-[11px]"
        style={CHIP_STYLE}
      >
        model used · {detail.model_used}
      </span>
    );
  }
  if (detail.status === "running") {
    return (
      <span data-testid="model-used" className="text-[11px]">
        running · {detail.model}
      </span>
    );
  }
  if (detail.status !== "error") {
    // A finished run whose record names no model (a row from before the field
    // existed): show the configured model without claiming that it answered.
    return (
      <span
        data-testid="model-used"
        className="rounded-[6px] px-[9px] py-[3px] text-[11px]"
        style={CHIP_STYLE}
      >
        model · {detail.model}
      </span>
    );
  }
  return (
    <span
      data-testid="model-used"
      data-tip="No model produced this review. This is the model it was configured with."
      className="rounded-[6px] px-[9px] py-[3px] text-[11px]"
      style={{ ...CHIP_STYLE, opacity: 0.6 }}
    >
      no model ran · configured {detail.model}
    </span>
  );
}

function RunNotes({ detail }: { detail: ReviewDetail }) {
  const fell = detail.fallback_from && detail.model_used;
  if (!fell && !detail.error) return null;
  return (
    <div className="mb-2 mt-4 flex flex-col gap-[10px]">
      {fell && (
        <div
          data-testid="fallback-note"
          className="panel px-4 py-3 text-[12.5px] leading-[1.6] text-dim"
        >
          <span className="font-semibold text-text">Fallback model.</span>{" "}
          <span className="font-mono">{detail.fallback_from}</span> failed, so{" "}
          <span className="font-mono">{detail.model_used}</span> reviewed this change.
          {detail.fallback_reason && (
            <div className="mt-1 break-words font-mono text-[12px] text-faint">
              {detail.fallback_reason}
            </div>
          )}
        </div>
      )}
      {detail.error && (
        <div
          data-testid="run-error"
          role="alert"
          className="panel px-4 py-3 text-[12.5px] leading-[1.6]"
          style={{ borderColor: "var(--crit-28)" }}
        >
          <span className="font-semibold" style={{ color: "var(--color-critical)" }}>
            Review failed.
          </span>{" "}
          <span className="text-dim">No findings were produced, so this is not a pass.</span>
          <div className="mt-1 whitespace-pre-wrap break-words font-mono text-[12px] text-faint">
            {detail.error}
          </div>
        </div>
      )}
    </div>
  );
}

export function PrDetailScreen({ detail }: { detail: ReviewDetail }) {
  const [fstate, setFstate] = useState<Record<string, FState>>({});
  const onMark = (id: string, s: FState) =>
    setFstate((prev) => ({ ...prev, [id]: s }));
  // A value the API grows and this map has not caught up with must render, not
  // crash the screen on an undefined lookup.
  const decision = DECISION[detail.decision] ?? UNKNOWN_DECISION;
  const bugCount = detail.bugs.length;
  const nitCount = detail.nits.length;

  return (
    <div>
      <Link
        href="/reviews"
        className="mb-[18px] inline-flex items-center gap-[7px] text-[13px] text-dim transition-colors hover:text-accent"
      >
        <ChevronLeft />
        All reviews
      </Link>

      <ScreenHeader
        className="mb-2"
        title={detail.title}
        subtitle={
          <div className="flex flex-wrap items-center gap-4 text-[12.5px] text-faint">
            {detail.branch && <span className="font-mono text-dim">{detail.branch}</span>}
            {detail.author && <span>by {detail.author}</span>}
            {detail.filesChanged !== undefined && (
              <span>
                {detail.filesChanged} {detail.filesChanged === 1 ? "file" : "files"}
                {detail.added !== undefined && detail.removed !== undefined && (
                  <>
                    {" · "}
                    <span className="text-sage">+{detail.added}</span>{" "}
                    <span style={{ color: "var(--color-diff-del)" }}>−{detail.removed}</span>
                  </>
                )}
              </span>
            )}
            <ModelUsed detail={detail} />
            <span>{detail.time}</span>
          </div>
        }
        controls={
          <div className="text-right">
            <div
              className="inline-flex items-center gap-2 rounded-[9px] px-[14px] py-[9px]"
              style={{ background: "var(--crit-08)", border: "1px solid var(--crit-28)" }}
            >
              <span
                className="h-2 w-2 rounded-full"
                style={{ background: decision.color, boxShadow: "0 0 8px var(--crit-60)" }}
              />
              <span
                data-tip="Argus overall decision on this PR: approve, comment, request changes, or not reviewed (nothing found, but no reviewer read the whole change)."
                className="text-[13px] font-semibold"
                style={{ color: decision.color }}
              >
                {decision.label}
              </span>
            </div>
            <div className="mt-2 text-xs text-faint">
              {bugCount} bug-class · {nitCount} nits folded
            </div>
          </div>
        }
      />
      <div className="mb-2 font-mono text-[13px]">
        <span className="text-dim">{detail.repo}</span> <span className="text-faint">#{detail.pr}</span>
      </div>
      <RunNotes detail={detail} />

      <div className="my-6 h-px bg-[var(--line)]" />

      <div className="mb-[14px] flex items-center gap-[10px]">
        <span className="text-[11px] font-semibold uppercase tracking-[0.12em] text-heading">
          Bug-class findings
        </span>
        <span className="text-[11px] text-faint">verified &amp; ranked</span>
      </div>
      <Reveal className="mb-8 flex flex-col gap-[14px]">
        {detail.bugs.map((vf, i) => (
          <BugCard key={i} vf={vf} id={`b${i}`} state={fstate[`b${i}`]} onMark={onMark} />
        ))}
      </Reveal>

      <div className="mb-[14px] flex items-center gap-[10px]">
        <span className="text-[11px] font-semibold uppercase tracking-[0.12em] text-faint">Nits</span>
        <span className="text-[11px] text-faint">folded · low signal</span>
      </div>
      <div className="panel mb-8 overflow-hidden">
        {detail.nits.map((vf, i) => (
          <NitRow key={i} vf={vf} />
        ))}
      </div>

      <div className="mb-4 flex items-center gap-[10px]">
        <span className="text-[11px] font-semibold uppercase tracking-[0.12em] text-faint">
          Comment thread
        </span>
      </div>
      <div className="flex flex-col gap-[14px]">
        {detail.thread.map((c, i) => (
          <ThreadItem key={i} c={c} />
        ))}
      </div>
    </div>
  );
}
