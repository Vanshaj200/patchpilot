from reporting import total_and_count


def test_generator_input():
    assert total_and_count(value for value in [1, 2]) == (3, 2)
