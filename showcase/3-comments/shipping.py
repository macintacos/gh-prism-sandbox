"""Shipping quotes for the storefront.

Quotes are Decimal amounts in the store's currency, rounded to cents.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from .zones import RATES, SURCHARGES, zone_of

CENTS = Decimal("0.01")
VOLUMETRIC_DIVISOR = Decimal("5000")
MAX_GIRTH = Decimal("300")
MAX_WEIGHT = Decimal("30")
FREE_OVER = Decimal("75.00")


@dataclass(frozen=True)
class Quote:
    """One carrier's price for one parcel."""

    carrier: str
    service: str
    amount: Decimal
    days: int


# Billable weight is the larger of a parcel's actual weight and its
# volumetric weight, so a light but bulky parcel pays for the space
# it takes up in the van rather than for what it weighs.
def billable_weight(parcel):
    """The weight a carrier charges for, in kilograms."""
    volume = parcel.length * parcel.width * parcel.height
    volumetric = volume / VOLUMETRIC_DIVISOR
    return max(parcel.weight, volumetric)


def girth(parcel):
    """Length plus twice the width plus twice the height."""
    return parcel.length + 2 * parcel.width + 2 * parcel.height


def can_ship(parcel):
    """Whether any carrier will take the parcel at all."""
    if parcel.weight <= 0:
        return False
    # Carriers measure girth around the two shorter sides, and
    # reject anything over the limit whatever it weighs.
    if girth(parcel) > MAX_GIRTH:
        return False
    return parcel.weight <= MAX_WEIGHT


def round_cents(amount):
    """Round to cents, halves away from zero."""
    return amount.quantize(CENTS, rounding=ROUND_HALF_UP)


def base_rate(carrier, service, destination, weight):
    """The carrier's table rate for a weight to a destination."""
    zone = zone_of(destination)
    table = RATES[carrier][service][zone]
    # Tables list a price per weight band, lightest first. The first
    # band the weight fits under sets the price; a weight over every
    # band uses the heaviest band's price plus the per-kilogram excess.
    for limit, price in table.bands:
        if weight <= limit:
            return price
    heaviest, price = table.bands[-1]
    return price + (weight - heaviest) * table.excess_per_kg


def surcharges(carrier, parcel, destination):
    """Extra fees a carrier adds on top of its table rate."""
    total = Decimal("0")
    fees = SURCHARGES.get(carrier, {})
    if destination.is_remote:
        total += fees.get("remote", Decimal("0"))
    if girth(parcel) > MAX_GIRTH * Decimal("0.8"):
        total += fees.get("oversize", Decimal("0"))  # near max girth
    if destination.is_residential:
        total += fees.get("residential", Decimal("0"))  # house delivery
    return total


def quote(carrier, service, parcel, destination):
    """One carrier's quote for a parcel, or None if it won't take it."""
    if not can_ship(parcel):
        return None
    weight = billable_weight(parcel)
    amount = base_rate(carrier, service, destination, weight)
    amount += surcharges(carrier, parcel, destination)
    days = RATES[carrier][service].days
    return Quote(carrier, service, round_cents(amount), days)


# Quote every carrier and service, then keep the ones that can deliver
# in time.
#
# A carrier that refuses the parcel is skipped rather than reported,
# so the result may be empty even though every carrier was asked, and
# the caller must handle an empty list.
def all_quotes(parcel, destination, *, within_days=None):
    """Every quote for a parcel, cheapest first."""
    quotes = []
    for carrier, services in RATES.items():
        for service in services:
            found = quote(carrier, service, parcel, destination)
            if found is None:
                continue
            if within_days is not None and found.days > within_days:
                continue
            quotes.append(found)
    quotes.sort(key=lambda q: (q.amount, q.days))
    return quotes


def cheapest(parcel, destination, *, within_days=None):
    """The cheapest quote for a parcel, or None."""
    quotes = all_quotes(parcel, destination, within_days=within_days)
    return quotes[0] if quotes else None


