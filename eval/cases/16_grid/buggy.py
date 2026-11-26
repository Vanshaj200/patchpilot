def empty_grid(rows: int, columns: int) -> list[list[int]]:
    """Return a rows by columns grid of zeros whose cells can be changed independently."""
    return [[0] * columns] * rows
