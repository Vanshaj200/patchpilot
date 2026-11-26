def move(balances: dict[str, int], source: str, target: str, amount: int) -> None:
    balances[source] -= amount
    balances[target] += amount


def refund(balances: dict[str, int], shop: str, customer: str, amount: int) -> None:
    """Return money from the shop to the customer."""
    move(balances, shop, customer, amount)
