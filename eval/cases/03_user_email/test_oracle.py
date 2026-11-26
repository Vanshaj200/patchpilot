from accounts import email_domain


def test_unknown_user():
    assert email_domain({}, 7) is None
