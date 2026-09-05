"""Tests for the no-match control population (`no_match_control/`).

This dataset exists to answer one question the existing headline cannot:
every linked case in `data/` and `data/holdout/` is ground-truth MATCHED or
PARTIAL, so 152/152 is a statement about *answerable* credits only. These
tests assert the claim the dataset is supposed to support -- that the
matcher declines, per credit, by name -- and, just as importantly, that the
existing headline numbers did not move.

The resolutions below are asserted individually and exactly, not with a
"resolution is not None" shrug that would pass whatever happened. If a
future change to reconagent.match turns one of these into MATCHED or
PARTIAL, that is a false match on money with no settlement behind it, and
this file is where it gets caught.
"""

from __future__ import annotations

import filecmp
import importlib.util
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from reconagent.camt053 import parse_camt053_file
from reconagent.eval import (
    compute_metrics,
    load_holdout,
    load_main,
    no_match_control_summary,
)
from reconagent.match import AMBIGUOUS, MATCHED, PARTIAL, TIE_AMBIGUOUS, UNMATCHED, match_all
from reconagent.razorpay import parse_razorpay_settlements

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "generate_no_match_control", REPO / "scripts" / "generate_no_match_control.py"
)
gen = importlib.util.module_from_spec(_spec)
sys.modules["generate_no_match_control"] = gen
_spec.loader.exec_module(gen)

SEED = 20260905
ROOT = REPO / "no_match_control"
CAMT_NS = "{urn:iso:std:iso:20022:tech:xsd:camt.053.001.02}"
CORRECT_REJECTIONS = (UNMATCHED, AMBIGUOUS, TIE_AMBIGUOUS)

# The observed, verified answer for every one of the twenty credits, asserted
# by name. Produced by running match_all against each split's own real
# settlement list (202 settlements for main, 100 for holdout, every one of
# them open because nothing here resolves at Stage 1). Zero MATCHED, zero
# PARTIAL: no false match anywhere in this population.
EXPECTED_RESOLUTIONS = {
    "main": {
        "NOMATCHM0001": TIE_AMBIGUOUS,
        "NOMATCHM0002": UNMATCHED,
        "NOMATCHM0003": TIE_AMBIGUOUS,
        "NOMATCHM0004": TIE_AMBIGUOUS,
        "NOMATCHM0005": TIE_AMBIGUOUS,
        "NOMATCHM0006": TIE_AMBIGUOUS,
        "NOMATCHM0007": TIE_AMBIGUOUS,
        "NOMATCHM0008": UNMATCHED,
        "NOMATCHM0009": TIE_AMBIGUOUS,
        "NOMATCHM0010": TIE_AMBIGUOUS,
    },
    "holdout": {
        "NOMATCHH0001": TIE_AMBIGUOUS,
        "NOMATCHH0002": TIE_AMBIGUOUS,
        "NOMATCHH0003": UNMATCHED,
        "NOMATCHH0004": TIE_AMBIGUOUS,
        "NOMATCHH0005": UNMATCHED,
        "NOMATCHH0006": TIE_AMBIGUOUS,
        "NOMATCHH0007": TIE_AMBIGUOUS,
        "NOMATCHH0008": UNMATCHED,
        "NOMATCHH0009": TIE_AMBIGUOUS,
        "NOMATCHH0010": TIE_AMBIGUOUS,
    },
}

SETTLEMENT_FILES = {
    "main": REPO / "data" / "razorpay_settlements.csv",
    "holdout": REPO / "data" / "holdout" / "HOLDOUT_razorpay_settlements.csv",
}

SPLITS = ["main", "holdout"]


def _truth(split: str) -> dict:
    return json.loads((ROOT / split / "ground_truth.json").read_text())


def _results(split: str) -> dict:
    credits = parse_camt053_file(ROOT / split / "bank_statement.camt053.xml")
    settlements = parse_razorpay_settlements(SETTLEMENT_FILES[split])
    return {r.bank_txn_id: r for r in match_all(credits, settlements)}


# --------------------------------------------------------------------------
# generator
# --------------------------------------------------------------------------


def test_generator_is_deterministic(tmp_path):
    """Same seed => byte-identical output, including the committed files."""
    gen.generate(SEED, tmp_path / "a")
    gen.generate(SEED, tmp_path / "b")
    for split in SPLITS:
        for name in ("bank_statement.camt053.xml", "ground_truth.json"):
            assert filecmp.cmp(
                tmp_path / "a" / split / name, tmp_path / "b" / split / name, shallow=False
            ), f"{split}/{name} differs between two runs at the same seed"
            assert filecmp.cmp(
                tmp_path / "a" / split / name, ROOT / split / name, shallow=False
            ), f"{split}/{name} does not match the committed file at seed {SEED}"


