"""Keep GitLab acceptance separate from the GitHub comparison."""

import importlib.util
import json
import sys
from contextlib import closing
from pathlib import Path

import httpx
import pytest

from argus import acceptance
from argus.api import db, metrics
from argus.forge import keys
from argus.forge.types import RepoRecord

GITHUB = "owner/repo"
GITLAB = keys.gitlab_key("gitlab.example.com", 12)
STAMP = "2026-11-02T00:00:00+00:00"
METRICS = (metrics.mark_precision, metrics.verified_severity_counts,
           metrics.verified_bug_split, metrics.verified_finding_count)


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_GITLAB_URL", "https://gitlab.example.com")

    def no_http(*args, **kwargs):
        pytest.fail("Unexpected HTTP request")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_http)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_http)
    path = str(tmp_path / "report.db")
    db.init_db(path)
    return path


def insert(conn, table, **values):
    return conn.execute(
        f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
        tuple(values.values()),
    ).lastrowid


def seed(path, repo=GITLAB, **updates):
    rid = db.create_review(path, repo, updates.get("pr", 7), "test", head_sha="original")
    values = dict(review_id=rid, repo=repo, pr=7, head_sha="original", fingerprint=f"fp-{rid}",
                  severity="major", path="app.py", line=1, title="Missing check",
                  placement="inline", mode="posted", posted_at=STAMP,
                  anchor_status="anchored" if keys.is_gitlab_key(repo) else None) | updates
    with closing(db.connect(path)) as conn, conn:
        return insert(conn, "published_findings", **values)


def reply(conn, fid, *, trusted=None, association=None, body="Fixed the check.", evidence=1):
    acceptance._record(conn, fid, "reply", acceptance.classify_reply(body), evidence, STAMP,
                       STAMP, body, association, "developer", author_trusted=trusted)


def report(path, since=None):
    with closing(db.connect(path)) as conn:
        return acceptance.report(conn, since)


def test_mixed_report_preserves_github_baseline(database):
    gh = seed(database, GITHUB)
    with closing(db.connect(database)) as conn, conn:
        reply(conn, gh, association="MEMBER")
        insert(conn, "publishing_identities", repo=GITHUB, bot_login="publisher", first_seen_at=STAMP)
        insert(conn, "outcome_cursors", repo=GITHUB, since=STAMP, last_collected_at=STAMP)
        insert(conn, "bot_actions", finding_id=gh, kind="reply", value="fixed",
               evidence_id="1", created_at=STAMP)
        insert(conn, "outcome_prs", repo=GITHUB, pr=7, state="closed", checked_at=STAMP)
    before = report(database)
    before_stats = acceptance.stats(database)
    gl = seed(database)
    with closing(db.connect(database)) as conn, conn:
        reply(conn, gl, trusted=1)
        insert(conn, "publishing_identities", repo=GITLAB, bot_login="gitlab-bot", first_seen_at=STAMP)
        insert(conn, "outcome_cursors", repo=GITLAB, since=STAMP, last_collected_at="2026-11-03")
        insert(conn, "bot_actions", finding_id=gl, kind="recheck", value="fixed",
               evidence_id="later", created_at=STAMP)
    # A closed GitHub request with the same number cannot finalize a GitLab request.
    after = report(database)
    assert {k: v for k, v in after.items() if k not in {"gitlab", "note"}} == {
        k: v for k, v in before.items() if k != "note"
    }
    after_stats = acceptance.stats(database)
    assert {k: v for k, v in after_stats.items() if k not in {"gitlab", "note"}} == {
        k: v for k, v in before_stats.items() if k != "note"
    }
    assert list(after) == [*before, "gitlab"]
    assert after["gitlab"]["totals"]["posted"] == 1
    assert after["gitlab"]["totals"]["strict_accepted"] == 1
    assert after["gitlab"]["final"]["totals"]["posted"] == 0
    assert after["note"] == acceptance.NOTE + " " + acceptance.GITLAB_NOTE
    with closing(db.connect(database)) as conn, conn:
        insert(conn, "outcome_prs", repo=GITLAB, pr=7, state="closed", checked_at=STAMP)
    assert report(database)["gitlab"]["final"]["totals"]["strict_accepted"] == 1


