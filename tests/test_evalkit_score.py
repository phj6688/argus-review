import hashlib
import json
from dataclasses import replace

import pytest

from argus.evalkit.cases import Case, Expectation
from argus.evalkit.score import (cluster_bootstrap, duplicate_counts, finding_key, location_match,
                                 main, markdown_report, mcnemar_exact, minimum_detectable_difference,
                                 score_results,
                                 semantic_hit_table, wilson_interval)
from test_evalkit_cases import case_data, write_cases


def expectation(id="e1", line=10, weight=1.0, source="manual", severity="major"):
    return Expectation(id, "a.py", line, line, severity, "logic", "Bad branch", source, weight)


def finding(line=10, title="Bad branch", lane="llm", severity="major", file="a.py"):
    return {"file": file, "line": line, "title": title, "description": "Wrong result",
            "severity": severity, "lane": lane}


def row(variant="a", run=1, findings=None, error=None, case_id="c"):
    return {"variant": variant, "case_id": case_id, "run": run,
            "reported": findings or [], "error": error, "duration_s": 2.0,
            "coverage": [{"lane": "llm", "status": "ran", "files_omitted": 0}],
            "snapshot": {"repo": "o/r", "base_sha": "abc", "head_sha": "def",
                         "patch_sha256": "fixed", "files": [
                             {"filename": "a.py", "status": "modified",
                              "patch": "@@ -9,3 +9,3 @@\n ctx\n-old\n+new\n ctx"}]}}


def test_location_windows_and_stable_key():
    e = expectation()
    assert location_match(e, finding(13, file="/./a.py"), 3)
    assert not location_match(e, finding(14), 3)
    assert not location_match(e, finding(10, file="other"), 10)
    assert not location_match(e, finding(0), 10)
    expected = hashlib.sha256(b"c|a.py|10|bad branch").hexdigest()
    assert finding_key("c", finding(title="  BAD   branch ", file="./a.py")) == expected


def test_wilson_and_exact_mcnemar_hand_computed():
    assert wilson_interval(5, 10) == pytest.approx((0.236593, 0.763407), abs=1e-6)
    assert wilson_interval(0, 0) == (None, None)
    assert mcnemar_exact(0, 0) == 1.0
    assert mcnemar_exact(0, 3) == 0.25
    assert mcnemar_exact(1, 3) == 0.625
    assert mcnemar_exact(2, 2) == 1.0


def test_duplicate_pairs_are_within_runs_and_normalize_paths():
    findings = [finding(10, "Bad branch returns null", file="./a.py"),
                finding(12, "Bad branch returns nil", file="/a.py"),
                finding(15, "Bad branch returns nil"), finding(10, "Timeout missing")]
    # Pairs 0/1 and 1/2 qualify, but 0/2 is five lines apart.
    assert duplicate_counts(findings) == (2, 6)


def test_metrics_windows_runs_weights_negatives_and_noise():
    case = Case("c", "o/r", 1, "abc", "def", "heldout",
                [expectation(), expectation("e2", 20, 3, "incident", "minor")],
                [replace(expectation("n", 12), source="known_fp")])
    records = [row(findings=[finding(), finding(12, "Bad branch"), finding(23, "Other issue", "probe")]),
               row(run=2, findings=[finding(20, "Other issue", "probe", "minor")])]
    metrics = score_results([case], records, [0, 3, 10])["variants"]["a"]["overall"]
    exact = metrics["recall"]["0"]
    assert exact["mean"]["recall"] == 0.5
    assert exact["mean"]["weighted_recall"] == 0.5
    assert exact["any_run"]["recall"] == 1.0
    near = metrics["recall"]["3"]
    assert near["mean"]["recall"] == 0.75
    assert near["mean"]["weighted_recall"] == pytest.approx(0.875)
    assert metrics["volume"]["overall"] == {"mean": 2.0, "median": 2.0, "p90": 2.8}
    assert metrics["volume"]["by_lane"]["probe"]["mean"] == 1.0
    assert metrics["changed_line_share"] == 0.25
    assert metrics["duplicate_pairs"] == 1
    assert metrics["duplicate_rate"] == pytest.approx(1 / 3)
    assert metrics["negatives_hit_rate"] == 0.5
    assert metrics["negatives_any_run_hit_rate"] == 1.0
    assert metrics["severity_distribution"] == {"major": 3, "minor": 1}
    assert metrics["coverage_levels"] == {"full": 2}
    assert metrics["mean_duration_s"] == 2.0
    assert metrics["error_rate"] == 0.0


