"""Exercise collection and reply gates with local HTTP responses."""

import copy
import json
from contextlib import closing

import httpx
import pytest

from argus import client as model_client, replies
from argus.api import db
from argus.forge.keys import gitlab_key
from argus.gitlab import client as client_module, projects, publisher, threads
from argus.gitlab.pacing import NotePacer
from test_acceptance_gitlab_collect import STAMP, collect, github_finding, rows, seed
import test_acceptance_gitlab_collect as collection_tests

from test_gitlab_pacing import FakeClock

collected_state = collection_tests.state


@pytest.fixture
def state(collected_state, monkeypatch):
    state = collected_state
    state.old = "b" * 40
    state.discussions[:] = [state.discussion]
    state.discussion["notes"][:] = state.discussion["notes"][:2]
    state.discussion["notes"][0]["resolved"] = False
    state.discussion["notes"][0]["position"]["head_sha"] = state.old
    state.responses[state.prefix + "/user"] = {"id": 2, "username": "argus-bot"}
    state.responses[state.project + "/repository/merge_base"] = {"id": state.old}
    state.responses[state.project + "/repository/compare"] = {
        "commit": {"id": state.head}, "commits": [{"id": state.head}], "compare_timeout": False,
        "compare_same_ref": False, "diffs": [{"old_path": "app.py", "new_path": "app.py",
            "new_file": False, "deleted_file": False, "renamed_file": False,
            "a_mode": "100644", "b_mode": "100644", "diff": "@@ -1 +1 @@\n-unsafe(value)\n+safe(value)\n"}],
    }
    state.post_status = 201
    state.resolve_status = 200
    state.pending = None
    clock = FakeClock()
    state.clock = clock
    monkeypatch.setattr("time.time", lambda: 1_800_000_000.0 + clock.now)

    def handle(request):
        state.requests.append(request)
        if request.method == "POST":
            assert request.url.path.endswith(f"/discussions/{state.discussion['id']}/notes")
            note = {"id": 900 + len(state.requests), "author": {"id": 2, "username": "argus-bot"},
                    "body": json.loads(request.content)["body"], "created_at": STAMP,
                    "system": False, "resolvable": False}
            state.pending = note
            if state.post_status == 201:
                state.discussion["notes"].append(note)
            return httpx.Response(state.post_status, json=note, headers={"Retry-After": "60"})
        if request.method == "PUT":
            assert json.loads(request.content) == {"resolved": True}
            if state.resolve_status == 200:
                for note in state.discussion["notes"]:
                    if note.get("resolvable"):
                        note["resolved"] = True
            return httpx.Response(state.resolve_status, json=state.discussion)
        assert request.method == "GET"
        assert request.url.path in state.responses, request.url.path
        value = state.responses[request.url.path]
        if callable(value):
            return value(request)
        return value if isinstance(value, httpx.Response) else httpx.Response(200, json=value)

    transport = client_module.GitLabClient(transport=httpx.MockTransport(handle))
    transport.pacing = NotePacer(30, clock=clock, sleep=clock.sleep)
    monkeypatch.setattr(client_module, "client", lambda: transport)
    monkeypatch.setattr(projects, "shared_client", lambda: transport)
    monkeypatch.setattr(publisher, "_identities", {})
    monkeypatch.setattr(threads, "COUNTERS", threads.Counter())
    monkeypatch.setenv("ARGUS_REPLY_LOOP", "1")
    monkeypatch.setenv("ARGUS_GITLAB_REPLY_LOOP", "1")
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    monkeypatch.setenv("ARGUS_GITLAB_PUBLISH", "1")
    monkeypatch.setenv("ARGUS_GITLAB_PUBLISH_PROJECTS", state.key)
    monkeypatch.setattr(replies, "recheck", lambda *a, **k: {"verdict": "fixed", "quote": "safe(value)"})
    monkeypatch.setattr(model_client, "complete_tool", lambda *a, **k: pytest.fail("Unexpected model call"))
    yield state
    transport.close()


def direct_run(state):
    run = {"bot": replies.counts(), "failures": 0, "rechecks_attempted": 0,
           "writes_attempted": 0, "compares_not_ancestor": 0}
    with closing(db.connect(state.path)) as conn:
        findings = [dict(row) for row in conn.execute("SELECT * FROM published_findings")]
        replies.run(conn, state.key, findings, state.head, {}, STAMP, run)
    return run


