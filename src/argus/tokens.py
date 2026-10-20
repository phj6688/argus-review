"""Token estimation for diff budgeting.

Budgeting offline uses a character heuristic on purpose: tiktoken is OpenAI's
tokenizer and undercounts Claude, and Anthropic's exact `count_tokens` needs a
live API call. The heuristic over-estimates (safety margin) so the packed diff
never overflows the model's real token count. Swap in an Anthropic-backed
estimator at call time when exactness matters.
"""

from dataclasses import dataclass
from typing import Protocol

CHARS_PER_TOKEN = 4
SAFETY_MARGIN = 1.15


class TokenEstimator(Protocol):
    def estimate(self, text: str) -> int: ...


@dataclass(frozen=True)
class HeuristicEstimator:
    chars_per_token: int = CHARS_PER_TOKEN
    safety_margin: float = SAFETY_MARGIN

    def estimate(self, text: str) -> int:
        return int(len(text) / self.chars_per_token * self.safety_margin)
