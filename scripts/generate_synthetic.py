#!/usr/bin/env python3
"""Synthetic ground-truth generator for the three-way reconciliation engine.

Emits, per split:
  - a Razorpay settlement recon export (CSV, Razorpay's real column set)
  - a bank statement as real SWIFT MT103 text (cross-border credits only)
  - a bank statement as real ISO 20022 camt.053 XML (every credit)
  - the merchant's invoice/order ledger (CSV)
  - an FBIL daily reference-rate feed (CSV) for the FX validator to benchmark against
  - ground_truth.json: the answer key, labelling every case

Money is integer minor units end to end. It is converted to a fixed-point decimal
string only at the serialization boundary, via Decimal.scaleb(-2), which is exact.
No float is constructed anywhere on a money path.

Deterministic: all randomness comes from a single seeded random.Random, and no value
is derived from the wall clock, so a rerun at the same --seed/--scale is byte-identical.

Run:  .venv/bin/python scripts/generate_synthetic.py --seed 20260903 --scale 200
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import string
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ALNUM = string.ascii_lowercase + string.ascii_uppercase + string.digits

# --- fixed calendar -----------------------------------------------------------------
# Rationale: a statement always covers a bounded period. Fixing it (rather than deriving
# from today) is what makes reruns byte-identical, and it gives the timing/EDPMS cases a
# stable "as of" date to age against.
PERIOD_START = date(2026, 8, 1)
PERIOD_END = date(2026, 8, 31)
STATEMENT_AS_OF = PERIOD_END

# --- money / fees -------------------------------------------------------------------
MINOR_EXP = 2  # every currency used here is 2-dp; JPY-style 0-dp is deliberately excluded
MDR_BPS_DOMESTIC = 200
MDR_BPS_INTERNATIONAL = 300
GST_PCT_ON_FEE = 18
OPENING_BALANCE_MINOR = 100_000_000  # INR 10,00,000.00

# Tolerance used *for labelling only*. The matcher and the FX validator own their own
# bands (spec: "the tolerance band itself must be a parameter the validator owns").
# These are published in ground_truth.conventions so a grader can see what the label means.
LABEL_AMOUNT_TOLERANCE_MINOR = 100  # INR 1.00
LABEL_FX_TOLERANCE_BPS = Decimal("50")

# --- parties ------------------------------------------------------------------------
MERCHANT_NAME = "GLOBEX EXPORTS PRIVATE LIMITED"
MERCHANT_ACCOUNT = "50200087654321"
MERCHANT_IBAN_OTHR = "50200087654321"
MERCHANT_BIC = "ICICINBBXXX"
MERCHANT_LT = "ICICINBBAXXX"
RAZORPAY_BANK_BIC = "RATNINBBXXX"
# The remitting bank follows the debtor's country -- a GBP remittance arriving via an
# Emirates NBD BIC is the kind of detail a payments-literate reader notices.
SENDER_BANKS = {
    "DE": ("DEUTDEFFXXX", "DEUTDEFFAXXX", "DEUTSCHE BANK AG"),
    "US": ("CHASUS33XXX", "CHASUS33AXXX", "JPMORGAN CHASE BANK NA"),
    "GB": ("BARCGB22XXX", "BARCGB22AXXX", "BARCLAYS BANK PLC"),
    "SG": ("DBSSSGSGXXX", "DBSSSGSGAXXX", "DBS BANK LTD"),
    "AE": ("EBILAEADXXX", "EBILAEADAXXX", "EMIRATES NBD BANK PJSC"),
    "NL": ("INGBNL2AXXX", "INGBNL2AAXXX", "ING BANK NV"),
}

FOREIGN_BUYERS = [
    ("ACME TRADING GMBH", "FRIEDRICHSTRASSE 12", "BERLIN", "DE", "EUR"),
    ("NORTHWIND SOFTWARE INC", "500 MARKET STREET", "SAN FRANCISCO CA", "US", "USD"),
    ("BRIDGEWORK ANALYTICS LLC", "1200 BROADWAY SUITE 4", "NEW YORK NY", "US", "USD"),
    ("KESTREL SYSTEMS LIMITED", "18 FENCHURCH AVENUE", "LONDON", "GB", "GBP"),
    ("MERIDIAN PTE LTD", "8 MARINA VIEW", "SINGAPORE", "SG", "SGD"),
    ("AL MAHA GENERAL TRADING LLC", "SHEIKH ZAYED ROAD", "DUBAI", "AE", "AED"),
    ("HELIOS DATA BV", "KEIZERSGRACHT 241", "AMSTERDAM", "NL", "EUR"),
    ("CASCADE RETAIL CORP", "77 PIONEER SQUARE", "SEATTLE WA", "US", "USD"),
]

DOMESTIC_BUYERS = [
    ("Sharma Retail LLP", "IN"),
    ("Kanpur Textiles Pvt Ltd", "IN"),
    ("Bluewave Logistics", "IN"),
    ("Sunrise Foods India", "IN"),
    ("Deccan Hardware Co", "IN"),
]

# RBI purpose codes actually used for export receipts.
PURPOSE_CODES = ["P0802", "P0801", "P1006", "P0103", "P0805"]

# FBIL publishes a daily INR reference rate per currency. Base levels are plausible
# 2026 levels; the walk below is what gives per-day variation.
FX_BASE = {
    "USD": Decimal("87.4000"),
    "EUR": Decimal("95.2000"),
    "GBP": Decimal("111.5000"),
    "SGD": Decimal("64.8000"),
    "AED": Decimal("23.8000"),
}

RATE_Q = Decimal("0.0001")
BPS_Q = Decimal("0.01")

# --- defect mix ---------------------------------------------------------------------
# Clean matches dominate, as they do in production: a real settlement file is boring.
# Cross-border defects are over-represented relative to a real Indian merchant's book
# because they are what this engine exists to handle -- stated here rather than implied.
WEIGHTS_MAIN = {
    "clean_match": 70,
    "subset_sum_bundle": 8,
    "fx_drift_benign": 6,
    "fx_drift_flagged": 4,
    "missing_remitter": 4,
    "partial_payment": 3,
    "refund_fx_asymmetry": 2,
    "timing_pending": 2,
    "edpms_open": 1,
}

# The holdout is deliberately meaner: fewer freebies, and every defect knob turned to
# its nastiest setting (see harden() below). It exists to be evaluated against, never
# tuned against.
WEIGHTS_HOLDOUT = {
    "clean_match": 35,
    "subset_sum_bundle": 15,
    "fx_drift_benign": 10,
    "fx_drift_flagged": 10,
    "missing_remitter": 12,
    "partial_payment": 7,
    "refund_fx_asymmetry": 5,
    "timing_pending": 4,
    "edpms_open": 2,
}


# ====================================================================================
# money helpers -- integer minor units in, exact decimal strings out
# ====================================================================================


def money_str(minor: int) -> str:
    """Exact fixed-point string for an integer minor-unit amount. Never a float."""
    return str(Decimal(minor).scaleb(-MINOR_EXP))


def money_swift(minor: int) -> str:
    """SWIFT amount format: comma as the decimal separator, no thousands separator."""
    return money_str(minor).replace(".", ",")


def to_base_minor(amount_minor: int, rate: Decimal) -> int:
    """Convert foreign minor units to INR minor units at `rate` (INR per 1 foreign unit)."""
    return int((Decimal(amount_minor) * rate).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def pct_minor(minor: int, numerator: int, denominator: int) -> int:
    return int(
        (Decimal(minor) * numerator / denominator).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    )


def fees_for(base_minor: int, international: bool) -> tuple[int, int]:
    bps = MDR_BPS_INTERNATIONAL if international else MDR_BPS_DOMESTIC
    fee = pct_minor(base_minor, bps, 10_000)
    tax = pct_minor(fee, GST_PCT_ON_FEE, 100)
    return fee, tax


def deviation_bps(applied: Decimal, reference: Decimal) -> Decimal:
    return ((applied - reference) / reference * 10_000).quantize(BPS_Q, rounding=ROUND_HALF_UP)


# ====================================================================================
# id helpers
# ====================================================================================


def rid(rng: random.Random, prefix: str, n: int = 14) -> str:
    return prefix + "".join(rng.choices(ALNUM, k=n))


def utr(rng: random.Random) -> str:
    return "".join(rng.choices(string.ascii_uppercase + string.digits, k=16))


def hexs(rng: random.Random, n: int) -> str:
    return "".join(rng.choices("0123456789abcdef", k=n))


# ====================================================================================
# the world: a mutable bag of records the case builders append to
# ====================================================================================


class World:
    def __init__(self, rng: random.Random, split: str, harden: bool):
        self.rng = rng
        self.split = split
        self.harden = harden
        self.tag = "H" if harden else "M"
        self.settlement_rows: list[dict] = []  # one row per payment/refund txn
        self.settlements: dict[str, dict] = {}  # settlement_id -> summary
        self.invoices: list[dict] = []
        self.credits: list[dict] = []  # bank statement entries
        self.cases: list[dict] = []
        self.fx: dict[tuple[str, str], Decimal] = {}

    # -- fx feed ---------------------------------------------------------------------
    def build_fx(self) -> None:
        """FBIL daily reference rate per currency, as a seeded random walk."""
        for ccy, base in FX_BASE.items():
            rate = base
            d = PERIOD_START - timedelta(days=10)
            while d <= PERIOD_END + timedelta(days=10):
                step = Decimal(self.rng.randint(-150, 150)).scaleb(-4)
                rate = (rate + step).quantize(RATE_Q)
                self.fx[(ccy, d.isoformat())] = rate
                d += timedelta(days=1)

    def ref_rate(self, ccy: str, value_date: date) -> Decimal:
        return self.fx[(ccy, value_date.isoformat())]

    # -- record constructors ---------------------------------------------------------
    def add_invoice(
        self,
        *,
        currency: str,
        amount_minor: int,
        issue_date: date,
        customer: str,
        country: str,
        export: bool,
        realised_minor: int | None = None,
    ) -> dict:
        rng = self.rng
        inv = {
            "invoice_id": f"INV-2026-{self.tag}{len(self.invoices) + 1:05d}",
            "invoice_date": issue_date.isoformat(),
            "order_id": rid(rng, "order_"),
            "customer_name": customer,
            "customer_country": country,
            "currency": currency,
            "invoice_amount": money_str(amount_minor),
            "invoice_amount_minor": amount_minor,
            "invoice_status": "ISSUED",
            "purpose_code": rng.choice(PURPOSE_CODES) if export else "",
            "shipping_bill_no": "",
            "shipping_bill_date": "",
            "realisation_deadline": "",
            "realised_amount": money_str(realised_minor) if realised_minor is not None else "",
            "notes": "",
        }
        if export:
            # FEMA: export proceeds must be realised within 9 months of shipment.
            sb_date = issue_date - timedelta(days=rng.randint(1, 20))
            inv["shipping_bill_no"] = f"{rng.randint(1000000, 9999999)}"
            inv["shipping_bill_date"] = sb_date.isoformat()
            inv["realisation_deadline"] = (sb_date + timedelta(days=270)).isoformat()
        self.invoices.append(inv)
        return inv

    def add_settlement(
        self,
        *,
        invoice: dict,
        gross_minor: int,
        settled_at: date,
        international: bool,
        conversion_rate: Decimal | None = None,
        base_minor: int | None = None,
        method: str = "card",
        txn_type: str = "payment",
    ) -> dict:
        """One settled payment == one settlement. Returns the settlement summary."""
        rng = self.rng
        if international:
            assert conversion_rate is not None and base_minor is not None
        else:
            base_minor = gross_minor
        fee, tax = fees_for(base_minor, international)
        net = base_minor - fee - tax
        sid = rid(rng, "setl_")
        pid = rid(rng, "pay_")
        row = {
            "entity_id": pid,
            "type": txn_type,
            "debit": money_str(0),
            "credit": money_str(net),
            "amount": money_str(gross_minor),
            "currency": invoice["currency"],
            "fee": money_str(fee),
            "tax": money_str(tax),
            "on_hold": "N",
            "settled": "Y",
            "created_at": (settled_at - timedelta(days=2)).isoformat(),
            "settled_at": settled_at.isoformat(),
            "settlement_id": sid,
            "settlement_utr": utr(rng),
            "description": f"Payment for {invoice['invoice_id']}",
            "notes": invoice["invoice_id"],
            "payment_id": pid,
            "order_id": invoice["order_id"],
            "order_receipt": invoice["invoice_id"],
            "method": method,
            "international": "Y" if international else "N",
            "conversion_rate": str(conversion_rate) if international else "",
            "base_amount": money_str(base_minor),
            "base_currency": "INR",
            "refund_id": "",
        }
        self.settlement_rows.append(row)
        summary = {
            "settlement_id": sid,
            "payment_id": pid,
            "utr": row["settlement_utr"],
            "settled_at": settled_at,
            "net_minor": net,
            "base_minor": base_minor,
            "fee_minor": fee,
            "tax_minor": tax,
            "international": international,
            "invoice_id": invoice["invoice_id"],
        }
        self.settlements[sid] = summary
        return summary

    def add_refund_row(
        self, *, invoice: dict, settlement: dict, refund_minor: int, refund_base_minor: int,
        refund_rate: Decimal, settled_at: date,
    ) -> str:
        rng = self.rng
        rfnd = rid(rng, "rfnd_")
        self.settlement_rows.append(
            {
                "entity_id": rfnd,
                "type": "refund",
                "debit": money_str(refund_base_minor),
                "credit": money_str(0),
                "amount": money_str(refund_minor),
                "currency": invoice["currency"],
                "fee": money_str(0),
                "tax": money_str(0),
                "on_hold": "N",
                "settled": "Y",
                "created_at": (settled_at - timedelta(days=1)).isoformat(),
                "settled_at": settled_at.isoformat(),
                "settlement_id": settlement["settlement_id"],
                "settlement_utr": settlement["utr"],
                "description": f"Refund against {invoice['invoice_id']}",
                "notes": invoice["invoice_id"],
                "payment_id": settlement["payment_id"],
                "order_id": invoice["order_id"],
                "order_receipt": invoice["invoice_id"],
                "method": "card",
                "international": "Y",
                "conversion_rate": str(refund_rate),
                "base_amount": money_str(refund_base_minor),
                "base_currency": "INR",
                "refund_id": rfnd,
            }
        )
        return rfnd

    def add_credit(
        self,
        *,
        value_date: date,
        inr_minor: int,
        narration: str,
        debtor_name: str,
        swift: bool,
        debtor_addr: tuple[str, str, str] | None = None,
        debtor_account: str = "",
        instructed_ccy: str | None = None,
        instructed_minor: int | None = None,
        exchange_rate: Decimal | None = None,
        purpose_code: str = "",
        charge_details: str = "SHA",
    ) -> dict:
        country = debtor_addr[2] if debtor_addr else "IN"
        bic, lt, bank_name = (
            SENDER_BANKS[country]
            if swift
            else (RAZORPAY_BANK_BIC, MERCHANT_LT, "RATNAKAR BANK LIMITED")
        )
        rng = self.rng
        cr = {
            "bank_txn_id": f"BNK{self.tag}{len(self.credits) + 1:06d}",
            "value_date": value_date,
            "booking_date": value_date,
            "amount_minor": inr_minor,
            "currency": "INR",
            "narration": narration,
            "debtor_name": debtor_name,
            "debtor_addr": debtor_addr,
            "debtor_account": debtor_account,
            "channel": "SWIFT_MT103" if swift else "DOMESTIC_NEFT",
            "instructed_ccy": instructed_ccy,
            "instructed_minor": instructed_minor,
            "exchange_rate": exchange_rate,
            "purpose_code": purpose_code,
            "charge_details": charge_details,
            "sender_bic": bic,
            "sender_lt": lt,
            "sender_bank": bank_name,
            "sender_ref": "FT" + "".join(rng.choices(string.digits, k=12)),
            "uetr": f"{hexs(rng, 8)}-{hexs(rng, 4)}-4{hexs(rng, 3)}-a{hexs(rng, 3)}-{hexs(rng, 12)}",
        }
        self.credits.append(cr)
        return cr

    def add_case(self, defect_class: str, **kw) -> dict:
        case = {
            "case_id": f"{self.split.upper()}-{len(self.cases) + 1:05d}",
            "defect_class": defect_class,
            "split": self.split,
            **kw,
        }
        self.cases.append(case)
        return case


# ====================================================================================
# narration -- the field 70 / RmtInf text the fuzzy matcher will actually chew on
# ====================================================================================


def clean_narration(invoice: dict, utr_val: str, domestic: bool) -> str:
    if domestic:
        return f"NEFT CR-RATN0000088-RAZORPAY SOFTWARE PVT LTD-{utr_val}-{invoice['invoice_id']}"
    return f"/INV/{invoice['invoice_id']}/RFB/EXPORT PROCEEDS {invoice['customer_name']}"


def mangle(rng: random.Random, text: str, hard: bool) -> str:
    """Simulate the ways remitter text degrades through SWIFT and manual keying."""
    ops = ["truncate", "squash", "drop_vowels", "abbrev", "noise", "reorder"]
    n = 3 if hard else 1
    chosen = rng.sample(ops, k=min(n, len(ops)))
    out = text
    for op in chosen:
        if op == "truncate":
            out = out[: max(12, len(out) - rng.randint(8, 20))]
        elif op == "squash":
            out = out.replace(" ", "")
        elif op == "drop_vowels":
            head, _, tail = out.partition(" ")
            out = head + " " + "".join(c for c in tail if c.upper() not in "AEIOU")
        elif op == "abbrev":
            out = (
                out.replace("PRIVATE", "PVT")
                .replace("LIMITED", "LTD")
                .replace("CORPORATION", "CORP")
                .replace("INCORPORATED", "INC")
            )
        elif op == "noise":
            out = out + " " + "".join(rng.choices(string.digits, k=6))
        elif op == "reorder":
            parts = out.split()
            if len(parts) > 2:
                rng.shuffle(parts)
                out = " ".join(parts)
    return out.strip().upper()[:140]


# ====================================================================================
# case builders -- one function per defect class, dispatched by name
# ====================================================================================


def _pick_foreign(w: World):
    return FOREIGN_BUYERS[w.rng.randrange(len(FOREIGN_BUYERS))]


def _pick_domestic(w: World):
    return DOMESTIC_BUYERS[w.rng.randrange(len(DOMESTIC_BUYERS))]


def _settle_date(w: World, lo: int = 3, hi: int = 26) -> date:
    return PERIOD_START + timedelta(days=w.rng.randint(lo, hi))


def _domestic_leg(w: World, *, amount_minor: int | None = None):
    cust, country = _pick_domestic(w)
    d = _settle_date(w)
    amount = amount_minor if amount_minor is not None else w.rng.randrange(50_000, 40_00_000)
    inv = w.add_invoice(
        currency="INR", amount_minor=amount, issue_date=d - timedelta(days=w.rng.randint(1, 12)),
        customer=cust, country=country, export=False,
    )
    st = w.add_settlement(
        invoice=inv, gross_minor=amount, settled_at=d, international=False,
        method=w.rng.choice(["card", "upi", "netbanking"]),
    )
    return inv, st, d


def _intl_leg(w: World, *, rate_dev_bps: Decimal, export: bool = True):
    """An international Razorpay payment settled in INR at `applied = ref*(1+dev)`."""
    name, addr, city, country, ccy = _pick_foreign(w)
    d = _settle_date(w)
    foreign_minor = w.rng.randrange(80_000, 60_00_000)
    ref = w.ref_rate(ccy, d)
    applied = (ref * (Decimal(1) + rate_dev_bps / 10_000)).quantize(RATE_Q, rounding=ROUND_HALF_UP)
    base = to_base_minor(foreign_minor, applied)
    inv = w.add_invoice(
        currency=ccy, amount_minor=foreign_minor,
        issue_date=d - timedelta(days=w.rng.randint(2, 15)),
        customer=name, country=country, export=export,
    )
    st = w.add_settlement(
        invoice=inv, gross_minor=foreign_minor, settled_at=d, international=True,
        conversion_rate=applied, base_minor=base,
    )
    return inv, st, d, ccy, ref, applied, (name, addr, city, country)


def _swift_credit(w: World, inv, st, d, ccy, applied, party, *, narration=None, debtor_name=None,
                  debtor_account=None, inr_minor=None):
    name, addr, city, country = party
    return w.add_credit(
        value_date=d,
        inr_minor=st["net_minor"] if inr_minor is None else inr_minor,
        narration=clean_narration(inv, st["utr"], domestic=False) if narration is None else narration,
        debtor_name=name if debtor_name is None else debtor_name,
        debtor_addr=(addr, city, country),
        debtor_account=(f"{country}{w.rng.randrange(10**16, 10**17)}" if debtor_account is None
                        else debtor_account),
        swift=True,
        instructed_ccy=ccy,
        instructed_minor=inv["invoice_amount_minor"],
        exchange_rate=applied,
        purpose_code=inv["purpose_code"],
    )


def case_clean_match(w: World) -> None:
    inv, st, d = _domestic_leg(w)
    cr = w.add_credit(
        value_date=d, inr_minor=st["net_minor"],
        narration=clean_narration(inv, st["utr"], domestic=True),
        debtor_name="RAZORPAY SOFTWARE PVT LTD", swift=False,
        debtor_account="Razorpay Settlement Account",
    )
    w.add_case(
        "clean_match",
        settlement_ids=[st["settlement_id"]], payment_ids=[st["payment_id"]],
        bank_txn_ids=[cr["bank_txn_id"]], invoice_ids=[inv["invoice_id"]],
        expected_link={
            "bank_txn_id": cr["bank_txn_id"],
            "covers_settlement_ids": [st["settlement_id"]],
            "credit_amount_minor": cr["amount_minor"],
            "credit_currency": "INR",
            "settlement_net_sum_minor": st["net_minor"],
            "residual_minor": 0,
        },
        expected_link_resolution="MATCHED",
        expected_exception_category=None,
        details={"utr_present_in_narration": True},
        notes="Single settlement, single credit, UTR quoted verbatim in the narration.",
    )


def case_subset_sum_bundle(w: World) -> None:
    """One bank credit sweeps k settlements. Plus a decoy subset that lands `delta`
    minor units away, so a solver with sloppy tolerance picks the wrong set."""
    rng = w.rng
    k = rng.randint(4, 7) if w.harden else rng.randint(2, 4)
    members = []
    for _ in range(k):
        inv, st, _ = _domestic_leg(w)
        members.append(st)
    bundle_sum = sum(m["net_minor"] for m in members)
    d = max(m["settled_at"] for m in members) + timedelta(days=1)

    # Decoy: two open settlements whose nets sum to bundle_sum +/- delta.
    delta = 1 if w.harden else 3
    delta = delta * rng.choice([1, -1])
    target = bundle_sum + delta
    # Work backwards from a chosen net to the gross that produces it, so the decoy rows
    # are internally consistent (net == base - fee - tax) rather than fabricated.
    decoys = []
    split_a = target // 2
    for want_net in (split_a, target - split_a):
        gross = _gross_for_net(want_net, international=False)
        inv, st, _ = _domestic_leg(w, amount_minor=gross)
        decoys.append(st)
    decoy_sum = sum(x["net_minor"] for x in decoys)

    cr = w.add_credit(
        value_date=d, inr_minor=bundle_sum,
        narration=(
            "NEFT CR-RATN0000088-RAZORPAY SOFTWARE PVT LTD-"
            f"{utr(rng)}-CONSOLIDATED SETTLEMENT {len(members)} TXN"
        ),
        debtor_name="RAZORPAY SOFTWARE PVT LTD", swift=False,
        debtor_account="Razorpay Settlement Account",
    )
    w.add_case(
        "subset_sum_bundle",
        settlement_ids=[m["settlement_id"] for m in members] + [x["settlement_id"] for x in decoys],
        payment_ids=[m["payment_id"] for m in members] + [x["payment_id"] for x in decoys],
        bank_txn_ids=[cr["bank_txn_id"]],
        invoice_ids=[m["invoice_id"] for m in members] + [x["invoice_id"] for x in decoys],
        expected_link={
            "bank_txn_id": cr["bank_txn_id"],
            "covers_settlement_ids": [m["settlement_id"] for m in members],
            "credit_amount_minor": cr["amount_minor"],
            "credit_currency": "INR",
            "settlement_net_sum_minor": bundle_sum,
            "residual_minor": 0,
        },
        expected_link_resolution="MATCHED",
        expected_exception_category=None,
        details={
            "cardinality": k,
            "member_net_minor": {m["settlement_id"]: m["net_minor"] for m in members},
            "decoy_settlement_ids": [x["settlement_id"] for x in decoys],
            "decoy_sum_minor": decoy_sum,
            "decoy_delta_minor": decoy_sum - bundle_sum,
            "expected_unmatched_settlement_ids": [x["settlement_id"] for x in decoys],
            "no_settlement_utr_in_narration": True,
        },
        notes=(
            f"One credit sweeps {k} settlements. A decoy pair sums to "
            f"{decoy_sum - bundle_sum:+d} minor units off the credit; a solver whose "
            "tolerance exceeds that will select the decoy."
        ),
    )


def _gross_for_net(want_net: int, *, international: bool) -> int:
    """Smallest gross whose net (gross - fee - GST) is >= want_net, then exact-fit.

    Fees are integer-rounded, so the map gross->net is monotone but not surjective.
    Search a tiny neighbourhood for an exact hit; fall back to the closest above.
    """
    bps = MDR_BPS_INTERNATIONAL if international else MDR_BPS_DOMESTIC
    # net ~= gross * (1 - bps/1e4 * 1.18); invert to seed the search.
    factor = Decimal(1) - Decimal(bps) / 10_000 * Decimal(118) / 100
    seed = int((Decimal(want_net) / factor).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    for g in range(seed - 4, seed + 6):
        fee, tax = fees_for(g, international)
        if g - fee - tax == want_net:
            return g
    return seed


def case_fx_drift_benign(w: World) -> None:
    _fx_case(w, benign=True)


def case_fx_drift_flagged(w: World) -> None:
    _fx_case(w, benign=False)


def _fx_case(w: World, *, benign: bool) -> None:
    rng = w.rng
    if benign:
        dev = Decimal(rng.randint(-4800, 4800) if w.harden else rng.randint(-4400, 4400)).scaleb(-2)
    else:
        dev = Decimal(
            rng.choice([1, -1]) * (rng.randint(5500, 9000) if w.harden else rng.randint(15000, 40000))
        ).scaleb(-2)
    inv, st, d, ccy, ref, applied, party = _intl_leg(w, rate_dev_bps=dev)
    actual_dev = deviation_bps(applied, ref)
    within = abs(actual_dev) <= LABEL_FX_TOLERANCE_BPS
    assert within == benign, f"fx label drift: dev={actual_dev} benign={benign}"
    cr = _swift_credit(w, inv, st, d, ccy, applied, party)
    w.add_case(
        "fx_drift_benign" if benign else "fx_drift_flagged",
        settlement_ids=[st["settlement_id"]], payment_ids=[st["payment_id"]],
        bank_txn_ids=[cr["bank_txn_id"]], invoice_ids=[inv["invoice_id"]],
        expected_link={
            "bank_txn_id": cr["bank_txn_id"],
            "covers_settlement_ids": [st["settlement_id"]],
            "credit_amount_minor": cr["amount_minor"],
            "credit_currency": "INR",
            "settlement_net_sum_minor": st["net_minor"],
            "residual_minor": 0,
        },
        expected_link_resolution="MATCHED",
        expected_exception_category="BENIGN_FX_DRIFT" if benign else "FLAGGED_FX_DRIFT",
        details={
            "currency_pair": f"{ccy}/INR",
            "value_date": d.isoformat(),
            "applied_rate": str(applied),
            "reference_rate": str(ref),
            "reference_source": "FBIL",
            "deviation_bps": str(actual_dev),
            "expected_within_tolerance": within,
            "labelling_tolerance_bps": str(LABEL_FX_TOLERANCE_BPS),
            "gross_foreign_minor": inv["invoice_amount_minor"],
            "base_amount_minor": st["base_minor"],
            "fee_minor": st["fee_minor"],
            "tax_minor": st["tax_minor"],
        },
        notes=(
            "Applied conversion rate sits "
            f"{actual_dev} bps from the FBIL reference for the value date; "
            + ("inside" if within else "outside")
            + " the labelling band. The validator supplies its own band."
        ),
    )


def case_missing_remitter(w: World) -> None:
    rng = w.rng
    dev = Decimal(rng.randint(-2000, 2000)).scaleb(-2)
    inv, st, d, ccy, ref, applied, party = _intl_leg(w, rate_dev_bps=dev)
    name = party[0]
    if w.harden:
        style = rng.choice(["mangled", "mangled", "initials"])
    else:
        style = rng.choice(["notprovided", "mangled"])
    if style == "notprovided":
        shown_name, shown_acct = "NOT PROVIDED", "/NOTPROVIDED"
        narration = f"/RFB/EXPORT PROCEEDS INV {inv['invoice_id'][-5:]}"
    elif style == "initials":
        shown_name = "".join(p[0] for p in name.split())
        shown_acct = "/NOTPROVIDED"
        narration = mangle(rng, f"RFB {inv['invoice_id']}", hard=True)
    else:
        shown_name = mangle(rng, name, hard=w.harden)
        shown_acct = f"/{party[3]}{rng.randrange(10**16, 10**17)}"
        narration = mangle(rng, clean_narration(inv, st["utr"], domestic=False), hard=w.harden)
    cr = _swift_credit(
        w, inv, st, d, ccy, applied, party,
        narration=narration, debtor_name=shown_name, debtor_account=shown_acct.lstrip("/"),
    )
    w.add_case(
        "missing_remitter",
        settlement_ids=[st["settlement_id"]], payment_ids=[st["payment_id"]],
        bank_txn_ids=[cr["bank_txn_id"]], invoice_ids=[inv["invoice_id"]],
        expected_link={
            "bank_txn_id": cr["bank_txn_id"],
            "covers_settlement_ids": [st["settlement_id"]],
            "credit_amount_minor": cr["amount_minor"],
            "credit_currency": "INR",
            "settlement_net_sum_minor": st["net_minor"],
            "residual_minor": 0,
        },
        expected_link_resolution="MATCHED",
        expected_exception_category="MISSING_SENDER_INFO",
        details={
            "mangling_style": style,
            "true_remitter_name": name,
            "field_50a_name_as_sent": shown_name,
            "field_70_as_sent": narration,
            "resolvable_by": ["amount", "value_date", "fuzzy_name"],
        },
        notes="Ordering-customer information absent or degraded in transit; amount and value date still identify it.",
    )


def case_partial_payment(w: World) -> None:
    rng = w.rng
    inv, st, d = _domestic_leg(w)
    cov_bps = rng.randint(9700, 9900) if w.harden else rng.randint(4000, 8000)
    paid = pct_minor(st["net_minor"], cov_bps, 10_000)
    cr = w.add_credit(
        value_date=d, inr_minor=paid,
        narration=f"NEFT CR-RATN0000088-RAZORPAY SOFTWARE PVT LTD-{st['utr']}-PART SETTLEMENT",
        debtor_name="RAZORPAY SOFTWARE PVT LTD", swift=False,
        debtor_account="Razorpay Settlement Account",
    )
    w.add_case(
        "partial_payment",
        settlement_ids=[st["settlement_id"]], payment_ids=[st["payment_id"]],
        bank_txn_ids=[cr["bank_txn_id"]], invoice_ids=[inv["invoice_id"]],
        expected_link={
            "bank_txn_id": cr["bank_txn_id"],
            "covers_settlement_ids": [st["settlement_id"]],
            "credit_amount_minor": paid,
            "credit_currency": "INR",
            "settlement_net_sum_minor": st["net_minor"],
            "residual_minor": st["net_minor"] - paid,
        },
        expected_link_resolution="PARTIAL",
        expected_exception_category="PARTIAL_PAYMENT",
        details={
            "settlement_net_minor": st["net_minor"],
            "credit_amount_minor": paid,
            "shortfall_minor": st["net_minor"] - paid,
            "coverage_bps": cov_bps,
        },
        notes="Credit covers only part of the settlement; the remainder stays open.",
    )


def case_refund_fx_asymmetry(w: World) -> None:
    """A full refund converts at its own FX event, so INR does not net to zero."""
    rng = w.rng
    dev = Decimal(rng.randint(-2500, 2500)).scaleb(-2)
    inv, st, d, ccy, ref, applied, party = _intl_leg(w, rate_dev_bps=dev)
    refund_date = d + timedelta(days=rng.randint(2, 5))
    refund_ref = w.ref_rate(ccy, refund_date)
    refund_dev = Decimal(rng.choice([1, -1]) * rng.randint(1500, 6000)).scaleb(-2)
    refund_rate = (refund_ref * (Decimal(1) + refund_dev / 10_000)).quantize(
        RATE_Q, rounding=ROUND_HALF_UP
    )
    foreign = inv["invoice_amount_minor"]
    refund_base = to_base_minor(foreign, refund_rate)
    rfnd = w.add_refund_row(
        invoice=inv, settlement=st, refund_minor=foreign, refund_base_minor=refund_base,
        refund_rate=refund_rate, settled_at=refund_date,
    )
    residual = st["base_minor"] - refund_base
    cr = _swift_credit(w, inv, st, d, ccy, applied, party)
    w.add_case(
        "refund_fx_asymmetry",
        settlement_ids=[st["settlement_id"]], payment_ids=[st["payment_id"], rfnd],
        bank_txn_ids=[cr["bank_txn_id"]], invoice_ids=[inv["invoice_id"]],
        expected_link={
            "bank_txn_id": cr["bank_txn_id"],
            "covers_settlement_ids": [st["settlement_id"]],
            "credit_amount_minor": cr["amount_minor"],
            "credit_currency": "INR",
            "settlement_net_sum_minor": st["net_minor"],
            "residual_minor": 0,
        },
        expected_link_resolution="MATCHED",
        expected_exception_category="REFUND_FX_ASYMMETRY",
        details={
            "refund_id": rfnd,
            "capture": {
                "value_date": d.isoformat(), "rate": str(applied),
                "reference_rate": str(ref),
                "foreign_minor": foreign, "inr_minor": st["base_minor"],
            },
            "refund": {
                "value_date": refund_date.isoformat(), "rate": str(refund_rate),
                "reference_rate": str(refund_ref),
                "foreign_minor": foreign, "inr_minor": refund_base,
            },
            "foreign_residual_minor": 0,
            "inr_residual_minor": residual,
            "currency": ccy,
        },
        notes=(
            "Full refund in the original currency; the two conversion events differ, so "
            f"INR residual is {residual} minor units. Not a break."
        ),
    )


def case_timing_pending(w: World) -> None:
    """Settled inside the T+2..T+7 nostro window as of the statement date: hold, not break."""
    rng = w.rng
    dev = Decimal(rng.randint(-2000, 2000)).scaleb(-2)
    name, addr, city, country, ccy = _pick_foreign(w)
    age = rng.randint(2, 6) if w.harden else rng.randint(2, 7)
    d = STATEMENT_AS_OF - timedelta(days=age)
    foreign_minor = rng.randrange(80_000, 60_00_000)
    ref = w.ref_rate(ccy, d)
    applied = (ref * (Decimal(1) + dev / 10_000)).quantize(RATE_Q, rounding=ROUND_HALF_UP)
    base = to_base_minor(foreign_minor, applied)
    inv = w.add_invoice(
        currency=ccy, amount_minor=foreign_minor, issue_date=d - timedelta(days=rng.randint(2, 10)),
        customer=name, country=country, export=True,
    )
    st = w.add_settlement(
        invoice=inv, gross_minor=foreign_minor, settled_at=d, international=True,
        conversion_rate=applied, base_minor=base,
    )
    w.add_case(
        "timing_pending",
        settlement_ids=[st["settlement_id"]], payment_ids=[st["payment_id"]],
        bank_txn_ids=[], invoice_ids=[inv["invoice_id"]],
        expected_link={
            "bank_txn_id": None,
            "covers_settlement_ids": [st["settlement_id"]],
            "credit_amount_minor": None,
            "credit_currency": None,
            "settlement_net_sum_minor": st["net_minor"],
            "residual_minor": st["net_minor"],
        },
        expected_link_resolution="UNMATCHED",
        expected_exception_category="TIMING_PENDING",
        details={
            "settled_at": d.isoformat(),
            "statement_as_of": STATEMENT_AS_OF.isoformat(),
            "days_outstanding": age,
            "expected_window_days": [2, 7],
            "inside_expected_window": True,
        },
        notes="No bank credit yet; still inside the cross-border T+2..T+7 window. Not an exception.",
    )


def case_edpms_open(w: World) -> None:
    """Export receipt with an open shipping-bill obligation against its FEMA deadline."""
    rng = w.rng
    dev = Decimal(rng.randint(-2000, 2000)).scaleb(-2)
    inv, st, d, ccy, ref, applied, party = _intl_leg(w, rate_dev_bps=dev)
    # Backdate the shipping bill so the realisation clock is close to (or past) expiry.
    days_left = rng.randint(-25, 10) if w.harden else rng.randint(5, 60)
    deadline = STATEMENT_AS_OF + timedelta(days=days_left)
    sb_date = deadline - timedelta(days=270)
    inv["shipping_bill_date"] = sb_date.isoformat()
    inv["realisation_deadline"] = deadline.isoformat()
    realised_bps = rng.randint(3000, 7000)
    realised_foreign = pct_minor(inv["invoice_amount_minor"], realised_bps, 10_000)
    realised_inr = to_base_minor(realised_foreign, applied)
    inv["realised_amount"] = money_str(realised_foreign)
    inv["invoice_status"] = "PARTIALLY_REALISED"
    inv["notes"] = "EDPMS open: shipping bill not fully realised"
    cr = _swift_credit(
        w, inv, st, d, ccy, applied, party,
        inr_minor=realised_inr - (st["fee_minor"] + st["tax_minor"]),
    )
    w.add_case(
        "edpms_open",
        settlement_ids=[st["settlement_id"]], payment_ids=[st["payment_id"]],
        bank_txn_ids=[cr["bank_txn_id"]], invoice_ids=[inv["invoice_id"]],
        expected_link={
            "bank_txn_id": cr["bank_txn_id"],
            "covers_settlement_ids": [st["settlement_id"]],
            "credit_amount_minor": cr["amount_minor"],
            "credit_currency": "INR",
            "settlement_net_sum_minor": st["net_minor"],
            "residual_minor": st["net_minor"] - cr["amount_minor"],
        },
        expected_link_resolution="PARTIAL",
        expected_exception_category="OPEN_EDPMS_LINKAGE",
        details={
            "shipping_bill_no": inv["shipping_bill_no"],
            "shipping_bill_date": inv["shipping_bill_date"],
            "purpose_code": inv["purpose_code"],
            "realisation_deadline": deadline.isoformat(),
            "statement_as_of": STATEMENT_AS_OF.isoformat(),
            "days_to_deadline": days_left,
            "overdue": days_left < 0,
            "currency": ccy,
            "invoiced_foreign_minor": inv["invoice_amount_minor"],
            "realised_foreign_minor": realised_foreign,
            "outstanding_foreign_minor": inv["invoice_amount_minor"] - realised_foreign,
        },
        notes=(
            "Export receipt partially realised against its shipping bill; "
            f"{days_left} days to the FEMA realisation deadline."
        ),
    )


def case_fee_mismatch(w: World) -> None:
    """Main split only, appended after the weighted deck (see `generate()`) --
    a real, labelled FEE_MISMATCH case for `reconagent.fx.decompose_variance`
    (spec section 6), which had zero coverage against generated data before
    this. The fee itself is booked correctly; the settlement export's own tax
    column is stale (a pre-rate-change 12% instead of the statutory 18% GST
    on the MDR), while the amount actually netted out used the correct 18%.
    Fully matchable at Stage 1 -- the anomaly lives only in the fee/tax
    breakdown, not the linkage."""
    rng = w.rng
    cust, country = _pick_domestic(w)
    d = _settle_date(w)
    gross = 500_000  # INR 5,000.00; sized so the GST gap is a clean few
    # hundred paise, comfortably outside decompose_variance's 1-minor-unit
    # NO_VARIANCE tolerance and inside its FEE_MISMATCH candidate check.
    inv = w.add_invoice(
        currency="INR", amount_minor=gross, issue_date=d - timedelta(days=rng.randint(1, 12)),
        customer=cust, country=country, export=False,
    )
    st = w.add_settlement(
        invoice=inv, gross_minor=gross, settled_at=d, international=False,
        method=rng.choice(["card", "upi", "netbanking"]),
    )
    fee = st["fee_minor"]
    correct_gst = st["tax_minor"]  # add_settlement already applied GST_PCT_ON_FEE (18%)
    tax_booked = pct_minor(fee, 12, 100)  # the stale rate mistakenly booked in the export
    w.settlement_rows[-1]["tax"] = money_str(tax_booked)
    residual = tax_booked - correct_gst  # what decompose_variance's residual will equal

    cr = w.add_credit(
        value_date=d, inr_minor=st["net_minor"],
        narration=clean_narration(inv, st["utr"], domestic=True),
        debtor_name="RAZORPAY SOFTWARE PVT LTD", swift=False,
        debtor_account="Razorpay Settlement Account",
    )
    w.add_case(
        "fee_mismatch",
        settlement_ids=[st["settlement_id"]], payment_ids=[st["payment_id"]],
        bank_txn_ids=[cr["bank_txn_id"]], invoice_ids=[inv["invoice_id"]],
        expected_link={
            "bank_txn_id": cr["bank_txn_id"],
            "covers_settlement_ids": [st["settlement_id"]],
            "credit_amount_minor": cr["amount_minor"],
            "credit_currency": "INR",
            "settlement_net_sum_minor": st["net_minor"],
            "residual_minor": 0,
        },
        expected_link_resolution="MATCHED",
        expected_exception_category="FEE_MISMATCH",
        details={
            "gross_minor": gross,
            "fee_minor": fee,
            "correct_gst_minor": correct_gst,
            "tax_booked_minor": tax_booked,
            "gst_residual_minor": residual,
            "actual_net_credited_minor": st["net_minor"],
            "utr_present_in_narration": True,
        },
        notes=(
            "Linkage is clean -- single settlement, single credit, UTR in the "
            "narration -- but the settlement export's own tax column books GST "
            f"at 12% ({tax_booked}) instead of the statutory 18% on the MDR "
            f"({correct_gst}); the amount actually netted out used the correct "
            f"rate, leaving a {residual:+d} minor-unit gap in the fee breakdown."
        ),
    )


def case_data_entry_error(w: World) -> None:
    """Main split only, appended after the weighted deck (see `generate()`) --
    a real, labelled DATA_ENTRY_ERROR case for `decompose_variance`. Fee and
    GST are both booked correctly, so the fee arithmetic ties out exactly;
    the anomaly is a fat-fingered payout -- two adjacent digits of the
    correct net transposed in the amount actually credited. That is exactly
    the fingerprint `_is_transposition` looks for (a non-zero multiple of 9,
    a digit permutation of the correct net). Fully matchable at Stage 1."""
    rng = w.rng
    cust, country = _pick_domestic(w)
    d = _settle_date(w)
    gross = 650_000  # INR 6,500.00
    inv = w.add_invoice(
        currency="INR", amount_minor=gross, issue_date=d - timedelta(days=rng.randint(1, 12)),
        customer=cust, country=country, export=False,
    )
    st = w.add_settlement(
        invoice=inv, gross_minor=gross, settled_at=d, international=False,
        method=rng.choice(["card", "upi", "netbanking"]),
    )
    expected_net = st["net_minor"]
    digits = list(str(expected_net))
    i = 1  # never the leading digit, so the digit count and sign can't change
    while digits[i] == digits[i + 1]:
        i += 1
    digits[i], digits[i + 1] = digits[i + 1], digits[i]
    actual_net = int("".join(digits))
    assert actual_net != expected_net
    w.settlement_rows[-1]["credit"] = money_str(actual_net)

    cr = w.add_credit(
        value_date=d, inr_minor=actual_net,
        narration=clean_narration(inv, st["utr"], domestic=True),
        debtor_name="RAZORPAY SOFTWARE PVT LTD", swift=False,
        debtor_account="Razorpay Settlement Account",
    )
    w.add_case(
        "data_entry_error",
        settlement_ids=[st["settlement_id"]], payment_ids=[st["payment_id"]],
        bank_txn_ids=[cr["bank_txn_id"]], invoice_ids=[inv["invoice_id"]],
        expected_link={
            "bank_txn_id": cr["bank_txn_id"],
            "covers_settlement_ids": [st["settlement_id"]],
            "credit_amount_minor": cr["amount_minor"],
            "credit_currency": "INR",
            "settlement_net_sum_minor": actual_net,
            "residual_minor": 0,
        },
        expected_link_resolution="MATCHED",
        expected_exception_category="DATA_ENTRY_ERROR",
        details={
            "gross_minor": gross,
            "fee_minor": st["fee_minor"],
            "tax_minor": st["tax_minor"],
            "expected_net_minor": expected_net,
            "actual_credited_minor": actual_net,
            "swapped_digit_positions": [i, i + 1],
            "residual_minor": actual_net - expected_net,
        },
        notes=(
            "Linkage is clean -- single settlement, single credit, UTR in the "
            "narration -- and fee/GST are both booked correctly, but the amount "
            f"actually credited ({actual_net}) transposes two adjacent digits of "
            f"the correct net ({expected_net}): a fat-fingered payout, not a fee "
            "problem."
        ),
    )


BUILDERS = {
    "clean_match": case_clean_match,
    "subset_sum_bundle": case_subset_sum_bundle,
    "fx_drift_benign": case_fx_drift_benign,
    "fx_drift_flagged": case_fx_drift_flagged,
    "missing_remitter": case_missing_remitter,
    "partial_payment": case_partial_payment,
    "refund_fx_asymmetry": case_refund_fx_asymmetry,
    "timing_pending": case_timing_pending,
    "edpms_open": case_edpms_open,
}


# ====================================================================================
# emitters
# ====================================================================================

SETTLEMENT_COLUMNS = [
    "entity_id", "type", "debit", "credit", "amount", "currency", "fee", "tax",
    "on_hold", "settled", "created_at", "settled_at", "settlement_id", "settlement_utr",
    "description", "notes", "payment_id", "order_id", "order_receipt", "method",
    "international", "conversion_rate", "base_amount", "base_currency", "refund_id",
]

INVOICE_COLUMNS = [
    "invoice_id", "invoice_date", "order_id", "customer_name", "customer_country",
    "currency", "invoice_amount", "invoice_status", "purpose_code", "shipping_bill_no",
    "shipping_bill_date", "realisation_deadline", "realised_amount", "notes",
]


def write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        wtr = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
        wtr.writeheader()
        wtr.writerows(rows)


def write_fx_csv(path: Path, fx: dict[tuple[str, str], Decimal]) -> None:
    rows = [
        {"value_date": d, "currency_pair": f"{ccy}/INR", "source": "FBIL", "reference_rate": str(r)}
        for (ccy, d), r in sorted(fx.items(), key=lambda kv: (kv[0][1], kv[0][0]))
    ]
    write_csv(path, ["value_date", "currency_pair", "source", "reference_rate"], rows)


def _mt_lines(text: str, width: int = 35, maxlines: int = 4) -> list[str]:
    out: list[str] = []
    rest = text
    while rest and len(out) < maxlines:
        out.append(rest[:width])
        rest = rest[width:]
    return out or [""]


def render_mt103(cr: dict) -> str:
    """A single MT103 message with real block structure and field tags."""
    vd = cr["value_date"]
    yymmdd = vd.strftime("%y%m%d")
    hhmm = f"{9 + (int(cr['bank_txn_id'][-3:]) % 8):02d}{(int(cr['bank_txn_id'][-2:]) % 60):02d}"
    mir = f"{yymmdd}{cr['sender_lt']}0001{int(cr['bank_txn_id'][-6:]) % 1000000:06d}"
    blk1 = f"{{1:F01{MERCHANT_LT}0000000000}}"
    blk2 = f"{{2:O103{hhmm}{mir}{yymmdd}{hhmm}N}}"
    blk3 = f"{{3:{{121:{cr['uetr']}}}}}"

    addr, city, country = cr["debtor_addr"] or ("", "", "")
    f50 = [f"/{cr['debtor_account']}"] + _mt_lines(cr["debtor_name"], maxlines=1)
    if addr:
        f50 += _mt_lines(addr, maxlines=1) + _mt_lines(f"{city} {country}".strip(), maxlines=1)
    f50 = f50[:5]

    body = [
        f":20:{cr['sender_ref']}",
        ":23B:CRED",
        f":32A:{yymmdd}INR{money_swift(cr['amount_minor'])}",
        f":33B:{cr['instructed_ccy']}{money_swift(cr['instructed_minor'])}",
        f":36:{str(cr['exchange_rate']).replace('.', ',')}",
        ":50K:" + f50[0],
        *f50[1:],
        f":52A:{cr['sender_bic']}",
        f":57A:{MERCHANT_BIC}",
        ":59:" + f"/{MERCHANT_ACCOUNT}",
        MERCHANT_NAME[:35],
        "MUMBAI IN",
        ":70:" + _mt_lines(cr["narration"])[0],
        *_mt_lines(cr["narration"])[1:],
        f":71A:{cr['charge_details']}",
    ]
    if cr["purpose_code"]:
        body.append(f":72:/ACC/PURPOSE CODE {cr['purpose_code']}")
    return blk1 + blk2 + blk3 + "{4:\n" + "\n".join(body) + "\n-}"


def write_mt103(path: Path, credits: list[dict]) -> None:
    msgs = [render_mt103(c) for c in credits if c["channel"] == "SWIFT_MT103"]
    path.write_text("\n$\n".join(msgs) + ("\n$\n" if msgs else ""), encoding="utf-8")


CAMT_NS = "urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"


def _sub(parent, tag, text=None, **attrib):
    el = ET.SubElement(parent, f"{{{CAMT_NS}}}{tag}", attrib)
    if text is not None:
        el.text = text
    return el


def write_camt053(path: Path, credits: list[dict], split: str) -> None:
    ET.register_namespace("", CAMT_NS)
    root = ET.Element(f"{{{CAMT_NS}}}Document")
    doc = _sub(root, "BkToCstmrStmt")
    hdr = _sub(doc, "GrpHdr")
    _sub(hdr, "MsgId", f"STMT-{split.upper()}-{PERIOD_END:%Y%m%d}")
    _sub(hdr, "CreDtTm", f"{PERIOD_END.isoformat()}T23:59:00")

    stmt = _sub(doc, "Stmt")
    _sub(stmt, "Id", f"{MERCHANT_ACCOUNT}-{PERIOD_END:%Y%m}")
    _sub(stmt, "ElctrncSeqNb", "1")
    _sub(stmt, "CreDtTm", f"{PERIOD_END.isoformat()}T23:59:00")
    per = _sub(stmt, "FrToDt")
    _sub(per, "FrDtTm", f"{PERIOD_START.isoformat()}T00:00:00")
    _sub(per, "ToDtTm", f"{PERIOD_END.isoformat()}T23:59:59")

    acct = _sub(stmt, "Acct")
    aid = _sub(acct, "Id")
    othr = _sub(aid, "Othr")
    _sub(othr, "Id", MERCHANT_IBAN_OTHR)
    _sub(acct, "Ccy", "INR")
    ownr = _sub(acct, "Ownr")
    _sub(ownr, "Nm", MERCHANT_NAME)
    svcr = _sub(acct, "Svcr")
    fi = _sub(svcr, "FinInstnId")
    _sub(fi, "BIC", MERCHANT_BIC)

    total = sum(c["amount_minor"] for c in credits)
    for code, amount, dt in (
        ("OPBD", OPENING_BALANCE_MINOR, PERIOD_START),
        ("CLBD", OPENING_BALANCE_MINOR + total, PERIOD_END),
    ):
        bal = _sub(stmt, "Bal")
        tp = _sub(bal, "Tp")
        cd = _sub(tp, "CdOrPrtry")
        _sub(cd, "Cd", code)
        _sub(bal, "Amt", money_str(amount), Ccy="INR")
        _sub(bal, "CdtDbtInd", "CRDT")
        bdt = _sub(bal, "Dt")
        _sub(bdt, "Dt", dt.isoformat())

    summ = _sub(stmt, "TxsSummry")
    tcdt = _sub(summ, "TtlCdtNtries")
    _sub(tcdt, "NbOfNtries", str(len(credits)))
    _sub(tcdt, "Sum", money_str(total))

    for c in credits:
        ntry = _sub(stmt, "Ntry")
        _sub(ntry, "NtryRef", c["bank_txn_id"])
        _sub(ntry, "Amt", money_str(c["amount_minor"]), Ccy="INR")
        _sub(ntry, "CdtDbtInd", "CRDT")
        _sub(ntry, "Sts", "BOOK")
        bd = _sub(ntry, "BookgDt")
        _sub(bd, "Dt", c["booking_date"].isoformat())
        vd = _sub(ntry, "ValDt")
        _sub(vd, "Dt", c["value_date"].isoformat())
        _sub(ntry, "AcctSvcrRef", c["sender_ref"])
        btc = _sub(ntry, "BkTxCd")
        dom = _sub(btc, "Domn")
        _sub(dom, "Cd", "PMNT")
        fam = _sub(dom, "Fmly")
        _sub(fam, "Cd", "RCDT")
        _sub(fam, "SubFmlyCd", "XBCT" if c["channel"] == "SWIFT_MT103" else "DMCT")

        det = _sub(ntry, "NtryDtls")
        tx = _sub(det, "TxDtls")
        refs = _sub(tx, "Refs")
        _sub(refs, "MsgId", c["sender_ref"])
        _sub(refs, "EndToEndId", c["sender_ref"])
        _sub(refs, "TxId", c["bank_txn_id"])
        if c["channel"] == "SWIFT_MT103":
            _sub(refs, "UETR", c["uetr"])

        _sub(tx, "Amt", money_str(c["amount_minor"]), Ccy="INR")
        _sub(tx, "CdtDbtInd", "CRDT")
        if c["instructed_ccy"]:
            ad = _sub(tx, "AmtDtls")
            instd = _sub(ad, "InstdAmt")
            _sub(instd, "Amt", money_str(c["instructed_minor"]), Ccy=c["instructed_ccy"])
            txa = _sub(ad, "TxAmt")
            _sub(txa, "Amt", money_str(c["amount_minor"]), Ccy="INR")
            cx = _sub(txa, "CcyXchg")
            _sub(cx, "SrcCcy", c["instructed_ccy"])
            _sub(cx, "TrgtCcy", "INR")
            _sub(cx, "XchgRate", str(c["exchange_rate"]))

        agts = _sub(tx, "RltdAgts")
        dbtragt = _sub(agts, "DbtrAgt")
        dfi = _sub(dbtragt, "FinInstnId")
        _sub(dfi, "BIC", c["sender_bic"])

        pties = _sub(tx, "RltdPties")
        dbtr = _sub(pties, "Dbtr")
        _sub(dbtr, "Nm", c["debtor_name"])
        if c["debtor_addr"]:
            adr = _sub(dbtr, "PstlAdr")
            _sub(adr, "StrtNm", c["debtor_addr"][0])
            _sub(adr, "TwnNm", c["debtor_addr"][1])
            _sub(adr, "Ctry", c["debtor_addr"][2])
        if c["debtor_account"]:
            da = _sub(pties, "DbtrAcct")
            dai = _sub(da, "Id")
            dao = _sub(dai, "Othr")
            _sub(dao, "Id", c["debtor_account"])
        cdtr = _sub(pties, "Cdtr")
        _sub(cdtr, "Nm", MERCHANT_NAME)
        ca = _sub(pties, "CdtrAcct")
        cai = _sub(ca, "Id")
        cao = _sub(cai, "Othr")
        _sub(cao, "Id", MERCHANT_ACCOUNT)

        if c["purpose_code"]:
            purp = _sub(tx, "Purp")
            _sub(purp, "Prtry", c["purpose_code"])

        rmt = _sub(tx, "RmtInf")
        _sub(rmt, "Ustrd", c["narration"])

    ET.indent(root, space="  ")
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    path.write_bytes(path.read_bytes().rstrip() + b"\n")


CONVENTIONS = {
    "money": (
        "Every *_minor field in this file is an integer count of currency minor units "
        "(paise for INR, cents for USD/EUR/GBP/SGD/AED). Never a float. The emitted CSV / "
        "MT103 / camt.053 carry the same values as exact fixed-point decimal strings."
    ),
    "rates": (
        "All rates are decimal strings, INR per 1 unit of the foreign currency, 4 dp. "
        "reference_rate is the FBIL daily rate for the value date (see fx_reference_rates.csv); "
        "applied_rate is what Razorpay actually used."
    ),
    "amount_tolerance_minor": LABEL_AMOUNT_TOLERANCE_MINOR,
    "fx_tolerance_bps": str(LABEL_FX_TOLERANCE_BPS),
    "tolerance_ownership": (
        "amount_tolerance_minor and fx_tolerance_bps describe how THIS FILE was labelled. "
        "They are not a configuration input to the matcher or the FX validator, which own "
        "their own bands. They are published so a grader can see what the label means."
    ),
    "case_fields": {
        "case_id": "stable id, unique within the split",
        "defect_class": (
            "one of " + ", ".join(sorted(BUILDERS))
            + " (main split only, appended after the weighted deck: fee_mismatch, "
            "data_entry_error)"
        ),
        "split": "main | holdout",
        "settlement_ids": "every settlement_id in razorpay_settlements.csv this case involves",
        "payment_ids": "every entity_id (pay_/rfnd_) this case involves",
        "bank_txn_ids": "every NtryRef in the camt.053 (and TxId) this case involves; [] if none",
        "invoice_ids": "every invoice_id in invoice_ledger.csv this case involves",
        "expected_link": (
            "the correct linkage: which bank credit covers which exact set of settlements, "
            "the credit amount, the sum of those settlements' net amounts, and the residual "
            "(settlement_net_sum_minor - credit_amount_minor)"
        ),
        "expected_link_resolution": "MATCHED | PARTIAL | UNMATCHED",
        "expected_exception_category": (
            "null, or one of BENIGN_FX_DRIFT, FLAGGED_FX_DRIFT, MISSING_SENDER_INFO, "
            "TIMING_PENDING, PARTIAL_PAYMENT, REFUND_FX_ASYMMETRY, OPEN_EDPMS_LINKAGE"
        ),
        "details": "class-specific expected values; see notes",
        "notes": "plain-English statement of what the case simulates",
    },
    "statement_as_of": STATEMENT_AS_OF.isoformat(),
    "period": [PERIOD_START.isoformat(), PERIOD_END.isoformat()],
}


def build(seed: int, scale: int, split: str, harden: bool) -> World:
    rng = random.Random(seed)
    w = World(rng, split, harden)
    w.build_fx()
    weights = WEIGHTS_HOLDOUT if harden else WEIGHTS_MAIN
    deck = [k for k, n in weights.items() for _ in range(n)]
    i = len(deck)
    while len(w.settlements) < scale:
        if i >= len(deck):
            rng.shuffle(deck)
            i = 0
        BUILDERS[deck[i]](w)
        i += 1
    return w


def emit(w: World, out_dir: Path, prefix: str, seed: int, scale: int) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    credits = sorted(w.credits, key=lambda c: (c["value_date"], c["bank_txn_id"]))
    write_csv(out_dir / f"{prefix}razorpay_settlements.csv", SETTLEMENT_COLUMNS, w.settlement_rows)
    write_csv(out_dir / f"{prefix}invoice_ledger.csv", INVOICE_COLUMNS, w.invoices)
    write_fx_csv(out_dir / f"{prefix}fx_reference_rates.csv", w.fx)
    write_mt103(out_dir / f"{prefix}bank_statement.mt103", credits)
    write_camt053(out_dir / f"{prefix}bank_statement.camt053.xml", credits, w.split)

    counts: dict[str, int] = {}
    for c in w.cases:
        counts[c["defect_class"]] = counts.get(c["defect_class"], 0) + 1
    gt = {
        "schema_version": "1.0",
        "generator": {
            "script": "scripts/generate_synthetic.py",
            "seed": seed,
            "scale": scale,
            "split": w.split,
            "adversarial_holdout": w.harden,
            "deterministic": "same seed + scale => byte-identical output; no wall-clock input",
        },
        "conventions": CONVENTIONS,
        "counts": {
            "cases": len(w.cases),
            "settlements": len(w.settlements),
            "settlement_rows": len(w.settlement_rows),
            "bank_credits": len(w.credits),
            "invoices": len(w.invoices),
            "by_defect_class": dict(sorted(counts.items())),
        },
        "fx_reference_rates": [
            {"value_date": d, "currency_pair": f"{ccy}/INR", "source": "FBIL", "reference_rate": str(r)}
            for (ccy, d), r in sorted(w.fx.items(), key=lambda kv: (kv[0][1], kv[0][0]))
        ],
        "cases": w.cases,
    }
    (out_dir / f"{prefix}ground_truth.json").write_text(
        json.dumps(gt, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )


def generate(seed: int, scale: int, data_dir: Path) -> dict[str, dict[str, int]]:
    main = build(seed, scale, "main", harden=False)
    # Main split only: two hand-specified cases appended strictly after the
    # weighted-deck loop above, consuming the seeded rng in its current state
    # (never rewound), so every settlement/case the deck loop already built
    # stays byte-identical -- see reconagent-design-description.md section 6
    # / CLAUDE.md build discipline. These close the only two decompose_variance
    # categories (FEE_MISMATCH, DATA_ENTRY_ERROR) that had no real generated
    # coverage.
    case_fee_mismatch(main)
    case_data_entry_error(main)
    emit(main, data_dir, "", seed, scale)

    holdout_scale = max(60, scale // 2)
    holdout_seed = seed + 100_000
    hold = build(holdout_seed, holdout_scale, "holdout", harden=True)
    emit(hold, data_dir / "holdout", "HOLDOUT_", holdout_seed, holdout_scale)
    (data_dir / "holdout" / "DO_NOT_TUNE_ON_THESE_FILES.txt").write_text(
        "Adversarial holdout. Evaluate against it; never tune against it.\n"
        "Generated by scripts/generate_synthetic.py with a different seed and a harder\n"
        "defect mix than data/. If a threshold was chosen by looking at these numbers,\n"
        "the holdout is burned and must be regenerated with a fresh seed.\n",
        encoding="utf-8",
    )
    return {
        "main": {c["defect_class"]: 1 for c in main.cases},
        "holdout": {c["defect_class"]: 1 for c in hold.cases},
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", type=int, default=20260903)
    ap.add_argument("--scale", type=int, default=200, help="target number of settlements (main set)")
    ap.add_argument("--out", type=Path, default=REPO / "data")
    args = ap.parse_args()
    generate(args.seed, args.scale, args.out)
    for path in sorted(args.out.rglob("*")):
        if path.is_file():
            print(path.relative_to(args.out.parent) if args.out.parent in path.parents else path)


if __name__ == "__main__":
    main()
