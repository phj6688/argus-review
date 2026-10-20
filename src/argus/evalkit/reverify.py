"""Rerun only the verifier on the raw findings of a replay result.

The reviewer output stays fixed. Each raw finding of a result row, kept and
refuted alike, goes through `argus.review._verify` again with the chosen model and
verifier variant. The output rows have the replay format, so `argus.evalkit.score`
scores them like any replay and a verifier variant can be compared with the replay
it started from. No reviewer call happens in this path.

    python -m argus.evalkit.reverify --cases cases.jsonl --results baseline.jsonl \\
        --out reverify.jsonl --variant default_keep --model MODEL \\
        --mirror-root mirrors --cache-dir eval-cache

`--variant` names the verifier variant (a key of `review.VERIFIER_VARIANTS`).
`--label` is the variant label of the output rows, the one `score` groups by; it
defaults to the verifier variant name. A results file that holds more than one
replay variant needs `--source-variant`.

Credentials are those of replay: `ANTHROPIC_BASE_URL` and `ANTHROPIC_AUTH_TOKEN`,
or `ARGUS_EVAL_AUTH_TOKEN` in place of the token, or `OPENROUTER_API_KEY` for a
model id with a slash. `deploy/cron/argus_replay.sh` runs the replay module only,
so export the credential yourself for this command.

`--model` defaults to the model of the source rows, which verified them first. It is
required when the source rows disagree or do not name one.

Every verdict is appended to a cache file (`<out>.verdicts` by default) as soon as
it arrives. The cache key holds everything that changes the verifier input or the
experiment arm: the hash of the source file content and the source variant, the label,
the case, the run, the finding key and its occurrence, a hash of the finding, the snapshot
digest and head commit, a hash of the exact diff, a hash of the verifier variant definition,
the variant and the model. A restart verifies no finding twice. Two labels never share
verdicts, so two runs of one variant measure the verifier's own noise. A row is written
after all its findings have a verdict. A restart skips rows that exist, including error rows.
Every row carries `arm_sha256`, a hash of the source content, the source variant, the label,
the verifier variant and its definition, the model, the bug cap and the diff budget. A restart
refuses to run, and writes nothing, when an existing row of the label has another arm hash.
Delete an error row to retry that case, which costs only the findings that failed.
The exit status is 1 when this run wrote any error row, and a summary goes to stderr.
Replay exits 0 in that case; a rerun feeds a measurement, so a script must notice.
`duration_s` of a row measures the verifier time only. Do not compare it with the
duration of a full replay.
The judge opinions on a row are the ones the replay recorded. They are not asked
again, so they can describe a finding the new verdicts no longer keep.
"""

import argparse
import hashlib
import json
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from argus import review
from argus.context import cached_reader
from argus.diff import compress_diff
from argus.schema import CATEGORIES, Finding, JudgeOpinion, Verdict, VerifiedFinding
from argus.tokens import HeuristicEstimator
from argus.verdict import LANE_LLM, LANE_RAN

from .cases import Case, load_cases
from .findings import finding_key
from .replay import (
    code_version, default_cache_dir, default_mirror_root, model_credential, positive_int,
    read_complete_rows, serialize_finding, snapshot_summary,
)
from .snapshot import SnapshotStore


VOID_COUNTS = ("refutations_voided", "refutations_voided_unreadable")


class VerdictKey(NamedTuple):
    """Everything that changes the verifier input or the experiment arm."""

    # The content hash names the source; a file name does not change when the file does.
    source_sha256: str
    source_variant: str
    label: str
    case_id: str
    run: int
    finding_key: str
    # Two raw findings of one row can share a key; each still gets its own verdict.
    occurrence: int
    # The key hashes title and line only, so an edited description needs its own hash.
    finding_sha256: str
    snapshot_sha256: str
    head_sha: str
    diff_sha256: str
    # A variant edited between two runs keeps its name but changes the model input.
    variant_sha256: str
    verify_variant: str
    verify_model: str


