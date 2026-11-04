"""Keep baseline measurements stable when other forges share the database."""

import argparse
import contextlib
import importlib.util
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import mock

import httpx
import measurement_support as parity
import pytest
from fastapi.testclient import TestClient

from argus import acceptance
from argus.api import benchmark, db, main, metrics, watch_metrics
from argus.evalkit import monthly
from argus.jarvis import corpus

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = ROOT / "tests/fixtures/measurement_github_only.json"
SINCE = "2026-10-01T00:00:00+00:00"
UNTIL = "2026-10-20T12:00:00+00:00"
MOMENT = datetime.fromisoformat(UNTIL)
GITLAB = "gitlab:gitlab.example.com#12"
GITHUB = ("owner/re_po", "owner/re-po", "owner/re%po")


def _script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


export_fps = _script("export_known_fps")


def seed(path, *, gitlab: bool):
    """Use existing storage APIs so this also runs before the filter change."""
    db.init_db(str(path))
    with contextlib.closing(sqlite3.connect(path)) as conn, conn:
        def insert(table, **values):
            return conn.execute(
                f"INSERT OR REPLACE INTO {table} ({','.join(values)}) "
                f"VALUES ({','.join('?' for _ in values)})", tuple(values.values()),
            ).lastrowid

        for foreign in range(2 if gitlab else 1):
            for i in range(18 if foreign else 6):
                repo = GITLAB if foreign else GITHUB[i % len(GITHUB)]
                status = (("done", "error", "error", "running")[i % 4] if foreign else
                          ("done", "done", "done", "error", "running", "done")[i])
                stamp = ("2026-10-01T01:00:00+00:00" if foreign and i == 0 else
                         "2026-10-20T11:55:00+00:00" if foreign else
                         f"2026-10-{(14, 15, 19, 20, 20, 20)[i]:02d}T10:00:00+00:00")
                # A repeated GitHub PR distinguishes review volume from PR volume.
                pr = i + 1 if foreign else (1, 2, 3, 4, 5, 3)[i]
                head = f"head-{foreign}-{i}"
                review = insert(
                    "reviews", repo=repo, pr=pr, model="test", status=status,
                    created_at=stamp, completed_at=stamp if status == "done" else None,
                    head_sha=head, publish_status="posted", published_at=stamp,
                    error="GitLabError: failure" if foreign else "TimeoutError: failure",
                    error_class="gitlab_failure" if foreign else "timeout",
                )
                for j in range(3):
                    finding = insert(
                        "findings", review_id=review, file="app.py", line=j + 1,
                        severity=("major", "nit", "minor")[j], confidence=.9,
                        title=f"Finding {i}-{j}", description="description", proposed_fix="fix",
                        is_bug=int(j != 1), verdict_is_real=int(j != 2), verdict_confidence=.9,
                    )
                    for marker, mark in ((None, "real"), ("reviewer", "fp" if j else "real"),
                                         ("automark:test", "stale")):
                        insert("marks", finding_id=finding, mark=mark, marker=marker,
                               marked_at=stamp, note="label")
                    published = insert(
                        "published_findings", review_id=review, repo=repo, pr=pr,
                        head_sha=head, fingerprint=f"fp-{i}-{j}", severity="major",
                        path="app.py", line=j + 1, title=f"Finding {i}-{j}",
                        placement="summary" if j == 1 else "inline", mode="posted", posted_at=stamp,
                    )
                    insert("finding_outcomes", finding_id=published, kind="reply",
                           value="fixed" if j == 0 else "rejected", evidence_id="1",
                           observed_at=stamp, occurred_at=stamp)
                    insert("reply_texts", finding_id=published, evidence_id="1",
                           body="Fixed", author_association="MEMBER", author_login="reviewer")
                    insert("finding_outcomes", finding_id=published, kind="addressed",
                           value="true", evidence_id="later-head", observed_at=stamp)
                    for kind, value in (("recheck", "fixed"), ("reply", "fixed"), ("resolve", "true")):
                        insert("bot_actions", finding_id=published, kind=kind, value=value,
                               evidence_id="later-head", created_at=stamp)
                head_id = insert(
                    "pr_heads", repo=repo, pr=pr, head_sha=head, first_seen_at=stamp,
                    eligible_at=stamp, eligible_pr_updated_at=stamp, head_committed_at=stamp,
                    status=status, review_id=review, is_open=int(i % 2 == 0),
                    baseline=int(i == 1), skip_reason="draft" if i == 2 else None,
                )
                insert("watch_attempts", head_id=head_id, review_id=review,
                       finished_at=stamp, error_class="timeout" if status == "error" else None)
                insert("outcome_prs", repo=repo, pr=pr, state="closed", merged=1,
                       closed_at=stamp, checked_at=stamp, head_sha=head)
                insert("known_fps_live", repo=repo, file="app.py", title=f"Rejected {i}",
                       fingerprint=f"rejected-{i}", reply_id=str(i), rejected_at=stamp)
            for repo in ((GITLAB,) if foreign else GITHUB):
                insert("outcome_cursors", repo=repo, since=SINCE,
                       last_collected_at=UNTIL if foreign else "2026-10-20T11:00:00+00:00")
                insert("publishing_identities", repo=repo, bot_login="gitlab-bot" if foreign else "publisher",
                       first_seen_at=SINCE)
        # Old attempts can outlive their head row and must retain their original weight.
        insert("watch_attempts", head_id=99999, finished_at="2026-10-20T11:00:00+00:00",
               error_class="orphan")
        insert("coverage_reports", day="2026-10-19", created_at=UNTIL,
               report_json=json.dumps({"day": "2026-10-19", "eligible_heads": 2}))


