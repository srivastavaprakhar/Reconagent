"""Parsers exercised against the real generated data in data/ -- parse counts
match what's actually in the files (computed from the raw file, not
hardcoded, so this doesn't rot if the generator's --scale changes), spot-
checked field values are exact, and the MT103/camt.053 cross-format
correspondence the spec calls out holds.
"""

import csv
import re
import xml.etree.ElementTree as ET
from decimal import Decimal
from pathlib import Path

import pytest

from reconagent.camt053 import CAMT_NS, parse_camt053_file
from reconagent.invoices import parse_invoice_ledger
from reconagent.mt103 import parse_mt103_file
from reconagent.razorpay import parse_razorpay_settlements

DATA = Path(__file__).resolve().parent.parent / "data"
_NS = {"c": CAMT_NS}


# ---- Razorpay settlements ---------------------------------------------------


def test_razorpay_settlement_count_matches_distinct_settlement_ids():
    rows = list(csv.DictReader(open(DATA / "razorpay_settlements.csv")))
    expected = len({r["settlement_id"] for r in rows})
    records = parse_razorpay_settlements(DATA / "razorpay_settlements.csv")
    assert len(records) == expected


def test_razorpay_first_settlement_field_values():
    records = parse_razorpay_settlements(DATA / "razorpay_settlements.csv")
    by_id = {r.record_id: r for r in records}
    r = by_id["setl_lBfnclOpWerO10"]
    assert r.amount_minor == 2488114  # credit 24881.14, no debit rows
    assert r.currency == "INR"
    assert r.utr == "K5K6QMGFIJVEMVVH"
    assert r.invoice_id == "INV-2026-M00001"
    assert r.payment_id == "pay_HgPDLLNAyJwgqL"
    assert r.base_amount_minor == 2548253
    assert len(r.rows) == 1


def test_razorpay_settlement_with_refund_aggregates_multiple_rows():
    """A settlement with a payment row and a later refund row: amount_minor is
    the capture net only -- the refund settles as its own bank movement at its
    own FX rate (spec §5) and must not be folded in. Both rows survive on .rows
    so refund-FX-asymmetry logic downstream can see each conversion rate."""
    records = parse_razorpay_settlements(DATA / "razorpay_settlements.csv")
    multi = [r for r in records if len(r.rows) > 1]
    assert multi, "expected at least one settlement with a refund row in the main dataset"
    r = multi[0]
    capture_net = sum(
        row.credit_minor - row.debit_minor for row in r.rows if row.type != "refund"
    )
    assert r.amount_minor == capture_net
    refund_debits = sum(row.debit_minor for row in r.rows if row.type == "refund")
    assert refund_debits > 0
    assert r.amount_minor != capture_net - refund_debits
    types = {row.type for row in r.rows}
    assert "payment" in types and "refund" in types
    refund_row = next(row for row in r.rows if row.type == "refund")
    payment_row = next(row for row in r.rows if row.type == "payment")
    assert refund_row.conversion_rate != payment_row.conversion_rate


def test_razorpay_international_row_preserves_conversion_rate_and_base_amount():
    records = parse_razorpay_settlements(DATA / "razorpay_settlements.csv")
    intl = [r for r in records if r.conversion_rate is not None]
    assert intl
    r = intl[0]
    assert isinstance(r.conversion_rate, Decimal)
    assert r.foreign_amount_minor is not None
    assert r.foreign_currency is not None
    assert r.base_amount_minor is not None


# ---- MT103 -------------------------------------------------------------------


def test_mt103_message_count_matches_dollar_delimited_blocks_in_file():
    text = (DATA / "bank_statement.mt103").read_text(encoding="utf-8")
    expected = len([p for p in re.split(r"\n\$\n?", text) if p.strip()])
    records = parse_mt103_file(DATA / "bank_statement.mt103")
    assert len(records) == expected
    assert expected > 0


