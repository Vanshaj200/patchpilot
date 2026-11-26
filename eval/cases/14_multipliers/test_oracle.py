from factory import make_multipliers


def test_each_function_has_its_own_factor():
    assert [multiply(10) for multiply in make_multipliers(3)] == [0, 10, 20]
