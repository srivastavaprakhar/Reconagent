"""Tests for the Tier 3 cross-encoder ablation
(`scripts/run_cross_encoder_ablation.py`).

`data/ground_truth.json` is read by the module under test -- that is Tier 3's
sanctioned calibration source, the same one Stage 3 and Stage 4 use.
`stress_test/ground_truth.json` is read ONLY by the grading half of that
script and by this file;
`test_matching_half_never_reads_stress_or_holdout_ground_truth` asserts the
functions that actually decide matches never see it, the same discipline
`tests/test_probabilistic.py` and `tests/test_fuzzy.py` hold via their own
source-level scans.

Every number asserted below was produced by actually running this ablation,
not copied from a task brief. Scoring `data/`'s full 30,704-pair blocked
candidate space through an 82M-parameter cross-encoder on CPU takes roughly
two and a half minutes, so the whole run happens once in a module-scoped
fixture.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load_ablation_module():
    """`scripts/` has no `__init__.py`; import by file path, exactly as
    `tests/test_tier2_ablation.py` does."""
    spec = importlib.util.spec_from_file_location(
        "run_cross_encoder_ablation", REPO / "scripts" / "run_cross_encoder_ablation.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_cross_encoder_ablation"] = mod
    spec.loader.exec_module(mod)
    return mod


A = _load_ablation_module()


@pytest.fixture(scope="module")
def report() -> dict:
    """The full ablation. Skips rather than fails if the pretrained weights
    cannot be fetched -- an unavailable model is an environment fact, not a
    regression in this project's code, and faking a result instead would be
    the one outcome the ablation's own brief forbids."""
    try:
        A.load_cross_encoder()
    except Exception as exc:  # pragma: no cover - network/hub availability
        pytest.skip(f"pretrained cross-encoder {A.MODEL_NAME} unavailable: {exc}")
    return A.run_ablation()


# ---------------------------------------------------------------------------
# The matching half never reads the answer key it is graded against.
# ---------------------------------------------------------------------------


def test_matching_half_never_reads_stress_or_holdout_ground_truth():
    """A source-level scan of the functions that actually build candidates,
    score them, derive the threshold and decide acceptance. Unlike the
    module-wide regex in tests/test_fuzzy.py, this one is scoped to the
    matching functions, because this file is a report generator whose
    grading half is *supposed* to read stress_test/ground_truth.json."""
    matching_functions = (
        A.build_candidate_pairs,
        A._pair_text,
        A._blocked_pool,
        A.score_pairs,
        A.derive_threshold,
        A.residual_population,
        A.propose_matches,
        A.top_ranked_settlement_ids,
    )
    for fn in matching_functions:
        # Strip the docstring: these functions *describe* which splits they
        # must never open, and the scan is about executable code.
        source = inspect.getsource(fn).replace(fn.__doc__ or "\0", "")
        assert "stress_test" not in source, fn.__name__
        assert "holdout" not in source.lower(), fn.__name__

    # The only ground_truth.json the matching half opens is data/'s, and it
    # is reached through a caller-supplied data_dir rather than a hardcoded
    # split path.
    derivation_source = inspect.getsource(A.derive_threshold)
    assert 'data_dir / "ground_truth.json"' in derivation_source

    # And the grading half genuinely is separate: it takes truth as an
    # argument rather than loading it.
    grade_source = inspect.getsource(A.grade)
    assert "ground_truth" not in grade_source


def test_model_is_used_pretrained_not_fine_tuned():
    """The whole premise of the ablation: off-the-shelf weights. Nothing in
    the module trains, fits or fine-tunes the cross-encoder."""
    source = (REPO / "scripts" / "run_cross_encoder_ablation.py").read_text()
    body = source.split('"""', 2)[-1]  # skip the module docstring's prose
    for forbidden in (".fit(", ".train(", "SoftmaxLoss", "CrossEncoderTrainer"):
        assert forbidden not in body, forbidden


# ---------------------------------------------------------------------------
# Threshold derivation, reproduced from data/.
# ---------------------------------------------------------------------------


