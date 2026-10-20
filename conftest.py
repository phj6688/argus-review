import pytest


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
