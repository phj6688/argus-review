import difflib
import hashlib
import json
from pathlib import Path

import pytest

from argus.diff import FilePatch
from argus.gitlab import positions


FIXTURES = Path(__file__).parent / "fixtures/gitlab/spike_b"
REFS = {"base_sha": "a" * 40, "start_sha": "b" * 40, "head_sha": "c" * 40}


def file_patch(patch, **kwargs):
    return FilePatch(filename="example.py", status="modified", patch=patch, **kwargs)


def expected_position(**lines):
    return {
        "position_type": "text", **REFS,
        "new_path": "example.py", "old_path": "example.py", **lines,
    }


def test_replay_b01_positions():
    before = [f"line {n:02d}\n" for n in range(1, 31)]
    after = []
    for n, line in enumerate(before, 1):
        if n != 20:
            after.append("changed ten\n" if n == 10 else line)
        if n == 25:
            after.extend(["added one\n", "added two\n"])
    patch = "".join(list(difflib.unified_diff(before, after, n=3))[2:])
    file = FilePatch("spike/pos.py", "modified", patch)
    variants = json.loads((FIXTURES / "b01_summary.json").read_text())["derived"]["variants"]
    for variant in variants:
        position = variant["request"]["position"]
        refs = {key: position[key] for key in REFS}
        if variant["label"] in {"b01_added_new", "b01_context_both"}:
            assert positions.position_for({"line": position["new_line"]}, file, refs) == position
        elif not variant["accepted"] or variant["label"] == "b01_removed_old":
            assert all(positions.position_for({"line": n}, file, refs) != position
                       for n in range(1, 41)), variant["label"]


def test_context_uses_later_hunk_old_number():
    file = file_patch("@@ -1 +1,4 @@\n keep\n+a\n+b\n+c\n@@ -20,2 +23,2 @@\n later\n tail\n")
    assert positions.position_for({"line": 24}, file, REFS) == expected_position(old_line=21, new_line=24)


def test_rename_uses_previous_path():
    file = FilePatch("new.py", "renamed", "@@ -1 +1 @@\n-old\n+new\n", previous_filename="old.py")
    result = positions.position_for({"line": 1}, file, REFS)
    assert result == {**expected_position(new_line=1), "new_path": "new.py", "old_path": "old.py"}


@pytest.mark.parametrize("patch", [None, "", "rename from old.py\nrename to new.py\n"])
def test_no_patch_or_no_hunks(patch):
    assert positions.position_for({"line": 1}, file_patch(patch), REFS) is None


@pytest.mark.parametrize("line", [None, 0, -1, "1", "bad", 1.5, True, [], {}, 9, 13])
def test_invalid_or_outside_target(line):
    file = file_patch("@@ -10,2 +10,2 @@\n keep\n tail\n")
    assert positions.position_for({"line": line}, file, REFS) is None


def test_removed_file_and_deletion_only_hunk_have_no_new_anchor():
    file = FilePatch("gone.py", "removed", "@@ -1 +0,0 @@\n-gone\n")
    assert positions.position_for({"line": 1}, file, REFS) is None
    deletion = file_patch("@@ -10,2 +9,0 @@\n-removed\n-also removed\n")
    assert all(positions.position_for({"line": n}, deletion, REFS) is None for n in range(1, 15))


def test_counts_distinguish_header_like_content_and_ignore_non_hunk_lines():
    file = file_patch(
        "diff --git a/example.py b/example.py\nindex abc..def\n--- a/example.py\n+++ b/example.py\n"
        "@@ -4,3 +4,3 @@\n--- removed text\n\\ No newline at end of file\n"
        "+++ added text\n\\ No newline at end of file\n\n tail\n"
        "--- ignored header\n+++ ignored header\nindex abc..def\n"
    )
    assert positions.position_for({"line": 4}, file, REFS) == expected_position(new_line=4)
    assert positions.position_for({"line": 5}, file, REFS) == expected_position(old_line=5, new_line=5)
    assert positions.position_for({"line": 6}, file, REFS) == expected_position(old_line=6, new_line=6)
    assert positions.position_for({"line": 7}, file, REFS) is None


