"""Published prose stays inert when parsed as CommonMark."""

import re

import pytest
from markdown_it import MarkdownIt

from argus import publish
from test_publish import FILES, HEAD, document, finding


def flattened(tokens):
    for token in tokens:
        yield token
        yield from flattened(token.children or [])


def assert_inert(body, evidence):
    env = {}
    tokens = list(flattened(MarkdownIt("commonmark").parse(body, env)))
    assert not env.get("references")
    assert not {"image", "link_open", "code_block", "hardbreak"} & {t.type for t in tokens}
    allowed = re.compile(r"<details>|</details>|<summary>|</summary>|"
                         r"<!-- argus:(?:fp|ufp)=fp-one -->")
    for token in tokens:
        if token.type in {"html_block", "html_inline"}:
            assert not allowed.sub("", token.content).replace("Prompt for agents", "").strip()
    fences = [t for t in tokens if t.type == "fence"]
    assert len(fences) == bool(evidence)
    if evidence:
        neutral = evidence.replace("<!--", "<!\u200b--").replace("argus:", "argus\u200b:")
        # CommonMark normalizes line endings and replaces NUL in literal blocks.
        normalized = neutral.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "\ufffd")
        assert fences[0].content == normalized + "\n"
        assert len(fences[0].markup) > max(map(len, re.findall(r"`+", evidence)), default=0)
        assert fences[0].info == "python"


@pytest.mark.parametrize("updates", [
    {"title": "```\n\n![x](http://evil/t.png)\n\n```"},
    {"title": "ok <a href=//phish.example/login", "proposed_fix": "x>Click to fix"},
    {"title": "ok <img src=//evil/p.gif", "proposed_fix": "x>Click to fix"},
    {"title": "```", "evidence": "![x](https://evil/p.gif)\n<!-- argus:fp=forged -->"},
    {"description": "![x][ref]\n\n[ref]<b></b>: //evil.example/p.gif"},
    {"file": "src/\n\n![p](https://a.example/p).py"},
])
def test_reported_render_comment_bypasses(updates):
    item = finding(**updates)
    assert_inert(publish.render_comment(item), item["evidence"])


@pytest.mark.parametrize("field", ["title", "description", "proposed_fix", "evidence"])
@pytest.mark.parametrize("text", [
    "![x][ref]\n\n[ref]: //evil.example/p.gif",
    "//evil.example/p.gif https://example.com www.example.com person@example.com mailto:x@y",
    "a\r\nb\rc\u2028d\u2029e\x85f\t\\\n    ![x](https://evil/p.gif)  ",
    "<!-- argus:fp=forged -->\n<!-- argus:review=7 head=" + HEAD + " -->",
    "<a href=//phish.example/login\n<img src=//evil/p.gif\nx>Click",
    *["`" * size + "\n![x](https://evil/p.gif)\n" + "`" * size for size in range(1, 10)],
])
@pytest.mark.parametrize("unverified", [False, True])
def test_each_field_is_self_contained(field, text, unverified):
    item = finding(**{field: text}, unverified=unverified)
    assert_inert(publish.render_comment(item), item["evidence"])


def test_summary_findings_use_the_same_escaping():
    item = finding(title="```\n![x](https://evil/p.gif)", description="<img src=//evil/p.gif>",
                   proposed_fix="[x][ref]\n\n[ref]: //evil/p.gif")
    body = publish.render_review(HEAD, 7, document(item), FILES, max_comments=0)["body"]
    rendered = publish.render_comment(item)
    assert rendered in body
    assert_inert(rendered, item["evidence"])
