"""Reply transport and collector contracts with local fakes."""

import sqlite3
import time

import httpx
import pytest

from argus import acceptance, client, github, replies
from argus.api import db
import test_acceptance as fixtures
from test_acceptance import rows, seed
from reply_helpers import file_entry

database = fixtures.database


@pytest.fixture
def live(database, monkeypatch):
    monkeypatch.setenv("ARGUS_PUBLISH", "1")
    monkeypatch.setenv("ARGUS_PUBLISH_REPOS", "owner/repo")
    monkeypatch.setenv("ARGUS_PUBLISH_TOKEN", "write-secret")
    monkeypatch.setenv("ARGUS_REPLY_LOOP", "1")
    monkeypatch.setattr(client, "complete_tool", lambda *a: pytest.fail("Unexpected model"))
    return database


def test_transport(live, monkeypatch):
    calls = []
    def post(url, **kw):
        calls.append((url, kw))
        assert kw["headers"]["Authorization"] == "Bearer write-secret"
        assert not kw["follow_redirects"]
        body = ({"id": 123} if url.endswith("replies") else
                {"data": {"resolveReviewThread": {"thread": {"id": "thread", "isResolved": True}}}})
        return httpx.Response(200, json=body, headers={"x-ratelimit-remaining": "5"},
                              request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx, "post", post)
    assert replies.reply("owner/repo", 3, 101, "Noted.") == 123
    replies.resolve_thread("owner/repo", "thread")
    assert calls[0][0].endswith("/repos/owner/repo/pulls/3/comments/101/replies")
    assert calls[0][1]["json"]["body"].endswith("<!-- argus:reply=reply -->")
    assert calls[1][1]["json"] == {
        "query": replies._RESOLVE, "variables": {"id": "thread"}}
    assert replies._RESOLVE.count("resolveReviewThread") == 1
    assert github.quota_snapshot() == {}


@pytest.mark.parametrize("mode,switch,repo", [("dry-run", "1", "owner/repo"),
    ("0", "1", "owner/repo"), ("1", "0", "owner/repo"), ("1", "1", "owner/other")])
def test_disabled_writes(live, monkeypatch, mode, switch, repo):
    monkeypatch.setenv("ARGUS_PUBLISH", mode)
    monkeypatch.setenv("ARGUS_REPLY_LOOP", switch)
    assert replies.reply(repo, 3, 101, "Noted.") is None
    assert replies.resolve_thread(repo, "thread") is None


@pytest.mark.parametrize("operation", ["reply", "resolve", "variable"])
def test_denied_before_request(live, monkeypatch, operation):
    monkeypatch.setenv("ARGUS_DENY_OWNERS", "acme-corp")
    owner = "acme-corp"
    monkeypatch.setenv("ARGUS_PUBLISH_REPOS", f"owner/repo,{owner}/repo")
    before = github.refusal_count()
    with pytest.raises(github.GitHubDenied):
        if operation == "reply":
            replies.reply(f"{owner}/repo", 3, 101, "Noted.")
        else:
            replies.resolve_thread("owner/repo" if operation == "variable" else f"{owner}/repo",
                                   owner if operation == "variable" else "thread")
    assert github.refusal_count() == before + 1


def setup_loop(live, monkeypatch, *, text=None, verdict="fixed", changed=True, resolved=False):
    fid = seed(live)
    stamp = db._now_iso()
    comments = [] if text is None else [{"id": 900, "in_reply_to_id": 101, "body": text,
        "user": {"login": "author"}, "author_association": "MEMBER", "created_at": stamp, "updated_at": stamp}]
    monkeypatch.setattr(github, "list_cached", lambda *a, **k: comments)
    monkeypatch.setattr(github, "get_json_cached", lambda *a: (
        {"state": "open", "head": {"sha": "b" * 40}, "user": {"login": "author"}}, ""))
    monkeypatch.setattr(github, "graphql_read", lambda *a: {"repository": {"pullRequest": {
        "reviewThreads": {"nodes": [{"id": "thread", "isResolved": resolved,
        "comments": {"nodes": [{"databaseId": 101}]}}]}}}})
    monkeypatch.setattr(github, "get", lambda *a: httpx.Response(200, json={"status": "ahead",
        "files": [{"filename": "app.py", "status": "removed"}] if changed else []},
        request=httpx.Request("GET", "https://api.github.com/repos/owner/repo/compare/a...b")))
    checks, writes = [], []
    def recheck(repo, finding, head):
        checks.append((finding["id"], head))
        return {"verdict": verdict, "quote": "if value is None: return"}
    monkeypatch.setattr(replies, "recheck", recheck)
    def reply(repo, pr, comment, body):
        writes.append(("reply", body))
        return 901
    def resolve(*a):
        writes.append(("resolve", "thread"))
        return True
    monkeypatch.setattr(replies, "reply", reply)
    monkeypatch.setattr(replies, "resolve_thread", resolve)
    return fid, checks, writes, comments


