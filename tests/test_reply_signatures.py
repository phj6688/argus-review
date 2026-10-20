"""Only code that identifies the flagged site can veto a re-check."""

import difflib

import pytest

from argus import client, replies
from reply_helpers import file_entry


@pytest.fixture
def check(monkeypatch):
    calls, reads = [], []

    def run(old, new, *, first=1, last=None, evidence="", extras=(), unreadable=False):
        def read(repo, path, head):
            reads.append((repo, path, head))
            return None if unreadable and head == "a" * 40 else "\n".join(old if head == "a" * 40 else new)

        def model(*args):
            calls.append(args)
            return dict(verdict="fixed", quote=new[first - 1])

        monkeypatch.setattr(replies, "_read_file", read)
        monkeypatch.setattr(client, "complete_tool", model)
        patch = "\n".join(list(difflib.unified_diff(old, new, lineterm=""))[2:])
        finding = dict(path="app.py", head_sha="a" * 40, line=first, end_line=last,
                       evidence=evidence, comparison=dict(status="ahead", files=[
                           file_entry("app.py", patch), *extras]))
        return replies.recheck("owner/repo", finding, "b" * 40)

    run.calls, run.reads = calls, reads
    return run


@pytest.mark.parametrize("expression", ['source.get("patch")', 'source.startswith("+")',
                                        'source.splitlines()', 'source.append(section)',
                                        '"### Unverified (the verifier disagreed)"'])
def test_repeated_subexpression_kept_by_refactor_reaches_model(check, expression):
    old = [f"result = {expression}", f"other = {expression}", f"another = {expression}"]
    new = [f"result = validate({expression})", *old[1:]]
    assert check(old, new) == dict(verdict="fixed", quote=new[0])
    assert len(check.calls) == 1
    assert set(check.reads) == {("owner/repo", "app.py", head * 40) for head in ("a", "b")}


@pytest.mark.parametrize("reflow", [False, True])
def test_repeated_evidence_kept_within_range_reaches_model(check, reflow):
    evidence = 'return {"verdict": "unsure", "quote": ""}'
    duplicate = ['return {', '"verdict": "unsure", "quote": ""}'] if reflow else [evidence]
    old = ["result = unsafe(value)", evidence, "context", *duplicate]
    new = ["result = validate(value)", *old[1:]]
    assert check(old, new, last=2, evidence=evidence) == dict(verdict="fixed", quote=new[0])
    assert len(check.calls) == 1


def test_unique_sql_subexpression_in_place_reaches_model(check):
    old = ["query = COALESCE(balance, 0) + adjustment"]
    new = ["query = COALESCE(balance, 0) + validated_adjustment"]
    assert check(old, new) == dict(verdict="fixed", quote=new[0])
    assert len(check.calls) == 1


@pytest.mark.parametrize("moved", ["output = eval(user_input)", "output = eval( user_input )",
                                   "output = eval(\n    user_input\n)"])
def test_unique_call_moved_to_other_file_vetoes(check, moved):
    extra = file_entry("helper.py", "@@ -0,0 +1," + str(len(moved.splitlines())) + " @@\n+"
                       + moved.replace("\n", "\n+"))
    assert check(["result = eval(user_input)"], ["result = validate(user_input)"],
                 extras=[extra]) == dict(verdict="unsure", quote="")
    assert not check.calls


@pytest.mark.parametrize("padding,multiline,expected", [(0, False, "fixed"), (1, False, "fixed"),
                                                       (2, False, "unsure"), (0, True, "unsure"),
                                                       (10, False, "unsure")])
def test_move_position_uses_each_match_inside_long_added_run(check, padding, multiline, expected):
    old = ["result = eval(user_input)", "context", "old_tail"]
    moved = ["output = eval(", "    user_input", ")"] if multiline else ["output = eval(user_input)"]
    new = ["result = validate(user_input)", "context", *[f"value_{n} = 1" for n in range(padding)],
           *moved, *[f"tail_{n} = 2" for n in range(20)]]
    assert check(old, new) == dict(verdict=expected, quote=new[0] if expected == "fixed" else "")
    assert len(check.calls) == (expected == "fixed")


def test_unique_flagged_line_moved_to_other_file_vetoes(check):
    old = ["permission = ADMIN_LEVEL + override"]
    extra = file_entry("helper.py", "@@ -0,0 +1,2 @@\n+permission =\n+ ADMIN_LEVEL + override")
    assert check(old, ["permission = USER_LEVEL"], extras=[extra]) == dict(verdict="unsure", quote="")
    assert not check.calls


def test_unique_evidence_kept_within_range_vetoes(check):
    old = ["result = unsafe(value)", 'return {"verdict": "unsure", "quote": ""}']
    assert check(old, ["result = validate(value)", old[1]], last=2,
                 evidence=old[1]) == dict(verdict="unsure", quote="")
    assert not check.calls


def test_unreadable_reviewed_file_keeps_generic_call_veto(check):
    old = ['result = source.get("patch")', 'other = source.get("patch")']
    new = ['result = validated.get("patch")', old[1]]
    assert check(old, new, unreadable=True) == dict(verdict="unsure", quote="")
    assert not check.calls


