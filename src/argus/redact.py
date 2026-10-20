"""Strip credentials from text that leaves the process (stored errors, API bodies)."""

import os
import re

REDACTED = "[redacted]"

# Values of these variables are removed wherever they appear verbatim.
_SECRET_ENV = (
    "ARGUS_API_TOKEN", "ARGUS_HEALTH_TOKEN", "ARGUS_PUBLISH_TOKEN", "GITHUB_TOKEN",
    "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY",
    "LINEAR_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ARGUS_JUDGE_ANTHROPIC_KEY",
    "ARGUS_JUDGE_OPENAI_KEY", "ARGUS_AUTOMARK_JUDGE_KEY",
)
_MIN_SECRET_LENGTH = 8

_USERINFO = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^\s/@]+@")
_URL_QUERY = re.compile(r"(?P<url>[A-Za-z][A-Za-z0-9+.-]*://[^\s?#]+)\?[^\s]*")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_ASSIGNED = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|password)"
    r"(\s*[=:]\s*)[^\s,;&\"']{4,}")
# JSON or Python mapping entries: "password": "value" or 'token': 'value'. The key
# may be quoted, the value is quoted, and the value ends at the matching quote.
_QUOTED_ASSIGNED = re.compile(
    r"(?i)(?P<key>[\"']?\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|password)"
    r"[\"']?\s*[=:]\s*)(?P<quote>[\"'])(?:\\.|(?!(?P=quote)).)+(?P=quote)")
_KNOWN_PREFIXES = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,})")


def redact(text: str | None) -> str | None:
    """Return text with keys, tokens and credential-bearing URL parts removed."""
    if not text:
        return text
    for name in _SECRET_ENV:
        value = os.environ.get(name, "").strip()
        if len(value) >= _MIN_SECRET_LENGTH:
            text = text.replace(value, REDACTED)
    text = _USERINFO.sub(lambda m: f"{m.group('scheme')}{REDACTED}@", text)
    text = _URL_QUERY.sub(lambda m: f"{m.group('url')}?{REDACTED}", text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = _QUOTED_ASSIGNED.sub(lambda m: f"{m.group('key')}{m.group('quote')}{REDACTED}{m.group('quote')}", text)
    text = _ASSIGNED.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    return _KNOWN_PREFIXES.sub(REDACTED, text)
