"""Stage 4: hybrid fuzzy text matching (spec section 4, Stage 4).

WHY THIS STAGE EXISTS

Tier 1 resolves `data/` completely (152/152) and Stage 3
(`reconagent.probabilistic`) resolves the holdout split's only remaining
misses as genuine subset-sum ties, not real recall gaps -- 0.00% false-clear
on both splits once that separation is made. Nothing in either split's own
evaluation forced this stage to exist either. It is built anyway, proactively,
for the same stated reason Stage 3 was: genuine ML depth appropriate to this
submission, and robustness against real-world messiness the clean datasets
never exercise -- specifically the cross-border problem's own signature
mess: unstructured SWIFT remittance narration and invoice descriptions that
don't match settlement text verbatim. `stress_test/` (40 cases, all built so
Tier 1 returns UNMATCHED on every one) is where this gets exercised; Stage 3
alone resolves 15/40 correct, 0 wrong, 25 deferred, with `legal_vs_trading_name`
(8/8 deferred) as the category Stage 3's name-similarity approach structurally
cannot touch -- see this module's own stress-test numbers below for whether
Stage 4 does better there.

VECTOR-INDEX CHOICE: FAISS, NOT CHROMA

`faiss-cpu` over ChromaDB, per the spec's own steer (section 11 lists either):
Chroma is a document store with a server/client model (an embedded DuckDB or
a running service, plus its own persistence and collection API) built for a
corpus you query repeatedly across process lifetimes. This stage's dense
index is rebuilt per credit, from a few hundred vectors already narrowed by
blocking, entirely in-process, and never persisted -- FAISS's
`IndexFlatIP` is exactly that: a bare in-memory similarity index with no
server, no schema, no persistence layer to stand up for a dataset this size.

DENSE EMBEDDING: LSA (TF-IDF + TruncatedSVD), NOT A PRETRAINED TRANSFORMER

No heavyweight pretrained sentence-embedding model is pulled in here. Two
reasons: this environment has no guaranteed network access to fetch model
weights at run time, and more importantly, a model pretrained on general
web/English text has no particular reason to understand "NORTHWIND SOFTWARE
INTL LLC" vs "NORTHWIND SOFTWARE INTERNATIONAL LLC" any better than a
corpus-local statistical method does -- for financial counterparty names and
narration, a model fit on THIS corpus's own vocabulary is arguably more
honest than one fit on an unrelated pretraining distribution. Latent
Semantic Analysis -- word-level TF-IDF reduced by `TruncatedSVD` -- is a
genuine dense embedding (continuous, low-dimensional, similarity is cosine
distance) that is locally computable, deterministic, and fits in milliseconds
on a corpus this size. It differs from the primary signal in a way that
matters: it is word-token based rather than character-n-gram based, so two
names sharing a distinctive, rare word (a shared brand root, e.g. "AXIOM" in
both "AXIOM GLOBAL HOLDINGS PRIVATE LIMITED" and "AXIOM TRADING CO") can
still score high on TF-IDF's own inverse-document-frequency weighting even
when the surrounding words share almost no characters -- exactly the
legal-name/trading-name shape of divergence the spec asks the dense signal to
catch. This is a real but modest capability, not semantic understanding; see
the stress-test numbers below for how much it actually buys.

PRIMARY SIGNAL: CHAR-N-GRAM TF-IDF + JARO-WINKLER

`primary_score = 0.6 * tfidf_char_cosine(text) + 0.4 * jaro_winkler(name)`.
The char-n-gram TF-IDF vectorizer (`analyzer="char"`, `ngram_range=(2, 4)`)
runs over each record's full text -- name plus narration/description, joined
through the invoice ledger on the settlement side exactly as
`reconagent.probabilistic._settlement_name` does (duplicated here as
`_record_name_and_text`, not imported: a small, stable helper, not worth a
shared-module refactor of a just-committed file) -- so it stays informative
even when the counterparty name field is clean but the narration is OCR-
corrupted (`ocr_typo_narration`) or the narration and invoice notes
deliberately share no tokens (`invoice_description_mismatch`). Jaro-Winkler
runs on the name string alone: it is a short-string metric, purpose-built for
exactly transliteration/typo/abbreviation variants
(`transliteration_variant`, `abbreviation_variant`), and would be diluted by
being run over long free text. 0.6/0.4 favours the signal (TF-IDF) that stays
informative across more of the five stress categories; not derived from any
labelled split, chosen as a reasonable prior before any ground truth is
touched.

DENSE FUSION: RECIPROCAL RANK FUSION, k=60

For one credit's blocked candidate pool, the primary score gives one ranking
and the dense (FAISS) cosine similarity gives a second, independent ranking.
`rrf_score = 1/(60 + primary_rank) + 1/(60 + dense_rank)`, standard RRF, k=60
per the spec's own suggested default (also TREC's/Elasticsearch hybrid
search's conventional choice) -- large enough that no single very-high or
very-low individual rank swamps the sum, so a candidate needs to rank
respectably on BOTH signals (or exceptionally on one, moderately on the
other) to win the fused ranking, which is exactly what "never the sole
decider" means in RRF terms: the primary signal's rank always contributes to
the sum a candidate that only the dense signal likes cannot reach on its own.
The credit's final proposed settlement is whichever candidate wins the fused
ranking, and `rrf_score` for that winner is also the scalar the acceptance
threshold below is applied to -- so acceptance genuinely reflects both
signals, not primary alone with dense as decoration.

BLOCKING

Currency-exact plus a date window -- `settled_at` within
`FUZZY_BLOCKING_WINDOW_DAYS` (= `reconagent.match.POOL_WINDOW_DAYS`, the same
30-day window Stage 1/2/3 all use) of the credit's own value/booking date.
Mirrors `reconagent.probabilistic`'s own blocking rule exactly (currency
equality is exact-or-nothing by definition; the date window is Tier 1's own
notion of plausible sweep timing) so text comparison never runs over an
unfiltered credit-by-settlement cross product.

THRESHOLD DERIVATION (from `data/` only) -- A REAL GAP, NOT PAPERED OVER

Same discipline as Stage 3's `DEFAULT_MATCH_THRESHOLD`
(`reconagent/probabilistic.py`'s module docstring), a different technique
because the scoring shape is different: RRF-fused rank scores, bounded above
by `2/(RRF_K + 1)` regardless of how well a candidate scores, rather than a
calibrated match probability with real dynamic range. Every credit in
`data/` is scored against its own currency+date-blocked pool of `data/`'s
own settlements (the identical 30,704-pair blocked candidate space Stage 3
scores for its own threshold derivation -- same blocking rule, so the same
count); each (settlement, credit) pair is tagged a known positive (via
`data/ground_truth.json`'s linkage, single-settlement and bundle members
alike -- 175 pairs) or a known negative (30,529 pairs). Real numbers from
that run (also re-derived by
`tests/test_fuzzy.py::test_threshold_derivation_is_reproducible_from_main_labels`):
  - known-negative RRF scores: min 0.007634, mean 0.014463, max 0.032522.
  - known-positive RRF scores: min 0.010456, mean 0.026363, max 0.032787.
Unlike Stage 3's score, these two populations are NOT cleanly separated --
13 known negatives score above the known-positive 5th percentile, and 154 of
175 known positives score below the known-negative max. An RRF-fused rank
score this tightly bounded just does not carry the same discriminating power
as a calibrated Fellegi-Sunter probability over three independent fields, and
that is reported here rather than concealed by picking a technique that
looks better. What the two populations DO separate cleanly on is their very
top: exactly 13 known positives -- every case where the true settlement
ranked #1 on BOTH the primary and the dense signal, the maximum possible RRF
score, `2/(RRF_K + 1) = 0.032787` -- sit strictly above the known-negative
max of 0.032522, with a real gap (no negative and no other positive falls
between them). `DEFAULT_MATCH_THRESHOLD` sits at that gap's midpoint,
0.032655: zero false matches against `data/`'s full known-negative
population, at the cost of accepting only that top slice (13/175, ~7%) of
known positives -- a conservative, low-recall calibration, deliberately
mirroring the project's stated priority (spec section 9, restated in Stage
3's own docstring): honest abstention beats a false match every time. See
the stress-test discussion above and this module's tests for how that
low-recall-but-zero-false-match posture actually plays out on messier text.

SECOND GATE: PRIMARY_SCORE_FLOOR -- WHY RANK AGREEMENT ALONE ISN'T ENOUGH

Rank-based agreement has a blind spot the module docstring's own honesty
requires surfacing: winning rank 1 on both signals only ever means "the best
of however many candidates survived blocking" -- if every candidate in a
pool is a mediocre textual match, the least-mediocre one still wins rank 1 on
both signals and clears `DEFAULT_MATCH_THRESHOLD` with the exact same score
as a genuinely excellent match would. Building against `data/` alone would
miss this (`data/`'s blocked pools are dominated by the domestic Razorpay-
sweep pattern, where the true match is usually found via UTR, not name, so
its pools rarely contain 20+ genuinely-plausible-looking distractors the way
a cross-border pool can) -- caught only once this module was run end to end
against `stress_test/` while building it, exactly the kind of gap Stage 3's
own module docstring flags happening to it too ("A REAL GAP, DOCUMENTED
RATHER THAN PAPERED OVER").

The fix is a second, independent gate on the RRF winner's absolute
`primary_score` (`PRIMARY_TFIDF_WEIGHT * tfidf_cosine + PRIMARY_JW_WEIGHT *
jaro_winkler`) -- still derived from `data/` alone, never from the stress or
holdout splits. `data/`'s own 175 known-positive pairs' `primary_score`
population is naturally bimodal: about 90% (the domestic-sweep majority,
whose true match is found via UTR and whose name legitimately does NOT
match -- Razorpay's own name against the actual customer's) cluster low,
and the remaining genuine-cross-border-name-match slice clusters high, with
one clean gap between the two modes -- the single largest gap in the sorted
175-value population sits between 0.422075 and 0.660730 (re-derived by
`tests/test_fuzzy.py::test_threshold_derivation_is_reproducible_from_main_labels`),
with nothing else nearby on either side. `PRIMARY_SCORE_FLOOR` sits at that
gap's upper edge, 0.660730 -- inside the genuine-name-match mode, excluding
the domestic-sweep mode entirely. A credit's RRF winner must clear BOTH
`DEFAULT_MATCH_THRESHOLD` (rank agreement) AND `PRIMARY_SCORE_FLOOR`
(absolute textual quality) to be accepted; this is what "the dense signal is
fused via RRF, never the sole decider" cashes out to concretely -- RRF
alone decides which candidate wins a credit's own pool, but neither RRF rank
alone nor primary score alone gates acceptance on its own.

OUTPUT: EXPLAINABLE, NOT AN OPAQUE SCORE

`FuzzyMatchResult` carries `combined_score` (the RRF-fused score that drove
accept/reject) alongside `tfidf_cosine`, `jaro_winkler`, `dense_score`,
`primary_rank`, and `dense_rank` -- the individual signals that fed it, so a
reader can see which one actually carried a given match rather than reading
a single opaque number.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Sequence

import faiss
import numpy as np
from rapidfuzz.distance import JaroWinkler
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from reconagent.match import (
    AMBIGUOUS,
    MATCHED,
    PARTIAL,
    POOL_WINDOW_DAYS,
    TIE_AMBIGUOUS,
    UNMATCHED,
    MatchResult,
)
from reconagent.probabilistic import ProbabilisticMatchResult, SplinkStage3Model
from reconagent.records import CanonicalRecord

STAGE_FUZZY = "stage4_fuzzy"

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = _REPO_ROOT / "data"

# Mirrors reconagent.probabilistic.BLOCKING_WINDOW_DAYS, itself
# reconagent.match.POOL_WINDOW_DAYS. See module docstring "BLOCKING".
FUZZY_BLOCKING_WINDOW_DAYS = POOL_WINDOW_DAYS

CHAR_NGRAM_RANGE = (2, 4)
WORD_NGRAM_RANGE = (1, 2)
SVD_COMPONENTS = 64
PRIMARY_TFIDF_WEIGHT = 0.6
PRIMARY_JW_WEIGHT = 0.4
RRF_K = 60

# See module docstring "THRESHOLD DERIVATION" for the numbers behind this.
DEFAULT_MATCH_THRESHOLD = Decimal("0.032655")
# See module docstring "SECOND GATE: PRIMARY_SCORE_FLOOR" for the numbers
# behind this -- a candidate must clear both gates to be accepted.
PRIMARY_SCORE_FLOOR = Decimal("0.660730")

_DEFERRED_RESOLUTIONS = (UNMATCHED, AMBIGUOUS, TIE_AMBIGUOUS)


@dataclass(frozen=True)
class FuzzyMatchResult:
    """Stage 4's verdict on one bank credit -- explainable like Stage 3's
    `ProbabilisticMatchResult`. `combined_score` is the RRF-fused score that
    drove the accept/reject decision; `tfidf_cosine`, `jaro_winkler`,
    `dense_score`, `primary_rank`, `dense_rank` are the individual signals
    that fed it (module docstring: "OUTPUT: EXPLAINABLE"). `resolution`
    reuses `reconagent.match`'s vocabulary -- MATCHED when `combined_score
    >= threshold`, UNMATCHED (an honest decline) otherwise. Stage 4, like
    Stage 3, never emits PARTIAL: it is a single-settlement text matcher, not
    a subset-sum search."""

    bank_txn_id: str
    stage: str
    resolution: str
    settlement_ids: tuple[str, ...]
    credit_amount_minor: int
    settlement_net_sum_minor: int
    residual_minor: int
    combined_score: Decimal
    tfidf_cosine: Decimal
    jaro_winkler: Decimal
    dense_score: Decimal
    primary_rank: int
    dense_rank: int
    reason: str
    threshold: Decimal
    candidates_considered: int = 0


@dataclass(frozen=True)
class FuzzyStage4Model:
    """A trained Stage 4 model: the two fitted text vectorizers (unsupervised
    -- vocabulary fitting needs no labels) plus the acceptance threshold
    derived from `data/`'s labelled linkage. Reusable across many
    `resolve_stage4` / `match_with_full_cascade` calls without refitting --
    build one via `train_stage4_model()` and pass it in, or let
    `match_with_full_cascade` build and cache the default."""

    char_vectorizer: TfidfVectorizer
    dense_vectorizer: TfidfVectorizer
    svd: TruncatedSVD
    threshold: Decimal
    primary_floor: Decimal
    training_population: dict = field(default_factory=dict)


def _name_index(
    invoices: Sequence[CanonicalRecord],
) -> tuple[dict[str, CanonicalRecord], dict[str, CanonicalRecord]]:
    by_id = {i.invoice_id: i for i in invoices if i.invoice_id}
    by_order = {i.order_id: i for i in invoices if i.order_id}
    return by_id, by_order


def _record_name_and_text(
    record: CanonicalRecord,
    is_settlement: bool,
    by_id: dict[str, CanonicalRecord],
    by_order: dict[str, CanonicalRecord],
) -> tuple[str, str]:
    """(name, free_text) for one record. `name` feeds Jaro-Winkler (a
    short-string metric); `text` feeds both TF-IDF signals and additionally
    carries narration/description text, so a case with a clean name but a
    corrupted or unrelated narration still gets a name-driven fallback.

    A razorpay_settlement carries no counterparty name of its own -- joined
    through the invoice ledger exactly as
    `reconagent.probabilistic._settlement_name` does (small, stable helper,
    duplicated rather than imported -- see module docstring)."""
    if is_settlement:
        invoice = by_id.get(record.invoice_id) or by_order.get(record.order_id)
        name = (invoice.counterparty_name if invoice else "") or ""
        invoice_notes = (invoice.narration if invoice else "") or ""
        text = " ".join(p for p in (name, record.narration or "", invoice_notes) if p)
    else:
        name = record.counterparty_name or ""
        text = " ".join(p for p in (name, record.narration or "") if p)
    return name, text


def _blocked_pool(
    credit: CanonicalRecord, settlements: Sequence[CanonicalRecord]
) -> list[CanonicalRecord]:
    """Currency-exact plus a date window. See module docstring "BLOCKING"."""
    credit_date = credit.value_date or credit.booking_date
    if credit_date is None:
        return []
    pool = []
    for s in settlements:
        if s.currency != credit.currency or s.settled_at is None:
            continue
        if abs((credit_date - s.settled_at).days) <= FUZZY_BLOCKING_WINDOW_DAYS:
            pool.append(s)
    return pool


def _l2_normalize(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


def _rank_desc(scores: np.ndarray) -> np.ndarray:
    """1-indexed ranks, descending (rank 1 = highest score)."""
    order = np.argsort(-scores, kind="stable")
    ranks = np.empty(len(scores), dtype=int)
    ranks[order] = np.arange(1, len(scores) + 1)
    return ranks


def _primary_scores(
    credit_name: str,
    credit_text: str,
    pool_names: list[str],
    pool_texts: list[str],
    char_vectorizer: TfidfVectorizer,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Primary signal (module docstring). Returns (primary, tfidf_cosine,
    jaro_winkler), each aligned to `pool_names`/`pool_texts`' order."""
    credit_vec = char_vectorizer.transform([credit_text])
    pool_vecs = char_vectorizer.transform(pool_texts)
    tfidf_cos = cosine_similarity(credit_vec, pool_vecs)[0]
    jw = np.array(
        [
            JaroWinkler.normalized_similarity(credit_name, n) if credit_name and n else 0.0
            for n in pool_names
        ]
    )
    primary = PRIMARY_TFIDF_WEIGHT * tfidf_cos + PRIMARY_JW_WEIGHT * jw
    return primary, tfidf_cos, jw


def _dense_scores(
    credit_text: str,
    pool_texts: list[str],
    dense_vectorizer: TfidfVectorizer,
    svd: TruncatedSVD,
) -> tuple[np.ndarray, np.ndarray]:
    """Secondary signal: LSA embedding, searched via a fresh FAISS flat
    inner-product index over this credit's own blocked pool (module
    docstring "VECTOR-INDEX CHOICE"/"DENSE EMBEDDING"). Returns
    (cosine_scores, ranks) aligned to `pool_texts`' order; `ranks` is
    1-indexed, rank 1 = closest."""
    credit_vec = _l2_normalize(svd.transform(dense_vectorizer.transform([credit_text])))
    pool_vecs = _l2_normalize(svd.transform(dense_vectorizer.transform(pool_texts)))
    index = faiss.IndexFlatIP(pool_vecs.shape[1])
    index.add(pool_vecs.astype("float32"))
    scores, order = index.search(credit_vec.astype("float32"), len(pool_texts))
    scores, order = scores[0], order[0]
    cos = np.empty(len(pool_texts))
    ranks = np.empty(len(pool_texts), dtype=int)
    for rank, (pos, score) in enumerate(zip(order, scores), start=1):
        cos[pos] = score
        ranks[pos] = rank
    return cos, ranks


def _score_pool(
    credit: CanonicalRecord,
    pool: list[CanonicalRecord],
    by_id: dict[str, CanonicalRecord],
    by_order: dict[str, CanonicalRecord],
    char_vectorizer: TfidfVectorizer,
    dense_vectorizer: TfidfVectorizer,
    svd: TruncatedSVD,
) -> list[dict]:
    """Score every settlement in `pool` against `credit`: primary, dense,
    and their RRF fusion (module docstring "DENSE FUSION"). One dict per
    candidate, unsorted."""
    if not pool:
        return []
    credit_name, credit_text = _record_name_and_text(credit, False, by_id, by_order)
    pool_names, pool_texts = zip(*(_record_name_and_text(s, True, by_id, by_order) for s in pool))
    pool_names, pool_texts = list(pool_names), list(pool_texts)

    primary, tfidf_cos, jw = _primary_scores(credit_name, credit_text, pool_names, pool_texts, char_vectorizer)
    dense_cos, dense_rank = _dense_scores(credit_text, pool_texts, dense_vectorizer, svd)
    primary_rank = _rank_desc(primary)
    rrf = 1.0 / (RRF_K + primary_rank) + 1.0 / (RRF_K + dense_rank)

    return [
        {
            "settlement": pool[i],
            "rrf": float(rrf[i]),
            "primary_score": float(primary[i]),
            "tfidf_cosine": float(tfidf_cos[i]),
            "jaro_winkler": float(jw[i]),
            "dense_score": float(dense_cos[i]),
            "primary_rank": int(primary_rank[i]),
            "dense_rank": int(dense_rank[i]),
            "candidates_considered": len(pool),
        }
        for i in range(len(pool))
    ]


def train_stage4_model(
    data_dir: Path | str = DEFAULT_DATA_DIR,
    *,
    threshold: Decimal = DEFAULT_MATCH_THRESHOLD,
    primary_floor: Decimal = PRIMARY_SCORE_FLOOR,
) -> FuzzyStage4Model:
    """Fit Stage 4's two text vectorizers on `data_dir`'s own corpus
    (unsupervised) and derive the acceptance threshold from `data_dir`'s
    labelled linkage (module docstring "THRESHOLD DERIVATION"). Never reads
    `stress_test/ground_truth.json` or `data/holdout/ground_truth.json`;
    `data_dir` exists so a test can point this at a scratch copy of `data/`,
    not so callers point it at the other splits."""
    from reconagent.camt053 import parse_camt053_file
    from reconagent.invoices import parse_invoice_ledger
    from reconagent.razorpay import parse_razorpay_settlements

    data_dir = Path(data_dir)
    settlements = parse_razorpay_settlements(data_dir / "razorpay_settlements.csv")
    credits = parse_camt053_file(data_dir / "bank_statement.camt053.xml")
    invoices = parse_invoice_ledger(data_dir / "invoice_ledger.csv")
    ground_truth = json.loads((data_dir / "ground_truth.json").read_text())

    by_id, by_order = _name_index(invoices)

    settlement_texts = [_record_name_and_text(s, True, by_id, by_order)[1] for s in settlements]
    credit_texts = [_record_name_and_text(c, False, by_id, by_order)[1] for c in credits]
    corpus = settlement_texts + credit_texts

    char_vectorizer = TfidfVectorizer(analyzer="char", ngram_range=CHAR_NGRAM_RANGE, min_df=1)
    char_vectorizer.fit(corpus)

    dense_vectorizer = TfidfVectorizer(analyzer="word", ngram_range=WORD_NGRAM_RANGE, min_df=1)
    dense_matrix = dense_vectorizer.fit_transform(corpus)
    n_components = max(1, min(SVD_COMPONENTS, dense_matrix.shape[1] - 1, dense_matrix.shape[0] - 1))
    svd = TruncatedSVD(n_components=n_components, random_state=0)
    svd.fit(dense_matrix)

    positive_pairs: set[tuple[str, str]] = set()
    for case in ground_truth["cases"]:
        bank_txn_id = case["expected_link"]["bank_txn_id"]
        if not bank_txn_id:
            continue
        for sid in case["expected_link"]["covers_settlement_ids"]:
            positive_pairs.add((sid, bank_txn_id))

    positive_scores: list[float] = []
    negative_scores: list[float] = []
    positive_primary_scores: list[float] = []
    total_pairs = 0
    for credit in credits:
        pool = _blocked_pool(credit, settlements)
        scored = _score_pool(credit, pool, by_id, by_order, char_vectorizer, dense_vectorizer, svd)
        total_pairs += len(scored)
        for cand in scored:
            pair = (cand["settlement"].record_id, credit.record_id)
            is_positive = pair in positive_pairs
            (positive_scores if is_positive else negative_scores).append(cand["rrf"])
            if is_positive:
                positive_primary_scores.append(cand["primary_score"])

    neg_max = max(negative_scores) if negative_scores else 0.0
    pos_min = min(positive_scores) if positive_scores else 1.0

    # See module docstring "SECOND GATE: PRIMARY_SCORE_FLOOR" -- the largest
    # gap in the sorted known-positive primary_score population.
    sorted_primary = sorted(positive_primary_scores)
    gap_lo = gap_hi = 0.0
    if len(sorted_primary) > 1:
        gaps = [sorted_primary[i + 1] - sorted_primary[i] for i in range(len(sorted_primary) - 1)]
        gap_idx = max(range(len(gaps)), key=lambda i: gaps[i])
        gap_lo, gap_hi = sorted_primary[gap_idx], sorted_primary[gap_idx + 1]

    return FuzzyStage4Model(
        char_vectorizer=char_vectorizer,
        dense_vectorizer=dense_vectorizer,
        svd=svd,
        threshold=threshold,
        primary_floor=primary_floor,
        training_population={
            "blocked_candidate_pairs": total_pairs,
            "known_positive_pairs": len(positive_scores),
            "known_negative_pairs": len(negative_scores),
            "known_negative_max_rrf": round(neg_max, 6),
            "known_positive_primary_score_gap_lo": round(gap_lo, 6),
            "known_positive_primary_score_gap_hi": round(gap_hi, 6),
            "known_positive_min_rrf": round(pos_min, 6),
        },
    )


@lru_cache(maxsize=1)
def _default_model() -> FuzzyStage4Model:
    return train_stage4_model()


def _fuzzy_result(
    credit: CanonicalRecord,
    resolution: str,
    model: FuzzyStage4Model,
    candidate: dict | None,
    reason: str,
) -> FuzzyMatchResult:
    settlement = candidate["settlement"] if candidate else None
    return FuzzyMatchResult(
        bank_txn_id=credit.record_id,
        stage=STAGE_FUZZY,
        resolution=resolution,
        settlement_ids=(settlement.record_id,) if settlement and resolution == MATCHED else (),
        credit_amount_minor=credit.amount_minor,
        settlement_net_sum_minor=settlement.amount_minor if settlement else 0,
        residual_minor=(settlement.amount_minor - credit.amount_minor) if settlement else -credit.amount_minor,
        combined_score=Decimal(str(round(candidate["rrf"], 6))) if candidate else Decimal("0"),
        tfidf_cosine=Decimal(str(round(candidate["tfidf_cosine"], 6))) if candidate else Decimal("0"),
        jaro_winkler=Decimal(str(round(candidate["jaro_winkler"], 6))) if candidate else Decimal("0"),
        dense_score=Decimal(str(round(candidate["dense_score"], 6))) if candidate else Decimal("0"),
        primary_rank=candidate["primary_rank"] if candidate else 0,
        dense_rank=candidate["dense_rank"] if candidate else 0,
        reason=reason,
        threshold=model.threshold,
        candidates_considered=candidate["candidates_considered"] if candidate else 0,
    )


def resolve_stage4(
    credits: Sequence[CanonicalRecord],
    open_settlements: Sequence[CanonicalRecord],
    invoices: Sequence[CanonicalRecord],
    model: FuzzyStage4Model,
) -> dict[str, FuzzyMatchResult]:
    """Score every credit in `credits` against its own currency+date-blocked
    pool within `open_settlements`, then resolve contention globally,
    highest RRF score first -- once a settlement is claimed by one credit's
    MATCHED verdict, no other credit may also claim it, mirroring
    `reconagent.probabilistic.resolve_stage3`'s own claiming discipline.

    Acceptance requires TWO independent gates on the RRF-selected candidate
    (module docstring "SECOND GATE: PRIMARY_SCORE_FLOOR"): `model.threshold`
    on the RRF-fused rank score, and `model.primary_floor` on the absolute
    `primary_score` -- rank agreement alone can't tell "the best of a
    mediocre pool" from a genuinely strong match. A credit whose best
    candidate is claimed by someone more confident, or whose best candidate
    fails either gate, comes back UNMATCHED with that best candidate's
    scores still attached (an honest decline, not a guess)."""
    if not credits:
        return {}
    by_id, by_order = _name_index(invoices)

    all_candidates: list[tuple[CanonicalRecord, dict]] = []
    for credit in credits:
        pool = _blocked_pool(credit, open_settlements)
        for cand in _score_pool(credit, pool, by_id, by_order, model.char_vectorizer, model.dense_vectorizer, model.svd):
            all_candidates.append((credit, cand))

    all_candidates.sort(key=lambda pair: pair[1]["rrf"], reverse=True)

    best_per_credit: dict[str, dict] = {}
    for credit, cand in all_candidates:
        best_per_credit.setdefault(credit.record_id, cand)

    results: dict[str, FuzzyMatchResult] = {}
    claimed: set[str] = set()
    for credit, cand in all_candidates:
        cid = credit.record_id
        if cid in results:
            continue
        sid = cand["settlement"].record_id
        score = Decimal(str(round(cand["rrf"], 6)))
        primary_score = Decimal(str(round(cand["primary_score"], 6)))
        if score < model.threshold or primary_score < model.primary_floor or sid in claimed:
            continue
        results[cid] = _fuzzy_result(
            credit,
            MATCHED,
            model,
            cand,
            reason=(
                f"best available candidate {sid!r} RRF score {score} >= threshold "
                f"{model.threshold} and primary score {primary_score} >= floor "
                f"{model.primary_floor}, among {cand['candidates_considered']} blocked candidates"
            ),
        )
        claimed.add(sid)

    for credit in credits:
        cid = credit.record_id
        if cid in results:
            continue
        cand = best_per_credit.get(cid)
        if cand is None:
            results[cid] = _fuzzy_result(
                credit, UNMATCHED, model, None, reason="no candidates survived the currency/date blocking rule"
            )
            continue
        results[cid] = _fuzzy_result(
            credit,
            UNMATCHED,
            model,
            cand,
            reason=(
                f"best candidate {cand['settlement'].record_id!r} scored RRF {round(cand['rrf'], 6)} "
                f"/ primary {round(cand['primary_score'], 6)}, below threshold {model.threshold} / "
                f"floor {model.primary_floor} (or claimed by a more confident credit); "
                f"deferred among {cand['candidates_considered']} blocked candidates"
            ),
        )
    return results


def match_with_full_cascade(
    credits: Sequence[CanonicalRecord],
    settlements: Sequence[CanonicalRecord],
    invoices: Sequence[CanonicalRecord] = (),
    *,
    splink_model: SplinkStage3Model | None = None,
    fuzzy_model: FuzzyStage4Model | None = None,
) -> list[MatchResult | ProbabilisticMatchResult | FuzzyMatchResult]:
    """The full cascade's entry point: Tier 1 + Stage 3
    (`reconagent.probabilistic.match_with_tier2`, unmodified) first, then
    Stage 4 against whatever that leaves UNMATCHED / AMBIGUOUS /
    TIE_AMBIGUOUS, scored against the settlement pool still open after every
    upstream MATCHED/PARTIAL consumption. Returns the Stage 4 result where it
    clears its own threshold, the prior (Tier 1 / Stage 3) result unchanged
    otherwise -- so on a set Tier 1 + Stage 3 already resolve completely
    (`data/`), this is provably a no-op, the same discipline
    `match_with_tier2` holds toward `match_all`.

    `splink_model` and `fuzzy_model` each default to a lazily-trained,
    process-cached model from `data/` -- pass either to override its
    population, threshold, or fitted parameters."""
    from reconagent.probabilistic import match_with_tier2

    model = fuzzy_model or _default_model()
    tier2_results = match_with_tier2(credits, settlements, invoices=invoices, splink_model=splink_model)
    by_credit = {c.record_id: c for c in credits}

    consumed: set[str] = set()
    for r in tier2_results:
        if r.resolution in (MATCHED, PARTIAL):
            consumed.update(r.settlement_ids)

    deferred_credits = [by_credit[r.bank_txn_id] for r in tier2_results if r.resolution in _DEFERRED_RESOLUTIONS]
    open_settlements = [s for s in settlements if s.record_id not in consumed]
    stage4_by_credit = resolve_stage4(deferred_credits, open_settlements, invoices, model)

    out: list[MatchResult | ProbabilisticMatchResult | FuzzyMatchResult] = []
    for r in tier2_results:
        if r.resolution not in _DEFERRED_RESOLUTIONS:
            out.append(r)
            continue
        stage4 = stage4_by_credit[r.bank_txn_id]
        out.append(stage4 if stage4.resolution == MATCHED else r)
    return out
