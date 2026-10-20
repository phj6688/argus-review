"""Model picker contract: env-owned options list, PUT validation, honest health."""

import pytest
from fastapi.testclient import TestClient

from argus.api import db, main


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_DB_PATH", str(tmp_path / "argus.db"))
    monkeypatch.setenv(main.ENV_MODEL_OPTIONS, "claude-opus-4-8, ~x-ai/grok-latest")
    db.init_db()
    with TestClient(main.app) as test_client:
        yield test_client


def _put(client, model):
    return client.put("/config", json={
        "repos": [], "model": model, "bugCap": 10, "minConfidence": 0.55})


def test_options_come_from_env_with_default_and_current_injected(client):
    options = client.get("/config").json()["modelOptions"]
    assert options[0] == "claude-opus-4-8"
    assert "~x-ai/grok-latest" in options
    assert main.DEFAULT_MODEL in options
    assert len(options) == len(set(options))


def test_put_accepts_listed_model_and_health_reports_it(client):
    response = _put(client, "~x-ai/grok-latest")
    assert response.status_code == 200
    assert response.json()["models"][0]["model"] == "~x-ai/grok-latest"
    assert client.get("/config").json()["models"][0]["model"] == "~x-ai/grok-latest"
    assert client.get("/health").json()["model"] == "~x-ai/grok-latest"


def test_put_rejects_unlisted_model(client):
    response = _put(client, "x-ai/grok-typo")
    assert response.status_code == 422
    assert "ARGUS_MODEL_OPTIONS" in response.json()["detail"]


def test_stored_model_stays_selectable_after_env_shrinks(client, monkeypatch):
    assert _put(client, "~x-ai/grok-latest").status_code == 200
    monkeypatch.setenv(main.ENV_MODEL_OPTIONS, "")
    options = client.get("/config").json()["modelOptions"]
    assert "~x-ai/grok-latest" in options  # current never orphaned
    assert _put(client, "~x-ai/grok-latest").status_code == 200


def test_empty_env_yields_default_only_plus_current(client, monkeypatch):
    monkeypatch.setenv(main.ENV_MODEL_OPTIONS, "")
    options = client.get("/config").json()["modelOptions"]
    assert options == [main.DEFAULT_MODEL]
