"""Malformed and edge-case input. For each case this asserts the specific
behavior chosen -- raise or skip/default -- rather than just "doesn't
crash". The choices, and why:

  truncated MT103 (missing '{4:'/'-}')     -> raise: can't trust field
                                               boundaries in a corrupt message
  MT103 missing a mandatory tag (:20:/:32A:) -> raise: no id or amount, no record
  :70: wraps mid-token                      -> not an error: rejoin exactly
                                               (covered in test_ingest.py)
  :32A: with a malformed date                -> raise
  camt.053 missing/wrong namespace           -> raise: silently returning an
                                               empty list would look like "no
                                               credits this period" instead of
                                               "wrong file"
  camt.053 entry with no RmtInf               -> default to "": a genuinely
                                               blank remittance field is real
                                               bank behavior, not corruption
  CSV row with an empty amount                -> raise
  CSV amount with the wrong decimal separator -> raise
  settlement whose rows don't net to a single
  row's value (multi-row aggregation)         -> not an error: this is what
                                               aggregation is for (covered in
                                               test_ingest.py's refund case)
"""

import csv
from pathlib import Path

import pytest

from reconagent.camt053 import CAMT_NS, Camt053ParseError, parse_camt053_file
from reconagent.mt103 import MT103ParseError, parse_mt103_file, parse_mt103_text
from reconagent.razorpay import RazorpayParseError, parse_razorpay_settlements

GOOD_MT103 = (
    "{1:F01ICICINBBAXXX0000000000}{2:O1031429260805BARCGB22AXXX00010000292608051429N}"
    "{3:{121:5e2f7beb-2b9d-4fa9-aff9-8fe49d2775b8}}{4:\n"
    ":20:FT274678066061\n"
    ":23B:CRED\n"
    ":32A:260805INR5663622,94\n"
    ":33B:GBP52575,02\n"
    ":36:111,6780\n"
    ":50K:/GB14630246802022641\n"
    "KESTREL SYSTEMS LIMITED\n"
    "18 FENCHURCH AVENUE\n"
    "LONDON GB\n"
    ":52A:BARCGB22XXX\n"
    ":57A:ICICINBBXXX\n"
    ":59:/50200087654321\n"
    "GLOBEX EXPORTS PRIVATE LIMITED\n"
    "MUMBAI IN\n"
    ":70:/INV/INV-2026-M00033/RFB/EXPORT PRO\n"
    "CEEDS KESTREL SYSTEMS LIMITED\n"
    ":71A:SHA\n"
    ":72:/ACC/PURPOSE CODE P1006\n"
    "-}"
)

CSV_HEADER = [
    "entity_id", "type", "debit", "credit", "amount", "currency", "fee", "tax",
    "on_hold", "settled", "created_at", "settled_at", "settlement_id",
    "settlement_utr", "description", "notes", "payment_id", "order_id",
    "order_receipt", "method", "international", "conversion_rate",
    "base_amount", "base_currency", "refund_id",
]

GOOD_ROW = {
    "entity_id": "pay_x", "type": "payment", "debit": "0.00", "credit": "100.00",
    "amount": "100.00", "currency": "INR", "fee": "2.00", "tax": "0.36",
    "on_hold": "N", "settled": "Y", "created_at": "2026-08-01",
    "settled_at": "2026-08-02", "settlement_id": "setl_x", "settlement_utr": "UTR1",
    "description": "Payment for INV-1", "notes": "INV-1", "payment_id": "pay_x",
    "order_id": "order_x", "order_receipt": "INV-1", "method": "card",
    "international": "N", "conversion_rate": "", "base_amount": "100.00",
    "base_currency": "INR", "refund_id": "",
}


def _write_csv(path: Path, rows: list[dict]) -> Path:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADER)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return path


# ---- MT103 ---------------------------------------------------------------------


def test_mt103_sanity_the_good_message_parses():
    msg = parse_mt103_text(GOOD_MT103)
    assert msg.reference == "FT274678066061"


def test_mt103_truncated_block_raises():
    truncated = GOOD_MT103.split(":50K:")[0]  # cuts off mid-message, no trailer
    with pytest.raises(MT103ParseError):
        parse_mt103_text(truncated)


def test_mt103_missing_trailer_raises():
    no_trailer = GOOD_MT103.rsplit("\n-}", 1)[0]
    with pytest.raises(MT103ParseError):
        parse_mt103_text(no_trailer)


def test_mt103_missing_mandatory_reference_tag_raises():
    without_20 = "\n".join(l for l in GOOD_MT103.splitlines() if not l.startswith(":20:"))
    with pytest.raises(MT103ParseError):
        parse_mt103_text(without_20)


def test_mt103_missing_mandatory_32a_tag_raises():
    without_32a = "\n".join(l for l in GOOD_MT103.splitlines() if not l.startswith(":32A:"))
    with pytest.raises(MT103ParseError):
        parse_mt103_text(without_32a)


def test_mt103_malformed_32a_date_raises():
    bad_date = GOOD_MT103.replace(":32A:260805INR", ":32A:261305INR")  # month 13
    with pytest.raises(MT103ParseError):
        parse_mt103_text(bad_date)


def test_mt103_malformed_32a_shape_raises():
    bad_shape = GOOD_MT103.replace(":32A:260805INR5663622,94", ":32A:not-a-32a-value")
    with pytest.raises(MT103ParseError):
        parse_mt103_text(bad_shape)


def test_mt103_file_level_truncated_message_raises(tmp_path):
    path = tmp_path / "broken.mt103"
    path.write_text(GOOD_MT103.split(":50K:")[0] + "\n$\n", encoding="utf-8")
    with pytest.raises(MT103ParseError):
        parse_mt103_file(path)


