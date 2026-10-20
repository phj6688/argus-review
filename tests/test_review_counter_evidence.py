"""Refutations whose quoted counter-evidence is absent at the PR head are void.

The check runs only for verifier variants whose verdict type carries counter_evidence.
The shipped variant never reads a file, so its prompts and results stay as they were.
"""

import json

import pytest

from argus import review
from argus.diff import FilePatch
from argus.evalkit.replay import serialize_finding
from argus.jarvis import live
from argus.schema import CounterEvidence, CounterEvidenceVerdict, Finding, Verdict

HANDLER = "src/app/handler.py"
OTHER = "src/app/other.py"
GUARD = "resp = requests.get(url, timeout=10)"
QUOTE_LINE = 40

FINDING = Finding(
    file=HANDLER, line=42, end_line=44, severity="major", confidence=0.8,
    title="Timeout missing", description="requests.get has no timeout.",
    proposed_fix="Pass timeout=10.", rule_id=None, category="resources",
    evidence="resp = requests.get(url)",
)
SECOND = Finding(
    file=OTHER, line=7, end_line=7, severity="major", confidence=0.8,
    title="Retry loop never ends", description="The loop has no exit.",
    proposed_fix="Bound the loop.", rule_id=None, category="correctness",
    evidence="while True:",
)
DIFF = "## File: src/app/handler.py (python)\n```diff\n@@ -40 +40 @@\n+resp = requests.get(url)\n```"


def _head_text(guard_line: int = QUOTE_LINE, guard: str = f"    {GUARD}") -> str:
    lines = [f"# line {number}" for number in range(1, 61)]
    lines[guard_line - 1] = guard
    return "\n".join(lines) + "\n"


def _reader(files: dict[str, str | None]):
    reads: list[str] = []

    def read(path: str) -> str | None:
        reads.append(path)
        return files.get(path)

    read.reads = reads
    return read


def _refute(quote: str = GUARD, *, path: str = HANDLER, start: int = QUOTE_LINE,
            end: int | None = None) -> dict:
    return {"is_real": False, "confidence": 0.93, "reasoning": "the guard exists",
            "counter_evidence": {"path": path, "start_line": start,
                                 "end_line": start if end is None else end, "quote": quote}}


def _verify(monkeypatch, reply, *, read, variant="default_keep", finding=FINDING):
    monkeypatch.setattr(review, "complete_tool", lambda *args: reply)
    return review._verify(finding, DIFF, "model-x", variant=variant, read=read)


# --- R1: the quote is compared with the head, whitespace normalized ---------

def test_quote_present_at_the_cited_line_lets_the_refutation_stand(monkeypatch):
    read = _reader({HANDLER: _head_text()})
    verdict = _verify(monkeypatch, _refute(), read=read)
    assert verdict.is_real is False
    assert verdict.refutation_voided is None
    assert verdict.counter_evidence == CounterEvidence(**_refute()["counter_evidence"])
    assert read.reads == [HANDLER]


@pytest.mark.parametrize("head_line,quote", [
    ("    resp   =\trequests.get(url,   timeout=10)", GUARD),
    (f"{GUARD}\r", GUARD),
    (f"        {GUARD}   ", f"  {GUARD}  "),
    (GUARD, "resp =\n    requests.get(url, timeout=10)"),
], ids=["inner-whitespace", "crlf", "outer-whitespace", "quote-wrapped"])
def test_whitespace_differences_do_not_void_a_refutation(monkeypatch, head_line, quote):
    read = _reader({HANDLER: _head_text(guard=head_line)})
    verdict = _verify(monkeypatch, _refute(quote), read=read)
    assert verdict.is_real is False and verdict.refutation_voided is None


def test_multi_line_quote_matches_across_lines(monkeypatch):
    text = _head_text(guard="    if timeout is None:\n        timeout = 10")
    read = _reader({HANDLER: text})
    quote = "if timeout is None: timeout = 10"
    verdict = _verify(monkeypatch, _refute(quote, start=QUOTE_LINE, end=QUOTE_LINE + 1), read=read)
    assert verdict.is_real is False and verdict.refutation_voided is None


