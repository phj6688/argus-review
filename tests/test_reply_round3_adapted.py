"""Conservative replies, unreadable contents, and trivial changes."""
import base64
import sqlite3

import httpx
import pytest

from argus import acceptance, client, github, replies
import test_replies as fixtures
from test_acceptance import rows
from test_replies import setup_loop
from reply_helpers import file_entry

database = fixtures.database
live = fixtures.live
REAL_RECHECK = replies.recheck
API = "https://api.github.com"


def pull(monkeypatch, head, author="author"):
    monkeypatch.setattr(github, "get_json_cached", lambda *a: (
        dict(state="open", head=dict(sha=head * 40), user=dict(login=author)), ""))


def add_evidence(path, evidence):
    with sqlite3.connect(path) as conn:
        review_id, fingerprint = conn.execute("SELECT review_id, fingerprint FROM published_findings").fetchone()
        conn.execute("INSERT INTO findings(review_id,file,line,severity,confidence,title,description,proposed_fix,"
                     "is_bug,verdict_is_real,verdict_confidence,fingerprint,evidence) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (review_id, "app.py", 10, "major", 0.9, "Missing Check", "eval of input", "validate",
                      1, 1, 0.9, fingerprint, evidence))


def router(monkeypatch, code, patch, contents=None):
    reads = []

    def fake_get(url, params=None):
        if "compare" in url:
            return httpx.Response(200, json=dict(status="ahead", files=[file_entry(filename="app.py", patch=patch)]),
                                  request=httpx.Request("GET", API + url))
        reads.append(url)
        body = contents if contents is not None else dict(
            encoding="base64", content=base64.b64encode(code.encode()).decode())
        return httpx.Response(200, json=body, request=httpx.Request("GET", API + url))

    monkeypatch.setattr(github, "get", fake_get)
    return reads


def test_veto_reply_quotes_the_surviving_defect(live, monkeypatch):
    _, checks, writes, _ = setup_loop(live, monkeypatch, text="recheck", changed=False)
    pull(monkeypatch, "b")
    add_evidence(live, "result = eval(user_input)")
    monkeypatch.setattr(replies, "recheck", REAL_RECHECK)
    code = "\n".join(["x = 1"] * 9 + ["value = validate(user_input)", "y = 0", "z = 0"] + ["y = 2"] * 40
                     + ["result = eval(user_input)"])
    router(monkeypatch, code, "@@ -10 +10 @@\n-result = eval(user_input)\n+value = validate(user_input)")
    monkeypatch.setattr(client, "complete_tool", lambda *a: dict(verdict="fixed", quote="value = validate(user_input)"))
    acceptance.collect(live)
    body = [w[1].splitlines()[0] for w in writes if w[0] == "reply"]
    assert body and body[0] == "Not sure at bbbbbbb." and "validate" not in body[0]


@pytest.mark.parametrize("contents", [dict(encoding="none", content="", size=2_000_000), [], dict(type="submodule")])
def test_large_file_contents_recorded_once(live, monkeypatch, contents):
    _, checks, writes, _ = setup_loop(live, monkeypatch, text="recheck", changed=False)
    pull(monkeypatch, "b")
    monkeypatch.setattr(replies, "recheck", REAL_RECHECK)
    reads = router(monkeypatch, "", "@@ -10 +10 @@\n-old_line\n+new_line_here", contents=contents)
    monkeypatch.setattr(client, "complete_tool", lambda *a: dict(verdict="present", quote=""))
    attempts = []
    for _ in range(3):
        acceptance.collect(live)
        attempts.append(acceptance.stats(live)["last_run"]["rechecks_attempted"])
    actions = [(a["kind"], a["value"]) for a in rows(live, "bot_actions")]
    assert len(reads) == 1 and attempts == [1, 0, 0] and ("recheck", "unsure") in actions


@pytest.mark.parametrize("quote", ["}", "return", "else:", ")"])
def test_trivial_replacement_line_is_not_a_fix(live, monkeypatch, quote):
    code = ["x = 1"] * 33 + ["    if (ok) {", "        run(eval(input))", "    " + quote] + ["y = 2"] * 40
    content = base64.b64encode("\n".join(code).encode()).decode()
    monkeypatch.setattr(github, "get", lambda *a, **k: httpx.Response(
        200, json=dict(encoding="base64", content=content), request=httpx.Request("GET", API + "/x")))
    monkeypatch.setattr(client, "complete_tool", lambda *a: dict(verdict="fixed", quote=quote))
    # Only the closing flagged line changes; the defect line stays as context.
    patch = "@@ -35,2 +35,2 @@\n         run(eval(input))\n-    }\n+    " + quote
    finding = dict(path="app.py", head_sha="a" * 40, line=34, end_line=36, evidence="",
                   comparison=dict(status="ahead", files=[file_entry(filename="app.py", patch=patch)]))
    result = REAL_RECHECK("owner/repo", finding, "b" * 40)
    assert result["verdict"] != "fixed"
