from types import SimpleNamespace

import pytest

from argus import github


def stub_github_client(monkeypatch, *, get=None, post=None):
    def unexpected(*args, **kwargs):
        pytest.fail("Unexpected GitHub HTTP request")

    client = SimpleNamespace(
        get=get if get is not None else unexpected,
        post=post if post is not None else unexpected,
    )
    monkeypatch.setattr(github, "_client", lambda: client)
    return client


@pytest.fixture(autouse=True)
def api_auth_mode(monkeypatch):
    # Existing tests exercise endpoint behavior; auth tests opt into token mode.
    monkeypatch.setenv("ARGUS_AUTH_MODE", "proxy")


@pytest.fixture(autouse=True)
def model_provider(monkeypatch):
    # POST /reviews refuses to start without a provider. Tests that stub the
    # engine still need to pass that check; tests about a missing provider clear
    # these variables themselves.
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://gateway.test")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-token")
