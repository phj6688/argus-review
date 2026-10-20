"""Regression cases for collected replies and re-anchored checks, using local fakes."""

import base64
import json
import re
import sqlite3
import time

import httpx
import pytest

from argus import acceptance, client, github, replies
from argus.api import db
from test_acceptance import rows, seed
import test_replies as fixtures
from test_replies import setup_loop
from reply_helpers import file_entry

database = fixtures.database
live = fixtures.live

REAL_REPLY = replies.reply
REAL_RECHECK = replies.recheck


def pull(monkeypatch, head, author="author"):
    monkeypatch.setattr(github, "get_json_cached", lambda *a: (
        {"state": "open", "head": {"sha": head * 40}, "user": {"login": author}}, ""))


def file_response(monkeypatch, code, statuses=(200,)):
    reads = []
    def get(url, params=None):
        reads.append(url)
        status = statuses[min(len(reads) - 1, len(statuses) - 1)]
        return httpx.Response(status, json={"encoding": "base64",
            "content": base64.b64encode(code.encode()).decode()},
            request=httpx.Request("GET", "https://api.github.com" + url))
    monkeypatch.setattr(github, "get", get)
    return reads


def finding(patch=None, **updates):
    files = [] if patch is None else [file_entry(filename="app.py", patch=patch)]
    return {"path": "app.py", "head_sha": "a" * 40, "line": 34, "end_line": 34,
            "evidence": "result = eval(user_input)", "comparison": {"status": "ahead", "files": files}, **updates}


@pytest.mark.parametrize("shift", [0, 10, 20, 21, 25, 40, 100])
def test_unchanged_flagged_line_skips_model_after_offset_change(live, monkeypatch, shift):
    code = "\n".join([f"line_{n} = {n}" for n in range(33 + shift)] +
                     ["    result = eval(user_input)"] + ["tail = 1"] * 60)
    file_response(monkeypatch, code)
    seen = []
    def complete(*args):
        seen.append(json.loads(args[2]))
        return {"verdict": "fixed", "quote": seen[-1]["current_code"].splitlines()[0]}
    monkeypatch.setattr(client, "complete_tool", complete)
    patch = (f"@@ -2,0 +3,{shift} @@\n" + "\n".join(f"+line_{n} = {n}" for n in range(shift))
             if shift else "@@ -100 +100 @@\n-old_tail\n+tail = 1")
    result = REAL_RECHECK("owner/repo", finding(patch), "b" * 40)
    assert result == {"verdict": "unsure", "quote": ""}
    assert not seen
    assert replies._anchor(finding(patch), {"patch": patch}) == (34 + shift, 34 + shift)


@pytest.mark.parametrize("patch,line,end,expected_start,quote,expected", [
    ("@@ -2,2 +2,5 @@\n-a\n-b\n+c\n+d\n+e\n+f\n+g\n"
     "@@ -12,4 +15,1 @@\n-a\n-b\n-c\n-d\n+e", 34, 36, 14, "fixed_line", "unsure"),
    ("@@ -2,0 +3,30 @@\n" + "+padding\n" * 30 +
     "@@ -32,5 +62,4 @@\n-a\n-b\n-c\n-d\n-e\n" + "+fixed_line\n" * 4,
     34, 35, 42, "fixed_line", "fixed"),
    ("@@ -34,2 +34,2 @@\n-old_line\n+fixed_line\n fixed_line", 34, 35, 14, "context_only", "unsure"),
    ("@@ -34,2 +34,2 @@\n-old_line\n+fixed_line\n fixed_line", 34, 35, 14, "fixed", "unsure"),
    ("@@ -34 +33,0 @@\n-old_line", 34, 34, 14, "context_only", "unsure"),
])
def test_anchor_offsets_overlap_and_full_line_quote(live, monkeypatch, patch, line, end, expected_start, quote, expected):
    code = ["context_only"] * 100
    code[33:36] = ["fixed_line"] * 3
    code[61:65] = ["fixed_line"] * 4
    file_response(monkeypatch, "\n".join(code))
    seen = []
    def complete(*args):
        seen.append(json.loads(args[2]))
        return {"verdict": "fixed", "quote": quote}
    monkeypatch.setattr(client, "complete_tool", complete)
    result = REAL_RECHECK("owner/repo", finding(patch, line=line, end_line=end), "b" * 40)
    assert result["verdict"] == expected
    anchor = replies._anchor(finding(patch, line=line, end_line=end), {"patch": patch})
    assert max(1, anchor[0] - 20) == expected_start
    if seen:
        assert seen[0]["current_start_line"] == expected_start


