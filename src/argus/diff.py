"""Fetch a GitHub PR's file patches and compress them to a token budget.

Compression follows PR-Agent's verified recipe: drop pure-deletion hunks, rank
files by language frequency then size, and pack until a token budget is hit,
spilling the remainder into a named-only overflow list.
"""

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import github
from .tokens import HeuristicEstimator, TokenEstimator

LANGUAGE_BY_EXTENSION = {
    ".py": "python", ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript", ".go": "go", ".rs": "rust",
    ".java": "java", ".rb": "ruby", ".php": "php", ".sh": "shell",
    ".yaml": "yaml", ".yml": "yaml", ".tf": "terraform", ".sql": "sql",
    ".md": "markdown", ".toml": "toml", ".json": "json",
}
REMOVED_STATUS = "removed"
HUNK_HEADER_PREFIX = "@@"
ADDED_FILE_HEADER = "+++"
log = logging.getLogger("argus.diff")


def _safe_path(path: str) -> bool:
    try:
        github.quote_file_path(path)
    except ValueError as error:
        log.warning("Dropping diff file with unsafe path %r: %s", path, error)
        return False
    return True


def _header_path(raw: str) -> str:
    # Git appends one TAB to ---/+++ paths that contain a space; any other tab stays
    # in the path so the safety check still rejects it.
    if raw.endswith("\t") and " " in raw[:-1]:
        raw = raw[:-1]
    return raw


def split_lines(text: str) -> list[str]:
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return [line.removesuffix("\r") for line in lines]


def touched_lines(patch: str | None) -> set[int]:
    """Added post-image lines and the post-image position of each deletion."""
    touched: set[int] = set()
    number: int | None = None
    for line in split_lines(patch or ""):
        header = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
        if header:
            number = int(header[1]) + (header[2] == "0")
        elif number is not None:
            if line.startswith(("+", "-")):
                touched.add(number)
            if not line or line.startswith(("+", " ")):
                number += 1
    return touched


def added_lines(patch: str | None) -> set[int]:
    """Post-image line numbers added by the patch, excluding file headers."""
    added: set[int] = set()
    number: int | None = None
    for line in split_lines(patch or ""):
        header = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if header:
            number = int(header[1])
        elif number is not None:
            if line.startswith("+"):
                added.add(number)
                number += 1
            # Some diff producers strip the context marker from blank lines.
            elif not line or line.startswith(" "):
                number += 1
    return added


@dataclass(frozen=True)
class FilePatch:
    filename: str
    status: str
    patch: str | None

    @property
    def language(self) -> str:
        return LANGUAGE_BY_EXTENSION.get(Path(self.filename).suffix.lower(), "other")


@dataclass
class CompressedDiff:
    prompt: str
    included: list[str] = field(default_factory=list)
    overflow: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    estimated_tokens: int = 0


def fetch_pr_files(repo: str, pr: int) -> list[FilePatch]:
    # Page through all changed files (100/page) so large PRs are reviewed whole,
    # never silently truncated; downstream compression trims to the token budget.
    files: list[FilePatch] = []
    page = 1
    while True:
        response = github.get(
            f"/repos/{repo}/pulls/{pr}/files", {"per_page": 100, "page": page}
        )
        response.raise_for_status()
        batch = response.json()
        files.extend(
            FilePatch(filename=f["filename"], status=f["status"], patch=f.get("patch"))
            for f in batch
            if _safe_path(f["filename"])
            and (not f.get("previous_filename") or _safe_path(f["previous_filename"]))
        )
        if len(batch) < 100:
            return files
        page += 1


def strip_deletion_only_hunks(patch: str) -> str:
    hunks: list[list[str]] = []
    current: list[str] = []
    for line in patch.splitlines():
        if line.startswith(HUNK_HEADER_PREFIX):
            if current:
                hunks.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        hunks.append(current)
    kept = [h for h in hunks if _has_addition(h)]
    return "\n".join("\n".join(h) for h in kept)


def _has_addition(hunk: list[str]) -> bool:
    return any(line.startswith("+") and not line.startswith(ADDED_FILE_HEADER) for line in hunk)


