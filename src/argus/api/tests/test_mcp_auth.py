import httpx
import pytest

pytest.importorskip("mcp")

from argus import mcp_server


@pytest.mark.parametrize("token", [None, "", "test-api-token-with-at-least-32-characters"])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_mcp_sends_api_token_when_set(monkeypatch, token, method):
    if token is None:
        monkeypatch.delenv("ARGUS_API_TOKEN", raising=False)
    else:
        monkeypatch.setenv("ARGUS_API_TOKEN", token)
    monkeypatch.setenv("ARGUS_API", "http://argus.test")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    client_type = httpx.Client
    monkeypatch.setattr(mcp_server.httpx, "Client", lambda **kwargs: client_type(
        transport=httpx.MockTransport(respond), **kwargs))
    assert mcp_server._api("/reviews", method) == {"ok": True}
    assert requests[0].headers.get("Authorization") == (f"Bearer {token}" if token else None)
