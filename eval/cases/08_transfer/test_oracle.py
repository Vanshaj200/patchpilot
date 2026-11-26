from ledger import refund


def test_refund_goes_to_customer():
    balances = {"shop": 100, "ann": 0}
    refund(balances, "shop", "ann", 30)
    assert balances == {"shop": 70, "ann": 30}
