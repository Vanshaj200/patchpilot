def page_items(items: list[str], page: int, size: int) -> list[str]:
    """Return the items on a page. Pages are numbered from 1."""
    start = (page - 1) * size + 1
    return items[start : start + size]
