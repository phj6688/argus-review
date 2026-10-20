"""Moved and partly changed findings cannot earn false fixes."""

import base64
import difflib
import json

import httpx
import pytest

from argus import client, github, replies
import test_replies as fixtures
from reply_helpers import file_entry

database = fixtures.database
live = fixtures.live
REAL_RECHECK = replies.recheck
API = "https://api.github.com"

HEAD = ["import os", ""] + [f"line_{n} = {n}" for n in range(30)]
DEFECT = ["def run(user_input):", "    result = eval(user_input)", "    return result"]
TAIL = [f"tail_{n} = {n}" for n in range(60)]


def unified_patch(old, new):
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=3))
    return "\n".join(lines[2:])


def serve(monkeypatch, current):
    def fake_get(url, params=None):
        content = base64.b64encode(current.encode()).decode()
        return httpx.Response(200, json=dict(encoding="base64", content=content),
                              request=httpx.Request("GET", API + url))
    monkeypatch.setattr(github, "get", fake_get)


def confident_model(monkeypatch, seen):
    """A model that claims a fix and quotes the first full line of the anchored range."""
    def model(model_id, system, user, tool):
        payload = json.loads(user)
        seen.append(payload)
        code = payload["current_code"].splitlines()
        first = payload["current_range"][0]
        return dict(verdict="fixed", quote=code[first - payload["current_start_line"]].strip())
    monkeypatch.setattr(client, "complete_tool", model)


def run_case(monkeypatch, original, current, line, end, evidence):
    serve(monkeypatch, current)
    seen = []
    confident_model(monkeypatch, seen)
    patch = unified_patch(original, current)
    files = [file_entry(filename="app.py", status="modified", patch=patch)]
    finding = dict(path="app.py", title="eval on user input", description="eval of untrusted input",
                   evidence=evidence, line=line, end_line=end, model="m", head_sha="a" * 40,
                   comparison=dict(status="ahead", files=files))
    result = REAL_RECHECK("owner/repo", finding, "b" * 40)
    window = seen[0]["current_code"] if seen else ""
    return result, window, (seen[0]["current_range"] if seen else None), patch


MOVED_BLOCK = "\n".join(HEAD + TAIL + DEFECT)
RENAMED_MOVE = "\n".join(HEAD + TAIL + ["def run(user_input):", "    output = eval(user_input)", "    return output"])


@pytest.mark.parametrize("label,current,evidence", [
    ("verbatim move, long evidence", MOVED_BLOCK, "result = eval(user_input)"),
    ("verbatim move, short evidence", MOVED_BLOCK, "eval(user_input)"),
    ("verbatim move, view-prefixed evidence", MOVED_BLOCK, "34 +     result = eval(user_input)"),
    ("move with renamed variable", RENAMED_MOVE, "result = eval(user_input)"),
], ids=["long-evidence", "short-evidence", "view-prefix", "renamed-move"])
def test_moved_defect_far_away(live, monkeypatch, label, current, evidence):
    original = "\n".join(HEAD + DEFECT + TAIL)
    result, window, rng, _ = run_case(monkeypatch, original, current, 34, 34, evidence)
    assert result["verdict"] != "fixed"


def test_partial_overlap_truncates_long_range(live, monkeypatch):
    body = [f"step_{n} = {n}" for n in range(60)]
    body[38] = "    result = eval(user_input)"
    original = "\n".join(HEAD + body + TAIL)
    current_lines = HEAD + body + TAIL
    current_lines[33] = "step_1 = 'changed near the start of the range'"
    current = "\n".join(current_lines)
    result, window, rng, _ = run_case(monkeypatch, original, current, 33, 75, "eval(user_input)")
    assert rng == [33, 75] and "eval(user_input)" in window
    assert result["verdict"] == "unsure"


def test_context_only_overlap_lets_any_added_line_be_the_fix(live, monkeypatch):
    original = "\n".join(HEAD + DEFECT + TAIL)
    current = "\n".join(HEAD + DEFECT[:2] + ["    log.debug('extra')"] * 15 + DEFECT[2:] + TAIL)
    result, window, rng, _ = run_case(monkeypatch, original, current, 34, 34, "eval(user_input)")
    assert rng is None and not window
    assert result["verdict"] == "unsure"
