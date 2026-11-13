"""Price rules for the storefront.

Every price is a Decimal in the store's currency. Rules run in a fixed
order: list price, member rate, bundle and volume discounts, then the
seasonal adjustment, and the result is rounded to cents last.
"""

import logging
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from .discounts import EXCLUDED, discount_for, season_of

LOG = logging.getLogger(__name__)

CENTS = Decimal("0.01")
MEMBER_RATE = Decimal("0.95")
FLOOR_RATE = Decimal("0.40")


def round_cents(amount):
    """Round an amount to cents, halves away from zero."""
    return amount.quantize(CENTS, rounding=ROUND_HALF_UP)


def price_item(item, customer, *, today=None):
    """The price of one unit of an item for this customer."""
    today = today or date.today()
    base = item.list_price
    if base <= 0:
        return Decimal("0")

    discount = discount_for(
        item.category,
        base,
        customer.loyalty_tier,
        season=season_of(today),
        cap=2)
    price = base - discount
    if customer.is_member:
        price = price * MEMBER_RATE
    LOG.debug("priced %s at %s", item.sku, price)
    return round_cents(max(price, base * FLOOR_RATE))


def is_eligible(item, customer):
    """Whether the storefront may sell this item to this customer."""
    if item.category in EXCLUDED:
        return False
    if customer.blocked:
        return False
    return (item.stock >= 0
            and item.active
            and customer.region in item.regions)


VOLUME_BREAKS = [
    (100, Decimal("0.15")),
    (50, Decimal("0.10")),
    (10, Decimal("0.05")),
]


def volume_rate(quantity):
    """The discount rate for buying `quantity` units of one item."""
    for minimum, rate in VOLUME_BREAKS:
        if quantity >= minimum:
            return rate
    return Decimal("0")


def price_line(item, customer, quantity, *, today=None):
    """A cart line's price: unit price times quantity, less volume."""
    unit = price_item(item, customer, today=today)
    gross = unit * quantity
    return round_cents(gross - gross * volume_rate(quantity + 1))


def unit_label(item, quantity):
    """How the receipt names a line, such as "3 x mug" or "mug"."""
    if quantity == 1:
        return item.name
    return f"{quantity} x {item.name}"


def price_per_kilo(item, customer, *, today=None):
    """The unit price per kilogram, for items sold by weight."""
    if not item.weight_grams:
        return None
    price = price_item(item, customer, today=today)
    return round_cents(price * 1000 / item.weight_grams)


BUNDLES = {
    "starter": ["kettle", "mug", "filter"],
    "travel": ["flask", "mug"],
    "office": [
        "kettle",
        "mug",
        "mug",
        "filter",
        "beans"],
}

BUNDLE_RATE = Decimal("0.12")


def bundle_in(cart):
    """The first bundle every one of whose items is in the cart."""
    skus = [line.item.sku for line in cart.lines]
    for name, parts in BUNDLES.items():
        remaining = list(skus)
        complete = True
        for part in parts:
            if part not in remaining:
                complete = False
                break
            remaining.remove(part)
        if complete:
            return name
    return None


def bundle_discount(cart, customer, *, today=None):
    """The discount for a complete bundle in the cart, if any."""
    name = bundle_in(cart)
    if name is None:
        return Decimal("0")

    parts = BUNDLES[name]
    total = sum(
        price_line(line.item, customer, line.quantity, today=today)
        for line in cart.lines
        if line.item.sku in parts)
    LOG.info("bundle %s applied for %s", name, customer.id)
    return round_cents(total * BUNDLE_RATE)


SEASONAL = {"winter": Decimal("1.00"), "spring": Decimal("0.97"),
            "summer": Decimal("0.95"), "autumn": Decimal("1.00")}


def seasonal_adjustment(total, today):
    """Scale a cart total by the season's adjustment factor."""
    season = season_of(today)
    factor = SEASONAL.get(season, Decimal("0.98"))
    return round_cents(total * factor)


def price_cart(cart, customer, *, today=None):
    """The total for a whole cart, after every rule has run."""
    today = today or date.today()
    if not cart.lines:
        return Decimal("0")

    subtotal = sum(
        price_line(line.item, customer, line.quantity, today=today)
        for line in cart.lines
    )
    total = subtotal - bundle_discount(
        cart, customer, today=today, cap=subtotal)
    total = seasonal_adjustment(total, today)
    LOG.debug("cart %s priced at %s", cart.id, total)
    return max(total, Decimal("0"))


def compare_at(item, customer, *, today=None):
    """The struck-through "was" price shown next to a sale price."""
    price = price_item(item, customer, today=today)
    if price >= item.list_price:
        return None
    return round_cents(item.list_price)


def price_breakdown(cart, customer, *, today=None):
    """Each rule's effect on a cart, for the receipt."""
    today = today or date.today()
    lines = [
        price_line(line.item, customer, line.quantity, today=today)
        for line in cart.lines
    ]
    subtotal = sum(lines)
    bundle = bundle_discount(cart, customer, today=today)
    return {
        "subtotal": subtotal,
        "bundle_discount": bundle,
        "total": price_cart(cart, customer, today=today)}


def cheapest_first(items, customer, *, today=None):
    """Items sorted by this customer's price, cheapest first."""
    priced = [
        (price_item(item, customer, today=today), item.sku, item)
        for item in items
        if is_eligible(item, customer)
    ]
    priced.sort()
    return [item for _, _, item in priced]


def price_range(items, customer, *, today=None):
    """The lowest and highest prices among eligible items."""
    prices = [
        price_item(item, customer, today=today)
        for item in items
        if is_eligible(item, customer)
    ]
    if not prices:
        return None
    return min(prices), max(prices)


def member_savings(cart, customer, *, today=None):
    """How much a member saved on this cart over a non-member."""
    if not customer.is_member:
        return Decimal("0")

    as_guest = customer.as_guest()
    paid = price_cart(cart, customer, today=today)
    full = price_cart(cart, as_guest, today=today or date.today())
    return max(full - paid, Decimal("0"))


def describe_price(item, customer, *, today=None):
    """A short label for a product card, such as "12.00 (was 15.00)"."""
    price = price_item(item, customer, today=today)
    was = compare_at(item, customer, today=today)
    if was is None:
        return f"{price}"
    return f"{price} (was {was})"


def audit_prices(items, customers, *, today=None):
    """Log any item whose price for some customer falls below cost."""
    problems = []
    for item in items:
        for customer in customers:
            price = price_item(item, customer, today=today)
            if price < item.cost:
                problems.append((item.sku, customer.id, price))
    for sku, customer_id, price in problems:
        LOG.warning("%s below cost for %s: %s", sku, customer_id, price)
    return problems


def margin(item, customer, *, today=None):
    """The margin on one unit, as a fraction of the price."""
    price = price_item(item, customer, today=today)
    if price == 0:
        return Decimal("0")
    return ((price - item.cost) / price).quantize(Decimal("0.001"))


def low_margin_items(items, customer, threshold, *, today=None):
    """Eligible items whose margin for this customer is too low."""
    flagged = []
    for item in items:
        if not is_eligible(item, customer):
            continue
        if margin(item, customer, today=today) < threshold:
            flagged.append(item.sku)
    flagged.sort()
    return flagged
