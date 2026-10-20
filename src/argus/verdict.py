"""The machine-facing verdict document and its evidence-tier vocabulary.

This is the contract downstream trust systems consume (a merge gate's risk fold, a
tier evidence policy, a grounding-prior table): `deterministic` maps to
executable-grade evidence, `consensus` to cross-family reviewer+judge agreement,
`single_critic` to one model family's opinion, `disputed` to reviewer/judge
disagreement (kept but down-weighted by consumers). Tiers are derived at read
time from the persisted lane + judge fields so the mapping can evolve without a
migration.

Coverage is the second half of that contract. A finding count answers "what did
you find"; it cannot answer "did you look". Those are different claims, and a
document that reports only the first lets a caller read silence as a pass. So
every run records which lanes ran and which were skipped, the level is derived
from those records, and a clean result only becomes `approve` when the coverage
supports it. Findings work the other way round and are unconditional: evidence
found is evidence found, so a probe hit requests changes however thin the lane set.
"""

import json

TIER_DETERMINISTIC = "deterministic"
TIER_CONSENSUS = "consensus"
TIER_SINGLE_CRITIC = "single_critic"
TIER_DISPUTED = "disputed"

BASELINE_NEW = "new"
BASELINE_UNCHANGED = "unchanged"

# How much of the pipeline actually executed against this change.
COVERAGE_FULL = "full"          # the reviewer lane ran: a clean result means something
COVERAGE_PARTIAL = "partial"    # checks ran, but nothing read the code
COVERAGE_NONE = "none"          # nothing executed against this change
COVERAGE_UNKNOWN = "unknown"    # run predates coverage recording

# The decision for a clean result the coverage cannot support. Deliberately not a
# member of the approve/comment/changes_requested set: a consumer switching on the
# old three values fails closed on this one instead of reading it as a pass.
INSUFFICIENT_EVIDENCE = "insufficient_evidence"

LANE_LLM = "llm"
LANE_RAN = "ran"
LANE_SKIPPED = "skipped"
LANE_ERROR = "error"

_BUG_SEVERITIES = ("critical", "major")


# Lanes whose findings are re-executable: same bytes, same pinned tool, same
# answer, checkable by anyone. That reproducibility is what earns the top tier,
# not the fact that a tool rather than a model produced the finding.
DETERMINISTIC_LANES = ("probe", "ruff", "config")


def evidence_tier(lane: str | None, judge_verdict: str | None) -> str:
    if lane in DETERMINISTIC_LANES:
        return TIER_DETERMINISTIC
    if judge_verdict == "real":
        return TIER_CONSENSUS
    if judge_verdict == "fp":
        return TIER_DISPUTED
    return TIER_SINGLE_CRITIC


def coverage_level(lanes: list[dict]) -> str:
    """Derive the level from the lane records so the two can never disagree.

    The reviewer lane is what makes a clean result meaningful: it is the only lane
    that reads code it was not told to look for. But it earns `full` only if it read
    the whole change. The diff is compressed to a token budget, and over-budget files
    reach the prompt as a names-only list, so a reviewer can run to completion having
    never seen the contents of a changed file. That is a partial read, not a full one.

    A deterministic lane that ran but executed zero checks (every probe filtered out
    by glob or by fidelity) is worth the same as no lane at all, so it does not lift
    the level off `none`.
    """
    # A lane that crashed is missing information, whichever lane it was. It cannot
    # lift the level, and it must stop a full claim: the reviewer may have read
    # everything, but a check we intended to run did not answer.
    broken = any(lane.get("status") == LANE_ERROR for lane in lanes)
    reviewed = [lane for lane in lanes
                if lane.get("lane") == LANE_LLM and lane.get("status") == LANE_RAN]
    if reviewed:
        omitted = sum(lane.get("files_omitted") or 0 for lane in reviewed)
        return COVERAGE_PARTIAL if (omitted or broken) else COVERAGE_FULL
    executed = sum(lane.get("checks") or 0
                   for lane in lanes if lane.get("status") == LANE_RAN)
    return COVERAGE_PARTIAL if executed else COVERAGE_NONE


def decision(findings: list[dict], coverage: dict | None = None) -> str:
    """The decision the engine would post given a finding mix and its coverage.

    Any confirmed bug -> changes_requested, any confirmed finding -> comment.
    `disputed` findings do not request changes on their own: the whole point of
    the second opinion is that a cross-family disagreement is weaker evidence.

    A finding-free run is where coverage decides. `full` (or an unrecorded legacy
    run) approves; anything less returns INSUFFICIENT_EVIDENCE, because nothing
    read the code and the caller must not spend that silence as a pass.
    """
    confirmed = [f for f in findings if f.get("verdict_is_real")]
    undisputed_bugs = [
        f for f in confirmed
        if f.get("is_bug") and evidence_tier(f.get("lane"), f.get("judge_verdict")) != TIER_DISPUTED
    ]
    if undisputed_bugs:
        return "changes_requested"
    if confirmed:
        return "comment"
    level = (coverage or {}).get("level", COVERAGE_UNKNOWN)
    if level in (COVERAGE_FULL, COVERAGE_UNKNOWN):
        return "approve"
    return INSUFFICIENT_EVIDENCE


