"""Real fixes and intentionally conservative verdicts."""
import difflib

import pytest

from argus import client, replies
import test_replies as fixtures
from reply_helpers import file_entry

database = fixtures.database
live = fixtures.live
REAL_RECHECK = replies.recheck

TOP = ["import os", ""] + [f"line_{n} = {n}" for n in range(30)]
TAIL = [f"tail_{n} = {n}" for n in range(60)]

CASES = [
    ("in-place replace, evidence is the line",
     ["def run(user_input):", "    result = eval(user_input)", "    return result"],
     ["def run(user_input):", "    result = ast.literal_eval(user_input)", "    return result"],
     34, 34, "result = ast.literal_eval(user_input)", "result = eval(user_input)", "fixed"),
    ("in-place replace, evidence is the call only",
     ["def run(user_input):", "    result = eval(user_input)", "    return result"],
     ["def run(user_input):", "    result = ast.literal_eval(user_input)", "    return result"],
     34, 34, "result = ast.literal_eval(user_input)", "eval(user_input)", "fixed"),
    ("add timeout argument",
     ["def fetch(url):", "    response = requests.get(url)", "    return response"],
     ["def fetch(url):", "    response = requests.get(url, timeout=10)", "    return response"],
     34, 34, "response = requests.get(url, timeout=10)", "response = requests.get(url)", "fixed"),
    ("add a guard above, flagged line unchanged",
     ["def parse(value):", "    number = int(value)", "    return number"],
     ["def parse(value):", "    if not value.isdigit():", "        raise ValueError(value)",
      "    number = int(value)", "    return number"],
     34, 34, "if not value.isdigit():", "number = int(value)", "unsure"),
    ("delete the flagged line",
     ["def login(user, secret):", "    print(secret)", "    return check(user, secret)"],
     ["def login(user, secret):", "    return check(user, secret)"],
     34, 34, "return check(user, secret)", "print(secret)", "unsure"),
    ("wrap two flagged lines in a with block",
     ["def load(path):", "    handle = open(path)", "    data = handle.read()", "    return data"],
     ["def load(path):", "    with open(path) as handle:", "        data = handle.read()", "    return data"],
     34, 35, "with open(path) as handle:", "handle = open(path)\ndata = handle.read()", "unsure"),
    ("unique evidence line kept beside a generic handler",
     ["def save(row):", "    try:", "        session.add(row)", "    except StorageError:", "        pass"],
     ["def save(row):", "    try:", "        session.add(row)", "    except StorageError:", "        logger.exception('save failed')", "        raise"],
     36, 37, "logger.exception('save failed')", "except StorageError:\n    pass", "unsure"),
    ("same unsafe call remains at another call site",
     ["def run(user_input):", "    result = eval(user_input)", "    return result"],
     ["def run(user_input):", "    result = ast.literal_eval(user_input)", "    return result"],
     34, 34, "result = ast.literal_eval(user_input)", "result = eval(user_input)", "fixed"),
]


@pytest.mark.parametrize("label,before,after,first,last,quote,evidence,expected", CASES, ids=[c[0] for c in CASES])
def test_legit_fix_shape(live, monkeypatch, label, before, after, first, last, quote, evidence, expected):
    old = TOP + before + TAIL
    new = TOP + after + TAIL
    if label.startswith("same unsafe call"):
        old = old + ["def other(text):", "    return eval(text) if False else 0", "x = eval(user_input)"]
        new = new + ["def other(text):", "    return eval(text) if False else 0", "x = eval(user_input)"]
    if label.startswith("unique evidence"):
        old = old + ["def other():", "    try:", "        work()", "    except Exception:", "        logger.warning('x')"]
        new = new + ["def other():", "    try:", "        work()", "    except Exception:", "        logger.warning('x')"]
    patch = "\n".join(list(difflib.unified_diff(old, new, lineterm="", n=3))[2:])
    finding = dict(path="app.py", head_sha="a" * 40, line=first, end_line=last, evidence=evidence,
                   comparison=dict(status="ahead", files=[file_entry(filename="app.py", patch=patch)]))
    monkeypatch.setattr(replies, "_read_file", lambda *a: "\n".join(old if a[2] == "a" * 40 else new))
    calls = []

    def model(*a):
        calls.append(1)
        return dict(verdict="fixed", quote=quote)

    monkeypatch.setattr(client, "complete_tool", model)
    result = REAL_RECHECK("owner/repo", finding, "b" * 40)
    assert result["verdict"] == expected
    assert len(calls) == (expected == "fixed")
    if expected == "unsure":
        assert result["quote"] == ""
