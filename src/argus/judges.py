"""Which model is allowed to second-guess the reviewer.

A judge exists to catch the reviewer being wrong. Two models from the same family
share training data, tuning and therefore error modes, so their agreement means
only that both made the same mistake in the same direction. A same-family judge
raises confidence without adding information, which is worse than no judge: it
mints `consensus` evidence out of a correlated echo.

The family a judge must avoid is therefore the REVIEWER's, and not a fixed vendor.
The rule this replaces banned Anthropic judges outright, which was correct only
while the reviewer was always Claude. Once the reviewer became an OpenAI model
(gpt-6.1-sol today) that ban forbade the one family that was still decorrelated,
so an absolute rule that started as a safeguard had quietly inverted into a bug.

Both judges are served by the same OpenAI-compatible route, which speaks the chat shape
for Claude and GPT models alike. Each carries its own profile-bound token so the
router attributes budget and telemetry per judge.
"""

import os
from dataclasses import dataclass

ANTHROPIC = "anthropic"
OPENAI = "openai"
UNKNOWN = None

ENV_BASE_URL = "ARGUS_JUDGE_BASE_URL"
ENV_ANTHROPIC_MODEL = "ARGUS_JUDGE_ANTHROPIC_MODEL"
ENV_ANTHROPIC_KEY = "ARGUS_JUDGE_ANTHROPIC_KEY"
ENV_OPENAI_MODEL = "ARGUS_JUDGE_OPENAI_MODEL"
ENV_OPENAI_KEY = "ARGUS_JUDGE_OPENAI_KEY"

# Matched against the model id after the optional `vendor/` prefix is stripped.
# Ordered longest-first per family so a specific id cannot be caught by a looser
# sibling. An id matching nothing here is UNKNOWN, never guessed.
_FAMILY_MARKERS = (
    (ANTHROPIC, ("claude", "anthropic", "opus", "sonnet", "haiku", "fable")),
    (OPENAI, ("gpt", "openai", "codex", "o1-", "o3-", "o4-")),
)

# The vendor prefix a gateway exposes is authoritative when present: `openai/...` is
# an OpenAI model whatever the suffix is called.
_VENDOR_PREFIX = {"anthropic": ANTHROPIC, "openai": OPENAI}


class NoDecorrelatedJudge(Exception):
    """No judge can be proven to differ in family from this reviewer.

    Raised rather than falling back to any available judge. A judge whose
    independence cannot be shown produces agreement that means nothing, and the
    verdict document would report that agreement as `consensus` evidence.
    """


@dataclass(frozen=True)
class JudgeSpec:
    family: str
    model: str
    url: str
    key: str


def family_of(model: str | None) -> str | None:
    """The model's family, or UNKNOWN when the id matches nothing configured.

    Never guesses. An unrecognised id is reported as unknown so the caller can
    refuse rather than assume, which is the difference between this and the rule
    it replaces.
    """
    if not model:
        return UNKNOWN
    ident = model.strip().lower()
    vendor, _, rest = ident.partition("/")
    if rest and vendor in _VENDOR_PREFIX:
        return _VENDOR_PREFIX[vendor]
    if rest:
        ident = rest
    for family, markers in _FAMILY_MARKERS:
        if any(marker in ident for marker in markers):
            return family
    return UNKNOWN


def _configured(family: str) -> JudgeSpec | None:
    """The judge configured for this family, or None if the slot is empty.

    Raises when the slot holds a model of some other family. Which variable a
    model id was typed into is a claim, not proof: a grok id parked in the
    anthropic slot would otherwise be handed out as a spec asserting
    `family: anthropic`, and the lane records that family in the coverage block.
    A spec must never claim a family its own model does not have.
    """
    model_var, key_var = (
        (ENV_ANTHROPIC_MODEL, ENV_ANTHROPIC_KEY) if family == ANTHROPIC
        else (ENV_OPENAI_MODEL, ENV_OPENAI_KEY)
    )
    model = os.environ.get(model_var)
    key = os.environ.get(key_var)
    if not model or not key:
        return None
    url = os.environ.get(ENV_BASE_URL, "").strip()
    if not url:
        raise NoDecorrelatedJudge("ARGUS_JUDGE_BASE_URL not set")
    actual = family_of(model)
    if actual != family:
        raise NoDecorrelatedJudge(
            f"{model_var} is set to {model!r}, whose family is "
            f"{actual or 'unknown'}, not {family}; a judge may not claim a family "
            "its model does not have"
        )
    return JudgeSpec(family=family, model=model, url=url, key=key)


def judge_for(reviewer_model: str | None) -> JudgeSpec:
    """The judge that does not share the reviewer's family.

    Raises NoDecorrelatedJudge when the reviewer's family cannot be identified, or
    when the counterpart judge is not configured. Both are reported rather than
    substituted: the caller records a skipped lane, which is a true statement,
    where a same-family fallback would be a false one.
    """
    reviewer_family = family_of(reviewer_model)
    if reviewer_family is UNKNOWN:
        raise NoDecorrelatedJudge(
            f"reviewer model {reviewer_model!r} has an unknown family, so no judge "
            "can be shown to differ from it"
        )
    counterpart = OPENAI if reviewer_family == ANTHROPIC else ANTHROPIC
    spec = _configured(counterpart)
    if spec is None:
        raise NoDecorrelatedJudge(
            f"reviewer is {reviewer_family}, so the judge must be {counterpart}, "
            f"which is not configured"
        )
    return spec
