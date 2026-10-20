"""Fingerprint identity: stable across line drift, sensitive to content change."""

from argus import fingerprint

PATCH = """@@ -10,4 +10,5 @@ def handler():
 context_before
+    timeout = None
+    retries = 3
 context_after
@@ -30,2 +31,3 @@ def other():
 keep
+    dangerous_call()
"""


def test_context_line_finds_added_line():
    assert fingerprint.context_line(PATCH, 11) == "timeout = None"
    assert fingerprint.context_line(PATCH, 12) == "retries = 3"


def test_context_line_counts_context_lines_in_post_image():
    assert fingerprint.context_line(PATCH, 10) == "context_before"
    assert fingerprint.context_line(PATCH, 13) == "context_after"
    assert fingerprint.context_line(PATCH, 32) == "dangerous_call()"


def test_context_line_outside_hunks_is_empty():
    assert fingerprint.context_line(PATCH, 999) == ""
    assert fingerprint.context_line(None, 5) == ""
    assert fingerprint.context_line("", 5) == ""


def test_fingerprint_survives_line_renumbering():
    ctx = "timeout = None"
    before = fingerprint.compute("src/app.py", "py-timeout", "t", ctx)
    after = fingerprint.compute("src/app.py", "py-timeout", "t", ctx)
    assert before == after
    assert before.startswith("argusfp:")


def test_fingerprint_changes_with_content_and_rule():
    base = fingerprint.compute("src/app.py", "py-timeout", "t", "timeout = None")
    assert fingerprint.compute("src/app.py", "py-timeout", "t", "timeout = 30") != base
    assert fingerprint.compute("src/app.py", "py-retry", "t", "timeout = None") != base
    assert fingerprint.compute("src/other.py", "py-timeout", "t", "timeout = None") != base


def test_rule_less_findings_key_on_normalized_title():
    a = fingerprint.compute("f.py", None, "Timeout of 30s is unsafe", "x = 1")
    b = fingerprint.compute("f.py", None, "timeout  of 60s is unsafe", "x = 1")
    assert a == b


def test_title_normalization_collapses_digits_and_case():
    assert fingerprint.title_key("Retry 3 Times") == fingerprint.title_key("retry 500 times")


def test_path_normalization():
    a = fingerprint.compute("./src/app.py", "r", "t", "c")
    b = fingerprint.compute("src/app.py", "r", "t", "c")
    assert a == b
