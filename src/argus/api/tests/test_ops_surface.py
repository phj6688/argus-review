"""E1-g: /health gains a `reviews` ok|degraded enum and /stats gains an additive
`ops` block, both computed from the review DB. Nothing pre-existing is renamed."""
import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from argus.api import db, main


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    path = tmp_path / "argus.db"
    monkeypatch.setenv("ARGUS_DB_PATH", str(path))
    db.init_db(str(path))
    return str(path)


def _insert(path, status, error_class=None):
    con = sqlite3.connect(path)
    con.execute(
        "INSERT INTO reviews (repo, pr, model, status, created_at, error_class) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("example/x", 1, "claude-opus-4-8", status,
         datetime.now(timezone.utc).isoformat(), error_class),
    )
    con.commit()
    con.close()


def test_health_ok_when_clean(seeded):
    body = TestClient(main.app).get("/health").json()
    assert body["reviews"] == "ok"
    assert "status" in body and "model" in body  # additive, pre-existing keys kept


def test_health_degraded_and_stats_ops(seeded):
    for _ in range(6):
        _insert(seeded, "error", "HTTPStatusError")
    _insert(seeded, "done")
    client = TestClient(main.app)

    assert client.get("/health").json()["reviews"] == "degraded"

    ops = client.get("/stats").json()["ops"]
    assert ops["reviews_24h"] == {"done": 1, "error": 6}
    assert ops["last_error_class"] == "HTTPStatusError"
    assert ops["journal_mode"].lower() == "wal"
    assert {"lanes", "corpus_loaded", "image_revision", "judge_live"} <= set(ops)
    assert ops["lanes"]["primary"]  # a model string
