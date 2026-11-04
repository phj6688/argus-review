"""Collect GitLab outcomes from local responses without contacting a forge."""

import copy
import json
import logging
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from argus import acceptance, github, replies
from argus.api import db
from argus.forge.keys import gitlab_key
from argus.gitlab import client as client_module, projects

FIXTURES = Path(__file__).parent / "fixtures/gitlab/spike_b"
STAMP = "2026-11-02T22:26:52.650Z"


def rows(state, table):
    with closing(db.connect(state.path)) as conn:
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]


def seed(state, *, repo=None, pr=7, **updates):
    repo = repo or state.key
    rid = db.create_review(state.path, repo, pr, "test", head_sha=state.head)
    data = dict(review_id=rid, repo=repo, pr=pr, head_sha=state.head,
                fingerprint="finding", severity="major", path="app.py", line=1, end_line=1,
                title="Missing check", placement="inline", mode="posted", unverified=0,
                gh_thread_id=state.discussion["id"], gh_comment_id=241, posted_at=STAMP) | updates
    with closing(db.connect(state.path)) as conn, conn:
        cursor = conn.execute(
            f"INSERT INTO published_findings ({','.join(data)}) VALUES ({','.join('?' for _ in data)})",
            tuple(data.values()),
        )
        conn.execute(
            "INSERT OR IGNORE INTO publishing_identities (repo,bot_login,first_seen_at,bot_user_id) "
            "VALUES (?,?,?,?)", (repo, "argus-bot", STAMP, 2),
        )
        return cursor.lastrowid


def collect(state):
    acceptance.collect(state.path)
    return acceptance.stats(state.path)["last_run"]


@pytest.fixture
def state(gitlab_event, monkeypatch):
    state = gitlab_event
    state.discussions = json.loads((FIXTURES / "b13_changed_push.json").read_text())["response"]["body"]
    state.discussion = state.discussions[0]
    state.discussion["notes"][0]["body"] = "Finding\n\n<!-- argus:fp=finding -->"
    state.discussion["notes"][1].update(
        body="Fixed the missing check.", author={"id": 4, "username": "developer"},
    )
    state.responses[state.mr + "/discussions"] = state.discussions
    state.member = state.project + "/members/all/4"
    state.responses[state.member] = {"id": 4, "access_level": 30}
    monkeypatch.setenv("ARGUS_REPLY_LOOP", "0")
    return state


@pytest.mark.parametrize("status,level,trusted", [(200, 30, 1), (200, 10, 0), (404, None, 0), (500, None, None)])
def test_human_reply_records_membership_trust(state, status, level, trusted):
    fid = seed(state)
    state.responses[state.member] = httpx.Response(status, json={"access_level": level})
    run = collect(state)
    assert rows(state, "reply_texts") == [{
        "finding_id": fid, "evidence_id": "243", "body": "Fixed the missing check.",
        "author_association": None, "author_login": "developer", "author_trusted": trusted,
        "answered_head": None,
    }]
    reply = next(row for row in rows(state, "finding_outcomes") if row["kind"] == "reply")
    assert (reply["finding_id"], reply["value"], reply["occurred_at"]) == (fid, "fixed", STAMP)
    assert (run["repos"], run["prs_checked"], run["failures"]) == (1, 1, 0)
    assert [r.url.path for r in state.requests[:3]] == [state.project, state.mr, state.mr + "/discussions"]


def test_unknown_trust_preserves_known_value_and_cache_is_per_run(state):
    seed(state)
    seed(state, pr=8, fingerprint="second")
    state.responses[state.project + "/merge_requests/8"] = state.responses[state.mr]
    state.responses[state.project + "/merge_requests/8/discussions"] = state.discussions
    collect(state)
    assert [r.url.path for r in state.requests].count(state.member) == 1
    state.requests.clear()
    state.responses[state.member] = httpx.Response(500)
    collect(state)
    assert [row["author_trusted"] for row in rows(state, "reply_texts")] == [1, 1]
    assert [r.url.path for r in state.requests].count(state.member) == 2  # The client retries a 500 once.


