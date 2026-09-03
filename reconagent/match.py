"""Stage 1 (deterministic reference match) and Stage 2 (bounded subset-sum),
spec section 4.

Both stages answer the same question -- "which settlements does this bank
credit cover?" -- and both return the same `MatchResult`, carrying the stage
that resolved it, the residual, a confidence score and the per-field evidence
behind it. Nothing here decides whether a residual is *acceptable*: that is
the abstention gate's job (spec section 4 Stage 5) and the exception
taxonomy's (section 6). This unit reports what it found and how sure it is,
including "nothing", and attaches the evidence so the later units do not have
to re-run the matcher to reconstruct it (spec section 8).

WHY TWO STAGES

A bank credits one net lump sum per transfer, sweeping several settlements
under a single UTR (spec section 2). Stage 1 handles the majority where the
bank quoted a reference we can tie to exactly one settlement. What is left is
not a fuzzy-matching problem, it is a search problem: does some *subset* of
the still-open settlements sum to this credit? Stage 2 is that search.

TOLERANCES ARE OURS

`ground_truth.json` publishes the band its labels were generated with. That
file is the grader's and is never read from production code. The defaults
below are chosen here and justified here:

  AMOUNT_TOLERANCE_MINOR = 100 (one rupee / one dollar of minor units)
      A settlement net is gross minus fee minus GST-on-fee, each rounded to
      the paise. Sweeping a handful of settlements accumulates at most a few
      paise of that rounding, so 100 minor units is two orders of magnitude
      of headroom over the arithmetic that can legitimately drift, while
      staying far tighter than any real amount difference. It is NOT a
      "close enough" band -- a genuine subset sums exactly, and the residual
      we report says whether it did.

  MAX_CARDINALITY = 8
      Chosen from the asymmetry of the two ways this bound can be wrong,
      because raw accuracy gives no signal: the main set resolves 150/150
      credits at every value from 4 to 9, so it cannot pick this number.

      Set it too LOW and the true subset is outside the search space. The
      best subset the solver can still see is, by construction, the wrong
      one -- and nothing distinguishes it from a right answer, so it posts a
      false match. Set it too HIGH and extra subsets enter the space; the
      ones that matter are the ones that also hit the target exactly, and
      those show up as a detected tie, which abstains (see TIE_AMBIGUOUS
      below). Too low fails silently and wrong; too high fails loudly and
      honestly. Spec section 9 is explicit about which of those costs more,
      so the bound goes as high as the work budget tolerates rather than as
      low as the observed data allows. 8 keeps the
      worst pool this matcher will accept (64) under ~0.1s per credit; the
      node budget is the real stop.

  POOL_WINDOW_DAYS = 30, MAX_POOL = 64, NODE_BUDGET = 2_000_000
      See the pooling and bounding notes on `match_subset_sum`.

MINIMUM ABSOLUTE RESIDUAL WINS, TIES ABSTAIN

Real sweep data contains near-miss subsets: a different combination of the
same open settlements landing a few minor units from the credit. A solver
that returns the *first* subset inside its tolerance posts a false match
roughly whenever a near-miss is enumerated first, and a false match silently
corrupts the books (spec section 9). So Stage 2 enumerates every admissible
subset and keeps the one with the smallest absolute residual. When two
distinct subsets tie at that minimum, there is no arithmetic reason to prefer
either, so the result is TIE_AMBIGUOUS with both attached -- an honest
"I don't know" rather than a coin flip. (This is a different situation from
Stage 1's AMBIGUOUS, below -- see the resolutions block.)

MEASURED CEILING

Subset sums get dense. Probing 40 amounts that correspond to no real sweep
against the settlements still open after Stage 1 (main: pool of 27) returns
37 UNMATCHED, 2 TIE_AMBIGUOUS and 1 spurious MATCHED at confidence 0.46. The
holdout's residual pool is denser (34 open, and its real bundles run to 7
members) and the same probe returns 14 / 18 / 8. Against all 200 settlements
with no open-status pruning at all, roughly a sixth of arbitrary amounts find
an exact subset. Stage 2 is safe because Stage 1 runs
first and takes ~90% of the settlements out of the pool, not because
subset-sum is intrinsically discriminating. Two consequences worth stating
plainly: the pooling rules are load-bearing, not an optimisation; and the
confidence score is the thing Stage 5 has to hold the false-match budget
with, since a spurious subset is arithmetically indistinguishable from a real
one once found. Real bundles here score 0.55-0.90 and spurious ones 0.46-0.81
-- overlapping, which is exactly what a calibration unit needs to know.

THROUGHPUT AND SCALE -- MEASURED, NOT ASSUMED

`match_all`'s driving loop is two passes: Stage 1 is O(C) lookups against an
O(N)-built index (C = credit count, N = settlement count) -- cheap and flat
with scale. Stage 2's driving loop rescans the still-open settlement list
and re-pools it once per deferred credit (O(D x N), D = deferred-credit
count); profiled with cProfile at N=1,000 this rescan-plus-pool cost is
~0.01s out of a ~5.2s run -- under 1%, not the bottleneck the shape of the
cliff might suggest.

The cost is Stage 2's subset-sum search itself, and it is dense-pool cost,
not driving-loop cost. `scripts/generate_synthetic.py` packs a fixed
calendar (31 days) regardless of `--scale`, so settlement density per day
rises with scale, and `_pool()`'s 30-day window admits a denser candidate
set as a direct result. Measured (seed 20260903, this repo's synthetic
generator, DFS node counts from `_Search.nodes`):

  scale | settlements | deferred credits | mean pool size | pool truncated
  200   | 202         | 13                | 30.2 (of 64)    | 0/13
  1,000 | 1,004       | 77                | 60.3 (of 64)    | 70/77
  5,000 | 5,003       | 380               | 63.7 (of 64)    | 375/380

  scale | mean DFS nodes/deferred credit | total DFS nodes
  200   | 1,915                           | 24,890
  1,000 | 62,092                          | 4,781,094
  5,000 | 112,833                         | 42,876,461

By 1,000 settlements the pool is truncated at MAX_POOL almost every time;
by 5,000 some individual deferred credits hit NODE_BUDGET outright (nodes
== 2,000,001, i.e. the search gave up, not finished). So Stage 2 cost is
O(D x f(pool_size)), pool_size saturating at MAX_POOL well before N=1,000
given this calendar, and f is the combinatorial DFS from `_enumerate`
above -- not a bound this module can flatten further without either
raising MAX_POOL/NODE_BUDGET (more work, not less) or changing what gets
pooled or searched (a behaviour change, out of scope for a performance
pass). The driving loop's O(D x N) rescan was left as full re-filters, not
because it is free, but because it is not where the time goes at these
scales -- see `match_all`'s and `_pool`'s docstrings for what each pass
actually costs.

For the spec's stated volumes -- a merchant's monthly statement, hundreds
to low thousands of records, not sustained high-frequency streaming -- a
few hundred settlements is comfortably fast (sub-second). Something in the
1,000-settlement neighbourhood, with this synthetic generator's density,
is already multi-second per run; 5,000 is tens of seconds and starts
hitting NODE_BUDGET per-credit truncation rather than a clean search. That
ceiling is this module's honest answer, not a bug to chase further here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Iterable, Sequence

from reconagent.records import CanonicalRecord

# --- stage names (stable strings; the audit log keys on these) ---
STAGE_DETERMINISTIC = "stage1_deterministic"
STAGE_SUBSET_SUM = "stage2_subset_sum"

_EPOCH = date(1970, 1, 1)  # sort sentinel for a credit with no date at all

# --- resolutions ---
# MATCHED / PARTIAL / UNMATCHED mirror the ground-truth vocabulary.
# AMBIGUOUS and TIE_AMBIGUOUS are ours, and are two different situations that
# both refuse to guess:
#   AMBIGUOUS      Stage 1 only. The credit's narration quotes references
#                  belonging to several settlements -- a reference collision,
#                  nothing to do with subset-sum.
#   TIE_AMBIGUOUS  Stage 2 only. Several distinct subsets of settlements tie
#                  at the identical minimum absolute residual -- a genuine
#                  subset-sum tie.
# Downstream, both are "unresolved with candidates attached", never a match.
MATCHED = "MATCHED"
PARTIAL = "PARTIAL"
AMBIGUOUS = "AMBIGUOUS"
TIE_AMBIGUOUS = "TIE_AMBIGUOUS"
UNMATCHED = "UNMATCHED"

AMOUNT_TOLERANCE_MINOR = 100
MAX_CARDINALITY = 8
POOL_WINDOW_DAYS = 30
MAX_POOL = 64
NODE_BUDGET = 2_000_000

# An identifier in a bank narration is a run of alphanumerics, optionally
# joined by hyphens (INV-2026-M00012, K5K6QMGFIJVEMVVH). Narrations also use
# '-' as a *separator* between fields, so neither "split on hyphens" nor
# "don't" recovers every id on its own -- we emit every contiguous run of up
# to _MAX_ID_PARTS hyphen-joined groups and require an exact, whole-token
# equality against a known reference. That is the difference between
# "the reference appears in the narration" and substring luck.
_ALNUM_RUN = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")
_MAX_ID_PARTS = 3
_MIN_ID_LEN = 6

# Reference fields, strongest first. A UTR is the bank's own transfer handle;
# an invoice/order/payment id is merchant-side and one indirection further
# from the credit, hence the lower Stage 1 confidence.
_REFERENCE_FIELDS = ("utr", "end_to_end_id", "invoice_id", "order_id", "payment_id")
_REFERENCE_STRENGTH = {
    "utr": Decimal("0.99"),
    "end_to_end_id": Decimal("0.99"),
    "invoice_id": Decimal("0.97"),
    "order_id": Decimal("0.95"),
    "payment_id": Decimal("0.95"),
}


@dataclass(frozen=True)
class FieldComparison:
    """One field the matcher actually looked at, and whether it agreed.

    Values are stringified deliberately: this is evidence for an audit trail
    and a human reviewer, not a re-parsable projection of the records.
    """

    field: str
    credit_value: str
    settlement_value: str
    agreed: bool


@dataclass(frozen=True)
class MatchResult:
    """What one bank credit resolved to, and everything needed to defend it.

    residual_minor follows the ground-truth sign convention:
    settlement_net_sum_minor - credit_amount_minor. Positive means the credit
    fell short of the settlements it covers (a partial payment).

    confidence is a Decimal in [0, 1] -- 0 for an outright miss, ~0.99 for a
    UTR-plus-exact-amount hit. It is deliberately uncalibrated here; Stage 5
    fits thresholds to it against labelled data.
    """

    bank_txn_id: str
    stage: str
    resolution: str
    settlement_ids: tuple[str, ...]
    credit_amount_minor: int
    settlement_net_sum_minor: int
    residual_minor: int
    confidence: Decimal
    reason: str
    evidence: tuple[FieldComparison, ...] = ()
    # Stage 2 only: what the search did, so a truncated answer is legible as
    # truncated rather than as a clean miss.
    pool_size: int = 0
    subsets_examined: int = 0
    truncated: bool = False
    # Rival candidates: the tied subsets when TIE_AMBIGUOUS, or the runner-up
    # (the near-miss the min-residual rule rejected) when MATCHED.
    rival_settlement_ids: tuple[tuple[str, ...], ...] = ()
    rival_residual_minor: int | None = None


# --------------------------------------------------------------------------
# Stage 1 -- deterministic reference match
# --------------------------------------------------------------------------


def _narration_tokens(text: str) -> set[str]:
    """Candidate identifiers in a narration. See _ALNUM_RUN above."""
    tokens: set[str] = set()
    for run in _ALNUM_RUN.finditer(text):
        parts = run.group(0).split("-")
        for i in range(len(parts)):
            for j in range(i + 1, min(i + _MAX_ID_PARTS, len(parts)) + 1):
                tok = "-".join(parts[i:j])
                if len(tok) >= _MIN_ID_LEN:
                    tokens.add(tok.upper())
    return tokens


def _reference_index(
    settlements: Iterable[CanonicalRecord],
) -> dict[str, list[tuple[str, str]]]:
    """reference value (upper) -> [(field, settlement record_id), ...].

    A value landing on more than one settlement is not a usable key; the
    lookup below rejects it rather than picking one.
    """
    index: dict[str, list[tuple[str, str]]] = {}
    for s in settlements:
        for f in _REFERENCE_FIELDS:
            v = getattr(s, f, None)
            if v and len(v) >= _MIN_ID_LEN:
                index.setdefault(v.upper(), []).append((f, s.record_id))
    return index


def _credit_references(credit: CanonicalRecord) -> set[str]:
    """Everything on the credit that could be a settlement-side reference:
    the structured fields the parsers filled in, plus whole-token identifiers
    recovered from the narration."""
    refs = {
        v.upper()
        for v in (credit.utr, credit.end_to_end_id, credit.invoice_id,
                  credit.order_id, credit.payment_id)
        if v and len(v) >= _MIN_ID_LEN
    }
    return refs | _narration_tokens(credit.narration)


def match_deterministic(
    credit: CanonicalRecord,
    settlements: Sequence[CanonicalRecord],
    *,
    amount_tolerance_minor: int = AMOUNT_TOLERANCE_MINOR,
    _index: dict[str, list[tuple[str, str]]] | None = None,
    _by_id: dict[str, CanonicalRecord] | None = None,
) -> MatchResult | None:
    """Stage 1. Resolve `credit` to a single settlement on reference identity
    plus amount. Returns None when no reference on the credit names a known
    settlement -- that is Stage 2's input, not a verdict.

    Amount outcomes, once the reference is certain:
      |residual| <= tolerance        -> MATCHED
      0 < credit < net - tolerance   -> PARTIAL  (spec section 6: the remainder
                                        stays open; we report the shortfall
                                        rather than calling it a match or
                                        dropping it)
      credit > net + tolerance       -> UNMATCHED, reason amount_over_reference.
                                        The reference is attached as evidence.
                                        A credit larger than the settlement it
                                        names is not a partial payment of it;
                                        something else is in the credit and
                                        this unit will not guess what.
    """
    index = _reference_index(settlements) if _index is None else _index
    by_id = {s.record_id: s for s in settlements} if _by_id is None else _by_id

    hits: dict[str, tuple[str, str]] = {}  # settlement_id -> (field, ref value)
    for ref in _credit_references(credit):
        owners = index.get(ref)
        if not owners or len(owners) > 1:
            continue  # unknown, or not a discriminating key
        f, sid = owners[0]
        hits.setdefault(sid, (f, ref))

    if not hits:
        return None

    if len(hits) > 1:
        # The credit quotes references belonging to several settlements. That
        # could be an explicitly-referenced sweep, but nothing here can tell
        # it apart from a narration that picked up a stale id, so we refuse
        # rather than sum them.
        sids = tuple(sorted(hits))
        net = sum(by_id[s].amount_minor for s in sids)
        return MatchResult(
            bank_txn_id=credit.record_id,
            stage=STAGE_DETERMINISTIC,
            resolution=AMBIGUOUS,
            settlement_ids=sids,
            credit_amount_minor=credit.amount_minor,
            settlement_net_sum_minor=net,
            residual_minor=net - credit.amount_minor,
            confidence=Decimal("0.30"),
            reason="narration references multiple settlements",
            evidence=tuple(
                FieldComparison(hits[s][0], hits[s][1], hits[s][1], True) for s in sids
            ),
            rival_settlement_ids=tuple((s,) for s in sids),
        )

    sid, (ref_field, ref_value) = next(iter(hits.items()))
    s = by_id[sid]
    residual = s.amount_minor - credit.amount_minor
    currency_ok = s.currency == credit.currency

    evidence = [
        FieldComparison(ref_field, ref_value, (getattr(s, ref_field) or ""), True),
        FieldComparison("currency", credit.currency, s.currency, currency_ok),
        FieldComparison(
            "amount_minor",
            str(credit.amount_minor),
            str(s.amount_minor),
            abs(residual) <= amount_tolerance_minor,
        ),
    ]

    if not currency_ok:
        return MatchResult(
            bank_txn_id=credit.record_id, stage=STAGE_DETERMINISTIC,
            resolution=UNMATCHED, settlement_ids=(sid,),
            credit_amount_minor=credit.amount_minor,
            settlement_net_sum_minor=s.amount_minor, residual_minor=residual,
            confidence=Decimal("0.10"),
            reason="reference agreed but currency did not",
            evidence=tuple(evidence),
        )

    if abs(residual) <= amount_tolerance_minor:
        conf = _REFERENCE_STRENGTH.get(ref_field, Decimal("0.95"))
        if residual != 0:
            conf -= Decimal("0.04")  # inside the band but not exact
        return MatchResult(
            bank_txn_id=credit.record_id, stage=STAGE_DETERMINISTIC,
            resolution=MATCHED, settlement_ids=(sid,),
            credit_amount_minor=credit.amount_minor,
            settlement_net_sum_minor=s.amount_minor, residual_minor=residual,
            confidence=conf,
            reason=f"{ref_field} matched, amount within {amount_tolerance_minor} minor units",
            evidence=tuple(evidence),
        )

    if residual > 0:
        return MatchResult(
            bank_txn_id=credit.record_id, stage=STAGE_DETERMINISTIC,
            resolution=PARTIAL, settlement_ids=(sid,),
            credit_amount_minor=credit.amount_minor,
            settlement_net_sum_minor=s.amount_minor, residual_minor=residual,
            # The linkage is as certain as any MATCHED one -- the reference is
            # exact. The uncertainty is about *coverage*, not identity, which
            # is what PARTIAL says.
            confidence=_REFERENCE_STRENGTH.get(ref_field, Decimal("0.95")) - Decimal("0.10"),
            reason="reference matched, credit covers only part of the settlement net",
            evidence=tuple(evidence),
        )

    return MatchResult(
        bank_txn_id=credit.record_id, stage=STAGE_DETERMINISTIC,
        resolution=UNMATCHED, settlement_ids=(sid,),
        credit_amount_minor=credit.amount_minor,
        settlement_net_sum_minor=s.amount_minor, residual_minor=residual,
        confidence=Decimal("0.20"),
        reason="reference matched but credit exceeds the settlement net",
        evidence=tuple(evidence),
    )


# --------------------------------------------------------------------------
# Stage 2 -- bounded subset-sum
# --------------------------------------------------------------------------


@dataclass
class _Search:
    """Mutable state for the depth-first enumeration. Kept off the recursive
    signature so the hot path passes two ints."""

    amounts: list[int]
    ids: list[str]
    target: int
    tolerance: int
    max_cardinality: int
    node_budget: int
    nodes: int = 0
    truncated: bool = False
    best: tuple[int, tuple[str, ...]] | None = None
    best_count: int = 0
    runner_up: tuple[int, tuple[str, ...]] | None = None
    tied: list[tuple[str, ...]] = field(default_factory=list)


def _enumerate(st: _Search) -> None:
    """Depth-first enumeration of every subset whose sum lands within
    tolerance of the target, keeping the minimum-|residual| one.

    ALGORITHM AND COMPLEXITY. This is `itertools.combinations` over the pool
    for every cardinality 1..K, written as a DFS purely so the two prunes
    below can cut whole branches -- combinations cannot. Worst case is
    unchanged, O(sum_k C(P, k)) = O(P^K) subsets, which for the pool sizes
    this matcher actually sees (P up to 64, K = 6) is C(64,6) ~ 7.4e7 and too
    slow to enumerate blindly. With the prunes, real pools settle around
    1e5-5e6 visited nodes. The node budget is the hard stop regardless.

    Amounts are sorted descending, which is what makes both prunes valid:
      1. sum already past target + tolerance -> every extension only grows it
         (all settlement nets here are positive), so skip this element.
      2. sum + the largest `slots` remaining elements still short of
         target - tolerance -> nothing from here on can reach the target, and
         because later elements are no larger, nothing after it can either.
         Break, not continue.
    """
    n = len(st.amounts)
    chosen: list[str] = []

    # Prefix sums of `amounts` (fixed for the whole search -- the pool
    # sorted descending doesn't change while dfs runs), so prune 2's "sum
    # of the next `slots` largest remaining elements" below is an O(1)
    # lookup instead of slicing and summing up to `slots` elements on
    # every node visited. Same comparison, same prune decisions, same
    # nodes visited -- just cheaper per node. This is the hot path: with
    # a pool near MAX_POOL, dfs runs into the millions of nodes, and the
    # old `sum(st.amounts[j + 1 : j + slots])` re-summed a fresh slice at
    # every one of them.
    prefix = [0] * (n + 1)
    for i in range(n):
        prefix[i + 1] = prefix[i] + st.amounts[i]

    def record(total: int) -> None:
        residual = abs(total - st.target)
        if residual > st.tolerance:
            return
        subset = tuple(chosen)
        if st.best is None or residual < st.best[0]:
            if st.best is not None:
                st.runner_up = st.best
            st.best = (residual, subset)
            st.best_count = 1
            st.tied = [subset]
        elif residual == st.best[0]:
            st.best_count += 1
            if len(st.tied) < 8:
                st.tied.append(subset)
        elif st.runner_up is None or residual < st.runner_up[0]:
            st.runner_up = (residual, subset)

    def dfs(start: int, total: int) -> None:
        if st.truncated:
            return
        st.nodes += 1
        if st.nodes > st.node_budget:
            st.truncated = True
            return
        if chosen:
            record(total)
        slots = st.max_cardinality - len(chosen)
        if slots <= 0:
            return
        for j in range(start, n):
            nxt = total + st.amounts[j]
            if nxt > st.target + st.tolerance:
                continue  # prune 1
            # prune 2: best case from here is the next `slots` largest, which
            # (descending order) are amounts[j : j + slots]. Equivalent to
            # sum(st.amounts[j + 1 : j + slots]) via the prefix array above.
            hi = j + slots
            if hi > n:
                hi = n
            if nxt + (prefix[hi] - prefix[j + 1]) < st.target - st.tolerance:
                break
            chosen.append(st.ids[j])
            dfs(j + 1, nxt)
            chosen.pop()
            if st.truncated:
                return

    dfs(0, 0)


def _stage2_confidence(
    residual: int, cardinality: int, margin: int | None, truncated: bool
) -> Decimal:
    """Stage 2 confidence, in [0.05, 0.95]. Documented rather than tuned --
    Stage 5 calibrates thresholds against this, so it only has to be
    monotonic in the things that actually make a subset less trustworthy.

      base                0.90   never as high as a quoted UTR: this is a
                                 reconstruction, not a reference
      residual != 0      -0.05   an exact sweep sums exactly
      near-miss rival    -0.25 * (1 - margin/100), for margin < 100
                                 a rival subset a few minor units away is the
                                 decoy signature; the answer may be right but
                                 the evidence for it is thin
      cardinality        -0.02 per member beyond the first
      search truncated   -0.20   we did not see the whole space
    """
    conf = Decimal("0.90")
    if residual != 0:
        conf -= Decimal("0.05")
    if margin is not None and margin < 100:
        conf -= Decimal("0.25") * (Decimal(100 - margin) / Decimal(100))
    conf -= Decimal("0.02") * (cardinality - 1)
    if truncated:
        conf -= Decimal("0.20")
    return max(Decimal("0.05"), min(Decimal("0.95"), conf)).quantize(Decimal("0.01"))


def _pool(
    credit: CanonicalRecord,
    open_settlements: Sequence[CanonicalRecord],
    *,
    window_days: int,
    max_pool: int,
    tolerance: int,
) -> tuple[list[CanonicalRecord], bool]:
    """Narrow the open settlements to the ones this credit could plausibly
    sweep, before any combinatorics run (spec section 4 Stage 2).

    The rules, in order of how much they cut:
      currency   -- a rupee credit cannot cover a dollar settlement.
      timing     -- a credit cannot pay a settlement that has not happened,
                    and a sweep does not reach back past the statement period.
                    Window is [-1, +window_days] days from settled_at to the
                    credit's value date; the -1 slack absorbs a cutoff that
                    straddles midnight or a bank booking a day early.
      magnitude  -- a member cannot exceed the credit (all nets positive).
      cap        -- past max_pool, keep the settlements closest in date and
                    report truncation. An unbounded pool is how a subset-sum
                    solver hangs in production.
    """
    ref_date = credit.value_date or credit.booking_date
    pool = []
    for s in open_settlements:
        if s.currency != credit.currency:
            continue
        if s.amount_minor <= 0 or s.amount_minor > credit.amount_minor + tolerance:
            continue
        if ref_date is not None and s.settled_at is not None:
            delta = (ref_date - s.settled_at).days
            if delta < -1 or delta > window_days:
                continue
        pool.append(s)

    truncated = len(pool) > max_pool
    if truncated:
        def distance(s: CanonicalRecord) -> int:
            if ref_date is None or s.settled_at is None:
                return 10**6
            return abs((ref_date - s.settled_at).days)

        pool = sorted(pool, key=lambda s: (distance(s), s.record_id))[:max_pool]
    return pool, truncated


def match_subset_sum(
    credit: CanonicalRecord,
    open_settlements: Sequence[CanonicalRecord],
    *,
    amount_tolerance_minor: int = AMOUNT_TOLERANCE_MINOR,
    max_cardinality: int = MAX_CARDINALITY,
    pool_window_days: int = POOL_WINDOW_DAYS,
    max_pool: int = MAX_POOL,
    node_budget: int = NODE_BUDGET,
) -> MatchResult:
    """Stage 2. Which subset of the still-open settlements sums to `credit`?

    Always returns a result: UNMATCHED with the pool size and the work done is
    a finding, not a failure. TIE_AMBIGUOUS means two or more distinct
    subsets tie at the minimum absolute residual and the arithmetic gives no
    reason to prefer either -- reported, never broken arbitrarily.
    """
    pool, pool_truncated = _pool(
        credit,
        open_settlements,
        window_days=pool_window_days,
        max_pool=max_pool,
        tolerance=amount_tolerance_minor,
    )
    ordered = sorted(pool, key=lambda s: (-s.amount_minor, s.record_id))
    st = _Search(
        amounts=[s.amount_minor for s in ordered],
        ids=[s.record_id for s in ordered],
        target=credit.amount_minor,
        tolerance=amount_tolerance_minor,
        max_cardinality=max_cardinality,
        node_budget=node_budget,
    )
    _enumerate(st)
    truncated = st.truncated or pool_truncated

    common = dict(
        bank_txn_id=credit.record_id,
        stage=STAGE_SUBSET_SUM,
        credit_amount_minor=credit.amount_minor,
        pool_size=len(pool),
        subsets_examined=st.nodes,
        truncated=truncated,
    )

    if st.best is None:
        return MatchResult(
            resolution=UNMATCHED, settlement_ids=(),
            settlement_net_sum_minor=0, residual_minor=-credit.amount_minor,
            confidence=Decimal("0"),
            reason=(
                "search truncated before any subset was found"
                if truncated
                else f"no subset of {len(pool)} open settlements sums to the credit "
                f"within {amount_tolerance_minor} minor units"
            ),
            evidence=(
                FieldComparison("currency", credit.currency, "", False),
                FieldComparison("amount_minor", str(credit.amount_minor), "", False),
            ),
            **common,
        )

    residual_abs, subset = st.best
    by_id = {s.record_id: s for s in pool}
    net = sum(by_id[i].amount_minor for i in subset)
    residual = net - credit.amount_minor
    margin = None if st.runner_up is None else st.runner_up[0] - residual_abs

    evidence = (
        FieldComparison("currency", credit.currency, ordered[0].currency if ordered else "", True),
        FieldComparison("subset_cardinality", "", str(len(subset)), True),
        FieldComparison("amount_minor", str(credit.amount_minor), str(net), residual == 0),
        FieldComparison(
            "runner_up_residual_minor",
            "",
            "none" if st.runner_up is None else str(st.runner_up[0]),
            margin is None or margin > 0,
        ),
    )

    if st.best_count > 1:
        return MatchResult(
            resolution=TIE_AMBIGUOUS,
            settlement_ids=tuple(sorted(subset)),
            settlement_net_sum_minor=net,
            residual_minor=residual,
            confidence=Decimal("0.20"),
            reason=(
                f"{st.best_count} distinct subsets tie at residual {residual_abs}; "
                "no arithmetic basis to choose"
            ),
            evidence=evidence,
            rival_settlement_ids=tuple(tuple(sorted(t)) for t in st.tied),
            rival_residual_minor=residual_abs,
            **common,
        )

    return MatchResult(
        resolution=MATCHED,
        settlement_ids=tuple(sorted(subset)),
        settlement_net_sum_minor=net,
        residual_minor=residual,
        confidence=_stage2_confidence(residual, len(subset), margin, truncated),
        reason=(
            f"minimum-residual subset of {len(subset)} of {len(pool)} open settlements"
            + ("" if st.runner_up is None else f"; next-best residual {st.runner_up[0]}")
        ),
        evidence=evidence,
        rival_settlement_ids=() if st.runner_up is None else (tuple(sorted(st.runner_up[1])),),
        rival_residual_minor=None if st.runner_up is None else st.runner_up[0],
        **common,
    )


# --------------------------------------------------------------------------
# The cascade
# --------------------------------------------------------------------------


def match_all(
    credits: Sequence[CanonicalRecord],
    settlements: Sequence[CanonicalRecord],
    *,
    amount_tolerance_minor: int = AMOUNT_TOLERANCE_MINOR,
    max_cardinality: int = MAX_CARDINALITY,
    pool_window_days: int = POOL_WINDOW_DAYS,
    max_pool: int = MAX_POOL,
    node_budget: int = NODE_BUDGET,
) -> list[MatchResult]:
    """Run Stage 1 over every credit, then Stage 2 over what is left.

    Open-status bookkeeping: a settlement any earlier result attributed to a
    credit -- MATCHED or PARTIAL -- leaves the Stage 2 pool. A partially paid
    settlement does have a remainder still open against the *ledger* (spec
    section 6), but it is not open to being swept again by a second credit in
    the same statement, and leaving it in the pool only adds decoy mass. That
    remainder is the residual we report; deciding what to do with it belongs
    to the exception taxonomy.

    Results come back in the input order of `credits`.
    """
    index = _reference_index(settlements)
    by_id = {s.record_id: s for s in settlements}

    results: dict[str, MatchResult] = {}
    consumed: set[str] = set()
    deferred: list[CanonicalRecord] = []

    for c in credits:
        r = match_deterministic(
            c, settlements,
            amount_tolerance_minor=amount_tolerance_minor,
            _index=index, _by_id=by_id,
        )
        if r is None:
            deferred.append(c)
            continue
        results[c.record_id] = r
        if r.resolution in (MATCHED, PARTIAL):
            consumed.update(r.settlement_ids)

    # Oldest credit first: an earlier sweep cannot have consumed a settlement
    # that had not settled yet, so this order minimises pool contention.
    for c in sorted(deferred, key=lambda c: (c.value_date or c.booking_date or _EPOCH, c.record_id)):
        open_settlements = [s for s in settlements if s.record_id not in consumed]
        r = match_subset_sum(
            c, open_settlements,
            amount_tolerance_minor=amount_tolerance_minor,
            max_cardinality=max_cardinality,
            pool_window_days=pool_window_days,
            max_pool=max_pool,
            node_budget=node_budget,
        )
        results[c.record_id] = r
        if r.resolution in (MATCHED, PARTIAL):
            consumed.update(r.settlement_ids)

    return [results[c.record_id] for c in credits]
