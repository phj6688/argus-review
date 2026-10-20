import pytest

from argus.diff import FilePatch, added_lines, compress_diff, strip_deletion_only_hunks
from argus.tokens import HeuristicEstimator

MIXED_HUNK = "@@ -1,3 +1,3 @@\n-old\n+new\n ctx"
DELETION_ONLY_HUNK = "@@ -1,2 +0,0 @@\n-gone\n-also gone"


@pytest.mark.parametrize("patch, expected", [
    (None, set()),
    ("", set()),
    (DELETION_ONLY_HUNK, set()),
    ("@@ -1 +1 @@\n-old\n+new", {1}),
    ("--- a/a.py\n+++ b/a.py\n@@ -2,3 +2,4 @@\n ctx\n-old\n+new\n"
     "\\ No newline at end of file\n+extra\n ctx\n@@ -10,2 +11,3 @@\n ctx\n+last\n ctx",
     {3, 4, 12}),
    ("@@ -1 +1 @@\n-++old\n+++new", {1}),
])
def test_added_lines_tracks_post_image_lines(patch, expected):
    assert added_lines(patch) == expected


def test_added_lines_counts_blank_context_lines():
    patch = "@@ -1,3 +1,3 @@\n first\n\n+third"
    assert added_lines(patch) == {3}


def test_strip_keeps_hunk_with_additions():
    assert "+new" in strip_deletion_only_hunks(MIXED_HUNK)


def test_strip_drops_deletion_only_hunk():
    assert strip_deletion_only_hunks(DELETION_ONLY_HUNK) == ""


def test_strip_empty_patch_is_empty():
    assert strip_deletion_only_hunks("") == ""


def test_strip_keeps_mixed_drops_deletion_only():
    combined = MIXED_HUNK + "\n" + DELETION_ONLY_HUNK
    kept = strip_deletion_only_hunks(combined)
    assert "+new" in kept
    assert "also gone" not in kept


def test_language_inference():
    assert FilePatch("a/b.py", "modified", "").language == "python"
    assert FilePatch("x.unknownext", "modified", "").language == "other"


def test_compress_records_deleted_separately():
    files = [
        FilePatch("a.py", "modified", MIXED_HUNK),
        FilePatch("gone.py", "removed", DELETION_ONLY_HUNK),
    ]
    result = compress_diff(files, budget_tokens=10_000)
    assert result.included == ["a.py"]
    assert result.deleted == ["gone.py"]


def test_compress_overflows_when_over_budget():
    big = "@@ -1 +1 @@\n+" + ("x" * 4000)
    files = [FilePatch("a.py", "modified", big), FilePatch("b.py", "modified", big)]
    result = compress_diff(files, budget_tokens=200, estimator=HeuristicEstimator())
    assert len(result.included) + len(result.overflow) == 2
    assert result.overflow, "at least one file should overflow a tiny budget"


def test_compress_ranks_common_language_first():
    py = "@@ -1 +1 @@\n+a"
    files = [
        FilePatch("only.go", "modified", py),
        FilePatch("one.py", "modified", py),
        FilePatch("two.py", "modified", py),
    ]
    result = compress_diff(files, budget_tokens=10_000)
    assert result.included[0].endswith(".py")


@pytest.mark.parametrize("separator", ["\f", "\u2028", "\r"])
def test_added_lines_uses_git_numbering(separator):
    from argus.diff import added_lines
    assert added_lines(f"@@ -1 +1,3 @@\n+one{separator}\n\n+three\n") == {1, 3}


def test_touched_lines_includes_deletion_positions():
    from argus.diff import touched_lines
    assert touched_lines("@@ -1,5 +1,3 @@\n keep\n-old\n-older\n next\n+new") == {2, 3}
    assert touched_lines("@@ -4,2 +3,0 @@\n-old\n-older") == {4}
    assert touched_lines(None) == set()


@pytest.mark.parametrize("text, expected", [
    ("", []), ("\n", [""]), ("one\n\n", ["one", ""]),
    ("one\r\ntwo\r\n", ["one", "two"]),
    ("one\r\r\n", ["one\r"]),
    ("a\fb\vc\x1cd\x1de\x1ef\x85g\u2028h\u2029i\rj", ["a\fb\vc\x1cd\x1de\x1ef\x85g\u2028h\u2029i\rj"]),
])
def test_split_lines_preserves_non_newline_separators(text, expected):
    from argus.diff import split_lines
    assert split_lines(text) == expected


@pytest.mark.parametrize("separator", ["\f", "\u2028"])
def test_touched_lines_uses_git_numbering(separator):
    from argus.diff import touched_lines
    assert touched_lines(f"@@ -1,2 +1,3 @@\n first{separator}\n-old\n+second\n+third") == {2, 3}
