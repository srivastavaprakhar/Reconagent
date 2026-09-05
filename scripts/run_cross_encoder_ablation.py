"""Tier 3 ablation: does a small pretrained cross-encoder recover any of the
matching cases Tier 1 (deterministic + subset-sum) and Tier 2 (Splink
probabilistic + hybrid fuzzy text) still cannot resolve?

This is spec section 11/12's stretch item, built last and reported honestly
either way. It is an ABLATION, NOT AN INTEGRATION: nothing here is wired into
the live cascade. `reconagent/match.py`, `reconagent/probabilistic.py` and
`reconagent/fuzzy.py` are only called, never modified, exactly as
`scripts/run_tier2_ablation.py` treats them.

MODEL CHOICE: `cross-encoder/stsb-distilroberta-base`

Chosen before any number in this file was measured, on three a-priori
grounds:

  - Task shape. Entity matching asks a symmetric question -- "are these two
    strings the same real-world thing?" -- which is the STS-B (semantic
    textual similarity) task, not the MS MARCO passage-ranking task the rest
    of the `cross-encoder/` org is trained for. MS MARCO models are
    asymmetric (query -> passage) and emit unbounded logits; an STS model
    emits a bounded [0, 1] similarity, which makes a single scalar
    acceptance threshold meaningful rather than an arbitrary logit cut.
  - Size/speed. 82.1M parameters, 6 transformer layers -- roughly 208
    pairs/second on this CPU, so `data/`'s full 30,704-pair blocked
    candidate space scores in about two and a half minutes and the
    20-credit residual in under three seconds. Hackathon scale, no GPU, no
    fine-tuning.
  - Strength. The smaller alternative in the same family
    (`cross-encoder/stsb-TinyBERT-L-4`, 14.4M) is a 4-layer distillation. A
    negative result from a toy model proves nothing about pretrained LMs;
    the point of this ablation is to give the pretrained-LM hypothesis its
    best honest shot at hackathon scale, so the strongest small STS
    cross-encoder available wins over the fastest one. (Note:
    `cross-encoder/stsb-MiniLM-L-6-v2`, the usual first choice, returns 401
    from the Hub as of this run -- that repo is gone, not a network fault
    here; every other repo probed resolved fine.)

The model is used strictly OFF-THE-SHELF. No fine-tuning on this project's
data -- the entire question being asked is whether a general pretrained LM's
semantic understanding transfers to short structured financial strings, and
fine-tuning it on `data/` would answer a different question.

PAIR TEXT: DELIBERATELY IDENTICAL TO STAGE 4's

Each side is the same concatenated string Stage 4 builds
(`reconagent.fuzzy._record_name_and_text`'s `text`): counterparty name plus
narration on the credit side; invoice-joined counterparty name plus
settlement narration plus invoice notes on the settlement side. A
`razorpay_settlement` carries no counterparty name of its own, so it is
resolved through the invoice ledger exactly as
`reconagent.probabilistic._settlement_name` does -- duplicated here as
`_pair_text`, a few stable lines, rather than refactoring a committed file
for a report script.

Keeping the text identical to Stage 4's is the point: the ONLY variable
changing between Stage 4 and this ablation is the scoring function
(TF-IDF/Jaro-Winkler/LSA-RRF versus a pretrained cross-encoder). Any
difference in outcome is attributable to the model, not to feature
engineering.

BLOCKING: currency-exact plus a +/- `POOL_WINDOW_DAYS` (30) window on
`settled_at`, mirroring `reconagent.probabilistic` and
`reconagent.fuzzy._blocked_pool` exactly -- same rule, so `data/` yields the
same 30,704 blocked candidate pairs Stage 3 and Stage 4 derive their own
thresholds over, and the three derivations are directly comparable.

THRESHOLD DERIVATION (from `data/` only) -- AND WHAT IT EXPOSES

Same discipline every prior matching unit held: calibrate on `data/`'s
labelled linkage, evaluate on the `stress_test/` residual, never the
reverse. All 30,704 blocked pairs from `data/` are scored and tagged known
positive (175 pairs, via `data/ground_truth.json`'s linkage, single
settlements and bundle members alike) or known negative (30,529 pairs).
Measured on this run (re-derived by
`tests/test_cross_encoder_ablation.py::test_threshold_derivation_is_reproducible_from_main_labels`):

  - known-positive scores: min 0.107633, mean 0.449878, max 0.866071
  - known-negative scores: min 0.055597, mean 0.346492, max 0.828032

The two populations barely separate. The positive mean sits 0.10 above the
negative mean -- real signal, not noise -- but 172 of 175 known positives
score below the known-negative maximum, and 30,399 of 30,529 known negatives
(99.57%) score above the weakest known positive. This is worse separation
than Stage 4's
RRF score managed and far worse than Stage 3's calibrated Fellegi-Sunter
probability, and it is reported rather than papered over.

What the populations DO separate on is their extreme top: exactly 3 known
positives (0.831737, 0.855163, 0.866071) sit strictly above the
known-negative maximum of 0.828032, with a clean gap and nothing between.
`DERIVED_THRESHOLD` is that gap's midpoint, 0.829885 -- the same
zero-false-match-on-`data/` construction `reconagent.fuzzy`'s
`DEFAULT_MATCH_THRESHOLD` uses, and the only threshold this population
honestly supports. It accepts 3 of 175 known positives (1.7%), an even more
conservative calibration than Stage 4's 13/175 -- which is itself the
finding, not a tuning failure: a threshold this high is what the model's own
score distribution forces if a false match is not acceptable.

RESULT ON THE 20-CASE `stress_test/` RESIDUAL

See `reports/tier3_cross_encoder_ablation.md` for the run this script
produces. Headline, restated here so a reader of the source sees it too:
0 correct, 0 wrong, 20 deferred -- and specifically 0/8 on
`legal_vs_trading_name`, the category this ablation was aimed at. Zero false
matches, and zero lift. The highest score any residual credit's best
candidate reached was 0.644592, well below the 0.829885 threshold.

The diagnostic worth keeping, because it is the honest version of "it did
nothing": the cross-encoder ranks the TRUE settlement first for 15 of the 20
residual credits (4 of 8 `legal_vs_trading_name`). Its ordering carries real
information; its calibration does not. Accepting its top-ranked candidate
unconditionally would score 15 correct and 5 WRONG -- a 25% false-match rate
on the residual, exactly the failure mode spec section 9 rejects. And a
threshold low enough to reach down into the score range where those residual
cases actually live is not available: even a threshold at `data/`'s own
weakest known positive admits 30,399 of its 30,529 known negatives (99.57%).
There is no threshold that buys the recall without buying the false matches.
That is the whole result.

This is CONSISTENT WITH, not a failure to reproduce, the research
literature's own caveat that spec section 11 already flags: dense/LM methods
do not reliably beat classical and probabilistic methods on short structured
financial strings. An ablation showing no lift, measured rather than
asserted, is the outcome the spec explicitly says is as credible as a gain.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import asdict, dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Sequence

from reconagent.camt053 import parse_camt053_file
from reconagent.fuzzy import match_with_full_cascade
from reconagent.invoices import parse_invoice_ledger
from reconagent.match import MATCHED, PARTIAL, POOL_WINDOW_DAYS
from reconagent.razorpay import parse_razorpay_settlements
from reconagent.records import CanonicalRecord

REPO = Path(__file__).resolve().parent.parent
DATA_ROOT = (REPO / "data").resolve()
STRESS_ROOT = (REPO / "stress_test").resolve()
DEFAULT_REPORT_JSON = REPO / "reports" / "tier3_cross_encoder_ablation.json"
DEFAULT_REPORT_MD = REPO / "reports" / "tier3_cross_encoder_ablation.md"

MODEL_NAME = "cross-encoder/stsb-distilroberta-base"
BATCH_SIZE = 64

# See module docstring "THRESHOLD DERIVATION". Derived from data/ alone;
# `derive_threshold()` re-computes it and the test asserts it lands here.
DERIVED_THRESHOLD = Decimal("0.829885")

# Same blocking rule as reconagent.fuzzy._blocked_pool / reconagent.probabilistic.
BLOCKING_WINDOW_DAYS = POOL_WINDOW_DAYS


# --------------------------------------------------------------------------
# Loading, pair text, blocking -- no ground truth anywhere below this line
# until the grading section.
# --------------------------------------------------------------------------


def load_dataset(data_dir: Path):
    """(settlements, credits, invoices) for one dataset directory."""
    return (
        parse_razorpay_settlements(data_dir / "razorpay_settlements.csv"),
        parse_camt053_file(data_dir / "bank_statement.camt053.xml"),
        parse_invoice_ledger(data_dir / "invoice_ledger.csv"),
    )


def _name_index(
    invoices: Sequence[CanonicalRecord],
) -> tuple[dict[str, CanonicalRecord], dict[str, CanonicalRecord]]:
    by_id = {i.invoice_id: i for i in invoices if i.invoice_id}
    by_order = {i.order_id: i for i in invoices if i.order_id}
    return by_id, by_order


def _pair_text(
    record: CanonicalRecord,
    is_settlement: bool,
    by_id: dict[str, CanonicalRecord],
    by_order: dict[str, CanonicalRecord],
) -> str:
    """One side's text for the cross-encoder. Identical to Stage 4's `text`
    (module docstring "PAIR TEXT") so the only variable in this ablation is
    the scoring model."""
    if is_settlement:
        invoice = by_id.get(record.invoice_id) or by_order.get(record.order_id)
        name = (invoice.counterparty_name if invoice else "") or ""
        notes = (invoice.narration if invoice else "") or ""
        return " ".join(p for p in (name, record.narration or "", notes) if p)
    return " ".join(p for p in (record.counterparty_name or "", record.narration or "") if p)


def _blocked_pool(
    credit: CanonicalRecord, settlements: Sequence[CanonicalRecord]
) -> list[CanonicalRecord]:
    """Currency-exact plus a date window (module docstring "BLOCKING")."""
    credit_date = credit.value_date or credit.booking_date
    if credit_date is None:
        return []
    return [
        s
        for s in settlements
        if s.currency == credit.currency
        and s.settled_at is not None
        and abs((credit_date - s.settled_at).days) <= BLOCKING_WINDOW_DAYS
    ]


def build_candidate_pairs(
    credits: Sequence[CanonicalRecord],
    settlements: Sequence[CanonicalRecord],
    invoices: Sequence[CanonicalRecord],
) -> tuple[list[tuple[str, str]], list[tuple[CanonicalRecord, CanonicalRecord]]]:
    """(texts, records) for every blocked (credit, settlement) pair, aligned."""
    by_id, by_order = _name_index(invoices)
    texts: list[tuple[str, str]] = []
    records: list[tuple[CanonicalRecord, CanonicalRecord]] = []
    for credit in credits:
        credit_text = _pair_text(credit, False, by_id, by_order)
        for settlement in _blocked_pool(credit, settlements):
            texts.append((credit_text, _pair_text(settlement, True, by_id, by_order)))
            records.append((credit, settlement))
    return texts, records


@lru_cache(maxsize=1)
def load_cross_encoder(model_name: str = MODEL_NAME):
    """Off-the-shelf, no fine-tuning (module docstring "MODEL CHOICE").
    Cached per process -- loading costs a few seconds from the HF cache and
    every caller here wants the same weights."""
    from sentence_transformers import CrossEncoder

    return CrossEncoder(model_name)


def score_pairs(texts: Sequence[tuple[str, str]], model=None) -> list[float]:
    if not texts:
        return []
    model = model or load_cross_encoder()
    return [float(s) for s in model.predict(list(texts), batch_size=BATCH_SIZE, show_progress_bar=False)]


# --------------------------------------------------------------------------
# Threshold derivation -- reads data/ground_truth.json ONLY (Tier 3's
# sanctioned calibration source, same as Stage 3's and Stage 4's).
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ThresholdDerivation:
    """Everything the module docstring's THRESHOLD DERIVATION section cites,
    recomputed rather than quoted."""

    threshold: Decimal
    blocked_candidate_pairs: int
    known_positive_pairs: int
    known_negative_pairs: int
    positive_min: float
    positive_mean: float
    positive_max: float
    negative_min: float
    negative_mean: float
    negative_max: float
    positives_above_negative_max: list[float]
    positives_below_negative_max: int
    negatives_above_weakest_positive: int
    negatives_above_weakest_positive_rate: float


def derive_threshold(data_dir: Path = DATA_ROOT, model=None) -> ThresholdDerivation:
    """Score `data_dir`'s full blocked candidate space, split it by
    `data_dir`'s own labelled linkage, and place the threshold at the
    midpoint of the gap between the known-negative maximum and the weakest
    known positive above it -- zero false matches against `data_dir`'s entire
    known-negative population.

    Reads `data_dir/ground_truth.json` and nothing else. It never opens
    `stress_test/`'s or `data/holdout/`'s answer key; `data_dir` is a
    parameter so a test can point this at a scratch copy of `data/`, not so
    callers can point it at a split it is graded on."""
    settlements, credits, invoices = load_dataset(data_dir)
    ground_truth = json.loads((data_dir / "ground_truth.json").read_text())

    positive_pairs: set[tuple[str, str]] = set()
    for case in ground_truth["cases"]:
        bank_txn_id = case["expected_link"]["bank_txn_id"]
        if not bank_txn_id:
            continue
        for sid in case["expected_link"]["covers_settlement_ids"]:
            positive_pairs.add((sid, bank_txn_id))

    texts, records = build_candidate_pairs(credits, settlements, invoices)
    scores = score_pairs(texts, model)

    positives, negatives = [], []
    for (credit, settlement), score in zip(records, scores):
        target = positives if (settlement.record_id, credit.record_id) in positive_pairs else negatives
        target.append(score)

    neg_max = max(negatives)
    above = sorted(p for p in positives if p > neg_max)
    # The gap's midpoint. If no positive clears the negative ceiling at all,
    # no threshold on this population is zero-false-match; fall back to just
    # above the negative max and say so in the report.
    threshold_float = (neg_max + above[0]) / 2 if above else neg_max + 1e-6
    weakest_positive = min(positives)

    return ThresholdDerivation(
        threshold=Decimal(str(round(threshold_float, 6))),
        blocked_candidate_pairs=len(scores),
        known_positive_pairs=len(positives),
        known_negative_pairs=len(negatives),
        positive_min=round(min(positives), 6),
        positive_mean=round(sum(positives) / len(positives), 6),
        positive_max=round(max(positives), 6),
        negative_min=round(min(negatives), 6),
        negative_mean=round(sum(negatives) / len(negatives), 6),
        negative_max=round(neg_max, 6),
        positives_above_negative_max=[round(p, 6) for p in above],
        positives_below_negative_max=sum(1 for p in positives if p < neg_max),
        negatives_above_weakest_positive=sum(1 for n in negatives if n > weakest_positive),
        negatives_above_weakest_positive_rate=round(
            sum(1 for n in negatives if n > weakest_positive) / len(negatives), 6
        ),
    )


# --------------------------------------------------------------------------
# The residual population + the cross-encoder's proposals on it.
# Still no ground truth: `propose_matches` decides purely on score.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Proposal:
    """The cross-encoder's verdict on one residual credit. `accepted` is
    False for an honest decline -- the best candidate and its score are kept
    either way, so a reader can see how far short it fell. Money fields are
    integer minor units, never float (CLAUDE.md)."""

    bank_txn_id: str
    accepted: bool
    settlement_id: str | None
    score: Decimal
    threshold: Decimal
    credit_amount_minor: int
    settlement_net_sum_minor: int
    residual_minor: int
    rank_of_best: int
    candidates_considered: int


def residual_population(
    credits: Sequence[CanonicalRecord],
    settlements: Sequence[CanonicalRecord],
    invoices: Sequence[CanonicalRecord],
) -> tuple[list[CanonicalRecord], list[CanonicalRecord]]:
    """(credits Tier 1+2 could not resolve, settlements still open). Runs
    `reconagent.fuzzy.match_with_full_cascade` unmodified and reads its
    output -- exactly what Tier 3 has to improve on."""
    results = match_with_full_cascade(credits, settlements, invoices)
    by_credit = {c.record_id: c for c in credits}
    consumed: set[str] = set()
    for r in results:
        if r.resolution in (MATCHED, PARTIAL):
            consumed.update(r.settlement_ids)
    residual = [by_credit[r.bank_txn_id] for r in results if r.resolution != MATCHED]
    open_settlements = [s for s in settlements if s.record_id not in consumed]
    return residual, open_settlements


def propose_matches(
    credits: Sequence[CanonicalRecord],
    open_settlements: Sequence[CanonicalRecord],
    invoices: Sequence[CanonicalRecord],
    threshold: Decimal,
    model=None,
) -> dict[str, Proposal]:
    """Score every blocked pair, then claim globally highest-score-first --
    one settlement to one credit, mirroring
    `reconagent.fuzzy.resolve_stage4`'s contention discipline. A credit whose
    best candidate falls below `threshold`, or whose best candidate was
    claimed by a more confident credit, comes back declined with that
    candidate's score still attached."""
    texts, records = build_candidate_pairs(credits, open_settlements, invoices)
    scores = score_pairs(texts, model)
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)

    pool_size = Counter(credit.record_id for credit, _ in records)
    best: dict[str, tuple[CanonicalRecord, float, int]] = {}
    accepted: dict[str, tuple[CanonicalRecord, float, int]] = {}
    claimed: set[str] = set()
    seen: Counter = Counter()
    for i in order:
        credit, settlement = records[i]
        cid = credit.record_id
        seen[cid] += 1
        best.setdefault(cid, (settlement, scores[i], seen[cid]))
        if cid in accepted or settlement.record_id in claimed:
            continue
        if Decimal(str(round(scores[i], 6))) < threshold:
            continue
        accepted[cid] = (settlement, scores[i], seen[cid])
        claimed.add(settlement.record_id)

    proposals: dict[str, Proposal] = {}
    for credit in credits:
        cid = credit.record_id
        is_accepted = cid in accepted
        entry = accepted.get(cid) or best.get(cid)
        settlement, score, rank = entry if entry else (None, 0.0, 0)
        proposals[cid] = Proposal(
            bank_txn_id=cid,
            accepted=is_accepted,
            settlement_id=settlement.record_id if (settlement and is_accepted) else None,
            score=Decimal(str(round(score, 6))),
            threshold=threshold,
            credit_amount_minor=credit.amount_minor,
            settlement_net_sum_minor=settlement.amount_minor if settlement else 0,
            residual_minor=(settlement.amount_minor - credit.amount_minor) if settlement else -credit.amount_minor,
            rank_of_best=rank,
            candidates_considered=pool_size.get(cid, 0),
        )
    return proposals


