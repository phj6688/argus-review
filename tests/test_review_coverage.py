"""The engine records which lanes ran, so the verdict document can stop guessing.

Coverage has to come from the engine: nothing downstream can tell, from a review
row and a finding list, whether a lane was skipped or simply found nothing.
"""

import json

import pytest

from argus import review as review_mod
from argus import verdict
from argus.diff import FilePatch
from argus.review import run_diff_review
from argus.schema import Finding, Review, Severity, Verdict

COMPOSE_PATCH = """@@ -1,2 +1,3 @@
 services:
+  web: {ports: ["0.0.0.0:8080:80"]}
 volumes:
"""

PY_PATCH = """@@ -1,4 +1,5 @@
 import os
+os.system("ls " + cmd)
 print(1)
"""


def _files():
    return [
        FilePatch(filename="deploy/docker-compose.yml", status="modified", patch=COMPOSE_PATCH),
        FilePatch(filename="src/app.py", status="modified", patch=PY_PATCH),
    ]


def _lane(result, name):
    return next((rec for rec in result.coverage if rec["lane"] == name), None)


def _boom(*args, **kwargs):
    raise AssertionError("this run must not call a model")


def test_fast_mode_records_the_reviewer_lane_as_skipped(monkeypatch):
    monkeypatch.setattr(review_mod, "complete_tool", _boom)
    result = run_diff_review(_files(), fast=True)
    llm = _lane(result, verdict.LANE_LLM)
    assert llm["status"] == verdict.LANE_SKIPPED
    assert "fast" in llm["reason"]


def test_fast_mode_records_how_many_probes_actually_executed(monkeypatch):
    monkeypatch.setattr(review_mod, "complete_tool", _boom)
    result = run_diff_review(_files(), fast=True)
    probe = _lane(result, "probe")
    assert probe["status"] == verdict.LANE_RAN
    # Raw patches, no repo/head_sha: FileProbes cannot run, so only the line
    # probes are eligible. That is the whole point of reporting the number.
    assert probe["checks"] == 1
    assert probe["eligible"] == 6


def test_a_python_only_diff_reports_zero_executed_checks(monkeypatch):
    """The reproduction case: nothing matched, so nothing ran, and the record says so."""
    monkeypatch.setattr(review_mod, "complete_tool", _boom)
    py_only = [FilePatch(filename="src/app.py", status="added", patch=PY_PATCH)]
    result = run_diff_review(py_only, fast=True)
    assert _lane(result, "probe")["checks"] == 0
    assert verdict.coverage_level(result.coverage) == verdict.COVERAGE_NONE


def test_full_mode_records_the_reviewer_lane_as_run(monkeypatch):
    finding = Finding(file="src/app.py", line=2, severity=Severity.major,
                      confidence=0.7, title="shell injection", description="d",
                      proposed_fix="use subprocess")
    monkeypatch.setattr(review_mod, "_deep_review",
                        lambda diff, rules, model, intent, pr_section="": Review(findings=[finding]))
    monkeypatch.setattr(review_mod, "_verify",
                        lambda f, diff, model, **_: Verdict(is_real=True, confidence=0.9, reasoning="r"))
    result = run_diff_review(_files(), repo=None)
    assert _lane(result, verdict.LANE_LLM)["status"] == verdict.LANE_RAN
    assert verdict.coverage_level(result.coverage) == verdict.COVERAGE_FULL


def test_a_diff_too_big_for_the_prompt_is_not_reported_as_fully_reviewed(monkeypatch):
    """The real context renderer path, not a stub. Over-budget files reach the prompt
    as a names-only list, so the reviewer never sees their contents. If every file
    overflows, the reviewer has nothing to read and must be skipped."""
    monkeypatch.setattr(review_mod, "complete_tool", _boom)
    # Comfortably past REVIEW_CONTEXT_BUDGET_TOKENS once the estimator is applied.
    body = "@@ -1,1 +1,2 @@\n" + "\n".join(f"+x{i} = {i}" for i in range(40_000))
    huge = [FilePatch(filename=f"src/big{n}.py", status="modified", patch=body)
            for n in range(3)]
    result = run_diff_review(huge, repo=None)
    llm = _lane(result, verdict.LANE_LLM)
    assert llm["status"] == verdict.LANE_SKIPPED
    assert llm["files_omitted"] > 0
    assert verdict.coverage_level(result.coverage) == verdict.COVERAGE_NONE
    assert verdict.decision([], {"level": verdict.COVERAGE_NONE}) == \
        verdict.INSUFFICIENT_EVIDENCE


