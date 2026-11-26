from collections.abc import Iterable


def total_and_count(amounts: Iterable[int]) -> tuple[int, int]:
    """Return the sum and the number of amounts. Any iterable may be passed."""
    total = sum(amounts)
    return total, len(list(amounts))
