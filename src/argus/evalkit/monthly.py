"""Monthly side-by-side outcomes and an operator retirement proposal."""

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from argus.api.watch_metrics import coverage

from .labels import labelled_findings
from .reports import average, display, rate, readonly, write_report
from .score import _read_jsonl

# The frozen values divide by held-out cases (review heads), not by distinct PRs.
FROZEN_INLINE_REFERENCE = {
    'value': 2.52, 'n': 86, 'wilson95': None, 'basis': 'per-case CodeRabbit rate',
    'source': '217 CodeRabbit inline comments over 86 held-out cases in eval/cr_heldout.jsonl'}
FROZEN_TOTAL_REFERENCE = {
    'value': 3.05, 'n': 86, 'wilson95': None, 'basis': 'per-case CodeRabbit rate',
    'source': '262 CodeRabbit items over 86 held-out cases in eval/cr_heldout.jsonl'}


def month_window(month):
    start = datetime.strptime(month, '%Y-%m').replace(tzinfo=timezone.utc)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return start, end


def retirement_gate(values, reference_inline):
    criteria = {}
    for key, name, threshold, minimum in (
        ('fair_head_coverage', 'coverage', .95, True), ('strict', 'strict_acceptance', .322, True),
        ('inline_per_pr', 'inline_items', reference_inline + 1, False),
    ):
        value = values.get(key)
        if not value or value.get('value') is None:
            criteria[name] = 'not evaluable'
        else:
            passing = value['value'] >= threshold if minimum else value['value'] <= threshold
            criteria[name] = 'pass' if passing else 'fail'
    strict = values.get('strict') or {}
    if strict.get('n', 0) < 200:
        criteria['strict_acceptance'] = f"not evaluable (n = {strict.get('n', 0)})"
    incidents = values.get('incidents') or {}
    criteria['incident_catches'] = ('pass' if incidents['hits'] >= 7 else 'fail') if incidents.get('n') == 17 else 'not evaluable (requires 17 incidents)'
    passing = all(v == 'pass' for v in criteria.values())
    summary = ('All criteria pass for this month.' if passing else 'The retirement gate does not pass for this month.')
    return {'criteria': criteria, 'passing': passing, 'summary': summary,
            'thresholds': {'coverage': 'at least 95%',
                           'strict_acceptance': 'at least 32.2% over at least 200 inline findings',
                           'incident_catches': 'at least 7 of 17',
                           'inline_items': f'at most {reference_inline + 1:.2f} per PR'},
            'decision': 'The gate needs two consecutive passing months. The decision stays with the operator.'}


