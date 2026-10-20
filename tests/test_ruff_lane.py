"""The ruff lane: the first finding source whose verdict needs no opinion.

Every other lane rests on a model's judgement, which today's panel showed does not
converge. A ruff rule either fires against a pinned binary and a pinned config or
it does not, and anyone can re-run it. That is what earns the deterministic tier.
"""

import importlib.metadata
import json
import subprocess

import pytest

from argus.diff import FilePatch
from argus.jarvis import ruff_lane

BAD = '''import os


def get_user(conn, name):
    conn.execute("SELECT * FROM users WHERE name = " % name)


def run(cmd):
    os.system("ls " + cmd)  # noqa: S605


def broken():
    return undefined_name
'''

CLEAN = '''def add(a: int, b: int) -> int:
    return a + b
'''


def _files(*names):
    return [FilePatch(filename=n, status="modified", patch="@@ -1 +1 @@\n+x\n")
            for n in names]


def test_eligible_files_are_python_only():
    files = _files("src/app.py", "web/app.tsx", "deploy/compose.yml",
                   "src/types.pyi", "README.md")
    assert ruff_lane.eligible(files) == ["src/app.py", "src/types.pyi"]


def test_a_deleted_python_file_is_not_eligible():
    files = [FilePatch(filename="src/gone.py", status="removed", patch=None)]
    assert ruff_lane.eligible(files) == []


def test_it_finds_the_planted_defects():
    hits = ruff_lane.scan_content("src/app.py", BAD)
    codes = {h.code for h in hits}
    assert "S608" in codes, "SQL injection"
    assert "F821" in codes, "undefined name"


def test_a_noqa_in_the_code_under_review_does_not_silence_it():
    """A suppression added by the diff is the change asking the reviewer not to
    look. The lane runs --ignore-noqa so the code cannot switch it off."""
    hits = ruff_lane.scan_content("src/app.py", BAD)
    assert "S605" in {h.code for h in hits}, "noqa must not hide the shell injection"


def test_clean_code_yields_nothing():
    assert ruff_lane.scan_content("src/ok.py", CLEAN) == []


