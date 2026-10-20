"""E4-c: /health?deep=1 only pings the backend from loopback or with the shared
token; anonymous public deep=1 degrades to the shallow body. Shallow unchanged."""
from types import SimpleNamespace
import hmac

import pytest
from fastapi.testclient import TestClient

from argus.api import db, main


@pytest.fixture
def app_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_DB_PATH", str(tmp_path / "argus.db"))
    db.init_db(str(tmp_path / "argus.db"))
    return monkeypatch


def _record_ping(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "ping", lambda m: calls.append(m) or "pong")
    return calls


def test_public_deep_does_not_ping(app_db):
    calls = _record_ping(app_db)
    app_db.delenv("ARGUS_HEALTH_TOKEN", raising=False)
    body = TestClient(main.app).get("/health?deep=1").json()  # client host = "testclient"
    assert calls == []
    assert body.get("ping") is None
    assert body["status"] == "ok" and "reviews" in body  # shallow intact


def test_shallow_never_pings(app_db):
    calls = _record_ping(app_db)
    body = TestClient(main.app).get("/health").json()
    assert calls == []
    assert body["status"] == "ok"


def test_token_deep_pings(app_db):
    calls = _record_ping(app_db)
    app_db.setenv("ARGUS_HEALTH_TOKEN", "s3cret")
    TestClient(main.app).get("/health?deep=1", headers={"x-argus-health-token": "s3cret"})
    assert calls  # credentialed deep=1 pinged


def test_deep_allowed_from_loopback():
    req = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})
    assert main._deep_allowed(req) is True


def test_health_token_is_compared_in_constant_time(app_db):
    calls = []
    compare = hmac.compare_digest

    def record_compare(left, right):
        calls.append((left, right))
        return compare(left, right)

    app_db.setattr(hmac, "compare_digest", record_compare)
    app_db.setenv("ARGUS_HEALTH_TOKEN", "s3cret")
    pings = _record_ping(app_db)
    client = TestClient(main.app)
    client.get("/health?deep=1", headers={"x-argus-health-token": "wrong"})
    assert pings == []
    client.get("/health?deep=1", headers={"x-argus-health-token": "s3cret"})
    assert len(pings) == 1
    assert len(calls) == 2


def test_deep_health_error_exposes_only_exception_class(app_db):
    app_db.setenv("ARGUS_HEALTH_TOKEN", "s3cret")

    def fail_ping(*_):
        raise RuntimeError("upstream body contains private credentials")

    app_db.setattr(main, "ping", fail_ping)
    response = TestClient(main.app).get(
        "/health?deep=1", headers={"x-argus-health-token": "s3cret"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["ping"] is None
    assert body["error"] == "RuntimeError"