def collect_outputs(path):
    """Collect deterministic outputs using only APIs available before the change."""
    path = str(path)
    with tempfile.TemporaryDirectory(dir=Path(path).parent) as scratch:
        scratch = Path(scratch)
        reference = scratch / "prs.jsonl"
        reference.write_text("".join(json.dumps({"repo": repo, "pr": 1}) + "\n"
                                     for repo in (GITHUB[0], GITHUB[1])))
        cr_threads = scratch / "cr.jsonl"
        cr_threads.write_text("")
        with (mock.patch.dict(os.environ, {"ARGUS_DB_PATH": path, "ARGUS_AUTH_MODE": "proxy",
                                         "ARGUS_BENCHMARK_BASELINES_PATH": ""}),
              parity.frozen_clock(MOMENT),
              mock.patch.object(main, "datetime", parity._frozen_datetime(MOMENT)),
              mock.patch.object(benchmark, "datetime", parity._frozen_datetime(MOMENT)),
              mock.patch.object(main, "CR_THREADS_PATH", cr_threads),
              mock.patch.object(httpx.HTTPTransport, "handle_request", side_effect=AssertionError("HTTP forbidden")),
              mock.patch.object(httpx.AsyncHTTPTransport, "handle_async_request", side_effect=AssertionError("HTTP forbidden"))):
            measured = parity.measure(path, SINCE, UNTIL, "2026-10")
            # Watchdog counters intentionally see every forge and are tested separately.
            measured["metrics"].pop("window_6h")
            measured["metrics"].pop("top_error_class_24h")
            out = {f"metrics.{key}": value for key, value in measured.pop("metrics").items()}
            out.update(measured)
            out["acceptance.stats"] = acceptance.stats(path, SINCE)
            out["watch.stats"] = watch_metrics.stats(path, now=MOMENT)
            out["monthly.full"] = monthly.build(path, {}, "2026-10", reference_prs=reference)
            # Avoid lifespan startup, which retires running rows and starts background workers.
            client = TestClient(main.app)
            try:
                for route in ("/benchmark", "/stats", "/reports/weekly"):
                    response = client.get(route)
                    assert response.status_code == 200, response.text
                    body = response.json()
                    if route == "/stats":
                        ops = body.pop("ops")
                        out["stats.acceptance"] = ops["acceptance"]
                        out["stats.coverage"] = ops["coverage"]
                    out[route] = body
            finally:
                client.close()
            jarvis_path = str(scratch / "corpus.db")
            corpus.init_corpus_db(jarvis_path)
            loader = corpus.CorpusLoader(jarvis_path, path)
            out["corpus.load_marks"] = loader.load_marks()
            with contextlib.closing(corpus.connect(jarvis_path)) as conn:
                out["corpus.marks"] = [dict(row) for row in conn.execute("SELECT * FROM corpus ORDER BY id")]
            out["corpus.finding_index"] = sorted([list(key), value] for key, value in loader._finding_index().items())
            stdout = io.StringIO()
            with mock.patch.object(sys, "argv", ["export_known_fps.py", path]), contextlib.redirect_stdout(stdout):
                export_fps.main()
            out["known_fps"] = stdout.getvalue()
    return json.loads(json.dumps(out, default=str))


READERS = (
    "acceptance", "coverage", "labelled_findings", "monthly", "monthly.full",
    "acceptance.stats", "watch.stats", "/benchmark", "/stats", "/reports/weekly",
    "stats.acceptance", "stats.coverage", "corpus.load_marks", "corpus.marks",
    "corpus.finding_index", "known_fps",
    *[f"metrics.{name}" for name in (
        "distinct_prs", "done_reviews", "mark_precision_human", "mark_precision_all",
        "verified_severity", "verified_bug_split", "verified_findings", "status_counts",
        "first_review_at", "last_error_at", "error_reasons", "repo_counts",
        "daily_reviews_7d", "daily_findings_7d", "rollup_7d",
    )],
)


