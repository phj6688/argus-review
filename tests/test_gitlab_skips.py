"""GitLab rows stay outside GitHub consumers without counting as failures."""

import logging
from contextlib import closing
from datetime import datetime, timezone
from unittest.mock import Mock

import httpx
import pytest
from conftest import stub_github_client

from argus import acceptance, publish, replies
from argus.api import db
from argus.evalkit import labels, monthly
from argus.jarvis import automark

REPO = "gitlab:gitlab.example.com#12"
HEAD = "a" * 40
STAMP = "2026-09-12T00:00:00+00:00"


@pytest.fixture(params=[None, "https://gitlab.example.com", "invalid", "   "])
def configured(request, monkeypatch, caplog):
    if request.param is None:
        monkeypatch.delenv("ARGUS_GITLAB_URL", raising=False)
    else:
        monkeypatch.setenv("ARGUS_GITLAB_URL", request.param)
    caplog.set_level(logging.INFO)
    return request.param == "https://gitlab.example.com"


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = str(tmp_path / "argus.db")
    monkeypatch.setenv("ARGUS_DB_PATH", path)
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    monkeypatch.setenv("ARGUS_PUBLISH_REPOS", REPO)
    monkeypatch.setenv("ARGUS_REPLY_LOOP", "1")
    stub_github_client(monkeypatch)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: pytest.fail("Unexpected HTTP"))
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("Unexpected HTTP"))
    db.init_db(path)
    return path


def seed(path, repo=REPO, *, posted=True):
    review_id = db.create_review(path, repo, 3, "test", head_sha=HEAD)
    with closing(db.connect(path)) as conn, conn:
        conn.execute(
            "UPDATE reviews SET status='done',publish_status=?,published_at=? WHERE id=?",
            ("posted" if posted else None, STAMP, review_id),
        )
        conn.execute(
            "INSERT INTO published_findings (review_id,repo,pr,head_sha,fingerprint,severity,"
            "path,line,end_line,title,placement,mode,gh_review_id,gh_comment_id,posted_at) "
            "VALUES (?,?,3,?,?,'major','app.py',10,12,'Missing check','inline','posted',20,101,?)",
            (review_id, repo, HEAD, f"fp-{review_id}", STAMP),
        )
        conn.execute(
            "INSERT OR IGNORE INTO publishing_identities (repo,bot_login,first_seen_at) VALUES (?,?,?)",
            (repo, "publisher", STAMP),
        )
    return review_id


def assert_counter(stats, configured, count):
    if configured:
        assert stats["skipped_gitlab"] == count
    else:
        assert "skipped_gitlab" not in stats


def assert_skip_log(caplog, consumer, count):
    records = [r for r in caplog.records if r.getMessage() == f"{consumer}: skipped_gitlab={count}"]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


@pytest.mark.parametrize("count", [0, 1])
def test_acceptance_skips_before_validation_and_writes(database, configured, count, monkeypatch, caplog):
    if count:
        seed(database)
    validate = Mock(side_effect=AssertionError("must not validate GitLab"))
    monkeypatch.setattr(acceptance, "validate_repo", validate)

    acceptance.collect(database)

    run = acceptance.stats(database)["last_run"]
    assert run["failures"] == 0
    assert run["repos"] == 0
    assert run["prs_checked"] == 0
    assert run["finished_at"] is not None
    assert_counter(run, configured, count)
    validate.assert_not_called()
    with closing(db.connect(database)) as conn:
        for table in ("outcome_cursors", "outcome_prs", "finding_outcomes", "bot_actions"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    if count:
        assert_skip_log(caplog, "outcomes", 1)


def test_replies_skip_before_gate_or_database(database, configured, monkeypatch, caplog):
    seed(database)
    enabled = Mock(side_effect=AssertionError("must skip before the gate"))
    monkeypatch.setattr(replies, "enabled_for", enabled)
    counters = {}
    with closing(db.connect(database)) as conn:
        findings = [dict(row) for row in conn.execute("SELECT * FROM published_findings")]
        statements = []
        conn.set_trace_callback(statements.append)
        replies.run(conn, REPO, findings, HEAD, {}, datetime.now(timezone.utc), counters)
    assert statements == []
    enabled.assert_not_called()
    assert_counter(counters, configured, 1)
    assert_skip_log(caplog, "replies", 1)


def test_replies_gate_rejects_gitlab_before_publish_gate(monkeypatch):
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    enabled = Mock(side_effect=AssertionError("must skip before publishing gate"))
    monkeypatch.setattr(publish, "enabled_for", enabled)
    assert replies.enabled_for(REPO) is False
    enabled.assert_not_called()


def test_replies_empty_run_has_stable_counter(database, configured):
    counters = {}
    with closing(db.connect(database)) as conn:
        replies.run(conn, "owner/repo", [], HEAD, {}, datetime.now(timezone.utc), counters)
    assert_counter(counters, configured, 0)


@pytest.mark.parametrize("filters", [
    {"repo": None, "pr": None, "since": None},
    {"repo": REPO, "pr": 3, "since": 1},
])
def test_automark_selection_drops_gitlab_before_filters(configured, filters, caplog):
    summaries = [{"id": 1, "repo": REPO, "pr": 3, "age": "bad"},
                 {"id": 2, "repo": "owner/repo", "pr": 3, "age": "1h"}]
    selected = automark._select_reviews(summaries, **filters)
    assert selected == ([summaries[1]] if filters["repo"] is None else [])


def test_automark_api_never_fetches_gitlab_details(monkeypatch, configured, caplog):
    paths = []

    def respond(request):
        paths.append(request.url.path)
        if request.url.path == "/reviews":
            return httpx.Response(200, json={"reviews": [{"id": 1, "repo": REPO}]})
        pytest.fail("must not fetch GitLab review details")

    client = httpx.Client(base_url="http://argus.test", transport=httpx.MockTransport(respond))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client)
    assert automark.list_findings_via_api() == []
    assert paths == ["/reviews"]
    assert_skip_log(caplog, "automark", 1)


