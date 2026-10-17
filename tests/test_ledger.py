from app.ledger import fee, total


def test_fee():
    assert fee(1000) == 30


def test_total():
    assert total([1, 2]) == 3
