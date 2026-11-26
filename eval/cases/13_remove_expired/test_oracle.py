from sessions import drop_expired


def test_neighbouring_expired_items():
    assert drop_expired([50, 60, 1], 10) == [1]
