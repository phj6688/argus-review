"""Project identity and nullable upgrades stay compatible with older databases."""

import sqlite3
from contextlib import closing

import pytest

from argus.api import db
from argus.forge.types import RepoRecord, RepoRef


@pytest.fixture
def conn(tmp_path):
    path = str(tmp_path / "forge.db")
    db.init_db(path)
    with closing(db.connect(path)) as connection:
        yield connection


@pytest.mark.parametrize("archived,stored", [(True, 1), (False, 0), (None, None)])
def test_create_forge_project(conn, monkeypatch, archived, stored):
    monkeypatch.setattr(db, "_now_iso", lambda: "2026-11-01T12:00:00+00:00")
    ref = RepoRef("gitlab", "gitlab.example", "old/path", 42)
    record = RepoRecord("group/project", archived, "main")

    assert db.forge_project(conn, "gitlab:gitlab.example#42") is None
    db.upsert_forge_project(conn, ref, record, visibility="private")

    assert db.forge_project(conn, "gitlab:gitlab.example#42") == {
        "repo_key": "gitlab:gitlab.example#42", "forge": "gitlab",
        "host": "gitlab.example", "project_id": 42,
        "path_with_namespace": "group/project", "default_branch": "main",
        "archived": stored, "visibility": "private",
        "checked_at": "2026-11-01T12:00:00+00:00",
    }


def test_rename_updates_mutable_fields_and_keeps_visibility(conn, monkeypatch):
    stamps = iter(["2026-11-01T12:00:00+00:00", "2026-11-02T12:00:00+00:00",
                   "2026-11-03T12:00:00+00:00"])
    monkeypatch.setattr(db, "_now_iso", lambda: next(stamps))
    ref = RepoRef("gitlab", "gitlab.example", "group/project", 42)
    db.upsert_forge_project(conn, ref, RepoRecord("group/project", False, "main"),
                            visibility="private")
    renamed = RepoRef("gitlab", ref.host, "renamed/project", ref.project_id)
    db.upsert_forge_project(conn, renamed, RepoRecord("renamed/project", True, "trunk"))

    row = db.forge_project(conn, "gitlab:gitlab.example#42")
    assert conn.execute("SELECT COUNT(*) FROM forge_projects").fetchone()[0] == 1
    assert (row["repo_key"], row["forge"], row["host"], row["project_id"]) == (
        "gitlab:gitlab.example#42", "gitlab", "gitlab.example", 42,
    )
    assert (row["path_with_namespace"], row["default_branch"], row["archived"],
            row["visibility"], row["checked_at"]) == (
        "renamed/project", "trunk", 1, "private", "2026-11-02T12:00:00+00:00",
    )

    db.upsert_forge_project(conn, renamed, RepoRecord("renamed/project", False, None),
                            visibility="public")
    row = db.forge_project(conn, row["repo_key"])
    assert (row["visibility"], row["default_branch"], row["archived"]) == ("public", None, 0)


def test_upsert_leaves_transaction_to_caller(conn):
    ref = RepoRef("gitlab", "gitlab.example", "group/project", 42)
    db.upsert_forge_project(conn, ref, RepoRecord("group/project", False, "main"))
    assert db.forge_project(conn, "gitlab:gitlab.example#42")["visibility"] is None
    assert conn.in_transaction
    conn.rollback()
    assert db.forge_project(conn, "gitlab:gitlab.example#42") is None


@pytest.mark.parametrize("project_id", [0, 42, 9223372036854775807])
def test_key_uses_plain_decimal_integer(conn, project_id):
    key = f"gitlab:gitlab.example#{project_id}"
    conn.execute(
        "INSERT INTO forge_projects (repo_key, forge, host, project_id) VALUES (?, 'gitlab', ?, ?)",
        (key, "gitlab.example", project_id),
    )
    row = conn.execute("SELECT repo_key, 'gitlab:' || host || '#' || project_id FROM forge_projects").fetchone()
    assert tuple(row) == (key, key)


