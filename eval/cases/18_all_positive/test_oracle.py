from validation import all_positive


def test_later_negative_value():
    assert not all_positive([1, -1])
