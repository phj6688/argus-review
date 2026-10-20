"""Auto-marker: decorrelated ground-truth labels for Argus findings.

Hand-labelling 275+ findings is the bottleneck on a measurable precision/recall
number. This generates labels automatically with three tiers, first conclusive
tier wins per finding:

  1. probe  -- a deterministic incident probe (probes.INCIDENT_PROBES) fires on
     the finding's file near its line. Near-zero-FP and fully decorrelated from
     the Opus reviewer, so a hit is strong evidence the finding is real.
  2. locate -- fetch the file at PR head: gone -> cant_locate, line out of range
     -> stale. Deterministic, no model.
  3. judge  -- a NON-Anthropic model reads the real code around the line under an
     adversarial prompt and returns real/fp/unsure.

The judge MUST NOT be Claude/Opus/Anthropic: a same-model judge's errors
correlate with Argus's and would silently corrupt the corpus. _assert_non_anthropic
enforces this; the reachability probe and per-finding try/except keep one bad
finding from sinking the run (it degrades to unsure -> needs-human, never crashes).

Auto-marks are written with marker 'automark:<tier>' so human-only precision stays
computable (metrics.mark_precision and corpus.load_marks exclude them by default).
Nothing is written unless --commit is passed; the default is a dry-run.
"""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ..client import llm_slot

from . import judges, live, probes

ENV_API_URL = "ARGUS_AUTOMARK_API_URL"
ENV_JUDGE_URL = "ARGUS_AUTOMARK_JUDGE_URL"
ENV_JUDGE_MODEL = "ARGUS_AUTOMARK_JUDGE_MODEL"
ENV_JUDGE_KEY = "ARGUS_AUTOMARK_JUDGE_KEY"  # Bearer token for hosted judges (OpenRouter)

# The judge tier is disabled until its endpoint and model are configured.
DEFAULT_API_URL = "http://127.0.0.1:8350"
JUDGE_DISABLED = f"automark judge disabled; set {ENV_JUDGE_URL} and {ENV_JUDGE_MODEL}"

DEFAULT_THRESHOLD = 0.75
# How close a probe hit must be to the finding's line to attribute it to that
# finding. A FileProbe reports the line of its `present` match, which can sit a
# little above where the reviewer flagged the same defect, so the window is
# generous; the probe + both lines are recorded in the note for human audit.
PROBE_LINE_WINDOW = 25
# Bound the default (no-filter) dry-run so it does not fan out over every review.
DEFAULT_RECENT_REVIEWS = 25
JUDGE_MAX_TOKENS = 2048  # GLM-5.2 et al. are reasoning models; leave room for thinking + the JSON
# Generous so the judge sees the whole flagged function/caller, not a keyhole; an
# "insufficient context" finding otherwise collapses to a wrong fp instead of unsure.
JUDGE_SNIPPET_RADIUS = 50
JUDGE_TIMEOUT = httpx.Timeout(60.0, connect=5.0)
_NOTE_MAX = 2000

MARKER_PROBE = "automark:probe"
MARKER_LOCATE = "automark:locate"
MARKER_JUDGE = "automark:judge"
AUTOMARK_MARKER_PREFIX = "automark:"

# The judge gate: only a confident real/fp from tier 3 is writable. Deterministic
# tiers (probe/locate) carry confidence 1.0 and are always writable.
_WRITABLE_JUDGE_MARKS = ("real", "fp")
_VERDICT_TO_MARK = {"real": "real", "fp": "fp", "unsure": "unsure"}
_ANTHROPIC_TOKENS = ("anthropic", "claude", "opus", "sonnet", "haiku")

JUDGE_SYSTEM = (
    "You are a strict, adversarial code-review judge. Default to NOT a real bug. "
    "Only answer real if you can quote the specific code that makes it a genuine "
    "defect; if the snippet is insufficient, answer unsure. Respond with ONLY a "
    'JSON object: {"verdict":"real|fp|unsure","confidence":0..1,"evidence":"..."}. '
    "Keep evidence to one short sentence."
)


@dataclass(frozen=True)
class Finding:
    """One Argus finding to label, plus whether it is already marked."""

    finding_id: int
    repo: str
    pr: int
    file: str
    line: int
    severity: str
    title: str
    description: str
    has_mark: bool