@pytest.mark.parametrize("defect,duplicate,moved", [
    ("result = eval(user_input)", "other = eval(user_input)", "output = eval(user_input)"),
    ("result = eval(user_input)", "other = eval(\n user_input\n)", "output = eval(user_input)"),
    ("permission = ADMIN_LEVEL + override", "permission = ADMIN_LEVEL + override",
     "permission = ADMIN_LEVEL + override"),
])
def test_defect_repeated_elsewhere_and_moved_is_left_to_model(check, defect, duplicate, moved):
    old = [defect, "context", *duplicate.splitlines()]
    new = ["result = validate(user_input)", *old[1:]]
    extra = file_entry("helper.py", "@@ -0,0 +1 @@\n+" + moved)
    assert check(old, new, extras=[extra]) == dict(verdict="fixed", quote=new[0])
    assert len(check.calls) == 1


def test_all_old_call_occurrences_on_deleted_flagged_lines_still_veto(check):
    old = ["result = eval(user_input)", "other = eval(user_input)"]
    extra = file_entry("helper.py", "@@ -0,0 +1 @@\n+output = eval(user_input)")
    assert check(old, ["result = validate(user_input)"], last=2,
                 extras=[extra]) == dict(verdict="unsure", quote="")
    assert not check.calls


@pytest.mark.parametrize("destination", ["same_file", "other_file"])
@pytest.mark.parametrize("with_evidence", [False, True])
@pytest.mark.parametrize("reflow", [False, True])
def test_all_old_line_occurrences_in_flagged_range_still_veto_moves(
        check, destination, with_evidence, reflow):
    defect = "permission = ADMIN_LEVEL + override"
    old = ["header", "setup", defect, "branch", defect,
           *[f"context_{n}" for n in range(41)]]
    new = [*old[:2], "permission = level_for(admin)", *old[5:]]
    moved = ["permission =", " ADMIN_LEVEL + override"] if reflow else [defect]
    extras = []
    if destination == "same_file":
        new.extend(moved)
    else:
        extras.append(file_entry("helper.py", f"@@ -0,0 +1,{len(moved)} @@\n+"
                                 + "\n+".join(moved)))
    assert check(old, new, first=3, last=5, evidence=defect if with_evidence else "",
                 extras=extras) == dict(verdict="unsure", quote="")
    assert not check.calls


def test_all_old_evidence_occurrences_in_flagged_range_still_veto(check):
    evidence = "permission = ADMIN_LEVEL + override"
    old = ["result = unsafe(value)", evidence, "branch", evidence]
    new = ["result = validate(value)", *old[1:]]
    assert check(old, new, last=4, evidence=evidence) == dict(verdict="unsure", quote="")
    assert not check.calls


@pytest.mark.parametrize("exit_rule", ["unchanged", "no_nearby_addition", "empty_evidence"])
def test_cheap_exits_skip_reviewed_file_read(check, exit_rule):
    old = ["permission = ADMIN_LEVEL + override", "other = unsafe(value)"]
    if exit_rule == "unchanged":
        new = [old[0], "other = validate(value)"]
    elif exit_rule == "no_nearby_addition":
        new = [old[1]]
    else:
        new = [old[0], "other = validate(value)"]
    last = 2 if exit_rule == "empty_evidence" else 1
    assert check(old, new, last=last) == dict(verdict="unsure", quote="")
    assert ("owner/repo", "app.py", "a" * 40) not in check.reads
    assert not check.calls


@pytest.mark.parametrize("suffix", ["other_eval(user_input)", "literal_eval(user_input)"])
def test_old_call_count_keeps_left_word_boundary(check, suffix):
    old = ["result = eval(user_input)", "other = " + suffix]
    extra = file_entry("helper.py", "@@ -0,0 +1 @@\n+output = eval(user_input)")
    assert check(old, ["result = validate(user_input)", old[1]],
                 extras=[extra]) == dict(verdict="unsure", quote="")
    assert not check.calls


@pytest.mark.parametrize("padding,expected", [(0, "fixed"), (1, "unsure")])
def test_moved_line_position_uses_match_inside_long_added_run(check, padding, expected):
    defect = "permission = ADMIN_LEVEL + override"
    old = [defect, "context_a", "context_b", "old_tail"]
    new = ["permission = USER_LEVEL", "context_a", "context_b", *[f"value_{n} = 1" for n in range(padding)],
           defect, *[f"tail_{n} = 2" for n in range(20)]]
    assert check(old, new) == dict(verdict=expected, quote=new[0] if expected == "fixed" else "")
    assert len(check.calls) == (expected == "fixed")


def test_old_evidence_count_requires_whole_line_boundaries(check):
    evidence = "permission = ADMIN_LEVEL + override"
    old = ["result = unsafe(value)", evidence, "prefix_" + evidence]
    assert check(old, ["result = validate(value)", *old[1:]],
                 evidence=evidence) == dict(verdict="unsure", quote="")
    assert not check.calls


def test_unreadable_reviewed_file_keeps_repeated_evidence_veto(check):
    evidence = 'return {"verdict": "unsure", "quote": ""}'
    old = ["result = unsafe(value)", evidence, evidence]
    assert check(old, ["result = validate(value)", *old[1:]], evidence=evidence,
                 unreadable=True) == dict(verdict="unsure", quote="")
    assert not check.calls
