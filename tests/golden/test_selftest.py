import hashlib
import json
import sqlite3

import httpx
import pytest

from argus.publish import PublishOutcome

from .harness import (
    Normalizer,
    Recorder,
    RecordingTransport,
    StubProvider,
    UnexpectedLlmCall,
    UnexpectedRequest,
    assert_golden,
    dump_db,
    to_json,
)


def test_transport_rejects_unknown_and_exhausted_routes():
    recorder = Recorder()
    transport = RecordingTransport(recorder)
    with httpx.Client(transport=transport) as client:
        with pytest.raises(UnexpectedRequest, match=r'GET https://api.github.com/missing'):
            client.get('https://api.github.com/missing')
        transport.add('GET', '/registered', (200, {'ok': True}, {}))
        assert client.get('https://api.github.com/registered').json() == {'ok': True}
        with pytest.raises(UnexpectedRequest, match=r'GET https://api.github.com/registered'):
            client.get('https://api.github.com/registered')


def test_publish_outcome_preserves_github_shape_and_records_numeric_identity():
    assert to_json(PublishOutcome("posted")) == {
        "status": "posted", "comments_posted": 0, "already_posted": 0, "reason": None,
        "payload": None, "items": [], "gh_review_id": None, "bot_login": None, "posted_at": None,
    }
    assert to_json(PublishOutcome("posted", bot_user_id=7))["bot_user_id"] == 7


def test_transport_records_raw_body_query_headers_and_transport():
    recorder = Recorder()
    transport = RecordingTransport(recorder)
    transport.add('POST', '/graphql', (200, {'data': {}}, {}), query={'a': ['1', '2'], 'z': '3'})
    body = b'{"variables": {"repo": "example/app"}}'
    with httpx.Client(transport=transport) as client:
        client.post('https://api.github.com/graphql?z=3&a=1&a=2', content=body, headers={
            'Authorization': 'Bearer TOKEN-REVIEWER', 'Content-Type': 'application/json',
            'Accept': 'application/vnd.github+json', 'X-Secret': 'MUST-NOT-RECORD',
        })
    assert recorder.requests == [{
        'method': 'POST', 'url': 'https://api.github.com/graphql',
        'query': {'a': ['1', '2'], 'z': '3'},
        'body': {'variables': {'repo': 'example/app'}},
        'body_sha256': hashlib.sha256(body).hexdigest(),
        'headers': {'accept': 'application/vnd.github+json',
                    'authorization': 'Bearer <reviewer-token>', 'content-type': 'application/json'},
        'transport': 'github.get',
    }]
    transport.assert_all_used()
    transport.add('GET', '/unused', (200, [], {}))
    with pytest.raises(AssertionError, match='unused'):
        transport.assert_all_used()


def test_stub_provider_rejects_unregistered_lane():
    provider = StubProvider(Recorder())
    with pytest.raises(UnexpectedLlmCall, match='unregistered'):
        provider.create(model='model-a', tools=[{'name': 'unregistered'}])


def test_stub_provider_records_full_call_and_consumes_outputs():
    recorder = Recorder()
    provider = StubProvider(recorder)
    provider.register('report_findings', [{'findings': []}, lambda call: {'model': call['model']}])
    kwargs = {'model': 'model-a', 'system': 'system', 'messages': [{'role': 'user', 'content': 'body'}],
              'tools': [{'name': 'report_findings'}], 'tool_choice': {'type': 'auto'},
              'max_tokens': 32, 'extra_body': {'prompt_cache_key': 'fixed'}}
    assert provider.create(**kwargs).content[0].input == {'findings': []}
    assert provider.create(**kwargs).content[0].input == {'model': 'model-a'}
    assert recorder.llm_calls[0] == {
        'lane': 'report_findings', **{k: v for k, v in kwargs.items() if k != 'extra_body'},
        'extra': {'extra_body': {'prompt_cache_key': 'uuid5 fixed'}},
    }
    with pytest.raises(UnexpectedLlmCall, match='report_findings'):
        provider.create(**kwargs)


