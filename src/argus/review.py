"""The review pipeline: deep review -> independent verification -> rank/cap.

Structured output comes from a single tool call (tool_choice auto, see
client.complete_tool) instead of freeform JSON. The gateway lane requests a
strict tool, but replies still need per-finding validation; the tool schemas close
every object (additionalProperties false) because the Claude API refuses a strict tool
whose objects stay open. The OpenRouter lane sends no strict flag: it gets the
same schema, and a JSON object in the reply text is accepted only when no tool
call came back.

Best practices (see ARCHITECTURE.md): the review pass reports everything with
confidence + severity and does NOT self-filter; a separate skeptical verification
pass tries to refute each finding and drops the unverifiable ones; findings are
grounded in the repo's own house rules (CLAUDE.md) when present.
"""

import base64
import copy
import logging
import os
import re
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from hashlib import sha256
from urllib.parse import urlparse

import httpx
from pydantic import ValidationError

from . import fingerprint, github, rules, verdict
from .client import DEFAULT_MODEL, ToolSpec, complete_tool, prompt_cache_scope
from .context import (
    REVIEW_CONTEXT_BUDGET_TOKENS,
    FileReader, RenderedContext, cached_reader, render_review_context,
)
from .diff import (
    REMOVED_STATUS, FilePatch, added_lines, change_stats, compress_diff, fetch_pr_files, split_lines,
    touched_lines,
)
from .errors import HeadMoved
from .schema import (
    CATEGORIES, CounterEvidence, CounterEvidenceVerdict, Finding, JudgeOpinion, Review, Severity, Verdict,
    VerifiedFinding, parse_findings,
)
from .tokens import HeuristicEstimator

log = logging.getLogger("argus.review")

REVIEW_MAX_TOKENS = 8000
VERIFY_MAX_TOKENS = 2000
DIFF_BUDGET_TOKENS = 60000
HOUSE_RULES_MAX_CHARS = 6000
DEFAULT_BUG_CAP = 10
# In offline replays of 40 historical pull requests, a second pass added 79 raw findings but
# only 3 matched accepted defects, lifting recall from 23 to 24 of 67 while retained findings rose 32%.
try:
    REVIEW_PASSES = int(os.getenv("ARGUS_REVIEW_PASSES", "1"))
except ValueError:
    REVIEW_PASSES = 1
if not 1 <= REVIEW_PASSES <= 3:
    REVIEW_PASSES = 1

BUG_SEVERITIES = (Severity.critical, Severity.major)
_SEVERITY_RANK = {Severity.critical: 0, Severity.major: 1, Severity.minor: 2, Severity.nit: 3}
_VIEW_PREFIX = re.compile(r"^\s*\d+ [+\- ] ")

_FINDING_SCHEMA = {
    "type": "object",
    "properties": {
        "file": {"type": "string"},
        "line": {"type": "integer", "description": "1-based line in the changed file"},
        "end_line": {"type": ["integer", "null"]},
        "category": {"type": "string", "enum": list(CATEGORIES)},
        "evidence": {"type": "string"},
        "severity": {"type": "string", "enum": ["critical", "major", "minor", "nit"]},
        "confidence": {"type": "number", "description": "0.0 to 1.0"},
        "title": {"type": "string"},
        "description": {"type": "string"},
        "proposed_fix": {"type": "string"},
        "rule_id": {"type": ["string", "null"]},
    },
    "required": ["file", "line", "severity", "confidence", "title", "description", "proposed_fix",
                 "end_line", "category", "evidence"],
    "additionalProperties": False,
}
REVIEW_TOOL = ToolSpec(
    name="report_findings",
    input_schema={
        "type": "object",
        "properties": {"findings": {"type": "array", "items": _FINDING_SCHEMA}},
        "required": ["findings"],
        "additionalProperties": False,
    },
    max_tokens=REVIEW_MAX_TOKENS,
)
VERDICT_TOOL = ToolSpec(
    name="report_verdict",
    input_schema={
        "type": "object",
        "properties": {
            "is_real": {"type": "boolean"},
            "confidence": {"type": "number", "description": "0.0 to 1.0"},
            "reasoning": {"type": "string"},
        },
        "required": ["is_real", "confidence", "reasoning"],
        "additionalProperties": False,
    },
    max_tokens=VERIFY_MAX_TOKENS,
)

REVIEW_SYSTEM = """You are a senior engineer reviewing a pull request. Find the defects this change introduces or exposes: problems a careful human reviewer would want fixed before merge.

How to read the input:
- Each changed file is shown with post-change line numbers in the left column. Lines marked "+" were added or changed by this pull request, lines marked "-" were removed (they have no line number), and unmarked lines are unchanged context. Some files are shown in full, some as windows around the changes, some as hunks only.
- Report "line" as the number in the left column of the line where the defect is (for a removed line, the number of the next line shown). Report "end_line" as the last line of the defective span, or null.
- The pull request title and description, when present, state what the author intended and claims. Check the code against them, and flag docs, comments, tests, changelogs or descriptions that contradict the code.

What to report:
- Report every defect you can substantiate from the code shown, including ones you are not fully sure of; a separate verification step filters, so coverage matters more than caution here. Do not pad: skip speculative performance concerns without a concrete hot path and cost, pure style or formatting, and issues that live only in unchanged code the change neither touches nor relies on.
- Work through this checklist on every change. Most defects this reviewer has missed fall in these classes:
  1. Contracts and consistency: does the change agree with its callers, callees, tests, docs, README, changelog, comments, configuration and the PR description? Environment variables or settings added in code but not where they are deployed or read; migrations that disagree with models or import application code; changed return shapes.
  2. Error paths: a failure recorded as success (exit code 0, "ok", HTTP 200, a passing gate or test on an error path), swallowed exceptions, fallbacks that hide a failure, partial failures, errors misclassified as a different kind.
  3. Input handling: unvalidated input at a trust boundary, unknown flags or options silently accepted (a misspelled --dry-run that runs live), lenient parsing (number prefixes, truthiness checks that treat an empty value as missing).
  4. Boundaries: off-by-one, inclusive versus exclusive limits, empty, None, zero, first and last element, a single page treated as the complete result, truncation.
  5. Time and resources: network or subprocess calls without a timeout, unbounded retries, loops, queues or memory, resources that are never closed.
  6. Concurrency and durability: read-then-write races, multi-step writes that are not atomic, state that a rollback can undo, retries that are not idempotent.
  7. Security: injection (SQL, shell, template, HTML), missing or bypassable authorization, secrets in code, logs or docs, path traversal, SSRF, unsafe deserialization, services bound to all interfaces.
  8. Tests: does each new or changed test exercise the changed behaviour and assert its outcome? Unused setup, assertions that cannot fail, tests that would still pass with the change reverted, a new failure path without a test.
  9. Data integrity: lossy or unstable serialization, identifiers or hashes that collide or drift, schema drift.

How to write each finding:
- title: the defect in one line.
- description: what is wrong, a concrete scenario (inputs or conditions leading to the wrong outcome), and why it matters. Be specific and brief.
- evidence: the exact code line or lines from the view that show the defect, copied verbatim without the line-number and marker columns, at most 3 lines.
- proposed_fix: the concrete code-level change; when the fix is local, include the corrected code (a few lines).
- category: the checklist class the defect belongs to.
- severity, by this rubric; when torn between two levels, choose the lower one:
  - critical: an exploitable security hole, data loss or corruption, or an outage in normal operation.
  - major: wrong behaviour on a realistic path, a broken contract between components, a failure reported as success, or a missing timeout that can hang a service.
  - minor: an edge-case bug, a robustness gap, or docs, comments or tests that mislead.
  - nit: a small maintainability point; report these sparingly.
- confidence: your honest probability that the defect is real.
- rule_id: cite one from the rule index only when the finding matches it; never invent one.

Report all findings in one report_findings call."""

