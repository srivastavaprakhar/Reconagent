"""Tests for the Tier 2 ablation report (scripts/run_tier2_ablation.py).

Two things get checked:

1. The four `compute_metrics` runs reproduce the numbers independently
   verified by the orchestrator before this task was dispatched: main
   152/152 correct under both Tier 1 and Tier 1+2 (a provable no-op there),
   stress-test 0/40 under Tier 1 alone and 20/40 correct / 0 wrong under
   Tier 1+2. If a run here disagrees, that is a real finding (something
   upstream changed) and this test says so rather than being adjusted to
   match a different run.
2. The report-writing function produces valid JSON, non-empty Markdown
   containing the plain-language finding and the delta table, and the
   output-path guard refuses to write over data/ or stress_test/.

Running all four combinations trains the Stage 3/Stage 4 default models
(cached per-process), so this module computes them once in a
module-scoped fixture rather than per test.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load_ablation_module():
    """`scripts/` has no `__init__.py` (mirrors how `reconagent/eval.py`'s
    own `_load_generator_module` loads `scripts/generate_synthetic.py`),
    so import by file path rather than as a package."""
    spec = importlib.util.spec_from_file_location(
        "run_tier2_ablation", REPO / "scripts" / "run_tier2_ablation.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_tier2_ablation"] = mod
    spec.loader.exec_module(mod)
    return mod


A = _load_ablation_module()


@pytest.fixture(scope="module")
def metrics() -> dict:
    return A.run_all()


@pytest.fixture(scope="module")
def report(metrics: dict) -> dict:
    return A.build_report(metrics)


# --------------------------------------------------------------------------
# 1. The four-way numbers
# --------------------------------------------------------------------------


def test_main_tier1_is_152_of_152_correct_zero_defects(metrics: dict):
    m = metrics["main_tier1"]
    assert m.total_linked == 152
    assert m.correct == 152
    assert m.false_match == 0
    assert m.false_clear == 0


def test_main_tier1_plus_2_is_identical_to_tier1_alone(metrics: dict):
    """Tier 1 already resolves data/ completely, so Tier 1+2 must be a
    provable no-op there -- same 152/152, zero defects."""
    t1, t12 = metrics["main_tier1"], metrics["main_tier1+2"]
    assert t12.total_linked == 152
    assert t12.correct == 152
    assert t12.false_match == 0
    assert t12.false_clear == 0
    assert t12.correct == t1.correct
    assert t12.match_rate == t1.match_rate == 1.0


def test_stress_tier1_alone_resolves_nothing(metrics: dict):
    m = metrics["stress_tier1"]
    assert m.total_linked == 40
    assert m.correct == 0
    assert m.false_match == 0
    assert m.false_clear == 40


def test_stress_tier1_plus_2_resolves_20_of_40_zero_wrong(metrics: dict):
    m = metrics["stress_tier1+2"]
    assert m.total_linked == 40
    assert m.correct == 20
    assert m.false_match == 0
    assert m.false_clear == 20
    assert m.match_rate == pytest.approx(0.5)


def test_legal_vs_trading_name_shows_no_improvement_on_stress(metrics: dict):
    """Verified directly here, not assumed from the orchestrator's numbers:
    the one stress-test defect class where Tier 2 buys nothing over Tier 1."""
    by_class = metrics["stress_tier1+2"].by_defect_class
    assert "legal_vs_trading_name" in by_class
    assert by_class["legal_vs_trading_name"]["correct"] == 0
    assert by_class["legal_vs_trading_name"]["total"] == 8


# --------------------------------------------------------------------------
# 2. Report writing
# --------------------------------------------------------------------------


def test_finding_states_zero_value_on_main_and_uneven_value_on_stress(report: dict):
    finding = report["finding"]
    assert "main" in finding.lower()
    assert "stress" in finding.lower()
    assert "legal_vs_trading_name" in finding


def test_delta_table_present_for_both_datasets(report: dict):
    deltas = report["deltas"]
    assert deltas["main"]["overall"]["match_rate_delta"] == pytest.approx(0.0)
    assert deltas["stress_test"]["overall"]["match_rate_delta"] == pytest.approx(0.5)
    classes = {row["defect_class"] for row in deltas["stress_test"]["by_defect_class"]}
    assert "legal_vs_trading_name" in classes


def test_write_report_produces_valid_json_and_markdown_with_finding(report: dict, tmp_path):
    json_path = tmp_path / "tier2_ablation.json"
    md_path = tmp_path / "tier2_ablation.md"
    A.write_report(report, json_path=json_path, md_path=md_path)

    written = json.loads(json_path.read_text())  # must parse
    assert written["finding"] == report["finding"]

    md_text = md_path.read_text()
    assert md_text.strip()
    assert report["finding"] in md_text
    assert "delta" in md_text.lower()
    assert "legal_vs_trading_name" in md_text


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
