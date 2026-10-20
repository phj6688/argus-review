"""The corpus spine: one table pooling scattered ground truth.

The reviewer is just its first query. v0 pools the frozen manual sweep and any
operator marks; later versions add incidents and the CodeRabbit export. Labels
are deliberately conservative: a sweep verdict the grader could not locate is
'unknown', never a silent true/false positive.
"""

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ..api import db as argus_db

ENV_DB_PATH = "JARVIS_DB_PATH"
_DEFAULT_DB = Path(__file__).resolve().parents[3] / "data" / "jarvis.db"

# The sweep is a frozen one-time pass (data/fixtures/README.md). Its date is the
# row's validation instant, since no per-row timestamp exists in the fixture.
SWEEP_VALIDATED_AT = "2026-06-23"

DEFAULT_VERSION = "v0"

# An operator/sweep mark collapses to one of three truth values. 'stale' was a
# real defect since fixed, so it counts as a true positive; 'cant_locate' and
# 'unsure' carry no usable signal, so they are 'unknown' and never labelled.
_MARK_TO_LABEL = {
    "real": "tp",
    "stale": "tp",
    "fp": "fp",
    "cant_locate": "unknown",
    "unsure": "unknown",
}

LABELLED = ("tp", "fp")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS corpus (
    id                INTEGER PRIMARY KEY,
    source            TEXT CHECK(source IN ('marks','findings_raw','cr_export','manual_sweep','known_fp','incident')),
    confidence        TEXT CHECK(confidence IN ('gold','high','weak')),
    lane              TEXT CHECK(lane IN ('probe','semantic','both')),
    label             TEXT CHECK(label IN ('tp','fp','unknown')),
    last_validated_at TEXT,
    corpus_version    TEXT,
    payload_json      TEXT
);

CREATE TABLE IF NOT EXISTS eval_runs (
    id                   INTEGER PRIMARY KEY,
    corpus_version       TEXT,
    n_positives          INTEGER,
    n_negatives          INTEGER,
    n_probes             INTEGER,
    loo_recall           REAL,
    loo_recall_ci        TEXT,
    fp_per_week          REAL,
    intent_recovery_rate REAL,
    ran_at               TEXT
);

-- One row per probe run: did the probe's path glob still match (a probe that
-- silently stops matching after a refactor is otherwise invisible), and did it
-- fire. Lets a probe's decay be measured, not assumed.
CREATE TABLE IF NOT EXISTS probe_eval_log (
    id               INTEGER PRIMARY KEY,
    probe_id         TEXT,
    pr_or_corpus_ref TEXT,
    path_matched     INTEGER,
    fired            INTEGER,
    ran_at           TEXT
);