VERIFY_SYSTEM = """You are a skeptical senior engineer verifying ONE finding from a code review. \
Try to REFUTE it using only the diff. Decide whether the flagged code is actually wrong and \
whether the proposed fix is needed. If you cannot confirm the issue is real from the evidence, \
mark it not real; default to refuted when uncertain. Report via the report_verdict tool."""

# Wording adapted from the review filter prompts of alibaba/open-code-review
# (internal/config/template/prompts/review_filter_task_system.md and
# review_filter_task_user.md, commit 8eb8334), Copyright 2026 alibaba/open-code-review
# Contributors, licensed under the Apache License, Version 2.0
# (http://www.apache.org/licenses/LICENSE-2.0). Changed here: one finding per call
# instead of a batch, this repository's protected classes, and quoted counter-evidence.
VERIFY_SYSTEM_DEFAULT_KEEP = """You are a fact-checker verifying ONE finding from a code review. \
The reviewer read full files and the pull request text. You see only the diff, so anything \
you cannot see, the reviewer may have seen.

Keep the finding (is_real true) unless the diff proves it wrong. Removing a real finding \
destroys it silently; keeping a wrong one costs a reader a few seconds. "I cannot verify \
this", "suspicious", "low value" and "the code looks fine to me" all mean keep.

Refute the finding (is_real false) only on one of two grounds:
A. The construct the finding describes is absent from the diff of the finding's file. The \
same construct in another file does not count.
B. A specific diff line literally contradicts the finding's central claim, with no chain of \
reasoning needed to see it.

Protected classes: never refute a finding about error paths, behavior changes, concurrency, \
boundaries, auth and middleware order, silent fallbacks or missing timeouts by reasoning \
alone. Refute one only when a quoted diff line meets ground A or B; your own belief that the \
code behaves correctly is never enough.

When you refute, set counter_evidence to the diff lines that establish the ground: the file \
path, the first and last line number in the changed file (from the hunk headers), and the \
lines copied verbatim without the leading +, - or space marker. For ground A, quote the lines \
where the finding places the construct. When you keep the finding, set counter_evidence to \
null. Report via the report_verdict tool."""

VERIFY_VARIANT_ENV = "ARGUS_VERIFY_VARIANT"
SHIPPED_VARIANT = "shipped"

# Original inputs kept 26/29 known defects versus 21/29, with fewer findings retained overall.
_SHIPPED_PAYLOAD_FIELDS = {
    "file", "line", "severity", "confidence", "title", "description", "proposed_fix", "rule_id",
}


def _payload_builder(fields: set[str]) -> Callable[[Finding, str], str]:
    def build(finding: Finding, diff: str) -> str:
        payload = finding.model_dump_json(indent=2, include=fields)
        return f"# Finding\n{payload}\n\n# Diff under review\n{diff}"
    return build


def _verify_prefix(diff: str) -> str:
    return f"# Diff under review\n{diff}\n\n"


def _diff_first_builder(fields: set[str]) -> Callable[[Finding, str], str]:
    def build(finding: Finding, diff: str) -> str:
        payload = finding.model_dump_json(indent=2, include=fields)
        return _verify_prefix(diff) + f"# Finding\n{payload}"
    return build


def _verdict_tool(properties: dict) -> ToolSpec:
    return ToolSpec(
        name=VERDICT_TOOL.name,
        input_schema={"type": "object", "properties": copy.deepcopy(properties),
                      "required": list(properties), "additionalProperties": False},
        max_tokens=VERIFY_MAX_TOKENS,
    )


_VERDICT_PROPERTIES = VERDICT_TOOL.input_schema["properties"]
# Strict tool schemas cannot express "required when is_real is false", so the field is
# always present and nullable, and the prompt says when to fill it.
DEFAULT_KEEP_VERDICT_TOOL = _verdict_tool({**_VERDICT_PROPERTIES, "counter_evidence": {
    "type": ["object", "null"],
    "properties": {"path": {"type": "string"}, "start_line": {"type": "integer"},
                   "end_line": {"type": "integer"}, "quote": {"type": "string"}},
    "required": ["path", "start_line", "end_line", "quote"],
    "additionalProperties": False,
    "description": "Required when is_real is false: the diff lines that refute the finding. "
                   "Null when is_real is true.",
}})
REASONING_FIRST_VERDICT_TOOL = _verdict_tool(
    {name: _VERDICT_PROPERTIES[name] for name in ("reasoning", "is_real", "confidence")})


@dataclass(frozen=True)
class VerifierVariant:
    """One verifier configuration. Each non-shipped variant changes one input."""

    system: str
    tool: ToolSpec
    user_prompt: Callable[[Finding, str], str]
    verdict_type: type[Verdict] = Verdict


CHECKLIST_CLOSING = ("These answers inform your decision but are not a ground to refute: "
                     "refute only on ground A or ground B.")


def _checklist(topic: str, questions: str) -> str:
    # The category is wider than the topic, so the scope line tells the verifier when to skip it.
    return (f"Use this checklist only if the finding is about {topic}; otherwise ignore it.\n"
            f"{questions}\n{CHECKLIST_CLOSING}")


# The reviewer's wording of these classes hides the defect behind a plausible guard, so each
# checklist asks the concrete question that exposes it. Keyed by finding category.
VERIFY_CHECKLISTS = {
    "resources": _checklist("a missing timeout", (
        "Answer from the diff before you decide:\n"
        "1. Does the call pass a timeout, or does the client, the session or a default in the "
        "diff set one that applies to this call?\n"
        "2. Is the call on a request path, so that a hang blocks a caller?")),
    "error_paths": _checklist("a silent fallback", (
        "Answer from the diff before you decide:\n"
        "1. After the fallback runs, can the caller tell it apart from a success: a raised "
        "error, a status, a flag or a log the caller reads?\n"
        "2. Does the fallback value look like a valid result?")),
    "input_handling": _checklist("an input guard", (
        "Answer from the diff before you decide:\n"
        "1. What exactly does the guard guarantee about the input?\n"
        "2. Which input shape bypasses it: empty, None, another type, a prefix, a different "
        "encoding, or a value that reaches the code by another path?")),
    "security": _checklist("auth or middleware order", (
        "Answer from the diff before you decide:\n"
        "1. Is the guard, check or middleware registered before the route or handler it "
        "protects?\n"
        "2. Does any path reach the handler without passing through the guard?")),
}
CHECKLIST_HEADING = "# Checklist for this finding"