def top_ranked_settlement_ids(
    credits: Sequence[CanonicalRecord],
    open_settlements: Sequence[CanonicalRecord],
    invoices: Sequence[CanonicalRecord],
    model=None,
) -> dict[str, str]:
    """The counterfactual the report leans on: which settlement would each
    residual credit get if the top-ranked candidate were accepted with NO
    threshold at all. Ordering only -- no acceptance decision, no ground
    truth."""
    texts, records = build_candidate_pairs(credits, open_settlements, invoices)
    scores = score_pairs(texts, model)
    top: dict[str, tuple[str, float]] = {}
    for (credit, settlement), score in zip(records, scores):
        current = top.get(credit.record_id)
        if current is None or score > current[1]:
            top[credit.record_id] = (settlement.record_id, score)
    return {cid: sid for cid, (sid, _) in top.items()}


# --------------------------------------------------------------------------
# GRADING -- the only place stress_test/ground_truth.json is touched, and it
# arrives as an argument. Nothing above this line sees an answer key it is
# scored against (module docstring; mirrors scripts/run_tier2_ablation.py).
# --------------------------------------------------------------------------


def load_grading_truth(data_dir: Path) -> tuple[dict[str, str], dict[str, str]]:
    """(true settlement per bank_txn_id, defect_class per bank_txn_id)."""
    gt = json.loads((data_dir / "ground_truth.json").read_text())
    truth, categories = {}, {}
    for case in gt["cases"]:
        bank_txn_id = case["expected_link"]["bank_txn_id"]
        if not bank_txn_id:
            continue
        truth[bank_txn_id] = case["expected_link"]["covers_settlement_ids"][0]
        categories[bank_txn_id] = case["defect_class"]
    return truth, categories


