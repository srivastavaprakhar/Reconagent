"""Tests for Stage 3 (probabilistic record linkage via Splink).

`data/ground_truth.json` is read HERE (Stage 3's own sanctioned training
source -- see `reconagent/probabilistic.py`'s module docstring).
`stress_test/ground_truth.json` and `data/holdout/ground_truth.json` are
read HERE ONLY to grade Stage 3's output; `test_probabilistic_module_never_
reads_stress_or_holdout_ground_truth` below asserts the module itself never
opens either.
"""

from __future__ import annotations

import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest

from reconagent import match as M
from reconagent import probabilistic as P
from reconagent.camt053 import parse_camt053_file
from reconagent.invoices import parse_invoice_ledger
from reconagent.razorpay import parse_razorpay_settlements

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"
STRESS = REPO / "stress_test"
HOLDOUT = REPO / "data" / "holdout"


@pytest.fixture(scope="module")
def model() -> P.SplinkStage3Model:
    return P.train_stage3_model()


def _load(d: Path, prefix: str = ""):
    settlements = parse_razorpay_settlements(d / f"{prefix}razorpay_settlements.csv")
    credits = parse_camt053_file(d / f"{prefix}bank_statement.camt053.xml")
    invoices = parse_invoice_ledger(d / f"{prefix}invoice_ledger.csv")
    return settlements, credits, invoices


# ---------------------------------------------------------------------------
# Production code never reads the grading answer keys.
# ---------------------------------------------------------------------------


def test_probabilistic_module_never_reads_stress_or_holdout_ground_truth():
    """The module's own docstring talks ABOUT stress_test/ and holdout/, in
    prose (backtick-quoted), to explain why it never reads their answer
    keys -- that must stay legal. What must never appear is an actual
    Python string literal (quote-delimited) naming one of those
    ground_truth.json files, which is what an `open`/`read_text` call
    would need."""
    import re

    source = (REPO / "reconagent" / "probabilistic.py").read_text()
    string_literals = re.findall(r"""(['"])((?:(?!\1).)*ground_truth\.json)\1""", source)
    offending = [
        literal for _, literal in string_literals if "stress_test" in literal or "holdout" in literal.lower()
    ]
    assert not offending, offending
    # And the one ground_truth.json this module DOES read is unconditionally
    # relative to a caller-supplied data_dir, not hardcoded to a split.
    assert any(literal == "ground_truth.json" for _, literal in string_literals)


# ---------------------------------------------------------------------------
# Training population and threshold derivation, reproduced from data/.
# ---------------------------------------------------------------------------


def test_training_population_matches_documented_composition(model):
    gt = json.loads((DATA / "ground_truth.json").read_text())
    single_pairs, all_pairs = P._positive_pairs_from_ground_truth(gt)

    assert len(single_pairs) == 140
    assert len(all_pairs) == 175
    assert model.training_population["single_settlement_m_training_pairs"] == 140
    assert model.training_population["blocked_candidate_pairs"] == 30704
    assert model.training_population["known_negative_pairs"] == 30529


