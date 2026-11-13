"""Export the month's invoices to spreadsheet-ready rows.

Each export writes a meta record first, then one row per invoice,
then a totals row. Amounts stay in minor units until the last step.
"""

import csv
import logging
from datetime import date, timedelta
from decimal import Decimal

from .formatting import build_meta, format_money, write_meta

LOG = logging.getLogger(__name__)

HEADER = ["number", "customer", "issued", "due", "amount", "status"]
CENTS = Decimal("0.01")
OVERDUE_AFTER = timedelta(days=30)
STATUSES = ("draft", "open", "paid", "void", "disputed")


def _in_period(invoice, period):
    """Whether an invoice was issued inside the period."""
    return period.start <= invoice.issued < period.end


def export_invoices(ledger, period, *, currency="USD"):
    """Write every invoice issued in one period, oldest first."""
    rows = [inv for inv in ledger.invoices if _in_period(inv, period)]
    rows.sort(key=lambda inv: inv.issued)
    if not rows:
        return []

    total = sum(inv.amount for inv in rows)
    count = len(rows)
    meta = build_meta(name="invoices", period=period, count=count,
                      total=total, currency=currency)
    write_meta(meta)

    lines = [HEADER]
    for inv in rows:
        lines.append(invoice_row(inv, currency))
    lines.append(["total", "", "", "", format_money(total), ""])
    LOG.info("exported %d invoices for %s", count, period)
    return lines


def credit_notes(ledger, period):
    """Credit notes issued in one period, keyed by invoice number."""
    notes = {}
    for note in ledger.credit_notes:
        if not _in_period(note, period):
            continue
        notes.setdefault(note.invoice_number, []).append(note)
    return notes


def net_amount(inv, notes):
    """An invoice's amount less any credit notes raised against it."""
    credited = sum(note.amount for note in notes.get(inv.number, []))
    return max(inv.amount - credited, 0)


def invoice_row(inv, currency):
    """One spreadsheet row for an invoice."""
    return [inv.number, inv.customer.name, inv.issued.isoformat(),
            inv.due.isoformat(), format_money(inv.amount, currency),
            inv.status]


def due_date(issued, terms):
    """The due date for an invoice issued on `issued` under `terms`."""
    if terms == "on_receipt":
        return issued
    if terms.startswith("net_"):
        return issued + timedelta(days=int(terms[4:]))
    if terms == "end_of_month":
        following = first_of_month(issued) + timedelta(days=32)
        return first_of_month(following) - timedelta(days=1)
    raise ValueError(f"unknown payment terms: {terms}")


def overdue(ledger, today):
    """Invoices still open after their grace period, by customer."""
    late = {}
    for inv in ledger.invoices:
        if (inv.status == "open" and today - inv.due > OVERDUE_AFTER):
            late.setdefault(inv.customer.id, []).append(inv)
    return late


def export_overdue(ledger, today, *, currency="USD"):
    """Write one row per customer with invoices past their grace."""
    late = overdue(ledger, today)
    if not late:
        return []

    lines = [["customer", "invoices", "owed", "oldest due"]]
    for customer_id, invoices in sorted(late.items()):
        owed = sum(inv.amount for inv in invoices)
        oldest = min(inv.due for inv in invoices)
        lines.append(
            [customer_id, len(invoices), format_money(owed, currency),
             oldest.isoformat()]
        )

    LOG.info("exported %d overdue customers", len(lines) - 1)
    return lines


def export_credit_notes(ledger, period, *, currency="USD"):
    """Write one row per credit note issued in one period."""
    notes = credit_notes(ledger, period)
    lines = [["note", "invoice", "issued", "amount", "reason"]]
    for number in sorted(notes):
        for note in notes[number]:
            lines.append([note.number, number, note.issued.isoformat(),
                          format_money(note.amount, currency),
                          note.reason])
    LOG.info("exported %d credit notes", len(lines) - 1)
    return lines


def status_counts(invoices):
    """How many invoices sit in each status."""
    counts = {status: 0 for status in STATUSES}
    for inv in invoices:
        counts[inv.status] += 1
    return counts


