"""Two kinds of caching.

Prompt caching happens on Anthropic's side: a marked prefix that is sent again within a few
minutes is billed at a much lower rate. The response cache is local: an identical request is
answered from disk and nothing is sent at all.
"""

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path


def text_block(text: str, cache: bool) -> dict[str, object]:
    """A prompt block, marked as the end of a cacheable prefix if `cache` is true."""
    block: dict[str, object] = {"type": "text", "text": text}
    if cache:
        block["cache_control"] = {"type": "ephemeral"}
    return block


class ResponseCache:
    """Stores replies as files named by a hash of everything that shapes the reply."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    @staticmethod
    def key(parts: Mapping[str, object]) -> str:
        # sort_keys gives the same text, and so the same hash, whatever the insertion order.
        canonical = json.dumps(parts, sort_keys=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def get(self, key: str) -> str | None:
        path = self._path(key)
        return path.read_text(encoding="utf-8") if path.is_file() else None

    def put(self, key: str, reply: str) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        self._path(key).write_text(reply, encoding="utf-8")

    def _path(self, key: str) -> Path:
        return self._directory / f"{key}.json"
