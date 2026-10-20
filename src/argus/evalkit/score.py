"""Score cached review results without contacting a reviewer or GitHub."""

import argparse
import json
import math
import random
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict
from itertools import combinations
from pathlib import Path

from argus.diff import FilePatch
from argus.verdict import coverage_level

from .cases import Case, Expectation, load_cases, normalize_path
from .findings import finding_key, inline_comments
from .snapshot import changed_lines

SIGNIFICANCE = 0.05
BOOTSTRAP_SEED = 20261017
BOOTSTRAP_RESAMPLES = 2000
_SEMANTIC_DEFINITIONS = {
    "semantic_run_pairs": "With a judge cache: a hit is a reported finding in the expectation's file judged "
                          "the same issue, per run. Exact McNemar for every run pair; a pair of runs with no "
                          "shared expectation has no test.",
    "semantic_bootstrap": "Recall difference (B minus A) of mean-over-runs judged hits, pooled over expectations "
                          "on cases both variants ran. Percentile 95% interval from a bootstrap over PR clusters; "
                          "a direct commit is its own cluster.",
    "minimum_detectable_difference": "Smallest net defect gain with exact McNemar p below 0.05, with half of the "
                                     "A/A discordant count on each side; an odd count is rounded up.",
}
_NO_SHARED = "n/a (no shared expectations)"
_OUTCOMES = {(True, True): "both", (False, False): "neither",
             (True, False): "first_only", (False, True): "second_only"}


def _valid_line(line: object) -> bool:
    return type(line) is int and line >= 1


def location_match(expectation: Expectation, finding: dict, window: int) -> bool:
    line = finding.get("line")
    return (_valid_line(line)
            and normalize_path(finding.get("file", "")) == normalize_path(expectation.file)
            and expectation.start_line - window <= line <= expectation.end_line + window)


def wilson_interval(hits: float, total: float) -> tuple[float | None, float | None]:
    if not total:
        return None, None
    z = 1.959963984540054
    p = hits / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total**2)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


def mcnemar_exact(a_only: int, b_only: int) -> float:
    total = a_only + b_only
    if not total:
        return 1.0
    # Integer arithmetic avoids underflow and overflow for large corpora.
    tail = sum(math.comb(total, k) for k in range(min(a_only, b_only) + 1))
    return min(1.0, 2 * tail / (1 << total))


def duplicate_counts(findings: list[dict]) -> tuple[int, int]:
    duplicates = 0
    for a, b in combinations(findings, 2):
        if not _valid_line(a.get("line")) or not _valid_line(b.get("line")):
            continue
        if normalize_path(a["file"]) != normalize_path(b["file"]) or abs(a["line"] - b["line"]) > 3:
            continue
        x = set(re.findall(r"\w+", a.get("title", "").casefold()))
        y = set(re.findall(r"\w+", b.get("title", "").casefold()))
        if x | y and len(x & y) / len(x | y) >= 0.5:
            duplicates += 1
    return duplicates, len(findings) * (len(findings) - 1) // 2