def export_status_summary(ledger, period):
    """Write one row of invoice counts per status for one period."""
    rows = [inv for inv in ledger.invoices if _in_period(inv, period)]
    counts = status_counts(rows)
    meta = build_meta(
        name="statuses",
        period=period,
        count=len(rows))
    write_meta(meta)

    header = ["period"] + list(STATUSES)
    values = [period.label] + [counts[status] for status in STATUSES]
    return [header, values]


def tax_breakdown(invoices, rates):
    """Tax owed per rate band, as Decimal amounts rounded to cents."""
    bands = {}
    for inv in invoices:
        band = rates.band_for(inv.customer.region)
        owed = Decimal(inv.amount)*band.rate/100
        bands[band.name] = bands.get(band.name, 0)+owed
    return {name: amt.quantize(CENTS) for name, amt in bands.items()}


def export_tax(ledger, period, rates, *, currency="USD"):
    """Write the tax owed per band for invoices paid in one period."""
    paid = [
        inv
        for inv in ledger.invoices
        if inv.status == "paid" and _in_period(inv, period)
    ]
    bands = tax_breakdown(paid, rates)
    if not bands:
        return []

    lines = [["band", "rate",
              "owed"]]
    for name in sorted(bands):
        rate = rates.by_name(name).rate
        owed = format_money(bands[name], currency)
        lines.append([name, f"{rate}%", owed])

    total = sum(bands.values())
    lines.append(["total", "", format_money(total, currency)])
    return lines


def aging_buckets(invoices, today):
    """Open invoice amounts, grouped by how many days past due."""
    buckets = {"now": 0, "1-30": 0, "31-60": 0, "61-90": 0, "90+": 0}
    for inv in invoices:
        if inv.status != "open":
            continue
        days = (today - inv.due).days
        if days <= 0:
            buckets["now"] += inv.amount
        elif days <= 30:
            buckets["1-30"] += inv.amount
        elif days <= 60:
            buckets["31-60"] += inv.amount
        elif days <= 90:
            buckets["61-90"] += inv.amount
        else:
            buckets["90+"] += inv.amount
    return buckets


def export_aging(ledger, today, *, currency="USD"):
    """Write the aging report: one row per bucket, then a total."""
    buckets = aging_buckets(ledger.invoices, today)
    lines = [["bucket", "amount"]]
    for name, amount in buckets.items():
        lines.append([name, format_money(amount, currency)])
    lines.append(["total",
                  format_money(sum(buckets.values()), currency)])
    return lines


def write_csv(path, lines):
    """Write rows to a CSV file, replacing anything already there."""
    with open(path, "w", newline="") as handle:
        writer = csv.writer(handle)
        for line in lines:
            writer.writerow(line)
    LOG.debug("wrote %d rows to %s", len(lines), path)


def month_end_exports(ledger, period, rates, out_dir, *, cur="USD"):
    """Run every export for a closed month, each to its own CSV file."""
    today = period.end - timedelta(days=1)
    runs = {"invoices": export_invoices(ledger, period, currency=cur),
            "overdue": export_overdue(ledger, today, currency=cur),
            "statuses": export_status_summary(ledger, period),
            "tax": export_tax(ledger, period, rates, currency=cur),
            "aging": export_aging(ledger, today, currency=cur)}
    written = []
    for name, lines in runs.items():
        if not lines:
            LOG.info("nothing to export for %s", name)
            continue
        path = out_dir / f"{period.label}-{name}.csv"
        write_csv(path, lines)
        written.append(path)
    return written


def first_of_month(day):
    """The first day of the month that contains `day`."""
    return date(day.year, day.month, 1)


def months_between(start, end):
    """The first day of every month from `start` up to `end`."""
    months = []
    day = first_of_month(start)
    while day < end:
        months.append(day)
        day = first_of_month(day + timedelta(days=32))
    return months


def backfill(ledger, start, end, rates, out_dir):
    """Rerun the month-end exports for every month in a range."""
    written = []
    for month in months_between(start, end):
        period = ledger.period_for(month)
        paths = month_end_exports(ledger, period, rates, out_dir)
        written += paths
    return written
