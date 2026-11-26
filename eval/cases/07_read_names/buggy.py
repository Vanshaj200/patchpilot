def read_names(path: str) -> list[str]:
    """Return the non-empty lines of a text file."""
    handle = open(path, encoding="utf-8")
    return [line.strip() for line in handle if line.strip()]
