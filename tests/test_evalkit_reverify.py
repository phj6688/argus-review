import hashlib
import json
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from argus import github, review
from argus.diff import FilePatch, compress_diff
from argus.evalkit import replay, reverify
from argus.evalkit.cases import load_cases
from argus.evalkit.score import score_results
from argus.evalkit.snapshot import Snapshot, patch_digest
from argus.schema import (
    CounterEvidenceVerdict, Finding, JudgeOpinion, Verdict, VerifiedFinding,
)
from argus.tokens import HeuristicEstimator
from test_evalkit_cases import case_data, write_cases

HEAD_TEXT = "def run():\n    return 1\n"
FILES = [FilePatch("a.py", "modified", "@@ -1 +1 @@\n-old\n+new")]
SAMPLE_CASE = SimpleNamespace(repo="owner/project", base_sha="abc", head_sha="def")
QUOTE = {"path": "a.py", "start_line": 1, "end_line": 1, "quote": "def run():"}
ABSENT_QUOTE = {"path": "a.py", "start_line": 1, "end_line": 1, "quote": "a line that is nowhere"}
STALE_COUNTS = {"refutations_voided": 99, "refutations_voided_unreadable": 98}


def stub_snapshot(self, case):
    return Snapshot(case.repo, case.base_sha, case.head_sha, list(FILES), "mirror",
                    patch_digest(FILES), contents={"a.py": HEAD_TEXT})


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    def forbidden(name):
        def refuse(*args, **kwargs):
            pytest.fail(f"{name} must not run in a verifier rerun")
        return refuse

    monkeypatch.setattr(github, "get", forbidden("network"))
    monkeypatch.setattr(review, "complete_tool", forbidden("model"))
    for name in ("run_review", "run_diff_review", "_deep_review"):
        monkeypatch.setattr(review, name, forbidden("reviewer"))
    monkeypatch.setattr(replay.SnapshotStore, "get", stub_snapshot)
    monkeypatch.setattr(reverify, "code_version", lambda: ("test-sha", False))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for name in ("ARGUS_MIRROR_ROOT", "ARGUS_EVAL_CACHE", "ARGUS_EVAL_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ARGUS_PROVIDER", "gateway")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "production-token")


def finding(title, line=1, severity="major", confidence=0.8):
    return Finding(file="a.py", line=line, severity=severity, confidence=confidence,
                   title=title, description=f"{title} description", proposed_fix="fix",
                   evidence="new")


def verified(title, real, lane="llm", severity="major", line=1, confidence=0.8, judge=None):
    return VerifiedFinding(finding=finding(title, line, severity, confidence),
                           verdict=Verdict(is_real=real, confidence=0.9), lane=lane,
                           judge=judge, fingerprint=f"fp:{title}")


def source_row(case_id="one", run=1, variant="baseline", raw=(), reported=(), **extra):
    snapshot = replay.snapshot_summary(stub_snapshot(None, SAMPLE_CASE))
    return {"variant": variant, "case_id": case_id, "run": run, "code_sha": "old-sha",
            "code_dirty": False, "credential_env": "ANTHROPIC_AUTH_TOKEN", "model": "reviewer-m",
            "github_mode": "mirror", "started_at": "t0", "error": None,
            "coverage": [{"lane": "llm", "status": "ran", "model": "reviewer-m",
                          "verify_variant": "shipped", "files_omitted": 0}],
            "reported": [replay.serialize_finding(i) for i in reported],
            "raw": [replay.serialize_finding(i) for i in raw],
            "snapshot": snapshot, "ended_at": "t1", "duration_s": 1.0, **extra}


def sample_key():
    return reverify.VerdictKey("c" * 64, "baseline", "label", "one", 1, "k", 0, "f" * 64, "s" * 64,
                               "h" * 40, "d" * 64, "v" * 64, "default_keep", "m")


