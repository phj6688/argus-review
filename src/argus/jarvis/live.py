"""Wire the probe lane into live review.

For a PR, fetch the full content (at head) of each changed file whose name could
match a probe, run the deterministic probes, and turn each hit into a verified
finding. Probe hits skip the LLM verifier on purpose: they are validated
deterministic checks, so their verdict is fixed (real, confidence 1.0). Intent
injection and corpus suppression are separate, later wirings.
"""

import base64
import fnmatch
import json
import logging
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import NamedTuple

from .. import github
from ..context import FileReader
from ..diff import FilePatch, fetch_pr_files
from ..schema import Finding, Severity, Verdict, VerifiedFinding
from . import intent, linear, probes

log = logging.getLogger("argus.jarvis.live")

ENV_KNOWN_FPS_PATH = "ARGUS_KNOWN_FPS_PATH"
_KNOWN_FP_KEYS: set | None = None
_LIVE_FP_CACHE: dict = {}
# Live reviews read only the first page of a PR's commits.
PR_COMMITS_PAGE = 100


def _fp_path() -> Path | None:
    """Return the optional file path set by ARGUS_KNOWN_FPS_PATH."""
    value = os.environ.get(ENV_KNOWN_FPS_PATH)
    return Path(value) if value else None


def suppress_known_fps(repo: str, findings: list[Finding]) -> tuple[list[Finding], list[Finding]]:
    """Split raw findings into kept and suppressed, dropping operator-confirmed FPs.

    Matches on (repo, file, normalised title). Run before verification so a known
    false positive does not even cost a verify call. Conservative by design: only
    an exact title match in the same file is dropped, so a real finding is never
    silently lost to a fuzzy collision.
    """
    keys = _known_fp_keys() | _live_fp_keys()
    kept, suppressed = [], []
    for finding in findings:
        bucket = suppressed if (repo, finding.file, _norm(finding.title)) in keys else kept
        bucket.append(finding)
    return kept, suppressed


def _known_fp_keys() -> set:
    """Cache file-backed suppression keys; an unset or missing path disables them."""
    global _KNOWN_FP_KEYS
    if _KNOWN_FP_KEYS is None:
        _KNOWN_FP_KEYS = set()
        path = _fp_path()
        if path is None:
            log.debug("Known-FP suppression from a file is off: %s is unset or empty", ENV_KNOWN_FPS_PATH)
        elif not path.exists():
            log.warning("Known-FP file does not exist: %s", path)
            log.debug("Known-FP suppression from a file is off: file does not exist")
        else:
            for line in path.read_text().splitlines():
                if line.strip():
                    record = json.loads(line)
                    _KNOWN_FP_KEYS.add((record["repo"], record["file"], _norm(record["title"])))
    return _KNOWN_FP_KEYS


def invalidate_live_fps():
    _LIVE_FP_CACHE.clear()


def _live_fp_keys():
    from ..api.db import db_path

    path = Path(db_path()).resolve()
    cached = _LIVE_FP_CACHE.get(path)
    if cached and time.monotonic() - cached[0] < 60:
        return cached[1]
    keys = set()
    if path.exists():
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='known_fps_live'").fetchone():
                keys = {(repo, file, _norm(title)) for repo, file, title in
                        conn.execute("SELECT repo, file, title FROM known_fps_live")}
    _LIVE_FP_CACHE[path] = (time.monotonic(), keys)
    return keys


def _norm(title: str) -> str:
    return " ".join(title.lower().split())


def intent_section(repo: str, pr: int, meta: dict | None = None) -> str | None:
    """The intent prompt slot for a PR, or None when there is no linked issue.

    Recovers the configured issue key from the PR's title/body/commits, fetches the issue from
    Linear, and renders the slot. A repo that files no issues yields None (nothing
    to inject); a recovered id whose issue cannot be fetched yields a slot that
    names the id and says the body was unavailable.
    """
    if meta is None:
        meta = _pr_meta(repo, pr)
    result = intent.recover(intent.PullRequest(
        repo=repo, title=meta["title"], body=meta["body"], commit_messages=meta["commits"],
        author_association=meta.get("author_association")))
    if result.status != intent.STATUS_RECOVERED:
        return None
    return intent.intent_slot(result, linear.fetch_issue(result.issue_ids[0]))


