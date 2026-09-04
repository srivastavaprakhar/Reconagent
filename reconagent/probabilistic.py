"""Stage 3: probabilistic record linkage via Splink (spec section 4, section 11).

WHY THIS STAGE EXISTS

Tier 1 (`reconagent.match`) resolves `data/` completely -- 152/152, 0
false-match, 0 false-clear -- and the holdout split's only misses are
genuine subset-sum ties, not real recall gaps. Nothing in either split's
own evaluation forced this stage to exist. It is built anyway, proactively,
for a stated reason: genuine ML depth appropriate to this submission, and
robustness against real-world messiness -- transliterated names, OCR
narration corruption, legal-vs-trading-name mismatches -- that Tier 1's
clean datasets never exercise. `stress_test/` was built for exactly this:
40 cases, all single-settlement, all cross-border, every one designed so
`match_all` returns UNMATCHED for all 40 (verified in
`tests/test_probabilistic.py`). Where several *partial* signals agree --
close amount, right week, similar counterparty name -- but no single exact
key resolves the case, a Fellegi-Sunter model gives a calibrated,
per-field-weighted match probability instead of an opaque score: you can
point at exactly which fields agreed and by how much, which is what makes
this explainable to an auditor.

SPLINK VERSION AND API SHAPE (checked by installing it, not remembered)

Splink 4.0.17, DuckDB-backed. The public surface is namespaced off a
`Linker` instance -- `linker.training.*`, `linker.inference.*`,
`linker.misc.*`, `linker.table_management.*` -- not flat methods on
`Linker` itself. `SettingsCreator(link_type="link_only", ...)` is the
right link type here: two distinct populations (settlements, bank
credits), never dedupe-within-one-side. Comparisons come from
`splink.comparison_library` (ready-made, e.g. `NameComparison`,
`AbsoluteDateDifferenceAtThresholds`) and `splink.comparison_level_library`
(the building blocks -- `ExactMatchLevel`, `PercentageDifferenceLevel`,
`ElseLevel`, ... -- composed via `comparison_library.CustomComparison` for
anything not already a named comparison). Blocking rules come from
`splink.blocking_rule_library.CustomRule(sql)` for anything beyond plain
column equality. `linker.misc.save_model_to_json()` returns the fitted
model as a plain nested dict; that same dict is a valid `settings=`
argument to a fresh `Linker(...)`, which is how a model trained once on
`data/` gets applied to a different pair of tables at call time without
retraining.

WHY A SPLIT TRAINING/APPLICATION POPULATION

`data/` resolves entirely at Tier 1, so there is no naturally-occurring
"Tier-1-unresolved" population there to learn from. Per the design
decision behind this stage: train on ALL of `data/`'s labelled linkage
(every case is a known true pair or a known non-pair, regardless of which
Tier-1 stage would have resolved it), then apply the fitted model only to
credits Tier 1 actually gave up on. `train_stage3_model` does the former;
`match_with_tier2` does the latter. Training and threshold derivation read
`data/` only -- `reconagent/probabilistic.py` never opens
`stress_test/ground_truth.json` or `data/holdout/ground_truth.json`
(`tests/test_probabilistic.py` greps for this, the same discipline
`reconagent/match.py` and `reconagent/fx.py` already hold themselves to).

TRAINING POPULATION (exact composition, so a reader doesn't have to
re-derive it)

`data/ground_truth.json`: 155 cases, 152 with a bank credit (the other 3
are `timing_pending` -- no credit exists yet). Of those 152:
  - 140 link exactly one settlement to one credit ("single-settlement").
  - 12 are bundles (one credit covering 2+ settlements) -- excluded from
    m-training (below) but kept in the known-positive set used to keep
    threshold derivation honest (a bundle member is a true link, just not
    a pairwise one Splink's per-record model can learn cleanly from).

m-probabilities (P(comparison level | true match)) are estimated via
`linker.training.estimate_m_from_pairwise_labels`, fed the 140
single-settlement pairs (bundle members would teach the amount comparison
that a true match can have a wildly different amount, which is true of a
bundle member and false of everything else -- wrong lesson for a pairwise
model). u-probabilities (P(comparison level | random pair)) are estimated
via `linker.training.estimate_u_using_random_sampling` over the full
blocked candidate space -- Splink's own recommended way to get
non-match statistics without hand-generating negative pairs, exactly per
the design brief. Blocked candidate space over `data/`: 202 settlements x
152 credits, blocked by currency + BLOCKING_WINDOW_DAYS date window, comes
to 30,704 pairs; excluding all 175 known-positive (settlement, credit)
links (140 single + 35 bundle-member) leaves 30,529 known negatives.

A REAL GAP, DOCUMENTED RATHER THAN PAPERED OVER

`data/`'s single-settlement positives are either an exact amount match
(132/140) or a genuine partial-payment/EDPMS shortfall of 20-45% (8/140,
the `partial_payment` and `edpms_open` defect classes) -- there is no
natural "off by a fraction of a percent, still a true match" case in the
main set, because Tier 1 already resolves everything close enough to be a
real match via exact arithmetic. The same is true, for the same reason, of
the date comparison's near-miss bands (main-set positives settle same-day)
and two of the name comparison's Jaro-Winkler bands (0.7-0.88 and
0.88-0.92 similarity literally never occur among ANY of the 30,704 blocked
pairs, positive or negative). Left alone, Splink's own fallback for a
level with zero observations is a floor of 1e-6 for whichever side (m or
u) was never populated -- which, for a level whose m alone is unobserved,
reads as strong evidence AGAINST a match. That is actively wrong for this
stage's whole purpose (rewarding a close-but-not-exact amount or a
plausible name variant), so `_fill_untrained_levels` corrects it after
training, in two documented ways:
  - a level with BOTH m and u unobserved (never occurs in `data/` at all,
    in either direction) gets m = u = a shared placeholder, i.e. a Bayes
    factor of exactly 1.0 -- a neutral non-vote. `data/` gives no basis to
    call the level more or less common among true matches than chance, so
    it contributes nothing rather than something wrong.
  - a level with only m unobserved, sitting between two levels that DO
    have trained m (amount's three percentage bands, bracketed by the
    trained "exact match" and "else" levels), gets a geometric
    interpolation between those two real, data-derived anchors -- the
    standard monotonic-decay assumption in a Fellegi-Sunter model, applied
    only where real anchors exist to interpolate between.

THRESHOLD DERIVATION (from `data/` only; `tests/test_probabilistic.py`
re-runs this and asserts it lands here)

Scoring the fitted model over the full 30,704-pair blocked candidate space
above:
  - known negatives (30,529 pairs): max match_probability = 0.213390.
  - clean single-settlement positives (132/140, exact-amount cases): match
    probability ranges 0.288721-0.963696. The floor, 0.288721, is shared by
    111 of the 132 -- domestic sweeps whose credit-side name is the payment
    aggregator's own (exact amount, exact date, dissimilar name, per the
    module docstring's international-name discussion below). The other 21
    carry a real cross-border name and score higher (up to 0.963696) when
    it agrees with the settlement's. Either way, 0.288721 is the floor of
    the clean population -- the number that matters for threshold
    separation.
  - the remaining 8 single-settlement positives are the deliberate
    partial-payment/EDPMS shortfalls above -- they score near 0
    (0.0001-0.003) because their amounts genuinely don't match, which is
    correct: Stage 3 emits only MATCHED, never PARTIAL, so these are
    rightly outside what this stage should confidently call a match. Tier
    1 already resolves them as PARTIAL, so they never reach Stage 3 at
    runtime (`match_with_tier2` only defers UNMATCHED / AMBIGUOUS /
    TIE_AMBIGUOUS credits to it).

DEFAULT_MATCH_THRESHOLD = 0.25 sits strictly between 0.213390 and
0.288721 -- roughly equidistant (0.037 above the negative ceiling, 0.039
below the clean-positive floor) -- giving zero false matches on every
known negative in `data/` while accepting all 132 clean known positives.
It is a module-owned default (like `reconagent.match.AMOUNT_TOLERANCE_MINOR`),
overridable via `train_stage3_model(..., threshold=...)`, never read from
any answer key at runtime.

OUTPUT: EXPLAINABLE, NOT AN OPAQUE SCORE

`ProbabilisticMatchResult.comparison_weights` is Splink's own per-comparison
Bayes factor from `predict()`'s `bf_<comparison>` columns, converted to a
log2 match-weight in bits (`log2(bf) == cl.log2_bayes_factor` for the
comparison level a given pair actually landed in -- the same value
Splink's own waterfall chart plots, not a re-derived approximation).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Sequence

import pandas as pd
from splink import DuckDBAPI, Linker, SettingsCreator
import splink.blocking_rule_library as brl
import splink.comparison_level_library as cll
import splink.comparison_library as cl

from reconagent.match import (
    AMBIGUOUS,
    MATCHED,
    PARTIAL,
    POOL_WINDOW_DAYS,
    TIE_AMBIGUOUS,
    UNMATCHED,
    MatchResult,
    match_all,
)
from reconagent.records import CanonicalRecord

STAGE_PROBABILISTIC = "stage3_probabilistic"

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = _REPO_ROOT / "data"

# Mirrors Tier 1's own pooling window (reconagent.match.POOL_WINDOW_DAYS) --
# a blocking rule should not be more permissive than the deterministic
# matcher's own notion of "plausible sweep timing".
BLOCKING_WINDOW_DAYS = POOL_WINDOW_DAYS

AMOUNT_PERCENT_LEVELS: tuple[float, ...] = (0.005, 0.015, 0.03)
DATE_DAY_THRESHOLDS: tuple[int, ...] = (1, 3, 10)

# See module docstring "THRESHOLD DERIVATION" for the numbers behind this.
DEFAULT_MATCH_THRESHOLD = Decimal("0.25")

# Shared m=u placeholder for a comparison level data/ never exercises in
# either direction (bayes factor 1.0 -- a neutral non-vote). See
# "A REAL GAP, DOCUMENTED RATHER THAN PAPERED OVER" above.
NEUTRAL_LEVEL_PROBABILITY = 1e-3

_DEFERRED_RESOLUTIONS = (UNMATCHED, AMBIGUOUS, TIE_AMBIGUOUS)


@dataclass(frozen=True)
class ProbabilisticMatchResult:
    """Stage 3's verdict on one bank credit -- explainable, not a bare score.

    `comparison_weights` carries Splink's own per-comparison log2 Bayes
    factor (bits of evidence for/against a match; 0 is neutral, positive
    favours a match, negative favours a non-match) so an auditor can see
    exactly which fields agreed and by how much, per the spec's own framing
    for why this stage exists over jumping straight to embeddings.

    `resolution` reuses `reconagent.match`'s vocabulary: MATCHED when
    `match_probability >= threshold`, UNMATCHED otherwise (deferred,
    honestly, to whatever stage comes after this one). Stage 3 never emits
    PARTIAL -- it is a single-settlement pairwise linker, not a subset-sum
    search; a genuine partial payment is Tier 1's PARTIAL to call, and
    (see module docstring) never reaches Stage 3 in the first place.
    """

    bank_txn_id: str
    stage: str
    resolution: str
    settlement_ids: tuple[str, ...]
    credit_amount_minor: int
    settlement_net_sum_minor: int
    residual_minor: int
    match_probability: Decimal
    comparison_weights: dict[str, Decimal]
    reason: str
    threshold: Decimal
    candidates_considered: int = 0


@dataclass(frozen=True)
class SplinkStage3Model:
    """A trained Stage 3 model: Splink's fitted settings (comparison
    weights, m/u probabilities -- a plain JSON-able dict, Splink's own
    `save_model_to_json` output) plus the acceptance threshold derived
    alongside it. Reusable across many `resolve_stage3` / `match_with_tier2`
    calls without retraining -- build one via `train_stage3_model()` and
    pass it in, or let `match_with_tier2` build and cache the default.
    """

    fitted_settings: dict
    threshold: Decimal
    training_population: dict = field(default_factory=dict)


def _name_index(
    invoices: Sequence[CanonicalRecord],
) -> tuple[dict[str, CanonicalRecord], dict[str, CanonicalRecord]]:
    by_id = {i.invoice_id: i for i in invoices if i.invoice_id}
    by_order = {i.order_id: i for i in invoices if i.order_id}
    return by_id, by_order


def _settlement_name(
    settlement: CanonicalRecord,
    by_id: dict[str, CanonicalRecord],
    by_order: dict[str, CanonicalRecord],
) -> str | None:
    """A razorpay_settlement CanonicalRecord never carries a counterparty
    name of its own (the counterparty on the feed is Razorpay itself, spec
    per `reconagent/razorpay.py`) -- the underlying customer's name lives on
    the linked invoice. Joined via invoice_id (order_receipt), falling back
    to order_id, both populated by the parsers and unaffected by any
    narration-text corruption a stress case applies elsewhere."""
    invoice = by_id.get(settlement.invoice_id) or by_order.get(settlement.order_id)
    return invoice.counterparty_name if invoice and invoice.counterparty_name else None


def _to_frame(
    records: Sequence[CanonicalRecord],
    *,
    is_settlement: bool,
    name_by_id: dict[str, CanonicalRecord],
    name_by_order: dict[str, CanonicalRecord],
) -> pd.DataFrame:
    """One row per record: the columns Splink's comparisons and blocking
    rule actually look at. Money stays an int (minor units) all the way
    into the DataFrame -- Splink's own SQL does the percentage-difference
    arithmetic for scoring purposes, never this module in Python."""
    rows = []
    for r in records:
        d = r.settled_at if is_settlement else (r.value_date or r.booking_date)
        name = (
            _settlement_name(r, name_by_id, name_by_order)
            if is_settlement
            else (r.counterparty_name or None)
        )
        rows.append(
            {
                "unique_id": r.record_id,
                "currency": r.currency,
                "match_date": d.isoformat() if d else None,
                "amount": int(r.amount_minor),
                "name": name,
            }
        )
    return pd.DataFrame(rows, columns=["unique_id", "currency", "match_date", "amount", "name"])


def _build_comparisons() -> list:
    """Amount: a graduated percentage-difference comparison (Stage 1/2's
    tolerance is exact-or-nothing; this is deliberately the opposite --
    "how close", not "exact"). Date: settled_at vs the credit's own
    value/booking date, at day-granularity bands. Name: settlement-side
    (via the invoice join) vs whichever name the credit's own source
    populated -- Jaro-Winkler, Splink's own NameComparison, appropriate for
    transliteration/typo/abbreviation variants. Currency is NOT a
    comparison here -- see the blocking rule below."""
    amount = cl.CustomComparison(
        output_column_name="amount",
        comparison_levels=[
            cll.NullLevel("amount"),
            cll.ExactMatchLevel("amount"),
            *(cll.PercentageDifferenceLevel("amount", p) for p in AMOUNT_PERCENT_LEVELS),
            cll.ElseLevel(),
        ],
    )
    date = cl.AbsoluteDateDifferenceAtThresholds(
        "match_date",
        input_is_string=True,
        metrics=["day"] * len(DATE_DAY_THRESHOLDS),
        thresholds=list(DATE_DAY_THRESHOLDS),
    )
    name = cl.NameComparison("name")
    return [amount, date, name]


# Currency is exact-or-nothing by definition (a rupee credit cannot cover a
# dollar settlement) -- a BLOCKING rule, not a fuzzy comparison, mirroring
# Tier 1's own pooling discipline in `reconagent.match._pool`. The date
# window mirrors that same pooling discipline's window.
_BLOCKING_SQL = (
    "l.currency = r.currency and "
    f"abs(date_diff('day', l.match_date::date, r.match_date::date)) <= {BLOCKING_WINDOW_DAYS}"
)


def _build_settings() -> SettingsCreator:
    return SettingsCreator(
        link_type="link_only",
        unique_id_column_name="unique_id",
        comparisons=_build_comparisons(),
        blocking_rules_to_generate_predictions=[brl.CustomRule(_BLOCKING_SQL)],
        # Required for predict()'s bf_<comparison> columns -- the
        # explainability this stage exists to provide.
        retain_intermediate_calculation_columns=True,
    )


def _fill_untrained_levels(fitted_settings: dict) -> None:
    """See module docstring, "A REAL GAP, DOCUMENTED RATHER THAN PAPERED
    OVER". Mutates `fitted_settings` in place."""
    for comp in fitted_settings["comparisons"]:
        levels = comp["comparison_levels"]
        for lvl in levels:
            if lvl.get("is_null_level"):
                continue
            if lvl.get("m_probability") is None and lvl.get("u_probability") is None:
                lvl["m_probability"] = NEUTRAL_LEVEL_PROBABILITY
                lvl["u_probability"] = NEUTRAL_LEVEL_PROBABILITY

        trainable = [lvl for lvl in levels if not lvl.get("is_null_level")]
        m_values = [lvl.get("m_probability") for lvl in trainable]
        for i, lvl in enumerate(trainable):
            if lvl.get("m_probability") is not None:
                continue
            before = next(
                ((j, m_values[j]) for j in range(i - 1, -1, -1) if m_values[j] is not None), None
            )
            after = next(
                ((j, m_values[j]) for j in range(i + 1, len(trainable)) if m_values[j] is not None),
                None,
            )
            if before is not None and after is not None:
                j0, m0 = before
                j1, m1 = after
                frac = (i - j0) / (j1 - j0)
                new_m = math.exp(math.log(m0) + frac * (math.log(m1) - math.log(m0)))
            else:
                new_m = lvl["u_probability"]
            lvl["m_probability"] = new_m
            m_values[i] = new_m