def _rate(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def _quantile(ordered: list[float], fraction: float) -> float:
    index = (len(ordered) - 1) * fraction
    lower, upper = math.floor(index), math.ceil(index)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def _statistics(values: list[float]) -> dict:
    if not values:
        return {"mean": None, "median": None, "p90": None}
    return {"mean": statistics.mean(values), "median": statistics.median(values),
            "p90": _quantile(sorted(values), 0.9)}


def _rates(values: list[float], weights: list[float]) -> dict:
    scale = max(weights, default=0)
    weights = [w / scale for w in weights] if scale else weights
    total_weight = sum(weights)
    weighted = _rate(sum(v * w for v, w in zip(values, weights, strict=True)), total_weight)
    # Kish effective sample size is the standard Wilson approximation for weights.
    effective = total_weight**2 / sum(w * w for w in weights) if total_weight else 0
    return {"recall": _rate(sum(values), len(values)), "weighted_recall": weighted,
            "wilson95": wilson_interval(sum(values), len(values)),
            "weighted_wilson95": wilson_interval((weighted or 0) * effective, effective)}


def _recall(labels: list[tuple[Case, Expectation]], by_case: dict, window: int,
            semantic: dict | None = None) -> dict:
    means, any_runs, weights = [], [], []
    hit_runs = total_runs = 0
    for case, label in labels:
        rows = by_case.get(case.case_id, [])
        if not rows:
            continue
        hits = []
        for row in rows:
            found = False
            if not row.get("error"):
                for finding in row.get("reported", []):
                    if location_match(label, finding, window) and (
                        semantic is None or semantic.get((label.id, finding_key(case.case_id, finding))) is True
                    ):
                        found = True
                        break
            hits.append(found)
        hit_runs += sum(hits)
        total_runs += len(hits)
        means.append(statistics.mean(hits))
        any_runs.append(float(any(hits)))
        weights.append(label.weight)
    return {"expectations": len(means), "hit_runs": hit_runs, "total_runs": total_runs,
            "mean": _rates(means, weights), "any_run": _rates(any_runs, weights)}


def _coverage(row: dict) -> str:
    if row.get("error"):
        return "none"
    coverage = row.get("coverage")
    if isinstance(coverage, dict):
        return coverage.get("level") or coverage_level(coverage.get("lanes", []))
    return coverage_level(coverage) if coverage else "unknown"


def _pr_key(case: Case) -> tuple:
    # PR 0 marks a direct commit, which has no PR to share with other heads.
    return (case.repo, case.pr) if case.pr else (case.repo, case.pr, case.case_id)


def _counts(values: list[float]) -> dict:
    return {**_statistics(values), "n": len(values), "total": sum(values)}


def _inline(cases: list[Case], rows: list[dict], severity: str | None) -> dict:
    pr_of = {case.case_id: _pr_key(case) for case in cases}
    per_review, per_head = [], defaultdict(list)
    unassessed_heads = set()
    for row in rows:
        # The publisher cannot anchor a finding without integer lines, so it never lands inline.
        placeable = [f for f in row.get("reported", []) if _valid_line(f.get("line"))
                     and (f.get("end_line") is None or _valid_line(f["end_line"]))]
        emitted = inline_comments({**row, "reported": placeable})
        if emitted is None:
            unassessed_heads.add(row["case_id"])
            continue
        count = sum(severity is None or f["severity"] == severity for f in emitted)
        per_review.append(count)
        per_head[row["case_id"]].append(count)
    # A head without any row would make its PR read low, so the PR is not assessed.
    evaluated = per_head.keys() | unassessed_heads
    unevaluated = {case.case_id for case in cases if case.case_id not in evaluated}
    unassessed_prs = {pr_of[case_id] for case_id in unassessed_heads | unevaluated}
    per_pr = defaultdict(float)
    for case_id, counts in per_head.items():
        if pr_of[case_id] not in unassessed_prs:
            per_pr[pr_of[case_id]] += statistics.mean(counts)
    return {"per_review": _counts(per_review), "per_pr": _counts(list(per_pr.values())),
            "unassessed_reviews": len(rows) - len(per_review), "unassessed_prs": len(unassessed_prs)}


def _metrics(cases: list[Case], rows: list[dict], labels: list[tuple[Case, Expectation]],
             windows: list[int], semantic: dict | None, severity: str | None = None) -> dict:
    by_case = defaultdict(list)
    for row in rows:
        by_case[row["case_id"]].append(row)
    recall = {}
    for window in windows:
        scores = _recall(labels, by_case, window)
        if semantic is not None:
            scores["confirmed"] = _recall(labels, by_case, window, semantic)
        recall[str(window)] = scores
    lists = [[f for f in row.get("reported", []) if severity is None or f["severity"] == severity]
             if not row.get("error") else [] for row in rows]
    lanes = sorted({f.get("lane", "llm") for findings in lists for f in findings})
    severities = sorted({f["severity"] for findings in lists for f in findings})
    volume = {"overall": _statistics([len(findings) for findings in lists]),
              "by_lane": {lane: _statistics([sum(f.get("lane", "llm") == lane for f in fs)
                                             for fs in lists]) for lane in lanes},
              "by_severity": {s: _statistics([sum(f["severity"] == s for f in fs)
                                             for fs in lists]) for s in severities}}
    on_change = assessed = unassessed = duplicates = pairs = 0
    severity_counts = Counter()
    for row, findings in zip(rows, lists, strict=True):
        snapshot = row.get("snapshot")
        changed = changed_lines([FilePatch(**f) for f in snapshot["files"]]) if snapshot else None
        for finding in findings:
            severity_counts[finding["severity"]] += 1
            if changed is None or not _valid_line(finding.get("line")):
                unassessed += 1
            else:
                assessed += 1
                on_change += finding["line"] in changed.get(normalize_path(finding["file"]), set())
        d, p = duplicate_counts(findings)
        duplicates += d
        pairs += p
    negatives = _recall([(case, label) for case in cases for label in case.negatives], by_case, 3)
    return {"cases": len(by_case), "runs": len(rows), "recall": recall, "volume": volume,
            "inline": _inline(cases, rows, severity),
            "changed_line_share": _rate(on_change, assessed), "changed_line_findings": on_change,
            "changed_line_assessed_findings": assessed, "changed_line_unassessed_findings": unassessed,
            "duplicate_pairs": duplicates, "finding_pairs": pairs, "duplicate_rate": _rate(duplicates, pairs),
            "negatives": negatives["expectations"], "negatives_hit_rate": negatives["mean"]["recall"],
            "negatives_any_run_hit_rate": negatives["any_run"]["recall"],
            "severity_distribution": dict(sorted(severity_counts.items())),
            "severity_shares": {s: _rate(n, sum(severity_counts.values()))
                                for s, n in sorted(severity_counts.items())},
            "error_rate": _rate(sum(bool(row.get("error")) for row in rows), len(rows)),
            "coverage_levels": dict(sorted(Counter(_coverage(row) for row in rows).items())),
            "mean_duration_s": statistics.mean([row["duration_s"] for row in rows]) if rows else None}


def _prepare(cases: list[Case], rows: list[dict]) -> list[dict]:
    known = {case.case_id: case for case in cases}
    seen, snapshots = {}, {}
    for row in rows:
        case_id = row["case_id"]
        if case_id not in known:
            raise ValueError(f"unknown result case_id: {case_id}")
        key = row["variant"], case_id, row["run"]
        if key in seen and seen[key] != row:
            raise ValueError(f"conflicting duplicate result: {key}")
        seen[key] = row
        snapshot = row.get("snapshot")
        if snapshot:
            case = known[case_id]
            if (snapshot["repo"], snapshot["base_sha"], snapshot["head_sha"]) != (
                case.repo, case.base_sha, case.head_sha
            ):
                raise ValueError(f"snapshot identity mismatch for case {case_id}")
            identity = snapshot["patch_sha256"], json.dumps(snapshot["files"], sort_keys=True)
            if case_id in snapshots:
                if snapshots[case_id] != identity:
                    raise ValueError(f"snapshot inputs differ for case {case_id}")
            else:
                snapshots[case_id] = identity
    return list(seen.values())


def _compare(a: str, b: str, cases: list[Case], rows: list[dict], window: int) -> dict:
    by_variant = {variant: {(r["case_id"], r["run"]): r for r in rows if r["variant"] == variant}
                  for variant in (a, b)}
    shared = by_variant[a].keys() & by_variant[b].keys()
    gained, lost, details = [], [], []
    both = neither = 0
    for case in cases:
        keys = sorted(k for k in shared if k[0] == case.case_id)
        if not keys:
            continue
        for label in case.expected:
            hits = {}
            for variant in (a, b):
                matched = [not by_variant[variant][k].get("error") and any(
                    location_match(label, f, window) for f in by_variant[variant][k].get("reported", [])
                ) for k in keys]
                hits[variant] = {"mean_hit_rate": statistics.mean(matched), "any_run_hit": any(matched)}
            item = {"case_id": case.case_id, "expectation_id": label.id}
            x, y = hits[a]["any_run_hit"], hits[b]["any_run_hit"]
            if x and y:
                both += 1
            elif not x and not y:
                neither += 1
            elif y:
                gained.append(item)
            else:
                lost.append(item)
            details.append({**item, "a": hits[a], "b": hits[b]})
    return {"a": a, "b": b, "window": window, "hit_definition": "any shared run",
            "shared_case_runs": len(shared), "expectations": len(details),
            "both": both, "neither": neither, "a_only": len(lost), "b_only": len(gained),
            "p_value": mcnemar_exact(len(lost), len(gained)), "gained": gained, "lost": lost,
            "paired_expectations": details}


def minimum_detectable_difference(aa_discordant: int, alpha: float = SIGNIFICANCE) -> int:
    """Smallest net defect gain that exact McNemar calls significant against A/A noise."""
    if type(aa_discordant) is not int or aa_discordant < 0:
        raise ValueError("aa_discordant must be a non-negative integer")
    if type(alpha) not in (int, float) or not 0 < alpha < 1:
        raise ValueError("alpha must be a finite number between 0 and 1")
    # With no effect, half of the A/A disagreement lands on each side. An odd count is
    # rounded up so the estimate never claims more power than the runs showed.
    noise = math.ceil(aa_discordant / 2)
    gain = 0
    while mcnemar_exact(noise + gain, noise) >= alpha:
        gain += 1
    return gain


def cluster_bootstrap(clusters: list[list[float]], seed: int = BOOTSTRAP_SEED,
                      resamples: int = BOOTSTRAP_RESAMPLES) -> tuple[float | None, float | None]:
    """Percentile 95% interval of the pooled mean, resampling whole clusters."""
    totals = [(sum(values), len(values)) for values in clusters if values]
    if not totals:
        return None, None
    generator = random.Random(seed)  # nosec B311: statistical resampling, not a secret
    estimates = []
    for _ in range(resamples):
        sample = generator.choices(totals, k=len(totals))
        estimates.append(sum(total for total, _ in sample) / sum(count for _, count in sample))
    estimates.sort()
    return _quantile(estimates, 0.025), _quantile(estimates, 0.975)


def _semantic_hit(case_id: str, label: Expectation, findings: list[dict], semantic: dict) -> bool:
    # Same rule as the eval-set reports: a reported finding in the expectation's file that
    # the judge called the same issue. The judge only saw pairs inside the emit window.
    return any(_valid_line(f.get("line"))
               and normalize_path(f.get("file", "")) == normalize_path(label.file)
               and semantic.get((label.id, finding_key(case_id, f))) is True for f in findings)


def semantic_hit_table(cases: list[Case], rows: list[dict],
                       semantic: dict) -> dict[tuple[str, str, int], dict[str, bool]]:
    """Judged hits keyed by (variant, case_id, run), then expectation id. Errors are misses."""
    by_case = {case.case_id: case for case in cases}
    table = {}
    for row in rows:
        case = by_case[row["case_id"]]
        findings = [] if row.get("error") else row.get("reported", [])
        table[row["variant"], case.case_id, row["run"]] = {
            label.id: _semantic_hit(case.case_id, label, findings, semantic) for label in case.expected}
    return table


def _runs(hits: dict, variant: str) -> list[int]:
    return sorted({run for v, _, run in hits if v == variant})


def _paired_outcomes(hits: dict, first: tuple[str, int], second: tuple[str, int]) -> Counter:
    outcomes = Counter({name: 0 for name in _OUTCOMES.values()})
    for (variant, case_id, run), labels in hits.items():
        other = hits.get((second[0], case_id, second[1]))
        if (variant, run) != first or other is None:
            continue
        for x, y in zip(labels.values(), other.values(), strict=True):
            outcomes[_OUTCOMES[x, y]] += 1
    return outcomes


def _paired_test(outcomes: Counter) -> float | None:
    # Runs with no shared expectation (an interrupted replay) carry no evidence either way.
    if not outcomes.total():
        return None
    return mcnemar_exact(outcomes["first_only"], outcomes["second_only"])


def _cluster_key(case: Case) -> tuple:
    # pr 0 marks a direct commit, which shares a review with no other case.
    return ("pr", case.repo, case.pr) if case.pr else ("case", case.case_id)


def _recall_differences(a: str, b: str, cases: list[Case], hits: dict) -> list[list[float]]:
    by_variant_case = defaultdict(list)
    for (variant, case_id, _), labels in hits.items():
        by_variant_case[variant, case_id].append(labels)
    clusters = defaultdict(list)
    for case in cases:
        a_runs, b_runs = by_variant_case[a, case.case_id], by_variant_case[b, case.case_id]
        if not a_runs or not b_runs:
            continue
        for label in case.expected:
            a_rate = sum(run[label.id] for run in a_runs) / len(a_runs)
            b_rate = sum(run[label.id] for run in b_runs) / len(b_runs)
            clusters[_cluster_key(case)].append(b_rate - a_rate)
    return [clusters[key] for key in sorted(clusters)]


def _semantic_compare(a: str, b: str, cases: list[Case], hits: dict) -> dict:
    run_pairs = []
    for a_run in _runs(hits, a):
        for b_run in _runs(hits, b):
            outcomes = _paired_outcomes(hits, (a, a_run), (b, b_run))
            run_pairs.append({"a_run": a_run, "b_run": b_run, "expectations": outcomes.total(),
                              "both": outcomes["both"], "neither": outcomes["neither"],
                              "a_only": outcomes["first_only"], "b_only": outcomes["second_only"],
                              "p_value": _paired_test(outcomes)})
    clusters = _recall_differences(a, b, cases, hits)
    expectations = sum(len(values) for values in clusters)
    bootstrap = {"seed": BOOTSTRAP_SEED, "resamples": BOOTSTRAP_RESAMPLES, "clusters": len(clusters),
                 "expectations": expectations,
                 "recall_difference": _rate(sum(map(sum, clusters)), expectations),
                 "ci95": list(cluster_bootstrap(clusters))}
    return {"a": a, "b": b, "run_pairs": run_pairs, "bootstrap": bootstrap}


def _semantic_noise(variant: str, hits: dict) -> list[dict]:
    result = []
    for first, second in combinations(_runs(hits, variant), 2):
        outcomes = _paired_outcomes(hits, (variant, first), (variant, second))
        p_value = _paired_test(outcomes)
        discordant = None if p_value is None else outcomes["first_only"] + outcomes["second_only"]
        result.append({"runs": [first, second], "expectations": outcomes.total(),
                       "discordant": discordant, "first_only": outcomes["first_only"],
                       "second_only": outcomes["second_only"], "p_value": p_value,
                       "minimum_detectable_difference": None if discordant is None
                       else minimum_detectable_difference(discordant)})
    return result


def _semantic_blocks(cases: list[Case], rows: list[dict], semantic: dict, variants: list[str]) -> dict:
    hits = semantic_hit_table(cases, rows, semantic)
    return {"definitions": _SEMANTIC_DEFINITIONS,
            "semantic_comparisons": [_semantic_compare(a, b, cases, hits)
                                     for a, b in combinations(variants, 2)],
            "semantic_noise": {variant: _semantic_noise(variant, hits) for variant in variants}}


def score_results(cases: list[Case], rows: list[dict], windows: list[int] | None = None,
                  semantic: dict | None = None, comparison_window: int = 3) -> dict:
    windows = sorted(set(windows if windows is not None else [0, 3, 10]))
    if not windows or min(windows) < 0 or comparison_window < 0:
        raise ValueError("windows must be nonnegative and nonempty")
    rows = _prepare(cases, rows)
    variants = {}
    for variant in sorted({row["variant"] for row in rows}):
        selected = [r for r in rows if r["variant"] == variant]
        labels = [(case, label) for case in cases for label in case.expected]
        metrics = {"overall": _metrics(cases, selected, labels, windows, semantic),
                   "unevaluated_cases": sorted({c.case_id for c in cases}
                                                - {r["case_id"] for r in selected})}
        splits = sorted({c.split for c in cases})
        metrics["by_split"] = {}
        for split in splits:
            group = [c for c in cases if c.split == split]
            ids = {c.case_id for c in group}
            metrics["by_split"][split] = _metrics(group, [r for r in selected if r["case_id"] in ids],
                                                 [(c, e) for c, e in labels if c.split == split],
                                                 windows, semantic)
        for attribute, key in (("source", "by_source"), ("severity", "by_severity"), ("cls", "by_cls")):
            metrics[key] = {}
            groups = {getattr(label, attribute) for _, label in labels}
            if attribute == "severity":
                groups |= {f["severity"] for row in selected for f in row.get("reported", [])}
            for group in sorted(groups):
                group_labels = [(c, e) for c, e in labels if getattr(e, attribute) == group]
                ids = {c.case_id for c, _ in group_labels}
                group_cases = cases if attribute == "severity" else [c for c in cases if c.case_id in ids]
                group_rows = selected if attribute == "severity" else [r for r in selected if r["case_id"] in ids]
                metrics[key][group] = _metrics(group_cases, group_rows, group_labels, windows, semantic,
                                              severity=group if attribute == "severity" else None)
        variants[variant] = metrics
    report = {"windows": windows, "comparison_window": comparison_window,
            "definitions": {
                "recall": "Average each expectation's hit rate across its recorded runs; errors are misses.",
                "wilson95": "Mean-over-runs intervals use fractional hits; weighted intervals use Kish effective n. Both are approximations.",
                "volume": "Findings per case/run, including zero-finding errors; p90 uses linear interpolation.",
                "duplicate_rate": "Matching duplicate pairs divided by all within-run finding pairs.",
                "inline": INLINE_DEFINITION,
                "source_cls_groups": "Recall uses matching labels; other metrics use all findings in cases carrying those labels. Groups can overlap.",
                "severity_groups": "Recall uses severity-labelled expectations; volume/noise uses findings of that severity.",
                "comparison": "Exact McNemar on each expectation's any-run hit, using only shared case/run pairs.",
            }, "variants": variants,
            "comparisons": [_compare(a, b, cases, rows, comparison_window) for a, b in combinations(variants, 2)]}
    if semantic is not None:
        blocks = _semantic_blocks(cases, rows, semantic, list(variants))
        report["definitions"].update(blocks.pop("definitions"))
        report.update(blocks)
    return report


def candidate_pairs(cases: list[Case], rows: list[dict], window: int):
    by_case = {case.case_id: case for case in cases}
    for row in rows:
        if row.get("error"):
            continue
        case = by_case[row["case_id"]]
        for label in case.expected + case.negatives:
            for finding in row.get("reported", []):
                if location_match(label, finding, window):
                    yield {"variant": row["variant"], "case_id": case.case_id, "run": row["run"],
                           "expectation_id": label.id, "finding_key": finding_key(case.case_id, finding),
                           "expectation": asdict(label), "finding": finding,
                           "negative": label in case.negatives}


def _read_jsonl(paths: list[str | Path]) -> list[dict]:
    rows = []
    for path in paths:
        with Path(path).open(encoding="utf-8") as stream:
            for number, line in enumerate(stream, 1):
                if line.strip():
                    try:
                        rows.append(json.loads(line))
                    except ValueError as error:
                        raise ValueError(f"{path}:{number}: {error}") from error
    return rows


def _semantic_cache(path: str | Path) -> dict:
    result = {}
    for row in _read_jsonl([path]):
        key = row["expectation_id"], row["finding_key"]
        if type(row["same_issue"]) is not bool:
            raise ValueError(f"semantic cache same_issue must be boolean: {key}")
        if key in result and result[key] != row["same_issue"]:
            raise ValueError(f"conflicting semantic decisions: {key}")
        result[key] = row["same_issue"]
    return result


INLINE_DEFINITION = (
    "Inline counts render each case/run through the publisher with its severity threshold and comment cap, "
    "and count one per emitted inline comment. Per review counts each case/run; errors count as zero. "
    "Per PR adds all heads of one repository and PR, taking each head's mean over its runs; a direct commit "
    "(PR 0) is its own group. Rows without a diff snapshot are unassessed. A PR is unassessed when any of its "
    "heads has such a row or has no row, which includes a PR with no rows at all. Split, source and class groups hold only some heads of a PR, so their "
    "per-PR values are sums over the group's heads. Severity groups count that severity among the inline "
    "comments of the whole review.")


def _inline_lines(inline: dict) -> list[str]:
    review, pr = inline["per_review"], inline["per_pr"]
    return ["| Inline per review (case/run) mean / median / p90 | Reviews | "
            "Inline per PR (sum over heads of each head's mean over runs) mean / median / p90 | PRs | "
            "Unassessed reviews | Unassessed PRs |",
            "| --- | ---: | --- | ---: | ---: | ---: |",
            f"| {' / '.join(_display(review[k]) for k in ('mean', 'median', 'p90'))} | {review['n']} | "
            f"{' / '.join(_display(pr[k]) for k in ('mean', 'median', 'p90'))} | {pr['n']} | "
            f"{inline['unassessed_reviews']} | {inline['unassessed_prs']} |", ""]


def _display(value, percent=False) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1%}" if percent else f"{value:.3f}"