def writes(state):
    return [r for r in state.requests if r.method in {"POST", "PUT"}]


@pytest.mark.parametrize("gate,value", [("ARGUS_GITLAB_REPLY_LOOP", None),
    ("ARGUS_GITLAB_PUBLISH", "dry-run"), ("ARGUS_PUBLISH", "dry-run"),
    ("ARGUS_REPLY_LOOP", "0"), ("ARGUS_GITLAB_PUBLISH_PROJECTS", ""),
    ("ARGUS_GITLAB_REPLY_LOOP", "bad"), ("ARGUS_GITLAB_PUBLISH", "bad")])
def test_closed_reply_gate_sends_zero_requests(state, monkeypatch, gate, value):
    seed(state, head_sha=state.old)
    if value is None:
        monkeypatch.delenv(gate)
    else:
        monkeypatch.setenv(gate, value)
    direct_run(state)
    assert state.requests == []
    assert replies.reply(state.key, 7, 241, "unused") is None
    assert replies.resolve_thread(state.key, state.discussion["id"]) is None
    assert state.requests == []


def test_trusted_fixed_reply_posts_resolves_and_rerun_deduplicates(state):
    seed(state, head_sha=state.old)
    run = collect(state)
    assert run["failures"] == 0
    assert [r.method for r in writes(state)] == ["POST", "PUT"]
    actions = rows(state, "bot_actions")
    assert {a["kind"] for a in actions} == {"recheck", "reply", "resolve"}
    assert next(a for a in actions if a["kind"] == "reply")["gh_comment_id"] == state.pending["id"]
    state.requests.clear()
    assert collect(state)["failures"] == 0
    assert writes(state) == []
    assert len(rows(state, "reply_texts")) == 1


@pytest.mark.parametrize("status", [502, 429])
def test_confirmed_reply_survives_resolve_error_and_stale_discussion(state, status):
    seed(state, head_sha=state.old)
    state.resolve_status = status
    first = collect(state)
    assert first["failures"] == (1 if status == 502 else 0)
    assert first.get("gitlab_paused", False) is (status == 429)
    assert [r.method for r in writes(state)] == ["POST", "PUT"]
    posted = [a for a in rows(state, "bot_actions") if a["kind"] == "reply"]
    assert [(a["gh_comment_id"], a["head_sha"], a["verdict"]) for a in posted] == [
        (state.pending["id"], state.head, "fixed"),
    ]
    assert not any(a["kind"] == "resolve" for a in rows(state, "bot_actions"))
    state.discussion["notes"].remove(state.pending)
    state.resolve_status = 200
    state.clock.sleep(61)
    state.requests.clear()
    assert collect(state)["failures"] == 0
    assert [r.method for r in writes(state)] == ["PUT"]
    assert [a for a in rows(state, "bot_actions") if a["kind"] == "reply"] == posted


@pytest.mark.parametrize("resolve_status", [200, 403])
def test_previous_fixed_request_allows_new_recheck_at_new_head(state, monkeypatch, resolve_status):
    seed(state, head_sha=state.old)
    state.discussion["notes"][1]["body"] = "@argus-bot recheck please"
    state.resolve_status = resolve_status
    assert collect(state)["failures"] == 0
    assert [r.method for r in writes(state)] == ["POST", "PUT"]
    assert state.discussion["notes"][0]["resolved"] is (resolve_status == 200)
    state.discussion["notes"][0]["resolved"] = False
    state.head = "c" * 40
    state.responses[state.mr]["sha"] = state.head
    request = copy.deepcopy(state.discussion["notes"][1])
    request.update(id=999, body="@argus-bot recheck this new commit please")
    state.discussion["notes"].append(request)
    monkeypatch.setattr(replies, "recheck", lambda *a: {"verdict": "present", "quote": "unsafe(value)"})
    state.requests.clear()
    run = collect(state)
    assert run["failures"] == 0
    assert run["bot"]["rechecks"]["present"] == 1
    assert len([r for r in writes(state) if r.method == "POST"]) == 1
    assert "Still present at ccccccc" in state.pending["body"]
    answered = next(a for a in rows(state, "bot_actions")
                    if a["kind"] == "reply" and a["evidence_id"] == "999")
    assert (answered["head_sha"], answered["verdict"]) == (state.head, "present")
    assert next(r for r in rows(state, "reply_texts") if r["evidence_id"] == "999")["answered_head"] == state.head


