"""build_client resilience config: an explicit timeout and extra retries so a
briefly-overloaded gateway is ridden out, not turned into an errored review.
Env-tunable, and garbage/negative overrides fall back instead of breaking boot.
"""

import pytest

from argus import client


@pytest.fixture
def _creds(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://gateway.test")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "t")


def test_defaults_applied(_creds, monkeypatch):
    monkeypatch.delenv("ARGUS_LLM_TIMEOUT", raising=False)
    monkeypatch.delenv("ARGUS_LLM_MAX_RETRIES", raising=False)
    c = client.build_client()
    assert c.max_retries == client.DEFAULT_MAX_RETRIES
    assert float(c.timeout) == client.DEFAULT_TIMEOUT_SECONDS


def test_env_overrides_honored(_creds, monkeypatch):
    monkeypatch.setenv("ARGUS_LLM_TIMEOUT", "45")
    monkeypatch.setenv("ARGUS_LLM_MAX_RETRIES", "7")
    c = client.build_client()
    assert c.max_retries == 7
    assert float(c.timeout) == 45.0


def test_garbage_and_nonpositive_env_falls_back(_creds, monkeypatch):
    monkeypatch.setenv("ARGUS_LLM_TIMEOUT", "fast")
    monkeypatch.setenv("ARGUS_LLM_MAX_RETRIES", "-3")
    c = client.build_client()
    assert c.max_retries == client.DEFAULT_MAX_RETRIES
    assert float(c.timeout) == client.DEFAULT_TIMEOUT_SECONDS


def test_missing_credentials_raises(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(client.BackendNotConfigured):
        client.build_client()