def test_threshold_derivation_is_reproducible_from_main_labels(report: dict):
    """Re-derive from `data/` what the module docstring's THRESHOLD
    DERIVATION section documents, and confirm the constant lands there."""
    d = report["threshold_derivation"]

    assert d["blocked_candidate_pairs"] == 30704
    assert d["known_positive_pairs"] == 175
    assert d["known_negative_pairs"] == 30529
    # Same blocking rule as Stage 3/Stage 4, so the same candidate space they
    # derive their own thresholds over.
    assert d["blocked_candidate_pairs"] == d["known_positive_pairs"] + d["known_negative_pairs"]

    assert d["positive_min"] == pytest.approx(0.107633, abs=1e-6)
    assert d["positive_mean"] == pytest.approx(0.449878, abs=1e-6)
    assert d["positive_max"] == pytest.approx(0.866071, abs=1e-6)
    assert d["negative_min"] == pytest.approx(0.055597, abs=1e-6)
    assert d["negative_mean"] == pytest.approx(0.346492, abs=1e-6)
    assert d["negative_max"] == pytest.approx(0.828032, abs=1e-6)

    # The measured separation failure, asserted rather than described: only
    # three known positives clear the known-negative ceiling at all.
    assert d["positives_above_negative_max"] == pytest.approx([0.831737, 0.855163, 0.866071], abs=1e-6)
    assert d["positives_below_negative_max"] == 172
    assert d["negatives_above_weakest_positive"] == 30399
    assert d["negatives_above_weakest_positive_rate"] == pytest.approx(0.9957, abs=1e-4)

    # The threshold is the midpoint of that one clean gap, and sits strictly
    # above every known negative in data/ -- zero false matches there.
    assert Decimal(d["threshold"]) == A.DERIVED_THRESHOLD == Decimal("0.829885")
    assert Decimal(str(d["negative_max"])) < Decimal(d["threshold"])
    assert Decimal(str(d["positives_above_negative_max"][0])) > Decimal(d["threshold"])


# ---------------------------------------------------------------------------
# The 20-case residual: the actual ablation result.
# ---------------------------------------------------------------------------


def test_residual_population_is_the_20_cases_tier1_plus_2_cannot_resolve(report: dict):
    r = report["residual"]
    assert r["residual_credits"] == 20
    assert r["open_settlements"] == 20
    assert r["blocked_candidate_pairs"] == 400


def test_residual_has_zero_false_matches(report: dict):
    """The assertion that matters most (spec section 9: honest abstention
    beats a false match every time). Not optional, and not folded into an
    aggregate: the cross-encoder asserted no wrong settlement link anywhere
    in the 20-case residual."""
    s = report["scoring"]
    assert s["wrong"] == 0
    assert s["wrong_detail"] == []
    for row in s["by_defect_class"]:
        assert row["wrong"] == 0, row


def test_residual_result_is_zero_correct_zero_wrong_twenty_deferred(report: dict):
    """The measured outcome of this ablation. If a future run disagrees,
    that is a real finding and this test should say so rather than be edited
    to match."""
    s = report["scoring"]
    assert s["total"] == 20
    assert s["correct"] == 0
    assert s["wrong"] == 0
    assert s["deferred"] == 20
    assert s["correct"] + s["wrong"] + s["deferred"] == s["total"]


def test_legal_vs_trading_name_shows_no_lift_over_tier2(report: dict):
    """The category this ablation primarily targeted. Tier 2 resolves 0/8;
    so does the cross-encoder."""
    by_class = {row["defect_class"]: row for row in report["scoring"]["by_defect_class"]}
    legal = by_class["legal_vs_trading_name"]
    assert legal["total"] == 8
    assert legal["correct"] == 0
    assert legal["wrong"] == 0
    assert legal["deferred"] == 8


def test_other_three_residual_categories_also_show_no_lift(report: dict):
    by_class = {row["defect_class"]: row for row in report["scoring"]["by_defect_class"]}
    assert set(by_class) == {
        "legal_vs_trading_name",
        "abbreviation_variant",
        "invoice_description_mismatch",
        "transliteration_variant",
    }
    assert by_class["abbreviation_variant"]["total"] == 6
    assert by_class["invoice_description_mismatch"]["total"] == 4
    assert by_class["transliteration_variant"]["total"] == 2
    for name in ("abbreviation_variant", "invoice_description_mismatch", "transliteration_variant"):
        assert by_class[name]["correct"] == 0, name
        assert by_class[name]["wrong"] == 0, name
        assert by_class[name]["deferred"] == by_class[name]["total"], name


def test_every_residual_decline_is_genuinely_below_threshold(report: dict):
    """Nothing was silently dropped: every deferred credit really did score
    below the derived threshold, and the best score anywhere in the residual
    falls well short of it."""
    threshold = A.DERIVED_THRESHOLD
    for p in report["proposals"]:
        assert p["accepted"] is False
        assert Decimal(p["score"]) < threshold, p
    assert report["residual"]["highest_best_candidate_score"] == pytest.approx(0.644592, abs=1e-6)
    assert Decimal(str(report["residual"]["highest_best_candidate_score"])) < threshold


