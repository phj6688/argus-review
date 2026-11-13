"""Tests for the 5 console-facing read endpoints.

Same harness as test_api.py: a temp SQLite db via ARGUS_DB_PATH and a stubbed
run_review so nothing hits the network. These lock the JSON SHAPE the Next.js
console (web/lib/types.ts) consumes, plus the DB/corpus-derived numbers.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from argus.api import benchmark, db, main
from argus.client import DEFAULT_MODEL
from argus.review import DEFAULT_BUG_CAP


def _insert_review(path, repo, pr, *, status=db.STATUS_DONE, created_at=None, raw_count=0, dropped=0):
    conn = db.connect(path)
    try:
        cur = conn.execute(
            "INSERT INTO reviews (repo, pr, model, status, created_at, raw_count, dropped) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (repo, pr, "claude-opus-4-8", status,
             (created_at or datetime.now(timezone.utc)).isoformat(), raw_count, dropped),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _insert_finding(path, review_id, *, severity="major", is_bug=1, verdict_is_real=1):
    conn = db.connect(path)
    try:
        conn.execute(
            "INSERT INTO findings ("
            "review_id, file, line, severity, confidence, title, description, "
            "proposed_fix, rule_id, is_bug, verdict_is_real, verdict_confidence"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (review_id, "src/a.py", 1, severity, 0.8, "t", "d", "f", None,
             is_bug, verdict_is_real, 0.9),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "argus.db"
    monkeypatch.setenv("ARGUS_DB_PATH", str(path))
    db.init_db(str(path))
    return str(path)


@pytest.fixture
def seeded(db_path):
    """Two done reviews today + one yesterday; a spread of verified findings.

    Today: r1 (example/app#1) with 1 critical-bug + 1 major-bug + 1 nit(real);
           r2 (example/service#2) with 1 minor(real) + 1 nit(refuted).
    2 days ago: r3 (example/app#3), 1 major-bug(real).
    """
    now = datetime.now(timezone.utc)
    r1 = _insert_review(db_path, "example/app", 1, created_at=now, raw_count=4, dropped=1)
    _insert_finding(db_path, r1, severity="critical", is_bug=1, verdict_is_real=1)
    _insert_finding(db_path, r1, severity="major", is_bug=1, verdict_is_real=1)
    _insert_finding(db_path, r1, severity="nit", is_bug=0, verdict_is_real=1)

    r2 = _insert_review(db_path, "example/service", 2, created_at=now, raw_count=3, dropped=0)
    _insert_finding(db_path, r2, severity="minor", is_bug=0, verdict_is_real=1)
    _insert_finding(db_path, r2, severity="nit", is_bug=0, verdict_is_real=0)  # refuted

    r3 = _insert_review(db_path, "example/app", 3, created_at=now - timedelta(days=2),
                        raw_count=2, dropped=0)
    _insert_finding(db_path, r3, severity="major", is_bug=1, verdict_is_real=1)
    return db_path


@pytest.fixture
def client(seeded):
    with TestClient(main.app) as test_client:
        yield test_client


# ---------------------------------------------------------------- /stats

def test_stats_shape_has_every_key(client):
    body = client.get("/stats").json()
    assert set(body) == {
        "prsReviewed", "openFindings", "bugVsNit", "labeling", "actionRate",
        "avgComments", "trends", "ops",
    }
    # prsReviewed.value is distinct PRs; reviews is the raw run count (re-reviews inflate it).
    assert set(body["prsReviewed"]) == {"value", "reviews", "deltaPct"}
    assert set(body["labeling"]) == {
        "labeled", "located", "precision", "real", "fp", "stale", "cant_locate", "unsure",
    }
    assert set(body["openFindings"]) == {"total", "critical", "major", "minor", "nit"}
    assert set(body["bugVsNit"]) == {"bug", "nit"}
    assert set(body["actionRate"]) == {"pct", "prevPct"}
    assert set(body["avgComments"]) == {"value", "prev"}
    assert set(body["trends"]) == {"reviewsPerDay", "findingsPerDay"}
    for t in body["trends"].values():
        assert set(t) == {"value", "deltaPct", "series"}
        assert isinstance(t["series"], list) and len(t["series"]) == 14
        assert all(isinstance(n, int) for n in t["series"])


def test_stats_counts_computed_from_db(client):
    body = client.get("/stats").json()
    # 3 done reviews seeded
    assert body["prsReviewed"]["value"] == 3
    # verdict_is_real findings: critical1 + major1 + nit1 (r1) + minor1 (r2) + major1 (r3) = 5
    of = body["openFindings"]
    assert of["critical"] == 1
    assert of["major"] == 2
    assert of["minor"] == 1
    assert of["nit"] == 1
    assert of["total"] == 5
    # bug/nit split among the 5 verified: is_bug -> critical,major,major = 3; rest = 2
    assert body["bugVsNit"] == {"bug": 3, "nit": 2}
    # avgComments = verified findings / done reviews = 5/3
    assert body["avgComments"]["value"] == pytest.approx(round(5 / 3, 1))


def test_stats_trend_series_sum_matches_window(client):
    body = client.get("/stats").json()
    # reviews/day series over 14 days should sum to reviews created in that window (3)
    assert sum(body["trends"]["reviewsPerDay"]["series"]) == 3
    # findings/day series sums to all findings created in-window (6 rows, regardless of verdict)
    assert sum(body["trends"]["findingsPerDay"]["series"]) == 6


# ---------------------------------------------------------------- /reports/{period}

def test_report_daily_shape_and_keys(client):
    body = client.get("/reports/daily").json()
    assert body["period"] == "daily"
    assert set(body) == {
        "period", "title", "range", "reviews", "findings", "acted", "merged",
        "actedPct", "severity", "category", "volume", "volumeLabel", "topRepos",
    }
    for item in body["severity"]:
        assert set(item) == {"label", "value", "severity"}
    for item in body["category"]:
        assert set(item) == {"label", "value", "color"}
    for item in body["volume"]:
        assert set(item) == {"label", "value"}
    for item in body["topRepos"]:
        assert set(item) == {"repo", "value"}


def test_report_daily_counts_from_db(client):
    body = client.get("/reports/daily").json()
    # daily window = last 24h -> only the 2 reviews created "now"
    assert body["reviews"] == 2
    # findings in those 2 reviews = 3 (r1) + 2 (r2) = 5
    assert body["findings"] == 5
    # topRepos within window
    repos = {row["repo"]: row["value"] for row in body["topRepos"]}
    assert repos.get("example/app") == 1
    assert repos.get("example/service") == 1


def test_report_monthly_includes_older_review(client):
    body = client.get("/reports/monthly").json()
    # 30d window catches all 3 reviews
    assert body["reviews"] == 3


def test_report_rejects_unknown_period(client):
    assert client.get("/reports/yearly").status_code in (404, 422)


# ---------------------------------------------------------------- /benchmark

def test_benchmark_seven_rows_and_keys(client):
    body = client.get("/benchmark").json()
    assert set(body) == {"rows", "note", "perRepo", "argusEmpty", "gate"}
    assert isinstance(body["note"], str) and body["note"]
    rows = body["rows"]
    assert len(rows) == 7
    metrics = [r["metric"] for r in rows]
    assert metrics == [
        "Comments / PR",
        "Action rate (acted-on)",
        "Merged-past (ignored)",
        "Bug-class share",
        "Cites guideline",
        "Catch rate (recall)",
        "False-positive rate",
    ]
    for r in rows:
        assert {"metric", "note", "argus", "codeRabbit", "winner"} <= set(r)
    # last two rows stay pending
    assert rows[5]["pending"] is True and rows[5]["winner"] is None
    assert rows[6]["pending"] is True and rows[6]["winner"] is None
    assert rows[5]["argus"] == "—" and rows[5]["codeRabbit"] == "—"
    # the gate object drives the verdict dashboard; rows stay diagnostic
    gate = body["gate"]
    assert gate["verdict"] in {"PROMOTE", "KEEP-GATHERING", "KILL", "INSUFFICIENT-DATA"}
    assert {"verdict", "clock", "floors", "axisA", "axisB"} <= set(gate)
    assert len(gate["floors"]) == 4


def test_benchmark_cites_guideline_row(client):
    # Corpus absent in the test env -> documented fallback fraction on the CR side.
    rows = {r["metric"]: r for r in client.get("/benchmark").json()["rows"]}
    guide = rows["Cites guideline"]
    assert guide["note"] == "grounded in docs"
    assert guide["codeRabbit"] == "10%"  # documented 23/234 fallback
    # Not derivable from the findings DB yet -> explicit dash, never a fake 0.
    assert guide["argus"] == "—"
    assert "pending" not in guide  # it is a real CR number, not a pending corpus row


def test_benchmark_cites_guideline_from_temp_corpus(client, tmp_path, monkeypatch):
    # 4 threads, 1 of which cites a guideline -> 25%.
    recs = [
        {"repo": "a", "pr": 1, "state": "MERGED", "resolved": True, "sev": "minor",
         "cites_guideline": True},
        {"repo": "a", "pr": 1, "state": "MERGED", "resolved": False, "sev": "major",
         "cites_guideline": False},
        {"repo": "b", "pr": 2, "state": "MERGED", "resolved": True, "sev": "critical",
         "cites_guideline": False},
        {"repo": "b", "pr": 2, "state": "CLOSED", "resolved": False, "sev": "minor",
         "cites_guideline": False},
    ]
    corpus = tmp_path / "cr.jsonl"
    corpus.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    monkeypatch.setattr(main, "CR_THREADS_PATH", corpus)
    rows = {r["metric"]: r for r in client.get("/benchmark").json()["rows"]}
    assert rows["Cites guideline"]["codeRabbit"] == "25%"


@pytest.mark.parametrize("empty_corpus", [False, True])
def test_benchmark_per_repo_empty_without_baselines(client, tmp_path, monkeypatch, empty_corpus):
    corpus = tmp_path / "cr.jsonl"
    if empty_corpus:
        corpus.write_text("", encoding="utf-8")
    monkeypatch.setattr(main, "CR_THREADS_PATH", corpus)
    monkeypatch.delenv("ARGUS_BENCHMARK_BASELINES_PATH", raising=False)
    assert client.get("/benchmark").json()["perRepo"] == []


@pytest.mark.parametrize("empty_corpus", [False, True])
def test_benchmark_per_repo_from_baselines(client, tmp_path, monkeypatch, empty_corpus):
    corpus = tmp_path / "cr.jsonl"
    if empty_corpus:
        corpus.write_text("", encoding="utf-8")
    monkeypatch.setattr(main, "CR_THREADS_PATH", corpus)
    # Listed out of order on purpose: the endpoint promises busiest first.
    baselines = [
        {"repo": "example/service", "crComments": 84, "resolved": 25,
         "mergedPast": 59, "critMajor": 58, "citesGuideline": 8},
        {"repo": "example/app", "crComments": 150, "resolved": 50,
         "mergedPast": 100, "critMajor": 90, "citesGuideline": 15},
    ]
    path = tmp_path / "baselines.json"
    path.write_text(json.dumps(baselines), encoding="utf-8")
    monkeypatch.setenv("ARGUS_BENCHMARK_BASELINES_PATH", str(path))
    per_repo = client.get("/benchmark").json()["perRepo"]
    assert per_repo == [baselines[1], baselines[0]]
    for row in per_repo:
        assert set(row) == {
            "repo", "crComments", "resolved", "mergedPast", "critMajor", "citesGuideline",
        }
    assert sum(r["crComments"] for r in per_repo) == 234
    assert sum(r["resolved"] for r in per_repo) == 75
    assert sum(r["mergedPast"] for r in per_repo) == 159
    assert sum(r["critMajor"] for r in per_repo) == 148
    assert sum(r["citesGuideline"] for r in per_repo) == 23


def test_benchmark_baselines_skip_malformed_entries(client, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "CR_THREADS_PATH", tmp_path / "absent.jsonl")
    valid = {"repo": "example/app", "crComments": 3, "resolved": 1,
             "mergedPast": 2, "critMajor": 2, "citesGuideline": 1}
    rows = [None, "invalid", {}, {**valid, "repo": 1}, {**valid, "resolved": "1"},
            {**valid, "crComments": -1}, {**valid, "critMajor": True}, valid]
    path = tmp_path / "baselines.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    monkeypatch.setenv("ARGUS_BENCHMARK_BASELINES_PATH", str(path))
    assert client.get("/benchmark").json()["perRepo"] == [valid]


@pytest.mark.parametrize("content", [None, "{", "{}"])
def test_benchmark_baselines_read_errors_are_logged(client, tmp_path, monkeypatch, caplog, content):
    monkeypatch.setattr(benchmark.log, "handlers", [caplog.handler])
    monkeypatch.setattr(main, "CR_THREADS_PATH", tmp_path / "absent.jsonl")
    path = tmp_path / "baselines.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    monkeypatch.setenv("ARGUS_BENCHMARK_BASELINES_PATH", str(path))
    assert client.get("/benchmark").json()["perRepo"] == []
    assert any(record.levelname == "WARNING" and "baseline" in record.message
               for record in caplog.records)


def test_benchmark_per_repo_from_temp_corpus(client, tmp_path, monkeypatch):
    recs = [
        {"repo": "a", "pr": 1, "state": "MERGED", "resolved": True, "sev": "critical",
         "cites_guideline": True},
        {"repo": "a", "pr": 2, "state": "MERGED", "resolved": False, "sev": "major",
         "cites_guideline": False},
        {"repo": "b", "pr": 3, "state": "MERGED", "resolved": True, "sev": "minor",
         "cites_guideline": False},
    ]
    corpus = tmp_path / "cr.jsonl"
    corpus.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    monkeypatch.setattr(main, "CR_THREADS_PATH", corpus)
    per_repo = {r["repo"]: r for r in client.get("/benchmark").json()["perRepo"]}
    assert per_repo["a"]["crComments"] == 2
    assert per_repo["a"]["resolved"] == 1
    assert per_repo["a"]["critMajor"] == 2  # critical + major
    assert per_repo["a"]["citesGuideline"] == 1
    assert per_repo["b"]["crComments"] == 1


def test_cr_threads_path_env_override(monkeypatch, tmp_path):
    # ARGUS_CR_THREADS_PATH wins so the container layout can point at a mounted
    # corpus instead of the (wrong) site-packages-relative default.
    target = tmp_path / "mounted" / "cr_threads.jsonl"
    monkeypatch.setenv("ARGUS_CR_THREADS_PATH", str(target))
    assert main._resolve_cr_threads_path() == target


def test_cr_threads_path_default_is_repo_data_dir(monkeypatch):
    # Unset -> the source-checkout default under the repo data dir.
    monkeypatch.delenv("ARGUS_CR_THREADS_PATH", raising=False)
    resolved = main._resolve_cr_threads_path()
    assert resolved.name == "cr_threads.jsonl"
    assert resolved.parent.name == "data"


def test_benchmark_argus_empty_false_when_runs_exist(client):
    # The seeded fixture has 3 done reviews -> the board is two-sided.
    assert client.get("/benchmark").json()["argusEmpty"] is False


def test_benchmark_argus_empty_true_on_fresh_db(db_path):
    # No reviews seeded -> Argus side has no runs; board reads as one-sided.
    with TestClient(main.app) as fresh:
        body = fresh.get("/benchmark").json()
    assert body["argusEmpty"] is True
    # CR side stays fully populated even when Argus is empty.
    cr = {r["metric"]: r["codeRabbit"] for r in body["rows"]}
    assert cr["Comments / PR"] == "11.3"


def test_benchmark_cr_metrics_from_temp_corpus(client, tmp_path, monkeypatch):
    # 4 threads across 2 PRs: 2 resolved, 1 MERGED&unresolved, 1 critical+1 major.
    recs = [
        {"repo": "a", "pr": 1, "state": "MERGED", "resolved": True, "sev": "minor"},
        {"repo": "a", "pr": 1, "state": "MERGED", "resolved": False, "sev": "major"},
        {"repo": "b", "pr": 2, "state": "MERGED", "resolved": True, "sev": "critical"},
        {"repo": "b", "pr": 2, "state": "CLOSED", "resolved": False, "sev": "minor"},
    ]
    corpus = tmp_path / "cr.jsonl"
    corpus.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    monkeypatch.setattr(main, "CR_THREADS_PATH", corpus)

    rows = {r["metric"]: r for r in client.get("/benchmark").json()["rows"]}
    # 4 threads / 2 PRs = 2.0
    assert rows["Comments / PR"]["codeRabbit"] == "2.0"
    # resolved 2/4 = 50%
    assert rows["Action rate (acted-on)"]["codeRabbit"] == "50%"
    # MERGED&!resolved 1/4 = 25%
    assert rows["Merged-past (ignored)"]["codeRabbit"] == "25%"
    # bug-class (critical|major) 2/4 = 50%
    assert rows["Bug-class share"]["codeRabbit"] == "50%"


def test_benchmark_falls_back_when_corpus_missing(client, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "CR_THREADS_PATH", tmp_path / "does-not-exist.jsonl")
    body = client.get("/benchmark").json()
    assert len(body["rows"]) == 7
    # CR columns fall back to documented baselines (non-empty, not a crash)
    cr = {r["metric"]: r["codeRabbit"] for r in body["rows"]}
    assert cr["Comments / PR"] == "11.3"
    assert cr["Cites guideline"] == "10%"


def test_benchmark_argus_side_from_db(client):
    rows = {r["metric"]: r for r in client.get("/benchmark").json()["rows"]}
    # Argus Comments/PR = verified findings / done reviews = 5/3 -> 1.7
    assert rows["Comments / PR"]["argus"] == "1.7"
    # Argus bug-class share = is_bug / findings among verified = 3/5 = 60%
    assert rows["Bug-class share"]["argus"] == "60%"


# ---------------------------------------------------------------- /config

def test_config_shape_and_bugcap_from_constant(client):
    body = client.get("/config").json()
    assert set(body) == {
        "repos", "hiddenCount", "totalRepos", "models", "modelOptions",
        "bugCap", "minConfidence", "deny", "hard_denied_owners", "watch_mode", "publish",
    }
    assert body["bugCap"] == DEFAULT_BUG_CAP
    assert isinstance(body["minConfidence"], float)
    # Two real stages (deep review + verification), both on the one engine model.
    assert len(body["models"]) == 2, "models must reflect the real review stages"
    for m in body["models"]:
        assert set(m) == {"name", "caption", "model", "tier"}
        assert m["tier"] in ("deep", "triage")
        assert m["model"] == DEFAULT_MODEL
    for r in body["repos"]:
        assert set(r) == {"name", "watched", "stored_watched", "effective_watched", "watched_source"}


# ---------------------------------------------------------------- /integrations

def test_integrations_shape_all_keys(client):
    body = client.get("/integrations").json()
    assert set(body) == {"health", "warning", "github", "api", "mcp"}
    for h in body["health"]:
        assert set(h) == {"label", "status", "state"}
        assert h["state"] in ("ok", "warn", "error")
    assert set(body["github"]) == {"token", "watcher", "publishing"}
    assert set(body["github"]["watcher"]) == {"enabled", "owners", "intervalSeconds", "watchedRepos"}
    assert set(body["github"]["publishing"]) == {"mode", "repoCount", "tokenConfigured", "source", "autoInvite"}
    assert set(body["api"]) == {"baseUrl", "auth", "endpoints"}
    for e in body["api"]["endpoints"]:
        assert set(e) == {"method", "path", "description"}
        assert e["method"] in ("GET", "POST", "PUT")
    assert set(body["mcp"]) == {"status", "config", "tools", "resources"}


# ---------------------------------------------------------------- CORS

def test_cors_allows_console_origin(client):
    res = client.get("/stats", headers={"Origin": "http://localhost:3000"})
    assert res.headers.get("access-control-allow-origin") == "http://localhost:3000"


# ---------------------------------------------------------------- error capture

def test_set_error_records_status_and_reason(db_path):
    rid = db.create_review(db_path, "r", 1, "claude-opus-4-8", head_sha="abc")
    db.set_error(db_path, rid, "APITimeoutError: gateway took too long")
    row = db.get_review(db_path, rid)
    assert row["status"] == db.STATUS_ERROR
    assert row["error"] == "APITimeoutError: gateway took too long"


def test_set_error_truncates_oversized_reason(db_path):
    rid = db.create_review(db_path, "r", 2, "claude-opus-4-8", head_sha="def")
    db.set_error(db_path, rid, "X" * 5000)
    row = db.get_review(db_path, rid)
    assert len(row["error"]) == db._ERROR_REASON_MAX


def test_init_db_adds_error_column_to_legacy_db(tmp_path):
    # A DB created before head_sha/error existed: CREATE TABLE IF NOT EXISTS will
    # not alter it, so init_db must backfill both columns and keep the old row.
    path = str(tmp_path / "legacy.db")
    conn = db.connect(path)
    conn.execute(
        "CREATE TABLE reviews (id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT NOT NULL, "
        "pr INTEGER NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, "
        "raw_count INTEGER, dropped INTEGER)"
    )
    conn.execute(
        "INSERT INTO reviews (repo, pr, model, status, created_at) VALUES (?,?,?,?,?)",
        ("old", 9, "m", db.STATUS_ERROR, "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    db.init_db(path)

    cols = {r["name"] for r in db.connect(path).execute("PRAGMA table_info(reviews)")}
    assert {"head_sha", "error"} <= cols
    row = db.get_review(path, 1)
    assert row["repo"] == "old" and row["error"] is None


def _insert_errored(path, pr, reason):
    conn = db.connect(path)
    try:
        conn.execute(
            "INSERT INTO reviews (repo, pr, model, status, created_at, error) "
            "VALUES (?,?,?,?,?,?)",
            ("r", pr, "claude-opus-4-8", db.STATUS_ERROR,
             datetime.now(timezone.utc).isoformat(), reason),
        )
        conn.commit()
    finally:
        conn.close()


def test_benchmark_gate_buckets_error_kinds(client, db_path):
    # Two timeouts, one overload, one legacy NULL-reason row -> three buckets,
    # timeouts ranked first, the NULL row labelled 'unrecorded'.
    _insert_errored(db_path, 11, "APITimeoutError: too slow")
    _insert_errored(db_path, 12, "APITimeoutError: too slow again")
    _insert_errored(db_path, 13, "InternalServerError: 529 overloaded")
    _insert_errored(db_path, 14, None)

    kinds = client.get("/benchmark").json()["gate"]["reviews"]["errorKinds"]
    by_kind = {k["kind"]: k["count"] for k in kinds}
    assert by_kind["APITimeoutError"] == 2
    assert by_kind["InternalServerError"] == 1
    assert by_kind["unrecorded"] == 1
    # Most frequent first.
    assert kinds[0]["kind"] == "APITimeoutError"
