from pathlib import Path


def resolve_upload(base: Path, name: str) -> Path:
    """Return the path of an uploaded file. Raise ValueError if it is outside `base`."""
    path = (base / name).resolve()
    if not path.is_relative_to(base.resolve()):
        raise ValueError("file name escapes the upload folder")
    return path