def test_a_quote_may_cover_part_of_a_longer_cited_range(monkeypatch):
    read = _reader({HANDLER: _head_text()})
    verdict = _verify(monkeypatch, _refute(start=QUOTE_LINE - 3, end=QUOTE_LINE + 3), read=read)
    assert verdict.is_real is False and verdict.refutation_voided is None


def test_quote_within_the_line_tolerance_stands(monkeypatch):
    tolerance = review.COUNTER_EVIDENCE_LINE_TOLERANCE
    for cited in (QUOTE_LINE - tolerance, QUOTE_LINE + tolerance):
        read = _reader({HANDLER: _head_text()})
        verdict = _verify(monkeypatch, _refute(start=cited), read=read)
        assert verdict.is_real is False, cited


def test_quote_beyond_the_line_tolerance_is_void(monkeypatch):
    tolerance = review.COUNTER_EVIDENCE_LINE_TOLERANCE
    for cited in (QUOTE_LINE - tolerance - 1, QUOTE_LINE + tolerance + 1):
        read = _reader({HANDLER: _head_text()})
        verdict = _verify(monkeypatch, _refute(start=cited), read=read)
        assert verdict.is_real is True, cited
        assert verdict.refutation_voided


def test_the_line_tolerance_is_small():
    assert 0 <= review.COUNTER_EVIDENCE_LINE_TOLERANCE <= 3


# --- R2: a missing or absent quote voids the refutation ---------------------

def test_absent_quote_voids_the_refutation_and_keeps_the_finding(monkeypatch):
    read = _reader({HANDLER: _head_text()})
    reply = _refute("resp = requests.get(url, timeout=30)")
    verdict = _verify(monkeypatch, reply, read=read)
    assert verdict.is_real is True
    assert "absent" in verdict.refutation_voided
    assert verdict.counter_evidence is not None, "the rejected quote stays on the record"
    assert verdict.reasoning == "the guard exists"
    assert verdict.confidence == 0.93


def test_quote_from_a_removed_line_is_void(monkeypatch):
    # The line existed on the base side only: the head file no longer holds it.
    head = _head_text(guard="    resp = requests.get(url)")
    read = _reader({HANDLER: head})
    verdict = _verify(monkeypatch, _refute(), read=read)
    assert verdict.is_real is True and verdict.refutation_voided


def test_refutation_without_a_quote_is_void(monkeypatch):
    read = _reader({HANDLER: _head_text()})
    reply = {"is_real": False, "confidence": 0.9, "reasoning": "looks fine",
             "counter_evidence": None}
    verdict = _verify(monkeypatch, reply, read=read)
    assert verdict.is_real is True
    assert "no counter_evidence" in verdict.refutation_voided
    assert read.reads == []


def test_refutation_with_an_unusable_quote_is_void(monkeypatch):
    read = _reader({HANDLER: _head_text()})
    reply = _refute()
    reply["counter_evidence"]["quote"] = "   "
    verdict = _verify(monkeypatch, reply, read=read)
    assert verdict.is_real is True and verdict.refutation_voided


def test_refutation_without_a_reader_is_void(monkeypatch):
    verdict = _verify(monkeypatch, _refute(), read=None)
    assert verdict.is_real is True
    assert "no file reader" in verdict.refutation_voided


@pytest.mark.parametrize("files", [{}, {HANDLER: None}], ids=["unknown-path", "reader-returns-none"])
def test_unreadable_file_at_the_head_voids_the_refutation(monkeypatch, files):
    verdict = _verify(monkeypatch, _refute(), read=_reader(files))
    assert verdict.is_real is True
    assert "not readable" in verdict.refutation_voided


def test_a_failing_reader_voids_the_refutation(monkeypatch):
    def read(path):
        raise RuntimeError("boom")

    verdict = _verify(monkeypatch, _refute(), read=read)
    assert verdict.is_real is True
    assert "RuntimeError" in verdict.refutation_voided