@pytest.mark.parametrize("comparison", [None, {"status": "behind"}, {"status": "diverged"},
    {"status": "identical"}, {"status": "ahead", "files": [{"filename": "app.py"}]}])
def test_unusable_comparison_never_calls_model(live, monkeypatch, comparison):
    file_response(monkeypatch, "return value\n" * 60)
    assert REAL_RECHECK("owner/repo", finding(comparison=comparison), "b" * 40)["verdict"] == "unsure"


def test_evidence_veto_searches_whole_file_with_normalized_whitespace(live, monkeypatch):
    file_response(monkeypatch, "fixed_line\n" * 80 + "result\t=   eval(user_input)\n")
    monkeypatch.setattr(client, "complete_tool", lambda *a: {"verdict": "fixed", "quote": "fixed_line"})
    assert REAL_RECHECK("owner/repo", finding("@@ -34 +34 @@\n-old_line\n+fixed_line"), "b" * 40)["verdict"] == "unsure"


def test_present_quote_must_be_in_current_code(live, monkeypatch):
    file_response(monkeypatch, "return value\n" * 60)
    monkeypatch.setattr(client, "complete_tool", lambda *a: {"verdict": "present", "quote": "@someone #12 https://example.org"})
    assert REAL_RECHECK("owner/repo", finding("@@ -34 +34 @@\n-old_line\n+return value"), "b" * 40) == {"verdict": "present", "quote": ""}


def test_original_head_records_unsure_and_answers_request(live, monkeypatch):
    _, checks, writes, _ = setup_loop(live, monkeypatch, text="recheck", changed=False)
    monkeypatch.setattr(replies, "recheck", REAL_RECHECK)
    pull(monkeypatch, "a")
    acceptance.collect(live)
    assert not checks
    assert writes[0][1].startswith("No change to this code since aaaaaaa.")
    assert [(a["kind"], a["value"]) for a in rows(live, "bot_actions")] == [("recheck", "unsure"), ("reply", "recheck")]


@pytest.mark.parametrize("text", ["No.", "Rejected."])
def test_confirmed_fix_does_not_seed_suppression(live, monkeypatch, text):
    setup_loop(live, monkeypatch, text=text)
    acceptance.collect(live)
    assert acceptance.stats(live)["totals"]["strict_accepted"] == 1
    assert not rows(live, "known_fps_live")


def test_author_resolved_fix_is_strict_and_uses_check_cap(live, monkeypatch):
    _, checks, writes, _ = setup_loop(live, monkeypatch, resolved=True)
    monkeypatch.setenv("ARGUS_RECHECK_MAX_PER_RUN", "0")
    acceptance.collect(live)
    assert not checks
    monkeypatch.setenv("ARGUS_RECHECK_MAX_PER_RUN", "1")
    acceptance.collect(live)
    assert len(checks) == 1 and not writes
    assert acceptance.stats(live)["totals"]["strict_accepted"] == 1
    assert acceptance.stats(live)["last_run"]["rechecks_attempted"] == 1


@pytest.mark.parametrize("login,association,allowed", [
    ("author", "NONE", False), ("AUTHOR", "NONE", False),
    ("visitor", "OWNER", True), ("visitor", "MEMBER", True), ("visitor", "COLLABORATOR", True),
    ("visitor", "CONTRIBUTOR", False), ("visitor", "NONE", False), ("visitor", None, False)])
def test_requests_require_trusted_association(live, monkeypatch, login, association, allowed):
    _, checks, writes, comments = setup_loop(live, monkeypatch, text="recheck", changed=False, verdict="present")
    pull(monkeypatch, "b")
    comments[0].update(user={"login": login}, author_association=association)
    acceptance.collect(live)
    assert len(checks) == len(writes) == int(allowed)
    assert rows(live, "reply_texts")[0]["author_association"] == association


def test_many_requests_at_same_head_get_one_reply(live, monkeypatch):
    _, checks, writes, comments = setup_loop(live, monkeypatch, text="recheck", changed=False, verdict="present")
    pull(monkeypatch, "b")
    comments[:] = [{**comments[0], "id": 900 + n} for n in range(5)]
    acceptance.collect(live)
    assert len(checks) == len(writes) == 1
    assert all(r["answered_head"] == "b" * 40 for r in rows(live, "reply_texts"))
    comments.clear()
    pull(monkeypatch, "c")
    acceptance.collect(live)
    assert len(checks) == len(writes) == 1


