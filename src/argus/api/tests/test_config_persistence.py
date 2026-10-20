"""Tests for the writable config: PUT /config persistence and the storage layer.

Same harness as test_console_api.py: a temp SQLite db via ARGUS_DB_PATH so
nothing touches the real data dir. These prove config is PERSISTED (survives a
fresh client against the same db file) and that GET reflects stored values.
"""

import pytest
from fastapi.testclient import TestClient

from argus.api import db, main
from argus.client import DEFAULT_MODEL
from argus.review import DEFAULT_BUG_CAP


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "argus.db"
    monkeypatch.setenv("ARGUS_DB_PATH", str(path))
    db.init_db(str(path))
    return str(path)


# ---------------------------------------------------------------- storage layer

def test_save_config_then_get_config_roundtrips(db_path):
    repos = [{"name": "example/argus", "watched": True},
             {"name": "example/app", "watched": False}]
    db.save_config(db_path, repos=repos, model="claude-sonnet-5-5",
                   bug_cap=3, min_confidence=0.7)
    stored = db.get_config(db_path)
    assert stored is not None
    assert stored["model"] == "claude-sonnet-5-5"
    assert stored["bug_cap"] == 3
    assert stored["min_confidence"] == 0.7
    assert stored["repos"] == repos


def test_get_config_is_none_when_unset(db_path):
    assert db.get_config(db_path) is None


def test_save_config_is_singleton(db_path):
    db.save_config(db_path, repos=[], model="m1", bug_cap=5, min_confidence=0.5)
    db.save_config(db_path, repos=[], model="m2", bug_cap=8, min_confidence=0.6)
    conn = db.connect(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM config").fetchone()[0]
    finally:
        conn.close()
    assert count == 1
    assert db.get_config(db_path)["model"] == "m2"


# ---------------------------------------------------------------- GET defaults

def test_get_config_returns_defaults_when_unset(db_path):
    with TestClient(main.app) as client:
        body = client.get("/config").json()
    assert body["bugCap"] == DEFAULT_BUG_CAP
    assert body["minConfidence"] == main.MIN_CONFIDENCE
    assert body["repos"] == []
    assert all(m["model"] == DEFAULT_MODEL for m in body["models"])


# ---------------------------------------------------------------- PUT persists

def test_put_config_persists_model_and_bugcap(db_path, monkeypatch):
    monkeypatch.setenv(main.ENV_MODEL_OPTIONS, "claude-sonnet-5-5")
    payload = {
        "repos": [{"name": "example/argus", "watched": True},
                  {"name": "example/service", "watched": False}],
        "model": "claude-sonnet-5-5",
        "bugCap": 3,
        "minConfidence": 0.8,
    }
    with TestClient(main.app) as client:
        put = client.put("/config", json=payload)
        assert put.status_code == 200
        echoed = put.json()
        # response is the same shape GET returns
        assert set(echoed) == {
            "repos", "hiddenCount", "totalRepos", "models", "modelOptions",
            "bugCap", "minConfidence", "deny", "hard_denied_owners", "watch_mode",
        }
        assert echoed["bugCap"] == 3
        assert all(m["model"] == "claude-sonnet-5-5" for m in echoed["models"])

    # A FRESH client against the SAME db file proves persistence across "restart".
    with TestClient(main.app) as client2:
        body = client2.get("/config").json()
    assert body["bugCap"] == 3
    assert body["minConfidence"] == 0.8
    assert all(m["model"] == "claude-sonnet-5-5" for m in body["models"])
    repos = {r["name"]: r["watched"] for r in body["repos"]}
    assert repos == {"example/argus": True, "example/service": False}
    # totalRepos reflects the stored allowlist
    assert body["totalRepos"] == 2


def test_put_config_rejects_out_of_range(db_path):
    with TestClient(main.app) as client:
        bad_cap = client.put("/config", json={
            "repos": [], "model": "m", "bugCap": 0, "minConfidence": 0.5})
        assert bad_cap.status_code == 422
        bad_conf = client.put("/config", json={
            "repos": [], "model": "m", "bugCap": 5, "minConfidence": 1.5})
        assert bad_conf.status_code == 422


def test_put_config_preserves_get_shape(db_path):
    """The persisted GET keeps the exact contract the console consumes."""
    with TestClient(main.app) as client:
        client.put("/config", json={
            "repos": [{"name": "a/b", "watched": True}],
            "model": "claude-opus-4-8", "bugCap": 6, "minConfidence": 0.4})
        body = client.get("/config").json()
    assert len(body["models"]) == 2
    for m in body["models"]:
        assert set(m) == {"name", "caption", "model", "tier"}
        assert m["tier"] in ("deep", "triage")
    for r in body["repos"]:
        assert set(r) == {"name", "watched", "stored_watched", "effective_watched", "watched_source"}


# ---------------------------------------------------------------- CORS allows PUT

def test_cors_allows_put_preflight(db_path):
    with TestClient(main.app) as client:
        res = client.options("/config", headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "PUT",
        })
    assert res.status_code in (200, 204)
    assert res.headers.get("access-control-allow-origin") == "http://localhost:3000"
    allow_methods = res.headers.get("access-control-allow-methods", "")
    assert "PUT" in allow_methods