def fastest(parcel, destination):
    """The quickest quote for a parcel, cheapest among ties."""
    quotes = all_quotes(parcel, destination)
    if not quotes:
        return None
    return min(quotes, key=lambda q: (q.days, q.amount))


def cart_parcel(cart):
    """Pack a cart into a single parcel, for quoting at checkout."""
    lines = cart.lines
    weight = sum(ln.item.weight * ln.quantity for ln in lines)
    # Packing is approximate: every item goes in one box sized to the
    # largest item, and the box grows in height with each item stacked
    # on top. Real packing happens in the warehouse later.
    length = max(ln.item.length for ln in lines)
    width = max(ln.item.width for ln in lines)
    height = sum(ln.item.height * ln.quantity for ln in lines)
    return Parcel(
        weight=weight, length=length, width=width, height=height
    )


@dataclass(frozen=True)
class Parcel:
    """A box to ship: weight in kilograms, sides in centimetres."""

    weight: Decimal
    length: Decimal
    width: Decimal
    height: Decimal


def checkout_quote(cart, destination):
    """The shipping line shown at checkout."""
    if cart.subtotal >= FREE_OVER:
        return Decimal("0")
    best = cheapest(cart_parcel(cart), destination)
    if best is None:
        raise ValueError("no carrier can ship this cart")
    # Free shipping above the threshold is a store promise, not a
    # carrier price, so it is applied here and never sent to the carrier
    # quote.
    return best.amount


def split_quotes(parcels, destination):
    """Quotes for an order shipped as several parcels."""
    total = Decimal("0")
    carriers = set()
    for parcel in parcels:
        best = cheapest(parcel, destination)
        if best is None:
            raise ValueError("a parcel cannot be shipped")
        total += best.amount
        carriers.add(best.carrier)
    # Each parcel picks its own cheapest carrier. Mixing carriers on
    # one order is allowed, though the receipt lists each carrier once.
    return round_cents(total), sorted(carriers)


def delivery_window(best, dispatched_on):
    """The earliest and latest delivery dates for a quote."""
    from datetime import timedelta

    earliest = dispatched_on + timedelta(days=best.days)
    # Allow two extra days at the far end: carriers miss their stated
    # times often enough that promising the exact day sets customers up
    # to be disappointed.
    latest = earliest + timedelta(days=2)
    return earliest, latest


def describe(best):
    """A label for a quote, such as "Parcelco Standard, 3 days"."""
    if best is None:
        return "No delivery available"
    days = "1 day" if best.days == 1 else f"{best.days} days"
    return f"{best.carrier.title()} {best.service.title()}, {days}"


def quote_table(parcel, destination):
    """Rows for the admin page: one per carrier and service."""
    rows = [["carrier", "service", "price", "days"]]
    for found in all_quotes(parcel, destination):
        rows.append(
            [found.carrier, found.service, str(found.amount),
             found.days]
        )
    return rows


def max_items_per_box(item):
    """How many of one item fit in one box before it can't ship."""
    count = 0
    while True:
        height = item.height * (count + 1)
        box = Parcel(
            weight=item.weight * (count + 1),
            length=item.length,
            width=item.width,
            height=height,
        )
        if not can_ship(box):
            return count
        count += 1


INSURANCE_RATE = Decimal("0.015")
INSURANCE_MIN = Decimal("2.50")


# Insurance covers the declared value of the goods, not the shipping
# cost, and is priced as a share of that value with a floor.
#
# Carriers cap what they will insure. Above the cap the parcel ships
# with the excess uninsured, and the receipt says so plainly.
def insurance_for(declared_value, carrier_cap):
    """The insurance premium for a parcel's declared value."""
    covered = min(declared_value, carrier_cap)
    if covered <= 0:
        return Decimal("0")
    premium = covered * INSURANCE_RATE
    return round_cents(max(premium, INSURANCE_MIN))


def uninsured_excess(declared_value, carrier_cap):
    """How much of the declared value the carrier will not cover."""
    return max(declared_value - carrier_cap, Decimal("0"))