def test_reopened_thread_stops_after_confirmed_fix(live, monkeypatch):
    _, checks, writes, _ = setup_loop(live, monkeypatch)
    for head in "bcdef":
        pull(monkeypatch, head)
        acceptance.collect(live)
    assert len(checks) == 1
    assert [kind for kind, _ in writes] == ["reply", "resolve"]


@pytest.mark.parametrize("cap", [0, 1, 2])
def test_write_cap_counts_replies_and_resolutions_and_retries(live, monkeypatch, cap):
    _, _, writes, _ = setup_loop(live, monkeypatch)
    monkeypatch.setenv("ARGUS_REPLY_MAX_WRITES_PER_RUN", str(cap))
    acceptance.collect(live)
    assert len(writes) == cap
    monkeypatch.setenv("ARGUS_REPLY_MAX_WRITES_PER_RUN", "2")
    acceptance.collect(live)
    assert [kind for kind, _ in writes] == ["reply", "resolve"]


@pytest.mark.parametrize("failure", ["timeout", "database"])
@pytest.mark.parametrize("text", ["Deferred until next release", "recheck"])
def test_ambiguous_reply_is_recovered_from_collected_marker(live, monkeypatch, failure, text):
    _, checks, _, comments = setup_loop(live, monkeypatch, text=text, changed=False, verdict="present")
    pull(monkeypatch, "b")
    server = []
    def post(url, **kw):
        body = kw["json"]["body"]
        server.append(body)
        assert "evidence=900 -->" in body
        comments.append({**comments[0], "id": 5000, "body": body,
                         "user": {"login": "PUBLISHER", "type": "Bot"}})
        if failure == "timeout":
            raise httpx.ReadTimeout("timeout", request=httpx.Request("POST", url))
        return httpx.Response(201, json={"id": 5000}, request=httpx.Request("POST", url))
    real_record = replies._record
    def record(conn, finding, kind, *args, **kwargs):
        if kind == "reply":
            raise sqlite3.OperationalError("locked")
        return real_record(conn, finding, kind, *args, **kwargs)
    monkeypatch.setattr(replies, "reply", REAL_REPLY)
    monkeypatch.setattr(httpx, "post", post)
    if failure == "database":
        monkeypatch.setattr(replies, "_record", record)
    acceptance.collect(live)
    monkeypatch.setattr(replies, "_record", real_record)
    pull(monkeypatch, "c")
    acceptance.collect(live)
    assert len(server) == 1
    action = next(a for a in rows(live, "bot_actions") if a["kind"] == "reply")
    assert action["gh_comment_id"] == 5000 and action["head_sha"] == "b" * 40


def test_old_request_cannot_resolve_for_failed_addressed_reply(live, monkeypatch):
    _, checks, writes, _ = setup_loop(live, monkeypatch, text="recheck", verdict="present")
    pull(monkeypatch, "b")
    acceptance.collect(live)
    good_reply = replies.reply
    def fail(*a):
        raise httpx.ReadTimeout("timeout")
    pull(monkeypatch, "c")
    monkeypatch.setattr(replies, "reply", fail)
    monkeypatch.setattr(replies, "recheck", lambda *a: {"verdict": "fixed", "quote": "return value"})
    acceptance.collect(live)
    assert [kind for kind, _ in writes] == ["reply"]
    monkeypatch.setattr(replies, "reply", good_reply)
    acceptance.collect(live)
    assert [kind for kind, _ in writes] == ["reply", "reply", "resolve"]
    assert writes[1][1].startswith("Addressed in commit ccccccc")
    assert [a["head_sha"] for a in rows(live, "bot_actions") if a["kind"] == "reply"] == ["b" * 40, "c" * 40]


@pytest.mark.parametrize("status", [403, 429, 500, 503])
def test_transient_read_records_nothing_and_retries(live, monkeypatch, status):
    _, _, writes, _ = setup_loop(live, monkeypatch, text="recheck")
    pull(monkeypatch, "b")
    monkeypatch.setattr(replies, "recheck", REAL_RECHECK)
    reads = []
    def get(url, params=None):
        if "/compare/" in url:
            return httpx.Response(200, json={"status": "ahead", "files": [
                file_entry(filename="app.py", patch="@@ -10 +10 @@\n-old\n+return value")]},
                request=httpx.Request("GET", "https://api.github.com" + url))
        reads.append(params["ref"])
        return httpx.Response(status if len(reads) == 1 else 200,
            json={"encoding": "base64", "content": base64.b64encode(b"return value\n" * 60).decode()},
            request=httpx.Request("GET", "https://api.github.com" + url))
    monkeypatch.setattr(github, "get", get)
    monkeypatch.setattr(client, "complete_tool", lambda *a: {"verdict": "fixed", "quote": "return value"})
    acceptance.collect(live)
    assert not rows(live, "bot_actions") and not writes
    acceptance.collect(live)
    assert reads == ["b" * 40, "b" * 40, "a" * 40]
    assert [kind for kind, _ in writes] == ["reply", "resolve"]


