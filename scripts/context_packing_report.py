#!/usr/bin/env python3
"""Measure file coverage and context size from local repository snapshots."""

import argparse
import json
from pathlib import Path
import re
import statistics
import subprocess
import time

from argus.context import REVIEW_CONTEXT_BUDGET_TOKENS, render_review_context
from argus.diff import parse_unified_diff
from argus.tokens import HeuristicEstimator


MAX_BLOB_BYTES = 1_000_000
PACKING_FIELDS = ('files_changed', 'files_with_patch', 'full', 'windowed', 'hunks_only',
                  'overflow', 'patchless', 'estimated_tokens', 'budget')


def git_output(*args: str) -> bytes:
    result = subprocess.run(['git', *args], capture_output=True)
    if result.returncode:
        # Diagnostics can contain repository text; keep the report to counts and errors.
        raise RuntimeError(f'git command failed (exit {result.returncode})')
    return result.stdout


def repository_dir(path: str) -> Path:
    repo = Path(path).resolve()
    if (repo / '.git').exists():
        return Path(git_output('-C', str(repo), 'rev-parse', '--absolute-git-dir').decode().strip())
    return repo


def read_at_head(git_dir: Path, head: str, path: str) -> str | None:
    blob = f'{head}:{path}'
    size = subprocess.run(['git', '--git-dir', str(git_dir), 'cat-file', '-s', blob],
                          capture_output=True)
    if size.returncode or int(size.stdout) > MAX_BLOB_BYTES:
        return None
    content = git_output('--git-dir', str(git_dir), 'show', blob)
    if len(content) > MAX_BLOB_BYTES:
        return None
    try:
        return content.decode('utf-8')
    except UnicodeDecodeError:
        return None


def measure(case: dict, budget: int, row: dict) -> None:
    for name in ('base', 'head'):
        if not isinstance(case[name], str) or not re.fullmatch(r'[0-9a-fA-F]{40}', case[name]):
            raise ValueError(f'{name} must be 40 hex characters')
    git_dir = repository_dir(case['git_dir'])
    diff = git_output('--git-dir', str(git_dir), 'diff', '--no-color', '--no-ext-diff',
                      '-U3', '-M', f'{case["base"]}...{case["head"]}')
    files = parse_unified_diff(diff.decode('utf-8', errors='replace'))
    row['files_changed'] = len(files)
    row['files_with_patch'] = sum(bool(f.patch) for f in files)

    def read(path: str) -> str | None:
        row['reads'] += 1
        return read_at_head(git_dir, case['head'], path)

    started = time.perf_counter()
    try:
        result = render_review_context(files, read, budget, HeuristicEstimator())
    finally:
        row['seconds'] = round(time.perf_counter() - started, 2)
    for mode in ('full', 'windowed', 'hunks_only', 'overflow'):
        row[mode] = len(getattr(result, mode))
    patchless = getattr(result, 'patchless', None)
    row['patchless'] = len(patchless) if patchless is not None else None
    row['estimated_tokens'] = result.estimated_tokens


def run(cases: Path, out: Path, budget: int) -> None:
    with cases.open(encoding='utf-8') as source, out.open('w', encoding='utf-8') as destination:
        for number, line in enumerate(source, 1):
            if not line.strip():
                continue
            row = dict.fromkeys(PACKING_FIELDS)
            row.update(id=f'line:{number}', budget=budget, reads=0, seconds=0.0)
            try:
                case = json.loads(line)
                if not isinstance(case, dict) or not isinstance(case.get('id'), str):
                    raise ValueError('case must be an object with a string id')
                row['id'] = case['id']
                measure(case, budget, row)
            except Exception as error:
                row['error'] = f'{type(error).__name__}: {error}'
            destination.write(json.dumps(row) + '\n')
            destination.flush()


def load_results(path: Path) -> dict[str, dict]:
    with path.open(encoding='utf-8') as source:
        rows = [json.loads(line) for line in source if line.strip()]
    return {row['id']: row for row in rows}


def compare(before: Path, after: Path) -> None:
    old, new = load_results(before), load_results(after)
    old_errors = {key for key, row in old.items() if 'error' in row}
    new_errors = {key for key, row in new.items() if 'error' in row}
    errors = old_errors | new_errors
    shared = sorted((old.keys() & new.keys()) - errors)
    pairs = [(old[key], new[key]) for key in shared]
    print(f'Cases compared: {len(pairs)}')
    print(f'{"Metric":<24} {"Before":>10} {"After":>10}')

    def totals(label, value):
        counts = [sum(value(pair[index]) for pair in pairs) for index in (0, 1)]
        print(f'{label:<24} {counts[0]:>10} {counts[1]:>10}')

    totals('Cases with overflow', lambda row: row['overflow'] > 0)
    totals('Overflow files', lambda row: row['overflow'])
    totals('Files with a view', lambda row: sum(row[key] for key in ('full', 'windowed', 'hunks_only')))
    for label, key in [('Full views', 'full'), ('Windowed views', 'windowed'), ('Hunks views', 'hunks_only')]:
        totals(label, lambda row: row[key])
    for label, aggregate in [('Max render seconds', max), ('Median render seconds', statistics.median)]:
        seconds = [aggregate([pair[index]['seconds'] for pair in pairs]) if pairs else 0
                   for index in (0, 1)]
        print(f'{label:<24} {seconds[0]:>10.2f} {seconds[1]:>10.2f}')
    identical = sum(all(left[key] == right[key] for key in ('full', 'windowed', 'hunks_only', 'overflow'))
                    for left, right in pairs)
    print(f'Cases identical: {identical}')
    old_only, new_only = old.keys() - new.keys(), new.keys() - old.keys()
    print(f'Cases present in only one file: {len(old_only) + len(new_only)} '
          f'(before: {len(old_only)}, after: {len(new_only)})')
    print(f'Cases with errors: {len(errors)} (before: {len(old_errors)}, after: {len(new_errors)})')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    measure_parser = commands.add_parser('run', help='measure local cases')
    measure_parser.add_argument('--cases', type=Path, required=True)
    measure_parser.add_argument('--out', type=Path, required=True)
    measure_parser.add_argument('--budget', type=int, default=REVIEW_CONTEXT_BUDGET_TOKENS)
    compare_parser = commands.add_parser('compare', help='compare recorded packing counts')
    compare_parser.add_argument('before', type=Path)
    compare_parser.add_argument('after', type=Path)
    args = parser.parse_args()
    if args.command == 'run':
        if args.budget < 0:
            parser.error('--budget must be nonnegative')
        if args.cases.resolve() == args.out.resolve():
            parser.error('--cases and --out must be different files')
        run(args.cases, args.out, args.budget)
    else:
        compare(args.before, args.after)


if __name__ == '__main__':
    main()