def _checklist_builder(base: Callable[[Finding, str], str]) -> Callable[[Finding, str], str]:
    # The checklist rides on the user prompt, not the system prompt: it depends on the finding,
    # and the system prompt is a plain string shared with default_keep.
    def build(finding: Finding, diff: str) -> str:
        prompt = base(finding, diff)
        checklist = VERIFY_CHECKLISTS.get(finding.category)
        return prompt if checklist is None else f"{prompt}\n\n{CHECKLIST_HEADING}\n{checklist}"
    return build


_SHIPPED_PAYLOAD = _payload_builder(_SHIPPED_PAYLOAD_FIELDS)
VERIFIER_VARIANTS = {
    SHIPPED_VARIANT: VerifierVariant(VERIFY_SYSTEM, VERDICT_TOOL, _SHIPPED_PAYLOAD),
    "shipped_diff_first": VerifierVariant(VERIFY_SYSTEM, VERDICT_TOOL,
                                          _diff_first_builder(_SHIPPED_PAYLOAD_FIELDS)),
    "default_keep": VerifierVariant(VERIFY_SYSTEM_DEFAULT_KEEP, DEFAULT_KEEP_VERDICT_TOOL,
                                    _SHIPPED_PAYLOAD, CounterEvidenceVerdict),
    "default_keep_checklists": VerifierVariant(
        VERIFY_SYSTEM_DEFAULT_KEEP, DEFAULT_KEEP_VERDICT_TOOL,
        _checklist_builder(_SHIPPED_PAYLOAD), CounterEvidenceVerdict),
    "reasoning_first": VerifierVariant(VERIFY_SYSTEM, REASONING_FIRST_VERDICT_TOOL, _SHIPPED_PAYLOAD),
    "evidence": VerifierVariant(VERIFY_SYSTEM, VERDICT_TOOL,
                                _payload_builder(_SHIPPED_PAYLOAD_FIELDS | {"evidence"})),
}


def _unknown_variant(value: str) -> ValueError:
    return ValueError(f"{value!r} is not a verifier variant; expected one of "
                      f"{', '.join(VERIFIER_VARIANTS)}")


def _variant_from_env() -> str:
    # A typo here would silently run the shipped verifier in a comparison run, so the
    # service refuses to start instead.
    value = os.getenv(VERIFY_VARIANT_ENV, "").strip() or SHIPPED_VARIANT
    if value not in VERIFIER_VARIANTS:
        raise ValueError(f"{VERIFY_VARIANT_ENV}: {_unknown_variant(value)}")
    return value


VERIFY_VARIANT = _variant_from_env()


def _resolve_variant(variant: str | None) -> str:
    """The variant a call runs: the explicit name, else the setting read at load."""
    name = VERIFY_VARIANT if variant is None else variant
    if name not in VERIFIER_VARIANTS:
        raise _unknown_variant(name)
    return name


FAST_MODE_REASON = "mode=fast: the reviewer lane is not run"
FAST_MODE_JUDGE_REASON = (
    "mode=fast: no reviewer finding exists for a judge to second-guess")
NO_REVIEWABLE_PATCH_REASON = "no reviewable patch text: nothing for the reviewer to read"


def _llm_skipped(reason: str, **counts) -> dict:
    return {"lane": verdict.LANE_LLM, "status": verdict.LANE_SKIPPED, "reason": reason, **counts}


def _llm_file_counts(files: list[FilePatch], rendered: RenderedContext) -> dict:
    seen = set(rendered.full + rendered.windowed + rendered.hunks_only)
    changed = [f for f in files if f.status != REMOVED_STATUS]
    unread = [f for f in changed if f.filename not in seen]
    return {"files_reviewed": len(seen),
            "files_full": len(rendered.full), "files_windowed": len(rendered.windowed),
            "files_hunks_only": len(rendered.hunks_only),
            "files_read_failed": rendered.files_read_failed,
            "context_tokens_estimated": rendered.estimated_tokens,
            "files_omitted": len(unread),
            "files_without_patch": sum(1 for f in unread if not f.patch)}


def _llm_ran(model: str, files: list[FilePatch], rendered: RenderedContext, stats: dict,
             verify_variant: str) -> dict:
    record = {"lane": verdict.LANE_LLM, "status": verdict.LANE_RAN, "model": model,
              "verify_variant": verify_variant, **_llm_file_counts(files, rendered), **stats}
    received = stats.get("findings_received", 0)
    if received > 0 and stats.get("findings_dropped") == received:
        record.update(status=verdict.LANE_ERROR,
                      reason=f"reviewer output unusable: all {received} findings were malformed")
    return record


@dataclass
class ReviewResult:
    raw_count: int
    bugs: list[VerifiedFinding] = field(default_factory=list)
    nits: list[VerifiedFinding] = field(default_factory=list)
    dropped: int = 0
    # Every merged finding (pre-verification), each with its shadow verifier
    # verdict. Persisted to findings_raw so raw vs verified precision is
    # measurable later. Empty for stub/legacy results, which keeps old callers valid.
    all_findings: list[VerifiedFinding] = field(default_factory=list)
    # One record per lane: what ran, what was skipped and why, how many checks
    # executed. Only the engine knows this — a finding list cannot distinguish a
    # lane that found nothing from a lane that never ran. Empty on stub results,
    # which the verdict document reports as `unknown` rather than assuming full.
    coverage: list[dict] = field(default_factory=list)
    head_committed_at: str | None = None
    head_sha: str | None = None
    # PR header facts the run already fetched. None means the source did not
    # supply them (a raw diff has no branch or author), and readers omit them.
    branch: str | None = None
    author: str | None = None
    files_changed: int | None = None
    lines_added: int | None = None
    lines_removed: int | None = None


ENV_FALLBACK_MODEL = "ARGUS_FALLBACK_MODEL"


def fallback_for(
    primary_model: str, make_runner: Callable[[str], Callable[[], ReviewResult]]
) -> tuple[Callable[[], ReviewResult] | None, str | None]:
    """Build the fallback runner if ARGUS_FALLBACK_MODEL is set and differs from
    the primary (else None). Opt-in: unset means fail-hard. The model choice stays
    the operator's; the fallback is explicit and recorded."""
    fallback = os.environ.get(ENV_FALLBACK_MODEL, "").strip()
    if fallback and fallback != primary_model:
        return make_runner(fallback), fallback
    return None, None


def _from_github(error: Exception) -> bool:
    """True when an httpx error came from a request to the GitHub API."""
    if not isinstance(error, httpx.HTTPError):
        return False
    try:
        return error.request.url.host == urlparse(github.GITHUB_API).hostname
    except RuntimeError:  # a transport error raised before any request was attached
        return False


def fallback_eligible(error: Exception) -> bool:
    """A second model cannot fix a GitHub-side failure (an auth, quota or HTTP
    error from the GitHub API) or a moved head. A failure of the model provider
    stays eligible."""
    return not (isinstance(error, (HeadMoved, github.GitHubRateLimited,
                                   github.GitHubDenied, github.GitHubAuthError))
                or _from_github(error))


