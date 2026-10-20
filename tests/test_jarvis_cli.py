"""Behaviour tests for the Jarvis audit CLIs (explain, why-quiet)."""

import json

import pytest

from argus.api import db
from argus.jarvis import cli, corpus


def _seed_finding(argus_path: str, repo: str, pr: int, file: str, line: int,
                  title: str, *, is_real: int = 1) -> int:
    review_id = db.create_review(argus_path, repo, pr, "m")
    conn = db.connect(argus_path)
    try:
        cursor = conn.execute(
            "INSERT INTO findings (review_id, file, line, severity, confidence, "
            "title, description, proposed_fix, rule_id, is_bug, verdict_is_real, "
            "verdict_confidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (review_id, file, line, "major", 0.8, title, "desc", "fix", "R1", 1,
             is_real, 0.9),
        )
        conn.execute("UPDATE reviews SET status='done', raw_count=1, dropped=0 "
                     "WHERE id=?", (review_id,))
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


@pytest.fixture
def argus_path(tmp_path, monkeypatch) -> str:
    path = tmp_path / "argus.db"
    monkeypatch.setenv("ARGUS_DB_PATH", str(path))
    db.init_db(str(path))
    return str(path)


@pytest.fixture
def jarvis_path(tmp_path, monkeypatch) -> str:
    path = tmp_path / "jarvis.db"
    monkeypatch.setenv(corpus.ENV_DB_PATH, str(path))
    corpus.init_corpus_db(str(path))
    return str(path)


def test_explain_finding_shows_provenance(argus_path, jarvis_path):
    fid = _seed_finding(argus_path, "example/app", 7, "a.py", 10, "bug A")
    out = cli.explain_finding(fid, argus_path=argus_path, jarvis_path=jarvis_path)
    assert "example/app" in out
    assert "7" in out                      # PR number
    assert "bug A" in out
    assert "a.py:10" in out
    assert "unlabeled" in out.lower()      # no operator mark yet
    assert "not in corpus" in out.lower()  # not loaded into a corpus version


def test_explain_finding_includes_operator_mark(argus_path, jarvis_path):
    fid = _seed_finding(argus_path, "example/app", 7, "a.py", 10, "bug A")
    db.add_mark(argus_path, fid, "fp", marker="alice", note="duplicate")
    out = cli.explain_finding(fid, argus_path=argus_path, jarvis_path=jarvis_path)
    assert "fp" in out.lower()
    assert "alice" in out


def test_explain_finding_includes_corpus_membership(argus_path, jarvis_path, tmp_path):
    fid = _seed_finding(argus_path, "example/app", 7, "a.py", 10, "bug A")
    sweep = tmp_path / "sweep.jsonl"
    sweep.write_text(json.dumps({"repo": "example/app", "file": "a.py", "line": 10,
        "title": "bug A", "severity": "major", "verdict": "REAL_BUG",
        "suggested_mark": "real", "reason": "r"}) + "\n")
    corpus.CorpusLoader(jarvis_path, argus_path).load_manual_sweep(str(sweep))
    out = cli.explain_finding(fid, argus_path=argus_path, jarvis_path=jarvis_path)
    assert "manual_sweep" in out
    assert "tp" in out.lower()


def test_explain_missing_finding_is_handled(argus_path, jarvis_path):
    out = cli.explain_finding(999999, argus_path=argus_path, jarvis_path=jarvis_path)
    assert "no finding" in out.lower() or "not found" in out.lower()


def test_why_quiet_no_review(argus_path):
    out = cli.why_quiet("example/app", 5, argus_path=argus_path)
    assert "no review" in out.lower()


def test_why_quiet_reports_zero_findings_as_ran_clean(argus_path):
    review_id = db.create_review(argus_path, "example/app", 9, "m")
    db.set_status(argus_path, review_id, "done")
    out = cli.why_quiet("example/app", 9, argus_path=argus_path)
    assert "0" in out
    assert "clean" in out.lower() or "no findings" in out.lower()


def test_why_quiet_surfaces_a_failed_review(argus_path):
    review_id = db.create_review(argus_path, "example/app", 9, "m")
    db.set_error(argus_path, review_id, "TimeoutError: upstream", error_class="TimeoutError")
    out = cli.why_quiet("example/app", 9, argus_path=argus_path)
    assert "fail" in out.lower() or "error" in out.lower()
    assert "TimeoutError" in out


def test_why_quiet_not_quiet_when_findings_exist(argus_path):
    _seed_finding(argus_path, "example/app", 9, "a.py", 1, "bug A")
    out = cli.why_quiet("example/app", 9, argus_path=argus_path)
    assert "1" in out  # one finding reported, so not actually quiet