def test_errors_and_missing_snapshots_are_visible():
    case = Case("c", "o/r", 1, "abc", "def", "dev", [expectation()])
    record = row(error={"class": "RuntimeError"})
    record["snapshot"] = None
    record["coverage"] = []
    metrics = score_results([case], [record], [3])["variants"]["a"]["overall"]
    assert metrics["error_rate"] == 1.0
    assert metrics["recall"]["3"]["mean"]["recall"] == 0.0
    assert metrics["coverage_levels"] == {"none": 1}
    assert metrics["changed_line_share"] is None


def test_semantic_pairs_and_cli_outputs(tmp_path):
    cases = write_cases(tmp_path / "cases", [case_data("c")])
    records = [row(findings=[finding(13)]), row("b", findings=[finding(10)])]
    for record in records:
        record["snapshot"]["repo"] = "owner/project"
    results = tmp_path / "results"
    results.write_text("".join(json.dumps(r) + "\n" for r in records))
    semantic = tmp_path / "semantic"
    semantic.write_text(json.dumps({"expectation_id": "c-e1",
                                   "finding_key": finding_key("c", finding(13)), "same_issue": True}) + "\n")
    json_out, md_out, pairs = tmp_path / "report.json", tmp_path / "report.md", tmp_path / "pairs"
    assert main(["--cases", str(cases), "--results", str(results), "--out-json", str(json_out),
                 "--out-md", str(md_out), "--semantic-cache", str(semantic),
                 "--emit-pairs", str(pairs), "--split", "heldout"]) == 0
    report = json.loads(json_out.read_text())
    assert report["variants"]["a"]["overall"]["recall"]["3"]["confirmed"]["mean"]["recall"] == 1
    assert report["variants"]["b"]["overall"]["recall"]["3"]["confirmed"]["mean"]["recall"] == 0
    candidates = [json.loads(line) for line in pairs.read_text().splitlines()]
    assert len(candidates) == 2
    assert candidates[0]["expectation"]["summary"] == "Bad branch"
    assert candidates[0]["finding"]["description"] == "Wrong result"
    assert "baseline" not in md_out.read_text()
    assert "a" in md_out.read_text() and "Recall" in md_out.read_text()


def test_paired_comparison_gains_losses_and_shared_runs():
    cases = [Case("c", "o/r", 1, "abc", "def", "dev", [expectation("e1"), expectation("e2", 20),
                                                         expectation("e3", 30), expectation("e4", 40)])]
    records = [row("a", findings=[finding(10)]),
               row("b", findings=[finding(20), finding(30), finding(40)]),
               row("a", 2, [finding(20), finding(30), finding(40)])]
    comparison = score_results(cases, records, [0, 3])["comparisons"][0]
    assert comparison["a_only"] == 1 and comparison["b_only"] == 3
    assert comparison["p_value"] == 0.625
    assert comparison["shared_case_runs"] == 1
    assert comparison["gained"] == [{"case_id": "c", "expectation_id": x} for x in ["e2", "e3", "e4"]]
    assert comparison["lost"] == [{"case_id": "c", "expectation_id": "e1"}]


def test_input_mismatch_and_conflicting_duplicate_results_rejected():
    cases = [Case("c", "o/r", 1, "abc", "def", "dev", [expectation()])]
    different = row("b")
    different["snapshot"]["patch_sha256"] = "different"
    with pytest.raises(ValueError, match="snapshot.*c"):
        score_results(cases, [row(), different], [3])
    with pytest.raises(ValueError, match="duplicate"):
        score_results(cases, [row(), row(findings=[finding()])], [3])
    report = score_results(cases, [row(), row()], [3])
    assert report["variants"]["a"]["overall"]["runs"] == 1


