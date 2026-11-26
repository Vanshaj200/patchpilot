from grades import mean_score


def test_fraction_is_kept():
    assert mean_score([1, 2]) == 1.5
