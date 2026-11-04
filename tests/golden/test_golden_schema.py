"""SQLite schema definitions and repeated initialization."""

from contextlib import closing

from argus.api import db, watch_store


def read_schema(path):
    with closing(db.connect(path)) as conn:
        objects = [tuple(row) for row in conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE type IN ('table', 'index') AND name NOT LIKE 'sqlite_%' "
            "ORDER BY type, name"
        )]
        tables = [row[1] for row in objects if row[0] == "table"]
        table_info = {}
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            table_info[table] = [tuple(row) for row in conn.execute(f"PRAGMA table_info({quoted})")]
        return {
            "objects": objects, "table_info": table_info,
            "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
        }


def test_empty_database(golden, tmp_path):
    path = str(tmp_path / "schema.db")
    golden.db_path = path
    db.init_db(path)
    initial = read_schema(path)

    db.init_db(path)
    repeated = read_schema(path)

    assert initial == repeated
    golden.check("schema", "empty-database", {"schema": initial, "idempotent": initial == repeated})


def test_side_tables(golden, db_path):
    initial = read_schema(db_path)

    with watch_store.connection(db_path):
        pass
    after_connection = read_schema(db_path)

    assert initial == after_connection
    golden.check("schema", "side-tables", {
        "schema": after_connection, "unchanged_after_watch_connection": initial == after_connection,
    })
