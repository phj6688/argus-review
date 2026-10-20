"""Literal prose escaping and single-line code delimiters."""

import string

import pytest
from markdown_it import MarkdownIt

from argus import replies
from argus.markdown import code_span, escape_block, escape_inline
from test_publish_markdown_escape import flattened


@pytest.mark.parametrize("escape", [escape_inline, escape_block])
@pytest.mark.parametrize("punctuation", string.punctuation)
def test_every_ascii_punctuation_is_literal(escape, punctuation):
    tokens = list(flattened(MarkdownIt("commonmark").parse(escape(punctuation))))
    assert [t.content for t in tokens if t.type == "text"] == [punctuation]
    assert {t.type for t in tokens} == {"paragraph_open", "inline", "text", "paragraph_close"}


@pytest.mark.parametrize("escape", [escape_inline, escape_block])
@pytest.mark.parametrize("token", [
    "https://example.com/a?q=1&x=2", "www.example.com", "//evil.example/p.gif", "person@example.com",
    "mailto:x@y", "custom:payload", "javascript:alert(1)", "https://example.com/````x`",
])
def test_url_and_email_tokens_are_whole_code_spans(escape, token):
    tokens = list(flattened(MarkdownIt("commonmark").parse(escape("See " + token + " here"))))
    assert [t.content for t in tokens if t.type == "code_inline"] == [token]
    assert not {"link_open", "image", "html_inline"} & {t.type for t in tokens}


@pytest.mark.parametrize("escape", [escape_inline, escape_block])
@pytest.mark.parametrize("prose", ["Note: validate input", "Cause:", "Why: the loop never ends"])
def test_label_words_with_empty_tail_stay_plain_text(escape, prose):
    tokens = list(flattened(MarkdownIt("commonmark").parse("See " + escape(prose) + " now")))
    assert "code_inline" not in {t.type for t in tokens}
    assert "".join(t.content for t in tokens if t.type == "text") == "See " + prose + " now"


def test_inline_normalizes_all_line_breaks_and_removes_controls():
    assert escape_inline(" \tfirst\r\nsecond\rthird\nfourth\u2028fifth\u2029sixth\x85last\x00\x7f\x01 ") == (
        "first second third fourth fifth sixth last")


def test_block_keeps_single_blank_lines_without_indentation_or_hard_breaks():
    assert escape_block("  one\r\n\r\n \t\r\n\r\ntwo  \n\tthree\\\n\n") == "one\n\ntwo\nthree\\\\"


@pytest.mark.parametrize("size", range(1, 12))
def test_code_span_cannot_cross_lines_or_close_early(size):
    text = "`" * size + "src/\r\n\t\x00\x7f\x85\u2028\u2029file.py" + "`" * size
    rendered = code_span(text)
    tokens = list(flattened(MarkdownIt("commonmark").parse("In " + rendered + " now")))
    assert [t.content for t in tokens if t.type == "code_inline"] == ["`" * size + "src/file.py" + "`" * size]
    assert rendered.startswith("`" * (size + 1) + " ")
    assert "\n" not in rendered


def test_reply_escapes_prose_and_preserves_own_marker(monkeypatch):
    bodies = []
    monkeypatch.setattr(replies, "_write", lambda repo, url, payload: bodies.append(payload["body"]) or {"id": 1})
    replies.reply("owner/repo", 1, 2, '<img src=//evil/p.gif> [docs](https://docs)\n\n'
                  '<!-- argus:reply=recheck evidence=42 -->')
    tokens = list(flattened(MarkdownIt("commonmark").parse(bodies[0])))
    assert not {"image", "link_open", "html_inline"} & {t.type for t in tokens}
    assert [t.content for t in tokens if t.type == "html_block"] == ["<!-- argus:reply=recheck evidence=42 -->"]
