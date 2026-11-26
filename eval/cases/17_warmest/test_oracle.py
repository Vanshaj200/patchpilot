from weather import highest_temperature


def test_all_below_zero():
    assert highest_temperature([-5.0, -2.0]) == -2.0
