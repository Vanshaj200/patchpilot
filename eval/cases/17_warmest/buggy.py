def highest_temperature(readings: list[float]) -> float:
    """Return the highest value in a non-empty list of temperatures."""
    highest = 0.0
    for reading in readings:
        if reading > highest:
            highest = reading
    return highest
