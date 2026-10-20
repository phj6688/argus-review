"use client";

import { useState } from "react";
import { Reveal, RevealItem } from "@/components/ui/reveal";
import { Slider } from "@/components/ui/slider";
import { Switch } from "@/components/ui/switch";
import { putConfig } from "@/lib/api";
import type { ConfigData } from "@/lib/types";
import { ScreenHeader } from "./screen-header";

type SaveState = "idle" | "saving" | "saved" | "error";

const SAVE_LABEL: Record<SaveState, string> = {
  idle: "Save changes",
  saving: "Saving…",
  saved: "Saved",
  error: "Retry save",
};

const MODEL_TIP: Record<string, string> = {
  "Deep review":
    "The reviewer pass reports every candidate issue with severity and confidence, no self-filtering.",
  Verification:
    "Independent skeptical pass that tries to refute each finding; ambiguous ones are discarded.",
};

export function ConfigScreen({ config }: { config: ConfigData }) {
  const [watch, setWatch] = useState<Record<string, boolean>>(
    Object.fromEntries(config.repos.map((r) => [r.name, r.watched])),
  );
  const [bugCap, setBugCap] = useState(config.bugCap);
  const [minConf, setMinConf] = useState(config.minConfidence);
  const savedModel = config.models.find((m) => m.tier === "deep")?.model ?? "";
  const [model, setModel] = useState(savedModel);
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const watchedCount = Object.values(watch).filter(Boolean).length;
  // The stored model is always offered even when the env list shrinks, so the
  // picker can never show a value it cannot submit.
  const modelOptions = config.modelOptions?.includes(savedModel)
    ? config.modelOptions
    : [savedModel, ...(config.modelOptions ?? [])];

  const dirty =
    bugCap !== config.bugCap ||
    minConf !== config.minConfidence ||
    model !== savedModel ||
    config.repos.some((r) => watch[r.name] !== r.watched);

  // Any edit invalidates a prior "saved"/"error" readout.
  function edited<T>(setter: (v: T) => void) {
    return (v: T) => {
      setSaveState("idle");
      setter(v);
    };
  }
  const onBugCap = edited(setBugCap);
  const onMinConf = edited(setMinConf);
  const onModel = edited(setModel);
  const onWatch = (name: string, v: boolean) => {
    setSaveState("idle");
    setWatch((p) => ({ ...p, [name]: v }));
  };

  async function handleSave() {
    setSaveState("saving");
    try {
      await putConfig({
        ...config,
        repos: config.repos.map((r) => ({ name: r.name, watched: watch[r.name] })),
        models: config.models.map((m) => ({ ...m, model })),
        bugCap,
        minConfidence: minConf,
      });
      setSaveState("saved");
    } catch {
      setSaveState("error");
    }
  }

  return (
    <div>
      <ScreenHeader
        title="Config"
        subtitle="What Argus watches, which models it runs, and how loud it gets"
      />

      <Reveal className="grid grid-cols-1 items-start gap-[14px] lg:grid-cols-[1.1fr_1fr]">
        <RevealItem className="panel px-[22px] py-5">
          <div className="mb-1 flex items-center justify-between">
            <span className="text-sm font-semibold text-heading">Repo allowlist</span>
            <span className="text-xs text-faint">
              {watchedCount} of {config.totalRepos} watched
            </span>
          </div>
          <p className="mb-4 text-[12.5px] text-faint">
            Only allowlisted repos trigger the webhook pipeline.
          </p>
          <div className="flex flex-col">
            {config.repos.map((r) => (
              <div
                key={r.name}
                className="flex min-h-[44px] items-center justify-between gap-3 border-t border-[var(--line-05)]"
              >
                <span className="font-mono text-[13px]" style={{ color: "var(--color-prose)" }}>
                  {r.name}
                </span>
                <Switch
                  checked={watch[r.name]}
                  onCheckedChange={(v) => onWatch(r.name, v)}
                  label={`Watch ${r.name}`}
                />
              </div>
            ))}
            <div className="flex min-h-[44px] items-center border-t border-[var(--line-05)] text-[12.5px] text-faint">
              + {config.hiddenCount} more repos
            </div>
          </div>
        </RevealItem>

        <RevealItem className="flex flex-col gap-[14px]">
          <div className="panel px-[22px] py-5">
            <div className="mb-4 text-sm font-semibold text-heading">Model pipeline</div>
            <div className="flex flex-col gap-[14px]">
              {config.models.map((m, index) => (
                <div key={m.name} className="flex items-center justify-between gap-3">
                  <div>
                    <div className="text-[13px] text-text" data-tip={MODEL_TIP[m.name]}>
                      {m.name}
                    </div>
                    <div className="text-xs text-faint">{m.caption}</div>
                  </div>
                  {index === 0 ? (
                    <span className="relative inline-flex items-center">
                      <select
                        aria-label="Reviewer model"
                        value={model}
                        onChange={(e) => onModel(e.target.value)}
                        className="appearance-none rounded-[8px] py-[7px] pl-3 pr-7 text-[12.5px]"
                        style={{
                          background: "var(--accent-10)",
                          border: "1px solid var(--accent-28)",
                          color: "var(--color-accent)",
                        }}
                      >
                        {modelOptions.map((option) => (
                          <option key={option} value={option}>
                            {option}
                          </option>
                        ))}
                      </select>
                      <span
                        aria-hidden
                        className="pointer-events-none absolute right-[10px] text-[11px]"
                        style={{ color: "var(--color-accent)" }}
                      >
                        ▾
                      </span>
                    </span>
                  ) : (
                    <span
                      className="rounded-[8px] px-3 py-[7px] text-[12.5px]"
                      style={
                        m.tier === "deep"
                          ? {
                              background: "var(--accent-10)",
                              border: "1px solid var(--accent-28)",
                              color: "var(--color-accent)",
                            }
                          : {
                              background: "var(--bone-05)",
                              border: "1px solid var(--line-12)",
                              color: "var(--color-dim)",
                            }
                      }
                    >
                      {m.tier === "deep" ? model : m.model}
                    </span>
                  )}
                </div>
              ))}
            </div>
          </div>

          <div className="panel px-[22px] py-5">
            <div className="mb-[18px] text-sm font-semibold text-heading">Thresholds</div>

            <div className="mb-[22px]">
              <div className="mb-3 flex items-baseline justify-between">
                <span
                  className="text-[13px] text-text"
                  data-tip="Hard cap on bug-class comments per PR. Beyond it, extra nits fold into one comment."
                >
                  Bug-class cap / PR
                </span>
                <span className="text-[18px] font-semibold tabular-nums text-accent">{bugCap}</span>
              </div>
              <Slider value={bugCap} min={1} max={20} step={1} onValueChange={onBugCap} label="Bug-class cap per PR" />
              <div className="mt-[7px] flex justify-between text-[11px] text-faint">
                <span>1</span>
                <span>hard cap before nits fold</span>
                <span>20</span>
              </div>
            </div>

            <div>
              <div className="mb-3 flex items-baseline justify-between">
                <span
                  className="text-[13px] text-text"
                  data-tip="Findings below this confidence are suppressed and never posted."
                >
                  Min confidence to show
                </span>
                <span className="text-[18px] font-semibold tabular-nums text-accent">
                  {minConf.toFixed(2)}
                </span>
              </div>
              <Slider value={minConf} min={0} max={1} step={0.05} onValueChange={onMinConf} label="Minimum confidence to show" />
              <div className="mt-[7px] flex justify-between text-[11px] text-faint">
                <span>0.00</span>
                <span>below this, suppress</span>
                <span>1.00</span>
              </div>
            </div>
          </div>
        </RevealItem>
      </Reveal>

      <div className="mt-[18px] flex items-center justify-end gap-3">
        {saveState === "saved" && (
          <span className="text-[12.5px] text-accent-dim" role="status">
            Config saved
          </span>
        )}
        {saveState === "error" && (
          <span className="text-[12.5px]" style={{ color: "var(--color-critical)" }} role="alert">
            Save failed, try again
          </span>
        )}
        <button
          type="button"
          onClick={handleSave}
          disabled={saveState === "saving" || !dirty}
          aria-busy={saveState === "saving"}
          className="min-h-[40px] rounded-[8px] px-4 py-[9px] text-[13px] font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-50"
          style={{
            background: "var(--accent-10)",
            border: "1px solid var(--accent-28)",
            color: "var(--color-accent)",
          }}
        >
          {SAVE_LABEL[saveState]}
        </button>
      </div>
    </div>
  );
}