def test_changed_fixed_once(live, monkeypatch):
    fid, checks, writes, _ = setup_loop(live, monkeypatch)
    acceptance.collect(live)
    assert len(checks) == 1
    assert writes[0][1].startswith("Addressed in commit bbbbbbb.")
    assert writes[0][1].endswith(f"<!-- argus:reply=addressed evidence={'b' * 40} -->")
    assert writes[1] == ("resolve", "thread")
    actions = rows(live, "bot_actions")
    assert [(a["kind"], a["value"]) for a in actions] == [
        ("recheck", "fixed"), ("reply", "addressed"), ("resolve", "true")]
    assert rows(live, "published_findings")[0]["gh_thread_id"] == "thread"
    report = acceptance.stats(live)
    assert report["totals"]["strict_accepted"] == 1
    assert report["bot"] == {"rechecks": {"fixed": 1, "present": 0, "unsure": 0}, "replies": 1, "resolutions": 1}
    assert report["last_run"]["bot"] == report["bot"]
    acceptance.collect(live)
    assert len(checks) == 1 and len(writes) == 2
    assert len(rows(live, "bot_actions")) == 3


@pytest.mark.parametrize("verdict", ["present", "unsure"])
def test_changed_not_fixed(live, monkeypatch, verdict):
    _, checks, writes, _ = setup_loop(live, monkeypatch, verdict=verdict)
    acceptance.collect(live)
    acceptance.collect(live)
    assert len(checks) == 1 and not writes
    assert acceptance.stats(live)["totals"]["strict_accepted"] == 0
    assert acceptance.stats(live)["totals"]["loose_accepted"] == 1


@pytest.mark.parametrize("text", ["RECHECK please", "re-check", "check again", "@Publisher please"])
@pytest.mark.parametrize("verdict,prefix", [("fixed", "Fixed"), ("present", "Still present"), ("unsure", "Not sure")])
def test_requested_recheck(live, monkeypatch, text, verdict, prefix):
    _, checks, writes, _ = setup_loop(live, monkeypatch, text=text, verdict=verdict, changed=False)
    acceptance.collect(live)
    acceptance.collect(live)
    assert len(checks) == 1
    expected = "No change to this code since aaaaaaa." if verdict == "unsure" else prefix + " at bbbbbbb"
    assert writes[0][1].startswith(expected)
    assert next(a for a in rows(live, "bot_actions") if a["kind"] == "reply")["value"] == "recheck"
    assert len(writes) == (2 if verdict == "fixed" else 1)


@pytest.mark.parametrize("text,body,resolve", [
    ("Rejected because callers already validate this input", "Understood. A finding with this title in this file will not be posted again.", True),
    ("By design callers already validate this input", "Understood. A finding with this title in this file will not be posted again.", True),
    ("Rejected.\nCallers already validate this input.", "Understood. A finding with this title in this file will not be posted again.", True),
    ("No.", None, False), ("Rejected no reason", None, False),
    ("By design only three words", None, False),
    ("Deferred until next release", "Noted.", False), ("Fixed now", None, False)])
def test_author_decisions(live, monkeypatch, text, body, resolve):
    _, checks, writes, _ = setup_loop(live, monkeypatch, text=text, changed=False)
    acceptance.collect(live)
    acceptance.collect(live)
    assert not checks
    assert len(writes) == (int(body is not None) + int(resolve))
    if body:
        assert writes[0][1].startswith(body)


def test_resolved_records_only(live, monkeypatch):
    _, checks, writes, _ = setup_loop(live, monkeypatch, text="recheck", resolved=True)
    acceptance.collect(live)
    assert len(checks) == 1 and not writes
    assert rows(live, "finding_outcomes")
    assert acceptance.stats(live)["totals"]["strict_accepted"] == 1