def write_results(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


class Gateway:
    """A complete_tool stand-in that answers by finding title and records every call."""

    def __init__(self, monkeypatch, answers=None, fail_at=None, interrupt_at=None, extra=None):
        self.calls = []
        self.answers = answers or {}
        self.fail_at, self.interrupt_at, self.extra = fail_at, interrupt_at, extra or {}
        monkeypatch.setattr(review, "complete_tool", self)

    @property
    def titles(self):
        return [call["title"] for call in self.calls]

    def __call__(self, model, system, user, tool):
        payload = json.loads(user.removeprefix("# Finding\n").split("\n\n# Diff under review\n")[0])
        title = payload["title"]
        self.calls.append({"model": model, "system": system, "tool": tool.name, "title": title})
        if self.interrupt_at == len(self.calls):
            raise KeyboardInterrupt
        if self.fail_at == title:
            raise RuntimeError("gateway down")
        return {"reasoning": "r", "is_real": self.answers.get(title, True), "confidence": 0.7,
                **self.extra}


@pytest.fixture
def workspace(tmp_path):
    cases = write_cases(tmp_path / "cases", [case_data("one"), case_data("two")])
    return {"cases": cases, "out": tmp_path / "out.jsonl", "tmp": tmp_path}


def run(workspace, results, *extra, variant="shipped", model="verifier-m", source="source_results.jsonl"):
    results_path = workspace["tmp"] / source
    if results is not None:
        write_results(results_path, results)
    model_args = ["--model", model] if model else []
    return reverify.main(["--cases", str(workspace["cases"]), "--results", str(results_path),
                          "--out", str(workspace["out"]), "--variant", variant, *model_args, *extra])


def output_rows(workspace):
    return [json.loads(line) for line in workspace["out"].read_text().splitlines()]


def standard_source():
    raw = [verified("A", True), verified("B", False, line=2), verified("C", True, line=3)]
    probe = verified("P", True, lane="probe", line=9)
    return [source_row(raw=raw, reported=[raw[0], raw[2], probe])]


# R1 + R3: one verifier call per raw finding, variant passed through.
def test_each_raw_finding_gets_one_verifier_call_with_the_variant(workspace, monkeypatch):
    gateway = Gateway(monkeypatch)
    seen = []
    real_verify = review._verify

    def spy(finding, diff, model, **kwargs):
        seen.append((finding.title, model, kwargs))
        return real_verify(finding, diff, model, **kwargs)

    monkeypatch.setattr(review, "_verify", spy)
    assert run(workspace, standard_source(), variant="evidence") == 0
    assert sorted(gateway.titles) == ["A", "B", "C"]
    assert {call["tool"] for call in gateway.calls} == {"report_verdict"}
    assert {kwargs["variant"] for _, _, kwargs in seen} == {"evidence"}
    assert {model for _, model, _ in seen} == {"verifier-m"}


def test_the_rebuilt_diff_comes_from_the_stored_snapshot(workspace, monkeypatch):
    diffs = []
    monkeypatch.setattr(review, "_verify", lambda f, diff, model, **kw: (
        diffs.append(diff), Verdict(is_real=True, confidence=0.5))[1])
    run(workspace, standard_source())
    expected = compress_diff(FILES, review.DIFF_BUDGET_TOKENS, HeuristicEstimator()).prompt
    assert diffs == [expected] * 3 and "a.py" in expected


# R2: refuted findings are verified again.
def test_refuted_findings_are_verified_again(workspace, monkeypatch):
    gateway = Gateway(monkeypatch)
    run(workspace, standard_source())
    assert "B" in gateway.titles
    assert len(output_rows(workspace)[0]["raw"]) == 3


# R4: a reader over the snapshot store reaches the verifier.
def test_verifier_receives_a_reader_that_returns_pr_head_text(workspace, monkeypatch):
    readers = []

    def capture(finding, diff, model, *, variant, read):
        readers.append(read)
        return Verdict(is_real=True, confidence=0.5)

    monkeypatch.setattr(review, "_verify", capture)
    run(workspace, standard_source())
    assert len(readers) == 3 and all(callable(read) for read in readers)
    assert readers[0]("a.py") == HEAD_TEXT


def test_the_shipped_verifier_ignores_the_reader(monkeypatch):
    payload = {"reasoning": "r", "is_real": True, "confidence": 0.7}
    calls = []
    monkeypatch.setattr(review, "complete_tool", lambda *args: calls.append(args) or dict(payload))

    def boom(path):
        raise AssertionError("the shipped verifier must not read files")

    plain = review._verify(finding("A"), "diff", "m")
    with_reader = review._verify(finding("A"), "diff", "m", read=boom)
    assert plain == with_reader
    assert calls[0][1:3] == calls[1][1:3] and calls[0][3] is calls[1][3]


# R5: replay-format rows that score_results accepts.
def test_reported_is_rank_and_cap_over_new_verdicts_plus_other_lanes(workspace, monkeypatch):
    Gateway(monkeypatch, answers={"A": False, "B": True, "C": True})
    source = standard_source()
    run(workspace, source)
    [row] = output_rows(workspace)
    new = [VerifiedFinding(finding=item.finding, fingerprint=item.fingerprint,
                           verdict=Verdict(is_real=real, confidence=0.7, reasoning="r"))
           for item, real in zip((verified("A", False), verified("B", True, line=2),
                                  verified("C", True, line=3)), (False, True, True), strict=True)]
    bugs, nits = review.rank_and_cap(new)
    probe = replay.serialize_finding(verified("P", True, lane="probe", line=9))
    expected = [replay.serialize_finding(i) for i in bugs] + [probe] + [
        replay.serialize_finding(i) for i in nits]
    assert row["reported"] == expected
    assert [f["verdict_is_real"] for f in row["raw"]] == [False, True, True]
    cases = load_cases(workspace["cases"])
    report = score_results(cases, [row])
    assert set(report["variants"]) == {"shipped"}


def test_bug_cap_applies_to_the_new_verdicts(workspace, monkeypatch):
    Gateway(monkeypatch)
    raw = [verified(t, True, line=i + 1, confidence=0.9 - i / 10) for i, t in enumerate("ABC")]
    run(workspace, [source_row(raw=raw, reported=raw)], "--bug-cap", "2")
    assert [f["title"] for f in output_rows(workspace)[0]["reported"]] == ["A", "B"]


def test_rows_record_what_ran_and_where_it_came_from(workspace, monkeypatch):
    Gateway(monkeypatch)
    source = standard_source()
    run(workspace, source, "--label", "rerun", variant="reasoning_first")
    [row] = output_rows(workspace)
    assert row["variant"] == "rerun" and row["verify_variant"] == "reasoning_first"
    assert row["verify_model"] == "verifier-m" and row["model"] == "verifier-m"
    assert row["reviewer_model"] == "reviewer-m"
    assert row["source_results"] == "source_results.jsonl"
    assert row["source_variant"] == "baseline"
    assert (row["code_sha"], row["code_dirty"]) == ("test-sha", False)
    assert row["credential_env"] == "ANTHROPIC_AUTH_TOKEN"
    assert row["case_id"] == "one" and row["run"] == 1 and row["error"] is None
    assert row["snapshot"] == source[0]["snapshot"]
    llm = next(lane for lane in row["coverage"] if lane["lane"] == "llm")
    assert llm["verify_variant"] == "reasoning_first" and llm["model"] == "reviewer-m"
    for name in ("started_at", "ended_at", "duration_s"):
        assert name in row


def test_label_defaults_to_the_verifier_variant(workspace, monkeypatch):
    Gateway(monkeypatch)
    run(workspace, standard_source(), variant="default_keep")
    assert output_rows(workspace)[0]["variant"] == "default_keep"


def test_judge_and_fingerprint_survive_the_rerun(workspace, monkeypatch):
    Gateway(monkeypatch)
    opinion = JudgeOpinion(verdict="real", confidence=0.8, model="judge")
    raw = [verified("A", True, judge=opinion)]
    run(workspace, [source_row(raw=raw, reported=raw)])
    [kept] = output_rows(workspace)[0]["reported"]
    assert kept["judge"] == {"verdict": "real", "confidence": 0.8, "model": "judge"}
    assert kept["fingerprint"] == "fp:A"


# R6: the verdict cache.
def test_a_restart_after_an_interrupt_verifies_no_finding_twice(workspace, monkeypatch):
    raw = [verified(t, True, line=i + 1) for i, t in enumerate("ABCD")]
    source = [source_row(raw=raw, reported=raw)]
    first = Gateway(monkeypatch, interrupt_at=3)
    with pytest.raises(KeyboardInterrupt):
        run(workspace, source)
    assert first.titles == ["A", "B", "C"]
    assert not workspace["out"].exists() or workspace["out"].read_text() == ""
    second = Gateway(monkeypatch)
    assert run(workspace, None) == 0
    assert second.titles == ["C", "D"]
    assert sorted(first.titles[:2] + second.titles) == ["A", "B", "C", "D"]
    assert [f["title"] for f in output_rows(workspace)[0]["raw"]] == list("ABCD")


def test_a_complete_output_makes_a_restart_do_nothing(workspace, monkeypatch):
    Gateway(monkeypatch)
    run(workspace, standard_source())
    before = workspace["out"].read_bytes()
    again = Gateway(monkeypatch)
    run(workspace, None)
    assert again.calls == [] and workspace["out"].read_bytes() == before


def test_cached_verdicts_survive_a_lost_output_file(workspace, monkeypatch):
    Gateway(monkeypatch)
    run(workspace, standard_source())
    workspace["out"].unlink()
    again = Gateway(monkeypatch)
    run(workspace, None)
    assert again.calls == [] and len(output_rows(workspace)[0]["raw"]) == 3


def test_a_torn_cache_tail_is_dropped_and_that_finding_runs_again(workspace, monkeypatch):
    Gateway(monkeypatch)
    run(workspace, standard_source())
    cache = workspace["out"].with_name("out.jsonl.verdicts")
    lines = cache.read_text().splitlines()
    cache.write_text("\n".join(lines[:-1]) + "\n" + lines[-1][:20])
    workspace["out"].unlink()
    again = Gateway(monkeypatch)
    run(workspace, None)
    assert len(again.calls) == 1


def test_a_cache_from_another_variant_or_model_is_not_reused(workspace, monkeypatch):
    Gateway(monkeypatch)
    run(workspace, standard_source())
    cache = workspace["out"].with_name("out.jsonl.verdicts")
    for variant, model in (("evidence", "verifier-m"), ("shipped", "other-m")):
        gateway = Gateway(monkeypatch)
        workspace["out"].unlink()
        run(workspace, None, "--verdict-cache", str(cache), variant=variant, model=model)
        assert len(gateway.calls) == 3


def test_the_same_title_twice_in_one_row_is_verified_twice(workspace, monkeypatch):
    gateway = Gateway(monkeypatch)
    raw = [verified("A", True), verified("A", False)]
    run(workspace, [source_row(raw=raw, reported=raw)])
    assert gateway.titles == ["A", "A"]
    restart = Gateway(monkeypatch)
    workspace["out"].unlink()
    run(workspace, None)
    assert restart.calls == []


def test_counter_evidence_comes_back_from_the_cache(workspace, monkeypatch):
    Gateway(monkeypatch, answers={"A": False}, extra={"counter_evidence": QUOTE})
    run(workspace, standard_source(), variant="default_keep")
    first = output_rows(workspace)[0]
    assert first["raw"][0]["verdict"]["counter_evidence"] == QUOTE
    workspace["out"].unlink()
    again = Gateway(monkeypatch)
    run(workspace, None, variant="default_keep")
    assert again.calls == []
    assert output_rows(workspace)[0]["raw"] == first["raw"]


def test_cached_verdicts_load_as_the_variant_verdict_type(tmp_path):
    cache = reverify.VerdictCache(tmp_path / "cache")
    key = sample_key()
    cache.put(key, CounterEvidenceVerdict(is_real=False, confidence=0.5, counter_evidence=QUOTE))
    loaded = reverify.VerdictCache(tmp_path / "cache").get(key)
    assert isinstance(loaded, CounterEvidenceVerdict) and loaded.counter_evidence.quote == "def run():"


# Failure handling and selection.
def test_a_verifier_failure_is_recorded_and_finished_work_is_kept(workspace, monkeypatch):
    Gateway(monkeypatch, fail_at="B")
    run(workspace, standard_source())
    [row] = output_rows(workspace)
    assert row["error"] == {"class": "RuntimeError", "message": "gateway down"}
    assert row["raw"] == [] and row["reported"] == []
    assert len(workspace["out"].with_name("out.jsonl.verdicts").read_text().splitlines()) == 1


def test_a_failed_source_row_is_carried_as_an_error_row(workspace, monkeypatch):
    gateway = Gateway(monkeypatch)
    failed = source_row(error={"class": "OSError", "message": "missing commit"})
    run(workspace, [failed])
    [row] = output_rows(workspace)
    assert gateway.calls == []
    assert row["error"]["class"] == "OSError"
    assert row["verify_variant"] == "shipped"


def test_two_source_variants_need_a_choice(workspace, monkeypatch):
    gateway = Gateway(monkeypatch)
    rows = [*standard_source(), source_row(variant="candidate")]
    with pytest.raises(SystemExit):
        run(workspace, rows)
    run(workspace, rows, "--source-variant", "candidate")
    assert gateway.calls == [] and output_rows(workspace)[0]["source_variant"] == "candidate"


def test_a_snapshot_that_differs_from_the_source_row_is_an_error(workspace, monkeypatch):
    Gateway(monkeypatch)
    row = standard_source()[0]
    row["snapshot"]["patch_sha256"] = "0" * 64
    run(workspace, [row])
    assert "snapshot" in output_rows(workspace)[0]["error"]["message"]


def test_unknown_variant_and_case_are_refused(workspace, monkeypatch):
    Gateway(monkeypatch)
    with pytest.raises(SystemExit):
        run(workspace, standard_source(), variant="nonsense")
    with pytest.raises(ValueError, match="unknown"):
        run(workspace, [source_row(case_id="ghost")])


def test_runs_and_cases_stay_separate_and_concurrency_is_accepted(workspace, monkeypatch):
    gateway = Gateway(monkeypatch)
    one = verified("A", True)
    rows = [source_row(case_id=c, run=r, raw=[one], reported=[one])
            for c in ("one", "two") for r in (1, 2)]
    run(workspace, rows, "--concurrency", "2")
    assert len(gateway.calls) == 4
    assert {(r["case_id"], r["run"]) for r in output_rows(workspace)} == {
        ("one", 1), ("one", 2), ("two", 1), ("two", 2)}
    only = Gateway(monkeypatch)
    workspace["out"].unlink()
    run(workspace, None, "--case-ids", "two")
    assert only.calls == []
    assert {r["case_id"] for r in output_rows(workspace)} == {"two"}


def test_concurrency_defaults_to_one(monkeypatch, workspace):
    sizes = []

    class RecordingPool(ThreadPoolExecutor):
        def __init__(self, workers):
            sizes.append(workers)
            super().__init__(workers)

    monkeypatch.setattr(reverify, "ThreadPoolExecutor", RecordingPool)
    Gateway(monkeypatch)
    run(workspace, standard_source())
    assert sizes == [1]


# Cache identity: nothing that changes the verifier input or the arm may share a verdict.
def test_a_finding_with_the_same_key_but_another_description_gets_its_own_hash(workspace, monkeypatch):
    Gateway(monkeypatch)
    run(workspace, standard_source())
    edited = standard_source()
    edited[0]["raw"][0]["description"] = "a different explanation"
    workspace["out"].unlink()
    again = Gateway(monkeypatch)
    run(workspace, edited)
    assert "A" in again.titles
    cached = [json.loads(line) for line in cache_file(workspace).read_text().splitlines()]
    hashes = {row["finding_sha256"] for row in cached if row["finding_key"] == cached[0]["finding_key"]}
    assert len(hashes) == 2


def cache_file(workspace):
    return workspace["out"].with_name("out.jsonl.verdicts")


def test_two_labels_with_the_same_variant_and_model_do_not_share_verdicts(workspace, monkeypatch):
    first = Gateway(monkeypatch)
    run(workspace, standard_source(), "--label", "noise-a")
    second = Gateway(monkeypatch)
    run(workspace, None, "--label", "noise-b")
    assert len(first.calls) == len(second.calls) == 3


def test_another_source_content_does_not_share_verdicts(workspace, monkeypatch):
    Gateway(monkeypatch)
    run(workspace, standard_source())
    workspace["out"].unlink()
    changed = standard_source()
    changed[0]["duration_s"] = 99.0
    again = Gateway(monkeypatch)
    run(workspace, changed)
    assert len(again.calls) == 3


def test_a_renamed_source_with_the_same_content_is_the_same_arm(workspace, monkeypatch):
    Gateway(monkeypatch)
    run(workspace, standard_source())
    again = Gateway(monkeypatch)
    assert run(workspace, standard_source(), source="renamed.jsonl") == 0
    assert again.calls == [] and len(output_rows(workspace)) == 1


def test_the_cache_rows_name_the_content_the_diff_and_the_head(workspace, monkeypatch):
    Gateway(monkeypatch)
    run(workspace, standard_source())
    content = (workspace["tmp"] / "source_results.jsonl").read_bytes()
    diff = compress_diff(FILES, review.DIFF_BUDGET_TOKENS, HeuristicEstimator()).prompt
    rows = [json.loads(line) for line in cache_file(workspace).read_text().splitlines()]
    assert {row["source_sha256"] for row in rows} == {hashlib.sha256(content).hexdigest()}
    assert {row["diff_sha256"] for row in rows} == {hashlib.sha256(diff.encode()).hexdigest()}
    assert {row["head_sha"] for row in rows} == {"def"}


def test_another_rendered_diff_does_not_share_verdicts(workspace, monkeypatch):
    Gateway(monkeypatch)
    run(workspace, standard_source())
    workspace["out"].unlink()
    monkeypatch.setattr(reverify, "compress_diff",
                        lambda *args, **kwargs: SimpleNamespace(prompt="another rendering"))
    again = Gateway(monkeypatch)
    run(workspace, None)
    assert len(again.calls) == 3


def test_an_edited_variant_definition_does_not_reuse_verdicts(workspace, monkeypatch):
    Gateway(monkeypatch)
    run(workspace, standard_source())
    workspace["out"].unlink()
    edited = replace(review.VERIFIER_VARIANTS["shipped"], system="a changed prompt")
    monkeypatch.setitem(review.VERIFIER_VARIANTS, "shipped", edited)
    again = Gateway(monkeypatch)
    run(workspace, None)
    assert len(again.calls) == 3


# Resume identity: one label is one arm.
def refused_restart(workspace, monkeypatch, capsys, *extra, before=None, **run_kwargs):
    gateway = Gateway(monkeypatch)
    run(workspace, standard_source())
    if before:
        before()
    calls = len(gateway.calls)
    out_before = workspace["out"].read_bytes()
    cache_before = cache_file(workspace).read_bytes()
    with pytest.raises(SystemExit) as stop:
        run(workspace, None, *extra, **run_kwargs)
    assert stop.value.code != 0
    assert "use another --label" in capsys.readouterr().err
    assert len(gateway.calls) == calls
    assert workspace["out"].read_bytes() == out_before and cache_file(workspace).read_bytes() == cache_before


@pytest.mark.parametrize("change", [
    {"variant": "evidence", "extra": ["--label", "shipped"]},
    {"model": "other-m"},
    {"extra": ["--bug-cap", "3"]},
])
def test_a_restart_with_another_arm_under_the_same_label_is_refused(workspace, monkeypatch, capsys, change):
    refused_restart(workspace, monkeypatch, capsys, *change.get("extra", []),
                    **{k: v for k, v in change.items() if k != "extra"})


def test_a_restart_with_another_source_variant_is_refused(workspace, monkeypatch, capsys):
    def swap_in_two_variants():
        write_results(workspace["tmp"] / "source_results.jsonl",
                      [*standard_source(), source_row(variant="candidate")])
    refused_restart(workspace, monkeypatch, capsys, "--source-variant", "candidate", before=swap_in_two_variants)


def test_a_restart_over_a_source_changed_in_place_is_refused(workspace, monkeypatch, capsys):
    def edit_source():
        changed = standard_source()
        changed[0]["duration_s"] = 99.0
        write_results(workspace["tmp"] / "source_results.jsonl", changed)
    refused_restart(workspace, monkeypatch, capsys, before=edit_source)


def test_a_restart_after_a_prompt_edit_is_refused_even_when_every_row_is_done(workspace, monkeypatch, capsys):
    def edit_prompt():
        edited = replace(review.VERIFIER_VARIANTS["shipped"], system="a changed prompt")
        monkeypatch.setitem(review.VERIFIER_VARIANTS, "shipped", edited)
    refused_restart(workspace, monkeypatch, capsys, before=edit_prompt)


def test_a_restart_after_a_diff_budget_change_is_refused(workspace, monkeypatch, capsys):
    refused_restart(workspace, monkeypatch, capsys,
                    before=lambda: monkeypatch.setattr(review, "DIFF_BUDGET_TOKENS", 1234))


def test_rows_written_before_the_arm_hash_existed_are_refused(workspace, monkeypatch, capsys):
    def strip_arm():
        rows = output_rows(workspace)
        for row in rows:
            del row["arm_sha256"]
        workspace["out"].write_text("".join(json.dumps(row) + "\n" for row in rows))
    refused_restart(workspace, monkeypatch, capsys, before=strip_arm)


def test_every_row_carries_the_same_arm_hash(workspace, monkeypatch):
    Gateway(monkeypatch)
    rows = [source_row(case_id=c, raw=[verified("A", True)], reported=[]) for c in ("one", "two")]
    run(workspace, rows)
    hashes = {row["arm_sha256"] for row in output_rows(workspace)}
    assert len(hashes) == 1 and len(hashes.pop()) == 64


def test_a_restart_with_the_same_arm_is_accepted(workspace, monkeypatch):
    Gateway(monkeypatch)
    run(workspace, standard_source(), "--label", "arm")
    again = Gateway(monkeypatch)
    assert run(workspace, None, "--label", "arm") == 0
    assert again.calls == []


# Exit status.
def test_the_exit_status_is_one_when_an_error_row_was_written(workspace, monkeypatch, capsys):
    Gateway(monkeypatch, fail_at="B")
    assert run(workspace, standard_source()) == 1
    err = capsys.readouterr().err
    assert "1 of 1 rows are error rows" in err and "RuntimeError: gateway down" in err
    assert "Delete an error row" in err
    assert output_rows(workspace)[0]["error"]["class"] == "RuntimeError"
    Gateway(monkeypatch)
    assert run(workspace, None) == 0


def test_a_carried_source_error_also_gives_exit_status_one(workspace, monkeypatch):
    Gateway(monkeypatch)
    assert run(workspace, [source_row(error={"class": "OSError", "message": "gone"})]) == 1


def test_the_exit_status_is_zero_when_every_row_succeeds(workspace, monkeypatch):
    Gateway(monkeypatch)
    assert run(workspace, standard_source()) == 0


# Reviewer and model provenance.
def test_a_rerun_of_a_rerun_keeps_the_original_reviewer(workspace, monkeypatch):
    Gateway(monkeypatch)
    run(workspace, standard_source())
    first = output_rows(workspace)[0]
    assert first["reviewer_model"] == "reviewer-m"
    second_out = workspace["tmp"] / "second.jsonl"
    second = {**workspace, "out": second_out}
    run(second, [first], "--label", "again", source="first.jsonl")
    assert json.loads(second_out.read_text().splitlines()[0])["reviewer_model"] == "reviewer-m"


def test_the_model_defaults_to_the_one_in_the_source_rows(workspace, monkeypatch):
    gateway = Gateway(monkeypatch)
    run(workspace, standard_source(), model=None)
    assert {call["model"] for call in gateway.calls} == {"reviewer-m"}
    row = output_rows(workspace)[0]
    assert row["verify_model"] == "reviewer-m" and row["model"] == "reviewer-m"


def test_disagreeing_or_missing_source_models_require_a_model(workspace, monkeypatch):
    Gateway(monkeypatch)
    mixed = [source_row(case_id="one"), source_row(case_id="two", model="other-m")]
    with pytest.raises(SystemExit):
        run(workspace, mixed, model=None)
    missing = source_row()
    del missing["model"]
    with pytest.raises(SystemExit):
        run(workspace, [missing], model=None)
    assert not workspace["out"].exists() or workspace["out"].read_text() == ""


# The variant hash covers the rendered call, not only the system prompt and the schema.
def test_a_checklist_edit_changes_the_checklist_variant_hash_only(monkeypatch):
    before = {name: reverify._variant_sha256(name) for name in review.VERIFIER_VARIANTS}
    assert len(set(before.values())) == len(before)
    edited = {**review.VERIFY_CHECKLISTS, "resources": "a changed checklist"}
    monkeypatch.setattr(review, "VERIFY_CHECKLISTS", edited)
    after = {name: reverify._variant_sha256(name) for name in review.VERIFIER_VARIANTS}
    changed = {name for name in before if before[name] != after[name]}
    assert changed == {"default_keep_checklists"}


def test_the_variant_hash_is_stable_across_calls():
    assert reverify._variant_sha256("evidence") == reverify._variant_sha256("evidence")


# Coverage counters come from the new verdicts.
def voiding_source():
    coverage = [{"lane": "llm", "status": "ran", "model": "reviewer-m", "verify_variant": "shipped",
                 **STALE_COUNTS}]
    raw = [verified("A", True), verified("B", True, line=2)]
    return [source_row(raw=raw, reported=raw, coverage=coverage)]


def llm_record(row):
    return next(lane for lane in row["coverage"] if lane["lane"] == "llm")


def test_a_checking_variant_counts_voided_refutations_in_the_rerun_row(workspace, monkeypatch):
    Gateway(monkeypatch, answers={"A": False}, extra={"counter_evidence": ABSENT_QUOTE})
    run(workspace, voiding_source(), variant="default_keep")
    [row] = output_rows(workspace)
    record = llm_record(row)
    assert (record["refutations_voided"], record["refutations_voided_unreadable"]) == (1, 0)
    refuted = row["raw"][0]["verdict"]
    assert refuted["is_real"] is True and refuted["refutation_void_kind"] == "unconfirmed"
    assert "absent from a.py" in refuted["refutation_voided"]


def test_the_shipped_variant_carries_no_voided_counts(workspace, monkeypatch):
    Gateway(monkeypatch)
    run(workspace, voiding_source(), variant="shipped")
    record = llm_record(output_rows(workspace)[0])
    assert "refutations_voided" not in record and "refutations_voided_unreadable" not in record


def test_a_refutation_that_quotes_the_head_is_not_counted(workspace, monkeypatch):
    Gateway(monkeypatch, answers={"A": False}, extra={"counter_evidence": QUOTE})
    run(workspace, voiding_source(), variant="default_keep")
    [row] = output_rows(workspace)
    assert llm_record(row)["refutations_voided"] == 0
    assert row["raw"][0]["verdict"]["is_real"] is False


# The server fields survive the cache, which reads its own dicts and never strips them.
def test_void_fields_survive_a_cache_reload(workspace, monkeypatch):
    Gateway(monkeypatch, answers={"A": False}, extra={"counter_evidence": ABSENT_QUOTE})
    run(workspace, voiding_source(), variant="default_keep")
    first = output_rows(workspace)[0]
    workspace["out"].unlink()
    again = Gateway(monkeypatch)
    run(workspace, None, variant="default_keep")
    [second] = output_rows(workspace)
    assert again.calls == []
    assert second["raw"] == first["raw"]
    assert second["raw"][0]["verdict"]["refutation_void_kind"] == "unconfirmed"
    assert llm_record(second)["refutations_voided"] == 1


def test_the_cache_returns_server_fields_unchanged(tmp_path):
    key = sample_key()
    voided = CounterEvidenceVerdict(is_real=True, confidence=0.5, counter_evidence=ABSENT_QUOTE,
                                    refutation_voided="the quote is absent",
                                    refutation_void_kind="unconfirmed")
    reverify.VerdictCache(tmp_path / "cache").put(key, voided)
    loaded = reverify.VerdictCache(tmp_path / "cache").get(key)
    assert loaded == voided


# Snapshot binding.
@pytest.mark.parametrize("field", ["head_sha", "base_sha", "repo"])
def test_a_snapshot_with_another_commit_than_the_source_row_is_an_error_row(workspace, monkeypatch, field):
    gateway = Gateway(monkeypatch)
    row = standard_source()[0]
    row["snapshot"][field] = "moved"
    run(workspace, [row])
    [written] = output_rows(workspace)
    assert gateway.calls == [] and field in written["error"]["message"]


# The tool schema order is part of a variant.
def test_property_order_alone_changes_the_variant_hash(monkeypatch):
    shipped = review.VERIFIER_VARIANTS["shipped"]
    schema = shipped.tool.input_schema
    reordered = {**schema, "properties": dict(reversed(list(schema["properties"].items())))}
    assert json.dumps(reordered, sort_keys=True) == json.dumps(schema, sort_keys=True)
    monkeypatch.setitem(review.VERIFIER_VARIANTS, "evidence",
                        replace(shipped, tool=replace(shipped.tool, input_schema=reordered)))
    assert reverify._variant_sha256("evidence") != reverify._variant_sha256("shipped")


def test_shipped_and_reasoning_first_differ_in_order_only():
    shipped = review.VERIFIER_VARIANTS["shipped"].tool.input_schema
    reasoning_first = review.VERIFIER_VARIANTS["reasoning_first"].tool.input_schema

    def normal(schema):
        return {**schema, "properties": dict(sorted(schema["properties"].items())),
                "required": sorted(schema["required"])}

    assert normal(shipped) == normal(reasoning_first)
    assert reverify._variant_sha256("shipped") != reverify._variant_sha256("reasoning_first")


# A source row without a model cannot supply the default.
def test_one_source_row_without_a_model_requires_a_model(workspace, monkeypatch):
    Gateway(monkeypatch)
    missing = source_row(case_id="two")
    del missing["model"]
    with pytest.raises(SystemExit):
        run(workspace, [source_row(case_id="one"), missing], model=None)


# An error row after a good snapshot load keeps the snapshot, like a replay error row.
def test_a_verifier_failure_keeps_the_validated_snapshot_on_the_error_row(workspace, monkeypatch):
    Gateway(monkeypatch, fail_at="B")
    source = standard_source()
    assert run(workspace, source) == 1
    [row] = output_rows(workspace)
    assert row["error"]["class"] == "RuntimeError"
    assert row["snapshot"] == source[0]["snapshot"]
    cases = load_cases(workspace["cases"])
    scored = score_results(cases, [row])["variants"]["shipped"]["overall"]
    assert scored["error_rate"] == 1.0
    assert scored["inline"]["unassessed_reviews"] == 0
    assert scored["recall"]["0"]["mean"]["recall"] == 0.0


def test_a_snapshot_mismatch_error_row_still_has_no_snapshot(workspace, monkeypatch):
    Gateway(monkeypatch)
    row = standard_source()[0]
    row["snapshot"]["head_sha"] = "moved"
    run(workspace, [row])
    assert output_rows(workspace)[0]["snapshot"] is None