def test_repo_config_cannot_widen_or_narrow_the_gate(tmp_path, monkeypatch):
    """--isolated means a repo's own pyproject cannot turn a gate rule off."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.ruff.lint]\nignore = ["S608", "F821", "S605"]\n')
    monkeypatch.chdir(tmp_path)
    hits = ruff_lane.scan_content("src/app.py", BAD)
    assert {"S608", "F821"} <= {h.code for h in hits}


@pytest.mark.parametrize("code, is_bug", [
    ("E902", True), ("E999", True), ("F821", True), ("invalid-syntax", True),
    ("S608", False), ("S605", False), ("S324", False),
])
def test_only_the_day_one_block_set_is_bug_class(code, is_bug):
    """E9*, F821 and an unparseable file block from day one; the security rules
    warn until measured. Shipping S as blocking without a yield number is how a
    tool gets ignored. `invalid-syntax` is ruff 0.15's spelling of E999 - the
    rename is exactly how a non-compiling file could have read as clean."""
    assert ruff_lane.is_blocking(code) is is_bug


def test_findings_carry_the_rule_id_and_the_deterministic_lane():
    hits = ruff_lane.scan_content("src/app.py", BAD)
    verified = [ruff_lane.to_finding(h) for h in hits]
    sql = next(v for v in verified if v.finding.rule_id == "S608")
    assert sql.lane == ruff_lane.LANE
    assert sql.finding.file == "src/app.py"
    assert sql.finding.confidence == 1.0
    assert sql.verdict.is_real is True, "a deterministic hit skips the LLM verifier"


def test_a_file_that_does_not_parse_is_reported_not_swallowed():
    """The worst possible silent pass: a file that cannot compile, reported clean."""
    hits = ruff_lane.scan_content("src/broken.py", "def oops(:\n")
    assert hits, "an unparseable file must never come back with no findings"
    assert all(ruff_lane.is_blocking(h.code) for h in hits)


def test_the_tool_identity_is_pinned_and_reported():
    """The tier claim rests on re-executability, so the version and the exact rule
    selection are part of the record, not incidental."""
    ident = ruff_lane.tool_identity()
    assert ident["name"] == "ruff"
    assert ident["version"], "a version must be resolvable or the tier is unearned"
    assert ident["rules"] == ruff_lane.RULES
    assert "--isolated" in ident["args"]
    assert "--ignore-noqa" in ident["args"]


def test_it_runs_the_pinned_ruff_not_whichever_is_first_on_path():
    """The deterministic tier claims a finding is re-executable against a known
    binary. On the box this was written `which ruff` resolved 0.15.10 while the
    project pinned 0.15.17, so resolving through PATH would have made the recorded
    version a fiction and the tier claim with it."""
    import sys as _sys
    assert ruff_lane.command()[0] == _sys.executable
    assert ruff_lane.command()[1:] == ["-m", "ruff"]

    pinned = [d for d in importlib.metadata.distribution("argus").requires or []
              if d.startswith("ruff==")]
    assert pinned, "ruff must be a pinned runtime dependency, not a floating one"
    wanted = pinned[0].split("==", 1)[1].strip()
    assert wanted in ruff_lane.tool_identity()["version"], (
        f"lane runs {ruff_lane.tool_identity()['version']!r}, pyproject pins {wanted!r}")


def test_a_missing_binary_is_an_error_lane_not_a_clean_result(monkeypatch):
    def _boom(*args, **kwargs):
        raise FileNotFoundError("ruff")

    monkeypatch.setattr(subprocess, "run", _boom)
    with pytest.raises(ruff_lane.RuffUnavailable):
        ruff_lane.scan_content("src/app.py", BAD)


def test_unparseable_output_raises_rather_than_reporting_clean(monkeypatch):
    class _Result:
        returncode = 1
        stdout = "not json"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Result())
    with pytest.raises(ruff_lane.RuffUnavailable):
        ruff_lane.scan_content("src/app.py", BAD)


def test_json_contract_is_what_we_think_it_is():
    """Pin the upstream shape we depend on, so a ruff upgrade that renames a field
    fails here instead of silently reporting every file clean."""
    raw = ruff_lane._run(["src/app.py"], BAD, "src/app.py")
    parsed = json.loads(raw)
    assert isinstance(parsed, list) and parsed
    for key in ("code", "message", "location"):
        assert key in parsed[0], f"ruff JSON lost {key!r}"
    assert "row" in parsed[0]["location"]


def test_the_measured_noise_rules_are_excluded_by_default():
    """S101 was 827 of 834 hits across src+tests and every one was an `assert` in
    a test. Shipping it would bury the two real leads under four hundred false
    ones per review, which is how a reviewer earns the habit of being skipped."""
    assert "S101" in ruff_lane.ignored()
    hits = ruff_lane.scan_content("tests/test_thing.py", "def test_x():\n    assert 1\n")
    assert hits == []


def test_the_exclusions_travel_in_the_tool_record():
    """A narrowed gate must never be invisible: whoever reads the coverage block
    can see exactly which rules were switched off."""
    assert ruff_lane.tool_identity()["ignored"] == ruff_lane.DEFAULT_IGNORE
    assert "--ignore" in ruff_lane.tool_identity()["args"]


def test_excluding_noise_does_not_disarm_the_injection_rules():
    """The rules the lane exists for survive the tuning."""
    hits = ruff_lane.scan_content("src/app.py", BAD)
    codes = {h.code for h in hits}
    assert "S608" in codes and "F821" in codes


@pytest.mark.parametrize("end, expected", [(None, 2), ({"row": 5}, 5)])
def test_scan_reads_end_location(monkeypatch, end, expected):
    monkeypatch.setattr(ruff_lane, "_run", lambda *args: json.dumps([
        {"code": "S608", "message": "SQL", "location": {"row": 2}, "end_location": end}]))
    hit, = ruff_lane.scan_content("a.py", "content")
    assert hit.line == 2
    assert hit.end_line == expected