def test_reply_failure_retries_after_cursor(live, monkeypatch, caplog):
    _, checks, writes, comments = setup_loop(live, monkeypatch, text="recheck", changed=False)
    real = replies.reply
    def fail(*a):
        raise ValueError("private body write-secret")
    monkeypatch.setattr(replies, "reply", fail)
    acceptance.collect(live)
    assert [a["kind"] for a in rows(live, "bot_actions")] == ["recheck"]
    assert "private body" not in caplog.text and "write-secret" not in caplog.text
    records = {id(r): r for r in caplog.records if r.name == "argus.acceptance" and r.levelname == "WARNING"}
    assert len(records) == 1
    comments.clear()
    monkeypatch.setattr(replies, "reply", real)
    acceptance.collect(live)
    assert len(checks) == 1 and len(writes) == 2


def test_write_limit_stops_run(live, monkeypatch):
    setup_loop(live, monkeypatch)
    def limited(*a):
        raise github.GitHubRateLimited(time.time() + 60)
    monkeypatch.setattr(replies, "reply", limited)
    with pytest.raises(github.GitHubRateLimited):
        acceptance.collect(live)
    assert acceptance.stats(live)["last_run"]["stopped_by_rate_limit"]
    assert [a["kind"] for a in rows(live, "bot_actions")] == ["recheck"]


@pytest.mark.parametrize("body,user", [
    ("Fixed\n<!-- argus:reply=addressed -->", {"login": "unknown"}),
    ("Fixed", {"login": "PUBLISHER"}), ("Fixed", {"login": "other", "type": "Bot"})])
def test_own_comments_not_classified(live, monkeypatch, body, user):
    _, checks, writes, comments = setup_loop(live, monkeypatch, text=body, changed=False)
    comments[0]["user"] = user
    acceptance.collect(live)
    assert not [o for o in rows(live, "finding_outcomes") if o["kind"] == "reply"]
    assert not checks and not writes


def test_limit_and_pending_requests(live, monkeypatch):
    _, checks, writes, comments = setup_loop(live, monkeypatch, text="recheck", changed=False, verdict="present")
    monkeypatch.setenv("ARGUS_RECHECK_MAX_PER_RUN", "1")
    seed(live)
    comments.append({**comments[0], "id": 902, "in_reply_to_id": 102})
    monkeypatch.setattr(github, "graphql_read", lambda *a: {"repository": {"pullRequest": {
        "reviewThreads": {"nodes": [{"id": f"thread-{cid}", "isResolved": False,
        "comments": {"nodes": [{"databaseId": cid}]}} for cid in (101, 102)]}}}})
    acceptance.collect(live)
    assert len(checks) == len(writes) == 1
    comments.clear()
    acceptance.collect(live)
    assert len(checks) == len(writes) == 2
    acceptance.collect(live)
    assert len(checks) == len(writes) == 2


@pytest.mark.parametrize("mode,switch", [("dry-run", "1"), ("0", "1"), ("1", "0")])
def test_loop_disabled(live, monkeypatch, mode, switch):
    _, checks, writes, _ = setup_loop(live, monkeypatch)
    monkeypatch.setenv("ARGUS_PUBLISH", mode)
    monkeypatch.setenv("ARGUS_REPLY_LOOP", switch)
    acceptance.collect(live)
    assert not checks and not writes and not rows(live, "bot_actions")


def test_resolution_failure_retries_without_reply(live, monkeypatch):
    _, checks, writes, _ = setup_loop(live, monkeypatch)
    real = replies.resolve_thread
    def fail(*a):
        raise ValueError("private")
    monkeypatch.setattr(replies, "resolve_thread", fail)
    acceptance.collect(live)
    monkeypatch.setattr(replies, "resolve_thread", real)
    acceptance.collect(live)
    assert len(checks) == 1 and len(writes) == 2
    assert len(rows(live, "bot_actions")) == 3


@pytest.mark.parametrize("verdict,quote,expected", [
    ("fixed", "return value", "fixed"), ("fixed", "invented", "unsure"),
    ("fixed", "", "unsure"), ("present", "return value", "present"),
    ("unsure", "", "unsure"), ("invalid", "return value", "unsure"), ("fixed", None, "unsure")])