def run_review(
    repo: str, pr: int, model: str = DEFAULT_MODEL, bug_cap: int = DEFAULT_BUG_CAP,
    *, expected_head_sha: str | None = None, verify_variant: str | None = None,
) -> ReviewResult:
    from .jarvis import live

    from .repo_policy import validate_repo

    validate_repo(repo)
    verify_variant = _resolve_variant(verify_variant)
    files = fetch_pr_files(repo, pr)
    patches = {f.filename: f.patch or "" for f in files}
    try:
        meta = live._pr_meta(repo, pr)
    except (github.GitHubRateLimited, github.GitHubDenied, github.GitHubAuthError):
        raise
    except Exception:
        log.exception("PR metadata lookup failed for %s#%s; reviewing without it", repo, pr)
        meta = {"title": "", "body": "", "commits": []}
    head_sha = meta.get("head_sha") or _head_sha_for(repo, pr)
    if expected_head_sha is not None:
        if not isinstance(head_sha, str) or not head_sha:
            raise RuntimeError("Could not resolve PR head after listing")
        if head_sha != expected_head_sha:
            raise HeadMoved("PR head changed after listing")
    read = _file_reader(repo, head_sha)
    pr_section = _pr_section(meta.get("title"), meta.get("body"), meta.get("commits"))
    rendered = render_review_context(files, read, REVIEW_CONTEXT_BUDGET_TOKENS, HeuristicEstimator())
    if rendered.views:
        house_rules = fetch_house_rules(repo)
        intent = _intent_for_prompt(repo, pr, meta)
        review = _deep_review(rendered.prompt, house_rules, model, intent, pr_section)
        stats = {**review.parsing_stats, **_reanchor_findings(review.findings, read, patches)}
        llm_record = _llm_ran(model, files, rendered, stats, verify_variant)
    else:
        review = Review()
        llm_record = _llm_skipped(NO_REVIEWABLE_PATCH_REASON,
                                 **_llm_file_counts(files, rendered))
    diff = compress_diff(files, DIFF_BUDGET_TOKENS, HeuristicEstimator()).prompt
    kept = _suppress_known_fps(repo, pr, review.findings)
    verified = _verify_findings(kept, diff, model, verify_variant, read)
    _record_voided_refutations(llm_record, verify_variant, verified)
    log_grounding_shadow(repo, pr, verified)
    bugs, nits = rank_and_cap(verified, bug_cap)
    probe_bugs, probe_nits, probe_record = _probe_lane(repo, pr, files, head_sha, read)
    ruff_found, ruff_record = _ruff_lane(files, repo, head_sha, read)
    config_found, config_record = _config_lane(files, repo, head_sha, read)
    deterministic = ruff_found + config_found
    det_bugs = [i for i in deterministic if i.finding.severity in BUG_SEVERITIES]
    det_nits = [i for i in deterministic if i.finding.severity not in BUG_SEVERITIES]
    _stamp_fingerprints(verified + probe_bugs + probe_nits + deterministic, patches)
    judge_record = _judge_lane(bugs, patches, model)
    files_changed, lines_added, lines_removed = change_stats(files)
    return ReviewResult(
        # raw_count/dropped stay scoped to the merged LLM findings; the
        # deterministic lanes are additive sources.
        raw_count=len(review.findings),
        bugs=bugs + probe_bugs + det_bugs, nits=nits + probe_nits + det_nits,
        dropped=len(review.findings) - len(bugs) - len(nits),
        all_findings=verified,
        head_committed_at=meta.get("head_committed_at"),
        head_sha=head_sha,
        branch=meta.get("branch"), author=meta.get("author"),
        files_changed=files_changed, lines_added=lines_added, lines_removed=lines_removed,
        coverage=[probe_record, ruff_record, config_record,
                  llm_record, judge_record],
    )


def run_diff_review(
    files: list[FilePatch], repo: str | None = None, head_sha: str | None = None,
    model: str = DEFAULT_MODEL, bug_cap: int = DEFAULT_BUG_CAP, fast: bool = False,
    *, file_loader: FileReader | None = None, pr_title: str | None = None,
    pr_body: str | None = None, base_sha: str | None = None,
    commits: list[str] | None = None, verify_variant: str | None = None,
) -> ReviewResult:
    """Review a change that need not belong to any PR: agent worktree diffs,
    pre-PR branches, or a repo/sha pair fetched via the compare API.

    fast=True runs only the deterministic lanes (probes over the changed files),
    no LLM calls — the sub-10s gate an agent can block on mid-loop. The full mode
    is the same pipeline as a PR review minus PR-only enrichment (Linear intent).
    """
    from .jarvis import live

    from .repo_policy import validate_repo

    if repo:
        validate_repo(repo)
    verify_variant = _resolve_variant(verify_variant)
    read = _file_reader(repo, head_sha, file_loader)
    patches = {f.filename: f.patch or "" for f in files}
    try:
        scan = live.probe_scan_for_files(repo, head_sha, files, read)
        probe_verified, probe_record = scan.findings, scan.coverage
    except (github.GitHubRateLimited, github.GitHubDenied, github.GitHubAuthError):
        raise
    except Exception:
        log.exception("probe lane failed for diff review; continuing without it")
        probe_verified, probe_record = [], live.probe_error_record()
    probe_bugs = [i for i in probe_verified if i.finding.severity in BUG_SEVERITIES]
    probe_nits = [i for i in probe_verified if i.finding.severity not in BUG_SEVERITIES]
    if fast:
        # Every deterministic lane, which is what "fast" has always claimed to
        # mean. Running the probe lane alone while the contract said "the
        # deterministic lanes" let a diff carrying an injection or an open auth
        # skip come back with nothing found, which is the overstatement this
        # whole contract exists to prevent.
        ruff_found, ruff_record = _ruff_lane(files, repo, head_sha, read)
        config_found, config_record = _config_lane(files, repo, head_sha, read)
        deterministic = ruff_found + config_found
        det_bugs = [i for i in deterministic if i.finding.severity in BUG_SEVERITIES]
        det_nits = [i for i in deterministic
                    if i.finding.severity not in BUG_SEVERITIES]
        _stamp_fingerprints(probe_verified + deterministic, patches)
        changed, added, removed = change_stats(files)
        return ReviewResult(
            raw_count=0, bugs=probe_bugs + det_bugs, nits=probe_nits + det_nits,
            dropped=0, files_changed=changed, lines_added=added, lines_removed=removed,
            # The judge lane is listed even though it obviously cannot run here.
            # A reader must not have to already know the full set of lanes to
            # notice that one is missing from the document.
            coverage=[probe_record, ruff_record, config_record,
                      _llm_skipped(FAST_MODE_REASON),
                      _judge_skipped(FAST_MODE_JUDGE_REASON)])

    pr_section = _pr_section(pr_title, pr_body, commits)
    rendered = render_review_context(files, read, REVIEW_CONTEXT_BUDGET_TOKENS, HeuristicEstimator())
    if rendered.views:
        house_rules = fetch_house_rules(repo) if repo else None
        review = _deep_review(rendered.prompt, house_rules, model, None, pr_section)
        stats = {**review.parsing_stats, **_reanchor_findings(review.findings, read, patches)}
        llm_record = _llm_ran(model, files, rendered, stats, verify_variant)
    else:
        review = Review()
        llm_record = _llm_skipped(NO_REVIEWABLE_PATCH_REASON,
                                 **_llm_file_counts(files, rendered))
    diff = compress_diff(files, DIFF_BUDGET_TOKENS, HeuristicEstimator()).prompt
    kept = _suppress_known_fps(repo or "", 0, review.findings)
    verified = _verify_findings(kept, diff, model, verify_variant, read)
    _record_voided_refutations(llm_record, verify_variant, verified)
    log_grounding_shadow(repo or "local", 0, verified)
    bugs, nits = rank_and_cap(verified, bug_cap)
    ruff_found, ruff_record = _ruff_lane(files, repo, head_sha, read)
    config_found, config_record = _config_lane(files, repo, head_sha, read)
    deterministic = ruff_found + config_found
    det_bugs = [i for i in deterministic if i.finding.severity in BUG_SEVERITIES]
    det_nits = [i for i in deterministic if i.finding.severity not in BUG_SEVERITIES]
    _stamp_fingerprints(verified + probe_verified + deterministic, patches)
    judge_record = _judge_lane(bugs, patches, model)
    changed, added, removed = change_stats(files)
    return ReviewResult(
        raw_count=len(review.findings),
        bugs=bugs + probe_bugs + det_bugs, nits=nits + probe_nits + det_nits,
        dropped=len(review.findings) - len(bugs) - len(nits),
        all_findings=verified,
        files_changed=changed, lines_added=added, lines_removed=removed,
        coverage=[probe_record, ruff_record, config_record,
                  llm_record, judge_record],
    )


