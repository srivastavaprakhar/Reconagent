"""Invoice/order ledger parser -- the third of the three sources (spec §3)
that collapses into a CanonicalRecord. A flat CSV, no wire format to target,
so this is deliberately the smallest of the four parsers here.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from reconagent.money import parse_minor
from reconagent.records import CanonicalRecord


class InvoiceParseError(ValueError):
    """An invoice row is missing data a CanonicalRecord cannot do without."""


def parse_invoice_ledger(path: str | Path) -> list[CanonicalRecord]:
    records: list[CanonicalRecord] = []
    with open(path, newline="", encoding="utf-8") as f:
        for raw in csv.DictReader(f):
            invoice_id = raw.get("invoice_id", "")
            try:
                amount_minor = parse_minor(raw["invoice_amount"])
            except (ValueError, TypeError, KeyError) as exc:
                raise InvoiceParseError(
                    f"malformed invoice_amount for {invoice_id!r}: {exc}"
                ) from exc
            issue_date = raw["invoice_date"].strip()
            records.append(
                CanonicalRecord(
                    source="invoice",
                    record_id=invoice_id,
                    counterparty_name=raw.get("customer_name", ""),
                    narration=raw.get("notes", "") or "",
                    amount_minor=amount_minor,
                    currency=raw["currency"],
                    booking_date=date.fromisoformat(issue_date) if issue_date else None,
                    invoice_id=invoice_id,
                    order_id=raw.get("order_id") or None,
                )
            )
    return records
