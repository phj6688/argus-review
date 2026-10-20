"use client";

import { cn } from "@/lib/utils";

export interface SegOption<T extends string> {
  value: T;
  label: string;
}

export function Segmented<T extends string>({
  options,
  value,
  onChange,
  compact = false,
}: {
  options: SegOption<T>[];
  value: T;
  onChange: (v: T) => void;
  compact?: boolean;
}) {
  return (
    <div className="segment" role="tablist">
      {options.map((o) => {
        const active = o.value === value;
        return (
          <button
            key={o.value}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(o.value)}
            className={cn(
              "cursor-pointer rounded-[7px] py-1.5 text-xs font-medium transition-colors",
              compact ? "px-[11px]" : "px-[13px]",
              active ? "bg-[var(--accent-12)] text-accent" : "text-dim hover:text-text",
            )}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}
