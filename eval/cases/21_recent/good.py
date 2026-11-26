def latest(entries: list[str], count: int) -> list[str]:
    """Return the last `count` entries. `count` is zero or more."""
    if count == 0:
        return []
    return entries[-count:]
