"""Prepare GitLab comment text without quick actions or mention notifications."""

import html
import re

from argus.publish import render_comment


_EXTRA_LEADING_SPACE = frozenset("\u200b\u200c\u200d\u2060\ufeff")
_LINE_ENDINGS = "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"
_ENTITY = re.compile(r"&(?:#[0-9]+|#[xX][0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]*);?")
_UNSAFE_AT = re.compile(r"@(?!\u200b)")
_FINGERPRINT = re.compile(r"[A-Za-z0-9_.:-]+")
_HTML_MARKER = re.compile(r"<!-- argus:(fp|ufp)=([A-Za-z0-9_.:-]+) -->")
_REF_MARKER = re.compile(r"\[//\]: # \(argus:(fp|ufp)=([A-Za-z0-9_.:-]+)\)")


class UnsafeBodyError(ValueError):
    """The body contains a possible quick action or mention."""


def _is_leading_space(ch: str) -> bool:
    return ch.isspace() or ch in _EXTRA_LEADING_SPACE


def _command_offset(line: str) -> int | None:
    for offset, ch in enumerate(line.rstrip(_LINE_ENDINGS)):
        if not _is_leading_space(ch):
            return offset if ch == "/" else None
    return None


def neutralize_quick_actions(text: str) -> str:
    """Escape every candidate independently so fence breakouts cannot expose commands."""
    result = []
    for line in text.splitlines(keepends=True):
        offset = _command_offset(line)
        result.append(line if offset is None else line[:offset] + "\\" + line[offset:])
    return "".join(result)


def assert_safe(text: str) -> None:
    for number, line in enumerate(text.splitlines(keepends=True), 1):
        if _command_offset(line) is not None:
            raise UnsafeBodyError(f"Possible quick action on line {number}")


def _decode_entity(entity: str) -> str:
    if entity.startswith("&#"):
        digits = entity[2:].removesuffix(";")
        prefix = digits[0] if digits.startswith(("x", "X")) else ""
        digits = digits[len(prefix):].lstrip("0") or "0"
        # Unicode needs at most seven digits; longer values must not reach int conversion.
        if len(digits) > 7:
            return entity
        entity = f"&#{prefix}{digits};"
    return html.unescape(entity)


def neutralize_mentions(text: str) -> str:
    def replace_entity(match: re.Match) -> str:
        decoded = _decode_entity(match[0])
        return decoded.replace("@", "@\u200b") if "@" in decoded else match[0]

    return _UNSAFE_AT.sub("@\u200b", _ENTITY.sub(replace_entity, text))


def assert_mentions_safe(text: str) -> None:
    if _UNSAFE_AT.search(text) or any("@" in _decode_entity(m[0]) for m in _ENTITY.finditer(text)):
        raise UnsafeBodyError("Possible mention in body")


def assert_body_safe(text: str) -> None:
    assert_safe(text)
    assert_mentions_safe(text)


def marker(fingerprint: str, *, unverified: bool = False, form: str = "html") -> str:
    if not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint):
        raise ValueError("Invalid fingerprint")
    kind = "ufp" if unverified else "fp"
    if form == "html":
        return f"<!-- argus:{kind}={fingerprint} -->"
    if form == "ref":
        return f"[//]: # (argus:{kind}={fingerprint})"
    raise ValueError("Unknown marker form")


def parse_marker_kind(body: str) -> tuple[str, str] | None:
    # GitLab breaks lines on \n only, so \v, \f and similar must not hide or fake a marker line.
    lines = re.split(r"\r?\n", body.rstrip())
    if not lines:
        return None
    match = _HTML_MARKER.fullmatch(lines[-1]) or _REF_MARKER.fullmatch(lines[-1])
    return (match[1], match[2]) if match else None


def parse_marker(body: str) -> str | None:
    parsed = parse_marker_kind(body)
    return parsed[1] if parsed else None


def render_gitlab_comment(finding: dict) -> str:
    rendered = render_comment(finding)
    fingerprint = finding.get("fingerprint") or ""
    # Validate before splitting so a fingerprint cannot inject another trailing marker.
    rebuilt = marker(str(fingerprint), unverified=bool(finding.get("unverified"))) if fingerprint else None
    content, _, trailing_marker = rendered.rpartition("\n\n")
    safe = neutralize_quick_actions(neutralize_mentions(content))
    result = safe + "\n\n" + (rebuilt if rebuilt is not None else trailing_marker)
    assert_body_safe(result)
    return result
