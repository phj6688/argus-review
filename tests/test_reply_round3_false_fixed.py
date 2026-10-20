"""Structural vetoes and remaining model judgment limits."""
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


def run_case(monkeypatch, old, new, first, last, quote, evidence, extra_files=()):
    patch = "\n".join(list(difflib.unified_diff(old, new, lineterm="", n=3))[2:])
    finding = dict(path="app.py", head_sha="a" * 40, line=first, end_line=last, evidence=evidence,
                   comparison=dict(status="ahead", files=[file_entry(filename="app.py", patch=patch), *extra_files]))
    monkeypatch.setattr(replies, "_read_file", lambda *a: "\n".join(new))
    monkeypatch.setattr(client, "complete_tool", lambda *a: dict(verdict="fixed", quote=quote))
    return REAL_RECHECK("owner/repo", finding, "b" * 40)


def test_extract_helper_far_away(live, monkeypatch):
    old = TOP + ["def run(user_input):", "    result = eval(user_input)", "    return result"] + TAIL
    new = (TOP + ["def run(user_input):", "    result = _evaluate(user_input)", "    return result"] + TAIL
           + ["def _evaluate(expression):", "    return eval(expression)"])
    result = run_case(monkeypatch, old, new, 34, 34, "result = _evaluate(user_input)", "result = eval(user_input)")
    # Semantic changes still depend on the model judging the defect correctly.
    assert result["verdict"] == "fixed"


def test_extract_helper_other_file_without_patch(live, monkeypatch):
    old = TOP + ["def run(user_input):", "    result = eval(user_input)", "    return result"] + TAIL
    new = TOP + ["def run(user_input):", "    result = helpers.evaluate(user_input)", "    return result"] + TAIL
    big = dict(filename="helpers.py", status="added")
    result = run_case(monkeypatch, old, new, 34, 34, "result = helpers.evaluate(user_input)",
                      "result = eval(user_input)", extra_files=[big])
    assert result["verdict"] != "fixed"


def test_short_flagged_line_moved_with_nearby_added_line(live, monkeypatch):
    old = TOP + ["def run(c):", "    exec(c)", "    return 0"] + TAIL
    new = TOP + ["def run(c):", "    logger.debug('running')", "    return 0"] + TAIL + ["def later(c):", "    exec(c)"]
    result = run_case(monkeypatch, old, new, 34, 34, "logger.debug('running')", "exec(c)")
    # Semantic changes still depend on the model judging the defect correctly.
    assert result["verdict"] == "fixed"


@pytest.mark.parametrize("evidence", ["", "def run(user_input):"])
def test_unchanged_defect_line_inside_range_after_cosmetic_edit(live, monkeypatch, evidence):
    old = TOP + ["def run(user_input):", "    result = eval(user_input)", "    return result"] + TAIL
    new = TOP + ["def run(user_input: str) -> object:", "    result = eval(user_input)", "    return result"] + TAIL
    result = run_case(monkeypatch, old, new, 33, 35, "def run(user_input: str) -> object:", evidence)
    # Semantic changes still depend on the model judging the defect correctly.
    assert result["verdict"] == "fixed"


def test_formatter_reflow_of_unchanged_defect(live, monkeypatch):
    old = TOP + ["def run(user_input):", "    result = eval(compile(user_input, filename, mode))", "    return result"] + TAIL
    new = TOP + ["def run(user_input):", "    result = eval(", "        compile(user_input, filename, mode)",
                 "    )", "    return result"] + TAIL
    result = run_case(monkeypatch, old, new, 34, 34, "compile(user_input, filename, mode)",
                      "result = eval(compile(user_input, filename, mode))")
    assert result["verdict"] != "fixed"


def test_helper_in_other_file_with_patch_still_needs_model_judgment(live, monkeypatch):
    old = TOP + ["def run(user_input):", "    result = eval(user_input)", "    return result"] + TAIL
    new = TOP + ["def run(user_input):", "    result = helpers.evaluate(user_input)", "    return result"] + TAIL
    other = file_entry(filename="helpers.py", status="added",
                 patch="@@ -0,0 +1,2 @@\n+def evaluate(expression):\n+    return eval(expression)")
    result = run_case(monkeypatch, old, new, 34, 34, "result = helpers.evaluate(user_input)",
                      "result = eval(user_input)", extra_files=[other])
    assert result["verdict"] == "fixed"


@pytest.mark.parametrize("before,after", [
    ('    password = "hunter2"', '    password = "hunter2"  # rotate later'),
    ("    result = eval(user_input)", "    result = eval(user_input.strip())"),
])
def test_in_place_edit_that_keeps_defect_still_needs_model_judgment(live, monkeypatch, before, after):
    old = TOP + ["def run(user_input):", before, "    return 0"] + TAIL
    new = TOP + ["def run(user_input):", after, "    return 0"] + TAIL
    result = run_case(monkeypatch, old, new, 34, 34, after.strip(), before.strip())
    assert result["verdict"] == "fixed"


def test_null_patch_in_other_file_is_unsure(live, monkeypatch):
    old = TOP + ["def run(user_input):", "    result = eval(user_input)", "    return 0"] + TAIL
    new = TOP + ["def run(user_input):", "    result = ast.literal_eval(user_input)", "    return 0"] + TAIL
    result = run_case(monkeypatch, old, new, 34, 34, "result = ast.literal_eval(user_input)",
                      "result = eval(user_input)", extra_files=[file_entry(filename="logo.png", patch=None)])
    assert result == dict(verdict="unsure", quote="")
