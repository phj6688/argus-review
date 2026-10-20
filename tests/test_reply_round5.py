"""Conservative re-checks on complete comparisons and older report copies."""

import base64
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from argus import acceptance, client, github, replies
from argus.api import db
from argus.jarvis import live
from test_acceptance import seed
from reply_helpers import file_entry


OLD = "result = eval(user_input)"
FIX = "result = ast.literal_eval(user_input)"
PATCH = f"@@ -1 +1 @@\n-{OLD}\n+{FIX}"


@pytest.fixture
def recheck(monkeypatch):
    reads, calls = [], []

    def run(patch=PATCH, code=FIX, extras=(), path="app.py", line=1, quote=FIX):
        def read(*args):
            reads.append(args)
            return code

        def model(*args):
            calls.append(json.loads(args[2]))
            return {"verdict": "fixed", "quote": quote}

        monkeypatch.setattr(replies, "_read_file", read)
        monkeypatch.setattr(client, "complete_tool", model)
        finding = dict(path=path, line=line, head_sha="a" * 40, evidence="",
                       comparison=dict(status="ahead", files=[file_entry(filename=path, patch=patch), *extras]))
        return replies.recheck("owner/repo", finding, "b" * 40)

    run.reads, run.calls = reads, calls
    return run


@pytest.mark.parametrize("count", [299, 300, 301])
def test_comparison_cap_prevents_reads_and_inference(recheck, count):
    extras = [file_entry(filename=f"other_{i}.py", patch="@@ -0,0 +1 @@\n+safe_value = 1")
              for i in range(count - 1)]
    result = recheck(extras=extras)
    assert result == {"verdict": "fixed" if count == 299 else "unsure",
                      "quote": FIX if count == 299 else ""}
    assert len(recheck.calls) == (count == 299)
    assert len(recheck.reads) == 2 * (count == 299)


@pytest.mark.parametrize("added", [
    ["output = eval( user_input )"],
    ["output = eval(", "    user_input", ")"],
    ["output = eval(", "\\ No newline at end of file", "    user_input", ")"],
])
def test_reformatted_call_move_vetoes_inference(recheck, added):
    patch = f"@@ -0,0 +1,{sum(not line.startswith(chr(92)) for line in added)} @@\n"
    patch += "\n".join(line if line.startswith("\\") else "+" + line for line in added)
    assert recheck(extras=[file_entry(filename="other.py", patch=patch)]) == dict(verdict="unsure", quote="")
    assert not recheck.calls


def test_reflowed_line_move_vetoes_inference(recheck):
    old = "const level = ADMIN_LEVEL + overrideBonus;"
    fixed = "const level = USER_LEVEL;"
    patch = f"@@ -1 +1 @@\n-{old}\n+{fixed}"
    extra = file_entry(filename="b.js", patch="@@ -0,0 +1,2 @@\n+const level =\n+  ADMIN_LEVEL + overrideBonus;")
    assert recheck(patch, fixed, [extra], "a.js", quote=fixed) == dict(verdict="unsure", quote="")
    assert not recheck.calls


def test_literal_eval_replacement_can_confirm_fix(recheck):
    assert recheck() == dict(verdict="fixed", quote=FIX)
    assert len(recheck.calls) == 1


@pytest.mark.parametrize("boundary", [" ", "-", "@@ -1 +1 @@"])
def test_call_cannot_span_separate_added_runs(recheck, boundary):
    if boundary == "@@ -1 +1 @@":
        patch = "@@ -0,0 +1 @@\n+output = eval(\n@@ -2 +3,2 @@\n-old_value\n+user_input)\n+safe_value"
    else:
        patch = f"@@ -1 +1,3 @@\n+output = eval(\n{boundary}old_value\n+user_input)"
        if boundary == "-":
            patch = patch.replace("+1,3", "+1,2")
    assert recheck(extras=[file_entry(filename="other.py", patch=patch)]) == dict(verdict="fixed", quote=FIX)
    assert len(recheck.calls) == 1


@pytest.mark.parametrize("separator", ["\f", "\u2028"])
def test_git_line_numbers_ignore_embedded_separators(recheck, separator):
    prefix = f'label = "before{separator}after"'
    patch = f"@@ -1,2 +1,2 @@\n {prefix}\n-{OLD}\n+{FIX}"
    finding = dict(line=2)
    span, deleted, added, counts = replies._mapped_patch(finding, dict(patch=patch))
    assert span == (2, 2) and deleted == {2: OLD} and added == {2: FIX}
    assert counts == (1, 1)
    assert recheck(patch, prefix + "\n" + FIX + "\n", line=2) == dict(verdict="fixed", quote=FIX)
    assert len(recheck.calls) == 1
    assert recheck.calls[0]["current_start_line"] == 1
    assert recheck.calls[0]["current_range"] == [2, 2]
    assert recheck.calls[0]["current_code"] == prefix + "\n" + FIX


