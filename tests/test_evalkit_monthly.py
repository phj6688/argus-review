import json

import pytest
import sqlite3

from argus.api import db
from argus.evalkit import monthly
from argus.evalkit.reports import average, rate


def test_monthly_empty_and_gate(tmp_path):
    path = tmp_path / 'copy.db'
    db.init_db(path)
    before = path.read_bytes()
    weekly = tmp_path / 'weekly.json'
    weekly.write_text('{}')
    out = tmp_path / 'out'
    assert monthly.main(['--db', str(path), '--weekly', str(weekly), '--month', '2026-09',
                         '--out-dir', str(out)]) == 0
    report = json.loads((out / 'report.json').read_text())
    assert report['gate']['criteria']['strict_acceptance'] == 'not evaluable (n = 0)'
    assert report['rows']['coverage']['kind'] == 'reviewer-independent'
    assert report['rows']['coverage']['current'] is None
    assert report['rows']['fair_head_coverage']['current']['n'] == 0
    assert report['rows']['visible_recall']['kind'] == 'reference-derived'
    assert report['rows']['reference_agreement']['biased'] is True
    text = (out / 'report.md').read_text()
    assert 'not available' in text and 'Wilson 95%' in text
    assert 'two consecutive' in text and 'operator' in text
    assert '| coverage | reviewer-independent | not available |' in text
    assert path.read_bytes() == before


def test_gate():
    values = {'coverage': rate(0, 100), 'fair_head_coverage': rate(95, 100),
              'strict': rate(65, 200), 'incidents': rate(7, 17),
              'inline_per_pr': average(700, 200), 'inline_per_review': average(700, 400)}
    gate = monthly.retirement_gate(values, 2.52)
    assert gate['passing'] is True
    assert set(gate['criteria'].values()) == {'pass'}
    for name, bad in [('fair_head_coverage', rate(94, 100)), ('strict', rate(64, 200)),
                      ('incidents', rate(6, 17)), ('inline_per_pr', average(705, 200))]:
        assert monthly.retirement_gate({**values, name: bad}, 2.52)['passing'] is False
    assert monthly.retirement_gate({**values, 'strict': rate(199, 199)}, 2.52)['criteria']['strict_acceptance'] == 'not evaluable (n = 199)'
    assert monthly.retirement_gate({**values, 'incidents': rate(7, 18)}, 2.52)['criteria']['incident_catches'].startswith('not evaluable')
    assert gate['thresholds']['inline_items'] == 'at most 3.52 per PR'


def test_month_window_and_live_reference(tmp_path):
    path = tmp_path / 'copy.db'
    db.init_db(path)
    with sqlite3.connect(path) as conn:
        for i, date in enumerate(['2026-08-31', '2026-09-01', '2026-09-30', '2026-10-01'], 1):
            conn.execute("INSERT INTO reviews (id,repo,pr,model,status,created_at,publish_status,published_at) VALUES (?,'o/r',1,'m','done',?,'posted',?)", (i, date, date))
            conn.execute("INSERT INTO published_findings(id,review_id,repo,pr,head_sha,fingerprint,severity,path,line,title,placement,mode,posted_at) VALUES (?,?, 'o/r',1,'aaa',?,'major','x.py',2,'title','inline','posted',?)", (i, i, str(i), date))
    reference = {'coverage': rate(9, 10), 'strict': rate(5, 10), 'loose': rate(6, 10),
                 'inline_per_reviewed_pr': average(30, 10)}
    report = monthly.build(path, {}, '2026-09', reference)
    assert report['rows']['strict_acceptance']['current']['n'] == 2
    assert report['rows']['inline_items']['current']['value'] == 2
    assert report['rows']['inline_items']['per_posted_review']['value'] == 1
    assert report['rows']['coverage']['reference'] == reference['coverage']
    assert report['rows']['strict_acceptance']['reference']['value'] == .5
    assert report['rows']['inline_items']['live_reference']['value'] == 3
    assert report['rows']['inline_items']['reference']['value'] == 3
    assert report['gate']['criteria']['inline_items'] == 'pass'
    assert report['gate']['thresholds']['inline_items'] == 'at most 4.00 per PR'


