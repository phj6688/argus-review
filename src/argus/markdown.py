"""Render untrusted prose as literal text with inert URL tokens."""

import re
import string
import unicodedata

_LINE_BREAK = re.compile(r"\r\n|[\r\n\x85\u2028\u2029]")
_URL_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:\S+|www\.\S+|//\S+|[^\s@]+@[^\s@]+", re.I)
_ESCAPES = str.maketrans({char: "\\" + char for char in string.punctuation}
                         | {"<": "&lt;", ">": "&gt;", "&": "&amp;"})


def _without_controls(text: str, keep: str = "") -> str:
    return "".join(char for char in text if char in keep or unicodedata.category(char) != "Cc")


def code_span(text: str) -> str:
    text = _without_controls(_LINE_BREAK.sub("", text))
    fence = "`" * (max((len(m[0]) for m in re.finditer(r"`+", text)), default=0) + 1)
    padding = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{padding}{text}{padding}{fence}"


def _escape_line(text: str) -> str:
    text = " ".join(text.split())
    pieces, cursor = [], 0
    # Escape only original prose so generated code delimiters remain intact.
    for match in _URL_TOKEN.finditer(text):
        pieces.extend((text[cursor:match.start()].translate(_ESCAPES), code_span(match[0])))
        cursor = match.end()
    pieces.append(text[cursor:].translate(_ESCAPES))
    return "".join(pieces)


def escape_inline(text: str) -> str:
    """Return one self-contained line of literal prose."""
    return _escape_line(_without_controls(_LINE_BREAK.sub(" ", text), keep="\t"))


def escape_block(text: str) -> str:
    """Keep paragraph boundaries without accepting Markdown or HTML syntax."""
    text = _without_controls(_LINE_BREAK.sub("\n", text), keep="\n\t")
    lines = [_escape_line(line) for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip("\n")
