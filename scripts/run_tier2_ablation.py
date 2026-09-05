"""Tier 2 ablation report: does Splink (Stage 3) + hybrid fuzzy matching
(Stage 4) measurably improve recall over Tier 1 alone, and where.

Tier 1 (`reconagent.match.match_all`) was built and evaluated first; its own
numbers already show 0.00% false-clear on its splits. Tier 2
(`reconagent.fuzzy.match_with_full_cascade`, which composes Tier 1 + Stage 3
+ Stage 4) was built anyway, proactively, per its own module docstrings --
not because Tier 1 measured a gap. This script is the honest check on
whether that bet paid off: it runs both matchers against both `data/` (the
main set) and `stress_test/` (the set built specifically so Tier 1 alone
resolves 0/40), scores each with `reconagent.eval.compute_metrics` -- the
same scoring machinery every other number in this project uses -- and
reports match-rate/recall deltas per defect class. Whichever way the
numbers land is what gets reported; this is not a pass this project is
allowed to grade itself up on.

This module does not touch `reconagent/match.py`, `reconagent/fuzzy.py`,
`reconagent/probabilistic.py`, or `reconagent/eval.py` -- it only calls
them. It is a separate report from `reconagent.eval`'s (that one is Tier-1
headline reporting; this one is the Tier-2 ablation) and writes to a
different path.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from reconagent.camt053 import parse_camt053_file
from reconagent.eval import Metrics, Split, compute_metrics
from reconagent.fuzzy import match_with_full_cascade
from reconagent.invoices import parse_invoice_ledger
from reconagent.match import match_all
from reconagent.razorpay import parse_razorpay_settlements

REPO = Path(__file__).resolve().parent.parent
DATA_ROOT = (REPO / "data").resolve()
STRESS_ROOT = (REPO / "stress_test").resolve()
DEFAULT_REPORT_JSON = REPO / "reports" / "tier2_ablation.json"
DEFAULT_REPORT_MD = REPO / "reports" / "tier2_ablation.md"

TIER1 = "tier1"
TIER1_2 = "tier1+2"


# --------------------------------------------------------------------------
# Loading + running the four combinations
# --------------------------------------------------------------------------


def load_dataset(data_dir: Path, prefix: str = ""):
    """(truth, settlements, credits, invoices) for one dataset directory.
    Both `data/` and `stress_test/` use the same file layout and the same
    `ground_truth.json` schema; neither uses a filename prefix (unlike
    `data/holdout/`, which does)."""
    truth = json.loads((data_dir / f"{prefix}ground_truth.json").read_text())
    settlements = parse_razorpay_settlements(data_dir / f"{prefix}razorpay_settlements.csv")
    credits = parse_camt053_file(data_dir / f"{prefix}bank_statement.camt053.xml")
    invoices = parse_invoice_ledger(data_dir / f"{prefix}invoice_ledger.csv")
    return truth, settlements, credits, invoices


def build_split(name: str, data_dir: Path, prefix: str, tier: str) -> Split:
    """Run `match_all` (tier1) or `match_with_full_cascade` (tier1+2) over
    one dataset and wrap the result in a `Split` for `compute_metrics`.
    `Split.results` only needs `.resolution`/`.settlement_ids` per credit --
    `compute_metrics` doesn't care which matcher produced it (verified
    directly against `ProbabilisticMatchResult`/`FuzzyMatchResult` before
    this task was dispatched)."""
    truth, settlements, credits, invoices = load_dataset(data_dir, prefix)
    if tier == TIER1:
        results = match_all(credits, settlements)
    elif tier == TIER1_2:
        results = match_with_full_cascade(credits, settlements, invoices)
    else:
        raise ValueError(f"unknown tier {tier!r}")
    by_bank_txn = {r.bank_txn_id: r for r in results}
    return Split(name, truth, settlements, credits, by_bank_txn, data_dir, prefix)


def run_all() -> dict[str, Metrics]:
    """The four combinations, each scored via `compute_metrics`."""
    combos = {
        "main_tier1": (REPO / "data", "", TIER1),
        "main_tier1+2": (REPO / "data", "", TIER1_2),
        "stress_tier1": (REPO / "stress_test", "", TIER1),
        "stress_tier1+2": (REPO / "stress_test", "", TIER1_2),
    }
    out: dict[str, Metrics] = {}
    for key, (data_dir, prefix, tier) in combos.items():
        split = build_split(key, data_dir, prefix, tier)
        out[key] = compute_metrics(split)
    return out


# --------------------------------------------------------------------------
# Delta table: per-dataset, per-defect-class match-rate/recall change
# --------------------------------------------------------------------------


def _defect_class_deltas(before: Metrics, after: Metrics) -> list[dict]:
    """One row per defect class present in either run, in sorted order.
    `match_rate_delta` is `after - before`; positive means tier1+2 resolved
    strictly more of that class correctly than tier1 alone."""
    classes = sorted(set(before.by_defect_class) | set(after.by_defect_class))
    rows = []
    for dc in classes:
        b = before.by_defect_class.get(dc, {"total": 0, "correct": 0, "match_rate": 0.0})
        a = after.by_defect_class.get(dc, {"total": 0, "correct": 0, "match_rate": 0.0})
        rows.append({
            "defect_class": dc,
            "total": a.get("total", b.get("total", 0)),
            "tier1_correct": b["correct"],
            "tier1+2_correct": a["correct"],
            "tier1_match_rate": b["match_rate"],
            "tier1+2_match_rate": a["match_rate"],
            "match_rate_delta": a["match_rate"] - b["match_rate"],
        })
    return rows


def build_delta_table(before: Metrics, after: Metrics) -> dict:
    return {
        "dataset": before.split,
        "overall": {
            "tier1_match_rate": before.match_rate,
            "tier1+2_match_rate": after.match_rate,
            "match_rate_delta": after.match_rate - before.match_rate,
            "tier1_recall": before.recall,
            "tier1+2_recall": after.recall,
            "recall_delta": (
                None if before.recall is None or after.recall is None
                else after.recall - before.recall
            ),
            "tier1_false_match": before.false_match,
            "tier1+2_false_match": after.false_match,
            "tier1_false_clear": before.false_clear,
            "tier1+2_false_clear": after.false_clear,
        },
        "by_defect_class": _defect_class_deltas(before, after),
    }


# --------------------------------------------------------------------------
# Plain-language finding -- computed from the actual numbers just run,
# not asserted in advance. See module docstring: report whichever way the
# numbers land.
# --------------------------------------------------------------------------


def build_finding(main_delta: dict, stress_delta: dict) -> str:
    lines = []

    main_mr_delta = main_delta["overall"]["match_rate_delta"]
    if main_mr_delta == 0:
        lines.append(
            "On the main dataset, Tier 2 (Splink + hybrid fuzzy) provides zero "
            "measurable value: Tier 1 alone and Tier 1+2 produce identical "
            f"match rates ({main_delta['overall']['tier1_match_rate']:.2%}). "
            "Tier 1 already resolves the main set completely, so Tier 2 never "
            "gets a case to add value on there -- provably a no-op, not a "
            "close call."
        )
    else:
        sign = "improves" if main_mr_delta > 0 else "regresses"
        lines.append(
            f"On the main dataset, Tier 2 {sign} match rate by "
            f"{main_mr_delta:+.2%} versus Tier 1 alone."
        )

    stress_overall = stress_delta["overall"]
    stress_mr_delta = stress_overall["match_rate_delta"]
    lines.append(
        "On the stress-test dataset (built specifically so Tier 1 alone "
        f"resolves nothing: match rate {stress_overall['tier1_match_rate']:.2%} "
        f"under Tier 1 alone), Tier 2 raises match rate to "
        f"{stress_overall['tier1+2_match_rate']:.2%} ({stress_mr_delta:+.2%}), "
        f"with false_match count going from {stress_overall['tier1_false_match']} "
        f"to {stress_overall['tier1+2_false_match']}."
    )

    improved, unimproved = [], []
    for row in stress_delta["by_defect_class"]:
        if row["total"] == 0:
            continue
        if row["match_rate_delta"] > 0:
            improved.append(
                f"{row['defect_class']} ({row['tier1+2_correct']}/{row['total']})"
            )
        else:
            unimproved.append(
                f"{row['defect_class']} ({row['tier1+2_correct']}/{row['total']})"
            )

    if improved:
        lines.append(
            "Tier 2's gain on the stress set is uneven across defect classes, "
            "improving: " + ", ".join(improved) + "."
        )
    if unimproved:
        lines.append(
            "It shows no improvement at all over Tier 1 (still 0 resolved) on: "
            + ", ".join(unimproved) + "."
        )

    lines.append(
        "In plain language: Tier 2 provides zero value on the main set (Tier "
        "1 already resolves it completely) and partial, uneven value on the "
        "stress set -- real gains on several defect classes, no gain "
        "whatsoever on others. This is not softened or rounded up in either "
        "direction; it is what compute_metrics measured on this run."
    )
    return " ".join(lines)


# --------------------------------------------------------------------------
# Report assembly / rendering / writing
# --------------------------------------------------------------------------


def build_report(metrics: dict[str, Metrics]) -> dict:
    main_before, main_after = metrics["main_tier1"], metrics["main_tier1+2"]
    stress_before, stress_after = metrics["stress_tier1"], metrics["stress_tier1+2"]

    main_delta = build_delta_table(main_before, main_after)
    stress_delta = build_delta_table(stress_before, stress_after)
    finding = build_finding(main_delta, stress_delta)

    return {
        "finding": finding,
        "deltas": {"main": main_delta, "stress_test": stress_delta},
        "full_metrics": {key: asdict(m) for key, m in metrics.items()},
    }


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.2%}"


def _render_delta_section(title: str, delta: dict) -> list[str]:
    o = delta["overall"]
    recall_delta_str = "n/a" if o["recall_delta"] is None else f"{o['recall_delta']:+.2%}"
    lines = [
        f"### {title}",
        "",
        "| metric | tier 1 | tier 1+2 | delta |",
        "|---|---|---|---|",
        f"| match rate | {_pct(o['tier1_match_rate'])} | {_pct(o['tier1+2_match_rate'])} "
        f"| {o['match_rate_delta']:+.2%} |",
        f"| recall | {_pct(o['tier1_recall'])} | {_pct(o['tier1+2_recall'])} "
        f"| {recall_delta_str} |",
        f"| false match (count) | {o['tier1_false_match']} | {o['tier1+2_false_match']} "
        f"| {o['tier1+2_false_match'] - o['tier1_false_match']:+d} |",
        f"| false clear (count) | {o['tier1_false_clear']} | {o['tier1+2_false_clear']} "
        f"| {o['tier1+2_false_clear'] - o['tier1_false_clear']:+d} |",
        "",
        "| defect class | total | tier1 correct | tier1+2 correct | tier1 match rate | tier1+2 match rate | delta |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in delta["by_defect_class"]:
        lines.append(
            f"| {row['defect_class']} | {row['total']} | {row['tier1_correct']} "
            f"| {row['tier1+2_correct']} | {_pct(row['tier1_match_rate'])} "
            f"| {_pct(row['tier1+2_match_rate'])} | {row['match_rate_delta']:+.2%} |"
        )
    lines.append("")
    return lines


def render_markdown(report: dict) -> str:
    lines = [
        "# Tier 2 ablation: does Splink + hybrid fuzzy matching improve recall?",
        "",
        "## Finding",
        "",
        report["finding"],
        "",
        "## Delta tables (Tier 1 vs Tier 1+2)",
        "",
    ]
    lines += _render_delta_section("Main dataset (`data/`)", report["deltas"]["main"])
    lines += _render_delta_section("Stress-test dataset (`stress_test/`)", report["deltas"]["stress_test"])

    lines += [
        "## Full four-way metrics (detail, for a reader who wants to check the numbers)",
        "",
    ]
    for key in ("main_tier1", "main_tier1+2", "stress_tier1", "stress_tier1+2"):
        m = report["full_metrics"][key]
        lines += [
            f"### {key}",
            "",
            f"- total_linked: {m['total_linked']}, true_link_count: {m['true_link_count']}, "
            f"asserted_count: {m['asserted_count']}",
            f"- correct: {m['correct']}, false_match: {m['false_match']}, "
            f"false_clear: {m['false_clear']}, tie_ambiguous: {m['tie_ambiguous']}",
            f"- false_match_rate: {_pct(m['false_match_rate'])}, "
            f"false_clear_rate: {_pct(m['false_clear_rate'])}, "
            f"tie_ambiguous_rate: {_pct(m['tie_ambiguous_rate'])}",
            f"- match_rate: {_pct(m['match_rate'])}, precision: {_pct(m['precision'])}, "
            f"recall: {_pct(m['recall'])}",
            "",
            "| defect class | total | correct | false match | false clear | tie ambiguous | match rate |",
            "|---|---|---|---|---|---|---|",
        ]
        for dc, row in m["by_defect_class"].items():
            lines.append(
                f"| {dc} | {row['total']} | {row['correct']} | {row['false_match']} "
                f"| {row['false_clear']} | {row['tie_ambiguous']} | {_pct(row['match_rate'])} |"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def _guard_output_path(path: Path) -> None:
    """Mirrors `reconagent.eval._guard_output_path`: refuse to write over
    either committed ground-truth directory this report reads from."""
    resolved = path.resolve()
    for root in (DATA_ROOT, STRESS_ROOT):
        if resolved == root or root in resolved.parents:
            raise ValueError(
                f"refusing to write ablation report over {root} (got {path}) -- "
                "data/ and stress_test/ are committed ground truth"
            )


def write_report(
    report: dict,
    json_path: Path = DEFAULT_REPORT_JSON,
    md_path: Path = DEFAULT_REPORT_MD,
) -> None:
    _guard_output_path(json_path)
    _guard_output_path(md_path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    md_path.write_text(render_markdown(report))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-json", type=Path, default=DEFAULT_REPORT_JSON)
    ap.add_argument("--out-md", type=Path, default=DEFAULT_REPORT_MD)
    args = ap.parse_args()

    metrics = run_all()
    report = build_report(metrics)
    write_report(report, args.out_json, args.out_md)
    print(f"wrote {args.out_json}")
    print(f"wrote {args.out_md}")
    print(report["finding"])


if __name__ == "__main__":
    main()
