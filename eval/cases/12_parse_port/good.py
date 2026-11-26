def parse_port(text: str) -> int:
    """Return the port number in `text`. Raise ValueError if it is not a valid port."""
    port = int(text)
    if not 1 <= port <= 65535:
        raise ValueError("port out of range")
    return port
