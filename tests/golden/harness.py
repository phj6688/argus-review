"""Strict recording boundaries and text snapshots for synthetic scenarios."""

import base64
import copy
import dataclasses
import difflib
import hashlib
import json
import os
import re
import sqlite3
from collections import deque
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from types import SimpleNamespace

import httpx

from argus.publish import PublishOutcome

SNAPSHOT_ROOT = Path(__file__).parent / 'github'
API = 'https://api.github.com'
HEAD = 'a' * 40
BASE = 'b' * 40
NOW = datetime.fromisoformat('2026-01-15T00:00:00+00:00')
_TIMESTAMP = re.compile(r'^\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)?$')
_UUID = re.compile(r'^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$', re.I)
_HEX_RUN = re.compile(r'^[0-9a-f]{16,64}$', re.I)


def to_json(value):
    if isinstance(value, Exception):
        return {'raised': type(value).__name__, 'message': str(value)}
    if hasattr(value, 'model_dump'):
        return value.model_dump(mode='json')
    if dataclasses.is_dataclass(value):
        fields = dataclasses.asdict(value)
        # The nullable GitLab identity does not change existing GitHub snapshots.
        if isinstance(value, PublishOutcome) and value.bot_user_id is None:
            fields.pop('bot_user_id')
        # The repeat count stays out of snapshots until a repeat occurs.
        if isinstance(value, PublishOutcome) and value.location_repeats == 0:
            fields.pop('location_repeats')
        return to_json(fields)
    if isinstance(value, dict):
        return {str(key): to_json(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((to_json(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
    if isinstance(value, (list, tuple)):
        return [to_json(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return to_json(value.value)
    return value


class Normalizer:
    def __init__(self, *, tmp_dir=None):
        self.tmp_dir = str(tmp_dir) if tmp_dir else None
        self.run_ids = {}

    def normalize(self, value, column=''):
        value = to_json(value)
        if isinstance(value, dict):
            return {key: self.normalize(item, key) for key, item in value.items()}
        if isinstance(value, list):
            return [self.normalize(item, column) for item in value]
        if isinstance(value, str):
            if _TIMESTAMP.fullmatch(value):
                return '<ts>'
            if _UUID.fullmatch(value) or ('run' in column.lower() and _HEX_RUN.fullmatch(value)):
                return self.run_ids.setdefault(value, f'<run-{len(self.run_ids) + 1}>')
            if self.tmp_dir:
                value = value.replace(self.tmp_dir, '<tmp>')
            return value
        if isinstance(value, (float, int)) and not isinstance(value, bool):
            epoch_column = (column.endswith(('_at', '_until', '_since', '_epoch', '_timestamp'))
                            or column in {'timestamp', 'epoch', 'reset', 'last_seen'})
            if epoch_column and 946684800 <= value <= 4102444800:
                return '<ts>'
        return value


def _db_value(value, column):
    json_column = column.endswith(('_json', '_payload')) or column in {
        'repo_allowlist', 'deny', 'owned_repos', 'publish_repos',
    }
    if json_column and isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def dump_db(path, *, normalizer=None):
    normalizer = normalizer or Normalizer()
    if not path or not Path(path).exists():
        return {}
    result = {}
    with sqlite3.connect(path) as conn:
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                              "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
        for (table,) in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            info = conn.execute(f'PRAGMA table_info({quoted})').fetchall()
            columns = [row[1] for row in info]
            keys = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
            ordering = ', '.join('"' + key.replace('"', '""') + '"' for key in keys) or 'rowid'
            rows = conn.execute(f'SELECT * FROM {quoted} ORDER BY {ordering}').fetchall()
            if rows:
                result[table] = {
                    'columns': columns,
                    'rows': [[normalizer.normalize(_db_value(item, column), column)
                              for column, item in zip(columns, row)]
                             for row in rows],
                }
    return result


def assert_golden(scenario, case, data, *, root=None):
    path = (Path(root) if root is not None else SNAPSHOT_ROOT) / scenario / f'{case}.json'
    actual = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + '\n'
    if os.environ.get('ARGUS_GOLDEN_UPDATE') == '1':
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding='utf-8')
        return
    if not path.exists():
        raise AssertionError(f'{path} is missing; regenerate with ARGUS_GOLDEN_UPDATE=1')
    expected = path.read_text(encoding='utf-8')
    if expected != actual:
        lines = list(difflib.unified_diff(expected.splitlines(True), actual.splitlines(True),
                                          fromfile=str(path), tofile='actual'))
        if len(lines) > 200:
            lines = lines[:160] + ['\n... diff truncated ...\n'] + lines[-35:]
        raise AssertionError(''.join(lines))


class Recorder:
    def __init__(self, db_path=None, *, tmp_dir=None):
        self.requests = []
        self.llm_calls = []
        self.db_path = db_path
        self.tmp_dir = tmp_dir
        self.errors = []

    def check(self, scenario, case, result, *, db=True):
        assert not self.errors, '\n'.join(self.errors)
        normalizer = Normalizer(tmp_dir=self.tmp_dir)
        rows = dump_db(self.db_path, normalizer=normalizer) if db else {}
        assert_golden(scenario, case, {
            'scenario': scenario, 'case': case, 'requests': self.requests,
            'llm_calls': self.llm_calls, 'result': normalizer.normalize(result), 'db_rows': rows,
        })


class UnexpectedRequest(AssertionError):
    pass


class UnexpectedLlmCall(AssertionError):
    pass


def _query(params):
    result = {}
    for key in sorted(set(params.keys())):
        values = params.get_list(key)
        result[key] = values[0] if len(values) == 1 else values
    return result


class RecordingTransport(httpx.MockTransport):
    def __init__(self, recorder, *, label='github.get', routes=None):
        self.recorder = recorder
        self.label = label
        self.routes = routes if routes is not None else []
        super().__init__(self._respond)

    def labelled(self, label):
        return RecordingTransport(self.recorder, label=label, routes=self.routes)

    def add(self, method, url, response, *, query=None):
        url = API + url if url.startswith('/') else url
        parsed = httpx.URL(url)
        assert parsed.host == 'api.github.com' and parsed.scheme == 'https', url
        if query is None and parsed.query:
            query = _query(parsed.params)
        if query is not None:
            query = {key: [str(v) for v in value] if isinstance(value, list) else str(value)
                     for key, value in sorted(query.items())}
        key = (method.upper(), str(parsed.copy_with(query=None)), query)
        specs = response if isinstance(response, list) else [response]
        for route in self.routes:
            if route['key'] == key:
                route['queue'].extend(copy.deepcopy(specs))
                return
        self.routes.append({'key': key, 'queue': deque(copy.deepcopy(specs))})

    def _respond(self, request):
        url = str(request.url.copy_with(query=None))
        if request.url.host != 'api.github.com' or request.url.scheme != 'https':
            return self._unexpected(request)
        query = _query(request.url.params)
        body_bytes = request.read()
        body = body_bytes.decode('utf-8') if body_bytes else None
        if body is not None:
            try:
                body = json.loads(body)
            except ValueError:
                pass
        headers = {}
        allow = {'accept', 'x-github-api-version', 'if-none-match', 'content-type'}
        for name, value in sorted(request.headers.items()):
            if name in allow:
                headers[name] = value
            elif name == 'authorization':
                scheme, _, token = value.partition(' ')
                label = {'TOKEN-REVIEWER': '<reviewer-token>', 'TOKEN-PUBLISH': '<publish-token>'}.get(
                    token, '<unknown-token>')
                headers[name] = f'{scheme} {label}'
        self.recorder.requests.append({
            'method': request.method, 'url': url, 'query': query, 'body': body,
            'body_sha256': hashlib.sha256(body_bytes).hexdigest() if body_bytes else None,
            'headers': headers, 'transport': self.label,
        })
        matching = [route for route in self.routes
                    if route['key'][:2] == (request.method, url)
                    and (route['key'][2] is None or route['key'][2] == query)]
        if not matching:
            return self._unexpected(request)
        route = next((item for item in matching if item['key'][2] is not None), matching[0])
        if not route['queue']:
            return self._unexpected(request, 'response queue exhausted')
        status, payload, response_headers = route['queue'].popleft()
        kwargs = {'headers': response_headers, 'request': request}
        if payload is None:
            return httpx.Response(status, **kwargs)
        if isinstance(payload, (str, bytes)):
            return httpx.Response(status, content=payload, **kwargs)
        return httpx.Response(status, json=payload, **kwargs)

    def _unexpected(self, request, reason='unregistered route'):
        message = f'{request.method} {request.url}: {reason}'
        self.recorder.errors.append(message)
        raise UnexpectedRequest(message)

    def assert_all_used(self):
        unused = [f"{r['key'][0]} {r['key'][1]} {r['key'][2]} ({len(r['queue'])} remaining)"
                  for r in self.routes if r['queue']]
        assert not unused, 'Unused routes:\n' + '\n'.join(unused)


class StubProvider:
    def __init__(self, recorder):
        self.recorder = recorder
        self.outputs = {}
        self.messages = SimpleNamespace(create=self.create)

    def register(self, lane, output):
        self.outputs[lane] = deque(output) if isinstance(output, list) else output

    def create(self, **kwargs):
        lane = kwargs['tools'][0]['name']
        call = {'lane': lane, **{key: copy.deepcopy(kwargs.get(key)) for key in (
            'model', 'system', 'messages', 'tools', 'tool_choice', 'max_tokens')},
            'extra': {key: copy.deepcopy(value) for key, value in kwargs.items()
                      if key not in {'model', 'system', 'messages', 'tools', 'tool_choice', 'max_tokens'}}}
        # The derived key is a uuid, which secret scanners read as a credential. The prefix
        # keeps the full value in the snapshot while breaking that pattern.
        body = call['extra'].get('extra_body')
        if isinstance(body, dict) and 'prompt_cache_key' in body:
            body['prompt_cache_key'] = f"uuid5 {body['prompt_cache_key']}"
        self.recorder.llm_calls.append(call)
        output = self.outputs.get(lane)
        if output is None or (isinstance(output, deque) and not output):
            message = f'No registered output for {lane}'
            self.recorder.errors.append(message)
            raise UnexpectedLlmCall(message)
        if isinstance(output, deque):
            output = output.popleft()
        if callable(output):
            output = output(call)
        return SimpleNamespace(
            content=[SimpleNamespace(type='tool_use', name=lane, input=copy.deepcopy(output))],
            stop_reason='tool_use', usage=SimpleNamespace(input_tokens=10, output_tokens=5,
                                                        cache_read_input_tokens=0, cache_creation_input_tokens=0),
        )


DEFAULT_CONTENTS = {
    'app.py': 'def divide(value):\n    return 10 / value\n\ndef display(value):\n    return str(value)\n',
    'compose.yaml': 'services:\n  app:\n    image: example/app:1.0\n    ports:\n      - "127.0.0.1:8080:8080"\n',
}
DEFAULT_FILES = [
    {'filename': 'app.py', 'status': 'modified', 'additions': 2, 'deletions': 2,
     'patch': '@@ -1,5 +1,5 @@\n def divide(value):\n-    return value\n+    return 10 / value\n \n def display(value):\n-    return value\n+    return str(value)\n'},
    {'filename': 'compose.yaml', 'status': 'modified', 'additions': 1, 'deletions': 1,
     'patch': '@@ -1,5 +1,5 @@\n services:\n   app:\n-    image: example/app:0.9\n+    image: example/app:1.0\n     ports:\n       - "127.0.0.1:8080:8080"\n'},
]


def register_review(transport, provider, *, repo='example/app', pr=7, files=None, contents=None,
                    findings=None, metadata_calls=1):
    files = copy.deepcopy(DEFAULT_FILES if files is None else files)
    contents = DEFAULT_CONTENTS if contents is None else contents
    meta = {'number': pr, 'title': 'Guard numeric input', 'body': 'Handle empty and zero input.',
            'head': {'sha': HEAD, 'ref': 'fix/input', 'repo': {'full_name': repo}},
            'base': {'sha': BASE, 'repo': {'full_name': repo}},
            'user': {'login': 'example-dev'}, 'author_association': 'MEMBER', 'draft': False,
            'state': 'open'}
    transport.add('GET', f'/repos/{repo}/pulls/{pr}/files', (200, files, {}))
    transport.add('GET', f'/repos/{repo}/pulls/{pr}', [(200, meta, {})] * metadata_calls)
    transport.add('GET', f'/repos/{repo}/pulls/{pr}/commits', (200, [{
        'sha': HEAD, 'commit': {'message': 'Guard numeric input',
                               'committer': {'date': NOW.isoformat()}}}], {}))
    if any(file.get('patch') for file in files):
        transport.add('GET', f'/repos/{repo}/contents/CLAUDE.md', (404, {'message': 'Not Found'}, {}))
    for path, content in contents.items():
        if any(file['filename'] == path for file in files):
            transport.add('GET', f'/repos/{repo}/contents/{path}', (200, {
                'encoding': 'base64', 'content': base64.b64encode(content.encode()).decode()}, {}),
                query={'ref': HEAD})
    provider.register('report_findings', {'findings': findings or []})
    provider.register('report_verdict', {'is_real': True, 'confidence': 0.95,
                                         'reasoning': 'The changed path reaches the unsafe operation.'})
    return meta