def test_a_rate_limited_reader_is_not_swallowed(monkeypatch):
    def read(path):
        raise review.github.GitHubRateLimited("slow down")

    with pytest.raises(review.github.GitHubRateLimited):
        _verify(monkeypatch, _refute(), read=read)


def test_the_cited_path_decides_which_file_is_read(monkeypatch):
    read = _reader({HANDLER: _head_text(), OTHER: "nothing here\n"})
    verdict = _verify(monkeypatch, _refute(path=OTHER), read=read)
    assert read.reads == [OTHER]
    assert verdict.is_real is True and verdict.refutation_voided


def test_cited_lines_past_the_end_of_the_file_are_void(monkeypatch):
    read = _reader({HANDLER: "short\n"})
    verdict = _verify(monkeypatch, _refute(start=500), read=read)
    assert verdict.is_real is True and verdict.refutation_voided


def test_a_kept_finding_reads_no_file(monkeypatch):
    read = _reader({})
    reply = {"is_real": True, "confidence": 0.7, "reasoning": "stands", "counter_evidence": None}
    verdict = _verify(monkeypatch, reply, read=read)
    assert verdict.is_real is True and verdict.refutation_voided is None
    assert read.reads == []


def test_the_check_keys_on_the_verdict_type_not_the_variant_name(monkeypatch):
    base = review.VERIFIER_VARIANTS["default_keep"]
    monkeypatch.setitem(review.VERIFIER_VARIANTS, "future_variant", base)
    verdict = _verify(monkeypatch, _refute("not in the file"), read=_reader({HANDLER: _head_text()}),
                      variant="future_variant")
    assert verdict.is_real is True and verdict.refutation_voided


@pytest.mark.parametrize("variant", ["shipped", "reasoning_first", "evidence"])
def test_variants_without_counter_evidence_are_not_checked(monkeypatch, variant):
    reply = {"is_real": False, "confidence": 0.9, "reasoning": "r"}
    read = _reader({})
    verdict = _verify(monkeypatch, reply, read=read, variant=variant)
    assert verdict.is_real is False
    assert not hasattr(verdict, "refutation_voided")
    assert type(verdict) is Verdict
    assert read.reads == []


def test_shipped_verdict_dump_has_no_new_key(monkeypatch):
    verdict = _verify(monkeypatch, {"is_real": False, "confidence": 0.9, "reasoning": "r"},
                      read=None, variant="shipped")
    assert set(verdict.model_dump()) == {"is_real", "confidence", "reasoning"}


# --- R2 + R3 through the review pipeline ------------------------------------

FILES = [
    FilePatch(HANDLER, "modified", "@@ -40 +40 @@\n+resp = requests.get(url)\n"),
    FilePatch(OTHER, "modified", "@@ -7 +7 @@\n+while True:\n"),
]
SKIPPED_LANE = {"lane": "stub", "status": "skipped", "reason": "stub"}


def _stub_other_lanes(monkeypatch):
    probe = {"lane": "probe", "status": "ran", "checks": 1}
    monkeypatch.setattr(live, "probe_scan_for_files", lambda *args: live.ProbeScan([], probe))
    monkeypatch.setattr(review, "REVIEW_PASSES", 1)
    monkeypatch.setattr(review, "_ruff_lane", lambda *args, **kw: ([], {**SKIPPED_LANE, "lane": "ruff"}))
    monkeypatch.setattr(review, "_config_lane",
                        lambda *args, **kw: ([], {**SKIPPED_LANE, "lane": "config"}))