def grade(
    proposals: dict[str, Proposal],
    truth: dict[str, str],
    categories: dict[str, str],
) -> dict:
    """correct / wrong / deferred, overall and per defect class. `wrong` is
    the number that matters most (spec section 9)."""
    correct: Counter = Counter()
    wrong: Counter = Counter()
    deferred: Counter = Counter()
    wrong_detail = []
    for cid, proposal in proposals.items():
        cat = categories.get(cid, "unknown")
        if not proposal.accepted:
            deferred[cat] += 1
        elif proposal.settlement_id == truth.get(cid):
            correct[cat] += 1
        else:
            wrong[cat] += 1
            wrong_detail.append(
                {
                    "bank_txn_id": cid,
                    "defect_class": cat,
                    "proposed_settlement_id": proposal.settlement_id,
                    "true_settlement_id": truth.get(cid),
                    "score": str(proposal.score),
                }
            )

    classes = sorted(set(categories.get(cid, "unknown") for cid in proposals))
    return {
        "total": len(proposals),
        "correct": sum(correct.values()),
        "wrong": sum(wrong.values()),
        "deferred": sum(deferred.values()),
        "wrong_detail": wrong_detail,
        "by_defect_class": [
            {
                "defect_class": dc,
                "total": correct[dc] + wrong[dc] + deferred[dc],
                "correct": correct[dc],
                "wrong": wrong[dc],
                "deferred": deferred[dc],
            }
            for dc in classes
        ],
    }


