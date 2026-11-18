import type { GitLabStatus } from "./types";

type ReviewTarget = { repo: string; pr: number; title: string };
type GitLabConfig = Pick<GitLabStatus, "host" | "baseUrl">;
const validPath = (path: string) => /^[A-Za-z0-9._-]+(\/[A-Za-z0-9._-]+)+$/.test(path)
  && !path.split("/").some((part) => part === "." || part === "..");
const positiveId = (value: string) => /^[1-9][0-9]*$/.test(value) && Number.isSafeInteger(Number(value));

export const isGitLabKey = (repo: string) => repo.startsWith("gitlab:");

export function keyHost(repo: string): string | null {
  const split = repo.lastIndexOf("#");
  return isGitLabKey(repo) && split > 7 ? repo.slice(7, split) : null;
}

export function projectLabel(review: ReviewTarget): string {
  const suffix = ` #${review.pr}`;
  if (isGitLabKey(review.repo) && review.title.endsWith(suffix)) {
    const label = review.title.slice(0, -suffix.length);
    if (label !== review.repo) return label;
  }
  return review.repo;
}

export function projectPathOrNull(review: ReviewTarget): string | null {
  const label = projectLabel(review);
  return validPath(label) ? label : null;
}

function configuredBase(baseUrl: string): URL | null {
  try {
    if (/[?#]/.test(baseUrl) || /^https:\/\/[^/]*@/i.test(baseUrl)) return null;
    const base = new URL(baseUrl);
    return base.protocol === "https:" && !base.username && !base.password && !base.search && !base.hash ? base : null;
  } catch { return null; }
}

export function mrUrl(baseUrl: string, path: string, iid: number): string | null {
  const base = configuredBase(baseUrl);
  if (!base || !validPath(path) || !Number.isSafeInteger(iid) || iid < 1) return null;
  return `${base.href.replace(/\/+$/, "")}/${path.split("/").map(encodeURIComponent).join("/")}/-/merge_requests/${iid}`;
}

export function parseTarget(raw: string, gitlab: GitLabConfig | null): { repo: string; pr: number } | null {
  const value = raw.trim();
  if (!value.startsWith("gitlab:") && !value.startsWith("https://")) {
    const match = /^([^/\s#]+\/[^/\s#]+)#(\d+)$/.exec(value);
    if (!match) return null;
    const pr = Number(match[2]);
    return Number.isInteger(pr) && pr >= 1 ? { repo: match[1], pr } : null;
  }
  if (!gitlab) return null;
  if (isGitLabKey(value)) {
    const split = value.lastIndexOf("#");
    const iid = value.slice(split + 1);
    const key = value.slice(7, split);
    const match = /^([^/#]+)(?:#([1-9][0-9]*)|\/(.+))$/.exec(key);
    if (!positiveId(iid) || !match || match[1].toLowerCase() !== gitlab.host.toLowerCase()) return null;
    if (match[2] ? !positiveId(match[2]) : !validPath(match[3])) return null;
    return { repo: `gitlab:${gitlab.host}${match[2] ? `#${match[2]}` : `/${match[3]}`}`, pr: Number(iid) };
  }
  // Inspect the original path before URL parsing can normalize traversal away.
  if (/[\s%\\\x00-\x1f\x7f-\x9f]/.test(value)) return null;
  const base = configuredBase(gitlab.baseUrl);
  const rawUrl = /^https:\/\/([^/]+)(\/[^?#]*)?(?:[?#].*)?$/.exec(value);
  if (!base || !rawUrl || rawUrl[1].includes("@")) return null;
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.username || url.password || url.host.toLowerCase() !== gitlab.host.toLowerCase()) return null;
    const segments = (rawUrl[2] ?? "").replace(/\/$/, "").slice(1).split("/");
    const prefix = base.pathname.replace(/\/$/, "").split("/").slice(1);
    if (segments.some((s) => !s || s === "." || s === "..") || prefix.some((s, i) => s !== segments[i])) return null;
    const path = segments.slice(prefix.length, -3).join("/");
    const iid = segments.at(-1)!;
    if (segments.at(-3) !== "-" || segments.at(-2) !== "merge_requests" || !validPath(path)
      || !/^[0-9]+$/.test(iid) || !Number.isSafeInteger(Number(iid)) || Number(iid) < 1) return null;
    return { repo: `gitlab:${gitlab.host}/${path}`, pr: Number(iid) };
  } catch { return null; }
}
