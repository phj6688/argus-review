"""Replay a frozen regression subset with one shared judge source."""

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from . import judge
from .cases import load_cases, normalize_path
from .findings import finding_key, inline_findings
from .replay import code_version
from .reports import average, display, rate, write_report
from .score import _read_jsonl, _semantic_cache

CASE_FILES = {'heldout': 'cases_eval_cr.jsonl', 'incidents': 'cases_eval_incidents.jsonl'}


def freeze_subset(eval_dir, state_dir, heldout=20, seed=20261016):
    if heldout < 1:
        raise ValueError('heldout must be positive')
    root = Path(eval_dir) / 'analysis/cases'
    groups = defaultdict(list)
    for case in load_cases(root / CASE_FILES['heldout']):
        if case.split == 'heldout':
            groups[case.repo].append(case.case_id)
    if sum(map(len, groups.values())) < heldout:
        raise ValueError('not enough held-out cases')
    rng = random.Random(seed)
    repos = sorted(groups)
    rng.shuffle(repos)
    for repo in sorted(groups):
        values = groups[repo]
        values.sort()
        rng.shuffle(values)
    chosen = []
    while len(chosen) < heldout:
        for repo in repos:
            if groups[repo] and len(chosen) < heldout:
                chosen.append(groups[repo].pop())
    subset = {'seed': seed, 'heldout': chosen,
              'incidents': sorted(c.case_id for c in load_cases(root / CASE_FILES['incidents']))}
    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    with (state / 'subset.json').open('x', encoding='utf-8') as stream:
        json.dump(subset, stream, indent=2)
        stream.write('\n')
    return subset


def snapshot_cache(eval_dir, state_dir):
    target = Path(state_dir) / 'snapshot-cache'
    if not target.exists():
        # Rename only a complete copy, so an interrupted seed is never reused.
        staging = Path(state_dir) / 'snapshot-cache.copying'
        if staging.exists():
            shutil.rmtree(staging)
        shutil.copytree(Path(eval_dir) / 'eval/snapshot-cache', staging)
        staging.rename(target)
    return target


def visible(row):
    if row.get('error'):
        return []
    findings = list(row.get('reported', []))
    findings.extend(f for f in row.get('raw', []) if f.get('lane', 'llm') == 'llm'
                    and f.get('severity') in {'critical', 'major'}
                    and f.get('verdict_is_real', (f.get('verdict') or {}).get('is_real')) is False)
    return list({finding_key(row['case_id'], f): f for f in findings}.values())


def _hunk_tail(snapshot, expectation):
    for file in (snapshot or {}).get('files', []):
        if normalize_path(file['filename']) != normalize_path(expectation.file):
            continue
        for hunk in re.split(r'(?=^@@ )', file.get('patch') or '', flags=re.MULTILINE):
            match = re.match(r'@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@', hunk)
            if match and int(match[1]) <= expectation.end_line and (
                int(match[1]) + int(match[2] or 1) > expectation.start_line
            ):
                return '\n'.join(hunk.splitlines()[-20:])
    return ''


def pairs_for(cases, rows):
    by_id = {c.case_id: c for c in cases}
    pairs = {}
    for row in rows:
        case = by_id[row['case_id']]
        for expectation in case.expected:
            for finding in visible(row):
                if normalize_path(expectation.file) != normalize_path(finding['file']):
                    continue
                pair = {'expectation_id': expectation.id, 'finding_key': finding_key(case.case_id, finding),
                        'expectation': {**asdict(expectation),
                                        'hunk_tail': _hunk_tail(row.get('snapshot'), expectation)},
                        'finding': finding}
                pairs[judge.key(pair)] = pair
    return list(pairs.values())


def metrics(cases, rows, decisions):
    by_id = {r['case_id']: r for r in rows}
    if len(by_id) != len(rows) or set(by_id) != {c.case_id for c in cases}:
        raise ValueError('each subset case needs exactly one replay row')
    found = inline = caught = volume = errors = 0
    inline_available = True
    for case in cases:
        row = by_id[case.case_id]
        findings = visible(row)
        selected = inline_findings(row)
        inline_available &= selected is not None
        volume += len(findings)
        errors += bool(row.get('error'))

        def hits(pool):
            return sum(any(normalize_path(f['file']) == normalize_path(e.file)
                           and decisions.get((e.id, finding_key(case.case_id, f))) is True
                           for f in pool) for e in case.expected)

        count = hits(findings)
        found += count
        caught += count > 0
        inline += hits(selected or [])
    n = sum(len(c.expected) for c in cases)
    return {'visible': rate(found, n), 'inline': rate(inline, n) if inline_available else None,
            'incidents': rate(caught, len(cases)), 'findings_per_case': average(volume, len(cases)),
            'errors': rate(errors, len(cases))}


def should_alert(visible_found, baseline_visible, incidents, baseline_incidents, error_share):
    noise = max(1, abs(baseline_visible[0] - baseline_visible[1]))
    return bool(visible_found < min(baseline_visible) - noise
                or incidents < min(baseline_incidents) or error_share > .05)