def test_threshold_derivation_is_reproducible_from_main_labels(model):
    """Re-derive the separation the module docstring documents: the max
    match_probability among data/'s own known-negative pairs, versus the
    (constant) match_probability shared by its 132 clean single-settlement
    positives -- and confirm DEFAULT_MATCH_THRESHOLD sits strictly between
    them, giving zero false matches on every known negative while accepting
    every clean known positive."""
    settlements, credits, invoices = _load(DATA)
    gt = json.loads((DATA / "ground_truth.json").read_text())
    single_pairs, all_pairs = P._positive_pairs_from_ground_truth(gt)

    by_id, by_order = P._name_index(invoices)
    settlement_df = P._to_frame(settlements, is_settlement=True, name_by_id=by_id, name_by_order=by_order)
    credit_df = P._to_frame(credits, is_settlement=False, name_by_id=by_id, name_by_order=by_order)

    from splink import DuckDBAPI, Linker

    linker = Linker(
        [settlement_df, credit_df],
        model.fitted_settings,
        db_api=DuckDBAPI(),
        input_table_aliases=["settlement", "credit"],
    )
    pdf = linker.inference.predict(threshold_match_probability=0.0).as_pandas_dataframe()
    pdf = P._normalise_pairs(pdf)

    positive_pairs = {(sid, bid) for sid, bid in all_pairs}
    is_positive = pdf.apply(
        lambda row: (row["settlement_id"], row["credit_id"]) in positive_pairs, axis=1
    )
    negatives = pdf[~is_positive]
    neg_max = float(negatives["match_probability"].max())
    assert neg_max == pytest.approx(0.213390, abs=1e-5)

    single_positive_pairs = {(sid, bid) for sid, bid in single_pairs}
    is_single_positive = pdf.apply(
        lambda row: (row["settlement_id"], row["credit_id"]) in single_positive_pairs, axis=1
    )
    single_pos = pdf[is_single_positive]
    assert len(single_pos) == 140

    # 132 of the 140 are exact-amount matches ("clean"); the other 8 are
    # genuine partial-payment/EDPMS shortfalls that legitimately score near
    # zero (see module docstring) -- exclude them the same way the
    # docstring does, by amount comparison level, not by an arbitrary
    # probability cutoff.
    clean = single_pos[single_pos["gamma_amount"] == single_pos["gamma_amount"].max()]
    assert len(clean) == 132

    clean_pos_min = float(clean["match_probability"].min())
    assert clean_pos_min == pytest.approx(0.288721, abs=1e-5)

    assert neg_max < float(P.DEFAULT_MATCH_THRESHOLD) < clean_pos_min
    assert float(model.threshold) == float(P.DEFAULT_MATCH_THRESHOLD)


# ---------------------------------------------------------------------------
# Main set: Stage 3 must never even be invoked, and must reproduce Tier 1
# exactly.
# ---------------------------------------------------------------------------


def test_main_set_reproduces_tier1_exactly(model):
    settlements, credits, invoices = _load(DATA)
    gt = json.loads((DATA / "ground_truth.json").read_text())
    truth = {c["expected_link"]["bank_txn_id"]: c for c in gt["cases"] if c["expected_link"]["bank_txn_id"]}

    results = P.match_with_tier2(credits, settlements, invoices=invoices, splink_model=model)
    assert len(results) == 152

    # Every single result stayed a Tier 1 MatchResult -- Stage 3 was never
    # invoked because Tier 1 left nothing UNMATCHED/AMBIGUOUS/TIE_AMBIGUOUS.
    assert all(type(r).__name__ == "MatchResult" for r in results)

    correct = false_match = false_clear = 0
    for r in results:
        case = truth[r.bank_txn_id]
        expected = case["expected_link"]
        expected_ok = (
            r.resolution == case["expected_link_resolution"]
            and set(r.settlement_ids) == set(expected["covers_settlement_ids"])
        )
        if expected_ok:
            correct += 1
        else:
            if r.resolution in (M.MATCHED, M.PARTIAL) and not expected_ok:
                false_match += 1
            if r.resolution == M.UNMATCHED and case["expected_link_resolution"] != M.UNMATCHED:
                false_clear += 1

    assert correct == 152
    assert false_match == 0
    assert false_clear == 0


def test_match_with_tier2_is_a_no_op_on_a_set_tier1_already_resolves(model):
    settlements, credits, invoices = _load(DATA)
    tier1_only = M.match_all(credits, settlements)
    combined = P.match_with_tier2(credits, settlements, invoices=invoices, splink_model=model)
    assert [r.resolution for r in tier1_only] == [r.resolution for r in combined]
    assert [r.settlement_ids for r in tier1_only] == [r.settlement_ids for r in combined]


# ---------------------------------------------------------------------------
# stress_test: the population Stage 3 was actually built for.
# ---------------------------------------------------------------------------


def test_tier1_resolves_none_of_stress_test():
    settlements, credits, _ = _load(STRESS)
    results = M.match_all(credits, settlements)
    assert len(results) == 40
    assert all(r.resolution == M.UNMATCHED for r in results)


