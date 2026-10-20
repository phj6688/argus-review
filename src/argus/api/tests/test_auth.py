"""Authentication covers every route, including routes registered later."""

import hmac

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from argus.api import db, main


@pytest.fixture(autouse=True)
def app_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_DB_PATH", str(tmp_path / "argus.db"))
    monkeypatch.setenv("ARGUS_WATCH_ENABLED", "0")
    db.init_db()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ARGUS_AUTH_MODE", "token")
    monkeypatch.setenv("ARGUS_API_TOKEN", "test-api-token-with-at-least-32-characters")
    with TestClient(main.app) as test_client:
        yield test_client


@pytest.mark.parametrize("authorization", [
    None, "Bearer wrong", "Basic test-api-token-with-at-least-32-characters", "Bearer", "Bearer ",
    "Bearer test-api-token-with-at-least-32-characters extra",
])
def test_token_mode_rejects_missing_or_invalid_credentials(client, authorization):
    headers = {"Authorization": authorization} if authorization is not None else {}
    response = client.get("/reviews", headers=headers)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize("scheme", ["Bearer", "bearer"])
def test_token_mode_accepts_correct_token(client, scheme):
    response = client.get("/reviews", headers={"Authorization": f"{scheme} test-api-token-with-at-least-32-characters"})
    assert response.status_code == 200
    assert response.json() == {"live": None, "reviews": []}


def test_token_is_compared_in_constant_time(client, monkeypatch):
    calls = []
    compare = hmac.compare_digest

    def record_compare(left, right):
        calls.append((left, right))
        return compare(left, right)

    monkeypatch.setattr(hmac, "compare_digest", record_compare)
    assert client.get("/reviews", headers={"Authorization": "Bearer test-api-token-with-at-least-32-characters"}).status_code == 200
    assert calls


def test_default_mode_requires_token(monkeypatch):
    monkeypatch.delenv("ARGUS_AUTH_MODE")
    monkeypatch.setenv("ARGUS_API_TOKEN", "test-api-token-with-at-least-32-characters")
    with TestClient(main.app) as client:
        assert client.get("/reviews").status_code == 401


@pytest.mark.parametrize("token", [None, ""])
@pytest.mark.parametrize("mode", [None, "token"])
def test_startup_refuses_missing_token(monkeypatch, token, mode):
    for name, value in (("ARGUS_AUTH_MODE", mode), ("ARGUS_API_TOKEN", token)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError) as error:
        with TestClient(main.app):
            pass
    assert "ARGUS_AUTH_MODE" in str(error.value)
    assert "ARGUS_API_TOKEN" in str(error.value)


@pytest.mark.parametrize("mode", [None, "token"])
def test_startup_refuses_short_token(monkeypatch, mode):
    if mode is None:
        monkeypatch.delenv("ARGUS_AUTH_MODE", raising=False)
    else:
        monkeypatch.setenv("ARGUS_AUTH_MODE", mode)
    monkeypatch.setenv("ARGUS_API_TOKEN", "x" * 31)
    with pytest.raises(RuntimeError, match="ARGUS_API_TOKEN.*32.*openssl rand -hex 32"):
        with TestClient(main.app):
            pass


def test_startup_accepts_32_character_token(monkeypatch):
    monkeypatch.setenv("ARGUS_AUTH_MODE", "token")
    monkeypatch.setenv("ARGUS_API_TOKEN", "x" * 32)
    with TestClient(main.app) as client:
        assert client.get("/reviews", headers={"Authorization": f"Bearer {'x' * 32}"}).status_code == 200


@pytest.mark.parametrize("mode", ["", "typo"])
def test_startup_refuses_invalid_auth_mode(monkeypatch, mode):
    monkeypatch.setenv("ARGUS_AUTH_MODE", mode)
    monkeypatch.setenv("ARGUS_API_TOKEN", "test-api-token-with-at-least-32-characters")
    with pytest.raises(RuntimeError, match="ARGUS_AUTH_MODE"):
        with TestClient(main.app):
            pass