def _pipeline(monkeypatch, replies: dict[str, dict], files=None, variant="default_keep"):
    """Run run_diff_review with a reviewer that reports two findings and a scripted verifier."""
    _stub_other_lanes(monkeypatch)

    def fake(model, system, user, tool):
        if tool.name == "report_findings":
            return {"findings": [FINDING.model_dump(), SECOND.model_dump()]}
        payload = json.loads(user.removeprefix("# Finding\n").split("\n\n# Diff under review\n")[0])
        return replies[payload["file"]]

    monkeypatch.setattr(review, "complete_tool", fake)
    loader = files if files is not None else {HANDLER: _head_text(), OTHER: "x\n" * 20}
    return review.run_diff_review(FILES, model="model-x", file_loader=_reader(loader),
                                  verify_variant=variant)


def test_voided_refutation_keeps_the_finding_and_the_reason(monkeypatch):
    absent = _refute("resp = requests.get(url, timeout=99)")
    kept = {"is_real": True, "confidence": 0.8, "reasoning": "stands", "counter_evidence": None}
    result = _pipeline(monkeypatch, {HANDLER: absent, OTHER: kept})
    handler = next(i for i in result.bugs if i.finding.file == HANDLER)
    assert handler.verdict.is_real is True
    assert handler.verdict.refutation_voided
    assert handler.finding.description == FINDING.description
    assert [i.finding.file for i in result.all_findings] == [HANDLER, OTHER]


def test_standing_refutation_still_drops_the_finding(monkeypatch):
    stands = _refute()
    kept = {"is_real": True, "confidence": 0.8, "reasoning": "stands", "counter_evidence": None}
    result = _pipeline(monkeypatch, {HANDLER: stands, OTHER: kept})
    assert [i.finding.file for i in result.bugs] == [OTHER]
    refuted = next(i for i in result.all_findings if i.finding.file == HANDLER)
    assert refuted.verdict.is_real is False
    assert refuted.finding.description == FINDING.description


def test_voided_reason_reaches_the_serialized_verdict(monkeypatch):
    absent = _refute("not there")
    kept = {"is_real": True, "confidence": 0.8, "reasoning": "stands", "counter_evidence": None}
    result = _pipeline(monkeypatch, {HANDLER: absent, OTHER: kept})
    row = serialize_finding(next(i for i in result.all_findings if i.finding.file == HANDLER))
    assert row["verdict"]["refutation_voided"]
    assert row["description"] == FINDING.description
    assert row["verdict_is_real"] is True


def test_voided_count_is_in_the_llm_lane_record_and_no_lane_is_added(monkeypatch):
    absent = _refute("not there")
    result = _pipeline(monkeypatch, {HANDLER: absent, OTHER: _refute("also not there", path=OTHER, start=7)})
    lanes = [record["lane"] for record in result.coverage]
    assert lanes.count("llm") == 1
    llm = next(r for r in result.coverage if r["lane"] == "llm")
    assert llm["refutations_voided"] == 2
    assert lanes == ["probe", "ruff", "config", "llm", "judge"]


def test_only_voided_refutations_are_counted(monkeypatch):
    kept = {"is_real": True, "confidence": 0.8, "reasoning": "stands", "counter_evidence": None}
    result = _pipeline(monkeypatch, {HANDLER: _refute(), OTHER: kept})
    llm = next(r for r in result.coverage if r["lane"] == "llm")
    assert llm["refutations_voided"] == 0


def test_shipped_variant_adds_no_key_to_the_llm_record(monkeypatch):
    shipped_reply = {"is_real": False, "confidence": 0.9, "reasoning": "r"}
    result = _pipeline(monkeypatch, {HANDLER: shipped_reply, OTHER: shipped_reply}, variant="shipped")
    llm = next(r for r in result.coverage if r["lane"] == "llm")
    assert "refutations_voided" not in llm
    assert result.bugs == [] and len(result.all_findings) == 2


def test_diff_review_without_a_reader_voids_every_refutation(monkeypatch):
    _stub_other_lanes(monkeypatch)
    monkeypatch.setattr(review, "complete_tool", lambda model, system, user, tool: (
        {"findings": [FINDING.model_dump()]} if tool.name == "report_findings" else _refute()))
    result = review.run_diff_review(FILES, model="model-x", verify_variant="default_keep")
    assert [i.verdict.is_real for i in result.all_findings] == [True]
    llm = next(r for r in result.coverage if r["lane"] == "llm")
    assert llm["refutations_voided"] == 1


