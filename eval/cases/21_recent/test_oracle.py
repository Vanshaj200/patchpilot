from history import latest


def test_zero_entries():
    assert latest(["a", "b"], 0) == []