def test_proxy_mode_warns_and_does_not_check_tokens(monkeypatch, caplog):
    monkeypatch.setenv("ARGUS_AUTH_MODE", "proxy")
    monkeypatch.delenv("ARGUS_API_TOKEN", raising=False)
    main.log.addHandler(caplog.handler)
    try:
        with TestClient(main.app) as client:
            assert client.get("/reviews").status_code == 200
            monkeypatch.setenv("ARGUS_API_TOKEN", "test-api-token-with-at-least-32-characters")
            assert client.get("/reviews", headers={"Authorization": "Bearer wrong"}).status_code == 200
    finally:
        main.log.removeHandler(caplog.handler)
    assert any(record.levelname == "WARNING" and "proxy" in record.message
               for record in caplog.records)


@pytest.mark.parametrize("query", ["", "?deep=0"])
def test_shallow_health_is_public(client, query, monkeypatch):
    monkeypatch.setattr(main, "ping", lambda *_: pytest.fail("shallow health must not ping"))
    response = client.get(f"/health{query}")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.parametrize("query", ["?deep=1", "?deep=-1", "?deep=0&deep=1"])
def test_deep_health_requires_api_token_even_with_health_token(client, monkeypatch, query):
    monkeypatch.setenv("ARGUS_HEALTH_TOKEN", "health-secret")
    monkeypatch.setattr(main, "ping", lambda *_: pytest.fail("unauthenticated health must not ping"))
    response = client.get(f"/health{query}", headers={"x-argus-health-token": "health-secret"})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_authenticated_deep_health_preserves_health_token_gate(client, monkeypatch):
    monkeypatch.setenv("ARGUS_HEALTH_TOKEN", "health-secret")
    monkeypatch.setattr(main, "ping", lambda *_: "pong")
    headers = {"Authorization": "Bearer test-api-token-with-at-least-32-characters"}
    response = client.get("/health?deep=1", headers=headers)
    assert response.status_code == 200
    assert "ping" not in response.json()
    headers["x-argus-health-token"] = "health-secret"
    assert client.get("/health?deep=1", headers=headers).json()["ping"] == "pong"


def test_loopback_deep_health_still_requires_api_token(client, monkeypatch):
    monkeypatch.delenv("ARGUS_HEALTH_TOKEN", raising=False)
    monkeypatch.setattr(main, "ping", lambda *_: "pong")
    with TestClient(main.app, client=("127.0.0.1", 50000)) as loopback:
        assert loopback.get("/health?deep=1").status_code == 401
        response = loopback.get("/health?deep=1", headers={"Authorization": "Bearer test-api-token-with-at-least-32-characters"})
        assert response.status_code == 200
        assert response.json()["ping"] == "pong"


def test_every_route_requires_authentication(client):
    for route in main.app.routes:
        path = route.path_format.format(**{name: "1" for name in route.param_convertors})
        for method in route.methods:
            if path == "/health" and method == "GET":
                continue
            response = client.request(method, path)
            assert response.status_code == 401, (method, path, response.text)
            assert response.headers["WWW-Authenticate"] == "Bearer", (method, path)


def test_new_routes_and_routers_inherit_authentication(client, monkeypatch):
    monkeypatch.setattr(main.app.router, "routes", list(main.app.routes))
    main.app.get("/new-route")(lambda: {"ok": True})
    router = APIRouter()
    router.get("/new-router-route")(lambda: {"ok": True})
    main.app.include_router(router)
    for path in ("/new-route", "/new-router-route"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer test-api-token-with-at-least-32-characters"}).status_code == 200


@pytest.mark.parametrize("path", ["/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"])
def test_documentation_remains_available_with_token(client, path):
    assert client.get(path, headers={"Authorization": "Bearer test-api-token-with-at-least-32-characters"}).status_code == 200


def test_cors_preflight_does_not_require_token(client):
    response = client.options("/reviews", headers={
        "Origin": "http://localhost:3000",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type",
    })
    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == "http://localhost:3000"
    assert "authorization" in response.headers["Access-Control-Allow-Headers"]
