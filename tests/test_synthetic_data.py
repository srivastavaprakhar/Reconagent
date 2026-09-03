"""Correctness tests for the synthetic ground-truth generator.

These do not test the matcher (it does not exist yet). They test that the answer key
is internally consistent with the files it claims to describe, that the bank formats are
real, and that no float ever touches a money path.
"""

from __future__ import annotations

import csv
import filecmp
import importlib.util
import json
import re
import sys
import xml.etree.ElementTree as ET
from decimal import Decimal
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "generate_synthetic", REPO / "scripts" / "generate_synthetic.py"
)
gen = importlib.util.module_from_spec(_spec)
sys.modules["generate_synthetic"] = gen
_spec.loader.exec_module(gen)

SEED = 20260903
SCALE = 120
CAMT_NS = "{urn:iso:std:iso:20022:tech:xsd:camt.053.001.02}"

MONEY_COLUMNS = {"debit", "credit", "amount", "fee", "tax", "base_amount",
                 "invoice_amount", "realised_amount"}


@pytest.fixture(scope="module")
def dataset(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("data")
    gen.generate(SEED, SCALE, out)
    return out


def split_dir(root: Path, split: str) -> tuple[Path, str]:
    return (root, "") if split == "main" else (root / "holdout", "HOLDOUT_")


SPLITS = ["main", "holdout"]


def load(root: Path, split: str, name: str):
    d, prefix = split_dir(root, split)
    return d / f"{prefix}{name}"


def ground_truth(root: Path, split: str) -> dict:
    return json.loads(load(root, split, "ground_truth.json").read_text())


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# ------------------------------------------------------------------ determinism


def test_generator_is_byte_identical_under_the_same_seed(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    gen.generate(SEED, 60, a)
    gen.generate(SEED, 60, b)
    files = sorted(p.relative_to(a) for p in a.rglob("*") if p.is_file())
    assert files, "generator produced nothing"
    mismatch = [str(f) for f in files if not filecmp.cmp(a / f, b / f, shallow=False)]
    assert mismatch == []


def test_a_different_seed_produces_different_data(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    gen.generate(SEED, 60, a)
    gen.generate(SEED + 1, 60, b)
    assert not filecmp.cmp(
        a / "razorpay_settlements.csv", b / "razorpay_settlements.csv", shallow=False
    )


# ------------------------------------------------------------------ holdout hygiene


def test_holdout_is_structurally_obvious(dataset):
    hold = dataset / "holdout"
    assert (hold / "DO_NOT_TUNE_ON_THESE_FILES.txt").exists()
    assert all(p.name.startswith(("HOLDOUT_", "DO_NOT_TUNE")) for p in hold.iterdir())
    assert ground_truth(dataset, "holdout")["generator"]["adversarial_holdout"] is True
    assert ground_truth(dataset, "main")["generator"]["adversarial_holdout"] is False


def test_holdout_uses_a_different_seed_and_harder_mix(dataset):
    main, hold = ground_truth(dataset, "main"), ground_truth(dataset, "holdout")
    assert main["generator"]["seed"] != hold["generator"]["seed"]
    clean_share = lambda g: g["counts"]["by_defect_class"]["clean_match"] / g["counts"]["cases"]
    assert clean_share(hold) < clean_share(main)


def test_both_splits_share_one_schema(dataset):
    keys = [set(ground_truth(dataset, s).keys()) for s in SPLITS]
    assert keys[0] == keys[1]
    for s in SPLITS:
        for case in ground_truth(dataset, s)["cases"]:
            assert set(case) >= {
                "case_id", "defect_class", "split", "settlement_ids", "payment_ids",
                "bank_txn_ids", "invoice_ids", "expected_link", "expected_link_resolution",
                "expected_exception_category", "details", "notes",
            }


# ------------------------------------------------------------------ referential integrity


@pytest.mark.parametrize("split", SPLITS)
def test_every_case_resolves_to_records_that_exist(dataset, split):
    gt = ground_truth(dataset, split)
    rows = read_csv(load(dataset, split, "razorpay_settlements.csv"))
    invoices = read_csv(load(dataset, split, "invoice_ledger.csv"))
    tree = ET.parse(load(dataset, split, "bank_statement.camt053.xml"))

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
        if link["bank_txn_id"] is not None:
            assert link["bank_txn_id"] in bank_ids, cid


@pytest.mark.parametrize("split", SPLITS)
def test_ground_truth_counts_match_the_emitted_files(dataset, split):
    gt = ground_truth(dataset, split)
    rows = read_csv(load(dataset, split, "razorpay_settlements.csv"))
    tree = ET.parse(load(dataset, split, "bank_statement.camt053.xml"))
    assert gt["counts"]["settlement_rows"] == len(rows)
    assert gt["counts"]["settlements"] == len({r["settlement_id"] for r in rows})
    assert gt["counts"]["invoices"] == len(read_csv(load(dataset, split, "invoice_ledger.csv")))
    assert gt["counts"]["bank_credits"] == len(list(tree.iter(f"{CAMT_NS}Ntry")))
    assert gt["counts"]["cases"] == len(gt["cases"])
    assert sum(gt["counts"]["by_defect_class"].values()) == len(gt["cases"])


# ------------------------------------------------------------------ the labels are true


@pytest.mark.parametrize("split", SPLITS)
def test_expected_links_reconcile_arithmetically(dataset, split):
    """settlement_net_sum_minor really is the sum of the named settlements' nets, and
    residual really is that sum minus the credit."""
    gt = ground_truth(dataset, split)
    nets = {}
    for r in read_csv(load(dataset, split, "razorpay_settlements.csv")):
        if r["type"] == "payment":
            nets[r["settlement_id"]] = int(Decimal(r["credit"]).scaleb(2))
    credits = {}
    tree = ET.parse(load(dataset, split, "bank_statement.camt053.xml"))
    for ntry in tree.iter(f"{CAMT_NS}Ntry"):
        ref = ntry.find(f"{CAMT_NS}NtryRef").text
        credits[ref] = int(Decimal(ntry.find(f"{CAMT_NS}Amt").text).scaleb(2))

    for case in gt["cases"]:
        link = case["expected_link"]
        cid = case["case_id"]
        assert link["settlement_net_sum_minor"] == sum(
            nets[s] for s in link["covers_settlement_ids"]
        ), cid
        if link["bank_txn_id"] is None:
            assert link["credit_amount_minor"] is None, cid
            continue
        assert credits[link["bank_txn_id"]] == link["credit_amount_minor"], cid
        assert link["residual_minor"] == (
            link["settlement_net_sum_minor"] - link["credit_amount_minor"]
        ), cid


@pytest.mark.parametrize("split", SPLITS)
def test_subset_sum_bundles_sum_exactly_and_decoys_do_not(dataset, split):
    gt = ground_truth(dataset, split)
    tol = gt["conventions"]["amount_tolerance_minor"]
    nets = {}
    for r in read_csv(load(dataset, split, "razorpay_settlements.csv")):
        if r["type"] == "payment":
            nets[r["settlement_id"]] = int(Decimal(r["credit"]).scaleb(2))

    bundles = [c for c in gt["cases"] if c["defect_class"] == "subset_sum_bundle"]
    assert bundles, "no bundles generated"
    for case in bundles:
        link, det, cid = case["expected_link"], case["details"], case["case_id"]
        members = link["covers_settlement_ids"]
        assert len(members) == det["cardinality"] >= 2, cid
        assert abs(sum(nets[s] for s in members) - link["credit_amount_minor"]) <= tol, cid
        # the decoy is a genuine trap: it sums close to, but not exactly onto, the credit
        decoys = det["decoy_settlement_ids"]
        assert set(decoys).isdisjoint(members), cid
        decoy_sum = sum(nets[s] for s in decoys)
        assert decoy_sum == det["decoy_sum_minor"], cid
        assert decoy_sum != link["credit_amount_minor"], cid
        assert abs(decoy_sum - link["credit_amount_minor"]) <= tol, cid


@pytest.mark.parametrize("split", SPLITS)
def test_fx_labels_agree_with_the_reference_feed(dataset, split):
    gt = ground_truth(dataset, split)
    tol = Decimal(gt["conventions"]["fx_tolerance_bps"])
    feed = {
        (r["currency_pair"], r["value_date"]): Decimal(r["reference_rate"])
        for r in read_csv(load(dataset, split, "fx_reference_rates.csv"))
    }
    cases = [c for c in gt["cases"] if c["defect_class"].startswith("fx_drift")]
    assert cases
    for case in cases:
        d, cid = case["details"], case["case_id"]
        ref = feed[(d["currency_pair"], d["value_date"])]
        assert Decimal(d["reference_rate"]) == ref, cid
        applied = Decimal(d["applied_rate"])
        assert gen.deviation_bps(applied, ref) == Decimal(d["deviation_bps"]), cid
        within = abs(Decimal(d["deviation_bps"])) <= tol
        assert within == d["expected_within_tolerance"], cid
        assert within == (case["defect_class"] == "fx_drift_benign"), cid


@pytest.mark.parametrize("split", SPLITS)
def test_refund_fx_asymmetry_nets_to_zero_in_foreign_and_not_in_inr(dataset, split):
    gt = ground_truth(dataset, split)
    cases = [c for c in gt["cases"] if c["defect_class"] == "refund_fx_asymmetry"]
    assert cases
    for case in cases:
        d, cid = case["details"], case["case_id"]
        assert d["capture"]["foreign_minor"] == d["refund"]["foreign_minor"], cid
        assert d["foreign_residual_minor"] == 0, cid
        assert d["inr_residual_minor"] != 0, cid
        assert d["inr_residual_minor"] == (
            d["capture"]["inr_minor"] - d["refund"]["inr_minor"]
        ), cid
        assert d["capture"]["rate"] != d["refund"]["rate"], cid


@pytest.mark.parametrize("split", SPLITS)
def test_timing_pending_cases_sit_inside_the_t2_t7_window_with_no_credit(dataset, split):
    gt = ground_truth(dataset, split)
    cases = [c for c in gt["cases"] if c["defect_class"] == "timing_pending"]
    assert cases
    for case in cases:
        assert case["bank_txn_ids"] == []
        assert case["expected_link"]["bank_txn_id"] is None
        lo, hi = case["details"]["expected_window_days"]
        assert lo <= case["details"]["days_outstanding"] <= hi


@pytest.mark.parametrize("split", SPLITS)
def test_edpms_cases_carry_a_purpose_code_and_a_deadline(dataset, split):
    gt = ground_truth(dataset, split)
    invoices = {i["invoice_id"]: i for i in read_csv(load(dataset, split, "invoice_ledger.csv"))}
    cases = [c for c in gt["cases"] if c["defect_class"] == "edpms_open"]
    assert cases
    for case in cases:
        d = case["details"]
        inv = invoices[case["invoice_ids"][0]]
        assert re.fullmatch(r"P\d{4}", d["purpose_code"])
        assert inv["purpose_code"] == d["purpose_code"]
        assert inv["shipping_bill_no"] == d["shipping_bill_no"]
        assert inv["realisation_deadline"] == d["realisation_deadline"]
        assert d["outstanding_foreign_minor"] > 0


@pytest.mark.parametrize("split", SPLITS)
def test_missing_remitter_cases_actually_degrade_the_sender_name(dataset, split):
    gt = ground_truth(dataset, split)
    cases = [c for c in gt["cases"] if c["defect_class"] == "missing_remitter"]
    assert cases
    for case in cases:
        d = case["details"]
        assert d["field_50a_name_as_sent"] != d["true_remitter_name"], case["case_id"]


# ------------------------------------------------------------------ MT103 is real MT103

MT_BLOCKS = re.compile(
    r"^\{1:F01[A-Z0-9]{12}\d{4}\d{6}\}\{2:O103\d{4}\d{6}[A-Z0-9]{12}\d{4}\d{6}\d{6}\d{4}[NU]\}"
    r"(\{3:\{121:[0-9a-f-]{36}\}\})?\{4:\n(?P<body>.*)\n-\}$",
    re.DOTALL,
)
MT_FIELD_ORDER = [":20:", ":23B:", ":32A:", ":33B:", ":36:", ":50K:", ":52A:", ":57A:",
                  ":59:", ":70:", ":71A:"]


@pytest.mark.parametrize("split", SPLITS)
def test_mt103_messages_are_structurally_valid(dataset, split):
    raw = load(dataset, split, "bank_statement.mt103").read_text()
    msgs = [m for m in raw.split("\n$\n") if m.strip()]
    assert msgs, "no MT103 messages emitted"
    for msg in msgs:
        m = MT_BLOCKS.match(msg)
        assert m, msg[:120]
        body = m.group("body")
        tags = [ln for ln in body.split("\n") if ln.startswith(":")]
        seen = [t[: t.index(":", 1) + 1] for t in tags]

        # every mandated field present
        for required in (":20:", ":23B:", ":32A:", ":50K:", ":59:", ":70:", ":71A:"):
            assert required in seen, (required, seen)
        # and in the SWIFT-mandated sequence
        positions = [MT_FIELD_ORDER.index(t) for t in seen if t in MT_FIELD_ORDER]
        assert positions == sorted(positions), seen

        f32a = next(ln for ln in tags if ln.startswith(":32A:"))[5:]
        assert re.fullmatch(r"\d{6}[A-Z]{3}\d+,\d{2}", f32a), f32a
        f33b = next(ln for ln in tags if ln.startswith(":33B:"))[5:]
        assert re.fullmatch(r"[A-Z]{3}\d+,\d{2}", f33b), f33b
        assert next(ln for ln in tags if ln.startswith(":71A:"))[5:] in {"SHA", "OUR", "BEN"}
        # field 70 is 4 lines of at most 35 chars
        i = seen.index(":70:")
        start = body.split("\n").index(tags[i])
        lines = []
        for ln in body.split("\n")[start:]:
            if lines and ln.startswith(":"):
                break
            lines.append(ln[5:] if not lines else ln)
        assert 1 <= len(lines) <= 4
        assert all(len(x) <= 35 for x in lines), lines


@pytest.mark.parametrize("split", SPLITS)
def test_mt103_and_camt053_describe_the_same_credits(dataset, split):
    raw = load(dataset, split, "bank_statement.mt103").read_text()
    mt_refs = set(re.findall(r"^:20:(\S+)$", raw, re.MULTILINE))
    tree = ET.parse(load(dataset, split, "bank_statement.camt053.xml"))
    camt_refs = {e.text for e in tree.iter(f"{CAMT_NS}AcctSvcrRef")}
    assert mt_refs, "no MT103 references"
    assert mt_refs <= camt_refs, mt_refs - camt_refs

    # every cross-border camt entry has an MT103 twin, and the INR amounts agree
    mt_amounts = dict(re.findall(r"^:20:(\S+)\n:23B:CRED\n:32A:\d{6}INR([\d,]+)$", raw, re.MULTILINE))
    assert len(mt_amounts) == len(mt_refs)
    for ntry in tree.iter(f"{CAMT_NS}Ntry"):
        ref = ntry.find(f".//{CAMT_NS}AcctSvcrRef").text
        if ref in mt_amounts:
            assert mt_amounts[ref].replace(",", ".") == ntry.find(f"{CAMT_NS}Amt").text


# ------------------------------------------------------------------ camt.053 is real camt.053


@pytest.mark.parametrize("split", SPLITS)
def test_camt053_is_structurally_valid(dataset, split):
    root = ET.parse(load(dataset, split, "bank_statement.camt053.xml")).getroot()
    assert root.tag == f"{CAMT_NS}Document"
    stmt = root.find(f"{CAMT_NS}BkToCstmrStmt/{CAMT_NS}Stmt")
    assert stmt is not None
    assert stmt.find(f"{CAMT_NS}Acct/{CAMT_NS}Ccy").text == "INR"

    bals = {b.find(f".//{CAMT_NS}Cd").text: b for b in stmt.findall(f"{CAMT_NS}Bal")}
    assert set(bals) == {"OPBD", "CLBD"}

    entries = stmt.findall(f"{CAMT_NS}Ntry")
    assert entries
    total = 0
    for ntry in entries:
        amt = ntry.find(f"{CAMT_NS}Amt")
        assert amt.get("Ccy") == "INR"
        total += int(Decimal(amt.text).scaleb(2))
        assert ntry.find(f"{CAMT_NS}CdtDbtInd").text == "CRDT"
        assert ntry.find(f"{CAMT_NS}Sts").text == "BOOK"
        assert ntry.find(f"{CAMT_NS}ValDt/{CAMT_NS}Dt") is not None
        tx = ntry.find(f"{CAMT_NS}NtryDtls/{CAMT_NS}TxDtls")
        assert tx.find(f"{CAMT_NS}Refs/{CAMT_NS}EndToEndId") is not None
        assert tx.find(f"{CAMT_NS}RltdPties/{CAMT_NS}Dbtr/{CAMT_NS}Nm").text
        assert tx.find(f"{CAMT_NS}RltdPties/{CAMT_NS}Cdtr/{CAMT_NS}Nm").text
        assert tx.find(f"{CAMT_NS}RmtInf/{CAMT_NS}Ustrd") is not None

    opbd = int(Decimal(bals["OPBD"].find(f"{CAMT_NS}Amt").text).scaleb(2))
    clbd = int(Decimal(bals["CLBD"].find(f"{CAMT_NS}Amt").text).scaleb(2))
    assert clbd - opbd == total, "closing balance does not tie to the entries"
    summary = stmt.find(f"{CAMT_NS}TxsSummry/{CAMT_NS}TtlCdtNtries")
    assert int(summary.find(f"{CAMT_NS}NbOfNtries").text) == len(entries)
    assert int(Decimal(summary.find(f"{CAMT_NS}Sum").text).scaleb(2)) == total


@pytest.mark.parametrize("split", SPLITS)
def test_cross_border_camt_entries_carry_the_fx_details(dataset, split):
    tree = ET.parse(load(dataset, split, "bank_statement.camt053.xml"))
    xchg = list(tree.iter(f"{CAMT_NS}CcyXchg"))
    assert xchg, "no cross-border entries carry CcyXchg"
    for cx in xchg:
        assert cx.find(f"{CAMT_NS}TrgtCcy").text == "INR"
        assert cx.find(f"{CAMT_NS}SrcCcy").text in gen.FX_BASE
        Decimal(cx.find(f"{CAMT_NS}XchgRate").text)  # parses exactly, no float


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


@pytest.mark.parametrize("split", SPLITS)
def test_no_float_in_ground_truth(dataset, split):
    path = load(dataset, split, "ground_truth.json")
    # a float would have to appear in the raw text as an unquoted decimal
    assert not re.search(r":\s*-?\d+\.\d+", path.read_text()), "unquoted decimal in JSON"
    for where, value in _walk(json.loads(path.read_text())):
        assert not isinstance(value, float), where
        if where.endswith("_minor") and value is not None:
            assert isinstance(value, int), where


@pytest.mark.parametrize("split", SPLITS)
def test_money_columns_are_exact_two_dp_decimal_strings(dataset, split):
    for name in ("razorpay_settlements.csv", "invoice_ledger.csv"):
        for row in read_csv(load(dataset, split, name)):
            for col in MONEY_COLUMNS & row.keys():
                raw = row[col]
                if raw == "":
                    continue
                assert DEC2.fullmatch(raw), (name, col, raw)
                assert str(Decimal(raw)) == raw
                assert Decimal(raw).scaleb(2) == int(Decimal(raw).scaleb(2))


@pytest.mark.parametrize("split", SPLITS)
def test_settlement_rows_are_internally_consistent(dataset, split):
    """net credit == base_amount - fee - tax, in integer minor units."""
    for r in read_csv(load(dataset, split, "razorpay_settlements.csv")):
        minor = lambda c: int(Decimal(r[c]).scaleb(2))
        if r["type"] == "payment":
            assert minor("credit") == minor("base_amount") - minor("fee") - minor("tax")
            if r["international"] == "Y":
                rate = Decimal(r["conversion_rate"])
                assert gen.to_base_minor(minor("amount"), rate) == minor("base_amount")
                assert r["currency"] != "INR" and r["base_currency"] == "INR"
            else:
                assert r["conversion_rate"] == ""
                assert minor("amount") == minor("base_amount")
        else:
            assert r["type"] == "refund" and minor("credit") == 0 and minor("debit") > 0


@pytest.mark.parametrize("split", SPLITS)
def test_fx_reference_feed_covers_every_case_value_date(dataset, split):
    gt = ground_truth(dataset, split)
    feed = {
        (r["currency_pair"], r["value_date"])
        for r in read_csv(load(dataset, split, "fx_reference_rates.csv"))
    }
    assert feed
    for case in gt["cases"]:
        d = case["details"]
        if "currency_pair" in d:
            assert (d["currency_pair"], d["value_date"]) in feed, case["case_id"]
