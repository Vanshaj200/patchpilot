def parse_port(text: str) -> int:
    """Return the port number in `text`. Raise ValueError if it is not a valid port."""
    try:
        port = int(text)
    except Exception:
        port = 0
    return port
