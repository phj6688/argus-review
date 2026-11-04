import importlib
import json
import random
import re
import subprocess
import sys
from pathlib import Path

import pytest

from argus.gitlab import body
from argus.publish import render_comment


FIXTURES = Path(__file__).parent / "fixtures/gitlab/spike_b"
LEADING_SPACE = ["\u00a0", "\t", "\u200b", "\u2060", "\ufeff", "\u200c", "\u200d", "   ", " \t\u00a0\u200b\u200c\u200d\u2060\ufeff"]
BREAKS = ["\n", "\r", "\r\n", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]
BREAKOUTS = [
    "```\nsnippet\n```\n/close",
    "`````\nsnippet\n```\n/close",
    "~~~\nsnippet\n~~~\n/merge",
    "```\nsnippet\n/close",
    "```\n/merge\n```\n/close",
    "<details>\n<summary>Example</summary>\n\n</details>\n/close",
]


def finding(**overrides):
    return {
        "file": "example.py", "line": 10, "severity": "major", "evidence_tier": "verified",
        "category": "security", "title": "Unsafe input", "description": "Check @user here.",
        "evidence": "example()", "proposed_fix": "Validate input.", "fingerprint": "abc123",
        **overrides,
    }


@pytest.mark.parametrize("prefix", LEADING_SPACE)
def test_leading_space_quick_actions(prefix):
    original = prefix + "/close"
    with pytest.raises(body.UnsafeBodyError, match=r"line 1\b"):
        body.assert_safe(original)
    neutral = body.neutralize_quick_actions(original)
    assert neutral == prefix + "\\/close"
    body.assert_safe(neutral)
    assert body.neutralize_quick_actions(neutral) == neutral


@pytest.mark.parametrize("separator", BREAKS)
def test_all_line_terminators_are_preserved(separator):
    original = f"text{separator}\t/close{separator} /merge{separator}"
    with pytest.raises(body.UnsafeBodyError, match=r"line 2\b") as error:
        body.assert_safe(original)
    assert "/close" not in str(error.value)
    neutral = body.neutralize_quick_actions(original)
    assert neutral == f"text{separator}\t\\/close{separator} \\/merge{separator}"
    body.assert_safe(neutral)


def test_replay_executed_quick_action_variants():
    summary = json.loads((FIXTURES / "b06_summary.json").read_text())["derived"]
    seen = set()
    for channel, result in summary["channels"].items():
        for variant in result["executed_variants"]:
            fixture = json.loads((FIXTURES / f"b06_{channel}_{variant}.json").read_text())
            original = fixture["request"]["json"]["note" if channel == "draft" else "body"]
            with pytest.raises(body.UnsafeBodyError):
                body.assert_safe(original)
            neutral = body.neutralize_quick_actions(original)
            body.assert_safe(neutral)
            assert neutral.replace("\\/", "/") == original
            seen.add(variant)
    assert seen == set(summary["executed_variants"])


def test_replay_command_fixtures():
    commands = set()
    for path in sorted(FIXTURES.glob("b06_cmd_*_argus-bot-dev.json")):
        original = json.loads(path.read_text())["request"]["json"]["body"]
        commands.add(original.split()[0])
        with pytest.raises(body.UnsafeBodyError):
            body.assert_safe(original)
        assert body.neutralize_quick_actions(original) == "\\" + original
        body.assert_safe(body.neutralize_quick_actions(original))
    assert commands == {"/close", "/draft", "/assign", "/unassign", "/reviewer", "/title", "/target_branch", "/label", "/approve", "/rebase", "/merge"}


@pytest.mark.parametrize("variant", ["backslash", "fenced", "zwsp", "nbsp", "tab"])
@pytest.mark.parametrize("channel", ["notes", "diff", "reply", "draft"])
def test_nonexecuting_variants_still_get_conservative_handling(channel, variant):
    fixture = json.loads((FIXTURES / f"b06_{channel}_{variant}.json").read_text())
    original = fixture["request"]["json"]["note" if channel == "draft" else "body"]
    neutral = body.neutralize_quick_actions(original)
    body.assert_safe(neutral)
    if variant == "backslash":
        assert neutral == original
    else:
        assert neutral != original
        assert neutral.replace("\\/", "/") == original


@pytest.mark.parametrize("original", ["", "\n\r\n", "just text /close", "\\/close", "\t\\/merge", "\\\\/close"])
def test_non_candidates_unchanged(original):
    assert body.neutralize_quick_actions(original) == original
    body.assert_safe(original)


