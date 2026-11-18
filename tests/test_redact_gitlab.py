"""GitLab credentials are removed from stored and displayed text."""

import json
import re
import time

import pytest

from argus.redact import redact


PREFIXES = ("glpat", "gloas", "gldt", "glrt", "glcbt", "glptt", "glft", "glimt", "glagent", "glsoat",
            "glrtr", "glwt", "glffct")
BODY = "Fake1234.Part5678_Chunk9012-End3456"


@pytest.mark.parametrize("prefix", PREFIXES)
@pytest.mark.parametrize("context", ["text", "json", "mapping", "exception"])
def test_gitlab_token_body_is_fully_redacted(prefix, context):
    credential = prefix + "-" + BODY
    if context == "json":
        text = json.dumps({"message": credential})
    elif context == "mapping":
        text = str({"message": credential})
    elif context == "exception":
        error = RuntimeError(f"request failed with {credential}")
        text = f"{type(error).__name__}: {error}"
    else:
        text = f"request failed with {credential} in response"

    result = redact(text)
    assert "[redacted]" in result
    assert BODY not in result
    for fragment in re.split(r"[._]", BODY):
        if len(fragment) >= 4:
            assert fragment not in result


@pytest.mark.parametrize("prefix", PREFIXES)
@pytest.mark.parametrize("template", ["before_{}", "https://gitlab.test/path/{}/end", "{}.", "{},"])
def test_gitlab_tokens_are_redacted_at_text_boundaries(prefix, template):
    credential = prefix + "-" + BODY
    result = redact(template.format(credential))
    assert "[redacted]" in result
    for fragment in re.split(r"[._]", BODY):
        assert fragment not in result


@pytest.mark.parametrize("body", ["Ab12.C_d", "Ab12.C_"])
def test_gitlab_token_body_minimum_length(body):
    credential = "glpat" + "-" + body
    assert redact(credential) == ("[redacted]" if len(body) >= 8 else credential)


def test_configured_gitlab_token_is_redacted_without_a_known_prefix(monkeypatch):
    credential = "synthetic credential for testing"
    monkeypatch.setenv("ARGUS_GITLAB_TOKEN", credential)
    assert redact(f"before {credential} after") == "before [redacted] after"
    monkeypatch.setenv("ARGUS_GITLAB_TOKEN", "another synthetic credential")
    assert redact("another synthetic credential") == "[redacted]"


@pytest.mark.parametrize("text", ["glpat", "the glpat- prefix", "glider-plane-1"])
def test_ordinary_words_are_not_redacted(text):
    assert redact(text) == text


def test_long_letters_are_redacted_in_under_two_seconds():
    text = "a" * 100_000
    started = time.perf_counter()
    result = redact(text)
    elapsed = time.perf_counter() - started
    assert result == text
    assert elapsed < 2.0


@pytest.mark.parametrize("text,expected", [
    ("https://reviewer:password@example.test/repo", "https://[redacted]@example.test/repo"),
    ("https://example.test/repo?private=value", "https://example.test/repo?[redacted]"),
    ("https://reviewer:password@example.test/repo?private=value",
     "https://[redacted]@example.test/repo?[redacted]"),
])
def test_url_credentials_are_still_redacted(text, expected):
    assert redact(text) == expected
