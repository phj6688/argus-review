"""A rerun with the verdicts the replay already had must give back the replay row.

The source row comes from the real run_diff_review, so a change to how that function
assembles `reported` and `raw` breaks this test instead of silently biasing reruns.
"""

import json

import pytest

from argus import review
from argus.diff import FilePatch
from argus.evalkit import replay, reverify
from argus.evalkit.cases import load_cases
from argus.evalkit.score import score_results
from argus.evalkit.snapshot import Snapshot, SnapshotStore, patch_digest
from argus.jarvis import live
from argus.schema import Finding, Verdict, VerifiedFinding
from test_evalkit_cases import case_data, write_cases

FILES = [FilePatch("a.py", "modified", "@@ -1 +1 @@\n-old\n+new")]
# title: (severity, confidence, verifier says real)
REVIEWER = {
    "Wrong branch": ("major", 0.9, True),
    "Missing timeout": ("major", 0.6, True),
    "Leaked handle": ("major", 0.8, True),
    "Loose name": ("minor", 0.7, True),
    "Style nit": ("nit", 0.5, False),
    "Imagined race": ("major", 0.85, False),
}
VERIFIER_CONFIDENCE = {title: 0.55 + index / 20 for index, title in enumerate(REVIEWER)}
BUG_CAP = 2


def snapshot(self, case):
    return Snapshot(case.repo, case.base_sha, case.head_sha, list(FILES), "mirror",
                    patch_digest(FILES), contents={"a.py": "new\n"})


def lane_item(title, lane, severity):
    return VerifiedFinding(
        finding=Finding(file="a.py", line=1, severity=severity, confidence=0.9, title=title,
                        description="found without a model", proposed_fix="fix", evidence="new"),
        verdict=Verdict(is_real=True, confidence=1.0), lane=lane)


def verdict_for(title):
    return Verdict(is_real=REVIEWER[title][2], confidence=VERIFIER_CONFIDENCE[title], reasoning="r")


def gateway(model, system, user, tool):
    if tool.name == "report_findings":
        return {"findings": [
            Finding(file="a.py", line=1, severity=severity, confidence=confidence, title=title,
                    description=f"{title} description", proposed_fix="fix",
                    evidence="new").model_dump(mode="json")
            for title, (severity, confidence, _) in REVIEWER.items()]}
    title = json.loads(user.removeprefix("# Finding\n").split("\n\n# Diff under review\n")[0])["title"]
    return verdict_for(title).model_dump()


@pytest.fixture(autouse=True)
def stubbed_lanes(monkeypatch, tmp_path):
    monkeypatch.setenv("ARGUS_PROVIDER", "gateway")
    monkeypatch.setenv("ARGUS_DB_PATH", str(tmp_path / "scratch.db"))
    monkeypatch.delenv("ARGUS_JUDGE_LIVE", raising=False)
    monkeypatch.setattr(replay.SnapshotStore, "get", snapshot)
    monkeypatch.setattr(review, "complete_tool", gateway)
    monkeypatch.setattr(review, "REVIEW_PASSES", 1)
    monkeypatch.setattr(review, "fetch_house_rules", lambda repo: None)
    probe_record = {"lane": "probe", "status": "ran", "checks": 1}
    monkeypatch.setattr(live, "probe_scan_for_files", lambda *args: live.ProbeScan(
        [lane_item("Open port", "probe", "major")], probe_record))
    monkeypatch.setattr(review, "_ruff_lane", lambda *args: (
        [lane_item("Unused import", "ruff", "minor"), lane_item("Bare except", "ruff", "major")],
        {"lane": "ruff", "status": "ran", "checks": 2}))
    monkeypatch.setattr(review, "_config_lane", lambda *args: (
        [lane_item("Open bind", "config", "minor")], {"lane": "config", "status": "ran", "checks": 1}))
    monkeypatch.setattr(reverify, "code_version", lambda: ("test-sha", False))
    monkeypatch.setattr(replay, "code_version", lambda: ("test-sha", False))


def without_time(value):
    """Durations differ by design: a rerun times the verifier only."""
    if isinstance(value, dict):
        return {key: without_time(item) for key, item in value.items() if key != "mean_duration_s"}
    if isinstance(value, list):
        return [without_time(item) for item in value]
    return value


def test_a_rerun_with_the_same_verdicts_reproduces_the_replay_row(tmp_path, monkeypatch):
    cases_path = write_cases(tmp_path / "cases", [case_data("one")])
    case = load_cases(cases_path)[0]
    store = SnapshotStore(None, tmp_path / "snapshots")
    replayed = replay.replay_case(case, 1, "baseline", store, "model-x", BUG_CAP,
                                  {"code_sha": "test-sha", "code_dirty": False,
                                   "credential_env": "ANTHROPIC_AUTH_TOKEN"})
    assert replayed["error"] is None
    source = json.loads(json.dumps(replayed))
    lanes = {item["lane"] for item in source["reported"]}
    assert lanes == {"llm", "probe", "ruff", "config"}
    assert len(source["raw"]) == len(REVIEWER)
    assert [item["severity"] for item in source["reported"]].count("major") >= 3
    results = tmp_path / "baseline.jsonl"
    results.write_text(json.dumps(source) + "\n")

    monkeypatch.setattr(review, "_verify", lambda finding, diff, model, **kwargs: verdict_for(finding.title))
    monkeypatch.setattr(review, "complete_tool", lambda *a: pytest.fail("no gateway call expected"))
    out = tmp_path / "rerun.jsonl"
    assert reverify.main(["--cases", str(cases_path), "--results", str(results), "--out", str(out),
                          "--variant", "shipped", "--label", "baseline", "--model", "model-x",
                          "--bug-cap", str(BUG_CAP)]) == 0
    [rerun] = [json.loads(line) for line in out.read_text().splitlines()]

    assert rerun["reported"] == source["reported"]
    assert rerun["raw"] == source["raw"]
    assert rerun["coverage"] == source["coverage"]
    assert rerun["snapshot"] == source["snapshot"]
    cases = load_cases(cases_path)
    before = score_results(cases, [source])["variants"]["baseline"]
    after = score_results(cases, [rerun])["variants"]["baseline"]
    assert without_time(after) == without_time(before)