def test_stage3_against_stress_test(model):
    """Report -- and assert something meaningful about -- how Stage 3
    actually does on the population it was built for: how many of the 40
    it resolves correctly, how many it gets wrong, and how many it
    correctly declines. The false-match assertion below is the one that
    matters most; the recall number is reported honestly, not tuned to hit
    a target."""
    settlements, credits, invoices = _load(STRESS)
    gt = json.loads((STRESS / "ground_truth.json").read_text())
    truth = {
        c["expected_link"]["bank_txn_id"]: c["expected_link"]["covers_settlement_ids"][0]
        for c in gt["cases"]
    }

    results = P.match_with_tier2(credits, settlements, invoices=invoices, splink_model=model)
    assert len(results) == 40

    resolved_correct = resolved_wrong = deferred = 0
    for r in results:
        expected_settlement_id = truth[r.bank_txn_id]
        if r.resolution == M.MATCHED:
            if expected_settlement_id in r.settlement_ids:
                resolved_correct += 1
            else:
                resolved_wrong += 1
        else:
            deferred += 1

    print(
        f"\nstress_test/: resolved_correct={resolved_correct} "
        f"resolved_wrong={resolved_wrong} deferred={deferred} (of 40)"
    )

    assert resolved_correct + resolved_wrong + deferred == 40
    # The property that matters most: Stage 3 must not confidently assert a
    # WRONG settlement. Honest abstention beats a false match every time
    # (spec section 9) -- this is the assertion that would catch a
    # threshold tuned for recall at the expense of precision.
    assert resolved_wrong == 0
    # Stage 3 should resolve a real, non-trivial slice of what Tier 1
    # couldn't -- not zero (that would mean the stage does nothing) and not
    # everything (this stress set was built to be hard; declining the
    # genuinely ambiguous legal-vs-trading-name cases is correct, not a
    # failure).
    assert resolved_correct >= 10
    assert deferred >= 10


def test_stage3_declines_are_genuinely_below_threshold(model):
    """Every deferred stress_test credit's own best candidate really did
    score below threshold (or lose it to a more confident credit) -- Stage
    3 isn't silently omitting a credit it could have resolved."""
    settlements, credits, invoices = _load(STRESS)
    results = {r.bank_txn_id: r for r in P.match_with_tier2(credits, settlements, invoices=invoices, splink_model=model)}
    deferred = [r for r in results.values() if type(r).__name__ != "MatchResult"]
    # match_with_tier2 only returns a Tier1 MatchResult for a deferred
    # credit, so inspect resolve_stage3 directly for the declined ones.
    stage3_direct = P.resolve_stage3(credits, settlements, invoices, model)
    declined = [r for r in stage3_direct.values() if r.resolution == M.UNMATCHED]
    assert declined
    for r in declined:
        assert r.match_probability < model.threshold or r.candidates_considered == 0


# ---------------------------------------------------------------------------
# Holdout: grading only, never used to shape the model.
# ---------------------------------------------------------------------------


def test_stage3_does_not_regress_holdout(model):
    """Sanity check on the split this module is never allowed to tune
    against: Stage 3 must not introduce a false match Tier 1 didn't
    already have."""
    settlements, credits, invoices = _load(HOLDOUT, "HOLDOUT_")
    gt = json.loads((HOLDOUT / "HOLDOUT_ground_truth.json").read_text())
    truth = {
        c["expected_link"]["bank_txn_id"]: c["expected_link"]["covers_settlement_ids"]
        for c in gt["cases"]
        if c["expected_link"]["bank_txn_id"]
    }

    tier1 = M.match_all(credits, settlements)
    combined = P.match_with_tier2(credits, settlements, invoices=invoices, splink_model=model)

    false_matches = 0
    for r in combined:
        if r.resolution == M.MATCHED and r.bank_txn_id in truth:
            if set(r.settlement_ids) != set(truth[r.bank_txn_id]):
                false_matches += 1
    assert false_matches == 0

    resolutions = Counter(r.resolution for r in combined)
    tier1_resolutions = Counter(r.resolution for r in tier1)
    print(f"\nholdout/: tier1={dict(tier1_resolutions)} tier1+stage3={dict(resolutions)}")
