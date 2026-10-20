"""Stable finding identity across review runs (SARIF partialFingerprints spirit).

Line numbers drift on every rebase, so identity hashes the flagged line's content
plus a rule/title key and the file path, never the line number. The same defect
re-reported by a later run keeps its fingerprint; a baseline fingerprint that no
re-review reports again is resolved. A collision is the intended dedup: the same
rule on the same line content in the same file IS the same finding.
"""

import hashlib
import re

PREFIX = "argusfp"

_WS = re.compile(r"\s+")
_DIGITS = re.compile(r"\d+")
_HUNK_NEW_START = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)")


def title_key(title: str) -> str:
    """Identity key for rule-less findings: lowercase, whitespace and digit runs
    collapsed, so an LLM restating "timeout of 30s" as "timeout of 60s" still
    names the same complaint."""
    return _DIGITS.sub("n", _WS.sub(" ", title.strip().lower()))


def context_line(patch: str | None, line: int) -> str:
    """Content of post-image `line` in a unified patch, stripped.

    Returns "" when the patch does not cover the line (overflow file, binary,
    or the finding points outside the hunks); identity then rests on the
    rule/title key + path alone, which is still a usable dedup bucket.
    """
    if not patch:
        return ""
    new_line = 0
    in_hunk = False
    for raw in patch.splitlines():
        header = _HUNK_NEW_START.match(raw)
        if header:
            new_line = int(header.group(1)) - 1
            in_hunk = True
            continue
        if not in_hunk or raw.startswith("-") or raw.startswith("\\"):
            continue
        new_line += 1
        if new_line == line:
            content = raw[1:] if raw.startswith("+") else raw.lstrip(" ")
            return _WS.sub(" ", content.strip())
    return ""


def compute(file: str, rule_id: str | None, title: str, context: str) -> str:
    """`argusfp:<sha256/32>` over (rule-or-title key, normalized path, line content)."""
    key = rule_id or title_key(title)
    path = file.replace("\\", "/").lstrip("./")
    ctx = _WS.sub(" ", context.strip())
    digest = hashlib.sha256(f"{key}\0{path}\0{ctx}".encode()).hexdigest()
    return f"{PREFIX}:{digest[:32]}"