def test_mt103_first_message_field_values():
    records = parse_mt103_file(DATA / "bank_statement.mt103")
    r = records[0]
    assert r.record_id == "FT274678066061"
    assert r.end_to_end_id == "FT274678066061"
    assert r.amount_minor == 566362294  # :32A: ...INR5663622,94
    assert r.currency == "INR"
    assert r.foreign_amount_minor == 5257502  # :33B: GBP52575,02
    assert r.foreign_currency == "GBP"
    assert r.conversion_rate == Decimal("111.6780")  # :36: 111,6780
    assert "KESTREL SYSTEMS LIMITED" in r.counterparty_name
    assert r.narration == "/INV/INV-2026-M00033/RFB/EXPORT PROCEEDS KESTREL SYSTEMS LIMITED"
    assert r.value_date.isoformat() == "2026-08-05"


def test_mt103_70_rejoins_mid_word_wrap_exactly():
    """The real file wraps ':70:' at a fixed 35-char width, splitting a word
    ('PROCEEDS') across two continuation lines. Rejoining must reproduce the
    original text exactly, not insert a space at the wrap point."""
    records = parse_mt103_file(DATA / "bank_statement.mt103")
    r = records[0]
    assert "PROCEEDS" in r.narration
    assert "PRO CEEDS" not in r.narration
    assert "PRO\nCEEDS" not in r.narration


# ---- camt.053 -----------------------------------------------------------------


def test_camt053_entry_count_matches_ntry_elements_in_file():
    tree = ET.parse(DATA / "bank_statement.camt053.xml")
    entries = tree.getroot().findall(".//c:Ntry", _NS)
    expected = sum(1 for e in entries if e.find("c:CdtDbtInd", _NS).text == "CRDT")
    records = parse_camt053_file(DATA / "bank_statement.camt053.xml")
    assert len(records) == expected
    assert expected > 0


def test_camt053_first_domestic_entry_field_values():
    records = parse_camt053_file(DATA / "bank_statement.camt053.xml")
    r = next(x for x in records if x.record_id == "BNKM000009")
    assert r.amount_minor == 965587
    assert r.currency == "INR"
    assert r.counterparty_name == "RAZORPAY SOFTWARE PVT LTD"
    assert r.narration == (
        "NEFT CR-RATN0000088-RAZORPAY SOFTWARE PVT LTD-PUJSGPO2BV77F2H5-INV-2026-M00012"
    )
    assert r.booking_date.isoformat() == "2026-08-04"
    assert r.value_date.isoformat() == "2026-08-04"
    assert r.end_to_end_id == "FT590445258346"
    assert r.channel == "camt.053"


def test_camt053_cross_border_entry_carries_fx_fields():
    records = parse_camt053_file(DATA / "bank_statement.camt053.xml")
    r = next(x for x in records if x.record_id == "BNKM000029")
    assert r.foreign_currency == "GBP"
    assert r.foreign_amount_minor == 5257502
    assert r.conversion_rate == Decimal("111.6780")


# ---- MT103 / camt.053 cross-format correspondence -----------------------------


def test_mt103_and_camt053_agree_on_shared_cross_border_credits():
    """Where a credit appears in both formats, the MT103's :20: equals the
    camt's EndToEndId and the amounts agree exactly (per the data table in
    the task brief)."""
    mt103_records = parse_mt103_file(DATA / "bank_statement.mt103")
    camt_records = parse_camt053_file(DATA / "bank_statement.camt053.xml")
    camt_by_e2e = {r.end_to_end_id: r for r in camt_records if r.end_to_end_id}

    assert mt103_records, "expected at least one MT103 message to cross-check"
    matched = 0
    for mt in mt103_records:
        camt = camt_by_e2e.get(mt.end_to_end_id)
        assert camt is not None, f"MT103 :20: {mt.end_to_end_id!r} has no camt.053 counterpart"
        assert camt.amount_minor == mt.amount_minor
        assert camt.currency == mt.currency
        assert camt.foreign_amount_minor == mt.foreign_amount_minor
        assert camt.foreign_currency == mt.foreign_currency
        assert camt.conversion_rate == mt.conversion_rate
        matched += 1
    assert matched == len(mt103_records)


# ---- invoice ledger -------------------------------------------------------------


