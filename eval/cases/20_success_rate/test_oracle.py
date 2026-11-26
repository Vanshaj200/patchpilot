from metrics import success_rate


def test_no_results():
    assert success_rate([]) == 0.0