@pytest.mark.parametrize("key,forge,host,project_id", [
    ("gitlab:other.example#42", "gitlab", "gitlab.example", 42),
    ("gitlab:gitlab.example#43", "gitlab", "gitlab.example", 42),
    ("gitlab:gitlab.example#042", "gitlab", "gitlab.example", 42),
    ("gitlab:gitlab.example#42", "github", "gitlab.example", 42),
])
def test_check_rejects_inconsistent_identity(conn, key, forge, host, project_id):
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        conn.execute(
            "INSERT INTO forge_projects (repo_key, forge, host, project_id) VALUES (?, ?, ?, ?)",
            (key, forge, host, project_id),
        )


def test_host_project_index_is_unique(conn):
    index = next(row for row in conn.execute("PRAGMA index_list(forge_projects)")
                 if row["name"] == "idx_forge_projects_host_id")
    assert index["unique"] == 1
    assert [row["name"] for row in conn.execute("PRAGMA index_info(idx_forge_projects_host_id)")] == [
        "host", "project_id",
    ]
    assert conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='idx_forge_projects_host_id'"
    ).fetchone()[0] == "CREATE UNIQUE INDEX idx_forge_projects_host_id ON forge_projects(host, project_id)"
    conn.execute(
        "INSERT INTO forge_projects (repo_key, forge, host, project_id) "
        "VALUES ('gitlab:gitlab.example#42', 'gitlab', 'gitlab.example', 42)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO forge_projects (repo_key, forge, host, project_id) "
            "VALUES ('gitlab:gitlab.example#43', 'gitlab', 'gitlab.example', 42)"
        )


def test_init_db_is_idempotent(tmp_path):
    path = str(tmp_path / "forge.db")
    db.init_db(path)
    with closing(db.connect(path)) as connection:
        before = list(connection.iterdump())
        version = connection.execute("PRAGMA schema_version").fetchone()[0]
    db.init_db(path)
    with closing(db.connect(path)) as connection:
        assert list(connection.iterdump()) == before
        assert connection.execute("PRAGMA schema_version").fetchone()[0] == version


_LEGACY_SCHEMA = """
CREATE TABLE reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT NOT NULL, pr INTEGER NOT NULL,
    model TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
    raw_count INTEGER, dropped INTEGER, head_sha TEXT, error TEXT
);
CREATE TABLE pr_heads (
    id INTEGER PRIMARY KEY,
    repo TEXT NOT NULL, pr INTEGER NOT NULL, head_sha TEXT NOT NULL,
    first_seen_at TEXT NOT NULL, pr_updated_at TEXT, head_committed_at TEXT,
    draft INTEGER NOT NULL DEFAULT 0, author TEXT, author_type TEXT, base_ref TEXT,
    status TEXT NOT NULL CHECK(status IN
        ('queued','running','done','error','parked','skipped','superseded','closed')),
    skip_reason TEXT, attempts INTEGER NOT NULL DEFAULT 0, last_error_class TEXT,
    next_attempt_at TEXT, claimed_at TEXT, review_id INTEGER REFERENCES reviews(id),
    completed_at TEXT, last_listed_at TEXT, is_open INTEGER NOT NULL DEFAULT 1,
    UNIQUE(repo, pr, head_sha)
);
CREATE TABLE published_findings (
    id INTEGER PRIMARY KEY, review_id INTEGER NOT NULL REFERENCES reviews(id),
    repo TEXT NOT NULL, pr INTEGER NOT NULL, head_sha TEXT NOT NULL,
    fingerprint TEXT NOT NULL, severity TEXT NOT NULL, category TEXT,
    path TEXT NOT NULL, line INTEGER NOT NULL, end_line INTEGER, title TEXT NOT NULL,
    placement TEXT NOT NULL CHECK(placement IN ('inline','summary')),
    mode TEXT NOT NULL CHECK(mode IN ('posted','dry_run')),
    gh_review_id INTEGER, gh_comment_id INTEGER, posted_at TEXT NOT NULL,
    comment_lookups INTEGER NOT NULL DEFAULT 0,
    UNIQUE(repo, pr, fingerprint, head_sha, mode)
);
CREATE TABLE reply_texts (
    finding_id INTEGER NOT NULL REFERENCES published_findings(id),
    evidence_id TEXT NOT NULL, body TEXT NOT NULL, author_association TEXT,
    author_login TEXT, answered_head TEXT,
    UNIQUE(finding_id, evidence_id)
);
CREATE TABLE publishing_identities (
    repo TEXT NOT NULL, bot_login TEXT NOT NULL, first_seen_at TEXT NOT NULL,
    PRIMARY KEY(repo, bot_login)
);
INSERT INTO reviews (id, repo, pr, model, status, created_at)
    VALUES (1, 'owner/repo', 3, 'test', 'done', '2026-11-01');
INSERT INTO pr_heads (repo, pr, head_sha, first_seen_at, status)
    VALUES ('owner/repo', 3, 'abc', '2026-11-01', 'done');
INSERT INTO published_findings
    (id, review_id, repo, pr, head_sha, fingerprint, severity, path, line, title, placement, mode, posted_at)
    VALUES (1, 1, 'owner/repo', 3, 'abc', 'fp', 'major', 'app.py', 1, 'Missing check', 'inline', 'posted', '2026-11-01');
INSERT INTO reply_texts (finding_id, evidence_id, body, author_association)
    VALUES (1, 'reply-1', 'Fixed the missing check', 'OWNER');
INSERT INTO publishing_identities (repo, bot_login, first_seen_at)
    VALUES ('owner/repo', 'publisher', '2026-11-01');
"""