@pytest.mark.parametrize("verdict", ["fixed", "present"])
def test_quotes_are_fenced_against_mentions_and_links(live, monkeypatch, verdict):
    _, _, writes, _ = setup_loop(live, monkeypatch, text="recheck", changed=False)
    pull(monkeypatch, "b")
    quote = "`@someone` #12 https://example.org ``value``"
    monkeypatch.setattr(replies, "recheck", lambda *a: {"verdict": verdict, "quote": quote})
    acceptance.collect(live)
    assert "``` " + quote + " ```" in writes[0][1]


@pytest.mark.parametrize("path", ["/repos/other/repo/pulls/1/comments/2/replies", "/repos/owner/repo/issues/1/comments",
    "/repos/owner/repo/pulls/1/comments/2/replies/extra", "/repos/owner/repo/pulls/x/comments/2/replies"])
def test_rest_write_must_match_validated_repository(live, path):
    with pytest.raises(ValueError):
        replies._write("owner/repo", "https://api.github.com" + path, {"body": "x"})


def test_rollback_positional_insert_still_works(database):
    fid = seed(database)
    with db.connect(database) as conn:
        conn.execute("INSERT INTO finding_outcomes VALUES (?, ?, ?, ?, ?, ?)",
                     (fid, "reply", "fixed", "9", "2026-01-01", None))
    assert rows(database, "finding_outcomes")[0]["value"] == "fixed"


@pytest.mark.parametrize("text,requested", [
    ("Rejected, but please do not recheck this file", False), ("The recheck() helper already guards this", False),
    ("No need to check again", False), ("please RECHECK", False), ("prose\nrecheck", False),
    ("\n **RECHECK** please", True), ("re-check", True), ("check again", True),
    ("Please @Publisher take a look", True), ("@Publisher-other", False), ("rechecker", False)])
def test_request_requires_opening_command_or_login_mention(live, monkeypatch, text, requested):
    _, checks, _, _ = setup_loop(live, monkeypatch, text=text, changed=False, verdict="present")
    pull(monkeypatch, "b")
    acceptance.collect(live)
    assert bool(checks) is requested


def test_marker_scan_is_bounded_for_unterminated_prefixes():
    body = "<!-- argus:reply=" * 4000
    start = time.monotonic()
    assert not replies.MARKER.search(body)
    assert time.monotonic() - start < 0.5
    assert re.search(r"<!--", body)


@pytest.mark.parametrize("later_head", [False, True])
def test_only_later_confirmed_fix_prevents_new_suppression(database, later_head):
    fid = seed(database)
    now = db._now_iso()
    with db.connect(database) as conn:
        acceptance._record(conn, fid, "reply", "rejected", 900, now,
                           body="Rejected", author_association="MEMBER")
        conn.execute("INSERT INTO bot_actions(finding_id,kind,value,evidence_id,created_at) "
                     "VALUES (?,'recheck','fixed',?,?)", (fid, ("b" if later_head else "a") * 40, now))
        acceptance._sync_rejections(conn, conn.execute("SELECT * FROM published_findings").fetchall(), now)
    assert bool(rows(database, "known_fps_live")) is not later_head


def test_later_confirmed_fix_removes_its_previous_suppression(live, monkeypatch):
    _, _, _, _ = setup_loop(live, monkeypatch, text="Rejected.", changed=False)
    acceptance.collect(live)
    assert rows(live, "known_fps_live")
    pull(monkeypatch, "c")
    monkeypatch.setattr(github, "get", lambda url, *a: httpx.Response(200,
        json={"status": "ahead", "files": [{"filename": "app.py", "status": "removed"}]},
        request=httpx.Request("GET", "https://api.github.com" + url)))
    acceptance.collect(live)
    assert acceptance.stats(live)["totals"]["strict_accepted"] == 1
    assert not rows(live, "known_fps_live")