# ---- camt.053 --------------------------------------------------------------------


def _camt(body_entries: str, ns: str = CAMT_NS) -> str:
    return (
        f'<?xml version="1.0" encoding="utf-8"?>\n'
        f'<Document xmlns="{ns}"><BkToCstmrStmt><Stmt>{body_entries}</Stmt>'
        f"</BkToCstmrStmt></Document>\n"
    )


GOOD_ENTRY = """
<Ntry>
  <NtryRef>BNKX000001</NtryRef>
  <Amt Ccy="INR">1000.00</Amt>
  <CdtDbtInd>CRDT</CdtDbtInd>
  <BookgDt><Dt>2026-08-04</Dt></BookgDt>
  <ValDt><Dt>2026-08-04</Dt></ValDt>
  <NtryDtls><TxDtls>
    <Refs><EndToEndId>FT1</EndToEndId></Refs>
    <RltdPties><Dbtr><Nm>SOME DEBTOR</Nm></Dbtr></RltdPties>
    <RmtInf><Ustrd>NARRATION TEXT</Ustrd></RmtInf>
  </TxDtls></NtryDtls>
</Ntry>
"""


def test_camt_sanity_the_good_entry_parses(tmp_path):
    path = tmp_path / "good.camt053.xml"
    path.write_text(_camt(GOOD_ENTRY), encoding="utf-8")
    records = parse_camt053_file(path)
    assert len(records) == 1
    assert records[0].narration == "NARRATION TEXT"


def test_camt_missing_namespace_raises(tmp_path):
    path = tmp_path / "no_ns.camt053.xml"
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f"<Document><BkToCstmrStmt><Stmt>{GOOD_ENTRY}</Stmt></BkToCstmrStmt></Document>\n"
    )
    path.write_text(xml, encoding="utf-8")
    with pytest.raises(Camt053ParseError):
        parse_camt053_file(path)


def test_camt_wrong_namespace_raises(tmp_path):
    path = tmp_path / "wrong_ns.camt053.xml"
    path.write_text(
        _camt(GOOD_ENTRY, ns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.08"),
        encoding="utf-8",
    )
    with pytest.raises(Camt053ParseError):
        parse_camt053_file(path)


def test_camt_absent_rmtinf_defaults_to_empty_narration(tmp_path):
    entry_no_rmtinf = GOOD_ENTRY.replace(
        "<RmtInf><Ustrd>NARRATION TEXT</Ustrd></RmtInf>", ""
    )
    path = tmp_path / "no_rmtinf.camt053.xml"
    path.write_text(_camt(entry_no_rmtinf), encoding="utf-8")
    records = parse_camt053_file(path)
    assert len(records) == 1
    assert records[0].narration == ""


def test_camt_missing_ntryref_raises(tmp_path):
    entry = GOOD_ENTRY.replace("<NtryRef>BNKX000001</NtryRef>", "")
    path = tmp_path / "no_ref.camt053.xml"
    path.write_text(_camt(entry), encoding="utf-8")
    with pytest.raises(Camt053ParseError):
        parse_camt053_file(path)


def test_camt_debit_entry_is_skipped_not_misread_as_credit(tmp_path):
    entry = GOOD_ENTRY.replace("<CdtDbtInd>CRDT</CdtDbtInd>", "<CdtDbtInd>DBIT</CdtDbtInd>")
    path = tmp_path / "debit.camt053.xml"
    path.write_text(_camt(entry), encoding="utf-8")
    assert parse_camt053_file(path) == []


# ---- Razorpay CSV ---------------------------------------------------------------


def test_razorpay_sanity_the_good_row_parses(tmp_path):
    path = _write_csv(tmp_path / "good.csv", [GOOD_ROW])
    records = parse_razorpay_settlements(path)
    assert len(records) == 1
    assert records[0].amount_minor == 10000


def test_razorpay_empty_amount_raises(tmp_path):
    row = dict(GOOD_ROW, amount="")
    path = _write_csv(tmp_path / "empty_amount.csv", [row])
    with pytest.raises(RazorpayParseError):
        parse_razorpay_settlements(path)


def test_razorpay_wrong_decimal_separator_raises(tmp_path):
    row = dict(GOOD_ROW, credit="100,00")  # comma instead of '.'
    path = _write_csv(tmp_path / "comma.csv", [row])
    with pytest.raises(RazorpayParseError):
        parse_razorpay_settlements(path)


def test_razorpay_settlement_with_no_payment_row_raises(tmp_path):
    refund_only = dict(GOOD_ROW, type="refund", credit="0.00", debit="100.00")
    path = _write_csv(tmp_path / "refund_only.csv", [refund_only])
    with pytest.raises(RazorpayParseError):
        parse_razorpay_settlements(path)


def test_razorpay_multi_row_settlement_nets_across_rows_not_a_single_row(tmp_path):
    """A settlement carrying both a payment and a refund row: amount_minor is
    the capture net, not capture-minus-refund. The refund is a separate bank
    movement at its own FX rate (spec §5); netting it here would produce an
    amount matching no bank credit."""
    payment = dict(GOOD_ROW, entity_id="pay_y", type="payment", credit="500.00", debit="0.00")
    refund = dict(
        GOOD_ROW, entity_id="rfnd_y", type="refund", credit="0.00", debit="120.00",
        refund_id="rfnd_y",
    )
    path = _write_csv(tmp_path / "multi.csv", [payment, refund])
    records = parse_razorpay_settlements(path)
    assert len(records) == 1
    assert records[0].amount_minor == 50000
    assert len(records[0].rows) == 2
