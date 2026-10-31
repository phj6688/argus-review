"use client";

import { useState } from "react";
import { putPublishRepos } from "@/lib/api";
import type { PublishAccess, PublishConfig, PublishRepo, RepoWatch } from "@/lib/types";

const COPY = {
  title: "GitHub posting",
  intro: "Drag a repo into Review and post to GitHub, or use the arrow button. Changes save at once.",
  saving: "Saving…",
  mode: {
    on: "Posting: on",
    "dry-run": "Posting: dry run",
    off: "Posting: off",
  },
  source: {
    dashboard: "Source: dashboard",
    env: "Source: ARGUS_PUBLISH_REPOS (the first change here saves the list to the dashboard and the env value is then ignored)",
  },
  modeNote: {
    on: null,
    "dry-run": "Dry run: reviews are rendered and stored but not posted.",
    off: "Posting is off. Repos you add here will not get GitHub posts until the operator sets ARGUS_PUBLISH to 1.",
  },
  noToken: "ARGUS_PUBLISH_TOKEN is not set, so nothing can be posted.",
  reviewTitle: "Review only",
  postTitle: "Review and post to GitHub",
  reviewEmpty: "No repos",
  postEmpty: "Nothing is posted yet. Drag a repo here.",
  postLabel: (name: string) => `Post ${name} to GitHub`,
  stopLabel: (name: string) => `Stop posting ${name}`,
  postArrow: "→",
  stopArrow: "←",
  access: {
    ok: "bot can push",
    missing: "no bot access",
    unknown: "access unchecked",
    unconfigured: "no publish token",
  },
  checkingAccess: "checking access",
  openSettings: "Open access settings",
  botAdded: "bot added automatically",
  saveError: "Could not save posting repos. Try again.",
};

type ListTarget = "review" | "post";