def test_a_deletion_only_change_has_no_reviewer_evidence(monkeypatch):
    monkeypatch.setattr(review_mod, "complete_tool", _boom)
    gone = [FilePatch(filename="src/old.py", status="removed", patch=None)]
    result = run_diff_review(gone, repo=None)
    llm = _lane(result, verdict.LANE_LLM)
    assert llm["status"] == verdict.LANE_SKIPPED
    assert llm["files_reviewed"] == 0
    assert llm["files_omitted"] == 0
    assert verdict.coverage_level(result.coverage) == verdict.COVERAGE_NONE
    assert verdict.decision([], {"level": verdict.COVERAGE_NONE}) == verdict.INSUFFICIENT_EVIDENCE


def test_an_all_binary_change_reviews_nothing_and_says_so(monkeypatch):
    """Every changed file unreadable means the reviewer read none of the change.
    Zero findings out of zero files read is not a pass."""
    monkeypatch.setattr(review_mod, "complete_tool", _boom)
    binaries = [
        FilePatch(filename="assets/a.png", status="modified", patch=None),
        FilePatch(filename="assets/b.woff2", status="added", patch=None),
    ]
    result = run_diff_review(binaries, repo=None)
    llm = _lane(result, verdict.LANE_LLM)
    assert llm["status"] == verdict.LANE_SKIPPED
    assert llm["files_reviewed"] == 0
    assert llm["files_omitted"] == 2
    assert llm["files_without_patch"] == 2
    assert verdict.coverage_level(result.coverage) == verdict.COVERAGE_NONE


def test_a_changed_file_with_no_patch_counts_as_unread(monkeypatch):
    """GitHub omits `patch` for binaries and for diffs it deems too large.
    compress_diff puts such a file in neither included nor overflow, so it used to
    vanish from every count: never reviewed, never recorded as missed."""
    monkeypatch.setattr(review_mod, "_deep_review",
                        lambda diff, rules, model, intent, pr_section="": Review(findings=[]))
    files = [
        FilePatch(filename="src/app.py", status="modified", patch=PY_PATCH),
        FilePatch(filename="assets/logo.png", status="modified", patch=None),
        FilePatch(filename="src/generated.ts", status="modified", patch=None),
        FilePatch(filename="src/gone.py", status="removed", patch=None),
    ]
    result = run_diff_review(files, repo=None)
    llm = _lane(result, verdict.LANE_LLM)
    assert llm["files_reviewed"] == 1
    assert llm["files_omitted"] == 2          # the two patchless files, not the deletion
    assert llm["files_without_patch"] == 2
    assert verdict.coverage_level(result.coverage) == verdict.COVERAGE_PARTIAL


def test_probe_lane_failure_is_recorded_not_silently_dropped(monkeypatch):
    """A crashed lane must not read as a lane that ran and found nothing."""
    from argus.jarvis import live

    def explode(*args, **kwargs):
        raise RuntimeError("probe scan blew up")

    monkeypatch.setattr(live, "probe_scan_for_files", explode)
    monkeypatch.setattr(review_mod, "complete_tool", _boom)
    result = run_diff_review(_files(), fast=True)
    probe = _lane(result, "probe")
    assert probe["status"] == "error"
    assert verdict.coverage_level(result.coverage) == verdict.COVERAGE_NONE


