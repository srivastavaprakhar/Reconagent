#!/usr/bin/env python3
"""Stress-test dataset generator for the upcoming Tier 2 matching stages
(Fellegi-Sunter probabilistic linkage, hybrid fuzzy text matching -- spec
section 4, Stages 3 and 4).

This is NOT another realistic-proportions split like data/ or data/holdout/.
Every case here is built to defeat Tier 1's deterministic reference match and
bounded subset-sum solver (reconagent.match) on purpose: no settlement
reference (UTR / invoice id / order id / payment id) ever appears as a whole
token anywhere in the bank narration, and wherever an amount relationship
exists it is deliberately a few currency units off Tier 1's own amount
tolerance -- close enough for a probabilistic, per-field-weighted model to
have a signal (right week, close amount), not exact enough for a
minimum-residual solver to land on.

Framing, stated plainly because it belongs on the record: this dataset is
built proactively, for genuine ML depth and robustness against real-world
messiness the clean synthetic set doesn't exercise -- NOT because Tier 1's
own evaluation found a recall gap. It didn't (0.00% false-clear on both
existing splits). See reconagent-design-description.md section 4 and this
repo's eval reports.

The five messiness categories (spec: roughly even coverage, >=5-8 cases
each), what carries the identifying signal in every one of them:

  1. transliteration_variant        counterparty name, same underlying
                                     entity, two plausible ASCII spellings
  2. abbreviation_variant           counterparty name, abbreviated further
                                     than mangle()'s PRIVATE/LIMITED swap
  3. legal_vs_trading_name          counterparty name, a genuinely different
                                     string for the same entity (legal vs
                                     brand name), not a spelling variant
  4. ocr_typo_narration             free-text narration only (field 70 /
                                     RmtInf/Ustrd), character-level OCR/
                                     re-keying corruption; the name is clean
  5. invoice_description_mismatch   the invoice ledger's own description
                                     (the `notes` column -- see
                                     reconagent.invoices, which reads it as
                                     the invoice's narration) shares zero
                                     vocabulary with the settlement/bank side

Reuses reconagent-agnostic pieces of scripts/generate_synthetic.py (the
World accumulator, the real MT103/camt.053 emitters, the money/id helpers)
rather than duplicating them -- this script owns only what's qualitatively
different: the five defect-class case builders and their own narrower
amount-mismatch convention. See that module's docstring for the emitter and
World internals this one drives.

Money is integer minor units end to end, same discipline as the base
generator. Deterministic: one seeded random.Random, no wall-clock input.

Run:  .venv/bin/python scripts/generate_stress_test.py --seed 20260904 --out stress_test
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import generate_synthetic as base  # noqa: E402  (reuse World, emitters, money/id helpers)

RATE_Q = base.RATE_Q
CASES_PER_CATEGORY = 8

# Tier 1's own amount tolerance is 100 minor units (reconagent.match.
# AMOUNT_TOLERANCE_MINOR) -- documented there, not imported here, same
# decoupling the base generator already keeps between its own labelling
# tolerance and the matcher's. This range (0.20%-2.50% of the settlement
# net, floored at 300 minor units) is chosen to sit comfortably outside
# that 100-unit band on every case while staying a small fraction of the
# settlement -- "close", not arbitrary.
#
# There is deliberately no absolute ceiling here. An earlier version capped
# the delta at a flat 8,000 minor units, which -- against this dataset's
# settlement range (roughly INR 73K to INR 50L) -- meant even the *lowest*
# bps draw (0.20%) already exceeded the cap on every single case, so all 40
# deltas silently collapsed to the same constant value regardless of the
# random bps drawn. A flat absolute cap is the wrong shape for a
# percentage-based defect when settlement sizes span two orders of
# magnitude: 2.5% is equally "close, not arbitrary" whether the settlement
# is INR 73K or INR 50L. `DELTA_SANITY_CEIL_MINOR` below is a true
# backstop against a pathological draw, not a routine constraint -- it sits
# far above anything `DELTA_BPS_RANGE` can produce on this dataset's amounts.
DELTA_BPS_RANGE = (20, 250)
DELTA_FLOOR_MINOR = 300
DELTA_SANITY_CEIL_MINOR = 20_000_000

# Country/address/currency carrier for the wire side. Codes match
# generate_synthetic.SENDER_BANKS keys so add_credit's bank-name lookup
# resolves; currencies match generate_synthetic.FX_BASE so w.ref_rate finds
# a rate. Cultural plausibility of name-vs-country is not the point here --
# the amount/reference-token defeat is -- so buyers cycle through this list
# rather than being hand-paired per name.
BUYER_ADDR = [
    ("DE", "AN DER RENNBAHN 8", "FRANKFURT", "EUR"),
    ("US", "220 MADISON AVENUE", "NEW YORK NY", "USD"),
    ("GB", "22 OLD BROAD STREET", "LONDON", "GBP"),
    ("SG", "1 RAFFLES PLACE", "SINGAPORE", "SGD"),
    ("AE", "AL MAKTOUM STREET", "DUBAI", "AED"),
    ("NL", "PRINSENGRACHT 200", "AMSTERDAM", "EUR"),
]

# --- category 1: transliteration variants -------------------------------
# ASCII-only by design: the base generator's MT103/camt.053 emitters target
# conventional uppercase-ASCII SWIFT text, so a genuine diacritic (MULLER
# rendered as MU"LLER) would carry a character the real-format parsers
# aren't built to round-trip. Transliteration divergence is simulated with
# ASCII spelling variants only, per the task's own guidance on this point.
TRANSLITERATION_PAIRS = [
    ("MUELLER INDUSTRIES GMBH", "MULLER INDUSTRIES GMBH"),
    ("SCHMIDT PRAZISIONSTECHNIK AG", "SHMIDT PRAZISIONSTECHNIK AG"),
    ("YEVGENIY KOVALENKO TRADING LTD", "EVGENY KOVALENKO TRADING LTD"),
    ("DMITRIY SOKOLOV IMPORTS LLC", "DMITRI SOKOLOV IMPORTS LLC"),
    ("AL RASHEED GENERAL TRADING LLC", "AL RASHID GENERAL TRADING LLC"),
    ("MOHAMMED AL FARSI ENTERPRISES", "MUHAMMAD AL FARSI ENTERPRISES"),
    ("NIKOLAI VOLKOV LOGISTICS BV", "NIKOLAY VOLKOV LOGISTICS BV"),
    ("KRISTOF NOWACK EXPORT SP", "CHRISTOPH NOVAK EXPORT SP"),
]

# --- category 2: abbreviation beyond mangle()'s PRIVATE/LIMITED swap -----
ABBREVIATION_PAIRS = [
    ("NORTHWIND SOFTWARE INTERNATIONAL LLC", "NORTHWIND SOFTWARE INTL LLC"),
    ("APEX MANUFACTURING AND TRADING COMPANY", "APEX MFG & TRADING CO"),
    ("BRIDGEWORK ANALYTICS AND CONSULTING INCORPORATED", "BRIDGEWORK ANALYTICS & CONSULTING INC"),
    ("CASCADE INTERNATIONAL HOLDINGS COMPANY", "CASCADE INTL HOLDINGS CO"),
    ("KESTREL SYSTEMS AND SOLUTIONS LIMITED", "KESTREL SYS & SOLUTIONS LTD"),
    ("NORTHWIND SOFTWARE INCORPORATED", "NSI"),
    ("MERIDIAN INTERNATIONAL TRADING COMPANY", "MERIDIAN INTL TRADING CO"),
    ("HELIOS DATA AND ANALYTICS BV", "HELIOS D&A BV"),
]

# --- category 3: legal name vs trading name, genuinely different strings -
LEGAL_VS_TRADING_PAIRS = [
    ("AXIOM GLOBAL HOLDINGS PRIVATE LIMITED", "AXIOM TRADING CO"),
    ("BRIGHTPATH VENTURES PRIVATE LIMITED", "GLOWROUTE EXPORTS"),
    ("STERLING MERCANTILE CORPORATION", "STERLING GOODS"),
    ("VERTEX INDUSTRIAL SUPPLIES LLC", "IRONPEAK SUPPLIES"),
    ("PRIME HORIZON ENTERPRISES LTD", "HORIZON RETAIL GROUP"),
    ("OCEANIC LOGISTICS HOLDINGS BV", "BLUEWATER SHIPPING"),
    ("GRANITE PEAK MANUFACTURING INC", "SUMMIT WORKS"),
    ("EMERALD COAST TRADING LLC", "COASTLINE IMPORTS"),
]

# --- category 5: invoice description shares zero tokens with the wire ----
# (category 4 -- OCR typos -- reuses generate_synthetic.FOREIGN_BUYERS
# directly below rather than its own name list, since the point there is a
# clean, unchanged name with a corrupted narration.)
INVOICE_DESCRIPTION_PAIRS = [
    ("ZENITH CONSULTING GROUP", "Q3 SaaS renewal - annual seat license, 24 seats"),
    ("PALLAS ROBOTICS LTD", "Precision servo actuator batch, custom firmware calibration"),
    ("TIDEWATER FISHERIES CORP", "Chilled catch shipment, cold-chain logistics surcharge included"),
    ("SOLSTICE ENERGY PARTNERS", "Rooftop solar array maintenance contract, quarterly visit"),
    ("IVORYLINE FURNITURE CO", "Bespoke office furniture set, walnut finish, 12 desks"),
    ("REDSHIFT ANALYTICS INC", "Predictive-maintenance dashboard subscription, enterprise tier"),
    ("MARROW BIOSCIENCES LLC", "Reagent kit restock, cold storage handling fee"),
    ("COPPERLEAF DESIGN STUDIO", "Brand identity refresh, logo and packaging redesign"),
]

# --- category 4: OCR-style character corruption in free text only --------
_OCR_CONFUSION = {
    "O": "0", "0": "O", "I": "1", "1": "I", "L": "1", "S": "5", "5": "S",
    "B": "8", "8": "B", "Z": "2", "2": "Z",
}
_QWERTY_ADJACENT = {
    "A": "S", "S": "D", "D": "F", "F": "G", "R": "T", "T": "Y", "E": "W",
    "N": "M", "M": "N", "C": "V", "V": "B", "U": "I",
}


def ocr_mangle(rng: random.Random, text: str) -> str:
    """Character-level corruption typical of OCR/manual re-keying: a digit/
    letter confusion pair, an adjacent-QWERTY-key substitution, a dropped or
    doubled character, and rn<->m. Applied to free text only -- never to a
    structured field (spec: this is specifically the Stage 4 problem)."""
    out = text.replace("rn", "m") if rng.random() < 0.5 else text
    chars = list(out)
    for _ in range(rng.randint(2, 4)):
        if not chars:
            break
        i = rng.randrange(len(chars))
        op = rng.choice(["confuse", "adjacent", "drop", "dup"])
        c = chars[i].upper()
        if op == "confuse" and c in _OCR_CONFUSION:
            chars[i] = _OCR_CONFUSION[c]
        elif op == "adjacent" and c in _QWERTY_ADJACENT:
            chars[i] = _QWERTY_ADJACENT[c]
        elif op == "drop" and len(chars) > 8:
            del chars[i]
        elif op == "dup":
            chars.insert(i, chars[i])
    return "".join(chars)


# ====================================================================================
# case construction
# ====================================================================================


def _delta_minor(rng: random.Random, net_minor: int) -> int:
    """A signed amount mismatch: outside Tier 1's tolerance, small relative
    to the settlement net -- "close", never exact."""
    bps = rng.randint(*DELTA_BPS_RANGE)
    delta = max(DELTA_FLOOR_MINOR, base.pct_minor(net_minor, bps, 10_000))
    delta = min(delta, DELTA_SANITY_CEIL_MINOR)
    return delta * rng.choice([1, -1])


def _settle(w: base.World, *, buyer_name: str, country: str, ccy: str):
    rng = w.rng
    d = base.PERIOD_START + timedelta(days=rng.randint(3, 26))
    foreign_minor = rng.randrange(80_000, 60_00_000)
    ref = w.ref_rate(ccy, d)
    dev = Decimal(rng.randint(-2000, 2000)).scaleb(-2)
    applied = (ref * (Decimal(1) + dev / 10_000)).quantize(RATE_Q, rounding=ROUND_HALF_UP)
    base_minor = base.to_base_minor(foreign_minor, applied)
    inv = w.add_invoice(
        currency=ccy, amount_minor=foreign_minor,
        issue_date=d - timedelta(days=rng.randint(2, 15)),
        customer=buyer_name, country=country, export=True,
    )
    st = w.add_settlement(
        invoice=inv, gross_minor=foreign_minor, settled_at=d, international=True,
        conversion_rate=applied, base_minor=base_minor,
    )
    return inv, st, d, applied


def _emit_case(
    w: base.World, *, category: str, buyer: tuple[str, str, str, str],
    true_name: str, wire_name: str, narration: str, clean_signal: str,
    notes_override: str | None = None, category_details: dict | None = None,
) -> dict:
    country, addr, city, ccy = buyer
    inv, st, d, applied = _settle(w, buyer_name=true_name, country=country, ccy=ccy)
    if notes_override is not None:
        inv["notes"] = notes_override

    delta = _delta_minor(w.rng, st["net_minor"])
    credit_amount = st["net_minor"] - delta
    cr = w.add_credit(
        value_date=d, inr_minor=credit_amount, narration=narration,
        debtor_name=wire_name, debtor_addr=(addr, city, country),
        debtor_account=f"{country}{w.rng.randrange(10**16, 10**17)}",
        swift=True, instructed_ccy=ccy, instructed_minor=inv["invoice_amount_minor"],
        exchange_rate=applied, purpose_code=inv["purpose_code"],
    )
    residual = st["net_minor"] - credit_amount

    details = {
        "category": category,
        "true_counterparty_name": true_name,
        "wire_counterparty_name": wire_name,
        "narration_as_sent": narration,
        "clean_signal_before_degradation": clean_signal,
        "amount_delta_minor": residual,
        "reason_tier1_cannot_resolve": (
            "No settlement reference (UTR / invoice id / order id / payment id) "
            "appears as a whole token anywhere in the narration, so Stage 1's "
            "deterministic reference match has nothing to key on and defers. "
            "The credited amount misses the settlement net by more than Stage "
            "1/2's amount tolerance, so Stage 2's minimum-residual subset-sum "
            "search does not land on it either."
        ),
        **(category_details or {}),
    }
    return w.add_case(
        category,
        settlement_ids=[st["settlement_id"]], payment_ids=[st["payment_id"]],
        bank_txn_ids=[cr["bank_txn_id"]], invoice_ids=[inv["invoice_id"]],
        expected_link={
            "bank_txn_id": cr["bank_txn_id"],
            "covers_settlement_ids": [st["settlement_id"]],
            "credit_amount_minor": credit_amount,
            "credit_currency": "INR",
            "settlement_net_sum_minor": st["net_minor"],
            "residual_minor": residual,
        },
        expected_link_resolution="MATCHED",
        expected_exception_category=category.upper(),
        details=details,
        notes=f"Stress-test case ({category}): {clean_signal}",
    )


def build(seed: int) -> base.World:
    rng = random.Random(seed)
    w = base.World(rng, "stress_test", harden=False)
    w.build_fx()

    idx = 0

    def buyer_for() -> tuple[str, str, str, str]:
        nonlocal idx
        b = BUYER_ADDR[idx % len(BUYER_ADDR)]
        idx += 1
        return b

    for true_name, wire_name in TRANSLITERATION_PAIRS:
        _emit_case(
            w, category="transliteration_variant", buyer=buyer_for(),
            true_name=true_name, wire_name=wire_name,
            narration=f"/RFB/EXPORT PROCEEDS {wire_name}",
            clean_signal=(
                f"Same legal entity as '{true_name}'; the wire spells it "
                f"'{wire_name}', a different but equally plausible ASCII "
                "transliteration of the same underlying name."
            ),
        )

    for true_name, wire_name in ABBREVIATION_PAIRS:
        _emit_case(
            w, category="abbreviation_variant", buyer=buyer_for(),
            true_name=true_name, wire_name=wire_name,
            narration=f"/RFB/EXPORT PROCEEDS {wire_name}",
            clean_signal=(
                f"Same counterparty as '{true_name}'; the wire abbreviates it "
                f"to '{wire_name}' -- corporate-suffix and multi-word "
                "abbreviation beyond what the main generator's mangle() covers."
            ),
        )

    for legal_name, trading_name in LEGAL_VS_TRADING_PAIRS:
        _emit_case(
            w, category="legal_vs_trading_name", buyer=buyer_for(),
            true_name=legal_name, wire_name=trading_name,
            narration=f"/RFB/EXPORT PROCEEDS {trading_name}",
            clean_signal=(
                f"The settlement/invoice side carries the buyer's legal name "
                f"'{legal_name}'; the wire carries their trading name "
                f"'{trading_name}' instead -- a genuinely different string for "
                "the same entity, not a spelling variant."
            ),
        )

    for name, addr, city, country, ccy in base.FOREIGN_BUYERS:
        clean = f"/RFB/EXPORT PROCEEDS {name}"
        corrupted = ocr_mangle(w.rng, clean)
        _emit_case(
            w, category="ocr_typo_narration", buyer=(country, addr, city, ccy),
            true_name=name, wire_name=name, narration=corrupted,
            clean_signal=(
                f"Counterparty name is clean and unchanged ('{name}' on both "
                f"sides); the free-text narration is OCR/re-keying-corrupted "
                f"from '{clean}' to '{corrupted}'."
            ),
            category_details={"clean_narration_before_ocr": clean},
        )

    for company, notes in INVOICE_DESCRIPTION_PAIRS:
        buyer = buyer_for()
        _emit_case(
            w, category="invoice_description_mismatch", buyer=buyer,
            true_name=company, wire_name=company,
            narration=f"/RFB/EXPORT PROCEEDS {company}",
            notes_override=notes,
            clean_signal=(
                f"Counterparty name is clean and unchanged ('{company}' on "
                "both sides); the invoice ledger's own description "
                f"('{notes}') shares zero tokens with the settlement/wire "
                "text ('EXPORT PROCEEDS ...'), so a matcher over-relying on "
                "invoice-description overlap has nothing to key on either."
            ),
            category_details={"invoice_description": notes},
        )

    return w


# ====================================================================================
# emission -- same schema as generate_synthetic.emit(), single unsplit file set
# ====================================================================================

CONVENTIONS = {
    "purpose": (
        "Stress test for Tier 2 (spec section 4, Stages 3-4: Fellegi-Sunter "
        "probabilistic linkage and hybrid fuzzy text matching), built "
        "proactively for genuine ML depth and robustness against real-world "
        "messiness -- not because Tier 1's own evaluation found a recall "
        "gap needing it. It didn't (0.00% false-clear on both data/ and "
        "data/holdout/, once genuine subset-sum ties are separated from "
        "misses). Every case here is constructed so Tier 1's deterministic "
        "reference match and bounded subset-sum solver (reconagent.match) "
        "have nothing exact to land on: no settlement reference appears as "
        "a whole token in the narration, and any amount relationship is "
        "deliberately outside Tier 1's amount tolerance. The identifying "
        "signal lives in the counterparty name and free-text narration."
    ),
    "money": (
        "Every *_minor field in this file is an integer count of currency "
        "minor units (paise for INR, cents for USD/EUR/GBP/SGD/AED). Never "
        "a float. The emitted CSV / MT103 / camt.053 carry the same values "
        "as exact fixed-point decimal strings."
    ),
    "not_a_realistic_proportions_dataset": (
        "Unlike data/ and data/holdout/, this is not weighted toward clean "
        "matches. Every case is deliberately hard; do not compute a "
        "production-style match-rate expectation from it."
    ),
    "case_fields": {
        "case_id": "stable id, unique within this dataset",
        "defect_class": (
            "one of transliteration_variant, abbreviation_variant, "
            "legal_vs_trading_name, ocr_typo_narration, "
            "invoice_description_mismatch"
        ),
        "split": "stress_test",
        "settlement_ids": "every settlement_id in razorpay_settlements.csv this case involves",
        "payment_ids": "every entity_id (pay_) this case involves",
        "bank_txn_ids": "every NtryRef in the camt.053 (and TxId) this case involves",
        "invoice_ids": "every invoice_id in invoice_ledger.csv this case involves",
        "expected_link": (
            "the correct linkage a Tier 2 matcher should recover: which bank "
            "credit covers which settlement, the credit amount, the "
            "settlement net, and the residual between them"
        ),
        "expected_link_resolution": "MATCHED for every case in this dataset",
        "expected_exception_category": "the defect_class, upper-cased",
        "details": (
            "class-specific: the true (pre-degradation) name/text, the "
            "degraded/alternate name/text actually on the wire, why Tier 1 "
            "cannot resolve it, and the amount delta"
        ),
        "notes": "plain-English statement of what the case simulates",
    },
    "period": [base.PERIOD_START.isoformat(), base.PERIOD_END.isoformat()],
}


def emit(w: base.World, out_dir: Path, seed: int) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    credits = sorted(w.credits, key=lambda c: (c["value_date"], c["bank_txn_id"]))
    base.write_csv(out_dir / "razorpay_settlements.csv", base.SETTLEMENT_COLUMNS, w.settlement_rows)
    base.write_csv(out_dir / "invoice_ledger.csv", base.INVOICE_COLUMNS, w.invoices)
    base.write_fx_csv(out_dir / "fx_reference_rates.csv", w.fx)
    base.write_mt103(out_dir / "bank_statement.mt103", credits)
    base.write_camt053(out_dir / "bank_statement.camt053.xml", credits, w.split)

    counts: dict[str, int] = {}
    for c in w.cases:
        counts[c["defect_class"]] = counts.get(c["defect_class"], 0) + 1

    gt = {
        "schema_version": "1.0",
        "generator": {
            "script": "scripts/generate_stress_test.py",
            "seed": seed,
            "scale": len(w.cases),
            "split": "stress_test",
            "adversarial_holdout": False,
            "deterministic": "same seed => byte-identical output; no wall-clock input",
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
    (out_dir / "ground_truth.json").write_text(
        json.dumps(gt, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )


def generate(seed: int, out_dir: Path) -> base.World:
    w = build(seed)
    emit(w, out_dir, seed)
    return w


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", type=int, default=20260904)
    ap.add_argument("--out", type=Path, default=REPO / "stress_test")
    args = ap.parse_args()
    w = generate(args.seed, args.out)
    print(f"generated {len(w.cases)} stress-test cases in {args.out}")
    for path in sorted(args.out.rglob("*")):
        if path.is_file():
            print(path.relative_to(args.out.parent))


if __name__ == "__main__":
    main()
