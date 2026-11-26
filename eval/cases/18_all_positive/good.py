def all_positive(values: list[int]) -> bool:
    """Return True if every value is greater than zero."""
    for value in values:
        if value <= 0:
            return False
    return True