def test_normalizer_parses_json_and_numbers_run_ids_by_first_appearance(tmp_path):
    path = tmp_path / 'normalize.db'
    first = '12345678-1234-5678-9012-123456789012'
    second = 'b' * 32
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE sample (id INTEGER PRIMARY KEY, created_at TEXT, reset_at REAL, '
                     'run_id TEXT, payload_json TEXT, head_sha TEXT)')
        conn.execute('INSERT INTO sample VALUES (?, ?, ?, ?, ?, ?)', (
            1, '2026-01-15T00:00:00+00:00', 1768435200.0, first,
            json.dumps({'run_id': second, 'nested': {'run_id': first, 'at': '2026-01-15T00:00:01Z'}}),
            'a' * 40,
        ))
        conn.execute('INSERT INTO sample VALUES (?, ?, ?, ?, ?, ?)', (
            2, None, None, second, json.dumps([{'run_id': first}]), 'b' * 40,
        ))
    assert dump_db(path) == {'sample': {
        'columns': ['id', 'created_at', 'reset_at', 'run_id', 'payload_json', 'head_sha'],
        'rows': [[1, '<ts>', '<ts>', '<run-1>',
                  {'run_id': '<run-2>', 'nested': {'run_id': '<run-1>', 'at': '<ts>'}}, 'a' * 40],
                 [2, None, None, '<run-2>', [{'run_id': '<run-1>'}], 'b' * 40]],
    }}
    normalizer = Normalizer(tmp_dir=str(tmp_path))
    assert normalizer.normalize({'path': str(tmp_path / 'queue.json'), 'id': 12}) == {
        'path': '<tmp>/queue.json', 'id': 12,
    }


def test_result_json_text_preserves_its_type_and_bytes():
    normalizer = Normalizer()
    text = '{ "z": 1, "a": [2, 3] }'
    assert normalizer.normalize({'config': text, 'rows': ['[1,2]']}) == {
        'config': text, 'rows': ['[1,2]'],
    }



def test_snapshot_missing_diff_and_update(tmp_path, monkeypatch):
    monkeypatch.delenv('ARGUS_GOLDEN_UPDATE', raising=False)
    data = {'scenario': 'self', 'case': 'basic', 'requests': [], 'llm_calls': [], 'result': 1, 'db_rows': {}}
    with pytest.raises(AssertionError, match=r'basic.json.*ARGUS_GOLDEN_UPDATE=1'):
        assert_golden('self', 'basic', data, root=tmp_path)
    monkeypatch.setenv('ARGUS_GOLDEN_UPDATE', '1')
    assert_golden('self', 'basic', data, root=tmp_path)
    path = tmp_path / 'self' / 'basic.json'
    assert path.read_text() == json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + '\n'
    monkeypatch.delenv('ARGUS_GOLDEN_UPDATE')
    changed = {**data, 'result': 2}
    with pytest.raises(AssertionError, match=r'(?s)--- .*basic.json.*\+\+\+ actual.*-  "result": 1.*\+  "result": 2'):
        assert_golden('self', 'basic', changed, root=tmp_path)
    monkeypatch.setenv('ARGUS_GOLDEN_UPDATE', '1')
    assert_golden('self', 'basic', changed, root=tmp_path)
    assert json.loads(path.read_text())['result'] == 2


def test_fixture_routes_all_http_entry_points(transport, golden):
    from argus import github

    transport.add('GET', '/repos/example/app', [(200, {}, {}), (200, {}, {})])
    transport.add('POST', '/repos/example/app/pulls/7/reviews', (200, {}, {}))
    transport.add('PATCH', '/user/repository_invitations/4', (204, None, {}))
    github.get('/repos/example/app')
    httpx.get('https://api.github.com/repos/example/app')
    httpx.post('https://api.github.com/repos/example/app/pulls/7/reviews', json={})
    httpx.request('PATCH', 'https://api.github.com/user/repository_invitations/4')
    assert [record['transport'] for record in golden.requests] == ['github.get', 'httpx', 'httpx', 'httpx']
    transport.assert_all_used()


def test_raw_network_is_blocked_even_when_the_error_is_caught(golden):
    try:
        with httpx.Client() as client:
            with pytest.raises(UnexpectedRequest, match='unrecorded network'):
                client.get('https://api.github.com/unregistered')
        assert golden.errors == ['unrecorded network request outside RecordingTransport']
    finally:
        golden.errors.clear()


def test_result_conversion():
    from dataclasses import dataclass
    from pydantic import BaseModel

    @dataclass
    class Row:
        id: int

    class Document(BaseModel):
        ok: bool

    from .harness import to_json
    assert to_json([Row(1), Document(ok=True), {3, 1}]) == [{'id': 1}, {'ok': True}, [1, 3]]
    assert to_json(ValueError('bad input')) == {'raised': 'ValueError', 'message': 'bad input'}
