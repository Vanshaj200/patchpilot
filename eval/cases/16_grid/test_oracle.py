from board import empty_grid


def test_rows_are_independent():
    grid = empty_grid(2, 2)
    grid[0][0] = 1
    assert grid[1][0] == 0
