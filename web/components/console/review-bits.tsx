import Link from "next/link";
import type { LiveReview, ReviewSummary, Severity } from "@/lib/types";
import { cn, SEV_ABBREV, SEV_TALLY_TIP, SEVERITIES, modelChipStyle, sevVar } from "@/lib/utils";

const ACTION = {
  acted: { label: "acted", color: "var(--color-accent)", tip: "You addressed this finding before the PR merged." },
  merged: { label: "merged past", color: "var(--color-faint)", tip: "Argus flagged it, but the PR merged without it being addressed." },
  pending: { label: "pending", color: "var(--color-dim)", tip: "PR is still open — not yet resolved or merged." },
} as const;

export function ModelChip({ model }: { model: string }) {
  return (
    <span
      data-tip="The deep-review model that produced this review."
      className="inline-block whitespace-nowrap rounded-[6px] px-[9px] py-[3px] text-[11px]"
      style={modelChipStyle(model)}
    >
      {model}
    </span>
  );
}

export function ActionLabel({ action }: { action: ReviewSummary["action"] }) {
  const a = ACTION[action];
  return (
    <span data-tip={a.tip} className="text-xs" style={{ color: a.color }}>
      {a.label}
    </span>
  );
}

export function Tally({ counts }: { counts: ReviewSummary["counts"] }) {
  const present = SEVERITIES.filter((s) => counts[s] > 0);
  if (present.length === 0) {
    return (
      <span data-tip="No findings — Argus reviewed this PR and flagged nothing." className="text-xs text-dim">
        clean
      </span>
    );
  }
  return (
    <>
      {present.map((s) => (
        <span
          key={s}
          data-tip={SEV_TALLY_TIP[s]}
          className="inline-flex items-center gap-[6px] whitespace-nowrap text-xs text-dim"
        >
          <span className="h-2 w-2 rounded-[2px]" style={{ background: sevVar(s) }} />
          {counts[s]} {SEV_ABBREV[s]}
        </span>
      ))}
    </>
  );
}

export function ReviewRow({
  review,
  variant,
}: {
  review: ReviewSummary;
  variant: "feed" | "list";
}) {
  const list = variant === "list";
  return (
    <Link
      href={`/reviews/${review.id}`}
      className={cn(
        "card-contain flex cursor-pointer items-center gap-3 rounded-[8px] transition-colors hover:bg-[var(--bone-03)] md:gap-[18px]",
        list
          ? "border-t border-[var(--line-05)] px-3 py-[14px] first:border-t-0 md:px-[18px]"
          : "border-t border-[var(--line-06)] px-2 py-[14px]",
      )}
    >
      <div className="flex w-[104px] shrink-0 items-center gap-2 overflow-hidden md:w-[176px]">
        <span className="truncate font-mono text-xs text-dim">{review.repo}</span>
        <span className="shrink-0 font-mono text-xs text-faint">#{review.pr}</span>
      </div>
      <div className="min-w-0 flex-1 truncate text-sm text-text">{review.title}</div>
      <div className="shrink-0 md:w-[76px]">
        <ModelChip model={review.model} />
      </div>
      <div className="hidden w-[150px] shrink-0 gap-2 md:flex">
        <Tally counts={review.counts} />
      </div>
      {list ? (
        <>
          <div className="hidden w-[78px] shrink-0 sm:block">
            <ActionLabel action={review.action} />
          </div>
          <div className="w-[34px] shrink-0 text-right text-xs text-faint">{review.age}</div>
        </>
      ) : (
        <div className="w-10 shrink-0 text-right text-xs text-faint">{review.age}</div>
      )}
    </Link>
  );
}

export function LiveStrip({ live, variant }: { live: LiveReview | null; variant: "feed" | "list" }) {
  if (!live) return null; // nothing reviewing right now — no strip
  const list = variant === "list";
  return (
    <div
      className={
        list
          ? "flex items-center gap-3 rounded-[8px] border-b border-[var(--line-06)] px-[18px] py-[13px]"
          : "flex items-center gap-[18px] rounded-[8px] border-t border-[var(--line-06)] px-2 py-[14px]"
      }
      style={{ background: "var(--accent-04)" }}
    >
      <span className="pulse h-2 w-2 shrink-0 rounded-full bg-accent" />
      <span className="font-mono text-xs text-dim">{live.repo}</span>
      <span className="font-mono text-xs text-faint">#{live.pr}</span>
      <span className="min-w-0 flex-1 truncate text-[13px] text-text max-sm:hidden">{live.title}</span>
      <span className="ml-auto whitespace-nowrap text-xs text-accent sm:ml-0">
        {list ? `reviewing · ${live.stage}` : "reviewing now…"}
      </span>
      <span className="w-[44px] shrink-0 text-right text-xs tabular-nums text-faint">{live.elapsed}</span>
    </div>
  );
}
