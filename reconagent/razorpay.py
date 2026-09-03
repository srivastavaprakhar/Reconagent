"""Razorpay settlement export parser.

Rows are transaction-level; several rows can share one settlement_id (a
payment row plus a later refund row against the same settlement). This
aggregates rows into one CanonicalRecord per settlement_id, attaching the raw
rows via `.rows` so downstream refund-FX-asymmetry logic (spec §5) can get at
each row's own conversion rate.

`amount_minor` is the settlement's *capture* net -- credits minus debits over
the non-refund rows. A refund is deliberately NOT netted into it: per spec §5 a
refund converts at its own FX event and settles as its own bank movement, so
netting it against the capture produces an amount that matches no bank credit
and silently breaks the subset-sum solver on exactly the refund cases. The
refund rows stay on `.rows`, which is where the FX layer wants them anyway.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from reconagent.money import parse_minor, parse_rate
from reconagent.records import CanonicalRecord, SettlementRow


class RazorpayParseError(ValueError):
    """A settlement row is missing data a CanonicalRecord cannot do without,
    or a settlement has no payment row to anchor its top-level fields on."""


def _yn(s: str) -> bool:
    return s.strip().upper() == "Y"


def _date_or_none(s: str) -> date | None:
    s = s.strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError as exc:
        raise RazorpayParseError(f"malformed date: {s!r}") from exc


def _parse_row(raw: dict[str, str]) -> SettlementRow:
    entity_id = raw.get("entity_id", "")
    try:
        amount_minor = parse_minor(raw["amount"])
        credit_minor = parse_minor(raw["credit"])
        debit_minor = parse_minor(raw["debit"])
        fee_minor = parse_minor(raw["fee"])
        tax_minor = parse_minor(raw["tax"])
        base_amount_minor = (
            parse_minor(raw["base_amount"]) if raw["base_amount"].strip() else None
        )
        conversion_rate = (
            parse_rate(raw["conversion_rate"]) if raw["conversion_rate"].strip() else None
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise RazorpayParseError(
            f"malformed settlement row {entity_id!r}: {exc}"
        ) from exc

    return SettlementRow(
        entity_id=entity_id,
        type=raw["type"],
        debit_minor=debit_minor,
        credit_minor=credit_minor,
        amount_minor=amount_minor,
        currency=raw["currency"],
        fee_minor=fee_minor,
        tax_minor=tax_minor,
        on_hold=_yn(raw["on_hold"]),
        settled=_yn(raw["settled"]),
        created_at=_date_or_none(raw["created_at"]),
        settled_at=_date_or_none(raw["settled_at"]),
        settlement_id=raw["settlement_id"],
        settlement_utr=raw["settlement_utr"],
        description=raw["description"],
        notes=raw["notes"],
        payment_id=raw["payment_id"],
        order_id=raw["order_id"],
        order_receipt=raw["order_receipt"],
        method=raw["method"],
        international=_yn(raw["international"]),
        conversion_rate=conversion_rate,
        base_amount_minor=base_amount_minor,
        base_currency=raw["base_currency"] or None,
        refund_id=raw["refund_id"] or None,
    )


def parse_razorpay_settlements(path: str | Path) -> list[CanonicalRecord]:
    with open(path, newline="", encoding="utf-8") as f:
        rows = [_parse_row(r) for r in csv.DictReader(f)]

    by_settlement: dict[str, list[SettlementRow]] = {}
    for row in rows:
        by_settlement.setdefault(row.settlement_id, []).append(row)

    records: list[CanonicalRecord] = []
    for settlement_id, group in by_settlement.items():
        payment_rows = [r for r in group if r.type == "payment"]
        net_minor = sum(r.credit_minor - r.debit_minor for r in payment_rows)
        if not payment_rows:
            raise RazorpayParseError(
                f"settlement {settlement_id!r} has no payment row "
                f"(rows: {[r.entity_id for r in group]})"
            )
        primary = payment_rows[0]
        records.append(
            CanonicalRecord(
                source="razorpay_settlement",
                record_id=settlement_id,
                # No counterparty name in this feed's own columns -- the
                # counterparty is Razorpay itself, a constant not worth
                # carrying as text. Left blank rather than hardcoded.
                counterparty_name="",
                narration=primary.description,
                amount_minor=net_minor,
                currency=primary.base_currency or "INR",
                created_at=primary.created_at,
                settled_at=primary.settled_at,
                utr=primary.settlement_utr,
                invoice_id=primary.order_receipt or None,
                order_id=primary.order_id or None,
                payment_id=primary.payment_id or None,
                conversion_rate=primary.conversion_rate,
                foreign_amount_minor=primary.amount_minor if primary.international else None,
                foreign_currency=primary.currency if primary.international else None,
                base_amount_minor=primary.base_amount_minor,
                rows=tuple(group),
            )
        )
    return records
