"""Rule index: the canonical set of rule ids a finding may cite.

A finding may cite a rule_id, but the model must never invent one. The index is
the single source of truth for what a real rule is, so an invented or misspelled
rule_id does not resolve and the finding reads as ungrounded. The shadow grounding
gate (review.log_grounding_shadow) uses is_grounded to measure how many bug-class
findings are anchored in a documented rule before any enforcement flips on.

The seed is a generic baseline. Extend or override it without a code edit via
data/rules/index.json (a JSON list of {"id","summary","source"}), or a path in
ARGUS_RULES_PATH.
"""

import functools
import json
import logging
import os
from pathlib import Path

log = logging.getLogger("argus.rules")

ENV_RULES_PATH = "ARGUS_RULES_PATH"
_DEFAULT_RULES = Path(__file__).resolve().parents[2] / "data" / "rules" / "index.json"

# Built-in floor: rules that hold even before the operator extends the JSON. Keyed
# by the canonical id the model is told to cite; the value is a one-line summary.
_SEED_RULES: dict[str, str] = {
    "loopback-binding": "Bind service ports to 127.0.0.1 when only a local reverse proxy needs them",
    "auth-gate": "Every public route must sit behind authentication; an anonymous request returns 302/401/403",
    "no-secret-in-code": "Never commit secrets or edit credential files",
    "least-privilege-token": "Tokens are read-only and scoped to the repos they need",
    "no-default-credentials": "Do not ship a default or placeholder password for a service; require it from the environment",
    "no-open-redirect": "Validate redirect targets; never redirect to an unvalidated URL",
    "parameterized-sql": "Pass values to SQL as bound parameters; never build a query by string formatting",
    "no-shell-true": "Do not run a subprocess with shell=True on untrusted input; pass an argument list",
    "webhook-signature": "Verify a webhook signature with a constant-time comparison before acting on the payload",
    "transaction-atomicity": "A unit of work commits or rolls back as one; no side-effecting commit, no partial write",
    "input-validation": "Validate and bound external input before it reaches storage or logic",
    "null-handling": "Handle the None/empty path explicitly; do not assume presence",
    "db-integrity": "Respect schema constraints; keep the read model and the event log consistent",
    "doc-code-consistency": "Code must match its docstring / documented contract",
    "pinned-dependencies": "Bound dependency versions; an open range lets a fresh resolve break a working build",
    "no-innerhtml-loop": "Never replace innerHTML in a WS/SSE/polling loop; update textContent only",
    "loading-error-states": "Every async operation has a loading and an error state",
    "feature-branch-only": "Never commit directly to the default branch; use a feature branch and a reviewed merge",
}


def _normalize(rule_id: str | None) -> str:
    return rule_id.strip().lower() if isinstance(rule_id, str) else ""


def _resolve_rules_path() -> Path | None:
    """First existing rules index among: the env override, the source-tree path,
    and the container /app layout. The installed package lives under site-packages
    so the parents[2] default does not resolve in the image; the /app candidate is
    where Dockerfile.api copies the index. None -> seed rules only."""
    candidates: list[Path] = []
    override = os.environ.get(ENV_RULES_PATH)
    if override:
        candidates.append(Path(override))
    candidates += [
        _DEFAULT_RULES,
        Path("/app/data/rules/index.json"),
        Path.cwd() / "data" / "rules" / "index.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


@functools.lru_cache(maxsize=1)
def index() -> dict[str, str]:
    """The merged rule index (built-in seed + the JSON file when present).

    Cached because it is stable for the life of the process; reset_cache() drops
    it (tests, or a hot config reload). A malformed or missing file degrades to
    the seed with a warning rather than failing a review.
    """
    merged = dict(_SEED_RULES)
    path = _resolve_rules_path()
    if path is None:
        return merged
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
        for entry in entries:
            rid = _normalize(entry.get("id"))
            if rid:
                merged[rid] = entry.get("summary", "")
    except (json.JSONDecodeError, OSError, AttributeError, TypeError) as error:
        log.warning("rules: could not load %s (%s); using seed only", path, error)
    return merged


def is_grounded(rule_id: str | None) -> bool:
    """Whether a finding's rule_id resolves to a real, documented rule."""
    return _normalize(rule_id) in index()


def reset_cache() -> None:
    """Drop the cached index so the next read re-merges seed + file."""
    index.cache_clear()
