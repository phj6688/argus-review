"""Judge selection: the judge must never share a model family with the reviewer.

The old rule banned Anthropic judges outright. That was the right answer only
while the reviewer was always Claude. The reviewer is gpt-6.1-sol now, so the
blanket ban forbids the one family that is still decorrelated. The rule is
relative, not absolute.
"""

import pytest

from argus.jarvis import judges


@pytest.fixture()
def both(monkeypatch):
    monkeypatch.setenv(judges.ENV_BASE_URL, "http://gateway.test/v1/chat/completions")
    monkeypatch.setenv(judges.ENV_ANTHROPIC_MODEL, "claude-opus-5-5")
    monkeypatch.setenv(judges.ENV_ANTHROPIC_KEY, "sk-judge-claude-x")
    monkeypatch.setenv(judges.ENV_OPENAI_MODEL, "gpt-6.1-sol")
    monkeypatch.setenv(judges.ENV_OPENAI_KEY, "sk-judge-gpt-x")


# claude-haiku-4-5-20251001, claude-opus-4-8 and gpt-5.5 are retired ids kept on
# purpose: the check keys on the id text, not on a served list.
@pytest.mark.parametrize("model, family", [
    ("claude-opus-5-5", judges.ANTHROPIC),
    ("claude-sonnet-5-5", judges.ANTHROPIC),
    ("claude-fable-5-1", judges.ANTHROPIC),
    ("claude-haiku-4-5-20251001", judges.ANTHROPIC),
    ("anthropic/claude-opus-5-5", judges.ANTHROPIC),
    ("claude-opus-4-8", judges.ANTHROPIC),
    ("gpt-6.1-sol", judges.OPENAI),
    ("gpt-5.6-terra", judges.OPENAI),
    ("gpt-5.5", judges.OPENAI),
    ("openai/gpt-6.1-sol", judges.OPENAI),
    ("codex-auto-review", judges.OPENAI),
    ("gpt-6-astra", judges.OPENAI),
])
def test_family_of_recognises_both_configured_families(model, family):
    assert judges.family_of(model) == family


@pytest.mark.parametrize("model", ["x-ai/grok-4.3", "z-ai/glm-5.2", "ollama/qwen3.5:35b-a3b", ""])
def test_family_of_reports_unknown_rather_than_guessing(model):
    assert judges.family_of(model) is judges.UNKNOWN


def test_a_gpt_reviewer_is_judged_by_claude(both):
    spec = judges.judge_for("gpt-6.1-sol")
    assert spec.family == judges.ANTHROPIC
    assert spec.model == "claude-opus-5-5"
    assert spec.url.endswith("/v1/chat/completions")


def test_a_claude_reviewer_is_judged_by_gpt(both):
    spec = judges.judge_for("claude-opus-5-5")
    assert spec.family == judges.OPENAI
    assert spec.model == "gpt-6.1-sol"


@pytest.mark.parametrize("reviewer", [
    "claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-4-8", "anthropic/claude-fable-5-1",
    "gpt-6.1-sol", "gpt-5.6-terra", "openai/gpt-5.5", "codex-auto-review",
])
def test_the_judge_never_shares_the_reviewers_family(both, reviewer):
    """The invariant the whole lane exists for."""
    spec = judges.judge_for(reviewer)
    assert spec.family != judges.family_of(reviewer)


def test_an_unidentifiable_reviewer_gets_no_judge(both):
    """We cannot prove a grok reviewer differs from either judge, and an
    unprovable decorrelation must not mint consensus-tier evidence. Absence of
    proof is reported, not assumed away."""
    with pytest.raises(judges.NoDecorrelatedJudge) as excinfo:
        judges.judge_for("x-ai/grok-4.3")
    assert "unknown" in str(excinfo.value).lower()


def test_a_missing_counterpart_judge_is_refused_not_substituted(monkeypatch):
    """Only the GPT judge is configured, so a GPT reviewer has no counterpart.
    Falling back to the same-family judge would be worse than no judge."""
    monkeypatch.setenv(judges.ENV_BASE_URL, "http://gateway.test/v1/chat/completions")
    monkeypatch.delenv(judges.ENV_ANTHROPIC_KEY, raising=False)
    monkeypatch.delenv(judges.ENV_ANTHROPIC_MODEL, raising=False)
    monkeypatch.setenv(judges.ENV_OPENAI_MODEL, "gpt-6.1-sol")
    monkeypatch.setenv(judges.ENV_OPENAI_KEY, "k")
    with pytest.raises(judges.NoDecorrelatedJudge):
        judges.judge_for("gpt-6.1-sol")
    # The other direction still works: a Claude reviewer has its GPT counterpart.
    assert judges.judge_for("claude-opus-5-5").family == judges.OPENAI


def test_a_model_in_the_wrong_family_slot_is_refused(monkeypatch):
    """The slot an env var occupies is a claim about the model, not proof. A grok
    id parked in the anthropic slot would otherwise be handed out as a spec
    claiming `family: anthropic`, and the judge lane would record that false
    family in coverage and mint consensus from it."""
    monkeypatch.setenv(judges.ENV_BASE_URL, "http://gateway.test/v1/chat/completions")
    monkeypatch.setenv(judges.ENV_ANTHROPIC_MODEL, "x-ai/grok-4.3")
    monkeypatch.setenv(judges.ENV_ANTHROPIC_KEY, "k")
    monkeypatch.setenv(judges.ENV_OPENAI_MODEL, "gpt-6.1-sol")
    monkeypatch.setenv(judges.ENV_OPENAI_KEY, "k")
    with pytest.raises(judges.NoDecorrelatedJudge, match="grok"):
        judges.judge_for("gpt-6.1-sol")


def test_a_spec_never_claims_a_family_its_model_does_not_have(both):
    """The invariant behind the previous test, asserted directly."""
    for reviewer in ("gpt-6.1-sol", "claude-opus-5-5"):
        spec = judges.judge_for(reviewer)
        assert judges.family_of(spec.model) == spec.family


def test_the_slot_check_does_not_reject_a_correctly_slotted_model(monkeypatch):
    monkeypatch.setenv(judges.ENV_BASE_URL, "u")
    monkeypatch.setenv(judges.ENV_ANTHROPIC_MODEL, "anthropic/claude-sonnet-5-5")
    monkeypatch.setenv(judges.ENV_ANTHROPIC_KEY, "k")
    assert judges.judge_for("gpt-6.1-sol").model == "anthropic/claude-sonnet-5-5"


def test_no_judges_configured_is_refused(monkeypatch):
    for var in (judges.ENV_ANTHROPIC_KEY, judges.ENV_ANTHROPIC_MODEL,
                judges.ENV_OPENAI_KEY, judges.ENV_OPENAI_MODEL):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(judges.NoDecorrelatedJudge):
        judges.judge_for("gpt-6.1-sol")


@pytest.mark.parametrize("base", [None, "", " \t"])
def test_configured_judge_requires_an_explicit_base_url(both, monkeypatch, base):
    if base is None:
        monkeypatch.delenv(judges.ENV_BASE_URL, raising=False)
    else:
        monkeypatch.setenv(judges.ENV_BASE_URL, base)
    with pytest.raises(judges.NoDecorrelatedJudge, match="^ARGUS_JUDGE_BASE_URL not set$"):
        judges.judge_for("gpt-6.1-sol")
