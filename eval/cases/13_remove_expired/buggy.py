def drop_expired(ages: list[int], limit: int) -> list[int]:
    """Remove every age over the limit from the list in place and return the list."""
    for age in ages:
        if age > limit:
            ages.remove(age)
    return ages