def _run_offline(monkeypatch, files, pr_review):
    from argus.jarvis import live

    monkeypatch.setattr(live, "_pr_meta", lambda *args: {"title": "", "body": "", "commits": []})
    monkeypatch.setattr(review_mod, "fetch_pr_files", lambda *args: files)
    monkeypatch.setattr(review_mod, "fetch_house_rules", lambda *args: None)
    monkeypatch.setattr(review_mod, "_intent_for_prompt", lambda *args: None)
    monkeypatch.setattr(review_mod, "_head_sha_for", lambda *args: None)
    # One executed deterministic check keeps partial coverage available even
    # when the reviewer has nothing to read.
    probe_record = {"lane": "probe", "status": "ran", "checks": 1}
    monkeypatch.setattr(review_mod, "_probe_lane", lambda *args: ([], [], probe_record))
    monkeypatch.setattr(live, "probe_scan_for_files",
                        lambda *args: live.ProbeScan([], probe_record))
    if pr_review:
        return review_mod.run_review("o/r", 1)
    return run_diff_review(files)


@pytest.mark.parametrize("pr_review", [False, True], ids=["diff", "pr"])
@pytest.mark.parametrize("files", [
    [], [FilePatch("asset.png", "modified", None)],
    [FilePatch("old.py", "removed", None)],
    [FilePatch("big.ts", "modified", "@@ -1 +1 @@\n+x = 1")],
], ids=["empty", "patchless", "deleted", "over-budget"])
def test_no_reviewable_text_skips_models_and_withholds_approval(monkeypatch, pr_review, files):
    monkeypatch.setattr(review_mod, "complete_tool", _boom)
    monkeypatch.setattr(review_mod, "REVIEW_CONTEXT_BUDGET_TOKENS", 0)
    result = _run_offline(monkeypatch, files, pr_review)
    llm = _lane(result, verdict.LANE_LLM)
    assert llm["status"] == verdict.LANE_SKIPPED
    assert llm["reason"] == "no reviewable patch text: nothing for the reviewer to read"
    assert llm["files_reviewed"] == 0
    assert llm["files_omitted"] == sum(f.status != "removed" for f in files)
    assert llm["files_without_patch"] == sum(f.status != "removed" and not f.patch for f in files)
    assert _lane(result, "probe")["checks"] == 1
    assert _lane(result, "ruff") is not None
    assert _lane(result, "config") is not None
    assert result.bugs + result.nits == []
    doc = verdict.document({"id": 1, "status": "done", "model": "model",
                            "coverage_json": json.dumps({"lanes": result.coverage})}, [])
    assert doc["verdict"] == verdict.INSUFFICIENT_EVIDENCE
    assert verdict.decision([], doc["coverage"]) == verdict.INSUFFICIENT_EVIDENCE
    assert doc["engine"]["reviewer_model"] is None


@pytest.mark.parametrize("pr_review", [False, True], ids=["diff", "pr"])
def test_llm_coverage_carries_parsing_stats(monkeypatch, pr_review):
    monkeypatch.setattr(review_mod, "REVIEW_PASSES", 2)
    payload = {"findings": [
        {"file": "src/app.py", "line": 2, "severity": "major", "confidence": 0.8,
         "description": "Wrong return value.", "proposed_fix": "Fix it"},
        {"title": "No location"},
    ]}

    payloads = iter([payload, {"findings": []}])

    def complete(model, system, user, tool):
        if tool.name == "report_findings":
            return next(payloads)
        assert tool.name == "report_verdict"
        return {"is_real": True, "confidence": 0.9, "reasoning": "Confirmed"}

    monkeypatch.setattr(review_mod, "complete_tool", complete)
    monkeypatch.delenv(review_mod.JUDGE_LIVE_ENV, raising=False)
    result = _run_offline(monkeypatch, _files(), pr_review)
    llm = _lane(result, verdict.LANE_LLM)
    assert llm["status"] == verdict.LANE_RAN
    assert llm["findings_received"] == 2
    assert llm["findings_repaired"] == 1
    assert llm["findings_dropped"] == 1
    assert llm["passes_run"] == 2
    assert llm["findings_per_pass"] == [1, 0]
    assert llm["duplicates_dropped"] == 0
    assert llm["later_pass_errors"] == 0
    assert result.raw_count == 1
    assert [i.finding.title for i in result.all_findings] == ["Wrong return value."]