def test_ranking_diagnostic_separates_ordering_from_calibration(report: dict):
    """The honest nuance in the negative result: the model's ordering does
    carry signal (true settlement first for 15/20), but accepting that
    ordering without a threshold would produce 5 false matches -- which is
    why 'it ranks well' is not an argument for integrating it."""
    r = report["ranking_diagnostic"]
    assert r["total"] == 20
    assert r["true_settlement_ranked_first"] == 15
    assert r["would_be_wrong_if_accepted_unconditionally"] == 5
    by_class = {row["defect_class"]: row for row in r["by_defect_class"]}
    assert by_class["legal_vs_trading_name"]["ranked_first"] == 4


def test_recommendation_is_do_not_integrate(report: dict):
    """No lift means this stays an ablation finding, per the task's own
    human-in-the-loop gate: only a clear win with zero false-match cost gets
    escalated, and this is not one."""
    rec = report["integration_recommendation"]
    assert rec.startswith("DO NOT INTEGRATE")
    assert "no lift" in rec


# ---------------------------------------------------------------------------
# The live cascade is untouched.
# ---------------------------------------------------------------------------


def test_ablation_does_not_modify_the_live_cascade():
    """Tier 3 is a standalone report, not an integration: the three matcher
    modules are imported and called, never edited. Checked against git
    rather than by scanning prose -- `git diff` on those exact paths is the
    claim, not a proxy for it."""
    import subprocess

    proc = subprocess.run(
        # Working tree vs index. `reconagent/fuzzy.py` is staged-but-uncommitted
        # at the time this was written, so `git diff HEAD` would flag it as
        # added; what this asserts is that Tier 3 introduced no edit to any of
        # the three.
        ["git", "diff", "--name-only", "--",
         "reconagent/match.py", "reconagent/probabilistic.py", "reconagent/fuzzy.py"],
        cwd=REPO, capture_output=True, text=True,
    )
    if proc.returncode != 0:  # pragma: no cover - not a git checkout
        pytest.skip("not a git working tree")
    assert proc.stdout.strip() == "", f"live cascade modified: {proc.stdout}"


# ---------------------------------------------------------------------------
# Report writing + output-path guard.
# ---------------------------------------------------------------------------


def test_write_report_produces_valid_json_and_markdown(report: dict, tmp_path):
    json_path = tmp_path / "tier3.json"
    md_path = tmp_path / "tier3.md"
    A.write_report(report, json_path=json_path, md_path=md_path)

    written = json.loads(json_path.read_text())
    assert written["finding"] == report["finding"]
    assert written["model"] == A.MODEL_NAME
    assert written["fine_tuned"] is False

    md = md_path.read_text()
    assert report["finding"] in md
    assert "legal_vs_trading_name" in md
    # The false-match count is a headline row, not a buried side note.
    assert "**wrong (false match)** | **0**" in md
    assert "HEADLINE: zero false matches" in md


def test_refuses_to_write_over_data_dir(report: dict):
    with pytest.raises(ValueError):
        A.write_report(report, json_path=REPO / "data" / "x.json", md_path=REPO / "data" / "x.md")


def test_refuses_to_write_over_stress_test_dir(report: dict):
    with pytest.raises(ValueError):
        A.write_report(
            report,
            json_path=REPO / "stress_test" / "x.json",
            md_path=REPO / "stress_test" / "x.md",
        )


def test_refuses_to_write_over_data_holdout_dir(report: dict):
    with pytest.raises(ValueError):
        A.write_report(
            report,
            json_path=REPO / "data" / "holdout" / "x.json",
            md_path=REPO / "data" / "holdout" / "x.md",
        )


# ---------------------------------------------------------------------------
# Money-path discipline (CLAUDE.md).
# ---------------------------------------------------------------------------


def test_no_float_on_money_path_fields(report: dict):
    """Every money field this module touches is integer minor units, and the
    acceptance score/threshold are Decimal. `run_ablation` serialises them to
    strings for the JSON report, so check the live objects."""
    settlements, credits, invoices = A.load_dataset(A.STRESS_ROOT)
    residual_credits, open_settlements = A.residual_population(credits, settlements, invoices)
    proposals = A.propose_matches(
        residual_credits, open_settlements, invoices, A.DERIVED_THRESHOLD
    )
    assert proposals
    for p in proposals.values():
        for field in (p.credit_amount_minor, p.settlement_net_sum_minor, p.residual_minor):
            assert isinstance(field, int) and not isinstance(field, bool), p
        assert isinstance(p.score, Decimal)
        assert isinstance(p.threshold, Decimal)
