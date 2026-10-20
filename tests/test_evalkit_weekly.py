import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from argus.evalkit import weekly
from argus.evalkit.cases import Case, Expectation
from argus.evalkit.score import finding_key


def fixture_set(tmp_path):
    root = tmp_path / 'eval'
    (root / 'analysis/cases').mkdir(parents=True)
    (root / 'eval/snapshot-cache').mkdir(parents=True)
    (root / 'eval/snapshot-cache/input').write_text('frozen')
    cases = [Case(str(i), f'o/r{i % 2}', i, 'aaa', 'bbb', 'heldout', [
        Expectation(f'e{i}', 'x.py', 2, 2, 'major', 'bug', 'Missing check', 'manual')]) for i in range(5)]
    for suffix, group in [('cr', cases[:4]), ('incidents', cases[4:])]:
        (root / f'analysis/cases/cases_eval_{suffix}.jsonl').write_text(
            ''.join(json.dumps(asdict(c)) + '\n' for c in group))
    return root, cases


def test_freeze(tmp_path):
    root, cases = fixture_set(tmp_path)
    a = weekly.freeze_subset(root, tmp_path / 'a', 2, 42)
    b = weekly.freeze_subset(root, tmp_path / 'b', 2, 42)
    assert a == b and a['incidents'] == ['4'] and len(a['heldout']) == 2
    assert len({cases[int(i)].repo for i in a['heldout']}) == 2
    with pytest.raises(FileExistsError):
        weekly.freeze_subset(root, tmp_path / 'a', 2, 42)
    cache = weekly.snapshot_cache(root, tmp_path / 'a')
    assert (cache / 'input').read_text() == 'frozen'
    (root / 'eval/snapshot-cache/input').write_text('new')
    assert (weekly.snapshot_cache(root, tmp_path / 'a') / 'input').read_text() == 'frozen'


@pytest.mark.parametrize('visible,incidents,errors,alert', [
    (7, 7, .05, False), (6, 7, .05, True), (9, 6, 0, True), (10, 7, .051, True)])
def test_alert(visible, incidents, errors, alert):
    assert weekly.should_alert(visible, [9, 11], incidents, [7, 8], errors) is alert


def row(case, title='Missing check'):
    return {'case_id': case.case_id, 'variant': 'v', 'run': 1, 'error': None,
            'reported': [{'file': 'x.py', 'line': 2, 'title': title, 'severity': 'major',
                          'description': 'd', 'evidence': 'e', 'proposed_fix': 'f'}], 'raw': [],
            'snapshot': {'files': [{'filename': 'x.py', 'status': 'modified',
                                   'patch': '@@ -1,2 +1,2 @@\n a\n-b\n+c'}]}}