def test_crlf_code_and_patch_keep_matching_quotes(recheck):
    prefix = 'label = "before"'
    patch = f"@@ -1,2 +1,2 @@\r\n {prefix}\r\n-{OLD}\r\n+{FIX}\r\n"
    assert recheck(patch, prefix + "\r\n" + FIX + "\r\n", line=2) == dict(verdict="fixed", quote=FIX)
    assert len(recheck.calls) == 1


def test_form_feed_in_deleted_patch_line_parses(recheck):
    patch = f'@@ -1 +1 @@\n-value = "before\fafter"\n+{FIX}'
    assert recheck(patch) == dict(verdict="fixed", quote=FIX)
    assert len(recheck.calls) == 1


def test_change_blocks_keeps_form_feed_in_deleted_line():
    for value in ("beforeafter", "before\fafter"):
        patch = f'@@ -7,2 +7 @@\n-value = "{value}"\n-old_value\n+new_value'
        assert list(acceptance._change_blocks(patch)) == ["@@ -7,2 +0,0 @@"]


@pytest.mark.parametrize("patch", [
    "@@ -0,0 +1,2 @@\n+output = safe_value",
    "@@ -1 +1 @@\n-old_value\n+new_value\n@@ -3 +4 @@\n-old_value\n+new_value",
    "@@ -1 +1 @@\n-old_value\ninvalid\n+new_value",
    "missing header\n+new_value",
])
def test_every_patch_must_validate_before_inference(recheck, patch):
    assert recheck(extras=[file_entry(filename="other.py", patch=patch)]) == dict(verdict="unsure", quote="")
    assert not recheck.calls


def test_no_newline_markers_validate_in_every_patch(recheck):
    marker = "\n\\ No newline at end of file"
    patch = f"@@ -1 +1 @@\n-{OLD}{marker}\n+{FIX}{marker}\n"
    extra = file_entry(filename="other.py", patch="@@ -1 +1 @@\n-old_value" + marker + "\n+new_value" + marker)
    assert recheck(patch, extras=[extra]) == dict(verdict="fixed", quote=FIX)
    assert len(recheck.calls) == 1


def test_previous_release_copy_reports_without_schema_writes(tmp_path):
    path = tmp_path / "previous.db"
    db.init_db(path)
    with sqlite3.connect(path) as conn:
        for table in ("bot_actions", "reply_attempts", "reply_texts"):
            conn.execute(f"DROP TABLE {table}")
        if sqlite3.sqlite_version_info >= (3, 35, 0):
            conn.execute("ALTER TABLE published_findings DROP COLUMN gh_thread_id")
    finding_id = seed(path)
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO finding_outcomes(finding_id,kind,value,evidence_id,occurred_at,observed_at) "
                     "VALUES (?, 'reply', 'fixed', '1', '2026-10-16', '2026-10-16')", (finding_id,))
    before = path.read_bytes()
    report = acceptance.stats(path)
    root = Path(__file__).resolve().parents[1]
    process = subprocess.run([sys.executable, str(root / "scripts/acceptance_report.py"), str(path)],
                             capture_output=True, text=True, check=True, cwd=root)
    offline = json.loads(process.stdout)
    for result in (report, offline):
        assert result["totals"]["strict_accepted"] == 0
        assert result["totals"]["posted"] == 1
        assert result["bot"] == dict(rechecks=dict(fixed=0, present=0, unsure=0), replies=0, resolutions=0)
    assert path.read_bytes() == before


@pytest.mark.parametrize("fetch", [replies._read_file, live._fetch_file_at])
@pytest.mark.parametrize("path,encoded", [
    ("src/a#b.py", b"src/a%23b.py"),
    ("src/a?b.py", b"src/a%3Fb.py"),
    ("src/a%b.py", b"src/a%25b.py"),
    ("src/a b.py", b"src/a%20b.py"),
])
def test_contents_paths_are_escaped_after_url_guard(monkeypatch, fetch, path, encoded):
    monkeypatch.setenv("GITHUB_TOKEN", "read-secret")
    monkeypatch.setattr(github, "_quota", {})
    monkeypatch.setattr(github, "_pause_until", 0)
    requests = []

    def get(url, **kwargs):
        request = httpx.Request("GET", url, params=kwargs["params"])
        requests.append(request)
        return httpx.Response(200, request=request, json=dict(
            encoding="base64", content=base64.b64encode(b"safe_value").decode()))

    monkeypatch.setattr(httpx, "get", get)
    assert fetch("owner/repo", path, "feature/ref") == "safe_value"
    assert len(requests) == 1
    url = requests[0].url
    assert url.raw_path.split(b"?", 1)[0] == b"/repos/owner/repo/contents/" + encoded
    assert dict(url.params) == {"ref": "feature/ref"}
    assert not url.fragment
