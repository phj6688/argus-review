"""Whole-file totals detect missing complete hunks before inference."""

import pytest

from argus import client, replies
from reply_helpers import file_entry


OLD = "result = eval(user_input)"
FIX = "result = ast.literal_eval(user_input)"
PATCH = f"@@ -1 +1 @@\n-{OLD}\n+{FIX}"
OTHER = "@@ -1 +1 @@\n-old_value\n+safe_value = 1"


@pytest.fixture
def recheck(monkeypatch):
    calls = []
    monkeypatch.setattr(replies, "_read_file", lambda *args: FIX)

    def model(*args):
        calls.append(args)
        return dict(verdict="fixed", quote=FIX)

    monkeypatch.setattr(client, "complete_tool", model)

    def run(files):
        finding = dict(path="app.py", line=1, head_sha="a" * 40, evidence="",
                       comparison=dict(status="ahead", files=files))
        return replies.recheck("owner/repo", finding, "b" * 40)

    run.calls = calls
    return run


@pytest.mark.parametrize("destination", ["app.py", "other.py"])
def test_missing_trailing_complete_hunk_blocks_fix(recheck, destination):
    patch = PATCH if destination == "app.py" else OTHER
    full = patch + "\n@@ -20,0 +21 @@\n+output = eval(user_input)"
    incomplete = file_entry(destination, full)
    incomplete["patch"] = patch
    files = [incomplete] if destination == "app.py" else [file_entry("app.py", PATCH), incomplete]
    assert recheck(files) == dict(verdict="unsure", quote="")
    assert not recheck.calls


@pytest.mark.parametrize("destination", ["app.py", "other.py"])
@pytest.mark.parametrize("field", ["additions", "deletions"])
@pytest.mark.parametrize("value", ["missing", None, "1", 1.0, True, False, 2])
def test_invalid_file_total_blocks_fix(recheck, destination, field, value):
    files = [file_entry("app.py", PATCH), file_entry("other.py", OTHER)]
    file = next(file for file in files if file["filename"] == destination)
    if value == "missing":
        file.pop(field)
    else:
        file[field] = value
    assert recheck(files) == dict(verdict="unsure", quote="")
    assert not recheck.calls


def test_no_newline_markers_do_not_count_as_changes(recheck):
    marker = "\n\\ No newline at end of file"
    patch = f"@@ -1 +1 @@\n-{OLD}{marker}\n+{FIX}{marker}\n"
    files = [file_entry("app.py", patch), file_entry("other.py", OTHER + marker)]
    assert [(file["additions"], file["deletions"]) for file in files] == [(1, 1), (1, 1)]
    assert recheck(files) == dict(verdict="fixed", quote=FIX)
    assert len(recheck.calls) == 1