@pytest.mark.parametrize('alert', [False, True])
@pytest.mark.parametrize('image_identity', [False, True])
def test_run(tmp_path, monkeypatch, alert, image_identity):
    root, cases = fixture_set(tmp_path)
    state, out = tmp_path / 'state', tmp_path / 'out'
    subset = weekly.freeze_subset(root, state, 2, 42)
    for name, group in [('heldout', cases[:4]), ('incidents', cases[4:])]:
        for prefix in ('v5p1', 'after'):
            (root / f'eval/{prefix}_{name}.jsonl').write_text(
                ''.join(json.dumps(row(c)) + '\n' for c in group))
    (state / 'calibration.json').write_text(json.dumps({'agreement': .9, 'kappa': .8}))
    monkeypatch.setattr(weekly, 'code_version', lambda: ('checkout-id', True))
    if image_identity:
        monkeypatch.setenv('ARGUS_EVAL_IMAGE_ID', 'image-id')
    else:
        monkeypatch.delenv('ARGUS_EVAL_IMAGE_ID', raising=False)
    calls = []

    def replay(command, **kwargs):
        calls.append(command)
        assert command[command.index('--cache-dir') + 1] == str(state / 'snapshot-cache')
        assert command[command.index('--mirror-root') + 1] == '/mirrors'
        assert command[command.index('--concurrency') + 1] == '1'
        assert command[command.index('--github-mode') + 1] == 'mirror'
        ids = command[command.index('--case-ids') + 1].split(',')
        target = command[command.index('--out') + 1]
        with open(target, 'w') as f:
            for c in cases:
                if c.case_id in ids:
                    record = row(c, 'New check')
                    if alert and c.case_id == '4':
                        record.update(error={'class': 'Failure'}, reported=[])
                    f.write(json.dumps(record) + '\n')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(weekly.subprocess, 'run', replay)
    from argus.evalkit import judge
    monkeypatch.setattr(judge, 'complete_tool', lambda *a: {'same_issue': True, 'reason': 'same'})
    assert weekly.run(root, state, out, '/mirrors', 'm', 'j', 1) == (3 if alert else 0)
    report = json.loads((out / 'report.json').read_text())
    assert report['pairs_judged'] == (5 if alert else 6) and report['alert'] is alert
    assert report['reviewer_model'] == 'm' and report['judge_model'] == 'j'
    image = 'image-id' if image_identity else None
    assert report['prompt_sha'] == judge.prompt_sha() and report['code_sha'] == 'checkout-id'
    assert report['image_id'] == image
    text = (out / 'report.md').read_text()
    assert 'Code: checkout-id.' in text and f'Image: {image or "not recorded"}.' in text
    assert judge.prompt_sha() in text
    assert report['calibration']['agreement'] == .9
    assert report['sets']['heldout']['new']['visible']['hits'] == len(subset['heldout'])
    assert report['sets']['heldout']['new']['inline']['hits'] == 2
    assert report['sets']['incidents']['new']['incidents']['hits'] == (0 if alert else 1)
    assert len((state / 'judge_api.jsonl').read_text().splitlines()) == (5 if alert else 6)
    assert 'Wilson' in (out / 'report.md').read_text() and len(calls) == 2
    assert weekly.run(root, state, out, '/mirrors', 'm', 'j', 1) == (3 if alert else 0)
    assert json.loads((out / 'report.json').read_text())['pairs_judged'] == 0


def test_unverified_and_same_file(tmp_path):
    _, cases = fixture_set(tmp_path)
    r = row(cases[0])
    f = {**r['reported'][0], 'line': 900, 'verdict_is_real': False, 'lane': 'llm'}
    r.update(reported=[], raw=[f, {**f, 'severity': 'minor'}, {**f, 'lane': 'static'}])
    pairs = weekly.pairs_for([cases[0]], [r])
    assert len(pairs) == 1 and pairs[0]['finding']['line'] == 900
    scores = weekly.metrics([cases[0]], [r], {('e0', finding_key('0', f)): True})
    assert scores['visible']['hits'] == 1 and scores['inline']['hits'] == 0
    assert scores['findings_per_case']['value'] == 1


def test_missing_baseline_and_operational_failure(tmp_path, monkeypatch):
    _, cases = fixture_set(tmp_path)
    with pytest.raises(ValueError, match='exactly one'):
        weekly.metrics(cases, [], {})
    with pytest.raises(ValueError, match='exactly one'):
        weekly.metrics(cases[:1], [row(cases[0]), row(cases[0])], {})
    assert weekly.main(['run', '--eval-dir', str(tmp_path), '--state-dir', str(tmp_path),
                        '--out-dir', str(tmp_path / 'out'), '--mirror-root', '/m',
                        '--model', 'm', '--judge-model', 'j']) == 1


def test_inline_limit_and_missing_snapshot(tmp_path):
    _, cases = fixture_set(tmp_path)
    r = row(cases[0])
    r['reported'] = [{**r['reported'][0], 'title': str(i), 'confidence': i / 10} for i in range(6)]
    assert [f['title'] for f in weekly.inline_findings(r)] == ['2', '3', '4', '5']
    r['snapshot'] = None
    assert weekly.metrics(cases[:1], [r], {})['inline'] is None


@pytest.fixture
def ready_run(tmp_path):
    root, cases = fixture_set(tmp_path)
    state, out = tmp_path / 'state', tmp_path / 'out'
    weekly.freeze_subset(root, state, 2, 42)
    for name, group in [('heldout', cases[:4]), ('incidents', cases[4:])]:
        for baseline in ('v5p1', 'after'):
            (root / f'eval/{baseline}_{name}.jsonl').write_text(
                ''.join(json.dumps(row(c)) + '\n' for c in group))
    return root, state, out, cases