def test_github_only_expected(tmp_path, monkeypatch):
    if not EXPECTED.exists():
        pytest.skip("Operator must generate measurement_github_only.json from the pre-change code with --write")
    monkeypatch.delenv("ARGUS_GITLAB_URL", raising=False)
    path = tmp_path / "github.db"
    seed(path, gitlab=False)
    assert collect_outputs(path) == json.loads(EXPECTED.read_text())


def test_collect_outputs_ignores_benchmark_baseline_override(tmp_path, monkeypatch):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps([{
        "repo": "outside/repository", "crComments": 10, "resolved": 2,
        "mergedPast": 1, "critMajor": 3, "citesGuideline": 0,
    }]))
    monkeypatch.setenv("ARGUS_BENCHMARK_BASELINES_PATH", str(baseline))
    path = tmp_path / "github.db"
    seed(path, gitlab=False)
    assert collect_outputs(path)["/benchmark"]["perRepo"] == []


@pytest.fixture(scope="module", params=[False, True], ids=["unconfigured", "configured"])
def measurements(request, tmp_path_factory):
    root = tmp_path_factory.mktemp("measurements")
    github, mixed = root / "github.db", root / "mixed.db"
    seed(github, gitlab=False)
    seed(mixed, gitlab=True)
    with mock.patch.dict(os.environ):
        os.environ.pop("ARGUS_GITLAB_URL", None)
        if request.param:
            os.environ["ARGUS_GITLAB_URL"] = "https://gitlab.example.com"
        yield request.param, collect_outputs(github), collect_outputs(mixed)


@pytest.mark.parametrize("reader", READERS)
def test_gitlab_does_not_change_baseline(measurements, reader):
    configured, github, mixed = measurements
    assert set(github) == set(mixed) == set(READERS)
    before, after = github[reader], mixed[reader]
    if reader == "monthly.full" and configured:
        assert before["skipped_gitlab"] == 0
        assert after["skipped_gitlab"] == 72  # 54 findings plus 18 posted reviews.
        before = {key: value for key, value in before.items() if key != "skipped_gitlab"}
        after = {key: value for key, value in after.items() if key != "skipped_gitlab"}
    assert after == before


@pytest.mark.parametrize("repo", ["GITLAB:gitlab.example.com#12", "other:x"])
def test_other_forge_prefixes_do_not_change_baseline(tmp_path, monkeypatch, repo):
    monkeypatch.delenv("ARGUS_GITLAB_URL", raising=False)
    github, mixed = tmp_path / "github.db", tmp_path / "mixed.db"
    seed(github, gitlab=False)
    seed(mixed, gitlab=True)
    with contextlib.closing(sqlite3.connect(mixed)) as conn, conn:
        for table in ("reviews", "published_findings", "pr_heads", "outcome_prs",
                      "outcome_cursors", "publishing_identities", "known_fps_live"):
            conn.execute(f"UPDATE {table} SET repo=? WHERE repo=?", (repo, GITLAB))
    before, after = collect_outputs(github), collect_outputs(mixed)
    for reader in READERS:
        assert after[reader] == before[reader], reader


def test_reference_pr_denominator_ignores_foreign_entries(tmp_path):
    path = tmp_path / "mixed.db"
    seed(path, gitlab=True)
    github_only, with_foreign = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    entries = [{"repo": repo, "pr": 1} for repo in GITHUB[:2]]
    github_only.write_text("".join(json.dumps(e) + "\n" for e in entries))
    foreign = [{"repo": GITLAB, "pr": 1}, {"repo": "other:x", "pr": 1}]
    with_foreign.write_text("".join(json.dumps(e) + "\n" for e in entries + foreign))
    with parity.frozen_clock(MOMENT):
        before = monthly.build(str(path), {}, "2026-10", reference_prs=github_only)
        after = monthly.build(str(path), {}, "2026-10", reference_prs=with_foreign)
    assert before["rows"]["coverage"]["current"]["n"] == 2
    assert after["rows"]["coverage"] == before["rows"]["coverage"]


def test_operational_metrics_include_gitlab(tmp_path):
    github, mixed = tmp_path / "github.db", tmp_path / "mixed.db"
    seed(github, gitlab=False)
    seed(mixed, gitlab=True)
    with parity.frozen_clock(MOMENT):
        assert metrics.reviews_window(6, str(github)) == {"done": 1, "error": 1}
        assert metrics.reviews_window(6, str(mixed)) == {"done": 5, "error": 10}
        assert metrics.top_error_class(24, str(github)) == "timeout"
        assert metrics.top_error_class(24, str(mixed)) == "gitlab_failure"
        assert metrics.review_health(path=str(github)) == "ok"
        assert metrics.review_health(path=str(mixed)) == "degraded"


