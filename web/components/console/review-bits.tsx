import Link from "next/link";
import { isGitLabKey, projectLabel } from "@/lib/forge";
import type { LiveReview, ReviewSummary, RiskTier } from "@/lib/types";
import { cn, SEV_ABBREV, SEV_TALLY_TIP, SEVERITIES, modelChipStyle, sevVar } from "@/lib/utils";

const ACTION = {
  posted: { label: "posted", color: "var(--color-accent)", tip: "Argus posted this review to the pull request on GitHub." },
  dry_run: { label: "dry run", color: "var(--color-dim)", tip: "Posting was rehearsed, nothing was sent to GitHub." },
  not_posted: { label: "review only", color: "var(--color-faint)", tip: "Argus reviewed this PR but did not post to GitHub: publishing is off for this repo, or there was nothing new." },
  post_failed: { label: "post failed", color: "var(--color-critical)", tip: "The review finished but posting it to GitHub failed." },
} as const;

function actionTip(tip: string, gitlab: boolean): string {
  return gitlab ? tip.replace("pull request on GitHub", "merge request on GitLab")
    .replace("this PR", "this merge request").replaceAll("GitHub", "GitLab") : tip;
}

export function ForgeChip() {
  return (
    <span data-tip="GitLab merge request"
      className="shrink-0 rounded-[6px] border border-[var(--line-14)] bg-[var(--bone-04)] px-[6px] py-[3px] text-[10px] text-dim">
      MR
    </span>
  );
}

export function ModelChip({
  model,
  tip = "The deep-review model that produced this review.",
}: {
  model: string;
  tip?: string;
}) {
  return (
    <span
      data-tip={tip}
      className="inline-block whitespace-nowrap rounded-[6px] px-[9px] py-[3px] text-[11px]"
      style={modelChipStyle(model)}
    >
      {model}
    </span>
  );
}

export function RiskChip({ risk }: { risk?: RiskTier | null }) {
  if (!risk) return null;
  const risky = risk.tier === "risky";
  return (
    <span
      data-testid="risk-tier"
      data-tier={risk.tier}
      data-tip={risk.reasons.length ? `${risk.label} Reasons: ${risk.reasons.join("; ")}` : risk.label}
      className="inline-block whitespace-nowrap rounded-[6px] border px-[9px] py-[3px] text-[11px]"
      style={{
        color: risky ? "var(--color-accent)" : "var(--color-dim)",
        borderColor: risky ? "var(--accent-35)" : "var(--line-14)",
        background: risky ? "var(--accent-08)" : "var(--bone-04)",
      }}
    >
      {risk.tier}
    </span>
  );
}

/** The model that answered, not the one configured. A run that errored shows "not run";
 *  a run that fell back shows the fallback model and says so in the tooltip. */
function RowModel({ review }: { review: ReviewSummary }) {
  if (review.model_used) {
    return (
      <ModelChip
        model={review.model_used}
        tip={
          review.fallback_from
            ? `Fell back from ${review.fallback_from}. This model produced the review.`
            : undefined
        }
      />
    );
  }
  if (review.status === "error") {
    return (
      <ModelChip model="not run" tip={`The review failed. Configured model: ${review.model}.`} />
    );
  }
  return <ModelChip model={review.model} />;
}

export function ActionLabel({ review }: { review: ReviewSummary }) {
  if (!review.action) {
    return (
      <span data-tip="The review failed, so there was nothing to post." className="text-xs text-faint">
        —
      </span>
    );
  }
  const a = ACTION[review.action];
  const count = review.action === "posted" && review.posted_comments ? ` · ${review.posted_comments}` : "";
  return (
    <span data-tip={actionTip(a.tip, isGitLabKey(review.repo))} className="text-xs" style={{ color: a.color }}>
      {a.label}
      {count}
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
        <span className="truncate font-mono text-xs text-dim">{projectLabel(review)}</span>
        <span className="shrink-0 font-mono text-xs text-faint">#{review.pr}</span>
        {isGitLabKey(review.repo) && <ForgeChip />}
      </div>
      <div className="min-w-0 flex-1 truncate text-sm text-text">{review.title}</div>
      <div className={cn("shrink-0", list ? "md:w-[124px]" : "md:w-[76px]")}>
        <RowModel review={review} />
      </div>
      <div className={cn("hidden shrink-0 gap-2 md:flex", list ? "w-[184px]" : "w-[150px]")}>
        {review.status === "error" ? (
          <span
            data-testid="row-failed"
            data-tip="The review failed, so nothing was checked. This is not a clean result."
            className="text-xs text-critical"
          >
            failed
          </span>
        ) : (
          <Tally counts={review.counts} />
        )}
      </div>
      {list ? (
        <>
          <div className="hidden w-[56px] shrink-0 md:block">
            <RiskChip risk={review.risk} />
          </div>
          <div className="hidden w-[92px] shrink-0 whitespace-nowrap sm:block">
            <ActionLabel review={review} />
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
      <span className="font-mono text-xs text-dim">{projectLabel(live)}</span>
      <span className="font-mono text-xs text-faint">#{live.pr}</span>
      {isGitLabKey(live.repo) && <ForgeChip />}
      <span className="min-w-0 flex-1 truncate text-[13px] text-text max-sm:hidden">{live.title}</span>
      <span className="ml-auto whitespace-nowrap text-xs text-accent sm:ml-0">
        {list ? `reviewing · ${live.stage}` : "reviewing now…"}
      </span>
      <span className="w-[44px] shrink-0 text-right text-xs tabular-nums text-faint">{live.elapsed}</span>
    </div>
  );
}
