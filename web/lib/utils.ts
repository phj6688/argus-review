import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";
import type { Severity } from "./types";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** CSS var for a severity color — used in inline styles so the hex stays
 *  in the token layer, never in a component. */
export function sevVar(sev: Severity): string {
  return `var(--sev-${sev})`;
}

export const SEV_LABEL: Record<Severity, string> = {
  critical: "Critical",
  major: "Major",
  minor: "Minor",
  nit: "Nit",
};

export const SEV_ABBREV: Record<Severity, string> = {
  critical: "crit",
  major: "major",
  minor: "minor",
  nit: "nit",
};

export const SEV_TIP: Record<Severity, string> = {
  critical:
    "Critical — a correctness, security, or data-loss bug. Hottest severity; blocks the PR.",
  major: "Major — a real bug or risk that should be fixed before merge. Bug-class.",
  minor: "Minor — a smaller issue or improvement. Nit-class; never blocks.",
  nit: "Nit — style or preference, lowest signal. Folded into one comment.",
};

export const SEV_TALLY_TIP: Record<Severity, string> = {
  critical:
    "Critical findings — correctness, security, or data-loss bugs. Hottest severity; these block the PR.",
  major: "Major findings — real bugs or risks that should be fixed before merge. Bug-class.",
  minor: "Minor findings — smaller issues or improvements. Nit-class; never blocks.",
  nit: "Nits — style or preference, lowest signal. Folded into one comment, never blocking.",
};

export const SEVERITIES: Severity[] = ["critical", "major", "minor", "nit"];

export function isBugClass(sev: Severity): boolean {
  return sev === "critical" || sev === "major";
}

/** Inline-style object for a model chip: Opus = amber tint, others neutral. */
export function modelChipStyle(model: string): React.CSSProperties {
  return model.toLowerCase().includes("opus")
    ? {
        background: "var(--accent-08)",
        border: "1px solid var(--accent-18)",
        color: "var(--color-accent-dim)",
      }
    : {
        background: "var(--bone-05)",
        border: "1px solid var(--line-12)",
        color: "var(--color-dim)",
      };
}

export function pctStr(n: number): string {
  return `${n}%`;
}
