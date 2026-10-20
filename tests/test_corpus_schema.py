"""E5-j: knob_history is retired (ENG-511 STOP). A freshly-initialised corpus DB
must not create it, while the tables that hold data still exist."""
from argus.jarvis import corpus


def test_fresh_db_has_no_knob_history(tmp_path):
    db = tmp_path / "jarvis.db"
    corpus.init_corpus_db(str(db))
    con = corpus.connect(str(db))
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "knob_history" not in tables
    assert {"corpus", "eval_runs", "probe_eval_log"} <= tables
