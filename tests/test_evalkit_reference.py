import json

import pytest

from argus.evalkit import reference


def test_reference(monkeypatch):
    roots = [{'id': i, 'user': {'login': 'reference', 'type': 'Bot'}, 'body': body,
              'original_commit_id': 'aaa' if i < 4 else 'old', 'path': 'x.py',
              'original_line': 10, 'original_start_line': 9, 'created_at': '2026-09-02'}
             for i, body in [(1, '✅ Addressed in commits abc'), (2, 'private'), (3, 'private'), (4, 'private')]]
    replies = [{'id': 10 + i, 'in_reply_to_id': parent, 'body': body,
                'user': {'login': 'human', 'type': kind}, 'created_at': f'2026-09-{3+i:02d}'}
               for i, (parent, body, kind) in enumerate([(1, 'Rejected this', 'User'),
                                                        (2, 'Rejected', 'Bot'), (2, 'Fixed this', 'User')])]
    def listing(path, params):
        if path.endswith('/pulls'):
            return [{'number': n, 'created_at': '2026-09-01', 'draft': n == 3,
                     'head': {'sha': 'bbb'}} for n in (1, 2, 3)]
        if path.endswith('/1/comments'):
            return roots + replies
        if path.endswith('/2/reviews'):
            return [{'user': {'login': 'reference'}}]
        return []

    compares = []
    def get(path):
        assert '/compare/' in path
        compares.append(path)
        class Response:
            def raise_for_status(self):
                pass
            def json(self):
                return {'status': 'diverged' if 'old' in path else 'ahead',
                        'files': [{'filename': 'x.py', 'patch': '@@ -10 +10 @@\n-old\n+new'}]}
        return Response()
    monkeypatch.setattr(reference.github, 'list_cached', listing)
    monkeypatch.setattr(reference.github, 'get', get)
    rows, summary, _ = reference.mine(['o/r'], '2026-09-01', '2026-10-01', 'reference')
    assert [r['label'] for r in rows] == ['strict_accepted', 'strict_accepted', 'unconfirmed', 'unconfirmed']
    assert rows[3]['loose'] is None and rows[2]['loose'] is True
    assert len(compares) == 2 and len(set(compares)) == 2
    assert all('body' not in r for r in rows)
    assert summary['coverage']['hits'] == 2 and summary['coverage']['n'] == 2
    assert summary['coverage']['wilson95'][0] < 1
    assert summary['strict']['hits'] == 2 and summary['loose']['hits'] == 3
    assert summary['inline_per_reviewed_pr']['value'] == 2


def test_changed_region_keeps_unconfirmed_label(monkeypatch):
    def listing(path, params):
        if path.endswith('/pulls'):
            return [{'number': 1, 'created_at': '2026-09-01', 'head': {'sha': 'b'}}]
        if path.endswith('/comments'):
            return [{'id': 1, 'user': {'login': 'reference'}, 'body': 'private',
                     'original_commit_id': 'a', 'original_line': 10, 'path': 'x'}]
        return []
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {'status': 'ahead', 'files': [{'filename': 'x', 'status': 'removed'}]}
    monkeypatch.setattr(reference.github, 'list_cached', listing)
    monkeypatch.setattr(reference.github, 'get', lambda *a: Response())
    rows, summary, _ = reference.mine(['o/r'], '2026-09-01', '2026-10-01', 'reference')
    assert rows[0]['label'] == 'unconfirmed'
    assert rows[0]['loose'] is True and 'polarity' not in rows[0]
    assert summary['loose']['hits'] == 1


def test_reference_exports_enumerated_prs(tmp_path, monkeypatch):
    def listing(path, params):
        if path.endswith('/pulls'):
            return [{'number': n, 'created_at': date, 'draft': n == 4,
                     'head': {'sha': 'head'}} for n, date in [
                         (1, '2026-09-01'), (2, '2026-09-02'), (3, '2026-09-03'),
                         (4, '2026-09-04'), (5, '2026-08-31'), (6, '2026-10-01')]]
        if path.endswith('/1/reviews'):
            return [{'user': {'login': 'REFERENCE'}}]
        if path.endswith('/2/comments'):
            return [{'id': 1, 'in_reply_to_id': 10, 'user': {'login': 'reference'}}]
        return []

    monkeypatch.setattr(reference.github, 'list_cached', listing)
    rows, summary, prs = [tmp_path / 'out' / name for name in ('rows.jsonl', 'summary.json', 'prs.jsonl')]
    assert reference.main(['--repos', 'o/r,o/r', '--since', '2026-09-01', '--until', '2026-10-01',
                           '--login', 'reference', '--out-rows', str(rows),
                           '--out-summary', str(summary), '--out-prs', str(prs)]) == 0
    assert [json.loads(line) for line in prs.read_text().splitlines()] == [
        {'repo': 'o/r', 'pr': 1, 'created_at': '2026-09-01', 'reviewed_by_reference': True},
        {'repo': 'o/r', 'pr': 2, 'created_at': '2026-09-02', 'reviewed_by_reference': True},
        {'repo': 'o/r', 'pr': 3, 'created_at': '2026-09-03', 'reviewed_by_reference': False},
    ]
    assert json.loads(summary.read_text())['coverage']['n'] == 3
    assert json.loads(summary.read_text())['coverage']['hits'] == 2
    assert rows.read_text() == ''


