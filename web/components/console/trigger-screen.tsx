"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { EyeMark } from "@/components/ui/icons";
import { createReview, getRunState, ReviewRefused } from "@/lib/api";
import { ScreenHeader } from "./screen-header";

const POLL_MS = 2000;

/** owner/repo#pr -> {repo, pr}. Returns null on anything malformed so the
 *  caller surfaces an inline error and never reaches the network. */
function parseTarget(raw: string): { repo: string; pr: number } | null {
  const value = raw.trim();
  const match = /^([^/\s#]+\/[^/\s#]+)#(\d+)$/.exec(value);
  if (!match) return null;
  const pr = Number(match[2]);
  if (!Number.isInteger(pr) || pr < 1) return null;
  return { repo: match[1], pr };
}

type Phase = "idle" | "running" | "error";

function runningLine(repo: string, pr: number, ticks: number): string {
  const dots = ".".repeat((ticks % 3) + 1);
  return `running review · ${repo}#${pr}${dots}`;
}

export function TriggerScreen() {
  const router = useRouter();
  const [value, setValue] = useState("");
  const [inputError, setInputError] = useState<string | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");

  // The readout text is written imperatively (textContent only, never
  // innerHTML) so the polling loop never re-renders a DOM subtree.
  const readoutRef = useRef<HTMLPreElement | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const navRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef = useRef(true);

  const write = useCallback((text: string) => {
    const node = readoutRef.current;
    if (node) node.textContent = text;
  }, []);

  const stopPolling = useCallback(() => {
    if (pollRef.current !== null) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    abortRef.current?.abort();
    abortRef.current = null;
    if (navRef.current !== null) {
      clearTimeout(navRef.current);
      navRef.current = null;
    }
  }, []);

  // Seed the readout once on mount. React owns no text node inside the <pre>,
  // so re-renders never clobber what the poll loop writes via textContent.
  useEffect(() => {
    write("idle · enter a pull request above to start");
  }, [write]);

  // Abort any in-flight poll when the screen unmounts or we navigate away, and
  // track mount state so a POST that resolves after unmount never arms a poll.
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      stopPolling();
    };
  }, [stopPolling]);

  const submit = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      if (phase === "running") return;

      const target = parseTarget(value);
      if (!target) {
        setInputError("Use the form owner/repo#pr, e.g. acme/api#5");
        return;
      }
      setInputError(null);
      setPhase("running");
      write(`queuing review · ${target.repo}#${target.pr}`);

      let id: string;
      try {
        const created = await createReview(target);
        id = created.id;
      } catch (error) {
        if (!mountedRef.current) return;
        setPhase("error");
        // The API explains a refusal (for example, no model provider set). Show it.
        write(
          error instanceof ReviewRefused && error.detail
            ? `review not started · ${error.detail}`
            : `request failed · could not reach the review API`,
        );
        return;
      }
      if (!mountedRef.current) return; // unmounted during POST: do not arm the poll

      let ticks = 0;
      write(runningLine(target.repo, target.pr, ticks));
      const controller = new AbortController();
      abortRef.current = controller;

      const tick = async () => {
        ticks += 1;
        let state;
        try {
          state = await getRunState(id, controller.signal);
        } catch (error) {
          if (controller.signal.aborted) return;
          setPhase("error");
          write(`polling failed · ${(error as Error).message}`);
          stopPolling();
          return;
        }

        if (state.status === "running") {
          write(runningLine(target.repo, target.pr, ticks));
          return;
        }
        if (state.status === "error") {
          setPhase("error");
          write(`review failed · ${state.error ?? "the run errored"}`);
          stopPolling();
          return;
        }
        // done: show the count, then hand off to the detail screen. The brief
        // pause keeps the final readout visible instead of vanishing on nav.
        stopPolling();
        const count = state.findings?.length ?? 0;
        write(`done · ${count} finding${count === 1 ? "" : "s"} · opening review…`);
        navRef.current = setTimeout(() => router.push(`/reviews/${id}`), 700);
      };

      pollRef.current = setInterval(tick, POLL_MS);
    },
    [phase, router, stopPolling, value, write],
  );

  const running = phase === "running";

  return (
    <div>
      <ScreenHeader
        title="Trigger a review"
        subtitle="Point Argus at a pull request and watch the run land, no CLI needed"
      />

      <form onSubmit={submit} className="panel max-w-[640px] px-[22px] py-6">
        <label htmlFor="pr-target" className="eyebrow mb-2 block">
          Pull request
        </label>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-stretch">
          <div
            className="flex min-h-[44px] flex-1 items-center gap-2 rounded-[9px] border bg-inset px-3"
            style={{
              borderColor: inputError ? "var(--crit-50)" : "var(--line)",
            }}
          >
            <span className="font-mono text-[13px] text-faint">$</span>
            <input
              id="pr-target"
              value={value}
              onChange={(event) => {
                setValue(event.target.value);
                if (inputError) setInputError(null);
              }}
              disabled={running}
              placeholder="owner/repo#pr"
              aria-label="PR to review"
              aria-invalid={inputError ? true : undefined}
              aria-describedby={inputError ? "input-error" : undefined}
              className="min-w-0 flex-1 bg-transparent font-mono text-[13px] text-text outline-none placeholder:text-faint disabled:opacity-60"
            />
          </div>
          <button
            type="submit"
            data-testid="run-button"
            disabled={running}
            className="flex min-h-[44px] shrink-0 items-center justify-center gap-2 rounded-[9px] px-5 text-[13px] font-semibold uppercase tracking-[0.08em] transition-colors disabled:cursor-not-allowed disabled:opacity-60"
            style={{
              background: "var(--accent-12)",
              border: "1px solid var(--accent-28)",
              color: "var(--color-accent)",
            }}
          >
            {running ? (
              <>
                <span className="pulse h-2 w-2 rounded-full bg-accent" />
                Running
              </>
            ) : (
              "Run review"
            )}
          </button>
        </div>

        {inputError && (
          <p
            id="input-error"
            data-testid="input-error"
            role="alert"
            className="mt-2 text-[12.5px]"
            style={{ color: "var(--color-critical)" }}
          >
            {inputError}
          </p>
        )}

        <p className="mt-3 text-[12.5px] text-faint">
          Argus queues the run, then this readout tracks it live and opens the
          report when it lands.
        </p>
      </form>

      <div className="panel mt-[14px] max-w-[640px] overflow-hidden">
        <div className="flex items-center gap-2 border-b border-[var(--line)] px-[18px] py-3">
          <EyeMark size={16} />
          <span className="eyebrow-wide">Run log</span>
          {running && <span className="pulse ml-auto h-2 w-2 rounded-full bg-accent" />}
        </div>
        {/* No JSX children: the text is owned entirely by the poll loop via
            textContent (UI rule 1), seeded on mount by the effect above. */}
        <pre
          ref={readoutRef}
          data-testid="run-readout"
          aria-live="polite"
          suppressHydrationWarning
          className="m-0 min-h-[64px] whitespace-pre-wrap break-words bg-inset px-[18px] py-4 font-mono text-[13px] leading-[1.7] text-dim"
        />
      </div>
    </div>
  );
}
