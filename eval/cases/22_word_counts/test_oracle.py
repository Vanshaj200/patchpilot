from frequency import count_words


def test_counts():
    assert count_words("a A") == {"a": 2}
