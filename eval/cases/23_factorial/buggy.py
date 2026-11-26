def factorial(number: int) -> int:
    """Return number! for a whole number of zero or more."""
    if number == 1:
        return 1
    return number * factorial(number - 1)
