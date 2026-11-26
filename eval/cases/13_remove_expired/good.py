def drop_expired(ages: list[int], limit: int) -> list[int]:
    """Remove every age over the limit from the list in place and return the list."""
    ages[:] = [age for age in ages if age <= limit]
    return ages
