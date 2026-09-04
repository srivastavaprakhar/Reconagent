"""Tests for Stage 4 (hybrid fuzzy text matching).

`data/ground_truth.json` is read HERE (Stage 4's own sanctioned training
source -- see `reconagent/fuzzy.py`'s module docstring).
`stress_test/ground_truth.json` is read HERE ONLY to grade Stage 4's output;
`test_fuzzy_module_never_reads_stress_or_holdout_ground_truth` below asserts
the module itself never opens it (or `data/holdout/ground_truth.json`).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest

from reconagent import fuzzy as F
from reconagent import match as M
from reconagent.camt053 import parse_camt053_file
from reconagent.invoices import parse_invoice_ledger
from reconagent.probabilistic import match_with_tier2
from reconagent.razorpay import parse_razorpay_settlements

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"
STRESS = REPO / "stress_test"


@pytest.fixture(scope="module")
def model() -> F.FuzzyStage4Model:
    return F.train_stage4_model()


def _load(d: Path, prefix: str = ""):
    settlements = parse_razorpay_settlements(d / f"{prefix}razorpay_settlements.csv")
    credits = parse_camt053_file(d / f"{prefix}bank_statement.camt053.xml")
    invoices = parse_invoice_ledger(d / f"{prefix}invoice_ledger.csv")
    return settlements, credits, invoices


# ---------------------------------------------------------------------------
# Production code never reads the grading answer keys.
# ---------------------------------------------------------------------------


def test_fuzzy_module_never_reads_stress_or_holdout_ground_truth():
    """Same approach as
    tests/test_probabilistic.py::test_probabilistic_module_never_reads_stress_or_holdout_ground_truth:
    a source-level regex scan for a `ground_truth.json`-naming string literal
    that mentions either forbidden split."""
    source = (REPO / "reconagent" / "fuzzy.py").read_text()
    string_literals = re.findall(r"""(['"])((?:(?!\1).)*ground_truth\.json)\1""", source)
    offending = [
        literal for _, literal in string_literals if "stress_test" in literal or "holdout" in literal.lower()
    ]
    assert not offending, offending
    # And the one ground_truth.json this module DOES read is unconditionally
    # relative to a caller-supplied data_dir, not hardcoded to a split.
    assert any(literal == "ground_truth.json" for _, literal in string_literals)


# ---------------------------------------------------------------------------
# Threshold / primary-floor derivation, reproduced from data/.
# ---------------------------------------------------------------------------


def test_threshold_derivation_is_reproducible_from_main_labels(model):
    """Re-derive the two gates the module docstring documents (THRESHOLD
    DERIVATION, SECOND GATE: PRIMARY_SCORE_FLOOR) and confirm the constants
    land where documented."""
    assert model.threshold == F.DEFAULT_MATCH_THRESHOLD == Decimal("0.032655")
    assert model.primary_floor == F.PRIMARY_SCORE_FLOOR == Decimal("0.660730")

    pop = model.training_population
    assert pop["blocked_candidate_pairs"] == 30704
    assert pop["known_positive_pairs"] == 175
    assert pop["known_negative_pairs"] == 30529
    assert pop["known_negative_max_rrf"] == 0.032522
    assert pop["known_positive_primary_score_gap_lo"] == 0.422075
    assert pop["known_positive_primary_score_gap_hi"] == 0.66073

    # DEFAULT_MATCH_THRESHOLD sits strictly above the known-negative RRF
    # ceiling (zero false matches on data/'s own blocked candidate space).
    assert Decimal(str(pop["known_negative_max_rrf"])) < model.threshold
    # PRIMARY_SCORE_FLOOR sits at the upper edge of the natural gap in
    # data/'s own known-positive primary_score population.
    assert Decimal(str(pop["known_positive_primary_score_gap_hi"])) == model.primary_floor


# ---------------------------------------------------------------------------
# data/: Tier 1 + Stage 3 already resolve everything -- Stage 4 must be a
# provable no-op, same discipline match_with_tier2 holds toward match_all.
# ---------------------------------------------------------------------------


def test_full_cascade_reproduces_main_set_exactly(model):
    settlements, credits, invoices = _load(DATA)
    results = F.match_with_full_cascade(credits, settlements, invoices, fuzzy_model=model)
    assert len(results) == 152
    counts = Counter(r.resolution for r in results)
    assert counts[M.MATCHED] == 144
    assert counts[M.PARTIAL] == 8
    assert counts[M.UNMATCHED] == 0
    assert counts[M.AMBIGUOUS] == 0
    assert counts[M.TIE_AMBIGUOUS] == 0
    # Tier 1 + Stage 3 leave nothing deferred on this split, so Stage 4
    # never even runs against it -- no FuzzyMatchResult anywhere in output.
    assert not any(type(r).__name__ == "FuzzyMatchResult" for r in results)


# ---------------------------------------------------------------------------
# stress_test: the population this stage was actually built for.
# ---------------------------------------------------------------------------


def test_stage3_alone_leaves_25_deferred_on_stress_test():
    """Sanity-check the starting point this module's own docstring and the
    task brief both cite, before Stage 4 gets a turn."""
    settlements, credits, invoices = _load(STRESS)
    results = match_with_tier2(credits, settlements, invoices=invoices)
    assert len(results) == 40
    deferred = [r for r in results if r.resolution != M.MATCHED]
    assert len(deferred) == 25


def test_full_cascade_against_stress_test(model):
    """Report -- and assert something meaningful about -- how the FULL
    cascade (Tier 1 + Stage 3 + Stage 4) does on stress_test/, and in
    particular how much Stage 4 adds on top of what Stage 3 alone resolves.
    The false-match assertion is the one that matters most; the recall
    number is reported honestly, not tuned to hit a target."""
    settlements, credits, invoices = _load(STRESS)
    gt = json.loads((STRESS / "ground_truth.json").read_text())
    truth = {c["expected_link"]["bank_txn_id"]: c["expected_link"]["covers_settlement_ids"][0] for c in gt["cases"]}
    cat_by_txn = {c["expected_link"]["bank_txn_id"]: c["defect_class"] for c in gt["cases"]}

    results = F.match_with_full_cascade(credits, settlements, invoices, fuzzy_model=model)
    assert len(results) == 40

    resolved_correct = resolved_wrong = deferred = 0
    stage4_correct = stage4_wrong = 0
    correct_by_cat: Counter = Counter()
    wrong_by_cat: Counter = Counter()
    deferred_by_cat: Counter = Counter()
    for r in results:
        cat = cat_by_txn[r.bank_txn_id]
        expected_settlement_id = truth[r.bank_txn_id]
        is_stage4 = type(r).__name__ == "FuzzyMatchResult"
        if r.resolution == M.MATCHED:
            if expected_settlement_id in r.settlement_ids:
                resolved_correct += 1
                correct_by_cat[cat] += 1
                if is_stage4:
                    stage4_correct += 1
            else:
                resolved_wrong += 1
                wrong_by_cat[cat] += 1
                if is_stage4:
                    stage4_wrong += 1
        else:
            deferred += 1
            deferred_by_cat[cat] += 1

    print(
        f"\nstress_test/ full cascade: resolved_correct={resolved_correct} "
        f"resolved_wrong={resolved_wrong} deferred={deferred} (of 40); "
        f"Stage 4 itself: correct={stage4_correct} wrong={stage4_wrong} "
        f"(of the 25 credits Stage 3 alone left deferred)\n"
        f"correct by category: {dict(correct_by_cat)}\n"
        f"wrong by category: {dict(wrong_by_cat)}\n"
        f"deferred by category: {dict(deferred_by_cat)}"
    )

    assert resolved_correct + resolved_wrong + deferred == 40
    # The property that matters most, not optional: Stage 4 must not
    # confidently assert a WRONG settlement anywhere across the 40 cases
    # (spec section 9: honest abstention beats a false match every time).
    assert resolved_wrong == 0
    assert stage4_wrong == 0
    # Stage 4 should resolve a real, non-trivial slice of what Stage 3 left
    # on the table -- not necessarily much (module docstring: the primary
    # score floor is a much tighter, more conservative bar than Stage 3's
    # own probability threshold), but more than zero.
    assert stage4_correct >= 1
    # legal_vs_trading_name is the category Stage 3's name-similarity
    # approach structurally cannot touch at all (module docstring). Report
    # -- don't assert a target for -- how much of it the full cascade picks
    # up; the honest expectation, per this module's own docstring, is "not
    # much, if any": the dense embedding here is a corpus-local LSA model,
    # not a semantic one, so a legal name and an unrelated trading name
    # sharing no distinctive word still won't clear PRIMARY_SCORE_FLOOR.
    print(f"legal_vs_trading_name: {correct_by_cat.get('legal_vs_trading_name', 0)}/8 resolved by the full cascade")


def test_stage4_declines_are_genuinely_below_threshold(model):
    """Every deferred stress_test credit Stage 4 actually scored really did
    fail one of its two acceptance gates -- Stage 4 isn't silently omitting
    a credit it could have resolved."""
    settlements, credits, invoices = _load(STRESS)
    tier2 = match_with_tier2(credits, settlements, invoices=invoices)
    by_credit = {c.record_id: c for c in credits}
    consumed: set[str] = set()
    for r in tier2:
        if r.resolution in (M.MATCHED, M.PARTIAL):
            consumed.update(r.settlement_ids)
    deferred_credits = [by_credit[r.bank_txn_id] for r in tier2 if r.resolution != M.MATCHED]
    open_settlements = [s for s in settlements if s.record_id not in consumed]

    stage4_direct = F.resolve_stage4(deferred_credits, open_settlements, invoices, model)
    declined = [r for r in stage4_direct.values() if r.resolution == M.UNMATCHED]
    assert declined
    for r in declined:
        if r.candidates_considered == 0:
            continue
        primary_score = r.tfidf_cosine * Decimal("0.6") + r.jaro_winkler * Decimal("0.4")
        assert r.combined_score < model.threshold or primary_score < model.primary_floor


# ---------------------------------------------------------------------------
# Money-path discipline.
# ---------------------------------------------------------------------------


def test_no_float_on_money_path_fields(model):
    settlements, credits, invoices = _load(STRESS)
    results = F.match_with_full_cascade(credits, settlements, invoices, fuzzy_model=model)
    fuzzy_results = [r for r in results if type(r).__name__ == "FuzzyMatchResult"]
    assert fuzzy_results, "expected at least one Stage 4 result to actually check"
    for r in fuzzy_results:
        assert isinstance(r.credit_amount_minor, int) and not isinstance(r.credit_amount_minor, bool)
        assert isinstance(r.settlement_net_sum_minor, int) and not isinstance(r.settlement_net_sum_minor, bool)
        assert isinstance(r.residual_minor, int) and not isinstance(r.residual_minor, bool)
        for score_field in (r.combined_score, r.tfidf_cosine, r.jaro_winkler, r.dense_score, r.threshold):
            assert isinstance(score_field, Decimal)