def test_group_breakdowns_and_unevaluated_cases():
    cases = [Case("c", "o/r", 1, "abc", "def", "dev", [expectation()]),
             Case("missing", "o/r", 2, "abc", "def", "heldout", [expectation("e2")])]
    variant = score_results(cases, [row(findings=[finding()])], [3])["variants"]["a"]
    assert variant["unevaluated_cases"] == ["missing"]
    for key, group in [("by_split", "dev"), ("by_source", "manual"),
                       ("by_severity", "major"), ("by_cls", "logic")]:
        assert variant[key][group]["recall"]["3"]["mean"]["recall"] == 1.0


def test_large_and_zero_weights_remain_finite():
    case = Case("c", "o/r", 1, "abc", "def", "dev",
                [expectation("e1", 10, 1e308), expectation("e2", 20, 1e308)])
    metrics = score_results([case], [row(findings=[finding()])], [0])["variants"]["a"]["overall"]
    assert metrics["recall"]["0"]["mean"]["weighted_recall"] == 0.5
    assert all(x is not None for x in metrics["recall"]["0"]["mean"]["weighted_wilson95"])
    json.dumps(metrics, allow_nan=False)
    zero = replace(case, expected=[replace(e, weight=0.0) for e in case.expected])
    metrics = score_results([zero], [row(findings=[finding()])], [0])["variants"]["a"]["overall"]
    assert metrics["recall"]["0"]["mean"]["weighted_recall"] is None


def test_semantic_cache_conflicts_fail_and_snapshot_contents_are_ignored(tmp_path):
    from argus.evalkit.score import _semantic_cache
    decisions = tmp_path / "semantic"
    decisions.write_text('\n'.join(json.dumps({"expectation_id": "e1", "finding_key": "key", "same_issue": v})
                                   for v in [True, False]))
    with pytest.raises(ValueError, match="conflicting semantic"):
        _semantic_cache(decisions)
    cases = [Case("c", "o/r", 1, "abc", "def", "dev", [expectation()])]
    a, b = row(), row("b")
    a["snapshot"]["contents"] = {"context.py": "old"}
    b["snapshot"]["contents"] = {"context.py": "new"}
    assert score_results(cases, [a, b], [3])["variants"].keys() == {"a", "b"}


@pytest.mark.parametrize("line", [None, 0, -1, True, "10", 10.0])
def test_invalid_lines_are_unassessed_and_not_duplicates(line):
    case = Case("c", "o/r", 1, "abc", "def", "dev", [expectation()])
    findings = [finding(line), finding()]
    assert not location_match(expectation(), findings[0], 3)
    assert duplicate_counts(findings) == (0, 1)
    metrics = score_results([case], [row(findings=findings)], [0])["variants"]["a"]["overall"]
    assert metrics["changed_line_share"] == 1.0
    assert metrics["changed_line_assessed_findings"] == 1
    assert metrics["changed_line_unassessed_findings"] == 1


def judged(case_id, pairs):
    return {(expectation_id, finding_key(case_id, f)): same for expectation_id, f, same in pairs}


def ten_expectation_case(case_id="c", pr=1):
    return Case(case_id, "o/r", pr, "abc", "def", "heldout",
                [expectation(f"{case_id}-e{i}", 10 * (i + 1)) for i in range(10)])


def hit_rows(case, variant, run, hit_indexes):
    findings = [finding(10 * (i + 1)) for i in hit_indexes]
    return row(variant, run, findings, case_id=case.case_id)


def confirm_all(case):
    return judged(case.case_id, [(e.id, finding(e.start_line), True) for e in case.expected])


def test_semantic_hit_table_keeps_each_run_separate():
    case = Case("c", "o/r", 1, "abc", "def", "heldout", [expectation()])
    first, second = finding(10, "Bad branch"), finding(11, "Bad branch twice")
    semantic = judged("c", [("e1", first, True), ("e1", second, False)])
    table = semantic_hit_table([case], [row(findings=[first]), row(run=2, findings=[second])], semantic)
    assert table[("a", "c", 1)] == {"e1": True}
    assert table[("a", "c", 2)] == {"e1": False}