def _cell(value) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def markdown_report(report: dict) -> str:
    lines = ["# Offline evaluation", "",
             "Recall averages expectation hit rates across recorded runs. Errors count as misses. "
             "Missing cases are excluded and listed below. Volume is per case/run.", "",
             "Wilson 95% intervals for mean recall and weights are approximations. "
             "Location matches do not prove that two findings describe the same issue.", ""]
    for variant, data in report["variants"].items():
        overall = data["overall"]
        lines.extend([f"## {_cell(variant)}", "", "| Window | Recall | Weighted | 95% interval | Any run | Confirmed |",
                      "| --- | ---: | ---: | --- | ---: | ---: |"])
        for window, recall in overall["recall"].items():
            mean, any_run = recall["mean"], recall["any_run"]
            ci = ", ".join(_display(x, True) for x in mean["wilson95"])
            confirmed = recall.get("confirmed", {}).get("mean", {}).get("recall")
            lines.append(f"| {window} | {_display(mean['recall'], True)} | "
                         f"{_display(mean['weighted_recall'], True)} | {ci} | "
                         f"{_display(any_run['recall'], True)} | {_display(confirmed, True)} |")
        volume = overall["volume"]["overall"]
        lines.extend(["", "| Cases | Runs | Findings mean / median / p90 | Changed lines | Duplicate pairs | Negatives hit | Errors | Mean seconds |",
                      "| ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: |",
                      f"| {overall['cases']} | {overall['runs']} | "
                      f"{' / '.join(_display(volume[k]) for k in ('mean', 'median', 'p90'))} | "
                      f"{_display(overall['changed_line_share'], True)} | {_display(overall['duplicate_rate'], True)} | "
                      f"{_display(overall['negatives_hit_rate'], True)} | {_display(overall['error_rate'], True)} | "
                      f"{_display(overall['mean_duration_s'])} |", ""])
        lines.extend(_inline_lines(overall["inline"]))
        lines.append("Coverage: " + ", ".join(f"{_cell(k)}: {v}" for k, v in overall["coverage_levels"].items()))
        if data["unevaluated_cases"]:
            lines.append("Unevaluated cases: " + ", ".join(_cell(x) for x in data["unevaluated_cases"]))
        for key, title in (("by_split", "Split"), ("by_source", "Source"),
                           ("by_severity", "Severity"), ("by_cls", "Class")):
            lines.extend(["", f"| {title} | Window | Recall | Weighted | Any run | Findings mean |",
                          "| --- | ---: | ---: | ---: | ---: | ---: |"])
            for group, metrics in data[key].items():
                for window, recall in metrics["recall"].items():
                    lines.append(f"| {_cell(group)} | {window} | {_display(recall['mean']['recall'], True)} | "
                                 f"{_display(recall['mean']['weighted_recall'], True)} | "
                                 f"{_display(recall['any_run']['recall'], True)} | "
                                 f"{_display(metrics['volume']['overall']['mean'])} |")
        lines.extend(["", "| Lane | Findings mean | Median | p90 |", "| --- | ---: | ---: | ---: |"])
        for lane, volume in overall["volume"]["by_lane"].items():
            lines.append(f"| {_cell(lane)} | {_display(volume['mean'])} | {_display(volume['median'])} | {_display(volume['p90'])} |")
        lines.extend(["", "| Severity | Findings | Share |", "| --- | ---: | ---: |"])
        for severity, count in overall["severity_distribution"].items():
            lines.append(f"| {_cell(severity)} | {count} | {_display(overall['severity_shares'][severity], True)} |")
        lines.append("")
    if report["comparisons"]:
        lines.extend(["## Paired comparisons", "", "Any-run hits on shared case/run pairs.", "",
                      "| A | B | Window | Expectations | Gained | Lost | Exact p |",
                      "| --- | --- | ---: | ---: | ---: | ---: | ---: |"])
        for comparison in report["comparisons"]:
            lines.append(f"| {_cell(comparison['a'])} | {_cell(comparison['b'])} | {comparison['window']} | "
                         f"{comparison['expectations']} | {comparison['b_only']} | {comparison['a_only']} | "
                         f"{_display(comparison['p_value'])} |")
        for comparison in report["comparisons"]:
            lines.extend(["", f"### {_cell(comparison['a'])} vs {_cell(comparison['b'])}", ""])
            for name in ("gained", "lost"):
                items = ", ".join(f"{_cell(x['case_id'])}/{_cell(x['expectation_id'])}" for x in comparison[name])
                lines.append(f"{name.capitalize()}: {items or 'none'}")
    if "semantic_comparisons" in report:
        lines.extend(_semantic_markdown(report))
    lines.extend(["", report["definitions"]["source_cls_groups"], report["definitions"]["severity_groups"], ""])
    return "\n".join(lines)


