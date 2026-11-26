from orders import is_shipped


def test_upper_case_status():
    assert is_shipped("SHIPPED")
