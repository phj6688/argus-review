"""The ruff lane inside a review: what it contributes, and what it admits."""

import pytest

from argus import review as review_mod
from argus import verdict
from argus.diff import FilePatch
from argus.jarvis import ruff_lane
from argus.review import run_diff_review
from argus.schema import Review, Verdict

BAD = '''import os


def get_user(conn, name):
    conn.execute("SELECT * FROM users WHERE name = " % name)


def run(cmd):
    os.system("ls " + cmd)
'''

PATCH = "@@ -1,2 +1,3 @@\n import os\n+os.system(cmd)\n"


def _files(*names):
    return [FilePatch(filename=n, status="modified", patch=PATCH) for n in names]


def _lane(result, name):
    return next((r for r in result.coverage if r["lane"] == name), None)


@pytest.fixture()
def quiet_llm(monkeypatch):
    monkeypatch.setattr(review_mod, "_deep_review",
                        lambda diff, rules, model, intent, pr_section="": Review(findings=[]))
    monkeypatch.setattr(review_mod, "_verify",
                        lambda f, d, m, **_: Verdict(is_real=True, confidence=0.9, reasoning="r"))
    monkeypatch.setattr(review_mod, "fetch_house_rules", lambda repo: None)


@pytest.fixture()
def whole_files(monkeypatch):
    """With repo + head_sha the engine can fetch whole files, which is what ruff
    needs; a patch fragment would report a syntax error on every truncation."""
    from argus.jarvis import live
    monkeypatch.setattr(live, "_fetch_file_at", lambda repo, path, sha: BAD)


def test_a_raw_patch_cannot_run_ruff_and_says_so(quiet_llm):
    """No repo/head_sha means no whole-file content. The lane declines and
    records why, rather than linting a fragment and calling it clean."""
    result = run_diff_review(_files("src/app.py"), repo=None)
    rec = _lane(result, ruff_lane.LANE)
    assert rec["status"] == verdict.LANE_SKIPPED
    assert "whole file" in rec["reason"]
    assert rec["files_eligible"] == 1, "it was eligible; the input was the problem"


def test_with_whole_files_it_finds_the_sql_injection(quiet_llm, whole_files):
    files = [FilePatch("src/app.py", "modified",
                       '@@ -4 +4,2 @@\n def get_user(conn, name):\n'
                       '+    conn.execute("SELECT * FROM users WHERE name = " % name)')]
    result = run_diff_review(files, repo="o/r", head_sha="deadbeef")
    codes = {i.finding.rule_id for i in result.bugs + result.nits}
    assert "S608" in codes
    rec = _lane(result, ruff_lane.LANE)
    assert rec["status"] == verdict.LANE_RAN
    assert rec["files_analyzed"] == 1
    assert rec["tool"]["name"] == "ruff"


def test_it_records_python_eligibility_rather_than_silently_ignoring(
        quiet_llm, whole_files):
    """A TypeScript change is not a ruff failure, it is out of scope, and the
    difference has to be visible or the lane looks clean on work it never saw."""
    files = _files("src/app.py", "web/page.tsx", "deploy/compose.yml")
    result = run_diff_review(files, repo="o/r", head_sha="deadbeef")
    rec = _lane(result, ruff_lane.LANE)
    assert rec["files_changed"] == 3
    assert rec["files_eligible"] == 1
    assert rec["files_analyzed"] == 1


def test_no_python_at_all_is_inapplicable_not_passed(quiet_llm, whole_files):
    result = run_diff_review(_files("web/page.tsx"), repo="o/r", head_sha="deadbeef")
    rec = _lane(result, ruff_lane.LANE)
    assert rec["files_eligible"] == 0
    assert rec["status"] == verdict.LANE_SKIPPED
    assert "python" in rec["reason"].lower()


def test_a_broken_binary_is_an_error_lane_not_a_clean_review(
        quiet_llm, whole_files, monkeypatch):
    def _boom(path, content):
        raise ruff_lane.RuffUnavailable("ruff not found")

    monkeypatch.setattr(ruff_lane, "scan_content", _boom)
    result = run_diff_review(_files("src/app.py"), repo="o/r", head_sha="deadbeef")
    rec = _lane(result, ruff_lane.LANE)
    assert rec["status"] == "error"
    assert verdict.coverage_level(result.coverage) != verdict.COVERAGE_FULL


def test_ruff_findings_earn_the_deterministic_tier():
    """The whole point of the lane: evidence that needs no opinion."""
    assert verdict.evidence_tier(ruff_lane.LANE, None) == verdict.TIER_DETERMINISTIC


