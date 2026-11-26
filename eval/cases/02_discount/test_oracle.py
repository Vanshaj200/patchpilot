from pricing import discounted_price


def test_discount_lowers_price():
    assert discounted_price(200, 10) == 180
