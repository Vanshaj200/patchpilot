import pytest

from patchpilot.config import PRICES
from patchpilot.cost import TokenUsage, cost_usd, estimate_tokens

MILLION = 1_000_000


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (TokenUsage(MILLION, 0), 4.0),
        (TokenUsage(0, MILLION), 20.0),
        (TokenUsage(0, 0, cache_write_tokens=MILLION), 5.0),
        (TokenUsage(0, 0, cache_read_tokens=MILLION), 0.2),
    ],
)
def test_each_token_kind_has_its_own_price(usage: TokenUsage, expected: float) -> None:
    assert cost_usd("claude-opus-5-5", usage, PRICES) == pytest.approx(expected)


def test_dated_model_id_uses_the_undated_price() -> None:
    usage = TokenUsage(MILLION, 0)

    assert cost_usd("claude-haiku-4-5-20251001", usage, PRICES) == pytest.approx(1.0)


def test_unknown_model_has_no_cost() -> None:
    assert cost_usd("claude-unreleased", TokenUsage(1, 1), PRICES) is None


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        # At the limit the normal price applies.
        (TokenUsage(100_000, 1000), (100_000 * 0.1 + 1000 * 0.5) / 1e6),
        # One token over, the whole request is charged five times as much.
        (TokenUsage(100_001, 1000), (100_001 * 0.5 + 1000 * 2.5) / 1e6),
        # Cached tokens count towards the prompt length.
        (TokenUsage(1, 0, cache_read_tokens=100_000), (0.5 + 100_000 * 0.05) / 1e6),
    ],
)
def test_long_prompt_pricing(usage: TokenUsage, expected: float) -> None:
    assert cost_usd("claude-haiku-5-5", usage, PRICES) == pytest.approx(expected)


@pytest.mark.parametrize(("text", "tokens"), [("", 0), ("abcd", 1), ("abcde", 2)])
def test_estimate_tokens(text: str, tokens: int) -> None:
    assert estimate_tokens(text) == tokens