def test_semantic_hit_table_counts_errors_and_unjudged_findings_as_misses():
    case = Case("c", "o/r", 1, "abc", "def", "heldout", [expectation()])
    semantic = judged("c", [("e1", finding(), True)])
    records = [row(findings=[finding()], error={"class": "RuntimeError"}),
               row(run=2, findings=[finding(12, "Unjudged")])]
    table = semantic_hit_table([case], records, semantic)
    assert table[("a", "c", 1)] == {"e1": False}
    assert table[("a", "c", 2)] == {"e1": False}


def test_semantic_run_pair_mcnemar_two_against_eight():
    case = ten_expectation_case()
    records = [hit_rows(case, "a", 1, [0, 1]), hit_rows(case, "b", 1, range(2, 10))]
    report = score_results([case], records, [3], confirm_all(case))
    pair = report["semantic_comparisons"][0]["run_pairs"][0]
    assert (pair["a_run"], pair["b_run"]) == (1, 1)
    assert (pair["a_only"], pair["b_only"], pair["both"], pair["neither"]) == (2, 8, 0, 0)
    assert pair["p_value"] == pytest.approx(0.1094, abs=5e-5)
    assert pair["p_value"] == mcnemar_exact(2, 8)


def test_minimum_detectable_difference_follows_the_half_split_rule():
    assert minimum_detectable_difference(10) == 10
    assert minimum_detectable_difference(0) == 6
    assert minimum_detectable_difference(9) == 10
    assert mcnemar_exact(5 + 10, 5) < 0.05 <= mcnemar_exact(5 + 9, 5)


def test_a_a_noise_reports_discordant_count_and_minimum_detectable_difference():
    case = ten_expectation_case()
    records = [hit_rows(case, "a", 1, range(5)), hit_rows(case, "a", 2, range(5, 10))]
    noise = score_results([case], records, [3], confirm_all(case))["semantic_noise"]["a"]
    assert noise == [{"runs": [1, 2], "expectations": 10, "discordant": 10, "first_only": 5,
                      "second_only": 5, "p_value": 1.0, "minimum_detectable_difference": 10}]


def test_cluster_bootstrap_is_reproducible_for_a_fixed_seed():
    clusters = [[1.0, 0.0], [-1.0], [0.0, 0.0, 1.0], [1.0], [0.0]]
    first = cluster_bootstrap(clusters, seed=7)
    assert first == cluster_bootstrap(clusters, seed=7)
    assert first[0] <= 0.25 <= first[1]
    assert cluster_bootstrap([], seed=7) == (None, None)


def test_bootstrap_clusters_cases_of_one_pull_request():
    cases = [Case(case_id, "o/r", pr, "abc", "def", "heldout", [expectation(f"{case_id}-e0")])
             for case_id, pr in (("c1", 5), ("c2", 5), ("c3", 6))]
    records = [r for case in cases for r in (hit_rows(case, "a", 1, []), hit_rows(case, "b", 1, [0]))]
    for record in records:
        record["snapshot"] = None
    semantic = {k: v for case in cases for k, v in confirm_all(case).items()}
    bootstrap = score_results(cases, records, [3], semantic)["semantic_comparisons"][0]["bootstrap"]
    assert bootstrap["clusters"] == 2
    assert bootstrap["expectations"] == 3
    assert bootstrap["resamples"] == 2000
    assert bootstrap["recall_difference"] == 1.0
    assert bootstrap["ci95"] == [1.0, 1.0]


def test_semantic_blocks_are_absent_without_a_judge_cache():
    case = ten_expectation_case()
    report = score_results([case], [hit_rows(case, "a", 1, [0]), hit_rows(case, "b", 1, [1])], [3])
    assert "semantic_comparisons" not in report and "semantic_noise" not in report


