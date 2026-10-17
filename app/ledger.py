# Ledger helpers for the fixture app.
# Amounts are integers in cents.

RATE = 3


def fee(amount):
    """Return the fee on an amount."""
    return amount * RATE // 100


def label(name):
    return name.title()


def pad(text):
    return text.ljust(10)


def strip(text):
    return text.strip()


def upper(text):
    return text.upper()


def total(amounts):
    """Sum amounts.

    Negative amounts are refunds.
    """
    result = 0
    for amount in amounts:
        result += amount
    return result