def test_coverage_uses_exact_reference_pr_set(tmp_path):
    path = tmp_path / 'copy.db'
    db.init_db(path)
    with sqlite3.connect(path) as conn:
        for repo, pr, status in [('o/r', 1, 'done'), ('o/r', 1, 'done'),
                                 ('o/r', 2, 'error'), ('o/other', 2, 'done'),
                                 ('o/r', 99, 'done')]:
            conn.execute('INSERT INTO reviews(repo,pr,model,status,created_at) VALUES (?,?,?, ?,?)',
                         (repo, pr, 'm', status, '2026-10-02'))
    prs = tmp_path / 'prs.jsonl'
    prs.write_text(''.join(json.dumps({'repo': repo, 'pr': pr, 'created_at': '2026-09-02',
                                      'reviewed_by_reference': reviewed}) + '\n'
                           for repo, pr, reviewed in [('o/r', 1, True), ('o/r', 2, True),
                                                      ('o/r', 3, False)]))
    weekly, reference, out = tmp_path / 'weekly.json', tmp_path / 'reference.json', tmp_path / 'out'
    weekly.write_text('{}')
    reference.write_text(json.dumps({'coverage': rate(2, 3)}))
    before = path.read_bytes()
    assert monthly.main(['--db', str(path), '--weekly', str(weekly), '--month', '2026-09',
                         '--reference', str(reference), '--reference-prs', str(prs),
                         '--out-dir', str(out)]) == 0
    report = json.loads((out / 'report.json').read_text())
    assert report['rows']['coverage']['current'] == rate(1, 3)
    assert report['rows']['coverage']['reference'] == rate(2, 3)
    assert report['rows']['fair_head_coverage']['current'] == rate(0, 0)
    assert report['gate']['criteria']['coverage'] == 'not evaluable'
    assert path.read_bytes() == before


def test_inline_volume_counts_distinct_repo_pr_pairs(tmp_path):
    path = tmp_path / 'copy.db'
    db.init_db(path)
    with sqlite3.connect(path) as conn:
        for i, (repo, pr, posted, date) in enumerate([
            ('o/r', 1, 'posted', '2026-09-01'), ('o/r', 1, 'posted', '2026-09-02'),
            ('o/other', 1, 'posted', '2026-09-03'), ('o/r', 2, 'posted', '2026-09-04'),
            ('o/r', 3, 'dry_run', '2026-09-05'), ('o/r', 4, 'posted', '2026-10-01'),
        ], 1):
            conn.execute('INSERT INTO reviews(id,repo,pr,model,status,created_at,publish_status,published_at) '
                         "VALUES (?,?,?,'m','done',?,?,?)", (i, repo, pr, date, posted, date))
            if pr == 2:
                continue  # Empty posted reviews still contribute to the denominator.
            for j in range(4):
                conn.execute('INSERT INTO published_findings(review_id,repo,pr,head_sha,fingerprint,'
                             'severity,path,line,title,placement,mode,posted_at) '
                             "VALUES (?,?,?,'aaa',?,'major','x.py',2,'title','inline',?,?)",
                             (i, repo, pr, f'{i}-{j}', posted, date))
        conn.execute("INSERT INTO published_findings(review_id,repo,pr,head_sha,fingerprint,severity,"
                     "path,line,title,placement,mode,posted_at) VALUES (1,'o/r',1,'aaa','body','major',"
                     "'x.py',2,'title','summary','posted','2026-09-01')")
    report = monthly.build(path, {}, '2026-09')
    volume = report['rows']['inline_items']
    assert volume['current'] == average(12, 3)
    assert volume['per_posted_review'] == average(12, 4)
    assert volume['reference']['value'] == 2.52
    assert report['gate']['criteria']['inline_items'] == 'fail'
    live = monthly.build(path, {}, '2026-09', {'inline_per_reviewed_pr': average(3, 1)})
    assert live['gate']['criteria']['inline_items'] == 'pass'
    assert live['rows']['inline_items']['reference']['value'] == 3