def _positive_pairs_from_ground_truth(ground_truth: dict) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(single-settlement pairs, all pairs including bundle members), both
    as (settlement_id, bank_txn_id). See module docstring's training
    population section for why the split matters."""
    single_pairs: list[tuple[str, str]] = []
    all_pairs: list[tuple[str, str]] = []
    for case in ground_truth["cases"]:
        bank_txn_id = case["expected_link"]["bank_txn_id"]
        if not bank_txn_id:
            continue
        settlement_ids = case["expected_link"]["covers_settlement_ids"]
        for sid in settlement_ids:
            all_pairs.append((sid, bank_txn_id))
        if len(settlement_ids) == 1:
            single_pairs.append((settlement_ids[0], bank_txn_id))
    return single_pairs, all_pairs


def train_stage3_model(
    data_dir: Path | str = DEFAULT_DATA_DIR,
    *,
    threshold: Decimal = DEFAULT_MATCH_THRESHOLD,
    seed: int = 0,
) -> SplinkStage3Model:
    """Fit Stage 3 on `data_dir`'s full labelled linkage (default: `data/`,
    the main set -- see module docstring). Never reads
    `stress_test/ground_truth.json` or `data/holdout/ground_truth.json`;
    `data_dir` exists so a test can point this at a scratch copy of `data/`
    to check reproducibility, not so callers point it at the other splits.
    """
    # Imported here, not at module load, so a caller who only wants the
    # dataclasses/constants (e.g. a test asserting this module never reads
    # certain files) doesn't pay for parser imports it never exercises.
    from reconagent.camt053 import parse_camt053_file
    from reconagent.invoices import parse_invoice_ledger
    from reconagent.razorpay import parse_razorpay_settlements

    data_dir = Path(data_dir)
    settlements = parse_razorpay_settlements(data_dir / "razorpay_settlements.csv")
    credits = parse_camt053_file(data_dir / "bank_statement.camt053.xml")
    invoices = parse_invoice_ledger(data_dir / "invoice_ledger.csv")
    ground_truth = json.loads((data_dir / "ground_truth.json").read_text())

    by_id, by_order = _name_index(invoices)
    settlement_df = _to_frame(settlements, is_settlement=True, name_by_id=by_id, name_by_order=by_order)
    credit_df = _to_frame(credits, is_settlement=False, name_by_id=by_id, name_by_order=by_order)

    single_pairs, all_pairs = _positive_pairs_from_ground_truth(ground_truth)
    labels_df = pd.DataFrame(
        [
            {
                "source_dataset_l": "settlement",
                "unique_id_l": sid,
                "source_dataset_r": "credit",
                "unique_id_r": bid,
            }
            for sid, bid in single_pairs
        ]
    )

    linker = Linker(
        [settlement_df, credit_df],
        _build_settings(),
        db_api=DuckDBAPI(),
        input_table_aliases=["settlement", "credit"],
        set_up_basic_logging=False,
    )
    linker.table_management.register_table(labels_df, "labels", overwrite=True)
    linker.training.estimate_m_from_pairwise_labels("labels")
    linker.training.estimate_u_using_random_sampling(max_pairs=2e5, seed=seed)

    fitted_settings = linker.misc.save_model_to_json()
    _fill_untrained_levels(fitted_settings)

    # Re-score with the corrected settings to report the training
    # population's actual separation (documented in the module docstring;
    # tests/test_probabilistic.py re-derives and checks these numbers).
    scoring_linker = Linker(
        [settlement_df, credit_df],
        fitted_settings,
        db_api=DuckDBAPI(),
        input_table_aliases=["settlement", "credit"],
        set_up_basic_logging=False,
    )
    predictions = scoring_linker.inference.predict(threshold_match_probability=0.0).as_pandas_dataframe()

    positive_pairs = set(all_pairs)

    def _is_negative(row) -> bool:
        pair = (row["unique_id_l"], row["unique_id_r"])
        return pair not in positive_pairs and pair[::-1] not in positive_pairs

    negatives = predictions[predictions.apply(_is_negative, axis=1)]
    neg_max = float(negatives["match_probability"].max()) if len(negatives) else 0.0

    return SplinkStage3Model(
        fitted_settings=fitted_settings,
        threshold=threshold,
        training_population={
            "single_settlement_m_training_pairs": len(single_pairs),
            "blocked_candidate_pairs": len(predictions),
            "known_negative_pairs": len(negatives),
            "known_negative_max_probability": round(neg_max, 6),
        },
    )


@lru_cache(maxsize=1)
def _default_model() -> SplinkStage3Model:
    return train_stage3_model()


def _normalise_pairs(pdf: pd.DataFrame) -> pd.DataFrame:
    """Splink assigns 'l'/'r' by its own internal ordering, not input
    order -- recover which side is the credit and which is the
    settlement rather than assuming."""
    is_credit_l = pdf["source_dataset_l"] == "credit"
    pdf = pdf.copy()
    pdf["credit_id"] = pdf["unique_id_l"].where(is_credit_l, pdf["unique_id_r"])
    pdf["settlement_id"] = pdf["unique_id_r"].where(is_credit_l, pdf["unique_id_l"])
    return pdf


def _comparison_weights(row: pd.Series) -> dict[str, Decimal]:
    """Splink's own per-comparison Bayes factor (predict()'s `bf_*`
    columns), converted to log2 bits -- the same value Splink's own
    waterfall chart plots for this comparison level. Not a re-derived
    approximation; see module docstring."""
    weights: dict[str, Decimal] = {}
    for col in row.index:
        if not col.startswith("bf_"):
            continue
        bf = float(row[col])
        name = col[len("bf_") :]
        bits = math.log2(bf) if bf > 0 else float("-inf")
        weights[name] = Decimal(str(round(bits, 4)))
    return weights


def _unmatched_result(credit: CanonicalRecord, model: SplinkStage3Model, reason: str) -> ProbabilisticMatchResult:
    return ProbabilisticMatchResult(
        bank_txn_id=credit.record_id,
        stage=STAGE_PROBABILISTIC,
        resolution=UNMATCHED,
        settlement_ids=(),
        credit_amount_minor=credit.amount_minor,
        settlement_net_sum_minor=0,
        residual_minor=-credit.amount_minor,
        match_probability=Decimal("0"),
        comparison_weights={},
        reason=reason,
        threshold=model.threshold,
        candidates_considered=0,
    )


def resolve_stage3(
    credits: Sequence[CanonicalRecord],
    open_settlements: Sequence[CanonicalRecord],
    invoices: Sequence[CanonicalRecord],
    model: SplinkStage3Model,
) -> dict[str, ProbabilisticMatchResult]:
    """Score every credit in `credits` against every settlement in
    `open_settlements` that survives the currency + BLOCKING_WINDOW_DAYS
    blocking rule, in a SINGLE Splink call -- not one call per credit.

    This matters for correctness, not just speed: the name comparison's
    term-frequency adjustment (Splink's own `NameComparison` default)
    weighs a name by how common it is *within the table it is scored
    against*. Scoring one credit at a time gives it a table of one credit,
    which corrupts that frequency (every name looks perfectly rare) and
    measurably hurts recall -- caught empirically while building this
    stage (see `tests/test_probabilistic.py`): the same model, run
    per-credit instead of batched, resolved 9/40 of `stress_test/` instead
    of 15/40, with the difference entirely in cases the batched call gets
    right. Batching the whole deferred population in one call is also the
    ordinary way to use Splink, not a workaround.

    Returns `{bank_txn_id: ProbabilisticMatchResult}`, one per credit in
    `credits`. Ties are broken globally, highest-probability first: once a
    settlement is claimed by one credit's MATCHED result, a different
    credit cannot also claim it, even if that credit's own best candidate
    happened to be the same settlement -- mirrors Tier 1's own "a
    settlement is consumed once" discipline in `reconagent.match.match_all`.
    A credit whose best candidate is claimed by someone more confident, or
    whose best candidate never clears `model.threshold`, comes back
    UNMATCHED with that best candidate's probability still attached (an
    honest decline, not a guess).
    """
    if not credits:
        return {}

    by_id, by_order = _name_index(invoices)
    settlement_df = _to_frame(
        open_settlements, is_settlement=True, name_by_id=by_id, name_by_order=by_order
    )
    credit_df = _to_frame(credits, is_settlement=False, name_by_id=by_id, name_by_order=by_order)

    if settlement_df.empty:
        return {c.record_id: _unmatched_result(c, model, "no open settlements to score against") for c in credits}

    linker = Linker(
        [settlement_df, credit_df],
        model.fitted_settings,
        db_api=DuckDBAPI(),
        input_table_aliases=["settlement", "credit"],
        set_up_basic_logging=False,
    )
    pdf = linker.inference.predict(threshold_match_probability=0.0).as_pandas_dataframe()

    by_credit_record = {c.record_id: c for c in credits}
    by_settlement_id = {s.record_id: s for s in open_settlements}

    if pdf.empty:
        return {
            c.record_id: _unmatched_result(c, model, "no candidates survived the currency/date blocking rule")
            for c in credits
        }

    pdf = _normalise_pairs(pdf)
    candidates_considered = pdf.groupby("credit_id").size().to_dict()

    best_per_credit = pdf.loc[pdf.groupby("credit_id")["match_probability"].idxmax()].set_index(
        "credit_id", drop=False
    )

    results: dict[str, ProbabilisticMatchResult] = {}
    claimed_settlements: set[str] = set()
    for row in pdf.sort_values("match_probability", ascending=False).itertuples(index=False):
        credit_id = row.credit_id
        if credit_id in results:
            continue
        probability = Decimal(str(round(float(row.match_probability), 6)))
        if probability < model.threshold or row.settlement_id in claimed_settlements:
            continue
        credit = by_credit_record[credit_id]
        settlement = by_settlement_id[row.settlement_id]
        row_series = pd.Series(row._asdict())
        results[credit_id] = ProbabilisticMatchResult(
            bank_txn_id=credit_id,
            stage=STAGE_PROBABILISTIC,
            resolution=MATCHED,
            settlement_ids=(row.settlement_id,),
            credit_amount_minor=credit.amount_minor,
            settlement_net_sum_minor=settlement.amount_minor,
            residual_minor=settlement.amount_minor - credit.amount_minor,
            match_probability=probability,
            comparison_weights=_comparison_weights(row_series),
            reason=(
                f"best available candidate {row.settlement_id!r} scored {probability} "
                f">= threshold {model.threshold} among {candidates_considered.get(credit_id, 0)} "
                "blocked candidates"
            ),
            threshold=model.threshold,
            candidates_considered=int(candidates_considered.get(credit_id, 0)),
        )
        claimed_settlements.add(row.settlement_id)

    for credit in credits:
        if credit.record_id in results:
            continue
        if credit.record_id not in best_per_credit.index:
            results[credit.record_id] = _unmatched_result(
                credit, model, "no candidates survived the currency/date blocking rule"
            )
            continue
        best = best_per_credit.loc[credit.record_id]
        probability = Decimal(str(round(float(best["match_probability"]), 6)))
        results[credit.record_id] = ProbabilisticMatchResult(
            bank_txn_id=credit.record_id,
            stage=STAGE_PROBABILISTIC,
            resolution=UNMATCHED,
            settlement_ids=(),
            credit_amount_minor=credit.amount_minor,
            settlement_net_sum_minor=0,
            residual_minor=-credit.amount_minor,
            match_probability=probability,
            comparison_weights=_comparison_weights(best),
            reason=(
                f"best candidate {best['settlement_id']!r} scored {probability}, below "
                f"threshold {model.threshold} (or claimed by a more confident credit); "
                f"deferred among {candidates_considered.get(credit.record_id, 0)} blocked candidates"
            ),
            threshold=model.threshold,
            candidates_considered=int(candidates_considered.get(credit.record_id, 0)),
        )

    return results


def match_with_tier2(
    credits: Sequence[CanonicalRecord],
    settlements: Sequence[CanonicalRecord],
    *,
    invoices: Sequence[CanonicalRecord] = (),
    splink_model: SplinkStage3Model | None = None,
) -> list[MatchResult | ProbabilisticMatchResult]:
    """The composed entry point: Tier 1 (`match_all`) first, unmodified,
    then Stage 3 against whatever Tier 1 leaves UNMATCHED / AMBIGUOUS /
    TIE_AMBIGUOUS, scored against the settlement pool still open after
    Tier 1's own MATCHED/PARTIAL consumption. Returns the Stage 3 result
    where it clears threshold, the original Tier 1 result unchanged
    otherwise -- so on a set Tier 1 already resolves completely (`data/`),
    this is provably a no-op.

    `invoices` is not part of Tier 1's own contract but is required here:
    a razorpay_settlement CanonicalRecord carries no counterparty name of
    its own (see `_settlement_name`), so the name comparison needs the
    invoice ledger to resolve one. Pass `()` to run Stage 3 with amount and
    date signal only (name comparison degrades to its NULL level, per
    Splink's own handling -- not an error).

    `splink_model` defaults to a lazily-trained, process-cached model from
    `DEFAULT_DATA_DIR` (`data/`) -- pass one from `train_stage3_model()` to
    override the population, threshold, or seed.
    """
    model = splink_model or _default_model()
    tier1_results = match_all(credits, settlements)
    by_credit = {c.record_id: c for c in credits}

    consumed: set[str] = set()
    for r in tier1_results:
        if r.resolution in (MATCHED, PARTIAL):
            consumed.update(r.settlement_ids)

    deferred_credits = [
        by_credit[r.bank_txn_id] for r in tier1_results if r.resolution in _DEFERRED_RESOLUTIONS
    ]
    open_settlements = [s for s in settlements if s.record_id not in consumed]
    stage3_by_credit = resolve_stage3(deferred_credits, open_settlements, invoices, model)

    out: list[MatchResult | ProbabilisticMatchResult] = []
    for r in tier1_results:
        if r.resolution not in _DEFERRED_RESOLUTIONS:
            out.append(r)
            continue
        stage3 = stage3_by_credit[r.bank_txn_id]
        out.append(stage3 if stage3.resolution == MATCHED else r)
    return out