def _pr_meta(repo: str, pr: int) -> dict:
    pull = github.get(f"/repos/{repo}/pulls/{pr}")
    pull.raise_for_status()
    body = pull.json()
    commits = github.get(f"/repos/{repo}/pulls/{pr}/commits", {"per_page": PR_COMMITS_PAGE})
    records = commits.json() if commits.status_code == 200 else []
    messages = [c["commit"]["message"] for c in records]
    head_sha = (body.get("head") or {}).get("sha")
    head_date = next(((c.get("commit", {}).get("committer") or {}).get("date")
                      for c in records if c.get("sha") == head_sha), None)
    return {"title": body.get("title") or "", "body": body.get("body") or "", "commits": messages,
            "head_sha": head_sha, "head_committed_at": head_date,
            "author_association": body.get("author_association"),
            "branch": (body.get("head") or {}).get("ref"),
            "author": (body.get("user") or {}).get("login")}


def probe_scan(repo: str, pr: int, files: list[FilePatch] | None = None,
               head_sha: str | None = None, read: FileReader | None = None) -> "ProbeScan":
    """Full-fidelity probe lane for a PR, with the record of what it executed."""
    if files is not None:
        return probe_scan_for_files(repo, head_sha, files, read)
    candidates = _candidate_paths(_changed_paths(repo, pr))
    if not candidates:
        return ProbeScan([], _probe_record(0, NO_CANDIDATES))
    head = head_sha or (_pr_head_sha(repo, pr) if read is None else None)
    contents = {}
    for path in candidates:
        content = read(path) if read is not None else _fetch_file_at(repo, path, head)
        if content is not None:
            contents[path] = content
    if not contents:
        return ProbeScan([], _probe_record(0, NO_CONTENT))
    hits = probes.scan_contents(probes.INCIDENT_PROBES, contents)
    return ProbeScan([_hit_to_finding(hit) for hit in hits],
                     _probe_record(len(probes.INCIDENT_PROBES)))


def probe_findings(repo: str, pr: int) -> list[VerifiedFinding]:
    """Findings only, for callers that do not track coverage."""
    return probe_scan(repo, pr).findings


def _candidate_paths(paths: list[str]) -> list[str]:
    """Changed paths whose basename could satisfy some probe glob's last segment.

    A cheap necessary filter: if the basename cannot match any probe's filename
    pattern, the full glob cannot either, so the file is never fetched. The real
    (whole-glob) match still happens later in scan_contents.
    """
    suffixes = {probe.path_glob.rsplit("/", 1)[-1] for probe in probes.INCIDENT_PROBES}
    return [path for path in paths
            if any(fnmatch.fnmatch(path.rsplit("/", 1)[-1], suffix) for suffix in suffixes)]


def _hit_to_finding(hit: probes.Hit) -> VerifiedFinding:
    probe = _probe_by_id(hit.probe_id)
    finding = Finding(
        file=hit.file, line=hit.line, severity=Severity(probe.severity), confidence=1.0,
        title=f"probe: {probe.probe_id}", description=probe.note,
        proposed_fix="Address per the linked incident pattern; see the probe note.",
        rule_id=probe.probe_id,
    )
    verdict = Verdict(
        is_real=True, confidence=1.0,
        reasoning=f"deterministic probe {probe.probe_id} matched {hit.text.strip()!r}",
    )
    return VerifiedFinding(finding=finding, verdict=verdict, lane="probe")


PROBE_LANE = "probe"

# Why a scan executed fewer checks than the catalogue holds. The count alone is a
# number; the caller needs to know whether the lane was thin because the change was
# irrelevant to it or because the input could not support it.
NO_CANDIDATES = "no changed file matches any probe glob"
NO_CONTENT = "candidate files matched but none could be read"
PATCH_ONLY = ("raw patch input: file probes need whole-file content to judge an "
              "absent fix-marker, so only line probes ran")
SCAN_FAILED = "the probe scan raised; no check completed"