def test_invoice_ledger_row_count_matches_file():
    rows = list(csv.DictReader(open(DATA / "invoice_ledger.csv")))
    records = parse_invoice_ledger(DATA / "invoice_ledger.csv")
    assert len(records) == len(rows)


def test_invoice_first_row_field_values():
    records = parse_invoice_ledger(DATA / "invoice_ledger.csv")
    r = records[0]
    assert r.record_id == "INV-2026-M00001"
    assert r.counterparty_name == "Deccan Hardware Co"
    assert r.amount_minor == 2548253
    assert r.currency == "INR"


# ---- holdout: parsers must handle it, never tuned against it ------------------


HOLDOUT = DATA / "holdout"


def test_holdout_razorpay_settlements_parse():
    records = parse_razorpay_settlements(HOLDOUT / "HOLDOUT_razorpay_settlements.csv")
    rows = list(csv.DictReader(open(HOLDOUT / "HOLDOUT_razorpay_settlements.csv")))
    assert len(records) == len({r["settlement_id"] for r in rows})
    assert all(isinstance(r.amount_minor, int) for r in records)


def test_holdout_mt103_parses():
    records = parse_mt103_file(HOLDOUT / "HOLDOUT_bank_statement.mt103")
    text = (HOLDOUT / "HOLDOUT_bank_statement.mt103").read_text(encoding="utf-8")
    expected = len([p for p in re.split(r"\n\$\n?", text) if p.strip()])
    assert len(records) == expected
    assert expected > 0


def test_holdout_camt053_parses():
    records = parse_camt053_file(HOLDOUT / "HOLDOUT_bank_statement.camt053.xml")
    tree = ET.parse(HOLDOUT / "HOLDOUT_bank_statement.camt053.xml")
    entries = tree.getroot().findall(".//c:Ntry", _NS)
    expected = sum(1 for e in entries if e.find("c:CdtDbtInd", _NS).text == "CRDT")
    assert len(records) == expected


def test_holdout_invoice_ledger_parses():
    records = parse_invoice_ledger(HOLDOUT / "HOLDOUT_invoice_ledger.csv")
    rows = list(csv.DictReader(open(HOLDOUT / "HOLDOUT_invoice_ledger.csv")))
    assert len(records) == len(rows)


def test_holdout_mt103_and_camt053_still_agree():
    mt103_records = parse_mt103_file(HOLDOUT / "HOLDOUT_bank_statement.mt103")
    camt_records = parse_camt053_file(HOLDOUT / "HOLDOUT_bank_statement.camt053.xml")
    camt_by_e2e = {r.end_to_end_id: r for r in camt_records if r.end_to_end_id}
    for mt in mt103_records:
        camt = camt_by_e2e.get(mt.end_to_end_id)
        assert camt is not None
        assert camt.amount_minor == mt.amount_minor


def test_refund_row_is_not_netted_into_the_settlement_capture():
    """A refund converts at its own FX event and settles as its own bank
    movement (spec §5). Netting it into the capture yields an amount that
    matches no bank credit, which would break the subset-sum solver on exactly
    the refund cases -- and do it silently, since the arithmetic still 'works'.
    """
    import json

    from reconagent.razorpay import parse_razorpay_settlements

    by_id = {r.record_id: r for r in parse_razorpay_settlements("data/razorpay_settlements.csv")}
    gt = json.load(open("data/ground_truth.json"))

    checked = 0
    for case in gt["cases"]:
        link = case.get("expected_link") or {}
        for sid in link.get("covers_settlement_ids") or []:
            assert by_id[sid].amount_minor == sum(
                by_id[s].amount_minor for s in [sid]
            )
            checked += 1
        if link.get("covers_settlement_ids"):
            assert link["settlement_net_sum_minor"] == sum(
                by_id[s].amount_minor for s in link["covers_settlement_ids"]
            ), f"{case['case_id']}: parsed settlement net disagrees with ground truth"
    assert checked > 0

    # And the refund rows are still reachable for the FX layer.
    refunds = [
        row
        for rec in by_id.values()
        for row in rec.rows
        if row.type == "refund"
    ]
    assert refunds, "refund rows must remain on .rows"
    assert all(r.conversion_rate is not None for r in refunds)
