"""Structural checks settle impossible fixes before inference."""

import difflib
import json

import httpx
import pytest

from argus import acceptance, client, github, replies
from argus.api import db
import test_replies as fixtures
from test_acceptance import rows
from test_replies import setup_loop
from reply_helpers import file_entry

database = fixtures.database
live = fixtures.live
REAL_RECHECK = replies.recheck


@pytest.mark.parametrize('shape', ['unchanged', 'outside', 'deleted', 'short', 'distant',
                                   'evidence', 'reflow', 'move', 'missing', 'null', 'empty_evidence'])
@pytest.mark.parametrize('requested', [False, True])
def test_impossible_fixed_skips_model_and_records_once(live, monkeypatch, shape, requested):
    _, _, writes, _ = setup_loop(live, monkeypatch, text='recheck' if requested else None, resolved=not requested)
    monkeypatch.setattr(replies, 'recheck', REAL_RECHECK)
    old = [f'context_{n}' for n in range(9)] + ['unsafe_call(value)', 'unchanged_context']
    new = old.copy()
    evidence = 'unsafe_call(value)'
    extra = []
    if shape == 'outside':
        new[8] = 'another_context'
    elif shape == 'deleted':
        del new[9]
    elif shape == 'short':
        new[9] = 'pass'
    elif shape == 'distant':
        new[9] = 'pass'
        new += ['#'] * 4 + ['validate(value)']
    elif shape == 'evidence':
        new[9:10] = ['validate(value)', 'unsafe_call(value)']
    elif shape == 'reflow':
        new[9:10] = ['unsafe_call(', '    value', ')']
    elif shape == 'move':
        new[9] = 'validate(value)'
        extra = [file_entry(filename='other.py', patch='@@ -0,0 +1 @@\n+return unsafe_call(value)')]
    elif shape in {'missing', 'null'}:
        new[9] = 'validate(value)'
        extra = [dict(filename='other.py', **({'patch': None} if shape == 'null' else {}))]
    elif shape == 'empty_evidence':
        evidence = ''
        new[10] = 'validate(value)'
    patch = '\n'.join(list(difflib.unified_diff(old, new, lineterm=''))[2:])
    files = [file_entry(filename='app.py', patch=patch), *extra] if shape != 'unchanged' else []
    monkeypatch.setattr(github, 'get', lambda url, *a: httpx.Response(
        200, json=dict(status='ahead', files=files), request=httpx.Request('GET', 'https://api.github.com' + url)))
    monkeypatch.setattr(replies, '_read_file', lambda *a: '\n'.join(old if a[2] == 'a' * 40 else new))
    with db.connect(live) as conn:
        conn.execute('UPDATE published_findings SET end_line=11')
        row = conn.execute('SELECT review_id,fingerprint FROM published_findings').fetchone()
        conn.execute('INSERT INTO findings(review_id,file,line,severity,confidence,title,description,proposed_fix,'
                     'is_bug,verdict_is_real,verdict_confidence,fingerprint,evidence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     (row[0], 'app.py', 10, 'major', 0.9, 'Unsafe call', 'Unchecked input', 'validate',
                      1, 1, 0.9, row[1], evidence))
    for _ in range(2):
        acceptance.collect(live)
    checks = [a for a in rows(live, 'bot_actions') if a['kind'] == 'recheck']
    # Unchanged regions need no automatic check; explicit requests still get an answer.
    expected_check = requested or shape != 'unchanged'
    assert [a['value'] for a in checks] == ['unsure'] * expected_check
    assert acceptance.stats(live)['last_run']['failures'] == 0
    if requested:
        body = 'No change to this code since aaaaaaa.' if shape == 'unchanged' else 'Not sure at bbbbbbb.'
        assert [w[1].splitlines()[0] for w in writes] == [body]
    else:
        assert not writes


@pytest.mark.parametrize('call', ['eval(user_input)', 'eval(compile(user_input, filename, mode))'])
@pytest.mark.parametrize('other_file', [False, True])
def test_exact_call_move_survives_assignment_change(live, monkeypatch, call, other_file):
    old = ['result = ' + call, 'return result']
    new = ['result = validate(user_input)', 'return result']
    files = []
    if other_file:
        files.append(file_entry(filename='helper.py', patch='@@ -0,0 +1 @@\n+return ' + call))
    else:
        new.extend(['# padding'] * 30 + ['return ' + call])
    patch = '\n'.join(list(difflib.unified_diff(old, new, lineterm=''))[2:])
    files.append(file_entry(filename='app.py', patch=patch))
    monkeypatch.setattr(replies, '_read_file', lambda *a: '\n'.join(old if a[2] == 'a' * 40 else new))
    finding = dict(path='app.py', head_sha='a' * 40, line=1, evidence='',
                   comparison=dict(status='ahead', files=files))
    assert REAL_RECHECK('owner/repo', finding, 'b' * 40) == dict(verdict='unsure', quote='')


def test_prompt_requests_complete_changed_lines(live, monkeypatch):
    monkeypatch.setattr(replies, '_read_file', lambda *a: 'result = validate(value)')
    def model(model_id, system, user, spec):
        assert 'complete changed lines' in system
        assert json.loads(user)['current_range'] == [1, 1]
        return dict(verdict='fixed', quote='result = validate(value)')
    monkeypatch.setattr(client, 'complete_tool', model)
    finding = dict(path='app.py', head_sha='a' * 40, line=1, evidence='result = unsafe_call(value)',
                   comparison=dict(status='ahead', files=[file_entry(filename='app.py',
                       patch='@@ -1 +1 @@\n-result = unsafe_call(value)\n+result = validate(value)')]))
    assert REAL_RECHECK('owner/repo', finding, 'b' * 40)['verdict'] == 'fixed'


def test_parenthesis_inside_argument_is_not_a_partial_call_move(live, monkeypatch):
    monkeypatch.setattr(replies, '_read_file', lambda *a: 'result = parse(")", safe=True)')
    monkeypatch.setattr(client, 'complete_tool', lambda *a: dict(verdict='fixed', quote='result = parse(")", safe=True)'))
    finding = dict(path='app.py', head_sha='a' * 40, line=1, evidence='',
                   comparison=dict(status='ahead', files=[file_entry(filename='app.py',
                       patch='@@ -1 +1 @@\n-result = parse(")")\n+result = parse(")", safe=True)')]))
    assert REAL_RECHECK('owner/repo', finding, 'b' * 40)['verdict'] == 'fixed'


def test_strict_mapping_explains_judgment_limits():
    from pathlib import Path
    for text in (acceptance.NOTE, Path("docs/publishing.md").read_text()):
        text = " ".join(text.lower().split())
        assert "stricter" in text and "mostly under-counts" in text
        assert "misjudges can still over-count" in text and "lower bound" not in text
        assert "deletions" in text
        assert "fixes outside the flagged lines" in text
        assert "fixes that keep an evidence line" in text