def test_coverage_preserves_orphan_attempt(tmp_path):
    path = tmp_path / "mixed.db"
    seed(path, gitlab=True)
    with contextlib.closing(db.connect(str(path))) as conn:
        result = watch_metrics.coverage(conn, datetime.fromisoformat(SINCE), MOMENT)
    assert result["finished_attempts"] == 7
    assert result["errors_by_class"] == {"timeout": 1, "orphan": 1}


@pytest.mark.parametrize("column", ["repo", "r.repo", "_repo", "r_1.repo2"])
def test_github_key_sql_matches_python(column):
    from argus.forge.keys import github_key_sql, is_github_key

    assert github_key_sql(column) == f"instr({column}, ':') = 0"
    keys = ["owner/re_po", "owner/re%po", "gitlab:h#1", "GITLAB:h#1", "other:x", "a/b"]
    with contextlib.closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("CREATE TABLE keys(repo TEXT)")
        conn.executemany("INSERT INTO keys VALUES (?)", [(key,) for key in keys])
        selected = [row[0] for row in conn.execute(
            f"SELECT repo FROM keys r WHERE {github_key_sql('r.repo')} ORDER BY rowid")]
    assert selected == [key for key in keys if is_github_key(key)]


@pytest.mark.parametrize("column", ["a;b", "a b", "1a", "a.b.c", "", "a'--", "repo\n"])
def test_github_key_sql_rejects_invalid_columns(column):
    from argus.forge.keys import github_key_sql

    with pytest.raises(ValueError, match="^invalid sql column name$"):
        github_key_sql(column)


FILTERED = {
    "src/argus/acceptance.py": "Reports are baseline; collection and reconciliation already skip GitLab.",
    "src/argus/api/metrics.py": "Baseline rollups are filtered; watchdog functions remain forge-wide.",
    "src/argus/api/watch_metrics.py": "Coverage measures the GitHub baseline.",
    "src/argus/evalkit/labels.py": "Acceptance labels measure GitHub findings.",
    "src/argus/evalkit/monthly.py": "Monthly reports measure the GitHub baseline.",
    "src/argus/jarvis/corpus.py": "Ground truth for the GitHub baseline.",
    "scripts/export_known_fps.py": "False-positive exports feed the GitHub baseline.",
}
OPERATIONAL = {
    "src/argus/api/watcher.py": "Queue and worker health must see every forge.",
    "src/argus/api/watch_store.py": "Queue claims and worker state must see every forge.",
    "src/argus/api/db.py": "Store inserts, id lookups and console lists serve every forge.",
    "src/argus/replies.py": "Per-repository operations already skip GitLab.",
    "src/argus/cli.py": "Lookups select a specific review id.",
    "src/argus/jarvis/cli.py": "Lookups select a specific review id.",
    "src/argus/jarvis/live.py": "False-positive suppression is keyed by repository.",
}
EXTRA = ()


def test_measurement_reader_allowlist():
    pattern = re.compile(
        r"(?i)\b(FROM|JOIN|UPDATE|INTO)\s+"
        r"(published_findings|reviews|findings|pr_heads|outcome_cursors|publishing_identities|known_fps_live)\b")
    sources = [*ROOT.glob("src/**/*.py"), *ROOT.glob("scripts/*.py")]
    found = {str(path.relative_to(ROOT)): path.read_text() for path in sources
             if "tests" not in path.relative_to(ROOT).parts and pattern.search(path.read_text())}
    found.update({path: (ROOT / path).read_text() for path, _reason in EXTRA})
    assert {"src/argus/acceptance.py", "src/argus/api/metrics.py", "src/argus/api/watch_metrics.py"} <= found.keys()
    assert not FILTERED.keys() & OPERATIONAL.keys()
    assert found.keys() == FILTERED.keys() | OPERATIONAL.keys(), (
        f"Unlisted: {found.keys() - (FILTERED.keys() | OPERATIONAL.keys())}; "
        f"stale: {(FILTERED.keys() | OPERATIONAL.keys()) - found.keys()}")
    for path in FILTERED:
        assert re.search(r"\b(github_key_sql|is_github_key)\b", found[path]), path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as scratch, mock.patch.dict(os.environ):
        os.environ.pop("ARGUS_GITLAB_URL", None)
        database = Path(scratch) / "github.db"
        seed(database, gitlab=False)
        outputs = collect_outputs(database)
    args.write.parent.mkdir(parents=True, exist_ok=True)
    args.write.write_text(json.dumps(outputs, indent=2, sort_keys=True) + "\n")