def run(eval_dir, state_dir, out_dir, mirror_root, model, judge_model, concurrency=1,
        replay_timeout_s=21600, deadline_s=36000):
    if concurrency != 1:
        raise ValueError('offline concurrency must be 1')
    if replay_timeout_s <= 0 or deadline_s <= 0:
        raise ValueError('timeouts must be positive')
    deadline = time.monotonic() + deadline_s
    root, state, out = Path(eval_dir), Path(state_dir), Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    subset = json.loads((state / 'subset.json').read_text())
    groups, results, all_pairs = {}, {}, {}
    decisions_path = state / 'judge_api.jsonl'
    if decisions_path.exists():
        if any(r.get('judge') != judge_model or r.get('source') != 'api'
               or r.get('prompt_sha') != judge.prompt_sha()
               for r in judge.read_cache(decisions_path)):
            raise ValueError('judge model and prompt must match; use a new state directory')
    for name, filename in CASE_FILES.items():
        ids = subset[name]
        if not ids or len(set(ids)) != len(ids):
            raise ValueError('subset must have distinct cases in each set')
        cases_path = root / 'analysis/cases' / filename
        groups[name] = [c for c in load_cases(cases_path) if c.case_id in ids]
        if {c.case_id for c in groups[name]} != set(ids):
            raise ValueError('subset case missing from case file')
        results[name] = {}
        for baseline in ('v5p1', 'after'):
            path = root / f'eval/{baseline}_{name}.jsonl'
            if not path.is_file():
                raise ValueError(f'missing baseline: {path}')
            rows = [r for r in _read_jsonl([path]) if r['case_id'] in ids]
            metrics(groups[name], rows, {})
            results[name][baseline] = rows
    cache = snapshot_cache(root, state)
    for name, filename in CASE_FILES.items():
        ids = subset[name]
        cases_path = root / 'analysis/cases' / filename
        output = out / f'replay_{name}.jsonl'
        command = [sys.executable, '-m', 'argus.evalkit.replay', '--cases', str(cases_path),
                   '--case-ids', ','.join(ids), '--out', str(output), '--variant', 'weekly',
                   '--runs', '1', '--cache-dir', str(cache), '--mirror-root', str(mirror_root),
                   '--github-mode', 'mirror', '--model', model, '--concurrency', '1']
        env = {**os.environ, 'ARGUS_PUBLISH': '0', 'ARGUS_JUDGE_LIVE': '0',
               'ARGUS_LLM_MAX_CONCURRENCY': '1', 'ARGUS_WATCH_ENABLED': '0'}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('weekly deadline expired before replay')
        subprocess.run(command, check=True, env=env, timeout=min(replay_timeout_s, remaining))  # nosec B603
        results[name]['new'] = _read_jsonl([output])
        for rows in results[name].values():
            metrics(groups[name], rows, {})
            for pair in pairs_for(groups[name], rows):
                all_pairs[judge.key(pair)] = pair
    pairs_path = out / 'pairs.jsonl'
    pairs_path.write_text(''.join(json.dumps(p) + '\n' for p in all_pairs.values()), encoding='utf-8')
    count = judge.judge_pairs(all_pairs.values(), judge_model, [], decisions_path, deadline=deadline)
    if time.monotonic() >= deadline:
        raise TimeoutError('weekly deadline expired')
    decisions = _semantic_cache(decisions_path) if decisions_path.exists() else {}
    sets = {name: {run: metrics(groups[name], rows, decisions) for run, rows in runs.items()}
            for name, runs in results.items()}
    incidents = sets['incidents']
    alert = any(should_alert(group['new']['visible']['hits'],
                         [group[b]['visible']['hits'] for b in ('v5p1', 'after')],
                         incidents['new']['incidents']['hits'],
                         [incidents[b]['incidents']['hits'] for b in ('v5p1', 'after')],
                         group['new']['errors']['value']) for group in sets.values())
    calibration = state / 'calibration.json'
    report = {'date': datetime.now(timezone.utc).isoformat(), 'judge_model': judge_model,
              'reviewer_model': model, 'prompt_sha': judge.prompt_sha(),
              'code_sha': code_version()[0],
              # The image id names the container image; it is not a code version.
              'image_id': os.environ.get('ARGUS_EVAL_IMAGE_ID') or None,
              'pairs_judged': count, 'calibration': json.loads(calibration.read_text()) if calibration.exists() else None,
              'sets': sets, 'alert': alert}
    lines = [f"# Weekly regression {report['date']}", '', f'Reviewer: {model}.',
             f'Judge: {judge_model}. New decisions: {count}.',
             f"Judge prompt: {report['prompt_sha']}. Code: {report['code_sha']}. "
             f"Image: {report['image_id'] or 'not recorded'}.",
             f'Alert: {str(alert).lower()}.', '', '| Set | Run | Metric | Value |', '| --- | --- | --- | --- |']
    for name, runs in sets.items():
        for run_name, values in runs.items():
            for metric, value in values.items():
                if metric == 'incidents' and name != 'incidents':
                    continue
                lines.append(f'| {name} | {run_name} | {metric} | {display(value)} |')
    lines += ['', 'Latest calibration: ' + (json.dumps(report['calibration']) if report['calibration'] else 'not available'),
              '', 'The subset monitors regressions only. It must never be used for tuning.']
    write_report(out, report, '\n'.join(lines))
    return 3 if alert else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    freeze = sub.add_parser('freeze-subset')
    replay = sub.add_parser('run')
    for command in (freeze, replay):
        command.add_argument('--eval-dir', required=True)
        command.add_argument('--state-dir', required=True)
    freeze.add_argument('--heldout', type=int, default=20)
    freeze.add_argument('--seed', type=int, default=20261016)
    for name in ('out-dir', 'mirror-root', 'model', 'judge-model'):
        replay.add_argument('--' + name, required=True)
    replay.add_argument('--concurrency', type=int, choices=[1], default=1)
    replay.add_argument('--replay-timeout-s', type=int, default=21600)
    replay.add_argument('--deadline-s', type=int, default=36000)
    args = vars(parser.parse_args(argv))
    command = args.pop('command')
    try:
        if command == 'freeze-subset':
            freeze_subset(**args)
            return 0
        return run(**args)
    except Exception as error:
        print(f'Weekly failed: {type(error).__name__}: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