def grade_ranking(
    top_ids: dict[str, str], truth: dict[str, str], categories: dict[str, str]
) -> dict:
    """How often the cross-encoder's ORDERING puts the true settlement first,
    ignoring the threshold entirely -- and what accepting that top candidate
    unconditionally would have cost. Diagnostic, not a proposed policy."""
    right: Counter = Counter()
    total: Counter = Counter()
    for cid, sid in top_ids.items():
        cat = categories.get(cid, "unknown")
        total[cat] += 1
        if sid == truth.get(cid):
            right[cat] += 1
    return {
        "true_settlement_ranked_first": sum(right.values()),
        "total": sum(total.values()),
        "would_be_wrong_if_accepted_unconditionally": sum(total.values()) - sum(right.values()),
        "by_defect_class": [
            {"defect_class": dc, "total": total[dc], "ranked_first": right[dc]}
            for dc in sorted(total)
        ],
    }


# --------------------------------------------------------------------------
# Report assembly
# --------------------------------------------------------------------------


def run_ablation(model=None) -> dict:
    derivation = derive_threshold(DATA_ROOT, model)

    settlements, credits, invoices = load_dataset(STRESS_ROOT)
    residual_credits, open_settlements = residual_population(credits, settlements, invoices)
    proposals = propose_matches(
        residual_credits, open_settlements, invoices, derivation.threshold, model
    )
    top_ids = top_ranked_settlement_ids(residual_credits, open_settlements, invoices, model)

    truth, categories = load_grading_truth(STRESS_ROOT)
    scoring = grade(proposals, truth, categories)
    ranking = grade_ranking(top_ids, truth, categories)

    best_scores = sorted((float(p.score) for p in proposals.values()), reverse=True)
    return {
        "model": MODEL_NAME,
        "fine_tuned": False,
        "threshold_derivation": asdict(derivation) | {"threshold": str(derivation.threshold)},
        "residual": {
            "residual_credits": len(residual_credits),
            "open_settlements": len(open_settlements),
            "blocked_candidate_pairs": sum(p.candidates_considered for p in proposals.values()),
            "highest_best_candidate_score": best_scores[0] if best_scores else None,
        },
        "scoring": scoring,
        "ranking_diagnostic": ranking,
        "proposals": [asdict(p) | {"score": str(p.score), "threshold": str(p.threshold)} for p in proposals.values()],
        "finding": build_finding(derivation, scoring, ranking, best_scores),
        "integration_recommendation": integration_recommendation(scoring),
    }


