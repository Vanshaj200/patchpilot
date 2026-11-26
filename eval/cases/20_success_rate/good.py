def success_rate(results: list[bool]) -> float:
    """Return the share of successful results, or 0.0 if there are none."""
    if not results:
        return 0.0
    return sum(results) / len(results)