class VerdictCache:
    """Verdicts on disk, appended one at a time so an interrupt loses at most one."""

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._stored: dict[VerdictKey, dict] = {}
        for row in read_complete_rows(path, "verdict cache"):
            self._stored[VerdictKey(*(row[name] for name in VerdictKey._fields))] = row["verdict"]

    def get(self, key: VerdictKey) -> Verdict | None:
        data = self._stored.get(key)
        if data is None:
            return None
        # The plain Verdict would drop the fields a variant's own verdict type adds.
        return review.VERIFIER_VARIANTS[key.verify_variant].verdict_type.model_validate(data)

    def put(self, key: VerdictKey, verdict: Verdict) -> None:
        data = verdict.model_dump(mode="json")
        line = json.dumps({**key._asdict(), "verdict": data}, ensure_ascii=True) + "\n"
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as stream:
                stream.write(line)
            self._stored[key] = data


@dataclass(frozen=True)
class Rerun:
    verify_variant: str
    verify_model: str
    label: str
    bug_cap: int
    source_name: str
    source_sha256: str
    variant_sha256: str
    arm_sha256: str
    github_mode: str
    store: SnapshotStore
    cache: VerdictCache
    provenance: dict


def _finding_of(raw: dict) -> Finding:
    return Finding.model_validate({name: raw[name] for name in Finding.model_fields if name in raw})


def _rebuild(raw: dict, finding: Finding, verdict: Verdict) -> VerifiedFinding:
    judge = raw.get("judge")
    return VerifiedFinding(
        finding=finding, verdict=verdict, lane=raw.get("lane", LANE_LLM),
        judge=JudgeOpinion.model_validate(judge) if judge else None,
        fingerprint=raw.get("fingerprint", ""))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# The checklist variant builds its user prompt from the finding's category, so the hash renders
# one finding per category and one without, against a fixed diff.
_PROBE_DIFF = "## File: a.py (python)\n```diff\n@@ -1 +1 @@\n-old\n+new\n```"


def _probe_findings() -> list[Finding]:
    return [Finding(file="a.py", line=1, severity="major", confidence=0.5, title="Probe title",
                    description="Probe description.", proposed_fix="Probe fix.", evidence="new",
                    category=category)
            for category in (*CATEGORIES, None)]


def _variant_sha256(name: str) -> str:
    """Hash what the variant sends to the model, so an edit under the same name shows."""
    variant = review.VERIFIER_VARIANTS[name]
    definition = {"system": variant.system, "tool": variant.tool.name,
                  "schema": variant.tool.input_schema, "max_tokens": variant.tool.max_tokens,
                  "verdict_type": variant.verdict_type.__name__,
                  "prompts": [variant.user_prompt(finding, _PROBE_DIFF) for finding in _probe_findings()]}
    # Key order stays: the order of the schema properties is part of a variant.
    return _sha256(json.dumps(definition))


def _verify_raw(source: dict, rerun: Rerun, snapshot: dict, diff: str,
                read) -> list[VerifiedFinding]:
    occurrences = Counter()
    diff_sha256 = _sha256(diff)
    verified = []
    for raw in source["raw"]:
        key = finding_key(source["case_id"], raw)
        finding = _finding_of(raw)
        verdict_key = VerdictKey(
            rerun.source_sha256, source["variant"], rerun.label, source["case_id"], source["run"],
            key, occurrences[key], _sha256(finding.model_dump_json()), snapshot["patch_sha256"],
            snapshot["head_sha"], diff_sha256, rerun.variant_sha256, rerun.verify_variant,
            rerun.verify_model)
        occurrences[key] += 1
        verdict = rerun.cache.get(verdict_key)
        if verdict is None:
            verdict = review._verify(finding, diff, rerun.verify_model,
                                     variant=rerun.verify_variant, read=read)
            rerun.cache.put(verdict_key, verdict)
        verified.append(_rebuild(raw, finding, verdict))
    return verified


def _coverage_with(source: dict, verify_variant: str, verified: list[VerifiedFinding]) -> list[dict]:
    coverage = deepcopy(source.get("coverage", []))
    for record in coverage:
        if record.get("lane") != LANE_LLM:
            continue
        if record.get("status") == LANE_RAN:
            record["verify_variant"] = verify_variant
        # The source counts belong to its verdicts; the helper adds them back for a checking variant.
        for name in VOID_COUNTS:
            record.pop(name, None)
        review._record_voided_refutations(record, verify_variant, verified)
    return coverage