def test_cli_two_runs_per_variant_prints_run_pairs_bootstrap_and_detectable_difference(tmp_path):
    data = case_data("c")
    cases = write_cases(tmp_path / "cases", [data])
    records = [row("a", 1, [finding(10)]), row("a", 2, []), row("b", 1, [finding(10)]),
               row("b", 2, [finding(11)])]
    for record in records:
        record["snapshot"]["repo"] = "owner/project"
    results = tmp_path / "results"
    results.write_text("".join(json.dumps(r) + "\n" for r in records))
    semantic = tmp_path / "semantic"
    semantic.write_text("".join(json.dumps({"expectation_id": "c-e1", "finding_key": finding_key("c", f),
                                            "same_issue": True}) + "\n" for f in (finding(10), finding(11))))
    json_out, md_out = tmp_path / "report.json", tmp_path / "report.md"
    assert main(["--cases", str(cases), "--results", str(results), "--out-json", str(json_out),
                 "--out-md", str(md_out), "--semantic-cache", str(semantic)]) == 0
    report = json.loads(json_out.read_text())
    comparison = report["semantic_comparisons"][0]
    assert [(p["a_run"], p["b_run"]) for p in comparison["run_pairs"]] == [(1, 1), (1, 2), (2, 1), (2, 2)]
    assert comparison["bootstrap"]["recall_difference"] == 0.5
    markdown = md_out.read_text()
    section = markdown.split("## Semantic run pairs", 1)[1]
    assert sum(line.startswith("| a | b | ") for line in section.splitlines()) == 4
    assert section.count("Cluster bootstrap") == 1
    assert "| a | 1, 2 | 1 | 7 |" in section
    assert "| b | 1, 2 | 0 | 6 |" in section


@pytest.mark.parametrize("arguments", [(-1, 0.05), (2.0, 0.05), (True, 0.05), (10, 0), (10, 1),
                                       (10, -0.1), (10, float("nan")), (10, float("inf")), (10, "0.05")])
def test_minimum_detectable_difference_rejects_invalid_arguments(arguments):
    with pytest.raises(ValueError):
        minimum_detectable_difference(*arguments)


def disjoint_runs(variant):
    return [row(variant, 1, [finding(10)], case_id="c1"), row(variant, 2, [finding(10)], case_id="c2")]


def one_expectation_cases(*ids_and_prs):
    return [Case(case_id, "o/r", pr, "abc", "def", "heldout", [expectation(f"{case_id}-e0")])
            for case_id, pr in ids_and_prs]


def test_runs_without_shared_expectations_report_no_test(tmp_path):
    cases = one_expectation_cases(("c1", 1), ("c2", 2))
    semantic = {k: v for case in cases for k, v in confirm_all(case).items()}
    records = disjoint_runs("a") + disjoint_runs("b")
    for record in records:
        record["snapshot"] = None
    report = score_results(cases, records, [3], semantic)
    noise = report["semantic_noise"]["a"][0]
    assert noise["expectations"] == 0
    assert noise["discordant"] is None and noise["p_value"] is None
    assert noise["minimum_detectable_difference"] is None
    pairs = {(p["a_run"], p["b_run"]): p for p in report["semantic_comparisons"][0]["run_pairs"]}
    assert pairs[1, 2]["expectations"] == 0 and pairs[1, 2]["p_value"] is None
    assert pairs[1, 1]["expectations"] == 1 and pairs[1, 1]["p_value"] == 1.0
    markdown = markdown_report(report)
    assert "| a | b | 1 | 2 | 0 | 0 | 0 | n/a (no shared expectations) |" in markdown
    assert "| a | 1, 2 | n/a | n/a (no shared expectations) |" in markdown


def test_bootstrap_pools_expectations_across_unequal_pr_clusters():
    cases = one_expectation_cases(("c1", 5), ("c2", 5), ("c3", 5), ("c4", 6), ("c5", 7), ("c6", 8))
    semantic = {k: v for case in cases for k, v in confirm_all(case).items()}
    a_hits, b_hits = {"c4"}, {"c1", "c2", "c3"}
    records = [row(variant, 1, [finding(10)] if case.case_id in hits else [], case_id=case.case_id)
               for case in cases for variant, hits in (("a", a_hits), ("b", b_hits))]
    for record in records:
        record["snapshot"] = None
    bootstrap = score_results(cases, records, [3], semantic)["semantic_comparisons"][0]["bootstrap"]
    assert bootstrap["clusters"] == 4
    # Pooled over expectations this is 2/6; a mean of cluster means would give 0 and 0.75.
    assert bootstrap["recall_difference"] == pytest.approx(1 / 3)
    assert bootstrap["ci95"][1] == pytest.approx(0.9)