def test_optional_section_preserves_unconfigured_output(database, monkeypatch):
    seed(database, GITHUB)
    baseline = json.dumps(report(database))
    baseline_stats = json.dumps(acceptance.stats(database))
    assert report(database)["note"] == acceptance.NOTE
    seed(database, mode="dry_run")
    assert json.dumps(report(database)) == baseline
    seed(database)
    monkeypatch.delenv("ARGUS_GITLAB_URL")
    assert json.dumps(report(database)) == baseline
    assert json.dumps(acceptance.stats(database)) == baseline_stats


@pytest.mark.parametrize("trusted,association,expected", [
    (1, None, 1), (None, None, 0), (0, None, 0), (None, "OWNER", 0), (0, "MEMBER", 0),
])
def test_gitlab_reply_uses_membership_trust(database, trusted, association, expected):
    fid = seed(database)
    with closing(db.connect(database)) as conn, conn:
        reply(conn, fid, trusted=trusted, association=association)
    totals = report(database)["gitlab"]["totals"]
    assert totals["strict_accepted"] == expected
    assert totals["unconfirmed"] == 1 - expected


def test_gitlab_first_trusted_reply_controls_acceptance(database):
    fid = seed(database)
    with closing(db.connect(database)) as conn, conn:
        reply(conn, fid, body="Rejected the finding.", evidence=1)
        reply(conn, fid, trusted=1, evidence=2)
        reply(conn, fid, trusted=1, body="Rejected later.", evidence=3)
    assert report(database)["gitlab"]["totals"]["strict_accepted"] == 1


def test_only_confirmed_inline_notes_enter_rates(database):
    seed(database, unverified=1)
    for status in ("rejected_400", "rejected_500", "downgraded"):
        seed(database, anchor_status=status, placement="summary", unverified=1)
        seed(database, anchor_status=status, mode="dry_run")
        seed(database, GITHUB, anchor_status=status, placement="summary")
    seed(database, anchor_status=None)
    seed(database, placement="summary")
    seed(database, mode="dry_run")
    with closing(db.connect(database)) as conn, conn:
        insert(conn, "outcome_prs", repo=GITLAB, pr=7, state="closed", checked_at=STAMP)
    gl = report(database)["gitlab"]
    assert {status: gl[status] for status in ("rejected_400", "rejected_500", "downgraded")} == {
        "rejected_400": 1, "rejected_500": 1, "downgraded": 1,
    }
    assert gl["totals"]["posted"] == gl["unverified"]["posted"] == 1
    assert gl["totals"]["strict_rate"] == 0
    assert gl["by_placement"]["summary"]["posted"] == 0
    assert gl["by_placement"]["inline"]["posted"] == 1
    assert sum(v["posted"] for v in gl["by_repo"].values()) == 1
    assert sum(v["posted"] for v in gl["by_severity"].values()) == 1
    assert gl["final"]["totals"] == gl["totals"]


def test_section_reports_rejections_without_anchored_findings(database):
    seed(database, anchor_status="rejected_400", placement="summary")
    gl = report(database)["gitlab"]
    assert gl["rejected_400"] == 1
    assert gl["totals"]["posted"] == 0
    assert gl["totals"]["strict_rate"] is None
    assert gl["by_repo"] == gl["by_severity"] == {}


def test_since_filters_rates_and_failure_counts_inclusively(database):
    for stamp in ("2026-11-01T23:59:59+00:00", "2026-11-02T01:00:00+01:00"):
        seed(database, posted_at=stamp)
        for status in ("rejected_400", "rejected_500", "downgraded"):
            seed(database, anchor_status=status, placement="summary", posted_at=stamp)
    gl = report(database, STAMP)["gitlab"]
    assert gl["totals"]["posted"] == 1
    assert [gl[status] for status in ("rejected_400", "rejected_500", "downgraded")] == [1, 1, 1]
    empty = report(database, "2026-11-03")["gitlab"]
    assert empty["totals"]["posted"] == empty["rejected_400"] == 0


