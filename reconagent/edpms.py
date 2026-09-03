"""EDPMS / shipping-bill linkage and aging (spec §5).

Separate from `fx.py` because it is a different concern on a different source:
`fx.py` reasons about conversion events on the settlement feed, this reasons
about export receipts on the invoice ledger against an RBI deadline. They share
nothing but the "as of" date.

The regulatory stake, which is the reason this exists at all: under FEMA an
export receipt must be realised within nine months of the shipping bill date.
An outstanding bill past that deadline gets the exporter caution-listed by RBI
-- the exporter then cannot ship against new orders without advance payment.
So "days to deadline" here is not a nicety, it is the number the finance team
actually needs, and it must be computed against a supplied statement date
rather than the wall clock so a closed period reports the same figures forever.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
import csv

from reconagent.money import parse_minor


REALISED = "REALISED"
AGING = "AGING"
OPEN_EDPMS_LINKAGE = "OPEN_EDPMS_LINKAGE"


@dataclass(frozen=True)
class ExportReceipt:
    """One shipping bill's realisation position as of a statement date.

    Amounts are in the invoice's own currency (minor units) -- EDPMS tracks
    realisation against the *foreign* invoice value, not its INR equivalent, so
    converting here would be answering a question RBI did not ask.
    """

    invoice_id: str
    shipping_bill_no: str
    shipping_bill_date: date | None
    purpose_code: str
    currency: str
    invoiced_foreign_minor: int
    realised_foreign_minor: int
    outstanding_foreign_minor: int
    realisation_deadline: date | None
    as_of: date
    days_to_deadline: int | None
    overdue: bool
    partially_realised: bool
    status: str
    signature: str


def load_export_receipts(path: str | Path, *, as_of: date) -> list[ExportReceipt]:
    """Every invoice ledger row carrying a shipping bill, aged against `as_of`.

    Rows without a shipping bill number are domestic sales; they have no EDPMS
    obligation and are skipped rather than reported with empty regulatory
    fields.

    Status rule, and the one judgement call in this module:

      * outstanding <= 0                      -> REALISED, nothing owed
      * outstanding > 0 and (partially
        realised or past the deadline)        -> OPEN_EDPMS_LINKAGE, an exception
      * outstanding > 0, nothing realised
        yet, deadline still ahead             -> AGING, on the clock but not yet
                                                 an exception

    The middle rule is what makes this an exception rather than a to-do list. A
    freshly issued export invoice with eight months to run is simply a young
    receivable; every export invoice would otherwise be an "exception" on the
    day it was raised, which is the over-reporting spec §5 exists to avoid. A
    *part*-realised bill is different in kind: money has moved against it, so
    the bill is half-closed in EDPMS and will sit there accruing age until
    someone reconciles the remainder. That, and anything past its deadline, is
    what a compliance officer needs surfaced.
    """
    receipts: list[ExportReceipt] = []
    with open(path, newline="", encoding="utf-8") as f:
        for raw in csv.DictReader(f):
            bill = (raw.get("shipping_bill_no") or "").strip()
            if not bill:
                continue
            invoiced = parse_minor(raw["invoice_amount"])
            realised_raw = (raw.get("realised_amount") or "").strip()
            realised = parse_minor(realised_raw) if realised_raw else 0
            outstanding = invoiced - realised
            deadline = _date_or_none(raw.get("realisation_deadline"))
            days = (deadline - as_of).days if deadline else None
            overdue = outstanding > 0 and days is not None and days < 0
            partial = 0 < realised < invoiced

            if outstanding <= 0:
                status = REALISED
                sig = f"shipping bill {bill} fully realised ({realised} of {invoiced} {raw['currency']} minor)"
            elif partial or overdue:
                status = OPEN_EDPMS_LINKAGE
                sig = (
                    f"shipping bill {bill} ({raw.get('purpose_code', '')}) dated "
                    f"{raw.get('shipping_bill_date', '')}: realised {realised} of "
                    f"{invoiced} {raw['currency']} minor, {outstanding} outstanding; "
                    f"deadline {deadline}, {_days_phrase(days)} as of {as_of}"
                )
            else:
                status = AGING
                sig = (
                    f"shipping bill {bill} unrealised, {outstanding} "
                    f"{raw['currency']} minor outstanding, {_days_phrase(days)} "
                    f"as of {as_of}: on the clock, not yet an exception"
                )

            receipts.append(
                ExportReceipt(
                    invoice_id=raw["invoice_id"],
                    shipping_bill_no=bill,
                    shipping_bill_date=_date_or_none(raw.get("shipping_bill_date")),
                    purpose_code=(raw.get("purpose_code") or "").strip(),
                    currency=raw["currency"],
                    invoiced_foreign_minor=invoiced,
                    realised_foreign_minor=realised,
                    outstanding_foreign_minor=outstanding,
                    realisation_deadline=deadline,
                    as_of=as_of,
                    days_to_deadline=days,
                    overdue=overdue,
                    partially_realised=partial,
                    status=status,
                    signature=sig,
                )
            )
    return receipts


def open_edpms_exceptions(receipts: list[ExportReceipt]) -> list[ExportReceipt]:
    """The subset a compliance officer has to act on, most urgent first."""
    return sorted(
        (r for r in receipts if r.status == OPEN_EDPMS_LINKAGE),
        key=lambda r: (r.days_to_deadline if r.days_to_deadline is not None else 0),
    )


def _date_or_none(raw: str | None) -> date | None:
    raw = (raw or "").strip()
    return date.fromisoformat(raw) if raw else None


def _days_phrase(days: int | None) -> str:
    if days is None:
        return "no realisation deadline recorded"
    return f"{days} days to deadline" if days >= 0 else f"{-days} days past deadline"