def _file_reader(repo: str | None, head_sha: str | None,
                 file_loader: FileReader | None = None) -> FileReader | None:
    from .jarvis import live

    if file_loader is None and repo and head_sha:
        def file_loader(path: str) -> str | None:
            return live._fetch_file_at(repo, path, head_sha)
    return cached_reader(file_loader)


def _pr_section(title: str | None, body: str | None,
                commits: list[str] | None = None) -> str:
    if not (title or body):
        return ""
    description = (body or "")[:4000]
    if body and len(body) > 4000:
        description += "\n[Description truncated at 4,000 characters]"
    section = (f"# Pull request\nTitle: {title or ''}\n"
               "Description (the author's stated intent and claims; check them against the code):\n"
               + description)
    if commits:
        subjects = [message.splitlines()[0] for message in commits if message.strip()][:20]
        section += "\nCommits:\n" + "\n".join(f"- {subject}" for subject in subjects)
    return section


# Shortest text, in non-whitespace characters, that counts as a quote of real code.
MIN_QUOTE_CHARS = 8


def _reanchor_findings(findings: list, read: FileReader | None,
                       patches: dict[str, str] | None = None) -> dict:
    counts = {"findings_reanchored": 0, "evidence_not_found": 0, "evidence_too_short": 0,
              "evidence_missing": 0, "evidence_unreadable": 0, "evidence_removed_line": 0}
    if read is None:
        return counts
    for finding in findings:
        evidence = ""
        for line in split_lines(finding.evidence):
            # Evidence may have been copied from the numbered review view.
            candidate = _VIEW_PREFIX.sub("", line).strip()
            if len(re.sub(r"\s", "", candidate)) >= MIN_QUOTE_CHARS:
                evidence = candidate
                break
        if not evidence:
            counts["evidence_too_short" if finding.evidence.strip() else "evidence_missing"] += 1
            continue
        try:
            content = read(finding.file)
        except (github.GitHubRateLimited, github.GitHubDenied, github.GitHubAuthError):
            raise
        except Exception:
            counts["evidence_unreadable"] += 1
            continue
        if content is None:
            continue
        lines = split_lines(content)
        nearby = lines[max(0, finding.line - 3):finding.line + 2]
        if any(evidence in line for line in nearby):
            continue
        matches = [n for n, line in enumerate(lines, 1) if evidence in line]
        patch = (patches or {}).get(finding.file) or ""
        if evidence in {line[1:].strip() for line in split_lines(patch) if line.startswith("-")}:
            # Code the change removed survives only as an added copy (a moved line); any other
            # occurrence is unrelated code.
            added = added_lines(patch)
            matches = [n for n in matches if n in added]
            if not matches:
                counts["evidence_removed_line"] += 1
                continue
        if not matches:
            counts["evidence_not_found"] += 1
            continue
        number = min(matches, key=lambda n: abs(n - finding.line))
        if finding.end_line is not None:
            finding.end_line += number - finding.line
        finding.line = number
        counts["findings_reanchored"] += 1
    return counts


def _stamp_fingerprints(items: list[VerifiedFinding], patches: dict[str, str]) -> None:
    for item in items:
        found = item.finding
        context = fingerprint.context_line(patches.get(found.file), found.line)
        item.fingerprint = fingerprint.compute(found.file, found.rule_id, found.title, context)


JUDGE_LIVE_ENV = "ARGUS_JUDGE_LIVE"
_JUDGE_VERDICTS = ("real", "fp", "unsure")


NO_WHOLE_FILES = ("raw patch input: ruff parses whole files, and a fragment would "
                  "report a syntax error on every truncation")
NO_PYTHON = "no changed file is Python, so no rule applies"
RUFF_ALWAYS_KEEP = {"F821", "invalid-syntax"}


def _head_sha_for(repo: str, pr: int) -> str | None:
    """The PR's head SHA, best-effort. Without it the ruff lane has no whole-file
    content to read and records a skip, which is the honest outcome."""
    from .jarvis import live

    try:
        return live._pr_head_sha(repo, pr)
    except (github.GitHubRateLimited, github.GitHubDenied, github.GitHubAuthError):
        raise
    except Exception:
        log.exception("head sha lookup failed for %s#%s", repo, pr)
        return None


def _ruff_lane(files: list[FilePatch], repo: str | None,
               head_sha: str | None, read: FileReader | None = None) -> tuple[list[VerifiedFinding], dict]:
    """Findings whose truth needs no opinion, plus the record of what was read.

    Counts come from the file set the orchestrator handed over, never from what
    the tool reports having looked at, so a lane cannot balance its own books
    against a denominator it chose.
    """
    from .jarvis import ruff_lane

    record: dict = {"lane": ruff_lane.LANE, "files_changed": len(files)}
    candidates = ruff_lane.eligible(files)
    record["files_eligible"] = len(candidates)
    if not candidates:
        return [], {**record, "status": verdict.LANE_SKIPPED, "reason": NO_PYTHON,
                    "files_analyzed": 0}
    if read is None and not (repo and head_sha):
        return [], {**record, "status": verdict.LANE_SKIPPED,
                    "reason": NO_WHOLE_FILES, "files_analyzed": 0}

    from .jarvis import live

    found: list[VerifiedFinding] = []
    analysed = 0
    # No patch means the change cannot be scoped, except a pure rename, which changed no line.
    changed_lines = {f.filename: None if f.patch is None and f.status != "renamed"
                     else touched_lines(f.patch) for f in files}
    suppressed = 0
    suppressed_codes: set[str] = set()
    try:
        for path in candidates:
            content = read(path) if read is not None else live._fetch_file_at(repo, path, head_sha)
            if content is None:
                continue
            for hit in ruff_lane.scan_content(path, content):
                if (hit.code.startswith("E9") or hit.code in RUFF_ALWAYS_KEEP
                        or changed_lines[path] is None
                        or any(hit.line <= line <= hit.end_line for line in changed_lines[path])):
                    found.append(ruff_lane.to_finding(hit))
                else:
                    suppressed += 1
                    suppressed_codes.add(hit.code)
            analysed += 1
    except (github.GitHubRateLimited, github.GitHubDenied, github.GitHubAuthError):
        raise
    except Exception as error:
        log.exception("ruff lane failed for %s@%s", repo, head_sha)
        return [], {**record, "status": "error", "files_analyzed": analysed,
                    "reason": f"{type(error).__name__}: {error}"[:200]}
    return found, {**record, "status": verdict.LANE_RAN, "files_analyzed": analysed,
                   "findings": len(found), "findings_preexisting_suppressed": suppressed,
                   "preexisting_codes": sorted(suppressed_codes)[:20],
                   "tool": ruff_lane.tool_identity()}