@pytest.mark.parametrize("original", BREAKOUTS)
def test_adversarial_fence_and_details_breakouts(original):
    original += "\n@user &#64;user"
    neutral = body.neutralize_quick_actions(body.neutralize_mentions(original))
    body.assert_safe(neutral)
    body.assert_mentions_safe(neutral)
    assert neutral.replace("\\/", "/").replace("\u200b", "") == original.replace("&#64;", "@")


def test_random_quick_action_bodies_are_reversible_and_idempotent():
    rng = random.Random(1409)
    pieces = ["/", "/cmd", "a", "words", " ", "\t", "\u00a0", "\u200b", "\u200c", "\u200d", "\u2060", "\ufeff", "`", *BREAKS]
    # This oracle recognizes prefixes independently of the production scanner.
    escaped = re.compile(r"^(?P<space>[\s\u200b\u200c\u200d\u2060\ufeff]*)\\(?=/)")
    for _ in range(4000):
        original = "".join(rng.choices(pieces, k=rng.randrange(100)))
        neutral = body.neutralize_quick_actions(original)
        body.assert_safe(neutral)
        restored = "".join(escaped.sub(r"\g<space>", line, count=1)
                           for line in neutral.splitlines(keepends=True))
        assert restored == original
        assert neutral.replace("\\", "") == original
        assert body.neutralize_quick_actions(neutral) == neutral


@pytest.mark.parametrize("original", [
    "@user", "@group/sub", "@all", "hello @user", "&#64;user", "&#x40;user",
    "&#X40;user", "&#064;user", "&commat;user", "&#64user", "&#x40user", "&#X040user",
    "\\@user", "\\\\@user", "@", "test@example.test", "@@user", "@\u200buser @other",
])
def test_mentions_and_entities(original):
    with pytest.raises(body.UnsafeBodyError):
        body.assert_mentions_safe(original)
    neutral = body.neutralize_mentions(original)
    body.assert_mentions_safe(neutral)
    assert re.search(r"@(?!\u200b)", neutral) is None
    assert body.neutralize_mentions(neutral) == neutral


@pytest.mark.parametrize("original", ["", "@\u200buser", "&amp;#64;user", "&amp;", "&#65;", "&unknown;", "＠user", "﹫user"])
def test_safe_mentions_and_unrelated_entities_unchanged(original):
    assert body.neutralize_mentions(original) == original
    body.assert_mentions_safe(original)


def test_replay_mention_fixtures():
    for channel in ["notes", "diff"]:
        for path in sorted(FIXTURES.glob(f"b09_{channel}_*.json")):
            data = json.loads(path.read_text())
            original = data.get("request", {}).get("json", {}).get("body")
            if original is not None:
                body.assert_mentions_safe(body.neutralize_mentions(original))


def test_entity_mentions_are_rejected_even_with_following_zero_width_space():
    with pytest.raises(body.UnsafeBodyError):
        body.assert_mentions_safe("&#64;\u200buser")
    assert body.neutralize_mentions("&#64;user") == "@\u200buser"


@pytest.mark.parametrize("entity", ["&#" + "0" * 5000 + "64;", "&#x" + "0" * 5000 + "40;"], ids=["decimal", "hex"])
def test_very_long_zero_padded_mention_entities(entity):
    with pytest.raises(body.UnsafeBodyError):
        body.assert_mentions_safe(entity + "user")
    assert body.neutralize_mentions(entity + "user") == "@\u200buser"
    body.assert_mentions_safe(body.neutralize_mentions(entity + "user"))


@pytest.mark.parametrize("entity", ["&#" + "9" * 5000 + ";", "&#" + "0" * 5000 + "65;", "&#x" + "f" * 5000 + ";"], ids=["decimal_overflow", "zero_padded_letter", "hex_overflow"])
def test_very_long_unrelated_numeric_entities_stay_unchanged(entity):
    assert body.neutralize_mentions(entity) == entity
    body.assert_mentions_safe(entity)


@pytest.mark.parametrize("original", ["/close", "@user", "&#64;user", "/close\n@user"])
def test_combined_guard_checks_commands_and_mentions(original):
    with pytest.raises(body.UnsafeBodyError):
        body.assert_body_safe(original)
    body.assert_body_safe(body.neutralize_quick_actions(body.neutralize_mentions(original)))