def test_bot_identity_survives_rename_and_human_marker_replies_are_ignored(state):
    seed(state)
    state.discussion["notes"][0]["author"]["username"] = "renamed-bot"
    state.discussion["notes"][2]["author"]["username"] = "renamed-bot"
    state.discussion["notes"][2]["body"] = "Fixed by the bot"
    state.discussion["notes"][1]["author"]["username"] = "argus-bot"
    marked = copy.deepcopy(state.discussion["notes"][1])
    marked.update(id=999, body="Fixed\n\n[//]: # (argus:fp=some-other-finding)")
    state.discussion["notes"].append(marked)
    collect(state)
    assert [(row["evidence_id"], row["author_login"]) for row in rows(state, "reply_texts")] == [
        ("243", "argus-bot"),
    ]


def test_resolution_flips_when_human_unresolves_thread(state):
    seed(state)
    for note in state.discussion["notes"]:
        if note["resolvable"]:
            note.update(resolved=True, resolved_by={"id": 4})
    collect(state)
    resolved = [row for row in rows(state, "finding_outcomes") if row["kind"] == "resolved"]
    assert [(row["evidence_id"], row["value"]) for row in resolved] == [("241", "true")]
    state.discussion["notes"][0]["resolved"] = False
    collect(state)
    resolved = [row for row in rows(state, "finding_outcomes") if row["kind"] == "resolved"]
    assert [(row["evidence_id"], row["value"]) for row in resolved] == [("241", "false")]


@pytest.mark.parametrize("mr_state,expected_state,merged", [("opened", "open", 0), ("merged", "closed", 1)])
def test_mr_metadata_and_default_cursor_are_stored(state, mr_state, expected_state, merged):
    seed(state)
    closed_at = datetime.now(timezone.utc).isoformat() if merged else None
    state.responses[state.mr].update(state=mr_state, closed_at=None, merged_at=closed_at)
    run = collect(state)
    assert rows(state, "outcome_prs") == [{
        "repo": state.key, "pr": 7, "state": expected_state, "merged": merged,
        "closed_at": closed_at, "head_sha": state.head, "checked_at": run["started_at"],
    }]
    assert rows(state, "outcome_cursors") == [{
        "repo": state.key,
        "since": (datetime.fromisoformat(run["started_at"]) - timedelta(days=30)).isoformat(),
        "last_collected_at": run["started_at"],
    }]
    assert "gitlab_paused" not in run
    assert "gitlab_threads_missing" not in run


