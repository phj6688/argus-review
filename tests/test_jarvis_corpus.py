"""Behaviour tests for the Jarvis corpus spine (v0 loader)."""

import json
import sqlite3

import pytest

from argus.api import db
from argus.jarvis import corpus


def _seed_finding(argus_path: str, repo: str, file: str, line: int, title: str) -> int:
    review_id = db.create_review(argus_path, repo, 1, "m")
    conn = db.connect(argus_path)
    try:
        cursor = conn.execute(
            "INSERT INTO findings (review_id, file, line, severity, confidence, "
            "title, description, proposed_fix, rule_id, is_bug, verdict_is_real, "
            "verdict_confidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (review_id, file, line, "major", 0.8, title, "d", "f", None, 1, 1, 0.9),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def _write_sweep(tmp_path, rows: list[dict]) -> str:
    path = tmp_path / "sweep.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return str(path)


def _sweep_row(repo: str, file: str, line: int, title: str, mark: str) -> dict:
    verdicts = {"real": "REAL_BUG", "fp": "FALSE_POSITIVE",
                "stale": "STALE_FIXED", "cant_locate": "CANT_LOCATE"}
    return {"repo": repo, "file": file, "line": line, "title": title,
            "severity": "major", "verdict": verdicts[mark],
            "suggested_mark": mark, "reason": "r"}


@pytest.fixture
def argus_path(tmp_path, monkeypatch) -> str:
    path = tmp_path / "argus.db"
    monkeypatch.setenv("ARGUS_DB_PATH", str(path))
    db.init_db(str(path))
    return str(path)


@pytest.fixture
def jarvis_path(tmp_path) -> str:
    return str(tmp_path / "jarvis.db")


def test_init_creates_corpus_tables(jarvis_path):
    corpus.init_corpus_db(jarvis_path)
    conn = sqlite3.connect(jarvis_path)
    names = {row[0] for row in
             conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"corpus", "eval_runs", "probe_eval_log"} <= names
    assert "knob_history" not in names  # retired: ENG-511 STOP, no writers ever


def test_load_manual_sweep_links_findings_and_labels(argus_path, jarvis_path, tmp_path):
    fid = _seed_finding(argus_path, "example/app", "a.py", 10, "bug A")
    _seed_finding(argus_path, "example/app", "b.py", 20, "nit B")
    rows = [
        _sweep_row("example/app", "a.py", 10, "bug A", "real"),
        _sweep_row("example/app", "b.py", 20, "nit B", "fp"),
        _sweep_row("example/app", "gone.py", 99, "vanished", "real"),
    ]
    sweep = _write_sweep(tmp_path, rows)
    corpus.init_corpus_db(jarvis_path)

    inserted = corpus.CorpusLoader(jarvis_path, argus_path).load_manual_sweep(sweep)

    assert inserted == 3
    conn = sqlite3.connect(jarvis_path)
    conn.row_factory = sqlite3.Row
    got = list(conn.execute(
        "SELECT source, confidence, lane, label, corpus_version, payload_json "
        "FROM corpus ORDER BY id"))
    assert [g["label"] for g in got] == ["tp", "fp", "tp"]
    assert all(g["source"] == "manual_sweep" for g in got)
    assert all(g["confidence"] == "weak" for g in got)
    assert all(g["corpus_version"] == "v0" for g in got)
    payloads = [json.loads(g["payload_json"]) for g in got]
    assert payloads[0]["finding_id"] == fid
    assert payloads[2]["finding_id"] is None  # unmatched row keeps null lineage


def test_cant_locate_is_unknown_and_not_labeled(argus_path, jarvis_path, tmp_path):
    _seed_finding(argus_path, "example/app", "c.py", 30, "maybe C")
    sweep = _write_sweep(tmp_path, [_sweep_row("example/app", "c.py", 30, "maybe C", "cant_locate")])
    corpus.init_corpus_db(jarvis_path)
    loader = corpus.CorpusLoader(jarvis_path, argus_path)

    loader.load_manual_sweep(sweep)

    assert loader.labeled_count() == 0
    conn = sqlite3.connect(jarvis_path)
    assert list(conn.execute("SELECT label FROM corpus"))[0][0] == "unknown"


def test_load_marks_is_gold(argus_path, jarvis_path, tmp_path):
    fid = _seed_finding(argus_path, "example/app", "d.py", 40, "real D")
    db.add_mark(argus_path, fid, "real", marker="alice")
    corpus.init_corpus_db(jarvis_path)

    inserted = corpus.CorpusLoader(jarvis_path, argus_path).load_marks()

    assert inserted == 1
    conn = sqlite3.connect(jarvis_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT source, confidence, label FROM corpus").fetchone()
    assert row["source"] == "marks"
    assert row["confidence"] == "gold"
    assert row["label"] == "tp"


def test_load_manual_sweep_is_idempotent(argus_path, jarvis_path, tmp_path):
    _seed_finding(argus_path, "example/app", "a.py", 10, "bug A")
    sweep = _write_sweep(tmp_path, [_sweep_row("example/app", "a.py", 10, "bug A", "real")])
    corpus.init_corpus_db(jarvis_path)
    loader = corpus.CorpusLoader(jarvis_path, argus_path)

    loader.load_manual_sweep(sweep)
    loader.load_manual_sweep(sweep)

    conn = sqlite3.connect(jarvis_path)
    assert list(conn.execute("SELECT COUNT(*) FROM corpus"))[0][0] == 1


def test_load_incidents_are_gold_probe_lane_positives(argus_path, jarvis_path):
    incidents = [
        {"id": "unscoped_cypher", "severity": "critical", "description": "cross-tenant cypher",
         "evidence": "A query without per-user scoping exposes another tenant's graph"},
        {"id": "client_price", "severity": "critical", "description": "client-trusted price",
         "evidence": "A client-supplied price bypasses server-side pricing"},
    ]
    corpus.init_corpus_db(jarvis_path)
    inserted = corpus.CorpusLoader(jarvis_path, argus_path).load_incidents(incidents)
    assert inserted == 2
    conn = sqlite3.connect(jarvis_path)
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute(
        "SELECT source, confidence, lane, label, payload_json FROM corpus "
        "WHERE source='incident' ORDER BY id"))
    assert len(rows) == 2
    assert all(r["source"] == "incident" and r["confidence"] == "gold"
               and r["lane"] == "probe" and r["label"] == "tp" for r in rows)
    assert json.loads(rows[0]["payload_json"])["id"] == "unscoped_cypher"


def test_counts_by_source(argus_path, jarvis_path, tmp_path):
    fid = _seed_finding(argus_path, "example/app", "a.py", 10, "bug A")
    db.add_mark(argus_path, fid, "real", marker="alice")
    sweep = _write_sweep(tmp_path, [_sweep_row("example/app", "a.py", 10, "bug A", "real")])
    corpus.init_corpus_db(jarvis_path)
    loader = corpus.CorpusLoader(jarvis_path, argus_path)

    loader.load_manual_sweep(sweep)
    loader.load_marks()

    assert loader.counts_by_source() == {"manual_sweep": 1, "marks": 1}