def test_skipped_llm_record_gets_no_count(monkeypatch):
    _stub_other_lanes(monkeypatch)
    monkeypatch.setattr(review, "complete_tool", lambda *args: pytest.fail("no model call expected"))
    result = review.run_diff_review([FilePatch(HANDLER, "removed", None)], model="model-x",
                                    verify_variant="default_keep")
    llm = next(r for r in result.coverage if r["lane"] == "llm")
    assert llm["status"] == "skipped"
    assert "refutations_voided" not in llm


def test_counter_evidence_verdict_defaults_to_not_voided():
    verdict = CounterEvidenceVerdict(is_real=True, confidence=0.5)
    assert verdict.refutation_voided is None


# --- Fix round: the fields are server-owned ---------------------------------

def test_a_reply_cannot_set_the_voided_fields_on_a_standing_refutation(monkeypatch):
    reply = {**_refute(), "refutation_voided": "x", "refutation_void_kind": "unreadable"}
    verdict = _verify(monkeypatch, reply, read=_reader({HANDLER: _head_text()}))
    assert verdict.is_real is False
    assert verdict.refutation_voided is None and verdict.refutation_void_kind is None


def test_a_reply_cannot_replace_the_servers_reason(monkeypatch):
    reply = {**_refute("not in the file anywhere"), "refutation_voided": "x"}
    verdict = _verify(monkeypatch, reply, read=_reader({HANDLER: _head_text()}))
    assert verdict.is_real is True
    assert verdict.refutation_voided != "x" and "absent" in verdict.refutation_voided
    assert verdict.refutation_void_kind == review.VOID_UNCONFIRMED


def test_a_reply_cannot_set_the_voided_fields_on_a_kept_finding(monkeypatch):
    reply = {"is_real": True, "confidence": 0.7, "reasoning": "stands",
             "counter_evidence": None, "refutation_voided": "x"}
    verdict = _verify(monkeypatch, reply, read=None)
    assert verdict.refutation_voided is None


def test_the_retry_path_also_drops_the_voided_fields(monkeypatch):
    replies = iter([{"confidence": 0.9}, {**_refute(), "refutation_voided": "x"}])
    monkeypatch.setattr(review, "complete_tool", lambda *args: next(replies))
    verdict = review._verify(FINDING, DIFF, "model-x", variant="default_keep",
                             read=_reader({HANDLER: _head_text()}))
    assert verdict.is_real is False and verdict.refutation_voided is None


# --- Fix round: a quote must be specific -------------------------------------

def test_the_quote_threshold_is_the_one_the_reanchor_step_uses():
    assert review.MIN_QUOTE_CHARS == 8


@pytest.mark.parametrize("quote,voided", [
    ("return x", True),       # "returnx" is 7 non-whitespace characters
    ("returns x", False),
    ("a" * 7, True),
    ("a" * 8, False),
    ("  )  ", True),
    ("a b c d e f g", True),  # 7 non-whitespace characters
    ("a b c d e f g h", False),
], ids=["seven-in-words", "eight-in-words", "seven", "eight", "paren", "spaced-seven", "spaced-eight"])
def test_a_quote_needs_eight_non_whitespace_characters(monkeypatch, quote, voided):
    head = _head_text(guard=f"    {quote}")
    verdict = _verify(monkeypatch, _refute(quote), read=_reader({HANDLER: head}))
    assert (verdict.refutation_voided is not None) is voided
    if voided:
        assert "fewer than 8" in verdict.refutation_voided
        assert verdict.refutation_void_kind == review.VOID_UNCONFIRMED