@pytest.mark.parametrize("resolve_status", [200, 403])
def test_fixed_answer_covers_later_requests_at_same_head(state, resolve_status):
    seed(state, head_sha=state.old)
    state.discussion["notes"][1]["body"] = "@argus-bot recheck please"
    request = copy.deepcopy(state.discussion["notes"][1])
    request.update(id=999, body="@argus-bot please check again")
    state.discussion["notes"].append(request)
    state.resolve_status = resolve_status
    run = collect(state)
    assert run["failures"] == 0
    assert [r.method for r in writes(state)] == ["POST", "PUT"]
    assert run["bot"]["rechecks"]["fixed"] == 1
    assert {r["evidence_id"]: r["answered_head"] for r in rows(state, "reply_texts")} == {
        "243": state.head, "999": state.head,
    }


@pytest.mark.parametrize("trusted", [0, None])
def test_untrusted_and_unknown_requests_cannot_trigger_writes(state, trusted):
    seed(state)
    state.discussion["notes"][1]["body"] = "@argus-bot recheck, fixed now"
    state.responses[state.member] = (httpx.Response(500) if trusted is None else {"access_level": 10})
    assert collect(state)["failures"] == 0
    assert rows(state, "reply_texts")[0]["author_trusted"] == trusted
    assert writes(state) == []
    assert rows(state, "bot_actions") == []


def test_reporter_forbidden_keeps_reply_and_suppresses_more_resolves(state):
    seed(state, head_sha=state.old)
    seed(state, pr=8, head_sha=state.old)
    state.responses[state.project + "/merge_requests/8"] = state.responses[state.mr]
    state.responses[state.project + "/merge_requests/8/discussions"] = state.discussions
    state.resolve_status = 403
    run = collect(state)
    assert run["failures"] == 0
    assert [r.method for r in writes(state)] == ["POST", "PUT"]
    assert run["gitlab_reply"]["resolve_forbidden"] == 2
    assert "resolve_forbidden" in run["gitlab_reply"]["reasons"]
    assert run["gitlab_reply"]["forbidden_projects"] == [state.key]
    assert len([a for a in rows(state, "bot_actions") if a["kind"] == "reply"]) == 2
    assert not any(a["kind"] == "resolve" for a in rows(state, "bot_actions"))


def test_outdated_thread_is_resolved_through_api(state):
    seed(state, head_sha=state.old)
    run = collect(state)
    assert run["gitlab_reply"]["resolved_outdated"] == 1
    assert any(r.method == "PUT" for r in writes(state))


def test_incomplete_comparison_never_claims_fixed_or_resolves(state, monkeypatch):
    seed(state, head_sha=state.old)
    state.discussion["notes"][1]["body"] = "@argus-bot fixed, please recheck"
    state.responses[state.project + "/repository/compare"]["compare_timeout"] = True
    monkeypatch.setattr(replies, "recheck", REAL_RECHECK)
    run = collect(state)
    assert run["failures"] == 0
    assert [r.method for r in writes(state)] == ["POST"]
    assert "Not sure" in state.pending["body"]
    assert all(a["value"] != "fixed" for a in rows(state, "bot_actions"))


REAL_RECHECK = replies.recheck


@pytest.mark.parametrize("operation", ["reply", "resolve"])
def test_deleted_discussion_records_reason_and_keeps_only_confirmed_reply(state, operation):
    seed(state)
    state.discussion["notes"][1]["body"] = "Rejected: this behavior is deliberately required here"
    if operation == "reply":
        state.post_status = 404
    else:
        state.resolve_status = 404
    run = collect(state)
    assert run["failures"] == 0
    assert run["gitlab_reply"]["discussion_missing"] == 1
    assert "discussion_missing" in run["gitlab_reply"]["reasons"]
    actions = rows(state, "bot_actions")
    if operation == "reply":
        assert actions == []
    else:
        assert [(a["kind"], a["gh_comment_id"]) for a in actions] == [("reply", state.pending["id"])]