@dataclass(frozen=True)
class Decision:
    """The tier outcome for one finding. writable = eligible to persist."""

    finding_id: int
    mark: str
    marker: str
    provenance: str
    confidence: float
    evidence: str
    tier: str
    writable: bool


@dataclass
class RunReport:
    commit: bool
    threshold: float
    judge_ok: bool
    judge_note: str
    scanned: int = 0
    marked: list = field(default_factory=list)        # written (commit only)
    would_mark: list = field(default_factory=list)    # writable, dry-run
    needs_human: list = field(default_factory=list)   # not writable
    skipped: list = field(default_factory=list)       # (Finding, reason)


# --------------------------------------------------------------------------
# Decorrelation guard
# --------------------------------------------------------------------------

def _assert_non_anthropic(url: str, model: str) -> None:
    """Refuse an Anthropic judge, for the nightly corpus lane only.

    That lane labels findings of mixed provenance, so there is no single reviewer
    to be relative to and the conservative absolute rule is the right one. Live
    review knows its reviewer and uses _assert_decorrelated instead.
    """
    blob = f"{url} {model}".lower()
    hit = next((tok for tok in _ANTHROPIC_TOKENS if tok in blob), None)
    if hit is not None:
        raise ValueError(
            f"automark judge must be NON-Anthropic (found {hit!r} in url/model). "
            "A same-model judge silently corrupts the corpus; point "
            f"{ENV_JUDGE_URL}/{ENV_JUDGE_MODEL} at a non-Claude model."
        )


def _assert_decorrelated(reviewer_model: str, judge_model: str) -> None:
    """Refuse a judge that shares the reviewer's family.

    Same-family models share error modes, so their agreement is an echo rather
    than a second opinion, and the verdict document would publish that echo as
    `consensus` evidence. Which family is forbidden depends on the reviewer, so
    this is checked per review rather than pinned to a vendor.
    """
    reviewer_family = judges.family_of(reviewer_model)
    judge_family = judges.family_of(judge_model)
    if reviewer_family is judges.UNKNOWN:
        raise judges.NoDecorrelatedJudge(
            f"reviewer model {reviewer_model!r} has an unknown family, so no judge "
            "can be shown to differ from it"
        )
    if judge_family is judges.UNKNOWN:
        raise ValueError(
            f"judge model {judge_model!r} has an unknown family, so it cannot be "
            "shown to differ from the reviewer; an unproven decorrelation must not "
            "be published as consensus"
        )
    if reviewer_family == judge_family:
        raise ValueError(
            f"judge {judge_model!r} is the same family ({judge_family}) as reviewer "
            f"{reviewer_model!r}; a same-family judge is not a decorrelated one"
        )


# --------------------------------------------------------------------------
# Tier classification (pure: no IO; deps are passed in)
# --------------------------------------------------------------------------

def classify(finding: Finding, content: str | None, head: str, *,
             judge, threshold: float = DEFAULT_THRESHOLD,
             window: int = PROBE_LINE_WINDOW) -> Decision:
    """Resolve one finding to a Decision. Any error degrades it to unsure (the
    judge tier's no-op), so a single bad finding never crashes the run."""
    try:
        hit = _probe_hit(finding.file, content, finding.line, window) if content else None
        if hit is not None:
            return _decision(
                finding, "real", MARKER_PROBE, "auto:probe", 1.0,
                f"probe {hit.probe_id} fired at line {hit.line} (finding line "
                f"{finding.line}): {hit.text.strip()}", "probe", writable=True)

        if content is None:
            return _decision(
                finding, "cant_locate", MARKER_LOCATE, "auto:locate", 1.0,
                f"{finding.file} not found at head {head[:12]}", "locate", writable=True)

        line_count = len(content.splitlines())
        if finding.line < 1 or finding.line > line_count:
            return _decision(
                finding, "stale", MARKER_LOCATE, "auto:locate", 1.0,
                f"line {finding.line} out of range (file has {line_count} lines at "
                f"head {head[:12]})", "locate", writable=True)

        verdict = judge(_judge_payload(finding, content))
        mark, confidence, evidence = _coerce_verdict(verdict)
        writable = mark in _WRITABLE_JUDGE_MARKS and confidence >= threshold
        return _decision(finding, mark, MARKER_JUDGE, "auto:judge", confidence,
                         evidence, "judge", writable=writable)
    except Exception as exc:  # noqa: BLE001 - any tier failure degrades to unsure
        return _decision(finding, "unsure", MARKER_JUDGE, "auto:error", 0.0,
                         f"{type(exc).__name__}: {exc}", "judge", writable=False)


