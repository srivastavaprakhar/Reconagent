"""Evaluation harness -- spec section 9.

Reports false-match rate and false-clear rate as the headline numbers,
not raw match rate, because those are the two failure directions that
actually cost money or trust (spec section 1). Everything else here --
precision, recall, match rate, the per-defect-class table, throughput,
the mutation test, the threshold sweep -- exists to support or stress
that headline, not to replace it.

FX ATTRIBUTION: NOT INCLUDED IN THIS PASS. `reconagent.fx` answers a
different question (why a matched credit's FX rate drifted) from the one
this harness answers (does the credit link to the right settlements at
all). Averaging the two into one score would hide both failure surfaces,
and scope here is the matcher. This is an explicit, honest cut, not an
oversight -- restated in every report this module emits.

COVERAGE GAP (already found by the FX unit, restated here because a
reader of *this* report should not have to go find it elsewhere): the
main split now carries one FEE_MISMATCH case and one DATA_ENTRY_ERROR
case (MAIN-00154, MAIN-00155), but the holdout set still has neither, and
no split has an overdue EDPMS receipt as of the statement date
(2026-08-31). Those paths have no holdout-validated accuracy number, in
this harness or anywhere else.

-----------------------------------------------------------------------
METRIC DEFINITIONS -- read this before trusting any number below.
-----------------------------------------------------------------------

Every metric here is computed over "linked cases": ground-truth cases
that name a real bank credit (`expected_link.bank_txn_id` is not null).
A `timing_pending` case has no credit yet -- there is nothing for the
matcher to have gotten right or wrong about it, so it is excluded from
these counts entirely (spec section 5 territory, not section 9's).

For each linked case, `classify()` compares the system's MatchResult for
that bank_txn_id against ground truth and returns one of four verdicts:

  "correct"      system asserted MATCHED/PARTIAL, with the exact
                 settlement-id subset ground truth names AND the same
                 resolution label (MATCHED vs PARTIAL) ground truth gives.
                 Also returned when ground truth says no link exists and
                 the system did not assert one, regardless of which
                 non-asserting resolution it gave (UNMATCHED, AMBIGUOUS, or
                 TIE_AMBIGUOUS) -- that direction is not what this task
                 distinguishes.

  "false_match"  system asserted MATCHED/PARTIAL and got any of:
                   - wrong settlement subset (includes the empty case:
                     ground truth says UNMATCHED but the system claimed
                     a link anyway)
                   - right subset, wrong resolution label (system said
                     MATCHED where truth is PARTIAL, or vice versa)

  "false_clear"  ground truth says a link exists (MATCHED or PARTIAL)
                 and the system did not assert one -- resolution is
                 UNMATCHED, or AMBIGUOUS. AMBIGUOUS is Stage 1's own
                 vocabulary for "the narration references several
                 settlements, none defensible"; it is scored as unresolved
                 here, never as a match, per its own docstring in
                 reconagent.match. Note TIE_AMBIGUOUS -- Stage 2's genuine
                 subset-sum tie -- is NOT scored false_clear; see the next
                 verdict.

  "tie_ambiguous" ground truth says a link exists and the system's
                 resolution is TIE_AMBIGUOUS: Stage 2 found several
                 distinct subsets tied at the identical minimum residual
                 and had no arithmetic basis to pick one. This is not the
                 same failure as false_clear ("no evidence at all") -- it
                 is the matcher finding the right answer among
                 indistinguishable siblings and honestly declining to
                 guess, so it is tallied separately rather than folded
                 into false_clear.

MATCHED/PARTIAL LABEL MISMATCH -- WHY IT COUNTS AS A FALSE MATCH, NOT A
FALSE CLEAR OR ITS OWN BUCKET. The system found the right settlement and
put a number on the residual; the number it asserted is wrong. That is a
concrete, false claim about the state of the books -- "this settlement
is fully covered" when it is not, or "only partly covered" when it is
fully covered -- not an absence of a claim, so it cannot be a false
clear (false clear means nothing was asserted). It also is not usefully
its own bucket: a payments-literate reader does not want a third rate to
track, they want to know whether anything the system said about a
credit was wrong. Bucketing it with false-match keeps the headline
metric answering exactly that question. (In this dataset every linked
case's ground truth is MATCHED or PARTIAL, never UNMATCHED, so the
"asserted where truth has none" leg of false_match never fires here --
`classify()` still handles it, for a dataset where it could.)

FALSE-MATCH RATE (headline #1)
  numerator:   count of "false_match" verdicts
  denominator: every linked case in the split (== every bank credit the
               split has a ground-truth answer for)
  This is "of everything that went through the matcher, how often did it
  write something wrong to the books" -- the split-wide base rate, not
  "of what we dared assert, how often were we wrong" (that narrower
  question is precision, below, and uses a different denominator on
  purpose).

FALSE-CLEAR RATE (headline #2)
  numerator:   count of "false_clear" verdicts (tie_ambiguous EXCLUDED --
               see TIE-AMBIGUOUS RATE below)
  denominator: linked cases where ground truth is MATCHED or PARTIAL
               (the population that could possibly be falsely cleared;
               in this dataset that is every linked case, since none of
               them have ground truth UNMATCHED)

TIE-AMBIGUOUS RATE (reported alongside the headline, same denominator)
  numerator:   count of "tie_ambiguous" verdicts
  denominator: same as false-clear rate above (true_link_count)
  Kept separate from false-clear rate rather than folded into it, so a
  reader can see how much of "the system didn't assert a link ground
  truth says exists" is an honest, arithmetic-forced abstention (a
  detected tie) versus a genuine miss.

MATCH RATE (below the headlines, not above)
  correct / total linked cases.

PRECISION / RECALL (below match rate)
  precision = correct / (count where the system asserted MATCHED/PARTIAL)
  recall    = correct / (count where ground truth is MATCHED/PARTIAL)
  None if the relevant denominator is zero.

PER-DEFECT-CLASS BREAKDOWN
  The same four-way tally (correct / false_match / false_clear /
  tie_ambiguous), grouped by `defect_class`, so a reader can see whether
  failures (or honest ties) cluster in one defect family rather than
  spreading evenly.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import random
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from reconagent.camt053 import parse_camt053_file
from reconagent.fx import (
    BENIGN_FX_DRIFT,
    DATA_ENTRY_ERROR,
    FEE_MISMATCH,
    FLAGGED_FX_DRIFT,
    NO_VARIANCE,
    UNRESOLVED,
    decompose_variance,
    load_reference_rates,
)
from reconagent.match import AMBIGUOUS, MATCHED, PARTIAL, TIE_AMBIGUOUS, MatchResult, match_all
from reconagent.razorpay import parse_razorpay_settlements

# Fixed order, all six named every time -- see decomposition_breakdown().
DECOMPOSITION_CATEGORIES = (
    NO_VARIANCE,
    BENIGN_FX_DRIFT,
    FLAGGED_FX_DRIFT,
    FEE_MISMATCH,
    DATA_ENTRY_ERROR,
    UNRESOLVED,
)

REPO = Path(__file__).resolve().parent.parent
DATA_ROOT = (REPO / "data").resolve()
DEFAULT_REPORT_JSON = REPO / "reports" / "eval_report.json"
DEFAULT_REPORT_MD = REPO / "reports" / "eval_report.md"

ASSERTED = (MATCHED, PARTIAL)

COVERAGE_GAPS = (
    "no FEE_MISMATCH case in the holdout set",
    "no DATA_ENTRY_ERROR case in the holdout set",
    "no overdue EDPMS receipt in ground truth as of 2026-08-31",
)
FX_METRICS_NOTE = (
    "FX attribution accuracy is not included in this pass -- matching "
    "accuracy and FX attribution accuracy are different failure surfaces "
    "and this harness reports the former only."
)


# --------------------------------------------------------------------------
# Loading a split
# --------------------------------------------------------------------------


@dataclass
class Split:
    name: str
    truth: dict
    settlements: list
    credits: list
    results: dict  # bank_txn_id -> MatchResult
    data_dir: Path
    prefix: str


def load_split(data_dir: Path, prefix: str, name: str) -> Split:
    truth = json.loads((data_dir / f"{prefix}ground_truth.json").read_text())
    settlements = parse_razorpay_settlements(data_dir / f"{prefix}razorpay_settlements.csv")
    credits = parse_camt053_file(data_dir / f"{prefix}bank_statement.camt053.xml")
    results = {r.bank_txn_id: r for r in match_all(credits, settlements)}
    return Split(name, truth, settlements, credits, results, data_dir, prefix)


def load_main() -> Split:
    return load_split(REPO / "data", "", "main")


def load_holdout() -> Split:
    return load_split(REPO / "data" / "holdout", "HOLDOUT_", "holdout")


def linked_cases(truth: dict) -> list[dict]:
    """Ground-truth cases that name a real bank credit. `timing_pending`
    cases do not (spec section 5) and are excluded here, not scored as
    misses -- there is no credit for the matcher to have judged."""
    return [c for c in truth["cases"] if c["expected_link"]["bank_txn_id"]]


# --------------------------------------------------------------------------
# Variance decomposition breakdown -- a descriptive tally of what
# `reconagent.fx.decompose_variance` produces for every settlement in a
# split, not a matching-accuracy metric (see FX_METRICS_NOTE and the
# module docstring: the two failure surfaces stay separate, this just
# stops the tally from being invisible to a reader of this report).
# --------------------------------------------------------------------------


def decomposition_breakdown(split: Split) -> dict[str, int]:
    """Count `decompose_variance` outcomes across every settlement in the
    split, keyed by attribution. All six category names are always
    present, zero-valued ones included, so a reader can see FEE_MISMATCH
    and DATA_ENTRY_ERROR as real categories in the schema even on a split
    (currently: holdout) where they happen to sit at zero."""
    reference = load_reference_rates(split.data_dir / f"{split.prefix}fx_reference_rates.csv")
    counts = {category: 0 for category in DECOMPOSITION_CATEGORIES}
    for record in split.settlements:
        decomposition = decompose_variance(record, reference)
        counts[decomposition.attribution] += 1
    return counts


# --------------------------------------------------------------------------
# The classifier -- the one function every metric in this module goes
# through, so a bug in the definition is a bug in one place.
# --------------------------------------------------------------------------


def classify(result: MatchResult | None, case: dict) -> str:
    """"correct" | "false_match" | "false_clear" | "tie_ambiguous" for one
    (system result, ground-truth case) pair. See the module docstring for
    the exact rule.

    `result=None` means "the system asserted nothing for this credit" --
    used directly by the real run (an AMBIGUOUS/UNMATCHED result) and by
    the threshold sweep (a result withheld for low confidence).

    TIE_AMBIGUOUS is Stage 2's genuine subset-sum tie (see
    reconagent.match): several distinct subsets landed on the identical
    minimum residual and there is no arithmetic basis to choose. When
    ground truth says a link exists, that is a different failure mode from
    "no evidence at all" (UNMATCHED) or Stage 1's reference collision
    (AMBIGUOUS) -- the matcher found the right answer among indistinguishable
    siblings and honestly refused to guess, so it gets its own verdict
    rather than being folded into false_clear. When ground truth says no
    link exists at all, a TIE_AMBIGUOUS result is scored the same as any
    other non-assertion -- "correct" -- exactly like AMBIGUOUS/UNMATCHED."""
    truth_resolution = case["expected_link_resolution"]
    truth_has_link = truth_resolution in (MATCHED, PARTIAL)

    asserted = result is not None and result.resolution in ASSERTED
    if not asserted:
        if not truth_has_link:
            return "correct"
        if result is not None and result.resolution == TIE_AMBIGUOUS:
            return "tie_ambiguous"
        return "false_clear"

    if not truth_has_link:
        return "false_match"

    truth_settlements = set(case["expected_link"]["covers_settlement_ids"])
    same_subset = set(result.settlement_ids) == truth_settlements
    same_resolution = result.resolution == truth_resolution
    return "correct" if (same_subset and same_resolution) else "false_match"


# --------------------------------------------------------------------------
# Metrics over a split
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Metrics:
    split: str
    total_linked: int
    true_link_count: int
    asserted_count: int
    correct: int
    false_match: int
    false_clear: int
    tie_ambiguous: int
    false_match_rate: float
    false_clear_rate: float
    tie_ambiguous_rate: float
    match_rate: float
    precision: float | None
    recall: float | None
    by_defect_class: dict[str, dict[str, float | int]]


def _tally(cases: list[dict], results: dict[str, MatchResult]) -> dict[str, int]:
    out = {"total": 0, "correct": 0, "false_match": 0, "false_clear": 0,
           "tie_ambiguous": 0, "true_link": 0, "asserted": 0}
    for c in cases:
        out["total"] += 1
        if c["expected_link_resolution"] in (MATCHED, PARTIAL):
            out["true_link"] += 1
        r = results.get(c["expected_link"]["bank_txn_id"])
        if r is not None and r.resolution in ASSERTED:
            out["asserted"] += 1
        verdict = classify(r, c)
        out[verdict] += 1
    return out


def compute_metrics(split: Split) -> Metrics:
    cases = linked_cases(split.truth)
    t = _tally(cases, split.results)

    by_class: dict[str, dict[str, float | int]] = {}
    classes = sorted({c["defect_class"] for c in cases})
    for dc in classes:
        class_cases = [c for c in cases if c["defect_class"] == dc]
        ct = _tally(class_cases, split.results)
        by_class[dc] = {
            "total": ct["total"],
            "correct": ct["correct"],
            "false_match": ct["false_match"],
            "false_clear": ct["false_clear"],
            "tie_ambiguous": ct["tie_ambiguous"],
            "match_rate": _ratio(ct["correct"], ct["total"]),
        }

    return Metrics(
        split=split.name,
        total_linked=t["total"],
        true_link_count=t["true_link"],
        asserted_count=t["asserted"],
        correct=t["correct"],
        false_match=t["false_match"],
        false_clear=t["false_clear"],
        tie_ambiguous=t["tie_ambiguous"],
        false_match_rate=_ratio(t["false_match"], t["total"]),
        false_clear_rate=_ratio(t["false_clear"], t["true_link"]),
        tie_ambiguous_rate=_ratio(t["tie_ambiguous"], t["true_link"]),
        match_rate=_ratio(t["correct"], t["total"]),
        precision=_ratio(t["correct"], t["asserted"]) if t["asserted"] else None,
        recall=_ratio(t["correct"], t["true_link"]) if t["true_link"] else None,
        by_defect_class=by_class,
    )


def _ratio(n: int, d: int) -> float:
    return n / d if d else 0.0


# --------------------------------------------------------------------------
# Confidence threshold sweep (spec section 4 Stage 5 -- input for a later
# unit; this harness does not pick a threshold, just reports the table)
# --------------------------------------------------------------------------


def threshold_sweep(split: Split, thresholds=tuple(x / 10 for x in range(11))) -> list[dict]:
    cases = linked_cases(split.truth)
    rows = []
    for th in thresholds:
        withheld = {
            bid: (r if not (r.resolution in ASSERTED and r.confidence < th) else None)
            for bid, r in split.results.items()
        }
        t = _tally(cases, withheld)
        rows.append({
            "threshold": th,
            "false_match_rate": _ratio(t["false_match"], t["total"]),
            "false_clear_rate": _ratio(t["false_clear"], t["true_link"]),
        })
    return rows


# --------------------------------------------------------------------------
# Mutation testing (spec section 9 / 12) -- the credibility check on the
# metric itself, not on the matcher. We corrupt the matcher's OUTPUT
# (swap a wrong settlement id into an already-computed, correct
# MatchResult) rather than the input data: corrupting the input would
# re-test whether the matcher can still find the right answer through
# noise, which is a different question from the one this test asks --
# "does false_match_rate actually move when a wrong link is present."
# --------------------------------------------------------------------------


def mutate_results(
    split: Split,
    rate: float,
    rng: random.Random,
) -> tuple[dict[str, MatchResult], int]:
    """Corrupt `rate` fraction of the split's *correctly* resolved linked
    credits by replacing their settlement_ids with a same-cardinality
    tuple that is NOT the true subset. A single-settlement credit gets a
    wrong single id; a bundle credit gets a wrong same-size subset --
    the exact failure mode Stage 2 exists to prevent. Returns the
    mutated results dict and how many results were actually mutated."""
    cases_by_bid = {c["expected_link"]["bank_txn_id"]: c for c in linked_cases(split.truth)}
    all_settlement_ids = [s.record_id for s in split.settlements]

    good_bids = [
        bid for bid, r in split.results.items()
        if bid in cases_by_bid and classify(r, cases_by_bid[bid]) == "correct"
    ]
    n = round(len(good_bids) * rate)
    chosen = rng.sample(good_bids, n) if n else []

    mutated = dict(split.results)
    for bid in chosen:
        r = mutated[bid]
        true_set = set(r.settlement_ids)
        k = len(r.settlement_ids)
        wrong_pool = [s for s in all_settlement_ids if s not in true_set]
        wrong = tuple(sorted(rng.sample(wrong_pool, min(k, len(wrong_pool)))))
        mutated[bid] = replace(r, settlement_ids=wrong)
    return mutated, len(chosen)


def mutation_sweep(
    split: Split,
    rates=(0.0, 0.05, 0.2, 0.5),
    seed: int = 20260903,
) -> list[dict]:
    cases = linked_cases(split.truth)
    rows = []
    for rate in rates:
        rng = random.Random(seed)  # fresh, identical stream per rate
        mutated, n_mutated = mutate_results(split, rate, rng)
        t = _tally(cases, mutated)
        rows.append({
            "mutation_rate": rate,
            "mutated_count": n_mutated,
            "false_match_rate": _ratio(t["false_match"], t["total"]),
        })
    return rows


def mutate_one_bundle(split: Split) -> dict | None:
    """Explicitly swap in the labelled decoy subset for one correctly
    resolved subset_sum_bundle case, so the wrong-subset failure mode is
    demonstrated on a real bundle rather than only by chance inside the
    random sweep above. Returns None if the split has no such case."""
    for c in linked_cases(split.truth):
        if c["defect_class"] != "subset_sum_bundle":
            continue
        bid = c["expected_link"]["bank_txn_id"]
        r = split.results.get(bid)
        if r is None or classify(r, c) != "correct":
            continue
        decoys = c.get("details", {}).get("decoy_settlement_ids")
        wrong = tuple(sorted(decoys)) if decoys else None
        if not wrong or set(wrong) == set(r.settlement_ids):
            continue
        mutated_r = replace(r, settlement_ids=wrong)
        before = classify(r, c)
        after = classify(mutated_r, c)
        return {
            "case_id": c["case_id"],
            "bank_txn_id": bid,
            "true_subset": sorted(r.settlement_ids),
            "wrong_subset_used": list(wrong),
            "verdict_before": before,
            "verdict_after": after,
        }
    return None


# --------------------------------------------------------------------------
# Throughput at multiple record-count scales
# --------------------------------------------------------------------------


def _load_generator_module():
    spec = importlib.util.spec_from_file_location(
        "generate_synthetic", REPO / "scripts" / "generate_synthetic.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["generate_synthetic"] = mod
    spec.loader.exec_module(mod)
    return mod


def throughput_table(scales=(200, 1000, 5000), seed: int = 20260903) -> list[dict]:
    gen = _load_generator_module()
    rows = []
    for scale in scales:
        out_dir = Path(tempfile.mkdtemp(prefix="reconagent_eval_throughput_"))
        gen.generate(seed, scale, out_dir)
        settlements = parse_razorpay_settlements(out_dir / "razorpay_settlements.csv")
        credits = parse_camt053_file(out_dir / "bank_statement.camt053.xml")

        t0 = time.perf_counter()
        match_all(credits, settlements)
        elapsed = time.perf_counter() - t0

        rows.append({
            "scale": scale,
            "settlements": len(settlements),
            "credits": len(credits),
            "seconds": elapsed,
            "records_per_sec": (len(credits) / elapsed) if elapsed > 0 else None,
        })
    return rows


# --------------------------------------------------------------------------
# Report generation -- a format string, not a template engine.
# --------------------------------------------------------------------------


def _guard_output_path(path: Path) -> None:
    resolved = path.resolve()
    if resolved == DATA_ROOT or DATA_ROOT in resolved.parents:
        raise ValueError(
            f"refusing to write eval report over {DATA_ROOT} (got {path}) -- "
            "data/ is committed ground truth"
        )


def build_report(
    main: Split,
    holdout: Split,
    *,
    scales=(200, 1000, 5000),
    mutation_rates=(0.0, 0.05, 0.2, 0.5),
    thresholds=tuple(x / 10 for x in range(11)),
    seed: int = 20260903,
) -> dict:
    main_metrics = compute_metrics(main)
    holdout_metrics = compute_metrics(holdout)
    return {
        "splits": {
            "main": asdict(main_metrics),
            "holdout": asdict(holdout_metrics),
        },
        "coverage_gaps": list(COVERAGE_GAPS),
        "fx_metrics_note": FX_METRICS_NOTE,
        "decomposition": {
            "main": decomposition_breakdown(main),
            "holdout": decomposition_breakdown(holdout),
        },
        "throughput": throughput_table(scales, seed=seed),
        "mutation_test": {
            "sweep": mutation_sweep(main, mutation_rates, seed=seed),
            "bundle_wrong_subset": mutate_one_bundle(main),
        },
        "threshold_sweep": {
            "main": threshold_sweep(main, thresholds),
            "holdout": threshold_sweep(holdout, thresholds),
        },
    }


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.2%}"


def render_markdown(report: dict) -> str:
    m = report["splits"]["main"]
    h = report["splits"]["holdout"]
    lines = [
        "# Reconciliation matcher -- evaluation report",
        "",
        "## Headline: false-match rate and false-clear rate",
        "",
        "tie-ambiguous rate is reported alongside false-clear rate, not "
        "folded into it: it is the same denominator (cases ground truth "
        "says are linked) but a different failure mode -- Stage 2 found "
        "the right settlements among several that tied on residual and "
        "honestly declined to guess, rather than finding no evidence at "
        "all.",
        "",
        "| split | false-match rate | false-clear rate | tie-ambiguous rate |",
        "|---|---|---|---|",
        f"| main | {_pct(m['false_match_rate'])} ({m['false_match']}/{m['total_linked']}) "
        f"| {_pct(m['false_clear_rate'])} ({m['false_clear']}/{m['true_link_count']}) "
        f"| {_pct(m['tie_ambiguous_rate'])} ({m['tie_ambiguous']}/{m['true_link_count']}) |",
        f"| holdout | {_pct(h['false_match_rate'])} ({h['false_match']}/{h['total_linked']}) "
        f"| {_pct(h['false_clear_rate'])} ({h['false_clear']}/{h['true_link_count']}) "
        f"| {_pct(h['tie_ambiguous_rate'])} ({h['tie_ambiguous']}/{h['true_link_count']}) |",
        "",
        "## Match rate, precision, recall",
        "",
        "| split | match rate | precision | recall |",
        "|---|---|---|---|",
        f"| main | {_pct(m['match_rate'])} | {_pct(m['precision'])} | {_pct(m['recall'])} |",
        f"| holdout | {_pct(h['match_rate'])} | {_pct(h['precision'])} | {_pct(h['recall'])} |",
        "",
        "## Coverage gaps",
        "",
        *[f"- {g}" for g in report["coverage_gaps"]],
        "",
        f"FX metrics: {report['fx_metrics_note']}",
        "",
    ]
    decomposition = report.get("decomposition")
    if decomposition:
        lines += [
            "## FX variance attribution (descriptive tally, not a matching-accuracy metric)",
            "",
            "This table counts what `decompose_variance` produced for every "
            "settlement in the split -- it is not graded against ground truth "
            "here, so there is no correct/wrong column the way the per-defect-"
            "class breakdown below has one. Whether an attribution is *correct* "
            "would require grading against ground truth's "
            "`expected_exception_category`, a different question from \"does "
            "this category exist and get produced\", and is out of scope for "
            "this table. See the FX metrics note above: matching accuracy and "
            "FX attribution are different failure surfaces and stay separate.",
            "",
            "| attribution | main | holdout |",
            "|---|---|---|",
        ]
        main_counts = decomposition.get("main", {})
        holdout_counts = decomposition.get("holdout", {})
        for category in DECOMPOSITION_CATEGORIES:
            lines.append(
                f"| {category} | {main_counts.get(category, 0)} "
                f"| {holdout_counts.get(category, 0)} |"
            )
        lines.append("")
    lines += [
        "## Per-defect-class breakdown (main)",
        "",
        "| defect class | total | correct | false match | false clear | tie ambiguous | match rate |",
        "|---|---|---|---|---|---|---|",
    ]
    for dc, row in m["by_defect_class"].items():
        lines.append(
            f"| {dc} | {row['total']} | {row['correct']} | {row['false_match']} "
            f"| {row['false_clear']} | {row['tie_ambiguous']} | {_pct(row['match_rate'])} |"
        )
    lines += [
        "",
        "## Throughput",
        "",
        "| scale (settlements) | credits | seconds | records/sec |",
        "|---|---|---|---|",
    ]
    for row in report["throughput"]:
        rps = f"{row['records_per_sec']:.0f}" if row["records_per_sec"] else "n/a"
        lines.append(f"| {row['scale']} | {row['credits']} | {row['seconds']:.4f} | {rps} |")
    lines += [
        "",
        "## Mutation test (harness credibility check, not a matcher metric)",
        "",
        "Corrupts the matcher's *output* -- swaps a wrong settlement id/subset "
        "into already-computed, correct MatchResults -- to confirm false-match "
        "rate actually moves in response to real error, rather than reporting "
        "a vacuous zero.",
        "",
        "| mutation rate | credits mutated | false-match rate |",
        "|---|---|---|",
    ]
    for row in report["mutation_test"]["sweep"]:
        lines.append(
            f"| {row['mutation_rate']:.0%} | {row['mutated_count']} | {_pct(row['false_match_rate'])} |"
        )
    bundle = report["mutation_test"]["bundle_wrong_subset"]
    if bundle:
        lines += [
            "",
            f"Bundle wrong-subset check ({bundle['case_id']}): true subset "
            f"{bundle['true_subset']} swapped for {bundle['wrong_subset_used']} "
            f"-- verdict {bundle['verdict_before']} -> {bundle['verdict_after']}.",
        ]
    lines += [
        "",
        "## Confidence threshold sweep (input for the abstention gate; no "
        "threshold is chosen here)",
        "",
        "| threshold | main false-match | main false-clear | holdout false-match | holdout false-clear |",
        "|---|---|---|---|---|",
    ]
    for m_row, h_row in zip(report["threshold_sweep"]["main"], report["threshold_sweep"]["holdout"]):
        lines.append(
            f"| {m_row['threshold']:.1f} | {_pct(m_row['false_match_rate'])} "
            f"| {_pct(m_row['false_clear_rate'])} | {_pct(h_row['false_match_rate'])} "
            f"| {_pct(h_row['false_clear_rate'])} |"
        )
    return "\n".join(lines) + "\n"


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
    ap.add_argument("--scales", type=int, nargs="+", default=[200, 1000, 5000])
    ap.add_argument("--seed", type=int, default=20260903)
    args = ap.parse_args()

    main_split = load_main()
    holdout_split = load_holdout()
    report = build_report(main_split, holdout_split, scales=tuple(args.scales), seed=args.seed)
    write_report(report, args.out_json, args.out_md)
    print(f"wrote {args.out_json}")
    print(f"wrote {args.out_md}")

    m = report["splits"]["main"]
    h = report["splits"]["holdout"]
    print(f"main:    false-match {m['false_match_rate']:.2%}  false-clear {m['false_clear_rate']:.2%}  tie-ambiguous {m['tie_ambiguous_rate']:.2%}")
    print(f"holdout: false-match {h['false_match_rate']:.2%}  false-clear {h['false_clear_rate']:.2%}  tie-ambiguous {h['tie_ambiguous_rate']:.2%}")


if __name__ == "__main__":
    main()