def integration_recommendation(scoring: dict) -> str:
    """The human-in-the-loop gate. A clear, unambiguous win with zero
    false-match cost is the ONLY outcome that gets escalated for an
    integration decision -- and even then this script does not wire it in."""
    if scoring["wrong"] > 0:
        return (
            f"DO NOT INTEGRATE. The cross-encoder produced {scoring['wrong']} FALSE MATCH(ES) on the "
            f"{scoring['total']}-case residual. A false match is the failure mode this project is "
            "built to avoid; this alone disqualifies the technique regardless of its recall."
        )
    if scoring["correct"] == 0:
        return (
            "DO NOT INTEGRATE -- no lift. Zero false matches, but also zero cases resolved: the "
            "cross-encoder recovers nothing Tier 1+2 could not already handle, so wiring it in would "
            "add a transformer dependency, model weights and per-pair inference cost for a measured "
            "gain of zero. Stays a reported ablation finding."
        )
    return (
        f"FLAG FOR HUMAN DECISION. The cross-encoder resolved {scoring['correct']} of "
        f"{scoring['total']} residual cases with {scoring['wrong']} false matches. This script does "
        "NOT integrate it; the project owner decides."
    )


def build_finding(derivation: ThresholdDerivation, scoring: dict, ranking: dict, best_scores: list[float]) -> str:
    """Computed from the numbers just measured, not asserted in advance."""
    lines = []

    if scoring["wrong"] > 0:
        lines.append(
            f"HEADLINE: {scoring['wrong']} FALSE MATCH(ES) in the {scoring['total']}-case residual. "
            "This is the single most important number in this report and is stated before any "
            "recall figure: the cross-encoder confidently asserted a settlement link that is wrong."
        )
    else:
        lines.append(
            f"HEADLINE: zero false matches across all {scoring['total']} residual cases -- the "
            "cross-encoder never confidently asserted a wrong settlement link."
        )

    lines.append(
        f"On the {scoring['total']} cases Tier 1 (deterministic + subset-sum) and Tier 2 (Splink + "
        f"hybrid fuzzy) leave unresolved in stress_test/, the pretrained cross-encoder "
        f"({MODEL_NAME}, off-the-shelf, no fine-tuning) resolved {scoring['correct']} correctly, "
        f"{scoring['wrong']} incorrectly, and deferred {scoring['deferred']}."
    )

    legal = next(
        (row for row in scoring["by_defect_class"] if row["defect_class"] == "legal_vs_trading_name"),
        None,
    )
    if legal:
        if legal["correct"] == 0:
            lines.append(
                f"On legal_vs_trading_name -- the category this ablation primarily targeted, where "
                f"the two sides' names are genuinely different strings and semantic understanding is "
                f"supposed to beat lexical similarity -- the cross-encoder resolved "
                f"0/{legal['total']}, exactly as Tier 2 did. No lift, stated plainly. This is "
                "CONSISTENT WITH, not a failure to reproduce, the research literature's own caveat "
                "(spec section 11) that dense/LM methods do not reliably beat classical and "
                "probabilistic methods on short structured financial strings."
            )
        else:
            lines.append(
                f"On legal_vs_trading_name, the cross-encoder resolved {legal['correct']}/"
                f"{legal['total']} -- a real lift over Tier 2's 0/8 -- at a cost of "
                f"{legal['wrong']} false match(es) in that category."
            )

    others = [row for row in scoring["by_defect_class"] if row["defect_class"] != "legal_vs_trading_name"]
    if others:
        lines.append(
            "On the other residual categories: "
            + ", ".join(
                f"{row['defect_class']} {row['correct']}/{row['total']} correct, "
                f"{row['wrong']} wrong, {row['deferred']} deferred"
                for row in others
            )
            + "."
        )

    if best_scores:
        lines.append(
            f"Why nothing cleared the bar: the highest score any residual credit's best candidate "
            f"reached was {best_scores[0]:.6f}, against a threshold of {derivation.threshold} "
            f"derived from data/'s own labelled population."
        )

    lines.append(
        f"The ordering-versus-calibration split is the honest version of this negative result: the "
        f"cross-encoder ranks the TRUE settlement first for "
        f"{ranking['true_settlement_ranked_first']}/{ranking['total']} residual credits, so its "
        f"ordering does carry real information -- but accepting that top-ranked candidate "
        f"unconditionally would have produced "
        f"{ranking['would_be_wrong_if_accepted_unconditionally']} false matches, and there is no "
        f"lower threshold available to reach those cases honestly -- even a threshold at data/'s "
        f"own weakest known positive already admits "
        f"{derivation.negatives_above_weakest_positive} of data/'s "
        f"{derivation.known_negative_pairs} known negatives "
        f"({derivation.negatives_above_weakest_positive_rate:.2%}). There is no threshold that buys "
        "the recall without buying the false matches."
    )
    return " ".join(lines)