@pytest.mark.parametrize("form", ["html", "ref"])
@pytest.mark.parametrize("unverified", [False, True])
@pytest.mark.parametrize("ending", ["", " \t", "\r\n", "\r\n \t\r\n"])
def test_markers_round_trip(form, unverified, ending):
    fp = "Abc_123.x:y-z"
    kind = "ufp" if unverified else "fp"
    marker = body.marker(fp, unverified=unverified, form=form)
    expected = f"<!-- argus:{kind}={fp} -->" if form == "html" else f"[//]: # (argus:{kind}={fp})"
    assert marker == expected
    for prefix in ["", "Finding text.\r\n\r\n"]:
        assert body.parse_marker(prefix + marker + ending) == fp
        assert body.parse_marker_kind(prefix + marker + ending) == (kind, fp)


def test_only_last_whole_line_marker_is_parsed():
    html = body.marker("first")
    ref = body.marker("second", unverified=True, form="ref")
    assert body.parse_marker(html + "\n\ntext") is None
    assert body.parse_marker("prefix " + html) is None
    assert body.parse_marker(html + " suffix") is None
    assert body.parse_marker(html + "\n\n" + ref) == "second"
    assert body.parse_marker_kind(ref + "\n\n" + html) == ("fp", "first")
    assert body.parse_marker(" \r\n\t") is None


@pytest.mark.parametrize("separator", ["\v", "\f", "\x85", " ", "\r"])
def test_marker_after_a_non_newline_break_is_not_a_marker_line(separator):
    assert body.parse_marker("text" + separator + body.marker("spoof")) is None


@pytest.mark.parametrize("fp", ["", "a b", "a\nb", "a>b", "a)b", "@user", "a/b", "x\u200b", "é", "x\n"])
def test_invalid_fingerprints_raise(fp):
    for form in ["html", "ref"]:
        with pytest.raises(ValueError):
            body.marker(fp, form=form)
    assert body.parse_marker(f"<!-- argus:fp={fp} -->") is None


def test_unknown_marker_form_raises():
    with pytest.raises(ValueError):
        body.marker("abc", form="other")


@pytest.mark.parametrize("unverified", [False, True])
def test_render_preserves_markdown_and_fingerprint(unverified):
    item = finding(unverified=unverified, evidence="/close\n@group/sub", proposed_fix="Check @user.")
    original = render_comment(item)
    neutral = body.render_gitlab_comment(item)
    assert neutral.replace("\\/", "/").replace("\u200b", "") == original
    assert body.parse_marker(original) == item["fingerprint"]
    assert body.parse_marker(neutral) == item["fingerprint"]
    assert neutral.endswith(body.marker(item["fingerprint"], unverified=unverified))
    body.assert_body_safe(neutral)


@pytest.mark.parametrize("evidence", BREAKOUTS)
def test_render_untrusted_finding_breakouts(evidence):
    item = finding(
        evidence=evidence + "\n@user &#64;user &#x40;user",
        description="Description\n/close\n@user &#64;user",
        proposed_fix="Fix\n/merge\n@group/sub &#X40;user",
    )
    original = render_comment(item)
    neutral = body.render_gitlab_comment(item)
    body.assert_safe(neutral)
    body.assert_mentions_safe(neutral)
    expected = original.replace("&#64;", "@").replace("&#x40;", "@").replace("&#X40;", "@")
    expected = expected.replace("@", "@\u200b").replace("\n/close", "\n\\/close").replace("\n/merge", "\n\\/merge")
    assert neutral == expected
    assert body.parse_marker(neutral) == item["fingerprint"]


@pytest.mark.parametrize("fp", ["bad fp", "@user", "x/y", "x>y", "x\n/close", "x -->\n\n<!-- argus:fp=forged"])
def test_render_rejects_malformed_fingerprint(fp):
    with pytest.raises(ValueError):
        body.render_gitlab_comment(finding(fingerprint=fp))


@pytest.mark.parametrize("fp", [None, ""])
@pytest.mark.parametrize("unverified", [False, True])
def test_render_keeps_empty_fingerprint_marker(fp, unverified):
    item = finding(fingerprint=fp, unverified=unverified, description="Description")
    assert body.render_gitlab_comment(item) == render_comment(item)
    assert body.parse_marker(body.render_gitlab_comment(item)) is None


def test_github_rendering_unchanged_by_first_import():
    # A fresh process ensures the comparison really precedes the first import.
    script = """
import importlib
import json
import sys
from argus.publish import render_comment
finding = json.loads(sys.argv[1])
before = render_comment(finding)
importlib.import_module('argus.gitlab.body')
assert render_comment(finding) == before
"""
    subprocess.run([sys.executable, "-c", script, json.dumps(finding())], check=True, capture_output=True, text=True)
    original = render_comment(finding())
    importlib.import_module("argus.gitlab.body")
    assert render_comment(finding()) == original
