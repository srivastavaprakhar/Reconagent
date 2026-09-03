"""camt.053 (ISO 20022) parser, namespace-aware.

Targets the Debtor/Creditor blocks and the dedicated RmtInf/Ustrd element
specifically (spec §5), plus refs, amounts, CdtDbtInd, and both booking and
value dates -- not a generic "find all text nodes" walk.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

from reconagent.money import parse_minor, parse_rate
from reconagent.records import CanonicalRecord

CAMT_NS = "urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"
_NS = {"c": CAMT_NS}
_DOCUMENT_TAG = f"{{{CAMT_NS}}}Document"


class Camt053ParseError(ValueError):
    """The document isn't a recognizable camt.053.001.02 Document, or an
    entry is missing a field a CanonicalRecord cannot do without."""


def _text(el: ET.Element | None, path: str) -> str | None:
    if el is None:
        return None
    found = el.find(path, _NS)
    return found.text if found is not None else None


def _date(el: ET.Element | None, path: str) -> date | None:
    s = _text(el, path)
    if s is None:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError as exc:
        raise Camt053ParseError(f"malformed date {s!r} at {path}") from exc


def parse_camt053_file(path: str | Path) -> list[CanonicalRecord]:
    tree = ET.parse(path)
    root = tree.getroot()
    if root.tag != _DOCUMENT_TAG:
        raise Camt053ParseError(
            f"unexpected root/namespace {root.tag!r}; expected {_DOCUMENT_TAG!r} "
            "(missing or wrong camt.053.001.02 namespace)"
        )

    records: list[CanonicalRecord] = []
    for ntry in root.findall(".//c:Ntry", _NS):
        ntry_ref = _text(ntry, "c:NtryRef")
        amt_el = ntry.find("c:Amt", _NS)
        if ntry_ref is None or amt_el is None or amt_el.text is None:
            raise Camt053ParseError(
                f"Ntry missing NtryRef or Amt: {ET.tostring(ntry, encoding='unicode')[:200]!r}"
            )

        cdt_dbt_ind = _text(ntry, "c:CdtDbtInd")
        if cdt_dbt_ind != "CRDT":
            # This dataset's camt.053 covers every *credit*; a debit entry
            # (bank charge, chargeback) needs different handling this unit
            # doesn't own. Skip rather than silently treat it as a credit.
            continue

        amount_minor = parse_minor(amt_el.text)
        currency = amt_el.get("Ccy", "")
        booking_date = _date(ntry, "c:BookgDt/c:Dt")
        value_date = _date(ntry, "c:ValDt/c:Dt")

        tx = ntry.find("c:NtryDtls/c:TxDtls", _NS)
        end_to_end_id = _text(tx, "c:Refs/c:EndToEndId")
        dbtr_name = _text(tx, "c:RltdPties/c:Dbtr/c:Nm")
        narration = _text(tx, "c:RmtInf/c:Ustrd") or ""

        foreign_ccy = foreign_minor = rate = None
        if tx is not None:
            instd = tx.find("c:AmtDtls/c:InstdAmt/c:Amt", _NS)
            if instd is not None and instd.text is not None:
                foreign_ccy = instd.get("Ccy")
                foreign_minor = parse_minor(instd.text)
            rate_el = tx.find("c:AmtDtls/c:TxAmt/c:CcyXchg/c:XchgRate", _NS)
            if rate_el is not None and rate_el.text is not None:
                rate = parse_rate(rate_el.text)

        records.append(
            CanonicalRecord(
                source="bank_credit",
                record_id=ntry_ref,
                counterparty_name=dbtr_name or "",
                narration=narration,
                amount_minor=amount_minor,
                currency=currency,
                booking_date=booking_date,
                value_date=value_date,
                end_to_end_id=end_to_end_id,
                conversion_rate=rate,
                foreign_amount_minor=foreign_minor,
                foreign_currency=foreign_ccy,
                channel="camt.053",
            )
        )
    return records