NO_YAML = "no changed YAML or Dockerfile to read"
NO_CONFIG_CONTENT = ("raw patch input: the lane parses whole documents, and a "
                     "fragment would not parse at all")


def _config_lane(files: list[FilePatch], repo: str | None,
                 head_sha: str | None, read: FileReader | None = None) -> tuple[list[VerifiedFinding], dict]:
    """Deterministic rules over the config files no linter reads.

    Same accounting rule as the ruff lane: the denominator is the file set the
    orchestrator handed over, never what the lane decided to look at. A file that
    would not parse is an error, because "unreadable" and "clean" are different
    claims and only one of them is evidence.
    """
    from .jarvis import config_lane

    record: dict = {"lane": config_lane.LANE, "files_changed": len(files)}
    candidates = config_lane.eligible(files)
    record["files_eligible"] = len(candidates)
    if not candidates:
        return [], {**record, "status": verdict.LANE_SKIPPED, "reason": NO_YAML,
                    "files_analyzed": 0}
    if read is None and not (repo and head_sha):
        return [], {**record, "status": verdict.LANE_SKIPPED,
                    "reason": NO_CONFIG_CONTENT, "files_analyzed": 0}

    if read is None:
        read = _file_reader(repo, head_sha)

    found: list[VerifiedFinding] = []
    analysed = 0
    failures: list[str] = []
    for path in candidates:
        try:
            content = read(path)
            if content is None:
                continue
            found.extend(config_lane.to_finding(hit)
                         for hit in config_lane.scan(path, content, read=read))
            analysed += 1
        except (github.GitHubRateLimited, github.GitHubDenied, github.GitHubAuthError):
            raise
        except Exception as error:
            # Per file, never per lane. A templated Helm or Ansible document does
            # not parse as YAML and shares the .yml suffix; letting the first one
            # abandon the loop discarded the findings already collected from the
            # compose file beside it, which is a partial failure destroying
            # evidence rather than reporting it.
            log.exception("config lane failed on %s@%s:%s", repo, head_sha, path)
            failures.append(f"{path}: {type(error).__name__}")
    if failures:
        return found, {**record, "status": verdict.LANE_ERROR,
                       "files_analyzed": analysed, "findings": len(found),
                       "reason": "; ".join(failures)[:200],
                       "tool": config_lane.tool_identity()}
    return found, {**record, "status": verdict.LANE_RAN, "files_analyzed": analysed,
                   "findings": len(found), "tool": config_lane.tool_identity()}


JUDGE_LANE = "judge"


def _judge_skipped(reason: str) -> dict:
    return {"lane": JUDGE_LANE, "status": verdict.LANE_SKIPPED, "reason": reason}


def _judge_lane(bug_items: list[VerifiedFinding], patches: dict[str, str],
                reviewer_model: str) -> dict:
    """Decorrelated second opinion on the surviving bug-class findings.

    The judge is chosen so its family is not the reviewer's: same-family models
    share error modes, so their agreement is an echo rather than corroboration,
    and the verdict document would publish that echo as `consensus`. Which family
    is disqualified therefore depends on the reviewer, and is resolved per review
    rather than pinned to a vendor.

    Agreement mints `consensus`, disagreement demotes to `disputed` (kept and
    down-weighted, never dropped). The lane only ever adds information: any
    failure leaves the finding `single_critic`.

    Returns its own coverage record. This lane ran dark across 2945 findings
    because nothing recorded its absence, so it now reports whether it ran, which
    judge answered, and why it did not.
    """
    from .jarvis import automark, judges

    if os.environ.get(JUDGE_LIVE_ENV) != "1":
        return _judge_skipped(f"{JUDGE_LIVE_ENV} is not 1: the judge is a metered model")
    if not bug_items:
        return {"lane": JUDGE_LANE, "status": verdict.LANE_RAN, "checks": 0,
                "note": "no bug-class finding survived to be judged"}
    try:
        spec = judges.judge_for(reviewer_model)
    except judges.NoDecorrelatedJudge as error:
        log.warning("judge lane unavailable: %s", error)
        return _judge_skipped(str(error))

    judged = failures = 0
    failure_causes = Counter()
    for item in bug_items:
        snippet = patches.get(item.finding.file) or ""
        if not snippet:
            continue
        try:
            data = automark.judge_finding(item.finding, snippet,
                                          reviewer_model=reviewer_model)
        except Exception as error:
            failures += 1
            cause = (f"http_{error.response.status_code}" if isinstance(error, httpx.HTTPStatusError)
                     else type(error).__name__)
            failure_causes[cause] += 1
            continue
        raw = str(data.get("verdict", "unsure")).lower()
        item.judge = JudgeOpinion(
            verdict=raw if raw in _JUDGE_VERDICTS else "unsure",
            confidence=max(0.0, min(1.0, float(data.get("confidence") or 0.0))),
            model=spec.model,
        )
        judged += 1
    if failures:
        causes = ", ".join(f"{cause} x{count}" for cause, count in failure_causes.items())
        log.warning("judge lane: %d/%d second opinions failed (%s); those findings stay "
                    "single_critic", failures, len(bug_items), causes)
    return {"lane": JUDGE_LANE, "status": verdict.LANE_RAN, "model": spec.model,
            "family": spec.family, "reviewer_family": judges.family_of(reviewer_model),
            "checks": judged, "eligible": len(bug_items), "failures": failures,
            "failure_causes": dict(failure_causes)}


def _suppress_known_fps(repo: str, pr: int, findings: list) -> list:
    """Drop operator-confirmed false positives before verification, best-effort.

    Lazy import to avoid the import cycle; a load failure keeps every finding
    rather than risking a swallowed real one.
    """
    from .jarvis import live

    try:
        kept, suppressed = live.suppress_known_fps(repo, findings)
    except Exception:
        log.exception("known-FP suppression failed for %s#%s; keeping all findings", repo, pr)
        return findings
    if suppressed:
        log.info("suppressed %d known-FP finding(s) for %s#%s", len(suppressed), repo, pr)
    return kept


