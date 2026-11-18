"""Isolate synthetic scenarios from host credentials, clocks, caches, and networks."""

import importlib
import os
import sys
import threading
import time
from collections import OrderedDict
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest

from .harness import NOW, Recorder, RecordingTransport, StubProvider, UnexpectedRequest


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = NOW if tz is None else NOW.astimezone(tz)
        return cls.fromisoformat(value.replace(tzinfo=None).isoformat()) if tz is None else value

    @classmethod
    def utcnow(cls):
        return cls.fromisoformat(NOW.replace(tzinfo=None).isoformat())


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path, api_auth_mode, model_provider):
    update = os.environ.get('ARGUS_GOLDEN_UPDATE')
    prefixes = ('ARGUS_', 'GITHUB_', 'GH_', 'LINEAR_', 'OPENROUTER_', 'ANTHROPIC_', 'OPENAI_',
                'CLAUDE_', 'CODEX_')
    extras = {'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
              'API_BASE_URL', 'DATABASE_URL'}
    for name in list(os.environ):
        if name.startswith(prefixes) or name in extras:
            monkeypatch.delenv(name)
    values = {
        'GITHUB_TOKEN': 'TOKEN-REVIEWER', 'ARGUS_PUBLISH_TOKEN': 'TOKEN-PUBLISH',
        'ARGUS_DENY_OWNERS': 'denied-org', 'ARGUS_DB_PATH': str(tmp_path / 'argus.db'),
        'ARGUS_AUTH_MODE': 'proxy', 'ARGUS_PROVIDER': 'gateway', 'ARGUS_MODEL': 'model-a',
        'ARGUS_REVIEW_PASSES': '1', 'ARGUS_REVIEW_WORKERS': '1',
        'ANTHROPIC_BASE_URL': 'https://model.invalid', 'ANTHROPIC_AUTH_TOKEN': 'TOKEN-MODEL',
        'ARGUS_CR_THREADS_PATH': str(tmp_path / 'threads.json'),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    if update is not None:
        monkeypatch.setenv('ARGUS_GOLDEN_UPDATE', update)

    modules = ('github', 'publish', 'publish_access', 'review', 'client', 'context', 'rules',
               'replies', 'acceptance', 'live', 'automark', 'evalkit.labels',
               'evalkit.monthly', 'api.db', 'api.watch_store', 'api.watch_config', 'api.watcher', 'api.main')
    for name in modules:
        importlib.import_module('argus.' + name)
    from argus import client, github, publish, publish_access, review, rules
    from argus.api import main, watcher
    from argus import live

    frozen_time = SimpleNamespace(**{**vars(time), 'time': lambda: NOW.timestamp(),
                                    'monotonic': lambda: 1000.0, 'sleep': lambda *_: None})
    for name, module in list(sys.modules.items()):
        if name.startswith('argus.') and module:
            if getattr(module, 'datetime', None) is datetime:
                monkeypatch.setattr(module, 'datetime', FrozenDateTime)
            if getattr(module, 'time', None) is time:
                monkeypatch.setattr(module, 'time', frozen_time)
            if hasattr(module, 'DEFAULT_MODEL'):
                monkeypatch.setattr(module, 'DEFAULT_MODEL', 'model-a')
    for name, value in {
        '_quota': {}, '_cache': OrderedDict(), '_pause_until': 0.0, '_graphql_pause_until': 0.0,
        '_refusals': 0, '_not_modified': 0, '_last_quota_log': 0.0, '_was_low': False,
    }.items():
        monkeypatch.setattr(github, name, value)
    monkeypatch.setattr(publish, '_bot_logins', {})
    monkeypatch.setattr(publish_access, '_cache', {})
    monkeypatch.setattr(live, '_KNOWN_FP_KEYS', None)
    monkeypatch.setattr(live, '_LIVE_FP_CACHE', {})
    monkeypatch.setattr(review, 'REVIEW_PASSES', 1)
    monkeypatch.setattr(client, '_llm_active', 0)
    monkeypatch.setattr(client, '_llm_semaphore', threading.BoundedSemaphore(1))
    cache_token = client._prompt_cache.set(None)
    for name in ('_ticks', '_last_tick', '_repo_pushed_at', '_previous_watched_repos', '_outcome_tasks'):
        monkeypatch.setattr(watcher, name, {})
    monkeypatch.setattr(watcher, '_active_heads', set())
    monkeypatch.setattr(watcher, '_pause_logged', 0)
    monkeypatch.setattr(main, 'CR_THREADS_PATH', tmp_path / 'threads.json')
    monkeypatch.setattr(main, 'API_BASE_URL', 'http://127.0.0.1:8350')
    rules.reset_cache()

    network_errors = []

    def no_network(*args, **kwargs):
        message = 'unrecorded network request outside RecordingTransport'
        network_errors.append(message)
        raise UnexpectedRequest(message)

    async def no_async_network(*args, **kwargs):
        no_network()

    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', no_network)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', no_async_network)
    yield network_errors
    client._prompt_cache.reset(cache_token)
    rules.reset_cache()


@pytest.fixture
def frozen_now(isolated_environment):
    return NOW


@pytest.fixture
def db_path(isolated_environment):
    from argus.api import db

    path = db.db_path()
    db.init_db(path)
    return path


@pytest.fixture
def golden(db_path, tmp_path, isolated_environment):
    recorder = Recorder(db_path, tmp_dir=str(tmp_path))
    recorder.errors = isolated_environment
    yield recorder
    assert not recorder.errors, '\n'.join(recorder.errors)


@pytest.fixture(autouse=True)
def transport(monkeypatch, golden):
    from argus import github

    transport = RecordingTransport(golden)
    module_client = httpx.Client(transport=transport, follow_redirects=False)
    bare_client = httpx.Client(transport=transport.labelled('httpx'), follow_redirects=False)
    monkeypatch.setattr(github, '_http', module_client)

    def request(method, url, **kwargs):
        return bare_client.request(method, url, **kwargs)

    monkeypatch.setattr(httpx, 'request', request)
    monkeypatch.setattr(httpx, 'get', lambda url, **kwargs: request('GET', url, **kwargs))
    monkeypatch.setattr(httpx, 'post', lambda url, **kwargs: request('POST', url, **kwargs))
    yield transport
    module_client.close()
    bare_client.close()


@pytest.fixture(autouse=True)
def provider(monkeypatch, golden):
    from argus import client

    stub = StubProvider(golden)
    monkeypatch.setattr(client, 'build_client', lambda provider=None: stub)
    return stub