def test_unknown_outcome_recovers_marker_next_run_without_second_post(state):
    seed(state, head_sha=state.old)
    state.post_status = 503
    first = collect(state)
    assert first["failures"] == 1
    assert len(rows(state, "reply_attempts")) == 1
    assert [r.method for r in writes(state)] == ["POST"]
    state.discussion["notes"].append(state.pending)
    state.requests.clear()
    second = collect(state)
    assert second["failures"] == 0
    assert [r.method for r in writes(state)] == ["PUT"]
    posted = next(a for a in rows(state, "bot_actions") if a["kind"] == "reply")
    assert (posted["gh_comment_id"], posted["verdict"], posted["head_sha"]) == (state.pending["id"], "fixed", state.head)


def test_reply_marker_never_counts_as_human_when_bot_id_unknown(state):
    seed(state)
    with closing(db.connect(state.path)) as conn, conn:
        conn.execute("UPDATE publishing_identities SET bot_user_id=NULL")
    note = copy.deepcopy(state.discussion["notes"][1])
    note.update(id=999, body="Fixed\n\n<!-- argus:reply=addressed evidence=abc -->")
    state.discussion["notes"].append(note)
    collect(state)
    assert [r["evidence_id"] for r in rows(state, "reply_texts")] == ["243"]


@pytest.mark.parametrize("route", ["compare", "reply"])
def test_gitlab_rate_limit_during_reply_loop_pauses_only_gitlab(state, monkeypatch, route):
    seed(state, head_sha=state.old)
    seed(state, repo=gitlab_key(state.host, 99))
    fid = github_finding(state, monkeypatch)
    if route == "compare":
        state.responses[state.project + "/repository/compare"] = httpx.Response(429, headers={"Retry-After": "60"})
    else:
        state.post_status = 429
    run = collect(state)
    assert run["gitlab_paused"] is True
    assert run["failures"] == 0
    assert not run["stopped_by_rate_limit"]
    assert not any("/projects/99" in r.url.path for r in state.requests)
    assert any(r["finding_id"] == fid for r in rows(state, "reply_texts"))


def test_fork_source_rate_limit_pauses_gitlab_instead_of_denying_project(state, monkeypatch):
    seed(state, head_sha=state.old)
    seed(state, repo=gitlab_key(state.host, 99))
    fid = github_finding(state, monkeypatch)
    state.responses[state.mr]["source_project_id"] = 43
    source = state.prefix + "/projects/43"
    state.responses[source] = httpx.Response(429, headers={"Retry-After": "60"})
    monkeypatch.setenv("ARGUS_GITLAB_ALLOW", f"{state.key},{gitlab_key(state.host, 43)}")
    run = collect(state)
    assert run.get("gitlab_paused") is True
    assert run["failures"] == 0
    assert run.get("gitlab_reply", {}).get("project_denied", 0) == 0
    assert not run["stopped_by_rate_limit"]
    assert source in [r.url.path for r in state.requests]
    assert not any("/projects/99" in r.url.path for r in state.requests)
    assert not any("repository/compare" in r.url.path for r in state.requests)
    assert any(r["finding_id"] == fid for r in rows(state, "reply_texts"))
    assert writes(state) == []


@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR", "FIRST_TIMER",
    "FIRST_TIME_CONTRIBUTOR", "MANNEQUIN", "NONE", None, "owner", ""])
def test_github_trust_associations_unchanged(association):
    assert replies.trusted({"author_association": association}, {"repo": "owner/repo"}) == (
        association in {"OWNER", "MEMBER", "COLLABORATOR"})


@pytest.mark.parametrize("trusted", [0, None, 1])
def test_gitlab_trust_ignores_github_association(trusted):
    assert replies.trusted({"author_association": "OWNER", "author_trusted": trusted},
                           {"repo": gitlab_key("gitlab.example.test", 42)}) is (trusted == 1)


@pytest.mark.parametrize("case", ["hard_denied", "not_allowlisted"])
def test_live_denied_project_sends_no_write(state, case):
    seed(state, head_sha=state.old)
    state.deny(case)
    assert collect(state)["failures"] == 0
    assert writes(state) == []


@pytest.mark.parametrize("bot_id", [None, 9])
def test_live_gate_requires_recorded_token_identity(state, bot_id):
    seed(state, head_sha=state.old)
    with closing(db.connect(state.path)) as conn, conn:
        conn.execute("UPDATE publishing_identities SET bot_user_id=?", (bot_id,))
    run = direct_run(state)
    reason = "bot_id_missing" if bot_id is None else "bot_id_mismatch"
    assert run["gitlab_reply"][reason] == 1
    assert [r.url.path for r in state.requests] == ([] if bot_id is None else [state.prefix + "/user"])