def test_semantic_definitions_appear_only_with_a_judge_cache():
    case = ten_expectation_case()
    records = [hit_rows(case, "a", 1, [0]), hit_rows(case, "b", 1, [1])]
    plain = score_results([case], records, [3])["definitions"]
    judged_definitions = score_results([case], records, [3], confirm_all(case))["definitions"]
    new = {"semantic_run_pairs", "semantic_bootstrap", "minimum_detectable_difference"}
    assert not new & plain.keys()
    assert judged_definitions.keys() == plain.keys() | new
    assert "rounded up" in judged_definitions["minimum_detectable_difference"]


def postable(count, case_id="c", head="def", findings=None):
    record = row(case_id=case_id, findings=findings if findings is not None else [
        {**finding(10, title=str(i)), "confidence": i / 10} for i in range(count)])
    record["snapshot"]["head_sha"] = head
    return record


def test_shared_inline_selection_keeps_the_publisher_cap():
    from argus.evalkit import weekly
    from argus.evalkit.findings import inline_findings
    assert weekly.inline_findings is inline_findings
    selected = inline_findings(postable(6))
    assert [f["title"] for f in selected] == ["2", "3", "4", "5"]
    assert inline_findings(row(error={"class": "RuntimeError"})) == []
    missing = row()
    missing["snapshot"] = None
    assert inline_findings(missing) is None


def test_inline_counts_per_review_and_per_pr_add_all_heads():
    cases = [Case("c1", "o/r", 7, "abc", "def", "dev", [expectation()]),
             Case("c2", "o/r", 7, "abc", "fed", "dev", [expectation("e2")])]
    rows = [postable(6, "c1", "def"), postable(1, "c2", "fed")]
    inline = score_results(cases, rows, [3])["variants"]["a"]["overall"]["inline"]
    assert inline["per_review"] == {"mean": 2.5, "median": 2.5, "p90": pytest.approx(3.7),
                                    "n": 2, "total": 5}
    assert inline["per_pr"] == {"mean": 5.0, "median": 5.0, "p90": 5.0, "n": 1, "total": 5.0}
    assert inline["unassessed_reviews"] == 0 and inline["unassessed_prs"] == 0


def test_inline_per_pr_averages_runs_and_keeps_direct_commits_apart():
    cases = [Case("c1", "o/r", 7, "abc", "def", "dev", [expectation()]),
             Case("c2", "o/r", 7, "abc", "fed", "dev", [expectation("e2")]),
             Case("d1", "o/r", 0, "abc", "aaa", "dev", [expectation("e3")]),
             Case("d2", "o/r", 0, "abc", "bbb", "dev", [expectation("e4")])]
    first, second = postable(4, "c1", "def"), postable(2, "c1", "def")
    second["run"] = 2
    error = postable(3, "c2", "fed")
    error["error"] = {"class": "RuntimeError"}
    rows = [first, second, error, postable(1, "d1", "aaa"), postable(3, "d2", "bbb")]
    inline = score_results(cases, rows, [3])["variants"]["a"]["overall"]["inline"]
    assert inline["per_review"]["n"] == 5 and inline["per_review"]["total"] == 10
    assert inline["per_pr"]["n"] == 3 and inline["per_pr"]["total"] == 7.0
    assert inline["per_pr"]["mean"] == pytest.approx(7 / 3)
    assert inline["per_pr"]["median"] == 3.0


def test_inline_counts_exclude_unrenderable_rows_and_lines():
    cases = [Case("c1", "o/r", 7, "abc", "def", "dev", [expectation()]),
             Case("c2", "o/r", 7, "abc", "fed", "dev", [expectation("e2")]),
             Case("c3", "o/r", 8, "abc", "aaa", "dev", [expectation("e3")])]
    missing = postable(2, "c2", "fed")
    missing["snapshot"] = None
    invalid = postable(0, "c3", "aaa", [finding(line, title=str(line)) for line in (None, "10", 10.0, 10)])
    rows = [postable(2, "c1", "def"), missing, invalid]
    inline = score_results(cases, rows, [3])["variants"]["a"]["overall"]["inline"]
    assert inline["per_review"]["n"] == 2 and inline["per_review"]["total"] == 3
    assert inline["unassessed_reviews"] == 1
    assert inline["per_pr"]["n"] == 1 and inline["per_pr"]["total"] == 1.0
    assert inline["unassessed_prs"] == 1