# --------------------------------------------------------------------------
# Rendering / writing
# --------------------------------------------------------------------------


def render_markdown(report: dict) -> str:
    d = report["threshold_derivation"]
    s = report["scoring"]
    r = report["ranking_diagnostic"]
    lines = [
        "# Tier 3 ablation: does a pretrained cross-encoder recover the Tier 1+2 residual?",
        "",
        f"Model: `{report['model']}` -- off-the-shelf, **not** fine-tuned on this project's data.",
        "",
        "## Headline",
        "",
        report["finding"],
        "",
        "## Integration recommendation",
        "",
        report["integration_recommendation"],
        "",
        "## Threshold derivation (from `data/` only, never `stress_test/`)",
        "",
        f"- blocked candidate pairs scored: {d['blocked_candidate_pairs']:,} "
        f"({d['known_positive_pairs']} known positive, {d['known_negative_pairs']:,} known negative)",
        f"- known-positive scores: min {d['positive_min']:.6f}, mean {d['positive_mean']:.6f}, "
        f"max {d['positive_max']:.6f}",
        f"- known-negative scores: min {d['negative_min']:.6f}, mean {d['negative_mean']:.6f}, "
        f"max {d['negative_max']:.6f}",
        f"- known positives scoring BELOW the known-negative maximum: "
        f"{d['positives_below_negative_max']}/{d['known_positive_pairs']}",
        f"- known negatives scoring ABOVE the weakest known positive: "
        f"{d['negatives_above_weakest_positive']:,}/{d['known_negative_pairs']:,} "
        f"({d['negatives_above_weakest_positive_rate']:.2%})",
        f"- known positives strictly above the known-negative maximum: "
        f"{d['positives_above_negative_max']}",
        f"- **threshold = {d['threshold']}**, the midpoint of that gap -- zero false matches against "
        f"`data/`'s full known-negative population, at the cost of accepting only "
        f"{len(d['positives_above_negative_max'])}/{d['known_positive_pairs']} known positives.",
        "",
        "## Residual result (`stress_test/`, the 20 cases Tier 1+2 leave unresolved)",
        "",
        f"- residual credits: {report['residual']['residual_credits']}, "
        f"open settlements: {report['residual']['open_settlements']}, "
        f"blocked candidate pairs: {report['residual']['blocked_candidate_pairs']}",
        f"- highest score any residual credit's best candidate reached: "
        f"{report['residual']['highest_best_candidate_score']:.6f}",
        "",
        f"| outcome | count |",
        "|---|---|",
        f"| correct | {s['correct']} |",
        f"| **wrong (false match)** | **{s['wrong']}** |",
        f"| deferred | {s['deferred']} |",
        f"| total | {s['total']} |",
        "",
        "| defect class | total | correct | wrong | deferred |",
        "|---|---|---|---|---|",
    ]
    for row in s["by_defect_class"]:
        lines.append(
            f"| {row['defect_class']} | {row['total']} | {row['correct']} | {row['wrong']} "
            f"| {row['deferred']} |"
        )
    if s["wrong_detail"]:
        lines += ["", "### False matches (every one, itemised)", ""]
        for w in s["wrong_detail"]:
            lines.append(
                f"- `{w['bank_txn_id']}` ({w['defect_class']}): proposed "
                f"`{w['proposed_settlement_id']}`, truth `{w['true_settlement_id']}`, "
                f"score {w['score']}"
            )

    lines += [
        "",
        "## Ranking diagnostic: ordering vs calibration",
        "",
        "Whether the model's *ordering* is informative, ignoring the acceptance threshold entirely. "
        "This is a diagnostic, not a proposed policy -- accepting a top-ranked candidate with no "
        "threshold is exactly the behaviour spec section 9 rejects.",
        "",
        f"- true settlement ranked first for {r['true_settlement_ranked_first']}/{r['total']} "
        f"residual credits",
        f"- accepting that top candidate unconditionally would have produced "
        f"**{r['would_be_wrong_if_accepted_unconditionally']} false matches** "
        f"({r['would_be_wrong_if_accepted_unconditionally'] / r['total']:.0%} of the residual)",
        "",
        "| defect class | total | true settlement ranked first |",
        "|---|---|---|",
    ]
    for row in r["by_defect_class"]:
        lines.append(f"| {row['defect_class']} | {row['total']} | {row['ranked_first']} |")

    lines += [
        "",
        "## Scope",
        "",
        "This is an ablation, not an integration. `reconagent/match.py`, "
        "`reconagent/probabilistic.py` and `reconagent/fuzzy.py` are unmodified -- the live cascade "
        "does not call anything in this report.",
        "",
    ]
    return "\n".join(lines) + "\n"


def _guard_output_path(path: Path) -> None:
    """Mirrors `reconagent.eval._guard_output_path` and
    `scripts/run_tier2_ablation.py`'s guard: refuse to write over either
    committed ground-truth directory this report reads from."""
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
    ap = argparse.ArgumentParser(description="Tier 3 cross-encoder ablation")
    ap.add_argument("--out-json", type=Path, default=DEFAULT_REPORT_JSON)
    ap.add_argument("--out-md", type=Path, default=DEFAULT_REPORT_MD)
    args = ap.parse_args()

    report = run_ablation()
    write_report(report, args.out_json, args.out_md)
    print(f"wrote {args.out_json}")
    print(f"wrote {args.out_md}")
    print(report["finding"])
    print(report["integration_recommendation"])


if __name__ == "__main__":
    main()
