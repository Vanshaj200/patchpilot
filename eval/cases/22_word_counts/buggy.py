def count_words(text: str) -> dict[str, int]:
    """Count how often each word appears, ignoring case."""
    counts: dict[str, int] = {}
    for word in text.lower().split():
        counts[word] += 1
    return counts