def test_cited_range_at_the_cap_stands_and_one_wider_is_void(monkeypatch):
    cap = review.COUNTER_EVIDENCE_MAX_LINES
    read = _reader({HANDLER: _head_text()})
    at_cap = _verify(monkeypatch, _refute(start=QUOTE_LINE - 1, end=QUOTE_LINE - 2 + cap), read=read)
    assert at_cap.is_real is False
    wider = _verify(monkeypatch, _refute(start=QUOTE_LINE - 1, end=QUOTE_LINE - 1 + cap), read=read)
    assert wider.is_real is True and f"wider than {cap}" in wider.refutation_voided
    assert wider.refutation_void_kind == review.VOID_UNCONFIRMED


def test_a_too_short_or_too_wide_quote_reads_no_file(monkeypatch):
    read = _reader({HANDLER: _head_text()})
    _verify(monkeypatch, _refute("x"), read=read)
    _verify(monkeypatch, _refute(end=3000), read=read)
    assert read.reads == []


# --- Fix round: infrastructure voids are counted apart ----------------------

@pytest.mark.parametrize("files,expected", [
    ({}, review.VOID_UNREADABLE),
    ({HANDLER: None}, review.VOID_UNREADABLE),
    ({HANDLER: _head_text()}, review.VOID_UNCONFIRMED),
], ids=["unknown-path", "returns-none", "absent-quote"])
def test_void_kind_separates_unreadable_files_from_wrong_quotes(monkeypatch, files, expected):
    verdict = _verify(monkeypatch, _refute("something that is not there"), read=_reader(files))
    assert verdict.refutation_void_kind == expected


def test_no_reader_and_a_failing_reader_are_unreadable(monkeypatch):
    def failing(path):
        raise RuntimeError("boom")

    assert _verify(monkeypatch, _refute(), read=None).refutation_void_kind == review.VOID_UNREADABLE
    assert _verify(monkeypatch, _refute(), read=failing).refutation_void_kind == review.VOID_UNREADABLE


def test_a_missing_quote_is_a_model_void_not_an_unreadable_one(monkeypatch):
    reply = {"is_real": False, "confidence": 0.9, "reasoning": "r", "counter_evidence": None}
    assert _verify(monkeypatch, reply, read=None).refutation_void_kind == review.VOID_UNCONFIRMED


def test_the_llm_record_splits_unreadable_from_the_total(monkeypatch):
    absent = _refute("a quote that is not in the file")
    result = _pipeline(monkeypatch, {HANDLER: absent, OTHER: _refute(path=OTHER, start=7)},
                       files={HANDLER: _head_text()})
    llm = next(r for r in result.coverage if r["lane"] == "llm")
    assert llm["refutations_voided"] == 2
    assert llm["refutations_voided_unreadable"] == 1


def test_a_standing_run_counts_zero_of_both(monkeypatch):
    kept = {"is_real": True, "confidence": 0.8, "reasoning": "stands", "counter_evidence": None}
    result = _pipeline(monkeypatch, {HANDLER: _refute(), OTHER: kept})
    llm = next(r for r in result.coverage if r["lane"] == "llm")
    assert (llm["refutations_voided"], llm["refutations_voided_unreadable"]) == (0, 0)


def test_the_shipped_record_has_neither_count(monkeypatch):
    reply = {"is_real": False, "confidence": 0.9, "reasoning": "r"}
    result = _pipeline(monkeypatch, {HANDLER: reply, OTHER: reply}, variant="shipped")
    llm = next(r for r in result.coverage if r["lane"] == "llm")
    assert "refutations_voided" not in llm and "refutations_voided_unreadable" not in llm


# --- Fix round: diff markers -------------------------------------------------

@pytest.mark.parametrize("quote", [
    f"+{GUARD}",
    f" {GUARD}",
    "+resp =\n+    requests.get(url, timeout=10)",
    f"+{GUARD}\n ",
], ids=["added", "context", "multi-line", "trailing-blank"])
def test_a_quote_copied_from_the_diff_loses_its_markers(monkeypatch, quote):
    verdict = _verify(monkeypatch, _refute(quote), read=_reader({HANDLER: _head_text()}))
    assert verdict.is_real is False and verdict.refutation_voided is None