CREATE INDEX IF NOT EXISTS idx_corpus_version ON corpus(corpus_version);
CREATE INDEX IF NOT EXISTS idx_corpus_source ON corpus(source);
"""


def db_path() -> str:
    import os
    return os.environ.get(ENV_DB_PATH, str(_DEFAULT_DB))


def connect(path: str | None = None) -> sqlite3.Connection:
    target = path or db_path()
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    return conn


def init_corpus_db(path: str | None = None) -> None:
    conn = connect(path)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


@dataclass(frozen=True)
class _Row:
    source: str
    confidence: str
    lane: str
    label: str
    last_validated_at: str
    payload: dict


@dataclass
class CorpusLoader:
    """Builds one corpus version from the live Argus db plus the sweep fixture."""

    jarvis_path: str
    argus_path: str
    version: str = DEFAULT_VERSION

    def load_manual_sweep(self, sweep_path: str) -> int:
        index = self._finding_index()
        rows = _read_jsonl(sweep_path)
        return self._replace("manual_sweep", [self._sweep_row(r, index) for r in rows])

    def load_marks(self) -> int:
        latest = self._latest_marks()
        return self._replace("marks", [_mark_row(m) for m in latest])

    def load_incidents(self, incidents: list[dict]) -> int:
        """Incidents are operator-confirmed real bugs, so gold true positives on
        the probe lane. Each carries its evidence in the payload."""
        rows = [_Row("incident", "gold", "probe", "tp", incident.get("evidence", ""), incident)
                for incident in incidents]
        return self._replace("incident", rows)

    def counts_by_source(self) -> dict[str, int]:
        conn = connect(self.jarvis_path)
        try:
            rows = conn.execute(
                "SELECT source, COUNT(*) AS n FROM corpus WHERE corpus_version = ? "
                "GROUP BY source", (self.version,),
            ).fetchall()
        finally:
            conn.close()
        return {row["source"]: row["n"] for row in rows}

    def labeled_count(self) -> int:
        conn = connect(self.jarvis_path)
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM corpus WHERE corpus_version = ? "
                "AND label IN ('tp', 'fp')", (self.version,),
            ).fetchone()
        finally:
            conn.close()
        return int(row["n"])

    def _finding_index(self) -> dict[tuple, int]:
        conn = argus_db.connect(self.argus_path)
        try:
            rows = conn.execute(
                "SELECT f.id, r.repo, f.file, f.line, f.title FROM findings f "
                "JOIN reviews r ON r.id = f.review_id"
            ).fetchall()
        finally:
            conn.close()
        return {_match_key(r["repo"], r["file"], r["line"], r["title"]): r["id"]
                for r in rows}

    def _latest_marks(self) -> list[dict]:
        conn = argus_db.connect(self.argus_path)
        try:
            # Machine marks (namespaced markers: automark:*, agent:*) are
            # excluded: the 'marks' source is gold operator ground truth, and a
            # machine label folded in here would silently relabel the corpus.
            # Bare markers are human identities and stay gold.
            rows = conn.execute(
                "SELECT m.finding_id, m.mark, m.marker, m.marked_at, m.note, "
                "r.repo, f.file, f.line, f.title FROM marks m "
                "JOIN findings f ON f.id = m.finding_id "
                "JOIN reviews r ON r.id = f.review_id "
                "WHERE m.marker IS NULL OR m.marker NOT LIKE '%:%' "
                "ORDER BY m.id"
            ).fetchall()
        finally:
            conn.close()
        latest: dict[int, dict] = {}
        for row in rows:  # ordered by id asc, so the newest label for a finding wins
            latest[row["finding_id"]] = dict(row)
        return list(latest.values())

    def _sweep_row(self, raw: dict, index: dict[tuple, int]) -> _Row:
        finding_id = index.get(
            _match_key(raw["repo"], raw["file"], raw["line"], raw["title"]))
        label = _MARK_TO_LABEL.get(raw["suggested_mark"], "unknown")
        return _Row("manual_sweep", "weak", "semantic", label, SWEEP_VALIDATED_AT,
                    {**raw, "finding_id": finding_id})

    def _replace(self, source: str, rows: list[_Row]) -> int:
        conn = connect(self.jarvis_path)
        try:
            conn.execute(
                "DELETE FROM corpus WHERE source = ? AND corpus_version = ?",
                (source, self.version))
            conn.executemany(
                "INSERT INTO corpus (source, confidence, lane, label, "
                "last_validated_at, corpus_version, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(r.source, r.confidence, r.lane, r.label, r.last_validated_at,
                  self.version, json.dumps(r.payload)) for r in rows])
            conn.commit()
        finally:
            conn.close()
        return len(rows)


def _mark_row(mark: dict) -> _Row:
    label = _MARK_TO_LABEL.get(mark["mark"], "unknown")
    return _Row("marks", "gold", "semantic", label, mark["marked_at"], mark)


def _match_key(repo: str, file: str, line: int, title: str) -> tuple:
    return (repo, file, line, (title or "").strip())


def _read_jsonl(path: str) -> list[dict]:
    with open(path) as handle:
        return [json.loads(line) for line in handle if line.strip()]