def test_inline_counts_follow_severity_groups_and_reach_the_report():
    from argus.evalkit.score import markdown_report
    cases = [Case("c", "o/r", 1, "abc", "def", "dev", [expectation()])]
    findings = [finding(10, "a"), finding(10, "b", severity="minor"), finding(10, "c", severity="nit")]
    report = score_results(cases, [row(findings=findings)], [3])
    variant = report["variants"]["a"]
    assert variant["overall"]["inline"]["per_review"]["total"] == 2
    assert variant["by_severity"]["minor"]["inline"]["per_review"]["total"] == 1
    assert variant["by_severity"]["nit"]["inline"]["per_review"]["total"] == 0
    assert "inline" in report["definitions"]
    text = markdown_report(report)
    assert "Inline per review (case/run)" in text
    assert "Inline per PR (sum over heads of each head's mean over runs)" in text
    assert "| 2.000 / 2.000 / 2.000 | 1 | 2.000 / 2.000 / 2.000 | 1 | 0 | 0 |" in text


def test_inline_counts_follow_emitted_comments_when_findings_share_a_key():
    from argus.evalkit.findings import finding_key
    shared = [{**finding(10), "lane": lane, "confidence": i / 10}
              for i, lane in enumerate(["llm", "rules", "llm", "rules", "llm", "rules"])]
    assert len({finding_key("c", f) for f in shared}) == 1
    cases = [Case("c", "o/r", 1, "abc", "def", "dev", [expectation()])]
    inline = score_results(cases, [row(findings=shared)], [3])["variants"]["a"]["overall"]["inline"]
    assert inline["per_review"]["total"] == 4
    assert inline["per_pr"]["total"] == 4.0


def test_inline_counts_mark_prs_with_an_unevaluated_head_unassessed():
    cases = [Case("c1", "o/r", 7, "abc", "def", "dev", [expectation()]),
             Case("c2", "o/r", 7, "abc", "fed", "dev", [expectation("e2")]),
             Case("c3", "o/r", 8, "abc", "aaa", "dev", [expectation("e3")])]
    rows = [postable(2, "c1", "def"), postable(1, "c3", "aaa")]
    report = score_results(cases, rows, [3])
    inline = report["variants"]["a"]["overall"]["inline"]
    assert inline["per_review"]["n"] == 2 and inline["unassessed_reviews"] == 0
    assert inline["per_pr"]["n"] == 1 and inline["per_pr"]["total"] == 1.0
    assert inline["unassessed_prs"] == 1
    assert "sums over the group's heads" in report["definitions"]["inline"]


@pytest.mark.parametrize("end_line", [None, "11", 10.5, True, 0])
def test_inline_counts_skip_malformed_end_lines(end_line):
    cases = [Case("c", "o/r", 1, "abc", "def", "dev", [expectation()])]
    findings = [{**finding(10, "bad"), "end_line": end_line}, {**finding(10, "good"), "end_line": 11}]
    inline = score_results(cases, [row(findings=findings)], [3])["variants"]["a"]["overall"]["inline"]
    assert inline["per_review"]["total"] == (2 if end_line is None else 1)


def test_inline_counts_mark_prs_without_any_row_unassessed():
    cases = [Case("c1", "o/r", 7, "abc", "def", "dev", [expectation()]),
             Case("c2", "o/r", 8, "abc", "fed", "dev", [expectation("e2")])]
    inline = score_results(cases, [postable(2, "c1", "def")], [3])["variants"]["a"]["overall"]["inline"]
    assert inline["per_pr"]["n"] == 1 and inline["per_pr"]["total"] == 2.0
    assert inline["unassessed_prs"] == 1
