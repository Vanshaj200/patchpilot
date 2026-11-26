from pathlib import Path

from patchpilot.cache import ResponseCache, text_block


def test_text_block_without_caching() -> None:
    assert text_block("hello", cache=False) == {"type": "text", "text": "hello"}


def test_text_block_with_caching() -> None:
    assert text_block("hello", cache=True)["cache_control"] == {"type": "ephemeral"}


def test_key_ignores_order_but_not_values() -> None:
    key = ResponseCache.key({"a": 1, "b": 2})

    assert key == ResponseCache.key({"b": 2, "a": 1})
    assert key != ResponseCache.key({"a": 1, "b": 3})
    assert len(key) == 64


def test_stores_and_returns_replies(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path / "cache")

    assert cache.get("k") is None
    cache.put("k", '{"x": "café"}')

    assert ResponseCache(tmp_path / "cache").get("k") == '{"x": "café"}'