@pytest.mark.parametrize("pr_review", [False, True], ids=["diff", "pr"])
@pytest.mark.parametrize("later_fails", [False, True], ids=["merged", "later-error"])
def test_multipass_coverage_and_merged_verification(monkeypatch, pr_review, later_fails):
    monkeypatch.setattr(review_mod, "REVIEW_PASSES", 2)
    first = Finding(file="notes.txt", line=1, severity=Severity.major, confidence=0.9,
                    title="Request never times out", description="The request hangs",
                    proposed_fix="Set a timeout", category="resources", evidence="first_target()")
    second = Finding(file="notes.txt", line=2, severity=Severity.major, confidence=0.9,
                     title="Invalid flag accepted", description="Unknown flags run silently",
                     proposed_fix="Reject unknown flags", category="input_handling",
                     evidence="second_target()")
    payloads = iter([
        {"findings": [first.model_dump(mode="json")]},
        RuntimeError("unavailable") if later_fails else {
            "findings": [first.model_dump(mode="json"), second.model_dump(mode="json")]},
    ])
    verified = []

    def complete(model, system, user, tool):
        if tool.name == "report_findings":
            payload = next(payloads)
            if isinstance(payload, Exception):
                raise payload
            return payload
        assert tool.name == "report_verdict"
        payload = user.removeprefix("# Finding\n").split("\n\n# Diff under review\n")[0]
        verified.append(json.loads(payload))
        return {"is_real": True, "confidence": 0.9, "reasoning": "Confirmed"}

    content = "\n".join(["pass"] * 9 + ["first_target()"]
                        + ["pass"] * 9 + ["second_target()"])
    monkeypatch.setattr(review_mod, "_file_reader", lambda *args: lambda path: content)
    monkeypatch.setattr(review_mod, "complete_tool", complete)
    monkeypatch.delenv(review_mod.JUDGE_LIVE_ENV, raising=False)
    result = _run_offline(monkeypatch, [
        FilePatch("notes.txt", "modified", "@@ -10 +10 @@\n-old\n+first_target()\n"
                  "@@ -20 +20 @@\n-old\n+second_target()")], pr_review)
    llm = _lane(result, verdict.LANE_LLM)
    assert llm["passes_run"] == 2
    assert llm["findings_per_pass"] == ([1] if later_fails else [1, 2])
    assert llm["findings_received"] == (1 if later_fails else 3)
    assert llm["findings_repaired"] == llm["findings_dropped"] == 0
    assert llm["duplicates_dropped"] == (0 if later_fails else 1)
    assert llm["later_pass_errors"] == int(later_fails)
    assert llm["findings_reanchored"] == (1 if later_fails else 2)
    assert [f["line"] for f in verified] == ([10] if later_fails else [10, 20])
    assert [i.finding.title for i in result.all_findings] == (
        ["Request never times out"] if later_fails else
        ["Request never times out", "Invalid flag accepted"])
    assert result.raw_count == len(result.bugs) == (1 if later_fails else 2)


@pytest.mark.parametrize("pr_review", [False, True], ids=["diff", "pr"])
@pytest.mark.parametrize("count", [0, 2])
def test_all_malformed_findings_do_not_approve(monkeypatch, pr_review, count):
    monkeypatch.setattr(review_mod, "REVIEW_PASSES", 1)
    monkeypatch.setattr(review_mod, "complete_tool", lambda *args: {
        "findings": [{"path": "a.py", "title": "Wrong result"}] * count})
    result = _run_offline(monkeypatch, _files(), pr_review)
    llm = _lane(result, "llm")
    assert llm["findings_received"] == llm["findings_dropped"] == count
    assert llm["status"] == ("error" if count else "ran")
    level = verdict.coverage_level(result.coverage)
    if count:
        assert llm["reason"] == "reviewer output unusable: all 2 findings were malformed"
        assert level != "full"
        assert verdict.decision([], {"level": level}) != "approve"
    else:
        assert level == "full"
