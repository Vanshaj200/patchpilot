def latest(entries: list[str], count: int) -> list[str]:
    """Return the last `count` entries. `count` is zero or more."""
    return entries[-count:]