def test_recheck_code_and_validation(live, monkeypatch, verdict, quote, expected):
    fetched, requests = [], []
    def fetch(repo, path, head):
        fetched.append((repo, path, head))
        return "return value\n" * 60
    monkeypatch.setattr(replies, "_read_file", fetch)
    def complete(model, system, user, tool):
        requests.append((model, system, user, tool))
        return {"verdict": verdict, "quote": quote}
    monkeypatch.setattr(client, "complete_tool", complete)
    finding = {"path": "app.py", "head_sha": "a" * 40, "title": "Missing guard", "description": "No validation",
               "evidence": "value", "line": 10, "end_line": 12, "model": "configured-reviewer",
               "comparison": {"status": "ahead", "files": [file_entry(filename="app.py",
                   patch="@@ -10 +10 @@\n-old_value\n+return value")]}}
    result = replies.recheck("owner/repo", finding, "b" * 40)
    assert result["verdict"] == expected
    assert fetched == [("owner/repo", "app.py", head * 40) for head in ("b", "a")]
    model, system, user, tool = requests[0]
    assert model == "configured-reviewer" and tool is replies._CHECK
    assert "Missing guard" in user and "No validation" in user and "return value" in user
    assert "complete changed lines" in system
    assert tool.input_schema["properties"]["verdict"]["enum"] == ["fixed", "present", "unsure"]


@pytest.mark.parametrize("status,file,expected", [
    ("ahead", file_entry(filename="app.py", status="removed", patch="@@ -1 +0,0 @@\n-old"), "unsure"),
    ("ahead", {"filename": "app.py", "status": "removed"}, "unsure"),
    ("ahead", {"filename": "new.py", "previous_filename": "app.py", "status": "renamed"}, "unsure"),
    ("ahead", {"filename": "else.py", "status": "removed"}, "unsure"),
    ("diverged", {"filename": "app.py", "status": "removed"}, "unsure")])
def test_missing_file_cannot_supply_fix_quote(live, monkeypatch, status, file, expected):
    monkeypatch.setattr(replies, "_read_file", lambda *a: None)
    monkeypatch.setattr(github, "get", lambda *a: httpx.Response(200,
        json={"status": status, "files": [file]}, request=httpx.Request("GET", "https://api.github.com")))
    assert replies.recheck("owner/repo", {"path": "app.py", "head_sha": "a" * 40,
        "comparison": {"status": status, "files": [file]}}, "b" * 40)["verdict"] == expected


def test_strict_requires_later_recheck_head(database):
    fid = seed(database)
    with db.connect(database) as conn:
        acceptance._record(conn, fid, "addressed", "true", "b" * 40, db._now_iso())
        acceptance._record(conn, fid, "resolved", "true", "thread", db._now_iso())
    assert acceptance.stats(database)["totals"]["strict_accepted"] == 0
    with db.connect(database) as conn:
        conn.execute("INSERT INTO bot_actions(finding_id,kind,value,evidence_id,created_at) VALUES (?, 'recheck','fixed',?,?)",
                     (fid, "a" * 40, db._now_iso()))
    assert acceptance.stats(database)["totals"]["strict_accepted"] == 0
    with db.connect(database) as conn:
        conn.execute("UPDATE bot_actions SET evidence_id=?", ("b" * 40,))
    assert acceptance.stats(database)["totals"]["strict_accepted"] == 1


def test_missing_write_token_never_falls_back(live, monkeypatch):
    monkeypatch.delenv("ARGUS_PUBLISH_TOKEN")
    with pytest.raises(RuntimeError):
        replies.reply("owner/repo", 3, 101, "Noted.")
    with pytest.raises(RuntimeError):
        replies.resolve_thread("owner/repo", "thread")


def test_transport_failure_does_not_retry(live, monkeypatch):
    calls = []
    def post(*a, **k):
        calls.append(1)
        raise httpx.ConnectError("failed")
    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(httpx.ConnectError):
        replies.reply("owner/repo", 3, 101, "Noted.")
    assert calls == [1]


def test_schema_upgrade_preserves_rows(database):
    fid = seed(database)
    with sqlite3.connect(database) as conn:
        conn.execute("ALTER TABLE published_findings DROP COLUMN gh_thread_id")
        conn.execute("DROP TABLE reply_texts")
        conn.execute("DROP TABLE reply_attempts")
        conn.execute("DROP TABLE bot_actions")
    db.init_db(database)
    db.init_db(database)
    assert rows(database, "published_findings")[0]["id"] == fid
    assert rows(database, "published_findings")[0]["gh_thread_id"] is None
    assert not rows(database, "bot_actions")


