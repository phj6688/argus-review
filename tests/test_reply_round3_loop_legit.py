"""Conservative checks retain loose acceptance and answer requests."""
import base64
import difflib
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
OLD = [f"line_{n} = {n}" for n in range(1, 10)] + ["    print(secret)", "    audit(user)", "    return check(user)"] + [f"tail_{n} = {n}" for n in range(40)]


def pull(monkeypatch, head, author="author"):
    monkeypatch.setattr(github, "get_json_cached", lambda *a: (
        dict(state="open", head=dict(sha=head * 40), user=dict(login=author)), ""))


def add_evidence(path, evidence):
    with sqlite3.connect(path) as conn:
        review_id, fingerprint = conn.execute("SELECT review_id, fingerprint FROM published_findings").fetchone()
        conn.execute("INSERT INTO findings(review_id,file,line,severity,confidence,title,description,proposed_fix,"
                     "is_bug,verdict_is_real,verdict_confidence,fingerprint,evidence) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (review_id, "app.py", 10, "major", 0.9, "Secret printed", "secret in logs", "remove",
                      1, 1, 0.9, fingerprint, evidence))


def serve(monkeypatch, new):
    patch = "\n".join(list(difflib.unified_diff(OLD, new, lineterm="", n=3))[2:])

    def fake_get(url, params=None):
        if "compare" in url:
            return httpx.Response(200, json=dict(status="ahead", files=[file_entry(filename="app.py", patch=patch)]),
                                  request=httpx.Request("GET", API + url))
        content = base64.b64encode("\n".join(new).encode()).decode()
        return httpx.Response(200, json=dict(encoding="base64", content=content), request=httpx.Request("GET", API + url))

    monkeypatch.setattr(github, "get", fake_get)


@pytest.mark.parametrize("shape", ["delete", "veto"])
def test_unearned_fix_keeps_loose_and_loop(live, monkeypatch, shape):
    _, checks, writes, comments = setup_loop(live, monkeypatch, text="recheck please", changed=False)
    monkeypatch.setattr(replies, "recheck", REAL_RECHECK)
    if shape == "delete":
        add_evidence(live, "print(secret)")
        new = OLD[:9] + OLD[10:]
        quote = "audit(user)"
    else:
        add_evidence(live, "print(secret)\naudit(user)")
        new = OLD[:9] + ["    logger.info('login')"] + OLD[10:]
        quote = "logger.info('login')"
    serve(monkeypatch, new)
    monkeypatch.setattr(client, "complete_tool", lambda *a: dict(verdict="fixed", quote=quote))
    for head in "bcd":
        pull(monkeypatch, head)
        acceptance.collect(live)
    totals = acceptance.stats(live)["totals"]
    assert [a["value"] for a in rows(live, "bot_actions") if a["kind"] == "recheck"] == ["unsure"] * 3
    assert totals["strict_accepted"] == 0 and totals["loose_accepted"] == 1
    assert any(w[0] == "reply" for w in writes)