def _render_patch(patch: FilePatch) -> str:
    body = strip_deletion_only_hunks(patch.patch or "")
    return f"## File: {patch.filename} ({patch.language})\n```diff\n{body}\n```"


def compress_diff(
    files: list[FilePatch],
    budget_tokens: int,
    estimator: TokenEstimator | None = None,
) -> CompressedDiff:
    estimator = estimator or HeuristicEstimator()
    deleted = [f.filename for f in files if f.status == REMOVED_STATUS]
    modified = [f for f in files if f.status != REMOVED_STATUS and f.patch]

    frequency = Counter(f.language for f in modified)
    ranked = sorted(modified, key=lambda f: (-frequency[f.language], -estimator.estimate(f.patch or "")))

    result = CompressedDiff(prompt="", deleted=deleted)
    blocks: list[str] = []
    for patch in ranked:
        rendered = _render_patch(patch)
        cost = estimator.estimate(rendered)
        if result.estimated_tokens + cost <= budget_tokens:
            blocks.append(rendered)
            result.included.append(patch.filename)
            result.estimated_tokens += cost
        else:
            result.overflow.append(patch.filename)

    if result.overflow:
        blocks.append("## Other modified files (over budget, names only):\n" + "\n".join(result.overflow))
    if deleted:
        note = "## Deleted files:\n" + "\n".join(deleted)
        if result.estimated_tokens + estimator.estimate(note) <= budget_tokens:
            blocks.append(note)
            result.estimated_tokens += estimator.estimate(note)

    result.prompt = "\n\n".join(blocks)
    return result


def parse_unified_diff(text: str) -> list[FilePatch]:
    """Split `git diff` output into FilePatch items, the same shape the GitHub
    files API yields, so one review pipeline serves PR and pre-PR sources alike.

    Only hunk lines are kept as the patch body (GitHub's `patch` field has no
    ---/+++ headers either). Binary files carry patch=None; renames keep the
    post-image name.
    """
    files: list[FilePatch] = []
    name: str | None = None
    status = "modified"
    hunks: list[str] = []
    in_hunks = False
    unsafe = False
    path_header = False

    def flush() -> None:
        if name is not None and not unsafe:
            files.append(FilePatch(filename=name, status=status,
                                   patch="\n".join(hunks) if hunks else None))

    for line in split_lines(text):
        if line.startswith("diff --git"):
            flush()
            name, status, hunks, in_hunks = None, "modified", [], False
            unsafe = False
            path_header = False
        elif not in_hunks and line.startswith("new file mode"):
            status = "added"
        elif not in_hunks and line.startswith("deleted file mode"):
            status = "removed"
        elif not in_hunks and line.startswith(("rename from ", "rename to ")):
            path_header = True
            path = line.split(" ", 2)[2]
            if path.startswith('"') and path.endswith('"'):
                path = path[1:-1]
            unsafe = not _safe_path(path) or unsafe
            if line.startswith("rename to "):
                status = "renamed"
                name = path
        elif not in_hunks and line.startswith("+++ "):
            path_header = True
            path = _header_path(line[4:])
            if path.startswith('"') and path.endswith('"'):
                path = path[1:-1]
            if path != "/dev/null":
                name =path[2:] if path.startswith("b/") else path
                unsafe = not _safe_path(name) or unsafe
        elif not in_hunks and line.startswith("--- "):
            path_header = True
            path = _header_path(line[4:])
            if path.startswith('"') and path.endswith('"'):
                path = path[1:-1]
            if path != "/dev/null":
                path = path[2:] if path.startswith("a/") else path
                unsafe = not _safe_path(path) or unsafe
                if status == "removed" and name is None:
                    name = path
        elif line.startswith(HUNK_HEADER_PREFIX):
            in_hunks = True
            hunks.append(line)
        elif in_hunks:
            hunks.append(line)
        elif path_header and line:
            # A split filename must not silently turn into a different valid path.
            if line.startswith(("index ", "similarity index ", "dissimilarity index ",
                                "old mode ", "new mode ", "copy from ", "copy to ")):
                continue
            if line.startswith(("Binary files ", "GIT binary patch")):
                path_header = False
                continue
            log.warning("Dropping diff file with malformed path header %r", line)
            unsafe = True
    flush()
    return files