@pytest.mark.parametrize("model", ["reviewer", "provider/reviewer"])
def test_auto_call_uses_shared_slot(database, monkeypatch, model):
    monkeypatch.setenv("ARGUS_PROVIDER", "gateway")
    from types import SimpleNamespace
    sent = []
    def create(**kw):
        sent.append(kw)
        assert client.llm_in_flight() == 1
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name="recheck_finding",
            input={"verdict": "fixed", "quote": "return"})])
    monkeypatch.setattr(client, "build_client", lambda *args: SimpleNamespace(messages=SimpleNamespace(create=create)))
    monkeypatch.setenv(client.ENV_OPENROUTER_KEY, "fake-model-key")
    def post(url, **kw):
        sent.append(kw["json"])
        assert client.llm_in_flight() == 1
        return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [{"function": {
            "name": "recheck_finding", "arguments": '{"verdict":"fixed","quote":"return"}'}}]}}]},
            request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx, "post", post)
    assert client.complete_tool(model, "system", "user", replies._CHECK)["verdict"] == "fixed"
    expected = ("auto" if "/" in model else
                {"type": "auto", "disable_parallel_tool_use": True})
    assert sent[0]["tool_choice"] == expected
    assert client.llm_in_flight() == 0


def test_auto_call_accepts_prose_json(database, monkeypatch):
    monkeypatch.setattr(client, "_openrouter_request", lambda payload: {
        "choices": [{"message": {"content": '{"verdict":"fixed","quote":"return"}'}}]})
    assert client.complete_tool("provider/reviewer", "system", "user", replies._CHECK) == {
        "verdict": "fixed", "quote": "return"}


@pytest.mark.parametrize("document", ["mutation { deleteRepository { id } }", "query { viewer { login } }",
                                     replies._RESOLVE + " mutation { x }"])
def test_transport_refuses_other_documents(live, document):
    with pytest.raises(ValueError):
        replies._write("owner/repo", "https://api.github.com/graphql",
                       {"query": document, "variables": {"id": "thread"}})


def test_recheck_file_uses_read_token(live, monkeypatch):
    import base64
    calls = []
    def get(url, **kw):
        calls.append((url, kw["params"]["ref"]))
        assert kw["headers"]["Authorization"] == "Bearer read-secret"
        assert kw["params"]["ref"] in {"a" * 40, "b" * 40}
        return httpx.Response(200, json={"encoding": "base64",
            "content": base64.b64encode(b"return value").decode()}, request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(client, "complete_tool", lambda *a: {"verdict": "fixed", "quote": "return value"})
    assert replies.recheck("owner/repo", {"path": "app.py", "line": 1, "head_sha": "a" * 40,
        "comparison": {"status": "ahead", "files": [file_entry(filename="app.py",
            patch="@@ -1 +1 @@\n-old_value\n+return value")]}}, "b" * 40)["verdict"] == "fixed"
    assert calls == [("https://api.github.com/repos/owner/repo/contents/app.py", head * 40) for head in ("b", "a")]


def test_default_limit_is_ten(live, monkeypatch):
    _, checks, writes, _ = setup_loop(live, monkeypatch, verdict="present")
    monkeypatch.delenv("ARGUS_RECHECK_MAX_PER_RUN", raising=False)
    for _ in range(10):
        seed(live)
    monkeypatch.setattr(github, "graphql_read", lambda *a: {"repository": {"pullRequest": {
        "reviewThreads": {"nodes": [{"id": f"thread-{cid}", "isResolved": False,
        "comments": {"nodes": [{"databaseId": cid}]}} for cid in range(101, 112)]}}}})
    acceptance.collect(live)
    assert len(checks) == 10 and not writes
    acceptance.collect(live)
    assert len(checks) == 11


def test_confirmed_fix_skips_reopened_thread_checks(live, monkeypatch):
    _, checks, writes, _ = setup_loop(live, monkeypatch)
    acceptance.collect(live)
    monkeypatch.setattr(github, "get_json_cached", lambda *a: (
        {"state": "open", "head": {"sha": "c" * 40}}, ""))
    acceptance.collect(live)
    assert [head for _, head in checks] == ["b" * 40]
    assert len([kind for kind, _ in writes if kind == "reply"]) == 1
    assert len([kind for kind, _ in writes if kind == "resolve"]) == 1
