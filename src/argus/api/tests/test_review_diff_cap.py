"""E4-b: POST /review-diff rejects a diff declaring too many files with 413
before the probe scan lane materialises one temp file per entry. Normal diffs
pass the cap."""
import pytest
from fastapi.testclient import TestClient

from argus.api import db, main


@pytest.fixture
def app_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_DB_PATH", str(tmp_path / "argus.db"))
    db.init_db(str(tmp_path / "argus.db"))
    return TestClient(main.app)


def _diff(n):
    return "".join(
        f"diff --git a/f{i}.py b/f{i}.py\n--- a/f{i}.py\n+++ b/f{i}.py\n"
        f"@@ -0,0 +1 @@\n+x = {i}\n"
        for i in range(n)
    )


def test_oversize_file_count_rejected(app_db, monkeypatch):
    # scan lane must never run for an over-cap request
    import argus.jarvis.live as live
    monkeypatch.setattr(live, "probe_findings_for_files",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("scanned")))
    r = app_db.post("/review-diff", json={"diff": _diff(main.MAX_DIFF_FILES + 1), "mode": "fast"})
    assert r.status_code == 413


def test_normal_diff_not_size_rejected(app_db):
    r = app_db.post("/review-diff", json={"diff": _diff(2), "mode": "fast"})
    assert r.status_code != 413
