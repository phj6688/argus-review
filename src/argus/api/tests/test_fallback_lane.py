"""E1-e: an opt-in fallback model runs when the primary fails, recorded on the
row (never silent). Unset = today's fail-hard behavior, byte-identical."""
import pytest

from argus.api import db, main
from argus.review import ReviewResult


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "argus.db"
    monkeypatch.setenv("ARGUS_DB_PATH", str(path))
    db.init_db(str(path))
    return str(path)


def _ok():
    return ReviewResult(raw_count=0, bugs=[], nits=[], dropped=0)


def _boom():
    raise RuntimeError("primary lane down")


def test_no_fallback_primary_failure_errors(db_path):
    review_id = db.create_review(db_path, "example/x", 1, "primary")
    main.run_one_review(db_path, review_id, "example/x", 1, _boom)
    row = db.get_review(db_path, review_id)
    assert row["status"] == "error"
    assert row["error_class"] == "RuntimeError"
    assert row["model_used"] is None and row["fallback_from"] is None


def test_fallback_runs_and_is_recorded(db_path):
    review_id = db.create_review(db_path, "example/x", 1, "primary")
    main.run_one_review(
        db_path, review_id, "example/x", 1, _boom,
        fallback_runner=_ok, primary_model="primary", fallback_model="fb",
    )
    row = db.get_review(db_path, review_id)
    assert row["status"] == "done"
    assert row["model_used"] == "fb"
    assert row["fallback_from"] == "primary"
    assert "primary lane down" in row["fallback_reason"]


def test_fallback_for_env(monkeypatch):
    monkeypatch.delenv("ARGUS_FALLBACK_MODEL", raising=False)
    assert main._fallback_for("m", lambda x: lambda: _ok()) == (None, None)
    monkeypatch.setenv("ARGUS_FALLBACK_MODEL", "fb")
    runner, model = main._fallback_for("m", lambda x: lambda: _ok())
    assert model == "fb" and runner is not None
    # same model -> no fallback
    assert main._fallback_for("fb", lambda x: lambda: _ok()) == (None, None)
