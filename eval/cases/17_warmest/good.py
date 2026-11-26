def highest_temperature(readings: list[float]) -> float:
    """Return the highest value in a non-empty list of temperatures."""
    highest = readings[0]
    for reading in readings[1:]:
        if reading > highest:
            highest = reading
    return highest