def test_default_write_cap_is_shared_across_findings(live, monkeypatch):
    _, checks, writes, comments = setup_loop(live, monkeypatch, text="Deferred until later", changed=False)
    monkeypatch.delenv("ARGUS_REPLY_MAX_WRITES_PER_RUN", raising=False)
    for n in range(20):
        seed(live)
        comments.append({**comments[0], "id": 901 + n, "in_reply_to_id": 102 + n})
    monkeypatch.setattr(github, "graphql_read", lambda *a: {"repository": {"pullRequest": {
        "reviewThreads": {"nodes": [{"id": f"thread-{cid}", "isResolved": False,
        "comments": {"nodes": [{"databaseId": cid}]}} for cid in range(101, 122)]}}}})
    acceptance.collect(live)
    assert not checks and len(writes) == 20
    acceptance.collect(live)
    assert len(writes) == 21


def test_failed_writes_consume_the_run_cap(live, monkeypatch):
    _, _, _, comments = setup_loop(live, monkeypatch, text="Deferred until later", changed=False)
    seed(live)
    comments.append({**comments[0], "id": 901, "in_reply_to_id": 102})
    monkeypatch.setattr(github, "graphql_read", lambda *a: {"repository": {"pullRequest": {
        "reviewThreads": {"nodes": [{"id": f"thread-{cid}", "isResolved": False,
        "comments": {"nodes": [{"databaseId": cid}]}} for cid in (101, 102)]}}}})
    attempts = []
    def fail(*a):
        attempts.append(1)
        raise httpx.ReadTimeout("timeout")
    monkeypatch.setattr(replies, "reply", fail)
    monkeypatch.setenv("ARGUS_REPLY_MAX_WRITES_PER_RUN", "1")
    acceptance.collect(live)
    assert len(attempts) == 1


def test_other_login_cannot_recover_a_reply_marker(live, monkeypatch):
    _, _, writes, comments = setup_loop(live, monkeypatch, text="Deferred until later", changed=False)
    comments.append({**comments[0], "id": 901, "body": "<!-- argus:reply=deferred evidence=900 -->",
                     "user": {"login": "stranger"}})
    acceptance.collect(live)
    assert len(writes) == 1
    assert next(a for a in rows(live, "bot_actions") if a["kind"] == "reply")["gh_comment_id"] == 901


def test_rest_write_repository_match_is_case_insensitive(live, monkeypatch):
    calls = []
    def post(url, **kw):
        calls.append(url)
        return httpx.Response(201, json={"id": 1}, request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx, "post", post)
    assert replies._write("owner/repo", "https://api.github.com/repos/OWNER/REPO/pulls/1/comments/2/replies", {"body": "x"}) == {"id": 1}
    assert len(calls) == 1


def test_removed_file_without_patch_is_unsure(live, monkeypatch):
    file_response(monkeypatch, "", statuses=(404,))
    item = finding(comparison={"status": "ahead", "files": [{"filename": "app.py", "status": "removed"}]})
    assert REAL_RECHECK("owner/repo", item, "b" * 40) == {"verdict": "unsure", "quote": ""}


def test_collected_fixed_request_recovers_resolution_without_another_reply(live, monkeypatch):
    _, _, writes, comments = setup_loop(live, monkeypatch, text="recheck", changed=False)
    real_record = replies._record
    def fail_record(conn, finding, kind, *args, **kwargs):
        if kind == "reply":
            raise sqlite3.OperationalError("locked")
        return real_record(conn, finding, kind, *args, **kwargs)
    monkeypatch.setattr(replies, "_record", fail_record)
    acceptance.collect(live)
    assert len(writes) == 1
    comments.append({**comments[0], "id": 901, "body": writes[0][1], "user": {"login": "publisher"}})
    monkeypatch.setattr(replies, "_record", real_record)
    acceptance.collect(live)
    assert [kind for kind, _ in writes] == ["reply", "resolve"]
    action = next(a for a in rows(live, "bot_actions") if a["kind"] == "reply")
    assert action["verdict"] == "fixed"


def test_fixed_quote_cannot_borrow_matching_fragment_from_context(live, monkeypatch):
    code = ["return value + 1"] * 60
    code[33] = "return value"
    file_response(monkeypatch, "\n".join(code))
    monkeypatch.setattr(client, "complete_tool", lambda *a: {"verdict": "fixed", "quote": "return value + 1"})
    assert REAL_RECHECK("owner/repo", finding("@@ -34 +34 @@\n-old_line\n+return value", evidence="short"), "b" * 40)["verdict"] == "unsure"