def test_the_two_splits_are_different_populations():
    """Distinct remitters and distinct amounts -- not the same ten cases twice."""
    main, holdout = _truth("main"), _truth("holdout")
    main_names = {c["details"]["remitter_name"] for c in main["cases"]}
    holdout_names = {c["details"]["remitter_name"] for c in holdout["cases"]}
    # One shared name is legitimate (the merchant's own bank posts interest to
    # both splits); everything else must differ.
    assert len(main_names & holdout_names) <= 1
    main_amounts = {c["expected_link"]["credit_amount_minor"] for c in main["cases"]}
    holdout_amounts = {c["expected_link"]["credit_amount_minor"] for c in holdout["cases"]}
    assert not (main_amounts & holdout_amounts)


def test_ten_distinct_reasons_per_split():
    """A reviewer should see ten different causes, not one case repeated."""
    for split in SPLITS:
        classes = [c["defect_class"] for c in _truth(split)["cases"]]
        assert len(classes) == 10
        assert len(set(classes)) == 10


# --------------------------------------------------------------------------
# schema / referential sanity
# --------------------------------------------------------------------------


@pytest.mark.parametrize("split", SPLITS)
def test_ground_truth_is_well_formed(split):
    truth = _truth(split)
    assert truth["schema_version"] == "1.0"
    assert truth["generator"]["script"] == "scripts/generate_no_match_control.py"
    assert truth["counts"]["cases"] == len(truth["cases"]) == 10
    assert truth["counts"]["bank_credits"] == 10

    seen_ids, seen_txns = set(), set()
    for case in truth["cases"]:
        assert case["expected_link_resolution"] == UNMATCHED
        assert case["expected_link"]["covers_settlement_ids"] == []
        assert case["settlement_ids"] == []
        assert case["payment_ids"] == []
        assert case["invoice_ids"] == []

        link = case["expected_link"]
        bid = link["bank_txn_id"]
        assert case["bank_txn_ids"] == [bid]
        assert bid not in seen_txns
        seen_txns.add(bid)
        assert case["case_id"] not in seen_ids
        seen_ids.add(case["case_id"])

        # Money is integer minor units. A float here is a bug, not a style
        # issue (CLAUDE.md), and `bool` is an int subclass so it is excluded.
        for key in ("credit_amount_minor", "settlement_net_sum_minor", "residual_minor"):
            assert type(link[key]) is int, f"{case['case_id']}.{key} is not an int"
        assert link["credit_currency"] == "INR"
        assert link["settlement_net_sum_minor"] == 0
        # Same sign convention reconagent.match uses for its own UNMATCHED.
        assert link["residual_minor"] == -link["credit_amount_minor"]
        assert case["notes"] and case["details"]["why_no_settlement_exists"]


@pytest.mark.parametrize("split", SPLITS)
def test_camt053_matches_ground_truth(split):
    """Real camt.053, and every ground-truth bank_txn_id is really in it."""
    path = ROOT / split / "bank_statement.camt053.xml"
    root = ET.parse(path).getroot()
    assert root.tag == f"{CAMT_NS}Document"

    refs = [e.text for e in root.iter(f"{CAMT_NS}NtryRef")]
    assert len(refs) == 10
    assert set(refs) == {c["expected_link"]["bank_txn_id"] for c in _truth(split)["cases"]}

    credits = parse_camt053_file(path)
    by_id = {c.record_id: c for c in credits}
    for case in _truth(split)["cases"]:
        link = case["expected_link"]
        credit = by_id[link["bank_txn_id"]]
        assert credit.amount_minor == link["credit_amount_minor"]
        assert credit.currency == "INR"
        assert credit.narration == case["details"]["narration_as_sent"]


@pytest.mark.parametrize("split", SPLITS)
def test_bank_txn_ids_cannot_collide_with_the_real_datasets(split):
    """No id here can be confused with, or shadow, one from data/."""
    real = {c.record_id for c in (load_main().credits + load_holdout().credits)}
    ours = {c["expected_link"]["bank_txn_id"] for c in _truth(split)["cases"]}
    assert not (real & ours)
    assert all(b.startswith("NOMATCH") for b in ours)