def _intent_for_prompt(repo: str, pr: int, meta: dict | None = None) -> str | None:
    """The PR's recovered intent slot for the review prompt, best-effort.

    Imported lazily to avoid the review <- live <- corpus <- api.db <- review
    cycle. A Linear or GitHub failure degrades to no intent, never a failed review.
    """
    from .jarvis import live

    try:
        return live.intent_section(repo, pr, meta)
    except (github.GitHubRateLimited, github.GitHubDenied, github.GitHubAuthError):
        raise
    except Exception:
        log.exception("intent recovery failed for %s#%s; reviewing without it", repo, pr)
        return None


def _probe_lane(
    repo: str, pr: int, files: list[FilePatch] | None = None,
    head_sha: str | None = None, read: FileReader | None = None,
) -> tuple[list[VerifiedFinding], list[VerifiedFinding], dict]:
    """Deterministic probe findings, split into bug-class and nit, plus the lane's
    coverage record. Best-effort.

    The probe lane is an enhancement: a fetch or scan failure must never sink the
    core review, so any error degrades to no probe findings. The failure is still
    recorded, because a crashed lane and a lane that found nothing are different
    claims. Imported lazily to avoid the review <- live <- corpus <- api.db <-
    review import cycle.
    """
    from .jarvis import live

    try:
        scan = live.probe_scan(repo, pr, files, head_sha, read)
    except (github.GitHubRateLimited, github.GitHubDenied, github.GitHubAuthError):
        raise
    except Exception:
        log.exception("probe lane failed for %s#%s; continuing without it", repo, pr)
        return [], [], live.probe_error_record()
    verified = scan.findings
    bugs = [item for item in verified if item.finding.severity in BUG_SEVERITIES]
    nits = [item for item in verified if item.finding.severity not in BUG_SEVERITIES]
    return bugs, nits, scan.coverage


def log_grounding_shadow(repo: str, pr: int, verified: list[VerifiedFinding]) -> None:
    """Shadow-mode grounding gate: report, do not enforce.

    The trust win Argus is aiming for is 'never post an ungrounded critical/major'.
    Before that gate can flip on, we need to see how much it would drop. This logs
    the would_emit / would_drop split for bug-class findings (a rule_id that
    resolves in the index = grounded) without removing anything. Enforcement is a
    later, operator-judged flip; here it is pure measurement.
    """
    bug_class = [
        item for item in verified
        if item.verdict.is_real and item.finding.severity in BUG_SEVERITIES
    ]
    if not bug_class:
        return
    would_emit = sum(1 for item in bug_class if rules.is_grounded(item.finding.rule_id))
    would_drop = len(bug_class) - would_emit
    log.info(
        "grounding-shadow %s#%s: bug-class=%d would_emit=%d would_drop(ungrounded)=%d",
        repo, pr, len(bug_class), would_emit, would_drop,
    )


def fetch_house_rules(repo: str) -> str | None:
    # Best-effort enrichment: a missing CLAUDE.md (404), a transient error, or an
    # unexpected payload (a directory path returns a JSON list, not an object)
    # just means we review without house rules. Auth errors are NOT swallowed:
    # github.get raises GitHubAuthError on 401/403, so an under-scoped or expired
    # token fails the review loudly instead of silently degrading every review.
    response = github.get(f"/repos/{repo}/contents/CLAUDE.md")
    if response.status_code != 200:
        return None
    data = response.json()
    content = data.get("content") if isinstance(data, dict) else None
    if not content:
        return None
    return base64.b64decode(content).decode("utf-8", "replace")[:HOUSE_RULES_MAX_CHARS]


def rank_and_cap(verified: list[VerifiedFinding], bug_cap: int = DEFAULT_BUG_CAP) -> tuple[list, list]:
    real = [item for item in verified if item.verdict.is_real]
    real.sort(key=lambda item: (_SEVERITY_RANK[item.finding.severity], -item.finding.confidence))
    bugs = [item for item in real if item.finding.severity in BUG_SEVERITIES]
    nits = [item for item in real if item.finding.severity not in BUG_SEVERITIES]
    return bugs[:bug_cap], nits


def _deep_review(diff: str, house_rules: str | None, model: str,
                 intent_section: str | None = None, pr_section: str = "") -> Review:
    sections = [_rule_index_section()]
    if house_rules:
        sections.append(f"# Repository house rules (cite, never invent)\n{house_rules}")
    if intent_section:
        sections.append(intent_section)
    if pr_section:
        sections.append(pr_section)
    sections.append(f"# Changed files\n{diff}")
    user = "\n\n".join(sections)
    data = complete_tool(model, REVIEW_SYSTEM, user + "\n\n"
                         "Review the changes above and report every defect via report_findings.",
                         REVIEW_TOOL)
    findings, stats = parse_findings(data)
    passes_run = 1
    findings_per_pass = [len(findings)]
    duplicates_dropped = 0
    later_pass_errors = 0
    for pass_number in range(2, REVIEW_PASSES + 1):
        listed = "\n".join(
            f"- {f.file}:{f.line} [{f.severity.value}] {f.title}" for f in findings) or "- none"
        gap_section = (
            f"# Findings already reported by a first reviewer\n{listed}\n\n"
            "Review the change again and report ONLY defects that are not already listed above. "
            "Be especially thorough on the classes reviewers most often miss: tests that do not "
            "exercise or assert the changed behaviour, docs, comments, changelogs or descriptions "
            "that contradict the code, unknown or invalid input and flags accepted silently, "
            "failures recorded as success, missing timeouts, and boundary conditions. Report "
            "nothing rather than restating a listed finding. Report all findings in one "
            "report_findings call."
        )
        passes_run += 1
        try:
            data = complete_tool(model, REVIEW_SYSTEM, user + "\n\n" + gap_section, REVIEW_TOOL)
            later, pass_stats = parse_findings(data)
        except Exception as exc:
            # A coverage attempt must not discard an already usable review.
            log.warning("review pass %d failed; keeping earlier findings: %s", pass_number, exc)
            later_pass_errors += 1
            continue
        findings_per_pass.append(len(later))
        for key, value in pass_stats.items():
            stats[key] += value
        for finding in later:
            if any(_near_duplicate(finding, kept) for kept in findings):
                duplicates_dropped += 1
            else:
                findings.append(finding)
    if stats["findings_repaired"] or stats["findings_dropped"]:
        log.warning("review findings: received=%d repaired=%d dropped=%d",
                    stats["findings_received"], stats["findings_repaired"],
                    stats["findings_dropped"])
    return Review(findings=findings, parsing_stats={
        **stats, "passes_run": passes_run, "findings_per_pass": findings_per_pass,
        "duplicates_dropped": duplicates_dropped, "later_pass_errors": later_pass_errors,
    })


def _near_duplicate(finding: Finding, kept: Finding) -> bool:
    if finding.file != kept.file or abs(finding.line - kept.line) > 3:
        return False
    title = set(re.findall(r"\w+", finding.title.lower()))
    kept_title = set(re.findall(r"\w+", kept.title.lower()))
    union = title | kept_title
    if union and len(title & kept_title) / len(union) >= 0.5:
        return True
    evidence = finding.evidence.splitlines()
    kept_evidence = kept.evidence.splitlines()
    return bool(evidence and kept_evidence and evidence[0].strip()
                and evidence[0].strip() == kept_evidence[0].strip())