def build(db, weekly, month, reference=None, reference_prs=None):
    start, end = month_window(month)
    with readonly(db) as conn:
        watched = coverage(conn, start, end)
        findings = list(labelled_findings(conn, start.isoformat(), end.isoformat()))
        inline = [(label, loose) for f, label, loose in findings if f['placement'] == 'inline']
        posted = conn.execute(
            "SELECT repo,pr FROM reviews WHERE publish_status='posted' "
            'AND julianday(published_at)>=julianday(?) AND julianday(published_at)<julianday(?)',
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        pr_coverage = None
        if reference_prs is not None:
            prs = {(r['repo'], r['pr']) for r in _read_jsonl([reference_prs])}
            done = sum(bool(conn.execute(
                "SELECT 1 FROM reviews WHERE repo=? AND pr=? AND status='done' LIMIT 1", pair,
            ).fetchone()) for pair in prs)
            pr_coverage = rate(done, len(prs))
    try:
        stamp = datetime.fromisoformat(weekly.get('date') or '')
        stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
        fresh = start <= stamp < end + timedelta(days=7)
    except ValueError:
        fresh = False
    weekly_sets = weekly.get('sets', {}) if fresh else {}
    heldout = weekly_sets.get('heldout', {})
    incidents = weekly_sets.get('incidents', {}).get('new', {}).get('incidents')
    current = {'coverage': pr_coverage,
               'fair_head_coverage': rate(watched['done_heads'], watched['eligible_heads']),
               'incidents': incidents,
               'strict': rate(sum(label == 'strict_accepted' for label, _ in inline), len(inline)),
               'loose': rate(sum(loose for _, loose in inline), len(inline)),
               'inline_per_pr': average(len(inline), len({(r['repo'], r['pr']) for r in posted})),
               'inline_per_review': average(len(inline), len(posted)),
               'errors': rate(watched['errored_attempts'], watched['finished_attempts'])}
    reference = reference or {}
    reference_inline = reference.get('inline_per_reviewed_pr')
    if (reference_inline or {}).get('value') is None:
        reference_inline = dict(FROZEN_INLINE_REFERENCE)

    def row(kind, value, ref=None, **extra):
        return {'kind': kind, 'current': value, 'reference': ref, **extra}

    rows = {
        'coverage': row('reviewer-independent', current['coverage'], reference.get('coverage'),
                        note='PRs from the reference PR set with at least one done review in the database copy.'),
        'fair_head_coverage': row('reviewer-independent', current['fair_head_coverage'],
                        note='Eligible heads with a fair review opportunity in the month.'),
        'incident_catches': row('reviewer-independent', incidents, rate(0, 17)),
        'visible_recall': row('reference-derived', heldout.get('new', {}).get('visible'),
                              baselines={name: heldout.get(name, {}).get('visible') for name in ('v5p1', 'after')}),
        'strict_acceptance': row('reviewer-independent', current['strict'], reference.get('strict', rate(485, 1505))),
        'loose_acceptance': row('reviewer-independent', current['loose'], reference.get('loose', rate(608, 1505))),
        'inline_items': row('reviewer-independent', current['inline_per_pr'], reference_inline,
                            per_posted_review=current['inline_per_review'],
                            note='Posted inline findings per distinct repository and PR with a posted review in the month.',
                            reference_total=dict(FROZEN_TOTAL_REFERENCE),
                            live_reference=reference.get('inline_per_reviewed_pr')),
        'error_share': row('reviewer-independent', current['errors']),
        'reference_agreement': row('reference-derived', None, biased=True,
                                   note='Biased toward reference-accepted defects.'),
    }
    gate = retirement_gate(current, reference_inline['value'])
    if not fresh:
        gate['criteria']['incident_catches'] = 'not evaluable (no weekly report for the month)'
        rows['visible_recall']['note'] = f"Weekly report date: {weekly.get('date')}. No weekly report for the month."
    return {'date': datetime.now(timezone.utc).isoformat(), 'month': month, 'rows': rows, 'gate': gate,
            'weekly_date': weekly.get('date'), 'reference_period': {k: reference.get(k) for k in ('since', 'until')}}


def _labelled(value):
    text = display(value)
    if (value or {}).get('basis'):
        text += f", {value['basis']}"
        if value.get('source'):
            text += f"; source: {value['source']}"
    return text


def markdown(report):
    lines = [f"# Monthly outcomes {report['month']}", '', f"Generated {report['date']}.",
             '', '| Metric | Basis | Current | Reference |', '| --- | --- | --- | --- |']
    for name, row in report['rows'].items():
        current = display(row['current'])
        if 'per_posted_review' in row:
            current += ' per PR; per posted review: ' + display(row['per_posted_review'])
        reference = _labelled(row.get('reference'))
        if 'baselines' in row:
            reference = '; '.join(f'{k}: {display(v)}' for k, v in row['baselines'].items())
        if 'reference_total' in row:
            reference += '; frozen total: ' + _labelled(row['reference_total'])
        lines.append(f"| {name.replace('_', ' ')} | {row['kind']} | {current} | {reference} |")
    lines += ['', *[row['note'] for row in report['rows'].values() if row.get('note')]]
    lines += ['', 'PR coverage uses the reference PR set. The gate uses fair eligible-head coverage.',
              'Acceptance divides by all posted inline findings. Inline volume divides by distinct PRs with a posted review in the month.',
              'Reference agreement is biased toward reference-accepted defects.',
              'Item averages have no Wilson interval. Missing values are not available.',
              '', '## Retirement gate (proposal)', '']
    for name, result in report['gate']['criteria'].items():
        lines.append(f"- {name.replace('_', ' ')} ({report['gate']['thresholds'][name]}): {result}.")
    lines += ['', report['gate']['summary'], report['gate']['decision']]
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('db', 'weekly', 'month', 'out-dir'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--reference')
    parser.add_argument('--reference-prs')
    args = parser.parse_args(argv)
    weekly = json.loads(Path(args.weekly).read_text(encoding='utf-8'))
    reference = json.loads(Path(args.reference).read_text(encoding='utf-8')) if args.reference else None
    report = build(args.db, weekly, args.month, reference, args.reference_prs)
    write_report(args.out_dir, report, markdown(report))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
