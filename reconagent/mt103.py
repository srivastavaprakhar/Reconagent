"""SWIFT MT103 parser.

Targets the specific fields the fuzzy matcher needs (spec §5): the ordering
customer in :50a: (K/A/F variants), the beneficiary in :59:, free-text
remittance information in :70:, charge details in :71A:, and value
date/currency/amount packed into :32A:. This walks the block structure and
pulls named tags -- it does not tokenize a whole statement line and hope.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from reconagent.money import parse_minor, parse_rate
from reconagent.records import CanonicalRecord

_TAG_LINE_RE = re.compile(r"^:(\d{2}[A-Z]?):(.*)$")
_32A_RE = re.compile(r"^(\d{6})([A-Z]{3})([\d,]+)$")
_33B_RE = re.compile(r"^([A-Z]{3})([\d,]+)$")
_PURPOSE_RE = re.compile(r"PURPOSE CODE\s+(\S+)")


class MT103ParseError(ValueError):
    """An MT103 message is structurally malformed, or missing a field the
    canonical record cannot do without."""


@dataclass(frozen=True)
class MT103Message:
    """The subset of an MT103 message the matcher needs, by field name."""

    reference: str  # :20:
    value_date: date  # :32A: date component
    settlement_currency: str  # :32A: currency component
    settlement_amount_minor: int  # :32A: amount component
    instructed_currency: str | None  # :33B:
    instructed_amount_minor: int | None  # :33B:
    exchange_rate: Decimal | None  # :36:
    ordering_account: str | None  # :50a: party identifier line
    ordering_customer: str  # :50a: name/address, rejoined
    beneficiary_account: str | None  # :59: party identifier line
    beneficiary: str  # :59: name/address, rejoined
    remittance_info: str  # :70:, continuation lines rejoined with no separator
    charge_details: str | None  # :71A:
    purpose_code: str | None  # pulled out of :72: free text


def _split_messages(text: str) -> list[str]:
    # write_mt103 joins messages with the literal separator "\n$\n" and
    # appends a trailing one; splitting on "\n$" with an optional trailing
    # newline recovers each message regardless of whether the file ends on
    # a final newline.
    parts = re.split(r"\n\$\n?", text)
    return [p for p in parts if p.strip()]


def _tagged_fields(block4: str) -> dict[str, list[str]]:
    """Split block 4 into {tag: [lines]}. A line that doesn't open a new tag
    is a continuation of the previous one -- this is what makes a
    35-char-wrapped :70: rejoinable instead of read as separate fields."""
    fields: dict[str, list[str]] = {}
    current: str | None = None
    for line in block4.splitlines():
        m = _TAG_LINE_RE.match(line)
        if m:
            current = m.group(1)
            fields.setdefault(current, []).append(m.group(2))
        elif current is not None and line.strip():
            fields[current].append(line)
    return fields


def _rejoin_wrapped(lines: list[str]) -> str:
    """:70: continuation lines are fixed 35-char slices with no regard for
    word boundaries -- concatenate with no separator to reconstruct."""
    return "".join(lines).strip()


def _party_text(lines: list[str]) -> tuple[str | None, str]:
    """Split a :50a:/:59: block into (account, name+address text). The
    first line is the party identifier when it starts with '/'."""
    if lines and lines[0].strip().startswith("/"):
        account = lines[0].strip().lstrip("/") or None
        text = " ".join(l.strip() for l in lines[1:] if l.strip())
    else:
        account = None
        text = " ".join(l.strip() for l in lines if l.strip())
    return account, text


def _parse_32a(raw: str) -> tuple[date, str, int]:
    raw = raw.strip()
    m = _32A_RE.match(raw)
    if not m:
        raise MT103ParseError(f"malformed field :32A: {raw!r}")
    yymmdd, ccy, amt = m.groups()
    try:
        value_date = date(2000 + int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6]))
    except ValueError as exc:
        raise MT103ParseError(f"malformed date in field :32A: {raw!r}") from exc
    return value_date, ccy, parse_minor(amt.replace(",", "."))


def _parse_33b(raw: str) -> tuple[str, int]:
    m = _33B_RE.match(raw.strip())
    if not m:
        raise MT103ParseError(f"malformed field :33B: {raw!r}")
    ccy, amt = m.groups()
    return ccy, parse_minor(amt.replace(",", "."))


def _field_50(fields: dict[str, list[str]]) -> list[str]:
    for variant in ("50K", "50A", "50F", "50"):
        if variant in fields:
            return fields[variant]
    raise MT103ParseError("missing mandatory field :50a: (ordering customer)")


def parse_mt103_text(raw_message: str) -> MT103Message:
    if "{4:" not in raw_message or not raw_message.rstrip().endswith("-}"):
        raise MT103ParseError("truncated MT103 message: missing block 4 or '-}' trailer")
    block4 = raw_message.split("{4:", 1)[1].rsplit("-}", 1)[0]
    fields = _tagged_fields(block4)

    if "20" not in fields:
        raise MT103ParseError("missing mandatory field :20: (transaction reference)")
    if "32A" not in fields:
        raise MT103ParseError("missing mandatory field :32A: (value date/currency/amount)")

    reference = fields["20"][0].strip()
    value_date, ccy, amount_minor = _parse_32a(fields["32A"][0])

    instructed_ccy = instructed_minor = None
    if "33B" in fields:
        instructed_ccy, instructed_minor = _parse_33b(fields["33B"][0])

    rate = parse_rate(fields["36"][0].strip().replace(",", ".")) if "36" in fields else None

    ordering_account, ordering_customer = _party_text(_field_50(fields))
    beneficiary_account, beneficiary = _party_text(fields.get("59", []))
    remittance = _rejoin_wrapped(fields.get("70", []))
    charges = fields["71A"][0].strip() if "71A" in fields else None

    purpose = None
    if "72" in fields:
        m = _PURPOSE_RE.search(_rejoin_wrapped(fields["72"]))
        purpose = m.group(1) if m else None

    return MT103Message(
        reference=reference,
        value_date=value_date,
        settlement_currency=ccy,
        settlement_amount_minor=amount_minor,
        instructed_currency=instructed_ccy,
        instructed_amount_minor=instructed_minor,
        exchange_rate=rate,
        ordering_account=ordering_account,
        ordering_customer=ordering_customer,
        beneficiary_account=beneficiary_account,
        beneficiary=beneficiary,
        remittance_info=remittance,
        charge_details=charges,
        purpose_code=purpose,
    )


def parse_mt103_file(path: str | Path) -> list[CanonicalRecord]:
    text = Path(path).read_text(encoding="utf-8")
    records = []
    for raw_message in _split_messages(text):
        msg = parse_mt103_text(raw_message)
        records.append(
            CanonicalRecord(
                source="bank_credit",
                record_id=msg.reference,
                counterparty_name=msg.ordering_customer,
                narration=msg.remittance_info,
                amount_minor=msg.settlement_amount_minor,
                currency=msg.settlement_currency,
                booking_date=msg.value_date,
                value_date=msg.value_date,
                end_to_end_id=msg.reference,
                conversion_rate=msg.exchange_rate,
                foreign_amount_minor=msg.instructed_amount_minor,
                foreign_currency=msg.instructed_currency,
                channel="SWIFT_MT103",
            )
        )
    return records
