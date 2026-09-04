"""Correctness tests for the Tier 2 stress-test dataset generator.

Mirrors tests/test_synthetic_data.py's approach (determinism, referential
integrity, no-float, real MT103/camt.053 structure) plus the one assertion
that is this dataset's whole reason to exist: Tier 1's own unmodified
cascade (reconagent.match.match_all) cannot resolve the overwhelming
majority of these cases.
"""

from __future__ import annotations

import csv
import filecmp
import importlib.util
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest

from reconagent.eval import load_split
from reconagent.match import MATCHED, PARTIAL

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "generate_stress_test", REPO / "scripts" / "generate_stress_test.py"
)
gen = importlib.util.module_from_spec(_spec)
sys.modules["generate_stress_test"] = gen
_spec.loader.exec_module(gen)

SEED = 20260904
CAMT_NS = "{urn:iso:std:iso:20022:tech:xsd:camt.053.001.02}"
DEFECT_CLASSES = {
    "transliteration_variant", "abbreviation_variant", "legal_vs_trading_name",
    "ocr_typo_narration", "invoice_description_mismatch",
}
MONEY_COLUMNS = {"debit", "credit", "amount", "fee", "tax", "base_amount",
                 "invoice_amount", "realised_amount"}


@pytest.fixture(scope="module")
def dataset(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("stress") / "stress_test"
    gen.generate(SEED, out)
    return out


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def ground_truth(root: Path) -> dict:
    return json.loads((root / "ground_truth.json").read_text())


# ------------------------------------------------------------------ determinism


def test_generator_is_byte_identical_under_the_same_seed(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    gen.generate(SEED, a)
    gen.generate(SEED, b)
    files = sorted(p.relative_to(a) for p in a.rglob("*") if p.is_file())
    assert files, "generator produced nothing"
    mismatch = [str(f) for f in files if not filecmp.cmp(a / f, b / f, shallow=False)]
    assert mismatch == []


def test_a_different_seed_produces_different_data(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    gen.generate(SEED, a)
    gen.generate(SEED + 1, b)
    assert not filecmp.cmp(
        a / "razorpay_settlements.csv", b / "razorpay_settlements.csv", shallow=False
    )


# ------------------------------------------------------------------ dataset shape


def test_five_categories_are_roughly_evenly_covered(dataset):
    gt = ground_truth(dataset)
    by_class = gt["counts"]["by_defect_class"]
    assert set(by_class) == DEFECT_CLASSES
    for dc, n in by_class.items():
        assert n >= 5, (dc, n)
    assert gt["counts"]["cases"] == sum(by_class.values())
    assert 30 <= gt["counts"]["cases"] <= 50


def test_every_case_is_matched_or_partial_in_ground_truth(dataset):
    """This dataset never asserts UNMATCHED ground truth -- the whole point
    is a real linkage exists, Tier 1 just can't see it."""
    gt = ground_truth(dataset)
    for case in gt["cases"]:
        assert case["expected_link_resolution"] in ("MATCHED", "PARTIAL"), case["case_id"]
        assert case["expected_link"]["bank_txn_id"] is not None, case["case_id"]


# ------------------------------------------------------------------ referential integrity


def test_every_case_resolves_to_records_that_exist(dataset):
    gt = ground_truth(dataset)
    rows = read_csv(dataset / "razorpay_settlements.csv")
    invoices = read_csv(dataset / "invoice_ledger.csv")
    tree = ET.parse(dataset / "bank_statement.camt053.xml")

    settlement_ids = {r["settlement_id"] for r in rows}
    payment_ids = {r["entity_id"] for r in rows}
    invoice_ids = {i["invoice_id"] for i in invoices}
    bank_ids = {e.text for e in tree.iter(f"{CAMT_NS}NtryRef")}

    seen_case_ids = set()
    for case in gt["cases"]:
        cid = case["case_id"]
        assert cid not in seen_case_ids
        seen_case_ids.add(cid)
        assert set(case["settlement_ids"]) <= settlement_ids, cid
        assert set(case["payment_ids"]) <= payment_ids, cid
        assert set(case["invoice_ids"]) <= invoice_ids, cid
        assert set(case["bank_txn_ids"]) <= bank_ids, cid
        link = case["expected_link"]
        assert set(link["covers_settlement_ids"]) <= set(case["settlement_ids"]), cid
        assert link["bank_txn_id"] in bank_ids, cid


def test_ground_truth_counts_match_the_emitted_files(dataset):
    gt = ground_truth(dataset)
    rows = read_csv(dataset / "razorpay_settlements.csv")
    tree = ET.parse(dataset / "bank_statement.camt053.xml")
    assert gt["counts"]["settlement_rows"] == len(rows)
    assert gt["counts"]["settlements"] == len({r["settlement_id"] for r in rows})
    assert gt["counts"]["invoices"] == len(read_csv(dataset / "invoice_ledger.csv"))
    assert gt["counts"]["bank_credits"] == len(list(tree.iter(f"{CAMT_NS}Ntry")))
    assert gt["counts"]["cases"] == len(gt["cases"])


def test_expected_links_reconcile_arithmetically(dataset):
    gt = ground_truth(dataset)
    nets = {
        r["settlement_id"]: int(Decimal(r["credit"]).scaleb(2))
        for r in read_csv(dataset / "razorpay_settlements.csv")
        if r["type"] == "payment"
    }
    credits = {}
    tree = ET.parse(dataset / "bank_statement.camt053.xml")
    for ntry in tree.iter(f"{CAMT_NS}Ntry"):
        ref = ntry.find(f"{CAMT_NS}NtryRef").text
        credits[ref] = int(Decimal(ntry.find(f"{CAMT_NS}Amt").text).scaleb(2))

    for case in gt["cases"]:
        link, cid = case["expected_link"], case["case_id"]
        assert link["settlement_net_sum_minor"] == sum(
            nets[s] for s in link["covers_settlement_ids"]
        ), cid
        assert credits[link["bank_txn_id"]] == link["credit_amount_minor"], cid
        assert link["residual_minor"] == (
            link["settlement_net_sum_minor"] - link["credit_amount_minor"]
        ), cid


def test_amount_delta_is_outside_tier1_tolerance_and_not_exact(dataset):
    """Every case's credit misses its settlement's net by more than Stage
    1/2's own 100-minor-unit tolerance (reconagent.match.AMOUNT_TOLERANCE_MINOR),
    and is never zero -- otherwise Stage 1/2 would land on it cleanly and
    the case would not be a stress case at all."""
    from reconagent.match import AMOUNT_TOLERANCE_MINOR

    gt = ground_truth(dataset)
    for case in gt["cases"]:
        residual = case["expected_link"]["residual_minor"]
        assert residual != 0, case["case_id"]
        assert abs(residual) > AMOUNT_TOLERANCE_MINOR, case["case_id"]


def test_amount_deltas_genuinely_vary_not_a_ceiling_artifact(dataset):
    """Regression test for a real bug found in review: an earlier version
    capped the delta at a flat 8,000 minor units, which -- against this
    dataset's settlement range (roughly INR 73K-50L) -- meant even the
    lowest bps draw already exceeded the cap on every case, so all 40
    deltas silently collapsed to the identical value 8000 regardless of the
    random bps drawn. `test_amount_delta_is_outside_tier1_tolerance_and_not_exact`
    above would not have caught this -- a constant delta is still nonzero
    and still outside tolerance. This test asserts the distribution itself,
    not just that each value individually looks plausible."""
    gt = ground_truth(dataset)
    deltas = [abs(case["expected_link"]["residual_minor"]) for case in gt["cases"]]
    assert len(set(deltas)) > len(deltas) // 2, (
        f"deltas are suspiciously uniform: {len(set(deltas))} unique values "
        f"out of {len(deltas)} cases -- looks like a ceiling/floor is clamping "
        f"nearly every draw to the same number instead of the intended bps range"
    )


def test_no_settlement_reference_appears_as_a_whole_token_in_any_narration(dataset):
    """The core defeat mechanism for Stage 1: no UTR / invoice id / order id /
    payment id belonging to a settlement is recoverable as a whole,
    case-insensitive token from any bank narration in this dataset."""
    rows = read_csv(dataset / "razorpay_settlements.csv")
    refs = set()
    for r in rows:
        for v in (r["settlement_utr"], r["order_receipt"], r["order_id"], r["payment_id"]):
            if v and len(v) >= 6:
                refs.add(v.upper())

    tree = ET.parse(dataset / "bank_statement.camt053.xml")
    for ntry in tree.iter(f"{CAMT_NS}Ntry"):
        narration = ntry.find(f".//{CAMT_NS}RmtInf/{CAMT_NS}Ustrd").text or ""
        tokens = {t.upper() for t in re.findall(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", narration)}
        leaked = tokens & refs
        assert not leaked, (ntry.find(f"{CAMT_NS}NtryRef").text, leaked)


# ------------------------------------------------------------------ MT103 is real MT103


MT_BLOCKS = re.compile(
    r"^\{1:F01[A-Z0-9]{12}\d{4}\d{6}\}\{2:O103\d{4}\d{6}[A-Z0-9]{12}\d{4}\d{6}\d{6}\d{4}[NU]\}"
    r"(\{3:\{121:[0-9a-f-]{36}\}\})?\{4:\n(?P<body>.*)\n-\}$",
    re.DOTALL,
)


def test_mt103_messages_are_structurally_valid(dataset):
    raw = (dataset / "bank_statement.mt103").read_text()
    msgs = [m for m in raw.split("\n$\n") if m.strip()]
    assert msgs, "no MT103 messages emitted"
    for msg in msgs:
        m = MT_BLOCKS.match(msg)
        assert m, msg[:120]
        body = m.group("body")
        tags = [ln for ln in body.split("\n") if ln.startswith(":")]
        seen = [t[: t.index(":", 1) + 1] for t in tags]
        for required in (":20:", ":23B:", ":32A:", ":50K:", ":59:", ":70:", ":71A:"):
            assert required in seen, (required, seen)
        f32a = next(ln for ln in tags if ln.startswith(":32A:"))[5:]
        assert re.fullmatch(r"\d{6}[A-Z]{3}\d+,\d{2}", f32a), f32a


def test_mt103_and_camt053_round_trip_through_the_real_parsers(dataset):
    """Stronger than shape-matching: the production parsers accept every
    message/entry without raising, and record counts agree."""
    from reconagent.camt053 import parse_camt053_file
    from reconagent.invoices import parse_invoice_ledger
    from reconagent.mt103 import parse_mt103_file
    from reconagent.razorpay import parse_razorpay_settlements

    settlements = parse_razorpay_settlements(dataset / "razorpay_settlements.csv")
    mt_credits = parse_mt103_file(dataset / "bank_statement.mt103")
    camt_credits = parse_camt053_file(dataset / "bank_statement.camt053.xml")
    invoices = parse_invoice_ledger(dataset / "invoice_ledger.csv")

    gt = ground_truth(dataset)
    assert len(settlements) == gt["counts"]["settlements"]
    assert len(mt_credits) == gt["counts"]["bank_credits"]
    assert len(camt_credits) == gt["counts"]["bank_credits"]
    assert len(invoices) == gt["counts"]["invoices"]


def test_camt053_is_structurally_valid(dataset):
    root = ET.parse(dataset / "bank_statement.camt053.xml").getroot()
    assert root.tag == f"{CAMT_NS}Document"
    stmt = root.find(f"{CAMT_NS}BkToCstmrStmt/{CAMT_NS}Stmt")
    assert stmt is not None
    assert stmt.find(f"{CAMT_NS}Acct/{CAMT_NS}Ccy").text == "INR"
    entries = stmt.findall(f"{CAMT_NS}Ntry")
    assert entries
    for ntry in entries:
        amt = ntry.find(f"{CAMT_NS}Amt")
        assert amt.get("Ccy") == "INR"
        assert ntry.find(f"{CAMT_NS}CdtDbtInd").text == "CRDT"
        tx = ntry.find(f"{CAMT_NS}NtryDtls/{CAMT_NS}TxDtls")
        assert tx.find(f"{CAMT_NS}RltdPties/{CAMT_NS}Dbtr/{CAMT_NS}Nm").text
        assert tx.find(f"{CAMT_NS}RmtInf/{CAMT_NS}Ustrd") is not None


# ------------------------------------------------------------------ no floats anywhere


DEC2 = re.compile(r"^-?\d+\.\d{2}$")


def _walk(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, node


def test_no_float_in_ground_truth(dataset):
    path = dataset / "ground_truth.json"
    assert not re.search(r":\s*-?\d+\.\d+", path.read_text()), "unquoted decimal in JSON"
    for where, value in _walk(json.loads(path.read_text())):
        assert not isinstance(value, float), where
        if where.endswith("_minor") and value is not None:
            assert isinstance(value, int), where


def test_money_columns_are_exact_two_dp_decimal_strings(dataset):
    for name in ("razorpay_settlements.csv", "invoice_ledger.csv"):
        for row in read_csv(dataset / name):
            for col in MONEY_COLUMNS & row.keys():
                raw = row[col]
                if raw == "":
                    continue
                assert DEC2.fullmatch(raw), (name, col, raw)
                assert str(Decimal(raw)) == raw


def test_settlement_rows_are_internally_consistent(dataset):
    """net credit == base_amount - fee - tax, in integer minor units -- this
    dataset has no fee_mismatch/data_entry_error-style deliberately-broken
    rows, so the identity holds for every payment row unconditionally."""
    for r in read_csv(dataset / "razorpay_settlements.csv"):
        minor = lambda c: int(Decimal(r[c]).scaleb(2))
        assert r["type"] == "payment"
        assert minor("credit") == minor("base_amount") - minor("fee") - minor("tax")
        assert r["international"] == "Y"
        rate = Decimal(r["conversion_rate"])
        assert gen.base.to_base_minor(minor("amount"), rate) == minor("base_amount")
        assert r["currency"] != "INR" and r["base_currency"] == "INR"


# ------------------------------------------------------------------ the whole point:
# Tier 1's unmodified cascade genuinely cannot resolve this dataset.


def test_tier1_cannot_resolve_the_overwhelming_majority_of_stress_cases(dataset):
    """Runs reconagent.match.match_all (via reconagent.eval.load_split, which
    calls it unmodified) against the generated stress-test files and asserts
    that almost none of the 40 credits come back MATCHED/PARTIAL. This is
    the dataset's entire value proposition, locked in as an assertion rather
    than left as something only reported and never checked.

    Observed on the committed seed: 0/40 resolve (all UNMATCHED). The bound
    below is deliberately looser than that observed 0% so a future,
    unrelated change to Tier 1's tolerance/pooling constants doesn't turn an
    incidental improvement into a spurious failure here -- the assertion is
    "Tier 1 is still overwhelmingly defeated", not "exactly zero forever".
    """
    split = load_split(dataset, "", "stress_test")
    resolutions = Counter(r.resolution for r in split.results.values())
    total = len(split.results)
    resolved = sum(resolutions[res] for res in (MATCHED, PARTIAL))

    assert total == 40
    assert resolved / total <= 0.15, (resolved, total, dict(resolutions))
