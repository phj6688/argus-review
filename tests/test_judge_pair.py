"""The live judge lane picks its judge from the reviewer, and says which it used."""

import pytest

from argus.jarvis import automark, judges
from argus.schema import Finding, Severity


@pytest.fixture()
def both(monkeypatch):
    monkeypatch.setenv(judges.ENV_BASE_URL, "http://gateway.test/v1/chat/completions")
    monkeypatch.setenv(judges.ENV_ANTHROPIC_MODEL, "claude-opus-5-5")
    monkeypatch.setenv(judges.ENV_ANTHROPIC_KEY, "claude-key")
    monkeypatch.setenv(judges.ENV_OPENAI_MODEL, "gpt-6.1-sol")
    monkeypatch.setenv(judges.ENV_OPENAI_KEY, "gpt-key")


@pytest.fixture()
def sent(monkeypatch):
    """Capture what the judge lane would POST, without any network."""
    calls = []

    class _Response:
        status_code = 200

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"choices": [{"message": {
                "content": '{"verdict":"real","confidence":0.9,"evidence":"e"}'}}]}

    def _post(url, *, timeout=None, headers=None, json=None):
        calls.append({"url": url, "headers": headers or {}, "body": json})
        return _Response()

    monkeypatch.setattr(automark.httpx, "post", _post)
    return calls


def _finding() -> Finding:
    return Finding(file="a.py", line=3, severity=Severity.major, confidence=0.8,
                   title="shell injection", description="d", proposed_fix="f")


def test_a_gpt_reviewer_is_judged_by_the_claude_profile(both, sent):
    out = automark.judge_finding(_finding(), "  3: os.system(cmd)",
                                 reviewer_model="gpt-6.1-sol")
    assert out["verdict"] == "real"
    assert sent[0]["body"]["model"] == "claude-opus-5-5"
    assert sent[0]["headers"]["Authorization"] == "Bearer claude-key"


def test_a_claude_reviewer_is_judged_by_the_gpt_profile(both, sent):
    automark.judge_finding(_finding(), "  3: os.system(cmd)",
                           reviewer_model="claude-opus-5-5")
    assert sent[0]["body"]["model"] == "gpt-6.1-sol"
    assert sent[0]["headers"]["Authorization"] == "Bearer gpt-key"


def test_a_claude_judge_is_no_longer_banned_outright(both, sent):
    """The blanket non-Anthropic ban forbade the only decorrelated judge once the
    reviewer moved to GPT. It must not fire on a GPT reviewer."""
    automark.judge_finding(_finding(), "  3: x", reviewer_model="gpt-6.1-sol")
    assert "claude" in sent[0]["body"]["model"]


def test_a_same_family_judge_is_still_refused(both, sent, monkeypatch):
    """Configure the anthropic judge for an anthropic reviewer by force; the
    relative guard must reject it rather than mint a correlated consensus."""
    spec = judges.JudgeSpec(family=judges.ANTHROPIC, model="claude-opus-5-5",
                            url="http://gateway.test/v1/chat/completions", key="k")
    with pytest.raises(ValueError, match="same family|decorrelat"):
        automark.default_judge({"title": "t", "severity": "major", "file": "a.py",
                                "line": 1, "description": "d", "snippet": "s"},
                               spec=spec, reviewer_model="claude-opus-5-5")
    assert sent == []


def test_a_judge_of_unprovable_family_is_refused(both, sent):
    """A spec built by hand can still carry a model whose family is unknown.
    Decorrelation that cannot be shown must not be assumed: grok may well differ
    from gpt, but the lane would publish that unproven difference as consensus."""
    spec = judges.JudgeSpec(family=judges.ANTHROPIC, model="x-ai/grok-4.3",
                            url="http://gateway.test/v1/chat/completions", key="k")
    with pytest.raises(ValueError, match="unknown family"):
        automark.default_judge({"title": "t", "severity": "major", "file": "a.py",
                                "line": 1, "description": "d", "snippet": "s"},
                               spec=spec, reviewer_model="gpt-6.1-sol")
    assert sent == []


def test_an_unidentifiable_reviewer_gets_no_judge_call(both, sent):
    with pytest.raises(judges.NoDecorrelatedJudge):
        automark.judge_finding(_finding(), "  3: x", reviewer_model="x-ai/grok-4.3")
    assert sent == []


def test_the_nightly_corpus_path_keeps_its_blanket_ban(monkeypatch, sent):
    """The nightly labeller judges findings of mixed provenance, so it has no one
    reviewer to be relative to. Its conservative absolute rule stays."""
    monkeypatch.setenv(automark.ENV_JUDGE_URL, "https://openrouter.ai/api/v1/chat/completions")
    monkeypatch.setenv(automark.ENV_JUDGE_MODEL, "anthropic/claude-opus-5-5")
    with pytest.raises(ValueError, match="NON-Anthropic"):
        automark.default_judge({"title": "t", "severity": "major", "file": "a.py",
                                "line": 1, "description": "d", "snippet": "s"})
    assert sent == []


def test_the_nightly_corpus_path_still_works_with_a_non_anthropic_judge(monkeypatch, sent):
    monkeypatch.setenv(automark.ENV_JUDGE_URL, "https://openrouter.ai/api/v1/chat/completions")
    monkeypatch.setenv(automark.ENV_JUDGE_MODEL, "z-ai/glm-5.2")
    out = automark.default_judge({"title": "t", "severity": "major", "file": "a.py",
                                  "line": 1, "description": "d", "snippet": "s"})
    assert out["verdict"] == "real"
    assert sent[0]["body"]["model"] == "z-ai/glm-5.2"
