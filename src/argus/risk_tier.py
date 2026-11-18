"""Label a change safe or risky from its paths and size only.

The merge decision stays with a human; this module only labels.
Patterns match path segments, ignoring case. A trailing slash matches directory
segments only, so files literally named traefik or cloudflared do not match.
"""

import json
import logging
import os
from dataclasses import dataclass, replace
from fnmatch import fnmatchcase

from .diff import FilePatch, change_stats

DEFAULT_TRIPWIRES = (
    "nginx*", "Caddyfile*", "traefik/", "cloudflared/", "docker-compose*",
    "compose.yml", "compose.yaml", "*auth*", "*proxy*",
)
DEFAULT_MAX_LINES = 300
ENV_TRIPWIRES = "ARGUS_RISK_TRIPWIRES"
ENV_MAX_LINES = "ARGUS_RISK_MAX_LINES"
LABEL_SAFE = "Risk tier: safe (path and size rule only; not a correctness verdict)"
LABEL_RISKY = (
    "Risk tier: risky (path and size rule only; needs the operator checkpoint; "
    "not a correctness verdict)"
)
log = logging.getLogger("argus.risk_tier")


def tripwires() -> tuple[str, ...]:
    patterns = tuple(part.strip() for part in os.environ.get(ENV_TRIPWIRES, "").split(",")
                     if part.strip())
    return patterns or DEFAULT_TRIPWIRES


def max_lines() -> int:
    raw = os.environ.get(ENV_MAX_LINES)
    if raw is None:
        return DEFAULT_MAX_LINES
    try:
        value = int(raw)
        if value > 0:
            return value
    except ValueError:
        pass
    log.warning("Invalid %s value %r; using %d", ENV_MAX_LINES, raw, DEFAULT_MAX_LINES)
    return DEFAULT_MAX_LINES


@dataclass(frozen=True)
class RiskTier:
    tier: str
    changed_lines: int
    max_lines: int
    files: int
    matches: tuple[tuple[str, str], ...]
    matches_total: int
    incomplete: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "tier": self.tier,
            "changed_lines": self.changed_lines,
            "max_lines": self.max_lines,
            "files": self.files,
            "matches": [{"path": path, "pattern": pattern} for path, pattern in self.matches[:20]],
            "matches_total": self.matches_total,
            "incomplete": self.incomplete,
            "reasons": list(self.reasons),
        }


def classify(paths, changed_lines, *, incomplete=False, patterns=None, limit=None) -> RiskTier:
    paths = list(paths)
    patterns = tripwires() if patterns is None else tuple(patterns)
    limit = max_lines() if limit is None else limit
    matches: list[tuple[str, str]] = []
    path_matches: dict[str, str] = {}
    for path in dict.fromkeys(paths):
        segments = path.casefold().split("/")
        for pattern in dict.fromkeys(patterns):
            candidates = segments[:-1] if pattern.endswith("/") else segments
            glob = pattern.removesuffix("/").casefold()
            if any(fnmatchcase(segment, glob) for segment in candidates):
                matches.append((path, pattern))
                path_matches.setdefault(path, pattern)
    reasons = [f"tripwire path {path} ({pattern})"
               for path, pattern in list(path_matches.items())[:5]]
    if len(path_matches) > 5:
        reasons.append(f"and {len(path_matches) - 5} more tripwire paths")
    if changed_lines > limit:
        reasons.append(f"{changed_lines} changed lines exceed the {limit} line limit")
    if incomplete:
        reasons.append("file list incomplete, an unseen path might match")
    return RiskTier(
        tier="risky" if reasons else "safe", changed_lines=changed_lines, max_lines=limit,
        files=len(paths), matches=tuple(matches[:20]), matches_total=len(matches),
        incomplete=bool(incomplete), reasons=tuple(reasons),
    )


def classify_files(files: list[FilePatch], *, incomplete=None) -> RiskTier:
    paths = []
    for item in files:
        paths.append(item.filename)
        if item.previous_filename:
            paths.append(item.previous_filename)
    count, added, removed = change_stats(files)
    if incomplete is None:
        incomplete = getattr(files, "unseen", False)
    return replace(classify(paths, added + removed, incomplete=incomplete), files=count)


def label(tier: str) -> str:
    return {"safe": LABEL_SAFE, "risky": LABEL_RISKY}[tier]


def from_row(review: dict, *, compact: bool = False) -> dict | None:
    tier = review.get("risk_tier")
    if tier not in ("safe", "risky"):
        return None
    try:
        stored = json.loads(review.get("risk_json"))
    except (ValueError, TypeError):
        stored = None
    result = dict(stored) if isinstance(stored, dict) else {}
    result.update(tier=tier, label=label(tier))
    reasons = result.get("reasons")
    if not isinstance(reasons, list) or not all(isinstance(reason, str) for reason in reasons):
        result["reasons"] = []
    if compact:
        return {key: result[key] for key in ("tier", "label", "reasons")}
    return result
