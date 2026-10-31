"""Dashboard publish settings override the environment without changing watch policy."""

import json
import sqlite3

import pytest

from argus import publish
from argus.api import db


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = str(tmp_path / "argus.db")
    monkeypatch.setenv("ARGUS_DB_PATH", path)
    monkeypatch.setenv("ARGUS_PUBLISH_REPOS", " Env/One, env/TWO, ")
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    monkeypatch.delenv("ARGUS_DENY_OWNERS", raising=False)
    db.init_db(path)
    db.save_config(path, repos=[], model="test", bug_cap=5, min_confidence=0.5)
    return path


def test_null_uses_env_and_saved_list_is_read_at_call_time(store):
    assert db.get_config(store)["publish_repos"] is None
    assert db.get_publish_repos(store) is None
    assert publish.repo_allowlist_source() == "env"
    assert publish.env_repo_allowlist() == {"env/one", "env/two"}
    assert publish.repo_allowlist() == {"env/one", "env/two"}
    db.set_publish_repos(store, ["Dashboard/Repo"])
    assert db.get_config(store)["publish_repos"] == ["Dashboard/Repo"]
    assert publish.repo_allowlist_source() == "dashboard"
    assert publish.repo_allowlist() == {"dashboard/repo"}
    assert publish.enabled_for("DASHBOARD/REPO")
    assert not publish.enabled_for("env/one")
    db.set_publish_repos(store, [])
    assert publish.repo_allowlist() == set()
    assert publish.repo_allowlist_source() == "dashboard"
    assert not publish.enabled_for("dashboard/repo")


def test_missing_database_does_not_create_file_or_parent(tmp_path, monkeypatch):
    path = tmp_path / "absent" / "argus.db"
    monkeypatch.setenv("ARGUS_DB_PATH", str(path))
    assert db.get_publish_repos() is None
    assert db.get_publish_repos(str(path)) is None
    assert not path.parent.exists()


@pytest.mark.parametrize("schema", ["", "CREATE TABLE config (id INTEGER PRIMARY KEY)"])
def test_legacy_database_without_table_or_column(tmp_path, schema):
    path = str(tmp_path / "legacy.db")
    with sqlite3.connect(path) as conn:
        if schema:
            conn.execute(schema)
    assert db.get_publish_repos(path) is None


def test_no_singleton_row_returns_none(tmp_path):
    path = str(tmp_path / "empty.db")
    db.init_db(path)
    assert db.get_publish_repos(path) is None


@pytest.mark.parametrize("value", ["{bad json", '"repo"', '{}', '[123]', 'null'])
def test_invalid_stored_value_fails_closed(store, value):
    with sqlite3.connect(store) as conn:
        conn.execute("UPDATE config SET publish_repos=? WHERE id=1", (value,))
    with pytest.raises(db.PublishReposUnreadable):
        db.get_publish_repos(store)
    assert db.get_config(store)["publish_repos"] is None
    assert publish.repo_allowlist() == set()
    assert not publish.enabled_for("env/one")


def test_hard_deny_changes_filter_stored_entries(store, monkeypatch):
    db.set_publish_repos(store, ["restricted/repo", "safe/repo"])
    assert publish.repo_allowlist() == {"restricted/repo", "safe/repo"}
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "Restricted")
    assert publish.repo_allowlist() == {"safe/repo"}
    assert publish.repo_allowlist_source() == "dashboard"


def test_broken_db_read_fails_closed(store, monkeypatch):
    def broken(path=None):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(db, "get_publish_state", broken)
    assert publish.repo_allowlist() == set()
    assert not publish.enabled_for("env/one")
    assert publish.repo_allowlist_source() == "dashboard"


def test_locked_database_fails_closed(store, monkeypatch):
    db.set_publish_repos(store, [])
    real = sqlite3.connect

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db.sqlite3, "connect", locked)
    assert publish.repo_allowlist() == set()
    monkeypatch.setattr(db.sqlite3, "connect", real)


def test_config_deny_list_filters_stored_entries(store):
    db.set_publish_repos(store, ["safe/repo", "blocked/repo", "wholeowner/repo"])
    db.save_config(store, repos=[], model="test", bug_cap=5, min_confidence=0.5,
                   deny=["blocked/repo", "wholeowner"])
    assert publish.repo_allowlist() == {"safe/repo"}


def test_config_upsert_preserves_publish_repos(store):
    db.set_publish_repos(store, ["dashboard/repo"])
    db.save_config(store, repos=[{"name": "watch/repo"}], model="changed",
                   bug_cap=7, min_confidence=0.7)
    db.init_db(store)
    assert db.get_publish_repos(store) == ["dashboard/repo"]
    with sqlite3.connect(store) as conn:
        assert conn.execute("SELECT publish_repos FROM config").fetchone()[0] == json.dumps(["dashboard/repo"])


def test_watcher_discovery_preserves_publish_repos(store, monkeypatch):
    from argus.api import watcher

    db.set_publish_repos(store, ["dashboard/repo"])
    monkeypatch.setenv("ARGUS_WATCH_OWNERS", "example")
    monkeypatch.setattr(watcher.github, "owned_repo_records", lambda: [])
    watcher.discover_repos(store)
    assert db.get_publish_repos(store) == ["dashboard/repo"]