_SELECT_ALL = {
    "pr_heads": "SELECT * FROM pr_heads",
    "reply_texts": "SELECT * FROM reply_texts",
    "published_findings": "SELECT * FROM published_findings",
    "publishing_identities": "SELECT * FROM publishing_identities",
}


def test_upgrade_preserves_rows_and_adds_nullable_columns(tmp_path):
    path = str(tmp_path / "legacy.db")
    with closing(db.connect(path)) as connection:
        connection.executescript(_LEGACY_SCHEMA)
        before = {table: dict(connection.execute(sql).fetchone()) for table, sql in _SELECT_ALL.items()}

    db.init_db(path)
    db.init_db(path)

    with closing(db.connect(path)) as connection:
        for table, name, kind in [
            ("pr_heads", "author_trusted", "INTEGER"),
            ("pr_heads", "author_id", "INTEGER"),
            ("reply_texts", "author_trusted", "INTEGER"),
            ("published_findings", "anchor_status", "TEXT"),
            ("publishing_identities", "bot_user_id", "INTEGER"),
        ]:
            columns = {row["name"]: row for row in connection.execute(f"PRAGMA table_info({table})")}
            column = columns[name]
            assert (column["type"], column["notnull"], column["dflt_value"]) == (kind, 0, None)
            row = connection.execute(_SELECT_ALL[table]).fetchone()
            assert row[name] is None
            assert {key: row[key] for key in before[table]} == before[table]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_bot_user_id_uses_newest_non_null_identity_for_repo(conn):
    repo = "gitlab:gitlab.example#42"
    assert db.bot_user_id(conn, repo) is None
    conn.executemany(
        "INSERT INTO publishing_identities (repo, bot_login, first_seen_at) VALUES (?, ?, ?)",
        [(repo, "old", "2026-11-01"), (repo, "new", "2026-11-02"),
         (repo, "unknown", "2026-11-03"), ("other/repo", "other", "2026-11-04")],
    )
    assert db.bot_user_id(conn, repo) is None
    conn.execute("UPDATE publishing_identities SET bot_user_id=10 WHERE bot_login='old'")
    assert db.bot_user_id(conn, repo) == 10
    conn.execute("UPDATE publishing_identities SET bot_user_id=20 WHERE bot_login='new'")
    conn.execute("UPDATE publishing_identities SET bot_user_id=30 WHERE bot_login='other'")
    assert db.bot_user_id(conn, repo) == 20
    assert db.bot_user_id(conn, "absent/repo") is None
    assert conn.in_transaction


def test_repo_key_cannot_be_null(conn):
    # SQLite lets a TEXT primary key hold NULL, and a CHECK that evaluates to
    # NULL passes, so the column needs its own NOT NULL.
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL constraint failed"):
        conn.execute("INSERT INTO forge_projects (forge, host, project_id) VALUES ('gitlab', 'gitlab.example', 42)")
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL constraint failed"):
        conn.execute(
            "INSERT INTO forge_projects (repo_key, forge, host, project_id) VALUES (NULL, 'gitlab', 'gitlab.example', 42)"
        )