def test_write_and_recheck_limits_are_separate_per_forge(state, monkeypatch):
    monkeypatch.setenv("ARGUS_REPLY_MAX_WRITES_PER_RUN", "1")
    monkeypatch.setenv("ARGUS_RECHECK_MAX_PER_RUN", "1")
    fid = seed(state, head_sha=state.old)
    gid = seed(state, repo="owner/repo", head_sha=state.old)
    run = {"bot": replies.counts(), "writes_attempted": 0, "rechecks_attempted": 0}
    for repo in ("owner/repo", state.key):
        assert replies._write_slot(run, repo)
        assert not replies._write_slot(run, repo)
    with closing(db.connect(state.path)) as conn:
        for finding_id in (gid, fid):
            finding = dict(conn.execute("SELECT * FROM published_findings WHERE id=?", (finding_id,)).fetchone())
            finding["comparison"] = {"status": "ahead"}
            assert replies._checked(conn, finding, state.head, STAMP, run, requested=True)["verdict"] == "fixed"
            assert replies._checked(conn, finding, "c" * 40, STAMP, run, requested=True) is None
    assert (run["writes_attempted"], run["gitlab_writes_attempted"]) == (1, 1)
    assert (run["rechecks_attempted"], run["gitlab_rechecks_attempted"]) == (1, 1)


@pytest.mark.parametrize("mode,flag,global_flag,enabled", [
    ("1", "1", "1", True), ("dry-run", "1", "1", False), ("bad", "1", "1", False),
    ("1", "bad", "1", False), ("1", "1", "0", False), ("1", "0", "1", False),
])
def test_integrations_exposes_reply_gate_and_refused_resolves(state, monkeypatch, mode, flag, global_flag, enabled):
    from fastapi.testclient import TestClient
    from argus.api import main
    from argus.gitlab import status

    monkeypatch.setenv("ARGUS_GITLAB_PUBLISH", mode)
    monkeypatch.setenv("ARGUS_GITLAB_REPLY_LOOP", flag)
    monkeypatch.setenv("ARGUS_REPLY_LOOP", global_flag)
    monkeypatch.setattr(status, "_identity_cache", None)
    state.responses[state.prefix + "/user"]["bot"] = True
    threads.COUNTERS["resolve_forbidden"] = 2
    result = TestClient(main.app).get("/integrations")
    assert result.status_code == 200
    assert result.json()["gitlab"]["replyLoop"] == {"enabled": enabled, "resolveForbidden": 2}
    assert {"label": "GitLab replies", "status": "2 resolves refused, raise the bot role to Developer",
            "state": "warn"} in result.json()["health"]


@pytest.mark.parametrize("global_mode,local_mode,want", [
    ("1", "1", "1"), ("1", "dry-run", "dry-run"), ("dry-run", "1", "dry-run"),
    ("0", "1", "0"), ("1", "0", "0"), ("invalid", "1", "0"),
])
def test_effective_publish_mode_uses_lower_ceiling(state, monkeypatch, global_mode, local_mode, want):
    from argus.gitlab import config

    monkeypatch.setenv("ARGUS_PUBLISH", global_mode)
    monkeypatch.setenv("ARGUS_GITLAB_PUBLISH", local_mode)
    assert config.effective_publish_mode() == want


def test_dry_run_collection_records_outcomes_without_reply_writes(state, monkeypatch):
    seed(state, head_sha=state.old)
    monkeypatch.setenv("ARGUS_GITLAB_PUBLISH", "dry-run")
    assert collect(state)["failures"] == 0
    assert len(rows(state, "reply_texts")) == 1
    assert writes(state) == []
    assert not any("repository/compare" in r.url.path for r in state.requests)