def test_default_counts_and_crlf():
    file = file_patch("@@ -10 +11 @@ function\r\n-old\r\n+new\r\n")
    assert positions.position_for({"line": 11}, file, REFS) == expected_position(new_line=11)
    assert positions.position_for({"line": 12}, file, REFS) is None


def test_multiline_disabled_anchors_end():
    file = file_patch("@@ -0,0 +1,3 @@\n+a\n+b\n+c\n")
    assert positions.MULTI_LINE_ENABLED is False
    assert positions.position_for({"line": 1, "end_line": 3}, file, REFS) == expected_position(new_line=3)


def test_multiline_added_recipe_matches_b05(monkeypatch):
    monkeypatch.setattr(positions, "MULTI_LINE_ENABLED", True)
    fixture = json.loads((FIXTURES / "b05_zero.json").read_text())["request"]["json"]["position"]
    refs = {key: fixture[key] for key in REFS}
    file = FilePatch("spike/pos.py", "modified", "@@ -24,0 +25,4 @@\n+a\n+b\n+c\n+d\n")
    result = positions.position_for({"line": 25, "end_line": 28}, file, refs)
    assert result == fixture
    for endpoint, line in [("start", 25), ("end", 28)]:
        assert result["line_range"][endpoint]["line_code"] == hashlib.sha1(file.filename.encode()).hexdigest() + "_0_" + str(line)


def test_multiline_context_and_mixed_endpoints(monkeypatch):
    monkeypatch.setattr(positions, "MULTI_LINE_ENABLED", True)
    file = file_patch("@@ -10,2 +13,3 @@\n keep\n tail\n+added\n")
    path_hash = hashlib.sha1(file.filename.encode()).hexdigest()
    for end, old in [(14, 11), (15, None)]:
        lines = {"new_line": end} if old is None else {"old_line": old, "new_line": end}
        result = positions.position_for({"line": 13, "end_line": end}, file, REFS)
        assert result == expected_position(**lines, line_range={
            "start": {"line_code": f"{path_hash}_10_13", "type": "new", "old_line": 10, "new_line": 13},
            "end": {"line_code": f"{path_hash}_{old or 0}_{end}", "type": "new", **lines},
        })


@pytest.mark.parametrize("start,end", [(1, 6), (2, 6), (6, 6), (7, 6), (None, 6), ("bad", 6)])
def test_invalid_or_cross_hunk_range_falls_back_to_end(monkeypatch, start, end):
    monkeypatch.setattr(positions, "MULTI_LINE_ENABLED", True)
    file = file_patch("@@ -0,0 +1 @@\n+a\n@@ -4,0 +6 @@\n+b\n")
    assert positions.position_for({"line": start, "end_line": end}, file, REFS) == expected_position(new_line=6)


@pytest.mark.parametrize("end", [0, -1, "bad", True])
def test_explicit_invalid_end_does_not_fall_back_to_start(end):
    assert positions.position_for({"line": 1, "end_line": end}, file_patch("@@ -0,0 +1 @@\n+a\n"), REFS) is None


@pytest.mark.parametrize("key", list(REFS))
@pytest.mark.parametrize("value", [None, "", "missing"])
def test_missing_refs_raise_before_position_is_used(key, value):
    refs = REFS.copy()
    if value == "missing":
        refs.pop(key)
    else:
        refs[key] = value
    with pytest.raises(ValueError, match=key):
        positions.position_for({"line": 1}, file_patch("@@ -0,0 +1 @@\n+a\n"), refs)


def test_refs_are_copied_exactly_without_extra_fields_or_mutation():
    refs = {**REFS, "extra": "ignored"}
    assert positions.position_for({"line": 1}, file_patch("@@ -0,0 +1 @@\n+a\n"), refs) == expected_position(new_line=1)
    assert refs == {**REFS, "extra": "ignored"}