def test_pr_failure_isolated(monkeypatch):
    def listing(path, params):
        if path.endswith('/pulls'):
            return [{'number': n, 'created_at': '2026-09-01', 'head': {'sha': 'b'}} for n in (1, 2, 3)]
        if '/2/' in path:
            raise RuntimeError('read failed')
        if path.endswith('/reviews'):
            return [{'user': {'login': 'reference'}}]
        return []
    monkeypatch.setattr(reference.github, 'list_cached', listing)
    rows, summary, prs = reference.mine(['o/r'], '2026-09-01', '2026-10-01', 'reference')
    assert rows == [] and [p['pr'] for p in prs] == [1, 3]
    assert summary['failed_prs'] == 1
    assert summary['coverage']['n'] == summary['coverage']['hits'] == 2


def test_partial_pr_rows_discarded(monkeypatch):
    def listing(path, params):
        if path.endswith('/pulls'):
            return [{'number': 1, 'created_at': '2026-09-01', 'head': {'sha': 'b'}}]
        if path.endswith('/comments'):
            return [{'id': i, 'user': {'login': 'reference'}, 'body': '✅ Addressed in commit b',
                     **({'path': 'x'} if i == 1 else {})} for i in (1, 2)]
        return []
    monkeypatch.setattr(reference.github, 'list_cached', listing)
    rows, summary, prs = reference.mine(['o/r'], '2026-09-01', '2026-10-01', 'reference')
    assert rows == prs == []
    assert summary['failed_prs'] == 1 and summary['strict']['n'] == summary['coverage']['n'] == 0


def test_rate_limit_retries_same_request(monkeypatch):
    clock, sleeps, calls = [100], [], []
    monkeypatch.setattr(reference.time, 'time', lambda: clock[0])
    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
    monkeypatch.setattr(reference.time, 'sleep', sleep)
    def listing(path, params):
        calls.append(path)
        if path.endswith('/pulls'):
            return [{'number': 1, 'created_at': '2026-09-01', 'head': {'sha': 'b'}}]
        if len(calls) == 2:
            raise reference.github.GitHubRateLimited(200)
        return []
    monkeypatch.setattr(reference.github, 'list_cached', listing)
    _, summary, prs = reference.mine(['o/r'], '2026-09-01', '2026-10-01', 'reference')
    assert sleeps == [130] and calls[1] == calls[2]
    assert summary['failed_prs'] == 0 and len(prs) == 1


def test_rate_limit_wait_budget_is_shared(tmp_path, monkeypatch):
    clock, waits = [0], []
    monkeypatch.setattr(reference.time, 'time', lambda: clock[0])
    def sleep(seconds):
        clock[0] += seconds
        waits.append(seconds)
    monkeypatch.setattr(reference.time, 'sleep', sleep)
    def listing(path, params):
        raise reference.github.GitHubRateLimited(clock[0] + 3570)
    monkeypatch.setattr(reference.github, 'list_cached', listing)
    assert reference.main(['--repos', 'o/r', '--since', '2026-09-01', '--until', '2026-10-01',
                           '--out-rows', str(tmp_path / 'rows'), '--out-summary', str(tmp_path / 'summary')]) == 1
    assert waits == [3600, 3600, 3600]
    assert not (tmp_path / 'summary').exists()



@pytest.mark.parametrize('status,loose', [('identical', False), ('failed', None)])
def test_compare_once_per_pr_commit(monkeypatch, status, loose):
    def listing(path, params):
        if path.endswith('/pulls'):
            return [{'number': n, 'created_at': '2026-09-01', 'head': {'sha': 'b'}} for n in (1, 2)]
        if path.endswith('/comments'):
            return [{'id': i, 'user': {'login': 'reference'}, 'body': '', 'original_commit_id': 'a',
                     'original_line': 10, 'path': 'x'} for i in (1, 2)]
        return []
    calls = []
    def get(path):
        calls.append(path)
        if status == 'failed':
            raise RuntimeError('compare failed')
        class Response:
            def raise_for_status(self):
                pass
            def json(self):
                return {'status': status, 'files': []}
        return Response()
    monkeypatch.setattr(reference.github, 'list_cached', listing)
    monkeypatch.setattr(reference.github, 'get', get)
    rows, summary, _ = reference.mine(['o/r'], '2026-09-01', '2026-10-01', 'reference')
    assert len(calls) == 2 and len(rows) == 4
    assert all(r['loose'] is loose for r in rows)
    assert summary['loose_unknown'] == (4 if loose is None else 0)


def test_bad_pr_metadata_isolated(monkeypatch):
    def listing(path, params):
        if path.endswith('/pulls'):
            return [{'number': 1}, {'number': 2, 'created_at': '2026-09-01', 'head': {'sha': 'b'}}]
        return []
    monkeypatch.setattr(reference.github, 'list_cached', listing)
    _, summary, prs = reference.mine(['o/r'], '2026-09-01', '2026-10-01', 'reference')
    assert summary['failed_prs'] == 1 and summary['coverage']['n'] == 1
    assert [p['pr'] for p in prs] == [2]


def test_reset_beyond_wait_budget(monkeypatch):
    clock, waits = [0], []
    monkeypatch.setattr(reference.time, 'time', lambda: clock[0])
    def sleep(seconds):
        waits.append(seconds)
        clock[0] += seconds
    monkeypatch.setattr(reference.time, 'sleep', sleep)
    def listing(*args):
        raise reference.github.GitHubRateLimited(14400)
    monkeypatch.setattr(reference.github, 'list_cached', listing)
    with pytest.raises(reference.github.GitHubRateLimited):
        reference.mine(['o/r'], '2026-09-01', '2026-10-01', 'reference')
    assert waits == [10800]