class ProbeScan(NamedTuple):
    """Probe findings plus the record of what the lane actually executed."""

    findings: list[VerifiedFinding]
    coverage: dict


def _probe_record(checks: int, note: str | None = None) -> dict:
    record = {"lane": PROBE_LANE, "status": "ran", "checks": checks,
              "eligible": len(probes.INCIDENT_PROBES)}
    if note:
        record["note"] = note
    return record


def probe_error_record() -> dict:
    """The record for a lane that crashed. Distinct from a lane that ran and found
    nothing: only one of the two is evidence about the code."""
    return {"lane": PROBE_LANE, "status": "error", "checks": 0,
            "eligible": len(probes.INCIDENT_PROBES), "note": SCAN_FAILED}


def probe_scan_for_files(repo: str | None, head_sha: str | None,
                         files: list, read: FileReader | None = None) -> ProbeScan:
    """Probe lane for a file set that need not belong to a PR (diff reviews).

    With repo + head_sha the changed files' full content is fetched and every
    probe runs, same fidelity as the PR path. With only patches, line probes run
    against each file's post-image patch text; FileProbes are skipped because
    their fix-marker-absent semantics cannot be judged on partial content — a
    marker outside the hunk would read as absent and fire a false positive.

    The returned coverage record carries how many probes actually executed, which
    is routinely zero: the catalogue is globbed to a handful of filenames, and a
    change touching none of them is scanned by nothing at all.
    """
    candidates = [f for f in files
                  if f.status != "removed" and _candidate_paths([f.filename])]
    if not candidates:
        return ProbeScan([], _probe_record(0, NO_CANDIDATES))
    contents: dict[str, str] = {}
    full_fidelity = read is not None or bool(repo and head_sha)
    if full_fidelity:
        for patch in candidates:
            content = (read(patch.filename) if read is not None
                       else _fetch_file_at(repo, patch.filename, head_sha))
            if content is not None:
                contents[patch.filename] = content
    else:
        contents = {f.filename: _post_image(f.patch) for f in candidates if f.patch}
    active = probes.INCIDENT_PROBES if full_fidelity else [
        probe for probe in probes.INCIDENT_PROBES if isinstance(probe, probes.Probe)]
    if not contents:
        return ProbeScan([], _probe_record(0, NO_CONTENT))
    findings = [_hit_to_finding(hit) for hit in probes.scan_contents(active, contents)]
    return ProbeScan(findings,
                     _probe_record(len(active), None if full_fidelity else PATCH_ONLY))


def probe_findings_for_files(repo: str | None, head_sha: str | None,
                             files: list) -> list[VerifiedFinding]:
    """Findings only, for callers that do not track coverage."""
    return probe_scan_for_files(repo, head_sha, files).findings


def _post_image(patch: str) -> str:
    """Added + context lines of a unified patch: the file as the change leaves it,
    minus unshown regions. Enough for line probes (they match single lines)."""
    kept = []
    for line in patch.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            kept.append(line[1:])
        elif not line.startswith(("-", "@", "\\", "+")):
            kept.append(line[1:] if line.startswith(" ") else line)
    return "\n".join(kept)


def _probe_by_id(probe_id: str):
    return next(probe for probe in probes.INCIDENT_PROBES if probe.probe_id == probe_id)


def _changed_paths(repo: str, pr: int) -> list[str]:
    return [f.filename for f in fetch_pr_files(repo, pr) if f.status != "removed"]


def _pr_head_sha(repo: str, pr: int) -> str:
    response = github.get(f"/repos/{repo}/pulls/{pr}")
    response.raise_for_status()
    return response.json()["head"]["sha"]


def _fetch_file_at(repo: str, path: str, ref: str) -> str | None:
    response = github.get(f"/repos/{repo}/contents/{github.quote_file_path(path)}", {"ref": ref})
    if response.status_code != 200:
        return None
    data = response.json()
    if isinstance(data, list) or data.get("encoding") != "base64":
        return None
    return decode_file_text(base64.b64decode(data["content"]))


def decode_file_text(content: bytes) -> str:
    """The text a review reads for a file. A NUL byte does not make it unreadable."""
    return content.decode("utf-8", "replace")
