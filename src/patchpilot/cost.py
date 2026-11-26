"""Cost of a request from its token counts and a price table."""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass

from patchpilot.config import ModelPrice

_DATE_SUFFIX = re.compile(r"-\d{8}$")
_CHARS_PER_TOKEN = 4


@dataclass(frozen=True)
class TokenUsage:
    """Token counts of one model attempt, as the API reports them."""

    input_tokens: int  # input that was neither written to nor read from the prompt cache
    output_tokens: int
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0


def cost_usd(model: str, usage: TokenUsage, prices: Mapping[str, ModelPrice]) -> float | None:
    """Return the cost in US dollars, or None if the model has no price (never guess one)."""
    # The API may answer with a dated ID such as "claude-haiku-4-5-20251001".
    price = prices.get(model) or prices.get(_DATE_SUFFIX.sub("", model))
    if price is None:
        return None
    total = (
        usage.input_tokens * price.input
        + usage.output_tokens * price.output
        + usage.cache_write_tokens * price.cache_write
        + usage.cache_read_tokens * price.cache_read
    )
    prompt_tokens = usage.input_tokens + usage.cache_write_tokens + usage.cache_read_tokens
    if price.long_prompt_over is not None and prompt_tokens > price.long_prompt_over:
        total *= price.long_prompt_multiplier
    return total / 1_000_000


def estimate_tokens(text: str) -> int:
    """Rough token count for text that is never sent. Billing always uses the API's counts."""
    return math.ceil(len(text) / _CHARS_PER_TOKEN)