def _decision(finding: Finding, mark: str, marker: str, provenance: str,
              confidence: float, evidence: str, tier: str, *, writable: bool) -> Decision:
    return Decision(finding.finding_id, mark, marker, provenance,
                    round(float(confidence), 3), evidence[:500], tier, writable)


def _probe_hit(file: str, content: str, finding_line: int, window: int):
    """A probe hit on this file within `window` lines of the finding, or None.

    Reuses the live candidate prefilter so a file that cannot match any probe
    glob never pays the scan; the real whole-glob match still runs in scan_contents.
    """
    if not live._candidate_paths([file]):
        return None
    hits = probes.scan_contents(probes.INCIDENT_PROBES, {file: content})
    near = [h for h in hits if h.file == file and abs(h.line - finding_line) <= window]
    return near[0] if near else None


def _coerce_verdict(verdict: dict) -> tuple[str, float, str]:
    mark = _VERDICT_TO_MARK.get(str(verdict.get("verdict", "")).strip().lower(), "unsure")
    try:
        confidence = max(0.0, min(1.0, float(verdict.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    return mark, confidence, str(verdict.get("evidence", ""))[:500]


# --------------------------------------------------------------------------
# Default judge (OpenAI-compatible chat completions over httpx)
# --------------------------------------------------------------------------

def _judge_headers() -> dict:
    """Bearer auth for a hosted judge (OpenRouter); empty for a local llama-server."""
    key = os.environ.get(ENV_JUDGE_KEY)
    return {"Authorization": f"Bearer {key}"} if key else {}


def default_judge(payload: dict, *, url: str | None = None, model: str | None = None,
                  spec: "judges.JudgeSpec | None" = None,
                  reviewer_model: str | None = None) -> dict:
    """Call a judge model and parse its JSON verdict.

    Two callers, two decorrelation rules. Live review passes `spec` and
    `reviewer_model`, and the judge is checked against that reviewer's family. The
    nightly corpus lane passes neither, judges findings of mixed provenance, and
    keeps the conservative absolute rule.

    Raises on transport, parse or guard failure; the caller turns that into an
    unsure decision or a skipped lane.
    """
    if spec is not None:
        url, model, headers = spec.url, spec.model, {"Authorization": f"Bearer {spec.key}"}
        _assert_decorrelated(reviewer_model or "", model)
    else:
        url = (url if url is not None else os.environ.get(ENV_JUDGE_URL, "")).strip()
        model = (model if model is not None else os.environ.get(ENV_JUDGE_MODEL, "")).strip()
        if not url or not model:
            raise RuntimeError(JUDGE_DISABLED)
        _assert_non_anthropic(url, model)
        headers = _judge_headers()
    with llm_slot():
        response = httpx.post(url, timeout=JUDGE_TIMEOUT, headers=headers, json={
            "model": model,
            "temperature": 0,
            "max_tokens": JUDGE_MAX_TOKENS,
            "messages": [
                {"role": "system", "content": JUDGE_SYSTEM},
                {"role": "user", "content": _judge_user(payload)},
            ],
        })
    response.raise_for_status()
    # A model can return null content (refusal / empty choice); coerce to "" so the
    # parser raises a clean "no verdict" ValueError (-> unsure) instead of a TypeError.
    content = response.json()["choices"][0]["message"].get("content") or ""
    return _parse_verdict(content)


def _judge_payload(finding: Finding, content: str) -> dict:
    return {
        "title": finding.title,
        "description": finding.description,
        "file": finding.file,
        "line": finding.line,
        "severity": finding.severity,
        "snippet": _snippet(content, finding.line),
    }


def judge_finding(finding: Finding, snippet: str,
                  reviewer_model: str | None = None) -> dict:
    """Live second-opinion entry point: judge one finding against a caller-built
    code view (full file snippet or patch text).

    `reviewer_model` selects the judge: the configured judge whose family is not
    the reviewer's. Without it the call falls back to the nightly lane's absolute
    rule, which keeps existing callers working. Raises on transport, parse or
    decorrelation failure so the caller decides the degradation.
    """
    payload = {
        "title": finding.title,
        "description": finding.description,
        "file": finding.file,
        "line": finding.line,
        "severity": finding.severity,
        "snippet": snippet,
    }
    if reviewer_model is None:
        return default_judge(payload)
    spec = judges.judge_for(reviewer_model)
    return default_judge(payload, spec=spec, reviewer_model=reviewer_model)


def _judge_user(payload: dict) -> str:
    return (
        "A code reviewer reported this finding. Decide whether it is a REAL defect.\n\n"
        f"Title: {payload['title']}\n"
        f"Severity: {payload['severity']}\n"
        f"Location: {payload['file']}:{payload['line']}\n"
        f"Description: {payload['description']}\n\n"
        f"Actual code (the flagged line is marked >>):\n{payload['snippet']}\n\n"
        "Answer real only if you can quote the specific code that makes this a genuine "
        "defect. If the relevant function or its callers are not fully visible here, you "
        "MUST answer unsure (never fp on missing context). Answer fp only when the visible "
        "code positively shows the finding is wrong. Respond with ONLY the JSON object."
    )


def _snippet(content: str, line: int, radius: int = JUDGE_SNIPPET_RADIUS) -> str:
    lines = content.splitlines()
    start = max(0, line - 1 - radius)
    end = min(len(lines), line - 1 + radius + 1)
    out = []
    for number in range(start, end):
        marker = ">>" if number == line - 1 else "  "
        out.append(f"{marker} {number + 1}: {lines[number]}")
    return "\n".join(out)


_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)
_VERDICT_RE = re.compile(r'"verdict"\s*:\s*"(real|fp|unsure)"', re.IGNORECASE)
_CONFIDENCE_RE = re.compile(r'"confidence"\s*:\s*([01](?:\.\d+)?|\.\d+)')
_EVIDENCE_RE = re.compile(r'"evidence"\s*:\s*"([^"]*)"')


def _parse_verdict(content: str) -> dict:
    """Extract the JSON verdict from a model reply, tolerating ```json fences and
    surrounding prose (medgemma/llama-server wrap their JSON in a code block).

    Falls back to field-level salvage when the object is truncated (the model hit
    max_tokens mid-string): verdict and confidence lead the object, so the
    load-bearing fields survive even when the closing brace does not.
    """
    match = _JSON_OBJECT.search(content)
    if match is not None:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    verdict = _VERDICT_RE.search(content)
    if verdict is None:
        raise ValueError(f"no verdict in judge reply: {content[:200]!r}")
    confidence = _CONFIDENCE_RE.search(content)
    evidence = _EVIDENCE_RE.search(content)
    return {
        "verdict": verdict.group(1).lower(),
        "confidence": float(confidence.group(1)) if confidence else 0.0,
        "evidence": evidence.group(1) if evidence else "",
    }


def judge_reachable(*, url: str | None = None) -> tuple[bool, str]:
    """Probe the judge's /v1/models so the run can degrade cleanly when it is down."""
    url = (url if url is not None else os.environ.get(ENV_JUDGE_URL, "")).strip()
    model = os.environ.get(ENV_JUDGE_MODEL, "").strip()
    if not url or not model:
        return False, JUDGE_DISABLED
    try:
        _assert_non_anthropic(url, model)
        models_url = url.split("/v1/", 1)[0] + "/v1/models"
        response = httpx.get(models_url, headers=_judge_headers(),
                             timeout=httpx.Timeout(5.0, connect=4.0))
        ok = response.status_code == 200
        return ok, f"{models_url} -> {response.status_code} (model={model})"
    except Exception as exc:  # noqa: BLE001 - reachability is best-effort
        return False, f"{url} unreachable: {type(exc).__name__}: {exc}"


def _degraded_judge(_payload: dict) -> dict:
    raise RuntimeError("judge endpoint unreachable; finding left for human review")


# --------------------------------------------------------------------------
# Finding source (live Argus API) + mark writer (local db)
# --------------------------------------------------------------------------

def list_findings_via_api(*, repo: str | None = None, pr: int | None = None,
                          since: int | None = None, base_url: str | None = None) -> list[Finding]:
    """Read findings the way the spec mandates: GET /reviews then /reviews/{id}.

    repo/pr/since narrow the scan; with no filter the most recent reviews are used
    so a bare dry-run stays bounded. since is an approximate day window derived
    from the API's coarse age label.
    """
    base_url = (base_url or os.environ.get(ENV_API_URL, DEFAULT_API_URL)).rstrip("/")
    with httpx.Client(base_url=base_url, timeout=httpx.Timeout(20.0, connect=5.0)) as client:
        summaries = client.get("/reviews").json().get("reviews", [])
        chosen = _select_reviews(summaries, repo=repo, pr=pr, since=since)
        findings: list[Finding] = []
        for summary in chosen:
            detail = client.get(f"/reviews/{summary['id']}").json()
            findings.extend(_findings_from_detail(detail))
    return findings


def _select_reviews(summaries: list[dict], *, repo, pr, since) -> list[dict]:
    chosen = [
        row for row in summaries
        if (repo is None or row.get("repo") == repo)
        and (pr is None or row.get("pr") == pr)
        and (since is None or _age_to_days(row.get("age")) <= since)
    ]
    if repo is None and pr is None and since is None:
        return chosen[:DEFAULT_RECENT_REVIEWS]  # summaries are newest-first
    return chosen


def _findings_from_detail(detail: dict) -> list[Finding]:
    out = []
    for item in detail.get("bugs", []) + detail.get("nits", []):
        finding = item.get("finding", {})
        out.append(Finding(
            finding_id=item["id"],
            repo=detail["repo"],
            pr=detail["pr"],
            file=finding.get("file", ""),
            line=int(finding.get("line", 0)),
            severity=finding.get("severity", ""),
            title=finding.get("title", ""),
            description=finding.get("description", ""),
            has_mark=item.get("mark") is not None,
        ))
    return out


_AGE_UNIT_DAYS = {"m": 1 / 1440, "h": 1 / 24, "d": 1.0}


def _age_to_days(age: str | None) -> float:
    match = re.fullmatch(r"(\d+)([mhd])", (age or "").strip())
    if match is None:
        return float("inf")  # unknown age never passes a since filter
    return int(match.group(1)) * _AGE_UNIT_DAYS[match.group(2)]


def db_write_mark(finding_id: int, mark: str, *, marker: str, note: str,
                  base_url: str | None = None) -> int:
    """Persist a mark via the live Argus API: the container owns the prod db, so a
    host-side db.add_mark would write to an empty db (no findings -> 404). Provenance
    rides in `marker` so the gold corpus / human precision can exclude auto-marks."""
    base = (base_url or os.environ.get(ENV_API_URL, DEFAULT_API_URL)).rstrip("/")
    response = httpx.post(
        f"{base}/findings/{finding_id}/mark",
        timeout=httpx.Timeout(15.0, connect=5.0),
        json={"mark": mark, "marker": marker, "note": note},
    )
    response.raise_for_status()
    return int(response.json().get("id", 0))


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------

def run(*, repo: str | None = None, pr: int | None = None, since: int | None = None,
        threshold: float = DEFAULT_THRESHOLD, commit: bool = False,
        list_findings=list_findings_via_api, fetch_file=live._fetch_file_at,
        head_sha_for=live._pr_head_sha, judge=default_judge, write_mark=db_write_mark,
        judge_ok: bool = True, judge_note: str = "") -> RunReport:
    """Classify each finding through the tiers and (with commit) write the eligible
    marks. Per-finding IO failures are skipped, never raised. An already-marked
    finding is skipped untouched: auto-marks never overwrite a human or prior label."""
    report = RunReport(commit=commit, threshold=threshold, judge_ok=judge_ok, judge_note=judge_note)
    effective_judge = judge if judge_ok else _degraded_judge
    head_cache: dict[tuple, str] = {}
    content_cache: dict[tuple, str | None] = {}

    for finding in list_findings(repo=repo, pr=pr, since=since):
        report.scanned += 1
        if finding.has_mark:
            report.skipped.append((finding, "already marked"))
            continue

        content = _resolve_content(finding, head_cache, content_cache, fetch_file, head_sha_for)
        if isinstance(content, _FetchError):
            report.skipped.append((finding, content.reason))
            continue

        decision = classify(finding, content, head_cache[(finding.repo, finding.pr)],
                            judge=effective_judge, threshold=threshold)
        _record(report, finding, decision, commit, write_mark)
    return report


@dataclass(frozen=True)
class _FetchError:
    reason: str


def _resolve_content(finding, head_cache, content_cache, fetch_file, head_sha_for):
    pr_key = (finding.repo, finding.pr)
    try:
        if pr_key not in head_cache:
            head_cache[pr_key] = head_sha_for(finding.repo, finding.pr)
    except Exception as exc:  # noqa: BLE001 - degrade this finding, not the run
        return _FetchError(f"head fetch failed: {type(exc).__name__}")
    file_key = (finding.repo, finding.pr, finding.file)
    try:
        if file_key not in content_cache:
            content_cache[file_key] = fetch_file(finding.repo, finding.file, head_cache[pr_key])
    except Exception as exc:  # noqa: BLE001 - degrade this finding, not the run
        return _FetchError(f"file fetch failed: {type(exc).__name__}")
    return content_cache[file_key]


def _record(report: RunReport, finding: Finding, decision: Decision, commit: bool, write_mark) -> None:
    if not decision.writable:
        report.needs_human.append(decision)
        return
    if not commit:
        report.would_mark.append(decision)
        return
    try:
        write_mark(finding.finding_id, decision.mark, marker=decision.marker, note=_note(decision))
        report.marked.append(decision)
    except Exception as exc:  # noqa: BLE001 - a write failure is a skip, never a crash
        report.skipped.append((finding, f"write failed: {type(exc).__name__}"))


def _note(decision: Decision) -> str:
    return (f"{decision.provenance} verdict={decision.mark} "
            f"conf={decision.confidence:.2f} — {decision.evidence}")[:_NOTE_MAX]


# --------------------------------------------------------------------------
# Output: human report + needs-human queue
# --------------------------------------------------------------------------

def _default_queue_path() -> Path:
    return Path(__file__).resolve().parents[3] / "data" / "automark_needs_human.jsonl"


def write_needs_human_queue(report: RunReport, path: str | None = None) -> str:
    target = Path(path) if path else _default_queue_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        for decision in report.needs_human:
            handle.write(json.dumps({
                "finding_id": decision.finding_id, "tier": decision.tier,
                "mark": decision.mark, "confidence": decision.confidence,
                "evidence": decision.evidence,
            }) + "\n")
    return str(target)


def render_report(report: RunReport) -> str:
    mode = "COMMIT" if report.commit else "DRY-RUN"
    judge = "reachable" if report.judge_ok else f"DEGRADED ({report.judge_note})"
    lines = [
        f"automark {mode} — threshold {report.threshold} — judge {judge}",
        f"scanned {report.scanned} finding(s): "
        f"{len(report.marked)} marked, {len(report.would_mark)} would-mark, "
        f"{len(report.needs_human)} needs-human, {len(report.skipped)} skipped",
    ]
    writable = report.marked or report.would_mark
    if writable:
        verb = "MARKED" if report.commit else "WOULD MARK"
        lines.append(f"\n{verb}:")
        lines += [f"  #{d.finding_id} {d.mark} [{d.tier}] conf={d.confidence:.2f} "
                  f"— {d.evidence}" for d in writable]
    if report.needs_human:
        lines.append("\nNEEDS HUMAN:")
        lines += [f"  #{d.finding_id} {d.mark} [{d.tier}] conf={d.confidence:.2f} "
                  f"— {d.evidence}" for d in report.needs_human]
    if report.skipped:
        lines.append("\nSKIPPED:")
        lines += [f"  #{f.finding_id} — {reason}" for f, reason in report.skipped]
    return "\n".join(lines)


def run_cli(*, repo, pr, since, threshold, commit) -> RunReport:
    """Wire the live judge/API/db and run, then persist + print. Returns the report."""
    judge_ok, judge_note = judge_reachable()
    report = run(repo=repo, pr=pr, since=since, threshold=threshold, commit=commit,
                 judge_ok=judge_ok, judge_note=judge_note)
    queue_path = write_needs_human_queue(report)
    print(render_report(report))
    print(f"\nneeds-human queue: {queue_path}")
    return report