STATUS_DONE = "done"


def coverage_of(review: dict) -> dict:
    """The run's coverage record, or an explicit `unknown` for rows that predate it.

    Never synthesises a level: a run that did not record its lanes is reported as
    unknown, which is the true statement, rather than assumed to be full.

    A run that did not complete is the one case decided here rather than read off
    the record, because there is no record to read: an error path never reaches
    save_result, so the row carries NULL coverage and would otherwise inherit the
    legacy grandfathering. The run's own status is better evidence than the absence
    it left behind — a review that crashed produced no information about the code,
    and a finding-free crash must never read as a clean bill of health.
    """
    status = review.get("status")
    if status and status != STATUS_DONE:
        reason = review.get("error_class") or review.get("error") or status
        return {"level": COVERAGE_NONE, "lanes": [
            {"lane": LANE_LLM, "status": "error",
             "reason": f"the run did not complete: {reason}"}]}
    raw = review.get("coverage_json")
    if not raw:
        return {"level": COVERAGE_UNKNOWN, "lanes": []}
    try:
        stored = json.loads(raw)
    except (TypeError, ValueError):
        return {"level": COVERAGE_UNKNOWN, "lanes": []}
    lanes = stored.get("lanes") or []
    return {"level": stored.get("level") or coverage_level(lanes), "lanes": lanes}


def document(review: dict, findings: list[dict], baseline: list[str] | None = None, *,
             unverified: list[dict] | None = None) -> dict:
    """Assemble the verdict document from persisted rows.

    `baseline` is the fingerprint set a prior review of the same change reported;
    each current finding is stamped new/unchanged against it and baseline entries
    no current finding carries come back under `resolved` — the fix loop's
    "did my change clear finding X" answer.
    """
    base = set(baseline or [])
    coverage = coverage_of(review)
    confirmed = [f for f in findings if f.get("verdict_is_real")]
    current_prints = {f["fingerprint"] for f in confirmed if f.get("fingerprint")}
    items = [_finding_item(f, base) for f in confirmed]
    items.sort(key=lambda f: (0 if f["severity"] in _BUG_SEVERITIES else 1, -f["confidence"]))
    return {
        "verdict": decision(findings, coverage),
        "review_id": review["id"],
        "coverage": coverage,
        "source": {
            "kind": review.get("source_kind") or "pr",
            "repo": review.get("repo") or None,
            "pr": review.get("pr") or None,
            "base_sha": review.get("base_sha"),
            "head_sha": review.get("head_sha"),
        },
        "engine": {
            "reviewer_model": _reviewer_model(review, coverage),
            "status": review.get("status"),
        },
        "counts": {
            "confirmed": len(items),
            "bugs": sum(1 for f in items if f["severity"] in _BUG_SEVERITIES),
            "by_tier": _tier_counts(items),
        },
        "findings": items,
        **({"unverified": [_finding_item(f, base) for f in unverified]} if unverified is not None else {}),
        "resolved": sorted(base - current_prints) if base else [],
    }


def _reviewer_model(review: dict, coverage: dict) -> str | None:
    """The model that actually read the diff, or None when none did.

    Stored configuration is intent; a lane record is provenance, and the two must
    never share a field. The row names a reviewer for every run, including runs
    that never call it, so only a lane that reached `ran` reports a model. A lane
    that was skipped, or that errored partway, completed no invocation and names
    nobody. Legacy rows fall back to the stored value: `unknown` coverage means
    unrecorded, not proven absent.
    """
    for lane in coverage.get("lanes") or []:
        if lane.get("lane") != LANE_LLM:
            continue
        if lane.get("status") == LANE_RAN:
            return lane.get("model") or review.get("model")
        return None
    return review.get("model")


def _finding_item(row: dict, baseline: set[str]) -> dict:
    print_ = row.get("fingerprint") or ""
    judge = None
    if row.get("judge_verdict"):
        judge = {
            "verdict": row["judge_verdict"],
            "confidence": row.get("judge_confidence"),
            "model": row.get("judge_model"),
        }
    return {
        "id": row.get("id"),
        "fingerprint": print_,
        "file": row["file"],
        "line": row["line"],
        "end_line": row.get("end_line"),
        "category": row.get("category"),
        "evidence": row.get("evidence") or "",
        "severity": row["severity"],
        "confidence": row["confidence"],
        "title": row["title"],
        "description": row["description"],
        "proposed_fix": row.get("proposed_fix", ""),
        "rule_id": row.get("rule_id"),
        "lane": row.get("lane") or "llm",
        "verifier_confidence": row.get("verdict_confidence"),
        "judge": judge,
        "evidence_tier": evidence_tier(row.get("lane"), row.get("judge_verdict")),
        "baseline_state": BASELINE_UNCHANGED if print_ and print_ in baseline else BASELINE_NEW,
    }


def _tier_counts(items: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for item in items:
        counts[item["evidence_tier"]] = counts.get(item["evidence_tier"], 0) + 1
    return counts