# --------------------------------------------------------------------------
# the actual claim
# --------------------------------------------------------------------------


@pytest.mark.parametrize("split", SPLITS)
def test_matcher_declines_every_no_match_credit(split):
    """The point of the whole dataset, asserted resolution by resolution
    against the split's real settlement pool."""
    results = _results(split)
    assert set(results) == set(EXPECTED_RESOLUTIONS[split])
    for bid, expected in EXPECTED_RESOLUTIONS[split].items():
        assert results[bid].resolution == expected, (
            f"{bid}: expected {expected}, got {results[bid].resolution} "
            f"on settlements {results[bid].settlement_ids}"
        )


@pytest.mark.parametrize("split", SPLITS)
def test_zero_false_matches(split):
    """The headline of this population. Asserted as an exact count, not a
    property that would pass at any value."""
    results = _results(split)
    false_matches = [r for r in results.values() if r.resolution in (MATCHED, PARTIAL)]
    assert false_matches == []
    assert len(false_matches) == 0
    assert all(r.resolution in CORRECT_REJECTIONS for r in results.values())


@pytest.mark.parametrize("split", SPLITS)
def test_credits_were_searched_against_a_real_non_empty_pool(split):
    """A credit declined because it had nothing to compare against proves
    nothing. Every Stage 2 decline here happened against a real pool of
    candidate settlements drawn from the split's own settlement file."""
    settlements = parse_razorpay_settlements(SETTLEMENT_FILES[split])
    assert len(settlements) >= 100
    for bid, r in _results(split).items():
        assert r.pool_size > 0, f"{bid} was declined against an empty pool"


# --------------------------------------------------------------------------
# the existing headline must not have moved
# --------------------------------------------------------------------------


def test_existing_main_headline_is_untouched():
    m = compute_metrics(load_main())
    assert (m.total_linked, m.correct, m.false_match, m.false_clear, m.tie_ambiguous) == (
        152, 152, 0, 0, 0
    )


def test_existing_holdout_headline_is_untouched():
    h = compute_metrics(load_holdout())
    assert (h.total_linked, h.correct, h.false_match, h.false_clear, h.tie_ambiguous) == (
        53, 50, 0, 0, 3
    )


def test_no_match_credits_are_absent_from_the_real_splits():
    """The new population never leaked into the old one."""
    for split in (load_main(), load_holdout()):
        ids = {c.record_id for c in split.credits}
        assert not any(i.startswith("NOMATCH") for i in ids)
        case_txns = {
            c["expected_link"]["bank_txn_id"] for c in split.truth["cases"]
        } - {None}
        assert not any(t.startswith("NOMATCH") for t in case_txns)


# --------------------------------------------------------------------------
# eval wiring
# --------------------------------------------------------------------------


def test_summary_is_well_formed_and_agrees_with_match_all():
    """The wiring must report what running match_all directly produces -- not
    a second, differently-computed number."""
    summary = no_match_control_summary()
    assert set(summary) == {"main", "holdout"}
    for split in SPLITS:
        s = summary[split]
        results = _results(split)
        assert s["total"] == 10 == len(s["cases"])
        assert s["false_matches"] == 0
        assert s["correctly_rejected"] == 10
        assert s["correctly_rejected"] + s["false_matches"] == s["total"]
        assert sum(s["by_resolution"].values()) == 10
        for case in s["cases"]:
            r = results[case["bank_txn_id"]]
            assert case["resolution"] == r.resolution
            assert case["settlement_ids"] == list(r.settlement_ids)
            assert case["expected_link_resolution"] == UNMATCHED
            assert case["false_match"] is False


def test_summary_accepts_preloaded_splits_without_changing_the_numbers():
    main, holdout = load_main(), load_holdout()
    assert no_match_control_summary(main, holdout) == no_match_control_summary()


def test_report_keeps_the_two_populations_separate():
    """The rendered section must not blend the denominators, and the headline
    metrics dict must not have absorbed the control population."""
    from reconagent.eval import _render_no_match_control_section

    main, holdout = load_main(), load_holdout()
    m, h = compute_metrics(main).__dict__, compute_metrics(holdout).__dict__
    control = no_match_control_summary(main, holdout)
    text = "\n".join(_render_no_match_control_section(control, m, h))

    assert "152/152 correct on answerable credits (unchanged)" in text
    assert "10/10 no-match credits correctly rejected, 0 false positives" in text
    assert "separate populations" in text
    # 162 would be the blended denominator; it must appear nowhere.
    assert "162" not in text
