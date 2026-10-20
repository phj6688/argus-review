import pytest


@pytest.fixture(autouse=True)
def api_auth_mode(monkeypatch):
    # Existing tests exercise endpoint behavior; auth tests opt into token mode.
    monkeypatch.setenv("ARGUS_AUTH_MODE", "proxy")