def run_args(ready_run):
    root, state, out, _ = ready_run
    return ['run', '--eval-dir', str(root), '--state-dir', str(state), '--out-dir', str(out),
            '--mirror-root', '/mirrors', '--model', 'm', '--judge-model', 'j']


@pytest.mark.parametrize('baseline', ['v5p1_heldout', 'after_heldout', 'v5p1_incidents', 'after_incidents'])
@pytest.mark.parametrize('missing', [False, True])
def test_baselines_checked_before_replay(ready_run, monkeypatch, baseline, missing):
    root, _, _, _ = ready_run
    path = root / f'eval/{baseline}.jsonl'
    if missing:
        path.unlink()
    else:
        path.write_text('')
    calls = []
    monkeypatch.setattr(weekly.subprocess, 'run', lambda *a, **kw: calls.append(a))
    assert weekly.main(run_args(ready_run)) == 1
    assert calls == []


@pytest.mark.parametrize('field,value', [('judge', 'other'), ('prompt_sha', 'old')])
def test_refuses_mixed_judge_identity(ready_run, monkeypatch, capsys, field, value):
    from argus.evalkit import judge
    _, state, _, _ = ready_run
    records = [{'expectation_id': str(i), 'finding_key': str(i), 'same_issue': True,
                'judge': 'j', 'source': 'api', 'prompt_sha': judge.prompt_sha()} for i in range(2)]
    records[1][field] = value
    (state / 'judge_api.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in records))
    calls = []
    monkeypatch.setattr(weekly.subprocess, 'run', lambda *a, **kw: calls.append(a))
    assert weekly.main(run_args(ready_run)) == 1
    assert 'new state directory' in capsys.readouterr().err
    assert calls == []


def test_replay_timeout_is_failure(ready_run, monkeypatch, capsys):
    import subprocess

    def replay(command, **kwargs):
        assert kwargs['timeout'] <= 12
        raise subprocess.TimeoutExpired(command, kwargs['timeout'])
    monkeypatch.setattr(weekly.subprocess, 'run', replay)
    assert weekly.main(run_args(ready_run) + ['--replay-timeout-s', '12']) == 1
    assert 'TimeoutExpired' in capsys.readouterr().err


def test_deadline_before_each_judge_call(ready_run, monkeypatch, capsys):
    from argus.evalkit import judge
    from pathlib import Path

    _, state, out, cases = ready_run
    clock = [0]
    monkeypatch.setattr(weekly.time, 'monotonic', lambda: clock[0])
    def replay(command, **kwargs):
        ids = command[command.index('--case-ids') + 1].split(',')
        Path(command[command.index('--out') + 1]).write_text(
            ''.join(json.dumps(row(c)) + '\n' for c in cases if c.case_id in ids))
    calls = []
    def decide(*args):
        calls.append(args)
        clock[0] = 11
        return {'same_issue': True, 'reason': 'same'}
    monkeypatch.setattr(weekly.subprocess, 'run', replay)
    monkeypatch.setattr(judge, 'complete_tool', decide)
    assert weekly.main(run_args(ready_run) + ['--deadline-s', '10']) == 1
    assert 'deadline' in capsys.readouterr().err
    assert len(calls) == 1
    assert len((state / 'judge_api.jsonl').read_text().splitlines()) == 1
    assert not (out / 'report.json').exists()


def test_refuses_three_vote_cache_in_one_vote_state(ready_run, monkeypatch, capsys):
    from argus.evalkit import judge
    _, state, _, _ = ready_run
    monkeypatch.setattr(judge, 'complete_tool', lambda *a: {'same_issue': True, 'reason': 'same'})
    pair = {'expectation_id': '0', 'finding_key': '0', 'expectation': {}, 'finding': {}}
    judge.judge_pairs([pair], 'j', [], state / 'judge_api.jsonl', votes=3)
    calls = []
    monkeypatch.setattr(weekly.subprocess, 'run', lambda *a, **kw: calls.append(a))
    assert weekly.main(run_args(ready_run)) == 1
    assert 'new state directory' in capsys.readouterr().err
    assert calls == []
