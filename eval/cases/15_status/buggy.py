def is_shipped(status: str) -> bool:
    """Return True if the status text is "shipped", ignoring case."""
    return status.lower() is "shipped"
