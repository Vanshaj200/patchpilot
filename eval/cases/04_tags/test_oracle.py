from tagging import add_tag


def test_calls_do_not_share_a_list():
    add_tag("a")
    assert add_tag("b") == ["b"]