def test_automark_run_counts_gitlab_dropped_by_the_api_selection(monkeypatch, configured, caplog):
    def respond(request):
        if request.url.path == "/reviews":
            return httpx.Response(200, json={"reviews": [{"id": 1, "repo": REPO}]})
        pytest.fail("must not fetch GitLab review details")

    client = httpx.Client(base_url="http://argus.test", transport=httpx.MockTransport(respond))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client)
    report = automark.run()
    assert report.scanned == 0
    assert_counter(report.stats, configured, 1)
    assert_skip_log(caplog, "automark", 1)


@pytest.mark.parametrize("has_mark", [False, True, None])
def test_automark_run_skips_without_processing(configured, has_mark, caplog):
    findings = [] if has_mark is None else [automark.Finding(
        1, REPO, 3, "app.py", 10, "major", "Missing check", "Unchecked input", has_mark,
    )]
    dependencies = {name: Mock(side_effect=AssertionError(f"unexpected {name}"))
                    for name in ("fetch_file", "head_sha_for", "judge", "write_mark")}
    report = automark.run(list_findings=lambda **kwargs: findings, commit=True, **dependencies)
    assert report.scanned == 0
    assert report.skipped == []
    assert report.marked == report.would_mark == report.needs_human == []
    assert "failed" not in automark.render_report(report).lower()
    assert_counter(report.stats, configured, len(findings))
    for dependency in dependencies.values():
        dependency.assert_not_called()
    if findings:
        assert_skip_log(caplog, "automark", 1)


@pytest.mark.parametrize("count", [0, 1])
def test_labelled_findings_skip_before_labelling(database, configured, count, monkeypatch, caplog):
    if count:
        seed(database)
    signals = Mock(side_effect=AssertionError("must not label GitLab"))
    monkeypatch.setattr(labels, "_signals", signals)
    stats = {}
    with closing(db.connect(database)) as conn:
        assert list(labels.labelled_findings(conn, stats=stats)) == []
    signals.assert_not_called()
    assert_counter(stats, configured, count)
    if count:
        assert_skip_log(caplog, "labels", 1)


@pytest.mark.parametrize("count,posted", [(0, False), (1, False), (1, True)])
def test_monthly_skips_findings_and_posted_reviews(database, configured, count, posted, caplog):
    seed(database, "owner/repo")
    if count:
        seed(database, posted=posted)
    report = monthly.build(database, {}, "2026-09")
    assert report["rows"]["strict_acceptance"]["current"]["n"] == 1
    assert report["rows"]["inline_items"]["current"]["value"] == 1
    assert report["rows"]["inline_items"]["per_posted_review"]["value"] == 1
    assert_counter(report, configured, count + int(posted))
    assert "skipped_gitlab" not in monthly.markdown(report)
    if count:
        assert_skip_log(caplog, "monthly", count + int(posted))


def test_skip_helpers_follow_parsed_configuration(monkeypatch):
    from argus.forge import skips
    from argus.gitlab import config

    monkeypatch.delenv("ARGUS_GITLAB_URL", raising=False)
    monkeypatch.setattr(config, "base_url", lambda: object())
    assert skips.gitlab_configured() is True
    stats = {"skipped_gitlab": 2}
    skips.count_skip(stats, 3)
    skips.count_skip(stats, 0)
    assert stats == {"skipped_gitlab": 5}
    skips.count_skip(None)
    skips.count_skip([])

    monkeypatch.setattr(config, "base_url", Mock(side_effect=config.GitLabConfigError("invalid")))
    assert skips.gitlab_configured() is False
    skips.count_skip(stats)
    assert stats == {"skipped_gitlab": 5}
