"""Complete fix quotes and clear unchanged-code replies."""
import difflib

import pytest

from argus import acceptance, client, replies
import test_replies as fixtures
from test_acceptance import rows
from test_replies import setup_loop
from reply_helpers import file_entry

database = fixtures.database
live = fixtures.live
REAL_RECHECK = replies.recheck
TOP = ["import os", ""] + [f"line_{n} = {n}" for n in range(30)]
TAIL = [f"tail_{n} = {n}" for n in range(60)]


@pytest.mark.parametrize("quote", ["result = ast.literal_eval(user_input)", "ast.literal_eval(user_input)",
                                   "    result = ast.literal_eval(user_input)\n    return result"])
def test_fragment_quote(live, monkeypatch, quote):
    old = TOP + ["def run(user_input):", "    result = eval(user_input)", "    return result"] + TAIL
    new = TOP + ["def run(user_input):", "    result = ast.literal_eval(user_input)", "    return result"] + TAIL
    patch = "\n".join(list(difflib.unified_diff(old, new, lineterm="", n=3))[2:])
    finding = dict(path="app.py", head_sha="a" * 40, line=34, end_line=34, evidence="result = eval(user_input)",
                   comparison=dict(status="ahead", files=[file_entry(filename="app.py", patch=patch)]))
    monkeypatch.setattr(replies, "_read_file", lambda *a: "\n".join(new))
    monkeypatch.setattr(client, "complete_tool", lambda *a: dict(verdict="fixed", quote=quote))
    result = REAL_RECHECK("owner/repo", finding, "b" * 40)
    assert result["verdict"] == ("unsure" if quote == "ast.literal_eval(user_input)" else "fixed")


def test_request_on_unchanged_file(live, monkeypatch):
    _, checks, writes, _ = setup_loop(live, monkeypatch, text="recheck", changed=False)
    monkeypatch.setattr(replies, "recheck", REAL_RECHECK)
    acceptance.collect(live)
    assert [w[1].splitlines()[0] for w in writes] == ["No change to this code since aaaaaaa."]
    assert rows(live, "bot_actions")[0]["value"] == "unsure"


def test_unchanged_file_reply_retry_keeps_clear_answer(live, monkeypatch):
    import httpx
    _, _, writes, _ = setup_loop(live, monkeypatch, text="recheck", changed=False)
    monkeypatch.setattr(replies, "recheck", REAL_RECHECK)
    good = replies.reply
    def fail(*a):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(replies, "reply", fail)
    acceptance.collect(live)
    monkeypatch.setattr(replies, "reply", good)
    acceptance.collect(live)
    assert [w[1].splitlines()[0] for w in writes] == ["No change to this code since aaaaaaa."]
    assert [a["value"] for a in rows(live, "bot_actions") if a["kind"] == "recheck"] == ["unsure"]