def _reported(source: dict, verified: list[VerifiedFinding], bug_cap: int) -> list[dict]:
    bugs, nits = review.rank_and_cap(verified, bug_cap)
    other = [f for f in source.get("reported", []) if f.get("lane", LANE_LLM) != LANE_LLM]
    other_bugs = [f for f in other if f["severity"] in review.BUG_SEVERITIES]
    other_nits = [f for f in other if f["severity"] not in review.BUG_SEVERITIES]
    # Replay lists bug-class findings of every lane first, then the rest.
    return [*map(serialize_finding, bugs), *deepcopy(other_bugs),
            *map(serialize_finding, nits), *deepcopy(other_nits)]


def _same_inputs(source: dict, snapshot: dict) -> None:
    recorded = source.get("snapshot")
    if not recorded:
        return
    for name in ("repo", "base_sha", "head_sha", "patch_sha256"):
        if name in recorded and recorded[name] != snapshot[name]:
            raise ValueError(f"snapshot {name} differs from the one in the source row; "
                             "use the cache directory of that replay")


def reverify_row(source: dict, case: Case, rerun: Rerun) -> dict:
    started = datetime.now(timezone.utc).isoformat()
    clock = time.monotonic()
    record = {"variant": rerun.label, "case_id": case.case_id, "run": source["run"],
              **rerun.provenance, "model": rerun.verify_model,
              "reviewer_model": source.get("reviewer_model", source.get("model")),
              "verify_variant": rerun.verify_variant, "verify_model": rerun.verify_model,
              "source_results": rerun.source_name, "source_variant": source["variant"],
              "arm_sha256": rerun.arm_sha256,
              "github_mode": rerun.github_mode,
              "started_at": started, "error": None, "coverage": [],
              "reported": [], "raw": [], "snapshot": None}
    try:
        if source.get("error"):
            record["error"] = source["error"]
        else:
            if source.get("raw") is None:
                raise ValueError("source row has no raw findings")
            snapshot = rerun.store.get(case)
            summary = snapshot_summary(snapshot)
            _same_inputs(source, summary)
            # Set before the verifier calls: replay keeps the snapshot on a failed case, and a
            # row without one reads as unassessed to the inline metrics.
            record["snapshot"] = summary
            diff = compress_diff(list(snapshot.files), review.DIFF_BUDGET_TOKENS,
                                 HeuristicEstimator()).prompt
            read = cached_reader(rerun.store.file_loader(case))
            verified = _verify_raw(source, rerun, summary, diff, read)
            record["coverage"] = _coverage_with(source, rerun.verify_variant, verified)
            record["reported"] = _reported(source, verified, rerun.bug_cap)
            record["raw"] = [serialize_finding(item) for item in verified]
    except Exception as error:
        record["error"] = {"class": type(error).__name__, "message": str(error)}
    record["ended_at"] = datetime.now(timezone.utc).isoformat()
    record["duration_s"] = time.monotonic() - clock
    return record


def _load_source(content: bytes) -> list[dict]:
    # Read only: the replay file is an input, so a torn tail is not repaired here.
    return [json.loads(line) for line in content.decode("utf-8").splitlines() if line.strip()]


def _select_source(parser: argparse.ArgumentParser, rows: list[dict],
                   source_variant: str | None) -> list[dict]:
    variants = sorted({row["variant"] for row in rows})
    if source_variant is None and len(variants) > 1:
        parser.error(f"the results hold several variants ({', '.join(variants)}); "
                     "choose one with --source-variant")
    if source_variant is not None:
        rows = [row for row in rows if row["variant"] == source_variant]
        if not rows:
            parser.error(f"no rows for source variant {source_variant!r}")
    keys = Counter((row["case_id"], row["run"]) for row in rows)
    duplicate = next((key for key, count in keys.items() if count > 1), None)
    if duplicate:
        raise ValueError(f"duplicate source row: {duplicate}")
    return rows


def _source_model(parser: argparse.ArgumentParser, rows: list[dict]) -> str:
    models = sorted({row["model"] for row in rows if row.get("model")})
    if any(not row.get("model") for row in rows):
        parser.error("--model is required: a source row names no model")
    if len(models) != 1:
        parser.error(f"--model is required: the source rows name several models ({', '.join(models)})")
    return models[0]


def _arm_sha256(**parts) -> str:
    return _sha256(json.dumps(parts, sort_keys=True))


