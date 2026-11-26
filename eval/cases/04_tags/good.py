def add_tag(tag: str, tags: list[str] | None = None) -> list[str]:
    """Add a tag to `tags`, or to a new list if none is given, and return it."""
    if tags is None:
        tags = []
    tags.append(tag)
    return tags