def test_env_entries_in_config_deny_list_are_filtered(store, monkeypatch):
    monkeypatch.setenv("ARGUS_PUBLISH_REPOS", "env/one,Blocked/Repo,wholeowner/x,env/two")
    db.save_config(store, repos=[], model="test", bug_cap=5, min_confidence=0.5,
                   deny=["blocked/repo", "wholeowner"])
    assert publish.repo_allowlist_source() == "env"
    assert publish.repo_allowlist() == {"env/one", "env/two"}
    assert not publish.enabled_for("blocked/repo")


def test_env_hard_denied_owner_is_filtered(store, monkeypatch):
    monkeypatch.setenv("ARGUS_PUBLISH_REPOS", "env/one,restricted/repo")
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "restricted")
    assert publish.repo_allowlist() == {"env/one"}


def test_malformed_env_entries_are_dropped(store, monkeypatch):
    monkeypatch.setenv("ARGUS_PUBLISH_REPOS", "env/one,foo,a/b/c,../x,-bad/repo,env/two")
    assert publish.repo_allowlist() == {"env/one", "env/two"}


@pytest.mark.parametrize("name", ["foo", "a/b/c", "../x", "-bad/repo", "example/.."])
def test_malformed_stored_entries_are_dropped(store, name):
    db.set_publish_repos(store, ["example/repo", name])
    assert publish.repo_allowlist() == {"example/repo"}
    assert not publish.enabled_for(name)
    assert not publish.enabled_for("env/one")


@pytest.mark.parametrize("deny", ["{bad json", '"owner"', "[1]", '{"a": 1}'])
def test_unreadable_deny_list_fails_closed_for_a_dashboard_list(store, deny):
    db.set_publish_repos(store, ["safe/repo", "blocked/repo"])
    with sqlite3.connect(store) as conn:
        conn.execute("UPDATE config SET deny=? WHERE id=1", (deny,))
    with pytest.raises(db.PublishReposUnreadable):
        db.get_publish_state(store)
    assert publish.repo_allowlist() == set()


def test_unreadable_deny_list_also_blocks_the_env_fallback(store):
    with sqlite3.connect(store) as conn:
        conn.execute("UPDATE config SET deny=? WHERE id=1", ("{bad json",))
    with pytest.raises(db.PublishReposUnreadable):
        db.get_publish_state(store)
    assert publish.repo_allowlist() == set()


def _legacy_db(tmp_path, *, with_deny, deny="[]"):
    path = str(tmp_path / "legacy.db")
    columns = "id INTEGER PRIMARY KEY, repo_allowlist TEXT NOT NULL, model TEXT NOT NULL, " \
              "bug_cap INTEGER NOT NULL, min_confidence REAL NOT NULL"
    with sqlite3.connect(path) as conn:
        if with_deny:
            conn.execute(f"CREATE TABLE config ({columns}, deny TEXT NOT NULL DEFAULT '[]')")
            conn.execute("INSERT INTO config (id, repo_allowlist, model, bug_cap, min_confidence, deny) "
                         "VALUES (1, '[]', 'm', 5, 0.5, ?)", (deny,))
        else:
            conn.execute(f"CREATE TABLE config ({columns})")
            conn.execute("INSERT INTO config VALUES (1, '[]', 'm', 5, 0.5)")
    return path


def test_old_schema_with_deny_but_no_publish_column_keeps_the_deny_list(tmp_path, monkeypatch):
    path = _legacy_db(tmp_path, with_deny=True, deny='["blocked/repo", "wholeowner"]')
    monkeypatch.setenv("ARGUS_DB_PATH", path)
    monkeypatch.setenv("ARGUS_PUBLISH_REPOS", "env/one,blocked/repo,wholeowner/x")
    assert db.get_publish_state(path) == (None, ["blocked/repo", "wholeowner"])
    assert publish.repo_allowlist_source() == "env"
    assert publish.repo_allowlist() == {"env/one"}


def test_old_schema_with_a_corrupt_deny_list_fails_closed(tmp_path, monkeypatch):
    path = _legacy_db(tmp_path, with_deny=True, deny="{bad json")
    monkeypatch.setenv("ARGUS_DB_PATH", path)
    monkeypatch.setenv("ARGUS_PUBLISH_REPOS", "env/one")
    with pytest.raises(db.PublishReposUnreadable):
        db.get_publish_state(path)
    assert publish.repo_allowlist() == set()


def test_old_schema_without_deny_column_reads_as_no_policy(tmp_path, monkeypatch):
    path = _legacy_db(tmp_path, with_deny=False)
    monkeypatch.setenv("ARGUS_DB_PATH", path)
    monkeypatch.setenv("ARGUS_PUBLISH_REPOS", "env/one")
    assert db.get_publish_state(path) == (None, [])
    assert publish.repo_allowlist() == {"env/one"}


def test_init_migrates_legacy_config_without_resetting_publish_list(tmp_path):
    path = _legacy_db(tmp_path, with_deny=True, deny='["blocked/repo"]')
    db.init_db(path)
    assert db.get_publish_state(path) == (None, ["blocked/repo"])
    db.set_publish_repos(path, [])
    db.init_db(path)
    assert db.get_publish_state(path) == ([], ["blocked/repo"])