def _refuse_other_arm(parser: argparse.ArgumentParser, existing: list[dict], label: str,
                      arm_sha256: str) -> None:
    """Stop a restart that would mix two arms of an experiment under one label."""
    for row in existing:
        if row["variant"] == label and row.get("arm_sha256") != arm_sha256:
            parser.error(
                f"the output already holds rows labelled {label!r} from another arm: the source "
                "content, source variant, verifier variant or definition, model, bug cap or diff "
                "budget differs (or the rows predate arm_sha256); use another --label or --out")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases", required=True)
    parser.add_argument("--results", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--variant", required=True, choices=tuple(review.VERIFIER_VARIANTS))
    parser.add_argument("--label")
    parser.add_argument("--source-variant")
    parser.add_argument("--split", choices=("dev", "heldout"))
    parser.add_argument("--case-ids")
    parser.add_argument("--concurrency", type=positive_int, default=1)
    parser.add_argument("--mirror-root", default=default_mirror_root())
    parser.add_argument("--cache-dir", default=default_cache_dir())
    parser.add_argument("--verdict-cache")
    parser.add_argument("--model")
    parser.add_argument("--bug-cap", type=positive_int, default=10)
    args = parser.parse_args(argv)
    cases = {case.case_id: case for case in load_cases(args.cases)}
    results = Path(args.results)
    source_content = results.read_bytes()
    source_rows = _select_source(parser, _load_source(source_content), args.source_variant)
    for row in source_rows:
        if row["case_id"] not in cases:
            raise ValueError(f"unknown result case_id: {row['case_id']}")
    if not source_rows:
        return 0
    model = args.model or _source_model(parser, source_rows)
    label = args.label or args.variant
    out = Path(args.out)
    # Checked before anything is written: no row, no cache line, no directory.
    existing = read_complete_rows(out)
    source_sha256 = hashlib.sha256(source_content).hexdigest()
    variant_sha256 = _variant_sha256(args.variant)
    arm_sha256 = _arm_sha256(
        source_sha256=source_sha256, source_variant=source_rows[0]["variant"], label=label,
        verify_variant=args.variant, verify_model=model, variant_sha256=variant_sha256,
        bug_cap=args.bug_cap, diff_budget_tokens=review.DIFF_BUDGET_TOKENS,
        diff_estimator=HeuristicEstimator.__name__)
    _refuse_other_arm(parser, existing, label, arm_sha256)
    ids = {s.strip() for s in args.case_ids.split(",")} if args.case_ids else None
    rows = [row for row in source_rows if (not args.split or cases[row["case_id"]].split == args.split)
            and (ids is None or row["case_id"] in ids)]
    out.parent.mkdir(parents=True, exist_ok=True)
    done = {(row["variant"], row["case_id"], row["run"]) for row in existing}
    pending = [row for row in rows if (label, row["case_id"], row["run"]) not in done]
    if not pending:
        return 0
    code_sha, code_dirty = code_version()
    cache_path = Path(args.verdict_cache) if args.verdict_cache else out.with_name(out.name + ".verdicts")
    failures = []
    with model_credential(model) as credential_env, out.open("a", encoding="utf-8") as stream:
        rerun = Rerun(
            verify_variant=args.variant, verify_model=model, label=label,
            bug_cap=args.bug_cap, source_name=results.name, source_sha256=source_sha256,
            variant_sha256=variant_sha256, arm_sha256=arm_sha256,
            github_mode="mirror" if args.mirror_root else "api",
            store=SnapshotStore(args.mirror_root, args.cache_dir), cache=VerdictCache(cache_path),
            provenance={"code_sha": code_sha, "code_dirty": code_dirty,
                        "credential_env": credential_env})
        pool = ThreadPoolExecutor(args.concurrency)
        try:
            futures = [pool.submit(reverify_row, row, cases[row["case_id"]], rerun) for row in pending]
            for future in as_completed(futures):
                record = future.result()
                stream.write(json.dumps(record, ensure_ascii=True) + "\n")
                stream.flush()
                if record["error"]:
                    failures.append(record)
                status = record["error"]["class"] if record["error"] else f"{len(record['reported'])} findings"
                print(f"{label} {record['case_id']} run={record['run']} "
                      f"{record['duration_s']:.2f}s {status}", file=sys.stderr)
        finally:
            # An interrupt must not start the queued cases; running ones finish before the
            # credential is restored.
            pool.shutdown(wait=True, cancel_futures=True)
    if failures:
        first = failures[0]
        print(f"{len(failures)} of {len(pending)} rows are error rows. First: {first['case_id']} "
              f"run={first['run']} {first['error']['class']}: {first['error']['message']}. "
              "Delete an error row from the output to retry it.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
