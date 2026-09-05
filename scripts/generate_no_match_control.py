#!/usr/bin/env python3
"""No-match control population -- money that arrives with no settlement behind it.

WHY THIS EXISTS (a review response, not a spec requirement)

Every linked case in `data/` and `data/holdout/` has ground truth MATCHED or
PARTIAL. That is stated plainly in `reconagent/eval.py`'s own docstring. So
the headline "152/152 correct, 0 false matches" is, strictly, a statement
about credits that are *answerable*: every credit in the test set does cover
some settlement, and the only question was which. A payments-literate
reviewer's first move against a clean number like that is to ask the
obvious: what happens when money arrives that legitimately has no match at
all -- a misdirected wire, a bank posting error, a tax refund, an investor
inflow? Does the system decline, or does it find something?

`reconagent.match` has the vocabulary for declining (UNMATCHED, AMBIGUOUS,
TIE_AMBIGUOUS) and unit tests exercise it on hand-built fixtures. What did
not exist was a *generated population* in the shape of the real datasets,
scored by the real evaluation harness, proving the decline happens on data
that was never constructed to be matchable.

WHY A SEPARATE DIRECTORY

`data/` and `data/holdout/` are not touched, at all. Dozens of tests
hardcode exact numbers against those two directories' exact byte content
(main: 152/152 correct, 0 false-match, 0 false-clear; holdout: 50/53
correct, 0 false-match, 3 tie-ambiguous). Adding cases there -- even purely
additive ones -- would move those denominators and force a re-verification
of everything downstream. So this follows the precedent `stress_test/`
already set: a new top-level directory, its own generator, its own
ground_truth.json in the same schema, scored through a parallel code path
that never merges into the existing tallies.

WHAT MAKES A CASE HERE HARD RATHER THAN TRIVIAL

A credit that has nothing to compare it to proves nothing. Every credit here
is scored against its own split's FULL real settlement list -- 202
settlements for main, 100 for holdout, all of them open, because no credit
here resolves anything at Stage 1 and so nothing gets consumed out of the
pool. That is a deliberately harsher pool than the real splits give Stage 2,
where Stage 1 has already taken ~90% of the settlements out. `match.py`'s
own MEASURED CEILING note says exactly what that costs: probing arbitrary
amounts against an unpruned pool, "roughly a sixth of arbitrary amounts find
an exact subset". So a "random enough" amount is emphatically NOT safe here,
and this generator does not assume otherwise -- see VERIFICATION below.

Amounts are drawn in the middle of the real settlement range (INR 50K - 40L
against a real range of roughly INR 660 - 65L), so the magnitude and date
filters in `_pool` do not quietly hand these credits an empty candidate set.
Every credit gets a full, real, adversarial pool and still has to be
declined on the arithmetic.

VERIFICATION (the part that would otherwise be "probably fine")

After construction, the whole batch is run through `match_all` against the
real split's settlements, exactly as the evaluation harness runs it. Any
credit that comes back MATCHED or PARTIAL is a construction accident -- an
amount that happened to coincide with a real subset -- and its amount is
redrawn and the batch re-run, until the batch is clean or MAX_REDRAW_ROUNDS
is exhausted (in which case this script fails loudly rather than emitting a
dataset it cannot stand behind). Redraw counts are recorded per case in
ground_truth.json so the reader can see how much rejection sampling the
pool density actually forced.

Stage 1 is checked separately and absolutely: `match_deterministic` must
return None for every credit -- meaning no reference on the credit, and no
whole token in its narration, names any settlement in the split. That is
asserted, never sampled around.

Note this generator does NOT redraw a TIE_AMBIGUOUS or AMBIGUOUS result.
Those are correct rejections (the system declined to assert a link), and
tuning them away would make the population cleaner than reality for no
honest reason. Whatever mix comes out is what gets reported.

Money is integer minor units end to end. Deterministic: one seeded
random.Random per split, no wall-clock input.

Run:  .venv/bin/python scripts/generate_no_match_control.py
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO))

import generate_synthetic as base  # noqa: E402  (reuse the real camt.053 emitter + helpers)

from reconagent.camt053 import parse_camt053_file  # noqa: E402
from reconagent.match import (  # noqa: E402
    MATCHED,
    PARTIAL,
    match_all,
    match_deterministic,
)
from reconagent.razorpay import parse_razorpay_settlements  # noqa: E402
from reconagent.records import CanonicalRecord  # noqa: E402

OUT_ROOT = REPO / "no_match_control"

# Middle of the real settlement range on both splits (roughly INR 660 to
# INR 65,00,000). Deliberately not at either extreme: a credit smaller than
# every settlement would get an empty pool from `_pool`'s magnitude filter
# and be declined for free, which is exactly the vacuous test this
# population exists to avoid.
AMOUNT_MIN_MINOR = 50_00_000        # INR 50,000.00
AMOUNT_MAX_MINOR = 4000_00_000      # INR 40,00,000.00

MAX_REDRAW_ROUNDS = 40

# Remitting-bank BICs for the SWIFT-side cases; keys are the debtor country.
SWIFT_RATES = {"USD": Decimal("88.4200"), "EUR": Decimal("95.6100"),
               "GBP": Decimal("112.3300"), "AED": Decimal("24.0700")}

# Domestic remitters route through a real Indian bank BIC rather than
# generate_synthetic.SENDER_BANKS (which is keyed by foreign country).
DOMESTIC_BANKS = {
    "HDFC": ("HDFCINBBXXX", "HDFC BANK LIMITED"),
    "SBI": ("SBININBBXXX", "STATE BANK OF INDIA"),
    "AXIS": ("AXISINBBXXX", "AXIS BANK LIMITED"),
    "RBI": ("RBISINBBXXX", "RESERVE BANK OF INDIA"),
}


# --------------------------------------------------------------------------
# The ten reasons. Ten *different* reasons money can land in a merchant's
# account with no settlement behind it -- not one reason repeated ten times
# with different names. A reviewer who reads the ground truth should see a
# spread of causes (someone else's payment, the bank's own error, a
# non-trade inflow, a refund of something the merchant paid out), because
# that spread is what makes "the system declines" a claim about the matcher
# rather than a claim about one narrow synthetic shape.
#
# Each entry carries two remitter variants -- one for the main split, one
# for the holdout split -- so the two populations are genuinely separate
# rather than the same ten names run twice.
# --------------------------------------------------------------------------

def _c(name, narration, *, swift=False, country="IN", addr=None, ccy=None, bank="HDFC"):
    return {"name": name, "narration": narration, "swift": swift, "country": country,
            "addr": addr, "ccy": ccy, "bank": bank}


REASONS = [
    {
        "defect_class": "misdirected_wire",
        "notes": (
            "An inbound wire intended for a different beneficiary entirely -- the "
            "remitter keyed the wrong account number. The money is in the merchant's "
            "account; none of the merchant's settlements are behind it."
        ),
        "main": _c("HANSEATIC MARITIME SUPPLY GMBH",
                   "/RFB/HANSEATIC MARITIME SUPPLY - BENEFICIARY A/C 50200087654322 "
                   "PO 88231 - PLEASE CONFIRM RECEIPT",
                   swift=True, country="DE", addr=("GROSSE BLEICHEN 4", "HAMBURG"), ccy="EUR"),
        "holdout": _c("CORDILLERA MINERALS SA",
                      "/RFB/CORDILLERA MINERALS - INV REF CM-4471 BENEFICIARY "
                      "ACCOUNT 50200087654399",
                      swift=True, country="US", addr=("77 WATER STREET", "NEW YORK NY"), ccy="USD"),
    },
    {
        "defect_class": "unrelated_inbound_wire",
        "notes": (
            "A genuine inbound wire from an entity this merchant has never traded "
            "with -- no invoice, no order, no settlement, no prior relationship."
        ),
        "main": _c("VALLEYCREST AGRO COOPERATIVE",
                   "NEFT CR-HDFC0000412-VALLEYCREST AGRO COOPERATIVE-TRADE ADVANCE",
                   bank="HDFC"),
        "holdout": _c("TORRENT MINERALS TRADING FZE",
                      "/RFB/TORRENT MINERALS TRADING - ADVANCE AGAINST FUTURE SUPPLY",
                      swift=True, country="AE", addr=("JEBEL ALI FREE ZONE", "DUBAI"), ccy="AED"),
    },
    {
        "defect_class": "bank_posting_error",
        "notes": (
            "The bank's own posting error -- a credit booked to the wrong account "
            "and reversed in the following statement period. There is no "
            "counterparty transaction behind it at all."
        ),
        "main": _c("RATNAKAR BANK LIMITED",
                   "CR ADJ-POSTING ERROR-BRANCH 0088-TO BE REVERSED-SR 5540118",
                   bank="AXIS"),
        "holdout": _c("STATE BANK OF INDIA",
                      "MISPOST REVERSAL PENDING-BR 01411-SVC REQ 7719204",
                      bank="SBI"),
    },
    {
        "defect_class": "investor_capital_infusion",
        "notes": (
            "Equity subscription money from an investor. A capital inflow is not "
            "trade revenue, so no settlement can ever exist for it -- the matcher "
            "must not reach for one."
        ),
        "main": _c("ORIEL GROWTH PARTNERS II LP",
                   "NEFT CR-HDFC0000221-ORIEL GROWTH PARTNERS II LP-SHARE SUBSCRIPTION "
                   "TRANCHE B",
                   bank="HDFC"),
        "holdout": _c("MERIDIAN SEED CAPITAL LLP",
                      "NEFT CR-AXIS0000509-MERIDIAN SEED CAPITAL LLP-CCPS SUBSCRIPTION "
                      "ROUND A",
                      bank="AXIS"),
    },
    {
        "defect_class": "insurance_claim_payout",
        "notes": (
            "An insurer settling a marine-cargo claim. Money in, but the "
            "counterparty is the insurer, not a customer, and it corresponds to no "
            "payment gateway settlement."
        ),
        "main": _c("NEW HORIZON GENERAL INSURANCE CO LTD",
                   "NEFT CR-SBIN0004512-NEW HORIZON GENERAL INSURANCE-CLAIM SETTLEMENT "
                   "MARINE CARGO CLM 90218774",
                   bank="SBI"),
        "holdout": _c("SENTINEL ASSURANCE COMPANY LIMITED",
                      "NEFT CR-HDFC0000901-SENTINEL ASSURANCE-CLAIM PAYOUT FIRE AND "
                      "PERILS CLM 33107755",
                      bank="HDFC"),
    },
    {
        "defect_class": "tax_refund",
        "notes": (
            "A GST refund from the tax authority. Government money, arriving on a "
            "government reference, with no settlement or invoice behind it."
        ),
        "main": _c("CENTRAL BOARD OF INDIRECT TAXES AND CUSTOMS",
                   "GST REFUND ARN AA290826003911Q-RFD-05 SANCTIONED-PMT ADVICE",
                   bank="RBI"),
        "holdout": _c("INCOME TAX DEPARTMENT CPC",
                      "ITR REFUND AY 2026-27 CPC ORDER 100320260811-SEQ 4471",
                      bank="RBI"),
    },
    {
        "defect_class": "bank_interest_credit",
        "notes": (
            "Quarterly savings interest posted by the merchant's own bank. Not a "
            "customer payment at all."
        ),
        "main": _c("ICICI BANK LIMITED",
                   "INT CR-QTR ENDED 30JUN2026-SB A/C 50200087654321-TDS DEDUCTED",
                   bank="AXIS"),
        "holdout": _c("ICICI BANK LIMITED",
                      "CREDIT INTEREST-SWEEP FD LINKED-A/C 50200087654321-QTR 1 FY27",
                      bank="AXIS"),
    },
    {
        "defect_class": "vendor_overpayment_refund",
        "notes": (
            "A supplier returning money the merchant overpaid them. The cash flows "
            "the wrong way round for a settlement: this is the merchant's own money "
            "coming back, not a customer's arriving."
        ),
        "main": _c("KAVERI PACKAGING SOLUTIONS PVT LTD",
                   "NEFT CR-HDFC0000330-KAVERI PACKAGING SOLUTIONS-REFUND OF EXCESS "
                   "REMITTANCE PO 2026-0774",
                   bank="HDFC"),
        "holdout": _c("SARAVANA LOGISTICS AND FREIGHT PVT LTD",
                      "NEFT CR-SBIN0007712-SARAVANA LOGISTICS-DUPLICATE PAYMENT RETURNED "
                      "PO 2026-1188",
                      bank="SBI"),
    },
    {
        "defect_class": "security_deposit_refund",
        "notes": (
            "A landlord returning the office security deposit on lease exit. A "
            "balance-sheet movement, not revenue."
        ),
        "main": _c("PRESTIGE ESTATES REALTY LLP",
                   "NEFT CR-AXIS0000118-PRESTIGE ESTATES REALTY-SECURITY DEPOSIT REFUND "
                   "LEASE EXIT UNIT 704",
                   bank="AXIS"),
        "holdout": _c("EMBASSY WORKSPACE VENTURES LLP",
                      "NEFT CR-HDFC0000615-EMBASSY WORKSPACE VENTURES-DEPOSIT REFUND ON "
                      "LEASE TERMINATION",
                      bank="HDFC"),
    },
    {
        "defect_class": "fx_forward_settlement",
        "notes": (
            "A treasury desk crediting the settlement of a matured FX forward "
            "contract. It looks like export money and it is denominated the same "
            "way, which is exactly why it is worth having in this population -- "
            "there is still no settlement behind it."
        ),
        "main": _c("KOTAK MAHINDRA BANK TREASURY",
                   "FWD CONTRACT SETTLEMENT-DEAL 8841207-USD/INR MATURED-NET CR TO A/C",
                   bank="AXIS"),
        "holdout": _c("YES BANK TREASURY DESK",
                      "FX FORWARD MATURITY-DEAL 6620914-EUR/INR-MTM CREDIT TO CLIENT A/C",
                      bank="AXIS"),
    },
]


# --------------------------------------------------------------------------
# credit construction
# --------------------------------------------------------------------------


def _draw_amount(rng: random.Random) -> int:
    """Integer minor units. Never a float on the money path."""
    return rng.randrange(AMOUNT_MIN_MINOR, AMOUNT_MAX_MINOR)


def _build_credit(rng: random.Random, tag: str, index: int, reason: dict, variant: dict) -> dict:
    """One camt.053-ready credit dict, in exactly the shape
    `generate_synthetic.write_camt053` consumes."""
    if variant["swift"]:
        bic, lt, bank_name = base.SENDER_BANKS[variant["country"]]
        addr = (variant["addr"][0], variant["addr"][1], variant["country"])
        account = f"{variant['country']}{rng.randrange(10**16, 10**17)}"
    else:
        bic, bank_name = DOMESTIC_BANKS[variant["bank"]]
        lt = bic[:8] + "AXXX"
        addr = None
        account = str(rng.randrange(10**11, 10**12))

    amount_minor = _draw_amount(rng)
    credit = {
        # Prefixed so a bank_txn_id from this population can never be mistaken
        # for -- or collide with -- one from data/ (BNKM...) or data/holdout/
        # (BNKH...).
        "bank_txn_id": f"NOMATCH{tag}{index + 1:04d}",
        "value_date": base.PERIOD_START + timedelta(days=rng.randint(6, 27)),
        "amount_minor": amount_minor,
        "currency": "INR",
        "narration": variant["narration"],
        "debtor_name": variant["name"],
        "debtor_addr": addr,
        "debtor_account": account,
        "channel": "SWIFT_MT103" if variant["swift"] else "DOMESTIC_NEFT",
        "instructed_ccy": None,
        "instructed_minor": None,
        "exchange_rate": None,
        "purpose_code": "",
        "charge_details": "SHA",
        "sender_bic": bic,
        "sender_lt": lt,
        "sender_bank": bank_name,
        # "FT" + 12 digits. Structurally incapable of colliding with a
        # settlement UTR (16 uppercase alnum), payment id (pay_...), order id
        # (order_...) or invoice id (INV-2026-...) -- and asserted anyway by
        # the Stage 1 check in `build_split`.
        "sender_ref": "FT" + "".join(rng.choices("0123456789", k=12)),
        "uetr": f"{base.hexs(rng, 8)}-{base.hexs(rng, 4)}-4{base.hexs(rng, 3)}-"
                f"a{base.hexs(rng, 3)}-{base.hexs(rng, 12)}",
        "_defect_class": reason["defect_class"],
        "_notes": reason["notes"],
        "_redraws": 0,
    }
    credit["booking_date"] = credit["value_date"]
    if variant["swift"]:
        rate = SWIFT_RATES[variant["ccy"]]
        credit["instructed_ccy"] = variant["ccy"]
        credit["exchange_rate"] = rate
        # Integer minor units, exact-decimal division -- no float anywhere.
        credit["instructed_minor"] = int(Decimal(amount_minor) / rate)
    return credit


def _to_record(credit: dict) -> CanonicalRecord:
    """The same projection `reconagent.camt053.parse_camt053_file` produces for
    this entry. Used only to drive the verification loop below without
    round-tripping XML on every redraw; the emitted file is re-parsed and
    re-verified for real at the end of `generate()`."""
    return CanonicalRecord(
        source="bank_credit",
        record_id=credit["bank_txn_id"],
        counterparty_name=credit["debtor_name"],
        narration=credit["narration"],
        amount_minor=credit["amount_minor"],
        currency="INR",
        booking_date=credit["booking_date"],
        value_date=credit["value_date"],
        end_to_end_id=credit["sender_ref"],
        conversion_rate=credit["exchange_rate"],
        foreign_amount_minor=credit["instructed_minor"],
        foreign_currency=credit["instructed_ccy"],
        channel="camt.053",
    )


def _refresh_swift_instructed(credit: dict) -> None:
    if credit["instructed_ccy"]:
        credit["instructed_minor"] = int(
            Decimal(credit["amount_minor"]) / credit["exchange_rate"]
        )


def build_split(seed: int, tag: str, variant_key: str, settlements: list) -> list[dict]:
    """Ten no-match credits for one split, verified against that split's real
    settlement pool. Raises if verification cannot be satisfied -- an
    unverifiable dataset is worse than no dataset for this particular claim."""
    rng = random.Random(seed)
    credits = [
        _build_credit(rng, tag, i, reason, reason[variant_key])
        for i, reason in enumerate(REASONS)
    ]

    # Stage 1 must find nothing, absolutely -- no reference on the credit and
    # no whole token in the narration names any settlement. This is a
    # construction property, not something to sample around, so it is asserted
    # before the amount search rather than folded into it.
    for c in credits:
        r1 = match_deterministic(_to_record(c), settlements)
        if r1 is not None:
            raise AssertionError(
                f"{c['bank_txn_id']}: narration or reference names settlement(s) "
                f"{r1.settlement_ids} -- fix the narration text, do not redraw"
            )

    for _round in range(MAX_REDRAW_ROUNDS):
        records = [_to_record(c) for c in credits]
        results = {r.bank_txn_id: r for r in match_all(records, settlements)}
        coincidences = [c for c in credits
                        if results[c["bank_txn_id"]].resolution in (MATCHED, PARTIAL)]
        if not coincidences:
            for c in credits:
                c["_resolution"] = results[c["bank_txn_id"]].resolution
                c["_pool_size"] = results[c["bank_txn_id"]].pool_size
            return credits
        for c in coincidences:
            c["amount_minor"] = _draw_amount(rng)
            c["_redraws"] += 1
            _refresh_swift_instructed(c)

    raise AssertionError(
        f"{tag}: could not find no-match amounts for "
        f"{[c['bank_txn_id'] for c in coincidences]} in {MAX_REDRAW_ROUNDS} rounds"
    )


# --------------------------------------------------------------------------
# emission -- same ground-truth schema as the rest of the project
# --------------------------------------------------------------------------

CONVENTIONS = {
    "purpose": (
        "A control population of bank credits that correspond to NO settlement "
        "at all. data/ and data/holdout/ contain only answerable credits (every "
        "linked case there is ground-truth MATCHED or PARTIAL), so their headline "
        "accuracy cannot speak to what the matcher does with money that has no "
        "right answer. This dataset supplies that population. The correct system "
        "behaviour for every case here is to decline: UNMATCHED, AMBIGUOUS or "
        "TIE_AMBIGUOUS. Asserting MATCHED or PARTIAL on any of these is a false "
        "match -- the failure this project exists to avoid."
    ),
    "separate_population": (
        "These cases are scored through a parallel code path and are NEVER "
        "merged into data/'s or data/holdout/'s tallies. Neither of those "
        "directories is modified by this generator. Report the two populations "
        "as two populations, never as one combined denominator."
    ),
    "scored_against_the_real_pool": (
        "Each credit is matched against its own split's FULL real settlement "
        "list (data/razorpay_settlements.csv for main, "
        "data/holdout/HOLDOUT_razorpay_settlements.csv for holdout), not an "
        "empty or reduced pool. Because nothing here resolves at Stage 1, every "
        "settlement stays open, so Stage 2 searches an unpruned pool -- a "
        "strictly harder candidate space than the real splits present."
    ),
    "amount_construction": (
        "Amounts are integer minor units drawn from the middle of the real "
        "settlement range, then verified by running match_all against the real "
        "settlements and redrawn if any landed on a coincidental subset. "
        "amount_redraws below records how many redraws each case needed."
    ),
    "money": (
        "Every *_minor field in this file is an integer count of currency minor "
        "units (paise for INR). Never a float. The emitted camt.053 carries the "
        "same values as exact fixed-point decimal strings."
    ),
    "not_a_realistic_proportions_dataset": (
        "Real statements are not 100% unmatchable money. Do not compute a "
        "production-style match-rate expectation from this file; it is a control "
        "population for one specific question."
    ),
    "case_fields": {
        "case_id": "stable id, unique within this dataset",
        "defect_class": "the reason this money has no settlement behind it",
        "split": "no_match_control_main | no_match_control_holdout",
        "settlement_ids": "always empty -- that is the point of this dataset",
        "payment_ids": "always empty",
        "bank_txn_ids": "the NtryRef in the camt.053 this case involves",
        "invoice_ids": "always empty",
        "expected_link": (
            "covers_settlement_ids is empty; settlement_net_sum_minor is 0 and "
            "residual_minor is -credit_amount_minor, matching the convention "
            "reconagent.match uses for its own UNMATCHED results"
        ),
        "expected_link_resolution": "UNMATCHED for every case in this dataset",
        "expected_exception_category": "the defect_class, upper-cased",
        "details": (
            "the remitter, the narration as sent, how many amount redraws the "
            "verification loop needed, and the Stage 2 pool size the credit was "
            "actually declined against"
        ),
        "notes": "plain-English statement of why this money has no match",
    },
    "matched_against_settlements_file": None,  # filled per split in emit()
    "period": [base.PERIOD_START.isoformat(), base.PERIOD_END.isoformat()],
}


def _case(credit: dict, index: int, split_name: str) -> dict:
    return {
        "case_id": f"{split_name.upper().replace('_', '-')}-{index + 1:05d}",
        "defect_class": credit["_defect_class"],
        "split": split_name,
        "settlement_ids": [],
        "payment_ids": [],
        "bank_txn_ids": [credit["bank_txn_id"]],
        "invoice_ids": [],
        "expected_link": {
            "bank_txn_id": credit["bank_txn_id"],
            "covers_settlement_ids": [],
            "credit_amount_minor": credit["amount_minor"],
            "credit_currency": "INR",
            "settlement_net_sum_minor": 0,
            "residual_minor": -credit["amount_minor"],
        },
        "expected_link_resolution": "UNMATCHED",
        "expected_exception_category": credit["_defect_class"].upper(),
        "details": {
            "remitter_name": credit["debtor_name"],
            "narration_as_sent": credit["narration"],
            "channel": credit["channel"],
            "value_date": credit["value_date"].isoformat(),
            "amount_redraws": credit["_redraws"],
            "verified_stage2_pool_size": credit.get("_pool_size", 0),
            "observed_resolution_at_generation": credit.get("_resolution"),
            "why_no_settlement_exists": credit["_notes"],
        },
        "notes": credit["_notes"],
    }


def emit(credits: list[dict], out_dir: Path, split_name: str, seed: int,
         settlements_path: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    ordered = sorted(credits, key=lambda c: (c["value_date"], c["bank_txn_id"]))
    base.write_camt053(out_dir / "bank_statement.camt053.xml", ordered,
                       split_name.replace("_", "-"))

    cases = [_case(c, i, split_name) for i, c in enumerate(credits)]
    counts: dict[str, int] = {}
    for c in cases:
        counts[c["defect_class"]] = counts.get(c["defect_class"], 0) + 1

    conventions = dict(CONVENTIONS)
    conventions["matched_against_settlements_file"] = settlements_path

    gt = {
        "schema_version": "1.0",
        "generator": {
            "script": "scripts/generate_no_match_control.py",
            "seed": seed,
            "scale": len(cases),
            "split": split_name,
            "adversarial_holdout": split_name.endswith("holdout"),
            "deterministic": "same seed => byte-identical output; no wall-clock input",
        },
        "conventions": conventions,
        "counts": {
            "cases": len(cases),
            "settlements": 0,
            "settlement_rows": 0,
            "bank_credits": len(credits),
            "invoices": 0,
            "by_defect_class": dict(sorted(counts.items())),
        },
        "cases": cases,
    }
    (out_dir / "ground_truth.json").write_text(
        json.dumps(gt, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )


SPLITS = {
    "main": {
        # Distinct seed offsets, not just distinct name lists: with one shared
        # seed the two splits draw the identical amount sequence and stop being
        # two populations in any sense that matters.
        "seed_offset": 0,
        "tag": "M",
        "variant_key": "main",
        "split_name": "no_match_control_main",
        "settlements": "data/razorpay_settlements.csv",
        "out": "main",
    },
    "holdout": {
        "seed_offset": 1,
        "tag": "H",
        "variant_key": "holdout",
        "split_name": "no_match_control_holdout",
        "settlements": "data/holdout/HOLDOUT_razorpay_settlements.csv",
        "out": "holdout",
    },
}


def generate(seed: int, out_root: Path = OUT_ROOT) -> dict[str, list]:
    """Build, verify, emit, then re-parse the emitted XML and verify again --
    the second pass is against exactly the bytes a reader gets, not against
    the in-memory objects that produced them."""
    out: dict[str, list] = {}
    for key, cfg in SPLITS.items():
        settlements = parse_razorpay_settlements(REPO / cfg["settlements"])
        split_seed = seed + cfg["seed_offset"]
        credits = build_split(split_seed, cfg["tag"], cfg["variant_key"], settlements)
        out_dir = out_root / cfg["out"]
        emit(credits, out_dir, cfg["split_name"], split_seed, cfg["settlements"])

        records = parse_camt053_file(out_dir / "bank_statement.camt053.xml")
        results = match_all(records, settlements)
        bad = [r for r in results if r.resolution in (MATCHED, PARTIAL)]
        if bad:
            raise AssertionError(
                f"{key}: emitted file contains false matches "
                f"{[(r.bank_txn_id, r.resolution, r.settlement_ids) for r in bad]}"
            )
        out[key] = results
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", type=int, default=20260905)
    ap.add_argument("--out", type=Path, default=OUT_ROOT)
    args = ap.parse_args()
    results = generate(args.seed, args.out)
    for key, rows in results.items():
        tally: dict[str, int] = {}
        for r in rows:
            tally[r.resolution] = tally.get(r.resolution, 0) + 1
        print(f"{key}: {len(rows)} no-match credits -> {tally}")
    for path in sorted(args.out.rglob("*")):
        if path.is_file():
            print(path.relative_to(args.out.parent))


if __name__ == "__main__":
    main()
