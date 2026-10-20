"""New probes on the fixed loop: suppression, resolved-thread cost, recovery gaps, trust. Fakes only."""
import base64
import sqlite3

import httpx

from argus import acceptance, client, github, replies
import test_replies as fixtures
from test_acceptance import rows
from test_replies import setup_loop
from reply_helpers import file_entry

database = fixtures.database
live = fixtures.live
REAL_REPLY = replies.reply
REAL_RECHECK = replies.recheck
API = "https://api.github.com"


def pull(monkeypatch, head, author="author"):
    monkeypatch.setattr(github, "get_json_cached", lambda *a: (
        dict(state="open", head=dict(sha=head * 40), user=dict(login=author)), ""))


def threads(monkeypatch, resolved):
    monkeypatch.setattr(github, "graphql_read", lambda *a: dict(repository=dict(pullRequest=dict(
        reviewThreads=dict(nodes=[dict(id="thread", isResolved=resolved,
                                       comments=dict(nodes=[dict(databaseId=101)]))])))))


def compare(monkeypatch, changed):
    files = [dict(filename="app.py", status="removed")] if changed else []
    monkeypatch.setattr(github, "get", lambda *a, **k: httpx.Response(200, json=dict(status="ahead", files=files),
                        request=httpx.Request("GET", API + "/repos/owner/repo/compare/a...b")))


def add_evidence(path, evidence):
    with sqlite3.connect(path) as conn:
        review_id, fingerprint = conn.execute("SELECT review_id, fingerprint FROM published_findings").fetchone()
        conn.execute("INSERT INTO findings(review_id,file,line,severity,confidence,title,description,proposed_fix,"
                     "is_bug,verdict_is_real,verdict_confidence,fingerprint,evidence) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (review_id, "app.py", 10, "major", 0.9, "Missing Check", "eval of input", "validate",
                      1, 1, 0.9, fingerprint, evidence))


def test_explained_rejection_suppression_survives_nearby_edit(live, monkeypatch):
    _, checks, writes, _ = setup_loop(live, monkeypatch, text="Rejected because callers already validate this input",
                                      changed=False, verdict="present")
    pull(monkeypatch, "a")
    acceptance.collect(live)
    seeded = len(rows(live, "known_fps_live"))
    threads(monkeypatch, True)
    compare(monkeypatch, True)
    pull(monkeypatch, "b")
    acceptance.collect(live)
    remaining = len(rows(live, "known_fps_live"))
    assert seeded == 1 and remaining == 1


def test_confirmed_fix_stops_checks_at_later_heads(live, monkeypatch):
    _, checks, writes, _ = setup_loop(live, monkeypatch, resolved=True)
    for head in "bcdef":
        pull(monkeypatch, head)
        acceptance.collect(live)
    assert len(checks) == 1


def test_listing_failure_waits_for_reply_recovery(live, monkeypatch):
    _, checks, writes, comments = setup_loop(live, monkeypatch, text="Deferred until next release", changed=False)
    server = []

    def post(url, **kw):
        server.append(kw["json"]["body"])
        comments.append(dict(comments[0], id=5000 + len(server), body=kw["json"]["body"],
                             user=dict(login="publisher", type="Bot")))
        raise httpx.ReadTimeout("timed out", request=httpx.Request("POST", url))

    monkeypatch.setattr(replies, "reply", REAL_REPLY)
    monkeypatch.setattr(httpx, "post", post)
    acceptance.collect(live)
    real_list = github.list_cached

    def broken(*a, **k):
        raise httpx.ConnectError("listing failed")

    monkeypatch.setattr(github, "list_cached", broken)
    acceptance.collect(live)
    monkeypatch.setattr(github, "list_cached", real_list)
    assert len(server) == 1
    acceptance.collect(live)
    assert len(server) == 1
    assert len(rows(live, "bot_actions")) == 1


def test_untrusted_rejection_has_no_effect(live, monkeypatch):
    _, checks, writes, comments = setup_loop(live, monkeypatch,
                                             text="Rejected because this is intended behaviour here", changed=False)
    comments[0].update(user=dict(login="drive-by-user"), author_association="NONE")
    acceptance.collect(live)
    assert not writes
    assert not rows(live, "known_fps_live")
    assert acceptance.stats(live)["totals"]["rejected"] == 0


def test_veto_reply_quotes_the_surviving_defect(live, monkeypatch):
    _, checks, writes, _ = setup_loop(live, monkeypatch, text="recheck", changed=False)
    add_evidence(live, "result = eval(user_input)")
    monkeypatch.setattr(replies, "recheck", REAL_RECHECK)
    code = "\n".join(["x = 1"] * 9 + ["value = validate(user_input)", "y = 0", "z = 0"] + ["y = 2"] * 40
                     + ["result = eval(user_input)"])

    def fake_get(url, params=None):
        if "compare" in url:
            return httpx.Response(200, json=dict(status="ahead", files=[file_entry(filename="app.py",
                patch="@@ -10 +10 @@\n-result = eval(user_input)\n+value = validate(user_input)")]),
                                  request=httpx.Request("GET", API + url))
        content = base64.b64encode(code.encode()).decode()
        return httpx.Response(200, json=dict(encoding="base64", content=content), request=httpx.Request("GET", API + url))

    monkeypatch.setattr(github, "get", fake_get)
    monkeypatch.setattr(client, "complete_tool", lambda *a: dict(verdict="fixed", quote="value = validate(user_input)"))
    acceptance.collect(live)
    body = [w[1].splitlines()[0] for w in writes if w[0] == "reply"]
    assert body == ["Not sure at bbbbbbb."]


def test_explained_rejection_suppression_survives_comment_added_above(live, monkeypatch):
    _, checks, writes, _ = setup_loop(live, monkeypatch, text="Rejected because callers already validate this input",
                                      changed=False, verdict="present")
    pull(monkeypatch, "a")
    acceptance.collect(live)
    seeded = len(rows(live, "known_fps_live"))
    threads(monkeypatch, True)
    patch = "@@ -6,6 +6,7 @@\n c6\n c7\n c8\n+# callers validate this input\n c9\n c10\n c11"
    files = [file_entry(filename="app.py", status="modified", patch=patch)]
    monkeypatch.setattr(github, "get", lambda *a, **k: httpx.Response(200, json=dict(status="ahead", files=files),
                        request=httpx.Request("GET", API + "/repos/owner/repo/compare/a...b")))
    pull(monkeypatch, "b")
    acceptance.collect(live)
    remaining = len(rows(live, "known_fps_live"))
    assert seeded == 1 and remaining == 1