def _test_cell(value, render) -> str:
    return _NO_SHARED if value is None else render(value)


def _semantic_markdown(report: dict) -> list[str]:
    lines = ["", "## Semantic run pairs", "", report["definitions"]["semantic_run_pairs"], "",
             "| A | B | A run | B run | Expectations | Gained | Lost | Exact p |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for comparison in report["semantic_comparisons"]:
        for pair in comparison["run_pairs"]:
            lines.append(f"| {_cell(comparison['a'])} | {_cell(comparison['b'])} | {_cell(pair['a_run'])} | "
                         f"{_cell(pair['b_run'])} | {pair['expectations']} | {pair['b_only']} | "
                         f"{pair['a_only']} | {_test_cell(pair['p_value'], _display)} |")
    lines.append("")
    for comparison in report["semantic_comparisons"]:
        bootstrap = comparison["bootstrap"]
        interval = ", ".join(_display(x, True) for x in bootstrap["ci95"])
        lines.append(f"Cluster bootstrap {_cell(comparison['b'])} minus {_cell(comparison['a'])}: recall "
                     f"difference {_display(bootstrap['recall_difference'], True)}, 95% interval [{interval}] "
                     f"over {bootstrap['clusters']} PR clusters (a direct commit is its own cluster; "
                     f"{bootstrap['resamples']} resamples, "
                     f"seed {bootstrap['seed']}).")
    lines.extend(["", report["definitions"]["semantic_bootstrap"]])
    lines.extend(["", "| Variant | A/A runs | Discordant | Minimum detectable difference |",
                  "| --- | --- | ---: | ---: |"])
    for variant, pairs in report["semantic_noise"].items():
        for pair in pairs:
            lines.append(f"| {_cell(variant)} | {', '.join(_cell(run) for run in pair['runs'])} | "
                         f"{'n/a' if pair['discordant'] is None else pair['discordant']} | "
                         f"{_test_cell(pair['minimum_detectable_difference'], str)} |")
    lines.extend(["", report["definitions"]["minimum_detectable_difference"]])
    return lines


def _write(path: str | Path, text: str):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", required=True)
    parser.add_argument("--results", nargs="+", required=True)
    parser.add_argument("--out-json", required=True)
    parser.add_argument("--out-md", required=True)
    parser.add_argument("--windows", default="0,3,10")
    parser.add_argument("--comparison-window", type=int, default=3)
    parser.add_argument("--semantic-cache")
    parser.add_argument("--emit-pairs")
    parser.add_argument("--split", choices=("dev", "heldout"))
    args = parser.parse_args(argv)
    cases = load_cases(args.cases)
    rows = _prepare(cases, _read_jsonl(args.results))
    if args.split:
        cases = [case for case in cases if case.split == args.split]
        ids = {case.case_id for case in cases}
        rows = [row for row in rows if row["case_id"] in ids]
    try:
        windows = sorted({int(value) for value in args.windows.split(",")})
    except ValueError:
        parser.error("--windows must be comma-separated nonnegative integers")
    if not windows or min(windows) < 0 or args.comparison_window < 0:
        parser.error("windows must be nonnegative and nonempty")
    semantic = _semantic_cache(args.semantic_cache) if args.semantic_cache else None
    report = score_results(cases, rows, windows, semantic, args.comparison_window)
    _write(args.out_json, json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
    _write(args.out_md, markdown_report(report))
    if args.emit_pairs:
        _write(args.emit_pairs, "".join(json.dumps(pair) + "\n"
                                      for pair in candidate_pairs(cases, rows, max(windows))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