@pytest.mark.parametrize('date,fresh', [
    ('2026-08-31T23:59:59Z', False), ('2026-09-01T00:00:00Z', True),
    ('2026-10-07T23:59:59Z', True), ('2026-10-08T00:00:00Z', False),
    ('2026-10-08T00:30:00+02:00', True), (None, False), ('invalid', False),
])
def test_weekly_freshness(tmp_path, date, fresh):
    path = tmp_path / 'copy.db'
    db.init_db(path)
    weekly = {'date': date, 'sets': {'incidents': {'new': {'incidents': rate(7, 17)}},
                                    'heldout': {'new': {'visible': rate(5, 10)}}}}
    report = monthly.build(path, weekly, '2026-09')
    visible = report['rows']['visible_recall']
    if fresh:
        assert report['gate']['criteria']['incident_catches'] == 'pass'
        assert visible['current'] == rate(5, 10)
    else:
        assert report['gate']['criteria']['incident_catches'] == 'not evaluable (no weekly report for the month)'
        assert visible['current'] is None and str(date) in visible['note']
        text = monthly.markdown(report)
        assert '| visible recall | reference-derived | not available |' in text
        assert str(date) in text


def test_frozen_inline_reference_states_per_case_basis_and_source(tmp_path):
    path = tmp_path / 'copy.db'
    db.init_db(path)
    with sqlite3.connect(path) as conn:
        conn.execute('INSERT INTO reviews(id,repo,pr,model,status,created_at,publish_status,published_at) '
                     "VALUES (1,'o/r',1,'m','done','2026-09-01','posted','2026-09-01')")
        for j in range(4):
            conn.execute('INSERT INTO published_findings(review_id,repo,pr,head_sha,fingerprint,severity,'
                         'path,line,title,placement,mode,posted_at) '
                         "VALUES (1,'o/r',1,'aaa',?,'major','x.py',2,'title','inline','posted','2026-09-01')",
                         (str(j),))
    report = monthly.build(path, {}, '2026-09')
    reference = report['rows']['inline_items']['reference']
    assert reference['value'] == 2.52 and reference['n'] == 86
    assert reference['basis'] == 'per-case CodeRabbit rate'
    assert reference['source'] == '217 CodeRabbit inline comments over 86 held-out cases in eval/cr_heldout.jsonl'
    assert report['gate']['criteria']['inline_items'] == 'fail'
    assert report['gate']['thresholds']['inline_items'] == 'at most 3.52 per PR'
    text = monthly.markdown(report)
    assert ('2.52 (n = 86; Wilson 95% not applicable), per-case CodeRabbit rate; source: '
            '217 CodeRabbit inline comments over 86 held-out cases in eval/cr_heldout.jsonl') in text
    live = monthly.build(path, {}, '2026-09', {'inline_per_reviewed_pr': average(3, 1)})
    assert 'basis' not in live['rows']['inline_items']['reference']
    assert live['gate']['criteria']['inline_items'] == 'pass'


def test_frozen_total_reference_states_per_case_basis_and_source(tmp_path):
    path = tmp_path / 'copy.db'
    db.init_db(path)
    report = monthly.build(path, {}, '2026-09')
    total = report['rows']['inline_items']['reference_total']
    assert total['value'] == 3.05 and total['n'] == 86
    assert total['basis'] == 'per-case CodeRabbit rate'
    assert total['source'] == '262 CodeRabbit items over 86 held-out cases in eval/cr_heldout.jsonl'
    assert ('frozen total: 3.05 (n = 86; Wilson 95% not applicable), per-case CodeRabbit rate; '
            'source: 262 CodeRabbit items over 86 held-out cases in eval/cr_heldout.jsonl') in monthly.markdown(report)


def test_reference_basis_without_source_renders(tmp_path):
    path = tmp_path / 'copy.db'
    db.init_db(path)
    reference = {'inline_per_reviewed_pr': {**average(3, 1), 'basis': 'per reviewed PR'}}
    text = monthly.markdown(monthly.build(path, {}, '2026-09', reference))
    assert '3.00 (n = 1; Wilson 95% not applicable), per reviewed PR; frozen total: ' in text