def _rule_index_section() -> str:
    """The rule vocabulary the model may cite, as `id: summary` lines.

    Injected so a finding's rule_id is an exact id from a real index, not a guess.
    This is what makes the shadow grounding gate measure something real instead of
    the model's invented labels.
    """
    lines = [f"- {rid}: {summary}" for rid, summary in sorted(rules.index().items())]
    return (
        "# Rule index (cite rule_id from this list when a finding matches a rule; "
        "use the exact id, never invent one)\n" + "\n".join(lines)
    )


# The model counts lines from hunk headers and is sometimes a line or two off. The quote
# must still appear verbatim near the cited lines, so a small drift does not void it.
COUNTER_EVIDENCE_LINE_TOLERANCE = 2
# A cited range wider than this is not a place: any short quote would sit somewhere inside it.
COUNTER_EVIDENCE_MAX_LINES = 30
# Why a refutation was voided. "unreadable" is an infrastructure cause (no reader, no file,
# a failing read), so a rate of voids can be split from the model quoting wrongly.
VOID_UNREADABLE = "unreadable"
VOID_UNCONFIRMED = "unconfirmed"


@dataclass(frozen=True)
class _Void:
    kind: str
    reason: str


def _squash_whitespace(text: str) -> str:
    return " ".join(text.split())


def _strip_diff_markers(quote: str) -> str:
    """Drop one leading "+" or " " per line when the model copied the unified diff.

    A "-" line shows code the head no longer holds, so it is left in and fails the match.
    """
    lines = [line for line in quote.split("\n") if line.strip()]
    if lines and all(line[0] in "+ " for line in lines):
        return "\n".join(line[1:] for line in quote.split("\n"))
    return quote


def _unconfirmed(evidence: CounterEvidence | None, read: FileReader | None) -> _Void | None:
    """Why a refutation's quote cannot be found at the PR head, or None when it is found."""
    if evidence is None:
        return _Void(VOID_UNCONFIRMED, "the refutation quoted no counter_evidence")
    quote = _squash_whitespace(_strip_diff_markers(evidence.quote))
    if len(quote.replace(" ", "")) < MIN_QUOTE_CHARS:
        return _Void(VOID_UNCONFIRMED,
                     f"the quote has fewer than {MIN_QUOTE_CHARS} non-whitespace characters")
    if evidence.end_line - evidence.start_line + 1 > COUNTER_EVIDENCE_MAX_LINES:
        return _Void(VOID_UNCONFIRMED,
                     f"the cited range is wider than {COUNTER_EVIDENCE_MAX_LINES} lines")
    if read is None:
        return _Void(VOID_UNREADABLE, "no file reader was available to confirm the quote")
    try:
        content = read(evidence.path)
    except (github.GitHubRateLimited, github.GitHubDenied, github.GitHubAuthError):
        raise
    except Exception as error:
        log.warning("counter_evidence read failed for %s: %s", evidence.path, error)
        return _Void(VOID_UNREADABLE,
                     f"reading {evidence.path} at the PR head failed ({type(error).__name__})")
    if content is None:
        return _Void(VOID_UNREADABLE, f"{evidence.path} is not readable at the PR head")
    lines = content.split("\n")
    first = max(evidence.start_line - 1 - COUNTER_EVIDENCE_LINE_TOLERANCE, 0)
    cited = _squash_whitespace(" ".join(lines[first:evidence.end_line + COUNTER_EVIDENCE_LINE_TOLERANCE]))
    if quote in cited:
        return None
    return _Void(VOID_UNCONFIRMED, f"the quote is absent from {evidence.path} lines "
                                   f"{evidence.start_line}-{evidence.end_line} at the PR head")


def _void_unconfirmed_refutation(parsed: CounterEvidenceVerdict,
                                 read: FileReader | None) -> CounterEvidenceVerdict:
    """Turn a refutation whose quote is not at the PR head back into a kept finding.

    The model's confidence and reasoning stay on the record next to the reason.
    """
    if parsed.is_real:
        return parsed
    void = _unconfirmed(parsed.counter_evidence, read)
    if void is None:
        return parsed
    return parsed.model_copy(update={"is_real": True, "refutation_voided": void.reason,
                                     "refutation_void_kind": void.kind})


def _verify_findings(findings: list[Finding], diff: str, model: str, variant: str,
                     read: FileReader | None) -> list[VerifiedFinding]:
    # The finding text is left alone: eval judges and caches key on it, and the reason
    # for a voided refutation lives on the verdict.
    return [VerifiedFinding(finding=finding,
                            verdict=_verify(finding, diff, model, variant=variant, read=read))
            for finding in findings]


def _record_voided_refutations(llm_record: dict, variant: str,
                               verified: list[VerifiedFinding]) -> None:
    runs_check = issubclass(VERIFIER_VARIANTS[variant].verdict_type, CounterEvidenceVerdict)
    if not runs_check or llm_record["status"] == verdict.LANE_SKIPPED:
        return
    kinds = [getattr(item.verdict, "refutation_void_kind", None) for item in verified]
    llm_record["refutations_voided"] = sum(1 for kind in kinds if kind)
    llm_record["refutations_voided_unreadable"] = kinds.count(VOID_UNREADABLE)


def _parse_verdict(verdict_type: type[Verdict], data) -> Verdict:
    # The quote check owns these fields. A reply that sets them would otherwise forge a
    # reason, or a stale one, onto a standing refutation.
    if isinstance(data, dict) and issubclass(verdict_type, CounterEvidenceVerdict):
        data = {k: v for k, v in data.items() if k not in CounterEvidenceVerdict.SERVER_FIELDS}
    return verdict_type.model_validate(data)


def _cache_key(diff: str, model: str, name: str) -> str:
    digest = sha256(diff.encode("utf-8")).hexdigest()
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"argus-verify:{digest}:{model}:{name}"))


def _verify(finding, diff: str, model: str, *, variant: str | None = None,
            read: FileReader | None = None) -> Verdict:
    name = _resolve_variant(variant)
    chosen = VERIFIER_VARIANTS[name]
    user = chosen.user_prompt(finding, diff)
    key = _cache_key(diff, model, name)
    prefix = _verify_prefix(diff)
    with prompt_cache_scope(key, prefix):
        data = complete_tool(model, chosen.system, user, chosen.tool)
        try:
            parsed = _parse_verdict(chosen.verdict_type, data)
        except ValidationError:
            # A malformed verdict used to kill the whole review permanently (that
            # PR head was never re-reviewed). One fresh judge call usually yields a
            # valid payload; a second failure falls through to the error path.
            log.warning(
                "invalid verdict payload for %s:%s; retrying judge once",
                finding.file, finding.line,
            )
            data = complete_tool(model, chosen.system, user, chosen.tool)
            parsed = _parse_verdict(chosen.verdict_type, data)
    if isinstance(parsed, CounterEvidenceVerdict):
        parsed = _void_unconfirmed_refutation(parsed, read)
    return parsed
