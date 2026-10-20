import json
import sqlite3

import pytest

from argus.api import db
from argus.evalkit import labels


def database(tmp_path):
    path = tmp_path / 'copy.db'
    db.init_db(path)
    with sqlite3.connect(path) as conn:
        for i, label in enumerate(['fixed', 'rejected', 'deferred', 'none', 'none', 'rejected'], 1):
            conn.execute("INSERT INTO published_findings(id,review_id,repo,pr,head_sha,fingerprint,severity,path,line,title,placement,mode,posted_at) VALUES (?,?, 'o/r',1,'aaa',?,'major','x.py',2,'title','inline','posted','2026-09-10')", (i, i, str(i)))
            if label != 'none':
                conn.execute("INSERT INTO finding_outcomes VALUES (?, 'reply', ?, ?, '2026-09-11', '2026-09-11')", (i, label, str(i)))
                conn.execute("INSERT INTO reply_texts(finding_id,evidence_id,body,author_association,author_login) VALUES (?, ?, 'PRIVATE REPLY', 'OWNER', 'human')", (i, str(i)))
        conn.execute("INSERT INTO finding_outcomes VALUES (4,'addressed','true','b','2026-09-11',NULL)")
        conn.execute("INSERT INTO bot_actions(finding_id,kind,value,evidence_id,created_at) VALUES (6,'recheck','fixed','bbb','2026-09-12')")
    return path


@pytest.mark.parametrize('actions', [True, False])
def test_export(tmp_path, actions):
    path = database(tmp_path)
    if not actions:
        with sqlite3.connect(path) as conn:
            conn.execute('DROP TABLE bot_actions')
    before = path.read_bytes()
    out = tmp_path / 'labels.jsonl'
    assert labels.main(['--db', str(path), '--out', str(out), '--since', '2026-09-01']) == 0
    rows = [json.loads(s) for s in out.read_text().splitlines()]
    assert [r['label'] for r in rows] == ['strict_accepted', 'rejected', 'deferred', 'loose_accepted', 'unconfirmed', 'strict_accepted' if actions else 'rejected']
    assert rows[0]['polarity'] == 'positive' and rows[1]['polarity'] == 'negative'
    assert 'polarity' not in rows[2] and 'polarity' not in rows[3]
    assert 'PRIVATE' not in out.read_text() and 'body' not in out.read_text()
    assert path.read_bytes() == before


def test_summary_and_same_head_recheck(tmp_path):
    path = database(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE published_findings SET placement='summary',unverified=1 WHERE id=1")
        conn.execute("UPDATE bot_actions SET evidence_id='aaa' WHERE finding_id=6")
    with labels.readonly(path) as conn:
        rows = list(labels.labelled_findings(conn, '2026-09-01', '2026-10-01'))
    assert rows[0][1] == 'unconfirmed' and rows[-1][1] == 'rejected'
    out = tmp_path / 'labels'
    labels.export(path, out, '2026-10-01')
    assert out.read_text() == ''
    with pytest.raises(ValueError):
        labels.export(path, path)


def test_until_excludes_boundary(tmp_path):
    path = database(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE published_findings SET posted_at='2026-10-01' WHERE id=1")
    out = tmp_path / 'labels'
    assert labels.main(['--db', str(path), '--out', str(out), '--since', '2026-09-01',
                        '--until', '2026-10-01']) == 0
    assert len(out.read_text().splitlines()) == 5


@pytest.mark.parametrize('association', [None, 'NONE', 'CONTRIBUTOR'])
def test_export_does_not_count_untrusted_replies(tmp_path, association):
    path = database(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute('UPDATE reply_texts SET author_association=?', (association,))
    with labels.readonly(path) as conn:
        rows = list(labels.labelled_findings(conn))
    assert [row[1] for row in rows] == [
        'unconfirmed', 'unconfirmed', 'unconfirmed', 'loose_accepted', 'unconfirmed', 'strict_accepted']


def test_export_of_old_database_does_not_infer_reply_authority(tmp_path):
    path = database(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute('DROP TABLE reply_texts')
    with labels.readonly(path) as conn:
        rows = list(labels.labelled_findings(conn))
    assert rows[0][1] == 'unconfirmed'
