import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/context_packing_report.py'
pytestmark = pytest.mark.skipif(shutil.which('git') is None, reason='git is not installed')


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    environment = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], env=environment,
                              capture_output=True, check=True, text=True).stdout.strip()
    git('init', '-q')
    (repo / 'a file.py').write_text('value = 1\n')
    (repo / 'old.py').write_text('def unchanged():\n    return 42\n')
    (repo / 'invalid.txt').write_bytes(b'\xff\n')
    (repo / 'large.txt').write_bytes(b'x' * 1_000_001)
    (repo / 'limit.txt').write_bytes(b'x' * 1_000_000)
    def commit():
        git('add', '.')
        git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
            '-c', 'commit.gpgsign=false', 'commit', '-qm', 'test')
        return git('rev-parse', 'HEAD')
    base = commit()
    (repo / 'a file.py').write_text('value = 2\n')
    (repo / 'old.py').rename(repo / 'new.py')
    head = commit()
    return repo, base, head, environment


@pytest.mark.parametrize('bare', [False, True])
def test_run_and_compare_local_repository(tmp_path, repository, bare):
    repo, base, head, environment = repository
    if bare:
        mirror = tmp_path / 'mirror.git'
        subprocess.run(['git', 'clone', '--bare', '-q', str(repo), str(mirror)],
                       env=environment, check=True)
        repo = mirror
    cases, before, after = [tmp_path / name for name in ('cases.jsonl', 'before.jsonl', 'after.jsonl')]
    cases.write_text(json.dumps({'id': 'one', 'git_dir': str(repo), 'base': base, 'head': head}) + '\n')
    def command(*args):
        return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], env=environment,
                              capture_output=True, check=True, text=True).stdout
    command('run', '--cases', cases, '--out', before, '--budget', '1')
    command('run', '--cases', cases, '--out', after)
    old = json.loads(before.read_text())
    new = json.loads(after.read_text())
    assert old['files_changed'] == new['files_changed'] == 2
    assert old['files_with_patch'] == new['files_with_patch'] == 1
    assert old['overflow'] == 1 and old['full'] == old['reads'] == 0
    assert new['full'] == new['reads'] == 1
    assert new['overflow'] == new['windowed'] == new['hunks_only'] == 0
    assert new['patchless'] == old['patchless'] == 1
    assert new['estimated_tokens'] > 0 and new['budget'] == 120_000
    assert new['seconds'] >= 0 and 'error' not in new
    assert 'value = ' not in after.read_text()
    report = command('compare', before, after)
    assert 'Cases compared: 1' in report
    assert 'Overflow files' in report
    assert 'Files with a view' in report
    assert re.search(r'^Cases with overflow\s+1\s+0$', report, re.MULTILINE)
    assert re.search(r'^Overflow files\s+1\s+0$', report, re.MULTILINE)
    assert re.search(r'^Files with a view\s+0\s+1$', report, re.MULTILINE)
    assert re.search(r'^Full views\s+0\s+1$', report, re.MULTILINE)
    assert 'Cases identical: 0' in report
    assert 'Cases present in only one file: 0' in report
    assert 'Cases with errors: 0' in report
    assert 'Cases identical: 1' in command('compare', after, after)


def test_run_keeps_going_after_invalid_case(tmp_path, repository):
    repo, base, head, environment = repository
    cases, output = tmp_path / 'cases.jsonl', tmp_path / 'results.jsonl'
    valid = {'id': 'good', 'git_dir': str(repo), 'base': base, 'head': head}
    cases.write_text('\n'.join(json.dumps(case) for case in [
        {**valid, 'id': 'bad-base', 'base': '--stat'},
        {**valid, 'id': 'bad-head', 'head': 'abcd'},
        {**valid, 'id': 'missing', 'head': '0' * 40}, valid]) + '\n')
    subprocess.run([sys.executable, str(SCRIPT), 'run', '--cases', str(cases), '--out', str(output)],
                   env=environment, check=True, capture_output=True)
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert [row['id'] for row in rows] == ['bad-base', 'bad-head', 'missing', 'good']
    assert all(row['error'] for row in rows[:3])
    assert rows[3]['full'] == 1 and 'error' not in rows[3]


def test_reader_rejects_missing_large_and_non_utf8_blobs(repository):
    from scripts.context_packing_report import read_at_head
    repo, _, head, _ = repository
    git_dir = repo / '.git'
    assert read_at_head(git_dir, head, 'missing') is None
    assert read_at_head(git_dir, head, 'invalid.txt') is None
    assert read_at_head(git_dir, head, 'large.txt') is None
    assert read_at_head(git_dir, head, 'limit.txt') == 'x' * 1_000_000
    assert read_at_head(git_dir, head, 'a file.py') == 'value = 2\n'


def test_measure_accepts_results_without_patchless_field(repository, monkeypatch):
    from scripts import context_packing_report as report
    original = report.render_review_context
    def old_result(*args):
        result = original(*args)
        del result.patchless
        return result
    monkeypatch.setattr(report, 'render_review_context', old_result)
    repo, base, head, _ = repository
    row = {'reads': 0}
    report.measure({'git_dir': str(repo), 'base': base, 'head': head}, 120_000, row)
    assert row['patchless'] is None
    assert row['full'] == 1 and row['reads'] == 1


def test_compare_counts_errors_unmatched_cases_and_ignores_timing(tmp_path, capsys):
    from scripts.context_packing_report import compare
    before, after = tmp_path / 'before.jsonl', tmp_path / 'after.jsonl'
    row = {'id': 'same', 'files_changed': 2, 'files_with_patch': 1, 'full': 1,
           'windowed': 0, 'hunks_only': 0, 'overflow': 0, 'patchless': None,
           'estimated_tokens': 50, 'budget': 120_000, 'reads': 1, 'seconds': .1}
    before.write_text('\n'.join(map(json.dumps, [row, {**row, 'id': 'before-only'},
                                                {'id': 'failed', 'error': 'failed'}])))
    after.write_text('\n'.join(map(json.dumps, [
        {**row, 'seconds': .9, 'reads': 2}, {**row, 'id': 'after-only'},
        {'id': 'failed', 'error': 'failed'}])))
    compare(before, after)
    report = capsys.readouterr().out
    assert 'Cases compared: 1' in report
    assert 'Cases identical: 1' in report
    assert 'Cases present in only one file: 2 (before: 1, after: 1)' in report
    assert 'Cases with errors: 1 (before: 1, after: 1)' in report
    assert 'Max render seconds' in report and 'Median render seconds' in report
