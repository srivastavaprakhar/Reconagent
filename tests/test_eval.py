"""Tests for the evaluation harness (spec section 9).

Two things get checked, deliberately in this order:

1. The metric definitions themselves, on small hand-built fixtures where the
   right answer is computed by hand. This matters more than the end-to-end
   run -- a metric wrong in the same direction as the code that produced it
   looks fine end-to-end and wrong everywhere else.
2. A known-good run against data/, reproducing the numbers independently
   verified by the orchestrator, plus the mutation test and the output-path
   guard.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from reconagent import eval as E
from reconagent.match import AMBIGUOUS, MATCHED, PARTIAL, UNMATCHED, MatchResult


def _result(resolution: str, settlement_ids: tuple[str, ...], confidence: str = "0.9") -> MatchResult:
    return MatchResult(
        bank_txn_id="B1",
        stage="stage1_deterministic",
        resolution=resolution,
        settlement_ids=settlement_ids,
        credit_amount_minor=1000,
        settlement_net_sum_minor=1000,
        residual_minor=0,
        confidence=Decimal(confidence),
        reason="test fixture",
    )


def _case(resolution: str, covers: list[str], bank_txn_id: str = "B1") -> dict:
    return {
        "case_id": "T1",
        "defect_class": "clean_match",
        "expected_link": {"bank_txn_id": bank_txn_id, "covers_settlement_ids": covers},
        "expected_link_resolution": resolution,
    }


# --------------------------------------------------------------------------
# 1. classify() -- the metric definition, on hand-built fixtures
# --------------------------------------------------------------------------


def test_classify_correct_match():
    r = _result(MATCHED, ("setl_A",))
    c = _case(MATCHED, ["setl_A"])
    assert E.classify(r, c) == "correct"


def test_classify_false_match_wrong_subset():
    r = _result(MATCHED, ("setl_B",))  # wrong settlement
    c = _case(MATCHED, ["setl_A"])
    assert E.classify(r, c) == "false_match"


def test_classify_false_clear_system_said_unmatched():
    r = _result(UNMATCHED, ())
    c = _case(MATCHED, ["setl_A"])
    assert E.classify(r, c) == "false_clear"


def test_classify_ambiguous_counted_as_unresolved_not_matched():
    """AMBIGUOUS is the matcher's own vocabulary for candidates-with-no-
    verdict. It must never be scored as a match, even when the rival
    settlement ids happen to include the true answer."""
    r = _result(AMBIGUOUS, ("setl_A",))
    c = _case(MATCHED, ["setl_A"])
    assert E.classify(r, c) == "false_clear"


def test_classify_partial_vs_matched_mismatch_counts_as_false_match():
    """Right settlement, wrong resolution label in either direction: this
    harness's documented rule (see reconagent/eval.py module docstring)
    buckets a MATCHED/PARTIAL disagreement as false_match, not false_clear
    or a separate bucket, because the system did assert something concrete
    and false about the state of the books."""
    system_says_matched = _result(MATCHED, ("setl_A",))
    truth_says_partial = _case(PARTIAL, ["setl_A"])
    assert E.classify(system_says_matched, truth_says_partial) == "false_match"

    system_says_partial = _result(PARTIAL, ("setl_A",))
    truth_says_matched = _case(MATCHED, ["setl_A"])
    assert E.classify(system_says_partial, truth_says_matched) == "false_match"


def test_classify_withheld_result_is_none_and_scored_as_no_assertion():
    """The threshold sweep withholds low-confidence results by passing
    result=None -- confirm that reads the same as an UNMATCHED verdict."""
    c = _case(MATCHED, ["setl_A"])
    assert E.classify(None, c) == "false_clear"
    assert E.classify(None, _case(UNMATCHED, [])) == "correct"


def test_tally_denominators():
    cases = [_case(MATCHED, ["setl_A"], "B1"), _case(PARTIAL, ["setl_B"], "B2")]
    results = {
        "B1": _result(MATCHED, ("setl_A",)),
        "B2": _result(UNMATCHED, ()),
    }
    t = E._tally(cases, results)
    assert t["total"] == 2
    assert t["true_link"] == 2
    assert t["correct"] == 1
    assert t["false_clear"] == 1
    assert t["false_match"] == 0


# --------------------------------------------------------------------------
# 2. Known-good run against data/
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def main() -> E.Split:
    return E.load_main()


@pytest.fixture(scope="module")
def holdout() -> E.Split:
    return E.load_holdout()


def test_main_reproduces_known_headline_numbers(main: E.Split):
    m = E.compute_metrics(main)
    assert m.total_linked == 150
    assert m.correct == 150
    assert m.false_match == 0
    assert m.false_clear == 0
    assert m.false_match_rate == 0.0
    assert m.false_clear_rate == 0.0


def test_holdout_reproduces_known_headline_numbers(holdout: E.Split):
    m = E.compute_metrics(holdout)
    assert m.total_linked == 53
    assert m.correct == 50
    assert m.false_match == 0
    assert m.false_clear == 3
    assert m.false_match_rate == 0.0


def test_threshold_sweep_shape(main: E.Split):
    rows = E.threshold_sweep(main, thresholds=(0.0, 0.5, 1.0))
    assert [r["threshold"] for r in rows] == [0.0, 0.5, 1.0]
    # withholding everything (threshold 1.0, no confidence reaches 1.0)
    # must turn every true link into a false clear.
    assert rows[-1]["false_clear_rate"] == 1.0


# --------------------------------------------------------------------------
# Mutation test: the metric must actually move.
# --------------------------------------------------------------------------


def test_mutation_sweep_is_monotonic(main: E.Split):
    rows = E.mutation_sweep(main, rates=(0.0, 0.05, 0.2, 0.5), seed=1)
    rates = [r["false_match_rate"] for r in rows]
    assert rates[0] == 0.0
    assert rates == sorted(rates)
    assert rates[-1] > rates[0]


def test_mutation_actually_corrupts_the_settlement_ids(main: E.Split):
    mutated, n = E.mutate_results(main, rate=0.5, rng=__import__("random").Random(1))
    assert n > 0
    changed = sum(
        1 for bid, r in mutated.items()
        if r.settlement_ids != main.results[bid].settlement_ids
    )
    assert changed == n


def test_bundle_wrong_subset_mutation_flips_a_correct_bundle_to_false_match(main: E.Split):
    bundle = E.mutate_one_bundle(main)
    assert bundle is not None
    assert bundle["verdict_before"] == "correct"
    assert bundle["verdict_after"] == "false_match"
    assert set(bundle["wrong_subset_used"]) != set(bundle["true_subset"])


# --------------------------------------------------------------------------
# Output-path guard
# --------------------------------------------------------------------------


def test_refuses_to_write_report_over_data_dir():
    report = {"splits": {}, "coverage_gaps": [], "fx_metrics_note": "", "throughput": [],
              "mutation_test": {"sweep": [], "bundle_wrong_subset": None},
              "threshold_sweep": {"main": [], "holdout": []}}
    with pytest.raises(ValueError):
        E.write_report(report, json_path=E.REPO / "data" / "eval_report.json",
                        md_path=E.REPO / "data" / "eval_report.md")
    with pytest.raises(ValueError):
        E.write_report(report, json_path=E.REPO / "data" / "holdout" / "eval_report.json",
                        md_path=E.REPO / "data" / "holdout" / "eval_report.md")


def test_writes_report_to_a_normal_path(tmp_path):
    report = {
        "splits": {
            "main": {
                "false_match_rate": 0.0, "false_clear_rate": 0.0, "match_rate": 1.0,
                "precision": 1.0, "recall": 1.0, "false_match": 0, "false_clear": 0,
                "total_linked": 1, "true_link_count": 1, "by_defect_class": {},
            },
            "holdout": {
                "false_match_rate": 0.0, "false_clear_rate": 0.0, "match_rate": 1.0,
                "precision": 1.0, "recall": 1.0, "false_match": 0, "false_clear": 0,
                "total_linked": 1, "true_link_count": 1, "by_defect_class": {},
            },
        },
        "coverage_gaps": ["x"],
        "fx_metrics_note": "note",
        "throughput": [{"scale": 10, "credits": 5, "seconds": 0.1, "records_per_sec": 50.0}],
        "mutation_test": {"sweep": [], "bundle_wrong_subset": None},
        "threshold_sweep": {"main": [], "holdout": []},
    }
    j = tmp_path / "report.json"
    md = tmp_path / "report.md"
    E.write_report(report, json_path=j, md_path=md)
    assert j.exists() and md.exists()
    assert "false-match" in md.read_text().lower()


# --------------------------------------------------------------------------
# Variance decomposition breakdown -- descriptive tally, not a matching-
# accuracy metric (see reconagent.eval.FX_METRICS_NOTE).
# --------------------------------------------------------------------------


def test_decomposition_breakdown_has_all_six_categories_main(main: E.Split):
    breakdown = E.decomposition_breakdown(main)
    assert set(breakdown) == {
        "NO_VARIANCE", "BENIGN_FX_DRIFT", "FLAGGED_FX_DRIFT",
        "FEE_MISMATCH", "DATA_ENTRY_ERROR", "UNRESOLVED",
    }


def test_decomposition_breakdown_reproduces_known_main_numbers(main: E.Split):
    breakdown = E.decomposition_breakdown(main)
    assert breakdown == {
        "NO_VARIANCE": 172,
        "BENIGN_FX_DRIFT": 23,
        "FLAGGED_FX_DRIFT": 5,
        "FEE_MISMATCH": 0,
        "DATA_ENTRY_ERROR": 0,
        "UNRESOLVED": 0,
    }
    assert sum(breakdown.values()) == 200 == len(main.settlements)


def test_decomposition_breakdown_reproduces_known_holdout_numbers(holdout: E.Split):
    breakdown = E.decomposition_breakdown(holdout)
    assert breakdown == {
        "NO_VARIANCE": 79,
        "BENIGN_FX_DRIFT": 18,
        "FLAGGED_FX_DRIFT": 3,
        "FEE_MISMATCH": 0,
        "DATA_ENTRY_ERROR": 0,
        "UNRESOLVED": 0,
    }
    assert sum(breakdown.values()) == 100 == len(holdout.settlements)


def test_render_markdown_shows_zero_valued_categories_not_just_nonzero_ones():
    """The whole point of the fix: FEE_MISMATCH and DATA_ENTRY_ERROR must be
    visible in the rendered table even though their count is zero today --
    a category silently missing from the table is the bug this closes."""
    report = {
        "splits": {
            "main": {
                "false_match_rate": 0.0, "false_clear_rate": 0.0, "match_rate": 1.0,
                "precision": 1.0, "recall": 1.0, "false_match": 0, "false_clear": 0,
                "total_linked": 1, "true_link_count": 1, "by_defect_class": {},
            },
            "holdout": {
                "false_match_rate": 0.0, "false_clear_rate": 0.0, "match_rate": 1.0,
                "precision": 1.0, "recall": 1.0, "false_match": 0, "false_clear": 0,
                "total_linked": 1, "true_link_count": 1, "by_defect_class": {},
            },
        },
        "coverage_gaps": [],
        "fx_metrics_note": E.FX_METRICS_NOTE,
        "decomposition": {
            "main": {
                "NO_VARIANCE": 172, "BENIGN_FX_DRIFT": 23, "FLAGGED_FX_DRIFT": 5,
                "FEE_MISMATCH": 0, "DATA_ENTRY_ERROR": 0, "UNRESOLVED": 0,
            },
            "holdout": {
                "NO_VARIANCE": 79, "BENIGN_FX_DRIFT": 18, "FLAGGED_FX_DRIFT": 3,
                "FEE_MISMATCH": 0, "DATA_ENTRY_ERROR": 0, "UNRESOLVED": 0,
            },
        },
        "throughput": [],
        "mutation_test": {"sweep": [], "bundle_wrong_subset": None},
        "threshold_sweep": {"main": [], "holdout": []},
    }
    md = E.render_markdown(report)
    assert "FEE_MISMATCH" in md
    assert "DATA_ENTRY_ERROR" in md
    assert "UNRESOLVED" in md
    assert E.FX_METRICS_NOTE in md


# --------------------------------------------------------------------------
# Throughput -- small scales only, so the suite stays fast.
# --------------------------------------------------------------------------


def test_throughput_table_small_scales():
    rows = E.throughput_table(scales=(50, 120))
    assert [r["scale"] for r in rows] == [50, 120]
    for r in rows:
        assert r["credits"] > 0
        assert r["seconds"] >= 0
        assert r["records_per_sec"] is None or r["records_per_sec"] > 0