def test_a_removed_line_quote_stays_void(monkeypatch):
    head = _head_text(guard="    resp = requests.get(url)")
    verdict = _verify(monkeypatch, _refute("-resp = requests.get(url)"), read=_reader({HANDLER: head}))
    assert verdict.is_real is True and verdict.refutation_voided


def test_a_mixed_removed_and_added_quote_stays_void(monkeypatch):
    quote = f"-resp = requests.get(url)\n+{GUARD}"
    verdict = _verify(monkeypatch, _refute(quote), read=_reader({HANDLER: _head_text()}))
    assert verdict.is_real is True and verdict.refutation_voided


# --- Fix round: the PR path --------------------------------------------------

@pytest.fixture
def pr_review(monkeypatch):
    monkeypatch.setattr(review, "fetch_pr_files", lambda repo, pr: FILES[:1])
    monkeypatch.setattr(live, "_pr_meta", lambda *args: {
        "title": "", "body": "", "commits": [], "head_sha": "sha"})
    monkeypatch.setattr(review, "fetch_house_rules", lambda repo: None)
    monkeypatch.setattr(review, "_deep_review",
                        lambda *args, **kw: review.Review(findings=[FINDING]))
    probe = {"lane": "probe", "status": "ran", "checks": 1}
    monkeypatch.setattr(live, "probe_scan", lambda *args: live.ProbeScan([], probe))
    monkeypatch.setattr(review, "_ruff_lane", lambda *args, **kw: ([], {**SKIPPED_LANE, "lane": "ruff"}))
    monkeypatch.setattr(review, "_config_lane",
                        lambda *args, **kw: ([], {**SKIPPED_LANE, "lane": "config"}))
    return monkeypatch


def test_run_review_passes_its_reader_to_the_quote_check(pr_review):
    fetched = []

    def fetch(repo, path, ref):
        fetched.append((repo, path, ref))
        return _head_text()

    pr_review.setattr(live, "_fetch_file_at", fetch)
    pr_review.setattr(review, "complete_tool", lambda *args: _refute())
    result = review.run_review("example/x", 1, verify_variant="default_keep")
    assert result.bugs == []
    assert [i.verdict.is_real for i in result.all_findings] == [False]
    assert ("example/x", HANDLER, "sha") in fetched
    llm = next(r for r in result.coverage if r["lane"] == "llm")
    assert (llm["refutations_voided"], llm["refutations_voided_unreadable"]) == (0, 0)


def test_run_review_voids_a_refutation_whose_quote_is_absent(pr_review):
    pr_review.setattr(live, "_fetch_file_at", lambda *args: _head_text())
    pr_review.setattr(review, "complete_tool", lambda *args: _refute("a quote that is not there"))
    result = review.run_review("example/x", 1, verify_variant="default_keep")
    assert [i.finding.file for i in result.bugs] == [HANDLER]
    assert result.bugs[0].verdict.refutation_voided
    llm = next(r for r in result.coverage if r["lane"] == "llm")
    assert (llm["refutations_voided"], llm["refutations_voided_unreadable"]) == (1, 0)


# --- The checklist variant shares the verdict type, so it shares the check ---

def test_default_keep_checklists_voids_an_absent_quote(monkeypatch):
    read = _reader({HANDLER: _head_text()})
    verdict = _verify(monkeypatch, _refute("a quote that is not in the file"), read=read,
                      variant="default_keep_checklists")
    assert verdict.is_real is True
    assert "absent" in verdict.refutation_voided
    assert verdict.refutation_void_kind == review.VOID_UNCONFIRMED


def test_default_keep_checklists_lets_a_present_quote_stand(monkeypatch):
    read = _reader({HANDLER: _head_text()})
    verdict = _verify(monkeypatch, _refute(), read=read, variant="default_keep_checklists")
    assert verdict.is_real is False and verdict.refutation_voided is None