def test_a_security_hit_alone_does_not_block(quiet_llm, whole_files):
    """S rules warn until their yield is measured, so a clean-but-for-S review
    comments rather than requesting changes."""
    files = [FilePatch("src/app.py", "modified", '@@ -8 +8,2 @@\n def run(cmd):\n'
                       '+    os.system("ls " + cmd)')]
    result = run_diff_review(files, repo="o/r", head_sha="deadbeef")
    findings = [
        {"verdict_is_real": True, "is_bug": i.finding.severity.value in ("critical", "major"),
         "lane": i.lane, "judge_verdict": None}
        for i in result.bugs + result.nits
    ]
    security_only = [f for f in findings if not f["is_bug"]]
    assert security_only, "the S hits should be present as non-blocking"
    assert verdict.decision(security_only, {"level": verdict.COVERAGE_FULL}) == "comment"


def test_ruff_only_reports_security_hits_on_added_lines(monkeypatch):
    from argus.jarvis import live

    content = "import pickle\nold = pickle.loads(b'old')\nnew = pickle.loads(b'new')\n"
    monkeypatch.setattr(live, "_fetch_file_at", lambda *args: content)
    files = [FilePatch("a.py", "modified", "@@ -1,2 +1,3 @@\n import pickle\n"
                       " old = pickle.loads(b'old')\n+new = pickle.loads(b'new')")]
    findings, rec = review_mod._ruff_lane(files, "o/r", "sha")
    assert [(i.finding.rule_id, i.finding.line) for i in findings] == [("S301", 3)]
    assert rec["status"] == verdict.LANE_RAN
    assert rec["findings"] == 1
    assert rec["findings_preexisting_suppressed"] == 1
    assert rec["preexisting_codes"] == ["S301"]


@pytest.mark.parametrize("code", ["invalid-syntax", "E902", "E999", "F821"])
def test_ruff_keeps_file_breaking_hits_on_unchanged_lines(monkeypatch, code):
    from argus.jarvis import live

    monkeypatch.setattr(live, "_fetch_file_at", lambda *args: "content")
    monkeypatch.setattr(ruff_lane, "scan_content",
                        lambda path, content: [ruff_lane.Hit(code, "broken", path, 1)])
    findings, rec = review_mod._ruff_lane(_files("a.py"), "o/r", "sha")
    assert [i.finding.rule_id for i in findings] == [code]
    assert rec["findings"] == 1
    assert rec["findings_preexisting_suppressed"] == 0
    assert rec["preexisting_codes"] == []


def test_ruff_caps_and_sorts_distinct_suppressed_codes(monkeypatch):
    from argus.jarvis import live

    monkeypatch.setattr(live, "_fetch_file_at", lambda *args: "content")
    codes = [f"S{n}" for n in range(100, 125)]
    monkeypatch.setattr(ruff_lane, "scan_content", lambda path, content: [
        ruff_lane.Hit(code, "old", path, 1) for code in [*reversed(codes), "S100"]])
    findings, rec = review_mod._ruff_lane(_files("a.py"), "o/r", "sha")
    assert findings == []
    assert rec["status"] == verdict.LANE_RAN
    assert rec["findings"] == 0
    assert rec["findings_preexisting_suppressed"] == 26
    assert rec["preexisting_codes"] == [f"S{n}" for n in range(100, 120)]


@pytest.mark.parametrize("patch,code,line,end", [
    ("@@ -10,2 +10,2 @@\n except Exception:\n-    raise\n+    pass", "S110", 10, 11),
    ("@@ -10,4 +10,3 @@\n requests.get(\n     url,\n-    timeout=10,\n )", "S113", 10, 13),
    ('@@ -10,3 +10,3 @@\n query = f"""SELECT\n-old\n+{value}\n """', "S608", 10, 12),
    (None, "S608", 10, 12),
])
def test_ruff_keeps_hits_spanning_touched_lines(monkeypatch, patch, code, line, end):
    from argus import review
    from argus.jarvis import ruff_lane
    hit = ruff_lane.Hit(code, "introduced", "a.py", line, end_line=end)
    old = ruff_lane.Hit("S605", "preexisting", "a.py", 100, end_line=101)
    monkeypatch.setattr(ruff_lane, "scan_content", lambda *args: [hit, old])
    monkeypatch.setattr(ruff_lane, "tool_identity", lambda: {})
    found, record = review._ruff_lane(
        [FilePatch("a.py", "added" if patch is None else "modified", patch)],
        None, None, lambda _: "content")
    assert [item.finding.rule_id for item in found] == ([code, "S605"] if patch is None else [code])
    assert record["findings_preexisting_suppressed"] == (0 if patch is None else 1)


def test_ruff_suppresses_hits_in_a_pure_rename(monkeypatch):
    from argus import review
    from argus.jarvis import ruff_lane
    hit = ruff_lane.Hit("S608", "preexisting", "b.py", 10, end_line=12)
    monkeypatch.setattr(ruff_lane, "scan_content", lambda *args: [hit])
    monkeypatch.setattr(ruff_lane, "tool_identity", lambda: {})
    found, record = review._ruff_lane(
        [FilePatch("b.py", "renamed", None)], None, None, lambda _: "content")
    assert found == []
    assert record["findings_preexisting_suppressed"] == 1