export function PublishPanel({ repos, publish }: { repos: RepoWatch[]; publish: PublishConfig }) {
  const [published, setPublished] = useState<PublishRepo[]>(publish.repos);
  const [source, setSource] = useState(publish.source);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState<ListTarget | null>(null);

  const catalogue = new Map(repos.map((repo) => [repo.name.toLowerCase(), repo]));
  const publishedNames = new Set(published.map((repo) => repo.name.toLowerCase()));
  const reviewOnly = repos
    .filter((repo) => (repo.effective_watched ?? repo.watched) && !publishedNames.has(repo.name.toLowerCase()))
    .sort((a, b) => a.name.localeCompare(b.name));
  const postingRepos = published.map((repo) => ({
    ...repo,
    name: catalogue.get(repo.name.toLowerCase())?.name ?? repo.name,
  }));
  const modeNote = COPY.modeNote[publish.mode];

  async function moveRepo(name: string, posting: boolean) {
    if (busy !== null) return;
    const key = name.toLowerCase();
    const existing = published.find((repo) => repo.name.toLowerCase() === key);
    const candidate = catalogue.get(key);
    if (posting && (existing || !(candidate?.effective_watched ?? candidate?.watched))) return;
    if (!posting && !existing) return;

    const displayName = candidate?.name ?? existing?.name ?? name;
    const previous = published;
    const next: PublishRepo[] = posting
      ? [...published, {
          name: displayName,
          access: {
            state: "unknown",
            detail: COPY.checkingAccess,
            settingsUrl: `https://github.com/${displayName}/settings/access`,
            grant: null,
          },
        }]
      : published.filter((repo) => repo.name.toLowerCase() !== key);

    setPublished(next);
    setBusy(displayName);
    setDragOver(null);
    try {
      const saved = await putPublishRepos(next.map((repo) => repo.name));
      setPublished(saved.repos);
      setSource(saved.source);
      setError(null);
    } catch (failure) {
      setPublished(previous);
      setError(failure instanceof Error ? failure.message : COPY.saveError);
    } finally {
      setBusy(null);
    }
  }

  function renderRow(name: string, posting: boolean, access?: PublishAccess) {
    const rowBusy = busy === name;
    const checking = posting && rowBusy;
    const warning = access && !checking && (
      access.state === "missing" || access.state === "unknown" || access.grant === "failed"
    );
    const accessColor = checking || !access
      ? "var(--color-faint)"
      : access.state === "ok"
        ? "var(--color-sage)"
        : access.state === "missing"
          ? "var(--color-warn)"
          : "var(--color-faint)";

    return (
      <div
        key={name}
        data-testid={`publish-row-${name}`}
        draggable={busy === null}
        aria-busy={rowBusy}
        onDragStart={(event) => {
          if (busy !== null) {
            event.preventDefault();
            return;
          }
          event.dataTransfer.setData("text/plain", name);
          event.dataTransfer.effectAllowed = "move";
        }}
        onDragEnd={() => setDragOver(null)}
        className={`min-h-[44px] min-w-0 border-t border-[var(--line-05)] ${rowBusy ? "opacity-50" : "cursor-grab"}`}
      >
        <div className="flex min-h-[44px] min-w-0 items-center gap-2">
          <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-3 gap-y-1 py-2">
            <span className="min-w-0 break-all font-mono text-[13px] text-prose">{name}</span>
            {posting && access && (
              <span
                data-testid={`publish-access-${name}`}
                title={access.detail}
                data-tip={access.detail}
                className="max-w-full rounded-[8px] bg-[var(--bone-05)] px-2 py-1 text-[11px]"
                style={{ color: accessColor }}
              >
                {checking ? COPY.checkingAccess : COPY.access[access.state]}
              </span>
            )}
          </div>
          <button
            type="button"
            aria-label={posting ? COPY.stopLabel(name) : COPY.postLabel(name)}
            disabled={busy !== null}
            onClick={() => void moveRepo(name, !posting)}
            className="flex min-h-[44px] min-w-[44px] shrink-0 items-center justify-center rounded-[8px] text-accent transition-colors hover:bg-[var(--accent-10)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-accent)] disabled:cursor-not-allowed disabled:opacity-50"
          >
            {posting ? COPY.stopArrow : COPY.postArrow}
          </button>
        </div>
        {warning && access && (
          <div
            data-testid={`publish-warning-${name}`}
            className="mb-3 min-w-0 break-words text-[12.5px] text-warn"
          >
            {access.detail}{" "}
            <a
              target="_blank"
              rel="noopener noreferrer"
              href={access.settingsUrl}
              className="rounded-[8px] underline underline-offset-2 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-accent)]"
            >
              {COPY.openSettings}
            </a>
          </div>
        )}
        {access?.grant === "granted" && (
          <p className="mb-3 text-[12.5px] text-sage">{COPY.botAdded}</p>
        )}
      </div>
    );
  }

  const lists = [
    { target: "review" as const, title: COPY.reviewTitle, empty: COPY.reviewEmpty, entries: reviewOnly },
    { target: "post" as const, title: COPY.postTitle, empty: COPY.postEmpty, entries: postingRepos },
  ];

  return (
    <section className="panel min-w-0 px-[22px] py-5 text-[13px]">
      <div className="mb-1 flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold text-heading">{COPY.title}</h2>
        {busy !== null && <span role="status" className="text-[12.5px] text-faint">{COPY.saving}</span>}
      </div>
      <p className="mb-4 text-[12.5px] text-faint">{COPY.intro}</p>

      <div className="flex min-w-0 flex-wrap items-center gap-3">
        <span
          data-testid="publish-mode"
          className="shrink-0 rounded-[8px] border border-[var(--accent-28)] bg-[var(--accent-10)] px-2 py-1 text-[12.5px] text-accent"
        >
          {COPY.mode[publish.mode]}
        </span>
        <span data-testid="publish-source" className="min-w-0 break-words text-[12.5px] text-faint">
          {COPY.source[source]}
        </span>
      </div>
      {modeNote && (
        <p data-testid="publish-mode-note" className="mt-2 text-[12.5px] text-faint">{modeNote}</p>
      )}
      {!publish.tokenConfigured && publish.mode !== "off" && (
        <p className="mt-2 text-[12.5px] text-warn">{COPY.noToken}</p>
      )}
      {error !== null && (
        <div
          role="alert"
          data-testid="publish-error"
          className="mt-3 min-w-0 break-words rounded-[8px] border border-[var(--line-12)] px-3 py-2 text-[12.5px] text-critical"
        >
          {error}
        </div>
      )}

      <div className="mt-4 grid grid-cols-1 gap-[14px] lg:grid-cols-2">
        {lists.map(({ target, title, empty, entries }) => (
          <section
            key={target}
            data-testid={target === "post" ? "publish-post-list" : "publish-review-list"}
            aria-label={title}
            onDragOver={(event) => {
              event.preventDefault();
              event.dataTransfer.dropEffect = busy === null ? "move" : "none";
              if (busy === null) setDragOver(target);
            }}
            onDragLeave={(event) => {
              if (!(event.relatedTarget instanceof Node) || !event.currentTarget.contains(event.relatedTarget)) {
                setDragOver(null);
              }
            }}
            onDrop={(event) => {
              event.preventDefault();
              setDragOver(null);
              void moveRepo(event.dataTransfer.getData("text/plain"), target === "post");
            }}
            className="min-h-[160px] min-w-0 rounded-[8px] border px-3 py-3 transition-colors"
            style={{
              borderColor: dragOver === target ? "var(--accent-28)" : "var(--line-12)",
              background: dragOver === target ? "var(--accent-10)" : "var(--bone-05)",
            }}
          >
            <div className="mb-2 flex min-w-0 items-center justify-between gap-2">
              <h3 className="min-w-0 text-[13px] font-semibold text-heading">{title}</h3>
              <span className="text-[12.5px] tabular-nums text-faint">{entries.length}</span>
            </div>
            {entries.length === 0 ? (
              <p className="border-t border-[var(--line-05)] py-3 text-[12.5px] text-faint">{empty}</p>
            ) : entries.map((repo) => renderRow(repo.name, target === "post", "access" in repo ? repo.access : undefined))}
          </section>
        ))}
      </div>
    </section>
  );
}
