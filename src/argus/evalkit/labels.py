"""Export current acceptance labels without reply text from a database copy."""

import argparse
import json
from collections import defaultdict
from pathlib import Path

from argus.acceptance import _signals

from .reports import readonly


def labelled_findings(conn, since=None, until=None):
    outcomes, actions = defaultdict(list), defaultdict(list)
    has_reply_texts = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reply_texts'").fetchone()
    outcome_query = (
        "SELECT o.*,t.author_association FROM finding_outcomes o "
        "LEFT JOIN reply_texts t ON o.kind='reply' AND o.finding_id=t.finding_id AND o.evidence_id=t.evidence_id"
        if has_reply_texts else "SELECT *,NULL AS author_association FROM finding_outcomes"
    )
    for row in conn.execute(outcome_query):
        outcomes[row['finding_id']].append(row)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bot_actions'").fetchone():
        for row in conn.execute('SELECT * FROM bot_actions'):
            actions[row['finding_id']].append(row)
    for finding in conn.execute(
        "SELECT * FROM published_findings WHERE mode='posted' "
        'AND (? IS NULL OR julianday(posted_at)>=julianday(?)) '
        'AND (? IS NULL OR julianday(posted_at)<julianday(?)) ORDER BY id',
        (since, since, until, until),
    ):
        finding = dict(finding)
        signals, checks = outcomes[finding['id']], actions[finding['id']]
        if finding['placement'] != 'inline':
            signals = [o for o in signals if o['kind'] == 'addressed']
            checks = []
        label, loose, _ = _signals(signals, checks, finding['head_sha'])
        yield finding, label, loose


def export(db, out, since=None, until=None):
    target = Path(out)
    if target.resolve() == Path(db).resolve():
        raise ValueError('output must differ from database copy')
    target.parent.mkdir(parents=True, exist_ok=True)
    with readonly(db) as conn, target.open('w', encoding='utf-8') as stream:
        for finding, label, _ in labelled_findings(conn, since, until):
            row = {k: finding[k] for k in ('repo', 'pr', 'fingerprint', 'severity', 'placement')}
            row.update(head=finding['head_sha'], unverified=bool(finding.get('unverified')), label=label)
            if label in {'strict_accepted', 'rejected'}:
                row['polarity'] = 'positive' if label == 'strict_accepted' else 'negative'
            stream.write(json.dumps(row, ensure_ascii=True) + '\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--since')
    parser.add_argument('--until')
    args = parser.parse_args(argv)
    export(args.db, args.out, args.since, args.until)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
