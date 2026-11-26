from permissions import can_delete


def test_active_stranger_cannot_delete():
    assert not can_delete(is_owner=False, is_active=True, is_admin=False)