@pytest.mark.parametrize("label", ["group/project", "", None])
def test_by_repo_uses_live_project_path_or_key(database, label):
    seed(database)
    with closing(db.connect(database)) as conn, conn:
        db.upsert_forge_project(conn, keys.parse_repo_key(GITLAB),
                                RepoRecord(path=label, archived=False, default_branch="main"))
    assert list(report(database)["gitlab"]["by_repo"]) == [label or GITLAB]


def test_duplicate_paths_fall_back_to_distinct_keys(database):
    other = keys.gitlab_key("other.example.com", 12)
    for repo in (GITLAB, other):
        seed(database, repo)
        with closing(db.connect(database)) as conn, conn:
            db.upsert_forge_project(conn, keys.parse_repo_key(repo),
                                    RepoRecord(path="group/project", archived=False, default_branch="main"))
    assert set(report(database)["gitlab"]["by_repo"]) == {GITLAB, other}


def test_report_without_reply_texts_table(database):
    fid = seed(database)
    with closing(db.connect(database)) as conn, conn:
        acceptance._record(conn, fid, "reply", "fixed", 1, STAMP)
        conn.execute("DROP TABLE reply_texts")
    assert report(database)["gitlab"]["totals"]["strict_accepted"] == 0


def test_metrics_select_each_forge(database):
    for repo, severities in ((GITHUB, ("minor",)), (GITLAB, ("major", "nit"))):
        rid = db.create_review(database, repo, 7, "test")
        with closing(db.connect(database)) as conn, conn:
            for severity in severities:
                fid = insert(conn, "findings", review_id=rid, file="app.py", line=1,
                             severity=severity, confidence=1, title="Missing check", description="Check",
                             proposed_fix="Add check", is_bug=int(severity != "nit"),
                             verdict_is_real=1, verdict_confidence=1)
                insert(conn, "marks", finding_id=fid, mark="fp" if severity == "nit" else "real",
                       marker="reviewer", marked_at=STAMP)
                insert(conn, "marks", finding_id=fid, mark="stale", marker="automark:test", marked_at=STAMP)
    for forge, count in (("github", 1), ("gitlab", 2)):
        assert metrics.verified_finding_count(database, forge=forge) == count
        assert metrics.verified_bug_split(database, forge=forge) == {"bug": 1, "nit": count - 1}
        assert metrics.mark_precision(database, forge=forge)["precision"] == 1 / count
        assert metrics.mark_precision(database, forge=forge, include_auto=True)["stale"] == count
        assert metrics.verified_severity_counts(database, forge=forge) == {
            "critical": 0, "major": count - 1, "minor": 2 - count, "nit": count - 1,
        }
    for metric in METRICS:
        assert metric(database) == metric(database, forge="github")


@pytest.mark.parametrize("metric", METRICS)
def test_metrics_reject_unknown_forge(database, metric):
    with pytest.raises(ValueError, match="forge"):
        metric(database, forge="other")


@pytest.mark.parametrize("gitlab", [False, True])
def test_script_preserves_github_json_and_separates_gitlab(database, monkeypatch, capsys, gitlab):
    seed(database, GITHUB)
    if gitlab:
        seed(database)
    expected = acceptance.stats(database)
    spec = importlib.util.spec_from_file_location(
        "acceptance_report", Path(__file__).resolve().parents[1] / "scripts/acceptance_report.py",
    )
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    monkeypatch.setattr(sys, "argv", ["acceptance_report.py", database])
    script.main()
    output = capsys.readouterr().out
    if gitlab:
        block = expected.pop("gitlab")
        assert output == (json.dumps(expected, indent=2) + "\n\n"
                          "GitLab (not comparable with the GitHub figures above)\n"
                          + json.dumps(block, indent=2) + "\n")
    else:
        assert output == json.dumps(expected, indent=2) + "\n"
