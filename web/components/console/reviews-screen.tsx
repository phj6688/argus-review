"use client";

import { useMemo, useState } from "react";
import { Reveal, RevealItem } from "@/components/ui/reveal";
import { Segmented } from "@/components/ui/segmented";
import { EyeMark, SearchIcon } from "@/components/ui/icons";
import type { LiveReview, ReviewSummary, Severity } from "@/lib/types";
import { LiveStrip, ReviewRow } from "./review-bits";
import { ScreenHeader } from "./screen-header";

type SevFilter = "all" | Severity;

export function ReviewsScreen({
  reviews,
  live,
}: {
  reviews: ReviewSummary[];
  live: LiveReview | null;
}) {
  const [search, setSearch] = useState("");
  const [sev, setSev] = useState<SevFilter>("all");

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return reviews.filter((r) => {
      const sevOk = sev === "all" || r.counts[sev] > 0;
      const qOk = !q || `${r.repo} #${r.pr} ${r.title}`.toLowerCase().includes(q);
      return sevOk && qOk;
    });
  }, [reviews, search, sev]);

  return (
    <div>
      <ScreenHeader
        title="Reviews"
        subtitle={
          <>
            All reviewed pull requests across <span className="text-accent">20 repos</span>
          </>
        }
        controls={
          <>
            <div className="flex min-w-[232px] items-center gap-2 rounded-[9px] border border-[var(--line)] bg-surface px-3 py-2 max-sm:min-w-0">
              <SearchIcon className="shrink-0 text-faint" />
              <input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search repo, PR, title…"
                aria-label="Search reviews"
                className="min-w-0 flex-1 bg-transparent text-[13px] text-text outline-none placeholder:text-faint"
              />
            </div>
            <div className="max-sm:hidden">
              <Segmented<SevFilter>
                compact
                options={[
                  { value: "all", label: "All" },
                  { value: "critical", label: "Critical" },
                  { value: "major", label: "Major" },
                  { value: "minor", label: "Minor" },
                  { value: "nit", label: "Nit" },
                ]}
                value={sev}
                onChange={setSev}
              />
            </div>
          </>
        }
      />

      <Reveal>
        <RevealItem className="panel overflow-hidden">
          <div className="flex items-center gap-3 border-b border-[var(--line)] px-3 py-3 text-[11px] font-semibold uppercase tracking-[0.08em] text-faint md:gap-[18px] md:px-[18px]">
            <div className="w-[104px] shrink-0 md:w-[176px]">Repo · PR</div>
            <div className="flex-1">Title</div>
            <div className="hidden shrink-0 md:block md:w-[76px]">Model</div>
            <div className="hidden w-[150px] shrink-0 md:block">Findings</div>
            <div className="hidden w-[78px] shrink-0 sm:block">Action</div>
            <div className="w-[34px] shrink-0 text-right">Age</div>
          </div>

          <LiveStrip live={live} variant="list" />

          {filtered.map((r) => (
            <ReviewRow key={r.id} review={r} variant="list" />
          ))}

          {filtered.length === 0 && (
            <div className="px-5 py-[54px] text-center">
              <EyeMark
                size={34}
                slash
                color="var(--color-faint)"
                className="mx-auto mb-[14px] opacity-50"
              />
              <div className="mb-[5px] text-[15px] text-text">No reviews match</div>
              <div className="text-[13px] text-faint">
                Try a different search or severity filter.
              </div>
            </div>
          )}
        </RevealItem>
      </Reveal>
    </div>
  );
}
