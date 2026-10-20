"""Database concurrency defaults."""
from argus.api import db


def test_connect_enables_wal_and_busy_timeout(tmp_path):
    con = db.connect(str(tmp_path / "argus.db"))
    assert con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert con.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
