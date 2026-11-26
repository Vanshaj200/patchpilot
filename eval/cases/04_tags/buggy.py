def add_tag(tag: str, tags: list[str] = []) -> list[str]:
    """Add a tag to `tags`, or to a new list if none is given, and return it."""
    tags.append(tag)
    return tags
