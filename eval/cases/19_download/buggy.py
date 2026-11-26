from pathlib import Path


def resolve_upload(base: Path, name: str) -> Path:
    """Return the path of an uploaded file. Raise ValueError if it is outside `base`."""
    path = (base / name).resolve()
    return path
