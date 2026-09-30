"""The shape of a payment, and where the money goes.

There is no gateway. An owner asks to pay an invoice, is told the seller's
bank or UPI details, sends the money themselves, and types the reference; an
operator matches it to the bank statement. Two things about that live here so
every route that shows a payment - the tenant's list, the invoice list, the
operator's inbox, the review - shows the same one:

  * ``pay_to()``: the seller's details from the runtime's own environment, or
    None when not enough of them are set to receive a rupee. None is a
    refusal upstream: a request with nowhere to send the money is a deadline
    the customer cannot meet, so the route answers 503 before writing a row.

  * ``shape()``: the ``core.payments`` row as the API says it. Money is a
    string of the numeric, never a float; timestamps are ISO 8601; ids are
    strings; nulls stay null. A field the row does not have is not invented.

Nothing here opens a connection. Every caller brings its own cursor, on the
connection its own rules require.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.config import get_settings

# One fixed sentence, part of the contract with the web app. It says the two
# things a customer must do and the one thing this button does not do.
PAY_TO_NOTE = (
    "Pay the invoice total exactly and quote the invoice number; then enter the "
    "UTR / UPI reference here. Nothing is confirmed until an operator matches it."
)

PAYMENT_STATUSES = ("awaiting_payment", "submitted", "approved", "rejected", "expired")


def pay_to() -> dict[str, Any] | None:
    """The seller's details, or None when there is nowhere to send money.

    A UPI id alone is enough - that is how most Indian customers will pay.
    Without one, a bank transfer needs all three of account name, account
    number and IFSC; two of the three is a transfer that bounces, so a partial
    triple counts as nothing rather than as "bank transfer, roughly".
    """
    s = get_settings()
    upi = s.seller_upi_id.strip() or None
    name = s.seller_bank_account_name.strip() or None
    number = s.seller_bank_account_number.strip() or None
    ifsc = s.seller_bank_ifsc.strip().upper() or None
    bank = s.seller_bank_name.strip() or None

    triple = name and number and ifsc
    if not upi and not triple:
        return None
    return {
        "account_name": name if triple else None,
        "account_number": number if triple else None,
        "ifsc": ifsc if triple else None,
        "bank_name": bank if triple else None,
        "upi_id": upi,
        "note": PAY_TO_NOTE,
    }


# The columns every payment read selects, so the SELECT and the shape cannot
# drift. ``p`` is core.payments and ``i`` is the invoice it names; the join is
# LEFT so a payment is still a payment if the invoice is somehow unreadable.
PAYMENT_COLUMNS = """
       p.id::text              as id,
       p.invoice_id::text      as invoice_id,
       i.number                as invoice_number,
       p.amount_inr,
       p.status::text          as status,
       p.method,
       p.reference,
       p.paid_on,
       p.window_ends_at,
       p.submitted_at,
       p.reviewed_at,
       p.review_note,
       p.created_at
"""

PAYMENT_FROM = """
  from core.payments p
  left join core.invoices i on i.id = p.invoice_id
"""


def _iso(value: datetime | date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _money(value: Decimal | None) -> str | None:
    return None if value is None else str(Decimal(value).quantize(Decimal("0.01")))


def shape(row: dict[str, Any]) -> dict[str, Any]:
    """A ``core.payments`` row (with ``invoice_number`` joined) as the API
    contract's Payment. Extra keys on the row - the operator's org columns -
    are passed through, so AdminPayment is shape() plus a wider SELECT."""
    out = {
        "id": row["id"],
        "invoice_id": row["invoice_id"],
        "invoice_number": row.get("invoice_number"),
        "amount_inr": _money(row["amount_inr"]),
        "status": row["status"],
        "method": row.get("method"),
        "reference": row.get("reference"),
        "paid_on": _iso(row.get("paid_on")),
        "window_ends_at": _iso(row.get("window_ends_at")),
        "submitted_at": _iso(row.get("submitted_at")),
        "reviewed_at": _iso(row.get("reviewed_at")),
        "review_note": row.get("review_note"),
        "created_at": _iso(row["created_at"]),
    }
    for key, value in row.items():
        if key not in out:
            out[key] = value
    return out


def fetch(cur: Any, payment_id: str) -> dict[str, Any] | None:
    """One payment, on whatever connection the cursor is - under RLS on the
    tenant connection, which is the point: a row the caller may not see is
    None here and a 404 upstream."""
    cur.execute(f"select {PAYMENT_COLUMNS} {PAYMENT_FROM} where p.id = %s::uuid", (payment_id,))
    row = cur.fetchone()
    return shape(row) if row else None