def test_recovery_precedes_new_head_recheck_and_explains_fix(state, monkeypatch):
    seed(state, head_sha=state.old)
    state.discussion["notes"][1]["body"] = "@argus-bot recheck please"
    monkeypatch.setattr(replies, "recheck", lambda *a: {"verdict": "unsure", "quote": ""})
    state.post_status = 503
    assert collect(state)["failures"] == 1
    pending = state.pending
    assert "Not sure" in pending["body"]
    state.discussion["notes"].append(pending)
    state.head = "c" * 40
    state.responses[state.mr]["sha"] = state.head
    state.post_status = 201
    state.requests.clear()
    monkeypatch.setattr(replies, "recheck", lambda *a: {"verdict": "fixed", "quote": "safe(value)"})
    assert collect(state)["failures"] == 0
    assert [r.method for r in writes(state)] == ["POST", "PUT"]
    assert "Addressed in commit ccccccc" in state.pending["body"]
    actions = rows(state, "bot_actions")
    recovered = next(a for a in actions if a["gh_comment_id"] == pending["id"])
    assert (recovered["head_sha"], recovered["verdict"]) == ("a" * 40, "unsure")


def test_unknown_reply_is_recovered_even_if_thread_was_resolved(state):
    seed(state, head_sha=state.old)
    state.post_status = 503
    assert collect(state)["failures"] == 1
    pending = state.pending
    state.discussion["notes"].append(pending)
    for note in state.discussion["notes"]:
        if note.get("resolvable"):
            note["resolved"] = True
    state.requests.clear()
    assert collect(state)["failures"] == 0
    assert writes(state) == []
    recovered = [a for a in rows(state, "bot_actions") if a["kind"] == "reply"]
    assert [(a["gh_comment_id"], a["verdict"]) for a in recovered] == [(pending["id"], "fixed")]


def test_forbidden_requested_fix_does_not_post_duplicate_addressed_note(state):
    seed(state, head_sha=state.old)
    state.discussion["notes"][1]["body"] = "@argus-bot fixed, please recheck"
    state.resolve_status = 403
    run = collect(state)
    assert run["failures"] == 0
    assert [r.method for r in writes(state)] == ["POST", "PUT"]
    assert run["gitlab_reply"]["resolve_forbidden"] == 1


def test_incomplete_current_comparison_blocks_retry_of_old_fixed_resolution(state, monkeypatch):
    seed(state, head_sha=state.old)
    state.discussion["notes"][1]["body"] = "@argus-bot fixed, please recheck"
    state.resolve_status = 403
    assert collect(state)["failures"] == 0
    state.head = "c" * 40
    state.responses[state.mr]["sha"] = state.head
    state.responses[state.project + "/repository/compare"]["compare_timeout"] = True
    state.resolve_status = 200
    state.requests.clear()
    monkeypatch.setattr(replies, "recheck", REAL_RECHECK)
    assert collect(state)["failures"] == 0
    assert writes(state) == []
    assert not any(a["kind"] == "resolve" for a in rows(state, "bot_actions"))
    state.requests.clear()
    assert collect(state)["failures"] == 0
    assert writes(state) == []


def test_rate_limit_during_reply_reconciliation_pauses_without_failure(state):
    seed(state, head_sha=state.old)
    state.post_status = 503

    def discussions(request):
        if state.pending is not None:
            return httpx.Response(429, headers={"Retry-After": "60"})
        return httpx.Response(200, json=state.discussions)

    state.responses[state.mr + "/discussions"] = discussions
    run = collect(state)
    assert run["gitlab_paused"] is True
    assert run["failures"] == 0
    assert len(rows(state, "reply_attempts")) == 1
    assert [r.method for r in writes(state)] == ["POST"]


def test_cached_fix_cannot_be_posted_after_comparison_becomes_incomplete(state, monkeypatch):
    seed(state, head_sha=state.old)
    state.discussion["notes"][1]["body"] = "@argus-bot recheck please"
    monkeypatch.setenv("ARGUS_REPLY_MAX_WRITES_PER_RUN", "0")
    assert collect(state)["failures"] == 0
    assert writes(state) == []
    assert rows(state, "bot_actions")[0]["value"] == "fixed"
    state.responses[state.project + "/repository/compare"]["compare_timeout"] = True
    monkeypatch.setenv("ARGUS_REPLY_MAX_WRITES_PER_RUN", "20")
    monkeypatch.setattr(replies, "recheck", REAL_RECHECK)
    assert collect(state)["failures"] == 0
    assert [r.method for r in writes(state)] == ["POST"]
    assert "Not sure" in state.pending["body"]
    assert "Fixed at" not in state.pending["body"]