def test_discussion_pages_do_not_hold_a_write_transaction(state):
    seed(state)

    def discussions(request):
        with closing(sqlite3.connect(state.path, timeout=0)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            assert conn.execute(
                "SELECT state, head_sha FROM outcome_prs WHERE repo=? AND pr=?", (state.key, 7),
            ).fetchone() is None
        if request.url.params.get("page", "1") == "1":
            return httpx.Response(200, json=state.discussions, headers={"X-Next-Page": "2"})
        return httpx.Response(200, json=[])

    state.responses[state.mr + "/discussions"] = discussions
    run = collect(state)
    assert run["failures"] == 0
    assert len([r for r in state.requests if r.url.path == state.mr + "/discussions"]) == 2
    assert {row["kind"] for row in rows(state, "finding_outcomes")} == {"reply", "resolved"}


@pytest.mark.parametrize("status,trusted,requests", [(200, 1, 2), (404, 0, 2), (500, None, 4)])
def test_membership_requests_do_not_hold_a_write_transaction(state, status, trusted, requests):
    seed(state, gh_thread_id=None)
    second_reply = copy.deepcopy(state.discussion["notes"][1])
    second_reply.update(id=999, author={"id": 5, "username": "maintainer"})
    state.discussion["notes"].append(second_reply)

    def member(request):
        with closing(sqlite3.connect(state.path, timeout=0)) as conn, conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError:
                pytest.fail("Membership requested while a write transaction is open")
            if conn.execute("SELECT 1 FROM outcome_prs").fetchone() is not None:
                pytest.fail("MR metadata written before membership was collected")
        return httpx.Response(status, json={"access_level": 30})

    state.responses[state.member] = member
    state.responses[state.project + "/members/all/5"] = member
    run = collect(state)
    assert run["failures"] == 0
    assert len([r for r in state.requests if "/members/all/" in r.url.path]) == requests
    assert [(row["evidence_id"], row["author_trusted"]) for row in rows(state, "reply_texts")] == [
        ("243", trusted), ("999", trusted),
    ]
    assert len(rows(state, "outcome_prs")) == 1


def test_old_closed_mr_is_retried_after_discussion_failure(state):
    fid = seed(state)
    closed_at = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    state.responses[state.mr].update(state="closed", closed_at=closed_at)
    state.responses[state.mr + "/discussions"] = httpx.Response(500)
    run = collect(state)
    assert (run["failures"], run["prs_checked"]) == (1, 1)
    for table in ("outcome_prs", "finding_outcomes", "reply_texts"):
        assert rows(state, table) == []

    state.responses[state.mr + "/discussions"] = state.discussions
    state.requests.clear()
    run = collect(state)
    assert (run["failures"], run["prs_checked"], run["prs_skipped_final"]) == (0, 1, 0)
    assert state.mr in [r.url.path for r in state.requests]
    assert [(row["finding_id"], row["evidence_id"]) for row in rows(state, "reply_texts")] == [(fid, "243")]
    pull, = rows(state, "outcome_prs")
    assert (pull["pr"], pull["state"], pull["closed_at"]) == (7, "closed", closed_at)


@pytest.mark.parametrize("comment_id,author_id,expected_id,updates", [
    (None, 2, 241, 1),
    (900, 2, 900, 0),
    (None, 4, None, 0),
])
def test_existing_thread_recovers_missing_comment_id_only_once(
    state, monkeypatch, comment_id, author_id, expected_id, updates,
):
    seed(state, gh_comment_id=comment_id)
    state.discussion["notes"][0]["author"]["id"] = author_id
    state.discussion["notes"][0]["body"] = "Finding without a marker"
    statements = []
    original = db.connect

    def traced(*args, **kwargs):
        conn = original(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(db, "connect", traced)
    run = collect(state)
    assert run["failures"] == 0
    finding, = rows(state, "published_findings")
    assert (finding["gh_thread_id"], finding["gh_comment_id"]) == (state.discussion["id"], expected_id)
    assert len([sql for sql in statements if sql.startswith("UPDATE published_findings")]) == updates

    statements.clear()
    run = collect(state)
    assert run["failures"] == 0
    finding, = rows(state, "published_findings")
    assert (finding["gh_thread_id"], finding["gh_comment_id"]) == (state.discussion["id"], expected_id)
    assert not any(sql.startswith("UPDATE published_findings") for sql in statements)


@pytest.mark.parametrize("unverified", [0, 1])
@pytest.mark.parametrize("comment_id", [None, 900])
def test_marker_recovers_thread_and_comment_ids_only_once(state, monkeypatch, unverified, comment_id):
    seed(state, gh_thread_id=None, gh_comment_id=comment_id, unverified=unverified)
    kind = "ufp" if unverified else "fp"
    state.discussion["notes"][0]["body"] = f"Finding\n\n<!-- argus:{kind}=finding -->"
    statements = []
    original = db.connect

    def traced(*args, **kwargs):
        conn = original(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(db, "connect", traced)
    collect(state)
    finding, = rows(state, "published_findings")
    assert (finding["gh_thread_id"], finding["gh_comment_id"]) == (
        state.discussion["id"], comment_id or 241,
    )
    assert len([sql for sql in statements if sql.startswith("UPDATE published_findings")]) == 1
    statements.clear()
    collect(state)
    assert not any(sql.startswith("UPDATE published_findings") for sql in statements)
    assert len(rows(state, "reply_texts")) == 1


@pytest.mark.parametrize("case", ["human_root", "human_marker", "wrong_kind", "wrong_fingerprint", "summary", "absent_marker"])
def test_unmapped_findings_and_non_bot_roots_record_nothing(state, case):
    updates = {"gh_thread_id": None}
    if case == "human_root":
        updates = {}
        state.discussion["notes"][0]["author"] = {"id": 4, "username": "argus-bot"}
    elif case == "human_marker":
        state.discussion["notes"][0]["author"] = {"id": 4, "username": "argus-bot"}
    elif case == "wrong_kind":
        updates["unverified"] = 1
    elif case == "wrong_fingerprint":
        updates["fingerprint"] = "other"
    elif case == "summary":
        updates = {"placement": "summary"}
    else:
        state.discussion["notes"][0]["body"] = "No marker"
    seed(state, **updates)
    collect(state)
    assert rows(state, "finding_outcomes") == []
    assert rows(state, "reply_texts") == []


def test_missing_discussion_counts_without_outcomes_or_body_logs(state, caplog):
    seed(state)
    state.responses[state.mr + "/discussions"] = []
    caplog.set_level(logging.INFO)
    run = collect(state)
    assert run["gitlab_threads_missing"] == 1
    assert rows(state, "finding_outcomes") == []
    assert any("missing" in r.getMessage() and r.levelno == logging.INFO for r in caplog.records)
    assert "Fixed the missing check" not in caplog.text


def test_denied_project_makes_no_mr_or_thread_request(state, caplog):
    seed(state)
    db.save_config(state.path, repos=[], model="test", bug_cap=5, min_confidence=0.5, deny=[state.key])
    caplog.set_level(logging.INFO)
    run = collect(state)
    assert (run["repos"], run["prs_checked"], run["skipped_gitlab"]) == (0, 0, 1)
    assert [r.url.path for r in state.requests] == [state.project]
    assert "outcomes: gitlab project denied:" in caplog.text
    for table in ("outcome_prs", "finding_outcomes", "reply_texts", "outcome_cursors"):
        assert rows(state, table) == []


def github_finding(state, monkeypatch):
    fid = seed(state, repo="owner/repo", pr=3, gh_thread_id="github-thread", gh_comment_id=101)
    monkeypatch.setattr(github, "list_cached", lambda *a, **k: [{
        "id": 102, "in_reply_to_id": 101, "body": "Fixed", "user": {"login": "owner"},
        "author_association": "OWNER", "created_at": STAMP, "updated_at": STAMP,
    }])
    monkeypatch.setattr(github, "get_json_cached", lambda *a, **k: ({
        "state": "open", "head": {"sha": state.head}, "user": {"login": "owner"},
    }, False))
    monkeypatch.setattr(github, "graphql_read", lambda *a, **k: {
        "repository": {"pullRequest": {"reviewThreads": {"nodes": []}}},
    })
    return fid


@pytest.mark.parametrize("route", ["project", "mr", "discussions"])
@pytest.mark.parametrize("failure", [500, 401, 403, "thrown"])
def test_gitlab_failure_does_not_stop_github(state, monkeypatch, route, failure):
    seed(state)
    fid = github_finding(state, monkeypatch)
    endpoint = {"project": state.project, "mr": state.mr, "discussions": state.mr + "/discussions"}[route]

    def thrown(request):
        raise RuntimeError("private failure text")

    state.responses[endpoint] = thrown if failure == "thrown" else httpx.Response(failure)
    run = collect(state)
    assert run["failures"] == 1
    assert "gitlab_paused" not in run
    assert run["stopped_by_rate_limit"] is False
    assert next(row for row in rows(state, "reply_texts") if row["finding_id"] == fid)["author_trusted"] is None
    assert any(row["repo"] == "owner/repo" for row in rows(state, "outcome_prs"))
    assert not any(row["repo"] == state.key for row in rows(state, "outcome_prs"))


@pytest.mark.parametrize("route", ["project", "mr", "discussions", "member"])
@pytest.mark.parametrize("retry_after", ["60", "0"])
def test_gitlab_rate_limit_pauses_only_gitlab(state, monkeypatch, caplog, route, retry_after):
    seed(state)
    seed(state, repo=gitlab_key(state.host, 99))
    fid = github_finding(state, monkeypatch)
    endpoint = {"project": state.project, "mr": state.mr,
                "discussions": state.mr + "/discussions", "member": state.member}[route]
    state.responses[endpoint] = httpx.Response(429, headers={"Retry-After": retry_after})
    run = collect(state)
    assert run["gitlab_paused"] is True
    assert run["stopped_by_rate_limit"] is False
    assert run["failures"] == 0
    assert ("argus.acceptance", logging.WARNING, f"outcomes: gitlab paused repo={state.key}") in caplog.record_tuples
    assert not any("/projects/99" in r.url.path for r in state.requests)
    assert any(row["finding_id"] == fid for row in rows(state, "reply_texts"))
    assert not any(row["repo"] == state.key for row in rows(state, "outcome_prs"))


@pytest.mark.parametrize("failure_pr", [7, 8])
def test_mr_failure_preserves_other_mrs_and_rolls_back_partial_writes(state, monkeypatch, failure_pr):
    first = seed(state)
    second = seed(state, pr=8)
    state.responses[state.project + "/merge_requests/8"] = state.responses[state.mr]
    state.responses[state.project + "/merge_requests/8/discussions"] = state.discussions
    original = acceptance._record
    failed_id = first if failure_pr == 7 else second

    def fail_resolution(conn, finding_id, kind, *args, **kwargs):
        if finding_id == failed_id and kind == "resolved":
            raise sqlite3.OperationalError("injected write failure")
        return original(conn, finding_id, kind, *args, **kwargs)

    monkeypatch.setattr(acceptance, "_record", fail_resolution)
    run = collect(state)
    assert (run["failures"], run["prs_checked"]) == (1, 2)
    assert {row["finding_id"] for row in rows(state, "finding_outcomes")} == ({first, second} - {failed_id})
    assert {row["finding_id"] for row in rows(state, "reply_texts")} == ({first, second} - {failed_id})
    assert [row["pr"] for row in rows(state, "outcome_prs")] == [8 if failure_pr == 7 else 7]


@pytest.mark.parametrize("open_head", [False, True])
def test_final_mr_skipped_unless_a_head_is_open(state, open_head):
    seed(state)
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    with closing(db.connect(state.path)) as conn, conn:
        conn.execute("INSERT INTO outcome_prs VALUES (?,?,?,?,?,?,?)",
                     (state.key, 7, "closed", 1, old, state.head, old))
        if open_head:
            conn.execute("INSERT INTO pr_heads(repo,pr,head_sha,first_seen_at,status,is_open) VALUES (?,?,?,?,?,1)",
                         (state.key, 7, state.head, STAMP, "done"))
    run = collect(state)
    assert run["prs_skipped_final"] == int(not open_head)
    assert run["prs_checked"] == int(open_head)
    if not open_head:
        assert [r.url.path for r in state.requests] == [state.project]
        assert rows(state, "finding_outcomes") == []


@pytest.mark.parametrize("recent,resolvable", [(False, True), (True, False), (True, True)])
def test_resolution_requires_recent_mr_and_resolvable_thread(state, recent, resolvable):
    seed(state)
    closed_at = (datetime.now(timezone.utc) - timedelta(days=1 if recent else 8)).isoformat()
    state.responses[state.mr].update(state="closed", closed_at=closed_at)
    if not resolvable:
        for note in state.discussion["notes"]:
            note["resolvable"] = False
    collect(state)
    outcomes = rows(state, "finding_outcomes")
    assert sum(row["kind"] == "resolved" for row in outcomes) == int(recent and resolvable)
    assert sum(row["kind"] == "reply" for row in outcomes) == 1


def test_disabled_gitlab_reply_loop_does_not_compare_write_or_sync_rejections(state, monkeypatch):
    seed(state)

    def unexpected(*args, **kwargs):
        pytest.fail("Unexpected GitHub outcome processing for GitLab")

    monkeypatch.setattr(acceptance, "_sync_rejections", unexpected)
    monkeypatch.setattr(replies, "reply", unexpected)
    monkeypatch.setattr(acceptance, "_comparisons", unexpected)
    monkeypatch.setattr(github, "get", unexpected)
    collect(state)
    assert {row["kind"] for row in rows(state, "finding_outcomes")} == {"reply", "resolved"}
    assert rows(state, "bot_actions") == []


def test_github_rate_limit_is_not_swallowed_by_gitlab_boundary(state, monkeypatch):
    seed(state)
    monkeypatch.setattr(projects, "live_deny", lambda *a, **k: (_ for _ in ()).throw(
        github.GitHubRateLimited(time.time() + 60),
    ))
    with pytest.raises(github.GitHubRateLimited):
        collect(state)


def test_client_initialization_error_does_not_stop_github(state, monkeypatch):
    seed(state)
    fid = github_finding(state, monkeypatch)

    def unavailable():
        raise client_module.GitLabAuthError("No token")

    monkeypatch.setattr(client_module, "client", unavailable)
    run = collect(state)
    assert run["failures"] == 1
    assert any(row["finding_id"] == fid for row in rows(state, "reply_texts"))


def test_membership_rate_limit_with_unchanged_reset_still_pauses(state, monkeypatch):
    seed(state)
    seed(state, repo=gitlab_key(state.host, 99))
    fid = github_finding(state, monkeypatch)
    client_module.client()._pause = 1001.0
    monkeypatch.setattr(client_module.time, "time", lambda: 1001.25)
    state.responses[state.member] = httpx.Response(429, headers={"RateLimit-Reset": "1001"})
    run = collect(state)
    assert run["gitlab_paused"] is True
    assert run["failures"] == 0
    assert not any("/projects/99" in r.url.path for r in state.requests)
    assert [row["finding_id"] for row in rows(state, "reply_texts")] == [fid]
