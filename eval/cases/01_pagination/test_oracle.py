from pagination import page_items


def test_first_page_starts_at_first_item():
    assert page_items(["a", "b", "c"], 1, 2) == ["a", "b"]
