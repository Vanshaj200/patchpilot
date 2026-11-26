from collections.abc import Callable


def make_multipliers(count: int) -> list[Callable[[int], int]]:
    """Return functions where the function at index i multiplies its argument by i."""
    return [lambda value: value * factor for factor in range(count)]
