def discounted_price(price: float, percent: float) -> float:
    """Return the price after taking `percent` percent off."""
    discount = price * percent / 100
    return round(price - discount, 2)
