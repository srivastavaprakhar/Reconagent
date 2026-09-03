"""Tests for Stage 1 (deterministic) and Stage 2 (bounded subset-sum).

`ground_truth.json` is read HERE and only here -- it is the answer key, and
`reconagent/match.py` never opens it. The tolerances and bounds the matcher
uses are its own module-level defaults; these tests check the matcher against
the labels, they do not feed the labels back in.

The holdout split is run with exactly the same defaults chosen from the main
split. Nothing in the matcher was tuned against it.
"""

from __future__ import annotations

import itertools
import json
import random
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from reconagent import match as M
from reconagent.camt053 import parse_camt053_file
from reconagent.razorpay import parse_razorpay_settlements
from reconagent.records import CanonicalRecord

REPO = Path(__file__).resolve().parent.parent
SPLITS = {
    "main": (REPO / "data", ""),
    "holdout": (REPO / "data" / "holdout", "HOLDOUT_"),
}


class Split:
    """One dataset split, parsed once, plus the cascade's verdict on it."""

    def __init__(self, name: str) -> None:
        d, prefix = SPLITS[name]
        self.name = name
        self.truth = json.loads((d / f"{prefix}ground_truth.json").read_text())
        self.settlements = parse_razorpay_settlements(d / f"{prefix}razorpay_settlements.csv")
        self.credits = parse_camt053_file(d / f"{prefix}bank_statement.camt053.xml")
        self.by_sid = {s.record_id: s for s in self.settlements}
        self.by_bid = {b.record_id: b for b in self.credits}
        self.results = {r.bank_txn_id: r for r in M.match_all(self.credits, self.settlements)}

    def cases(self, defect_class: str | None = None) -> list[dict]:
        return [
            c for c in self.truth["cases"]
            if defect_class is None or c["defect_class"] == defect_class
        ]

    def linked_cases(self, defect_class: str | None = None) -> list[dict]:
        """Cases that name a bank credit (timing_pending cases do not)."""
        return [c for c in self.cases(defect_class) if c["expected_link"]["bank_txn_id"]]


@pytest.fixture(scope="module")
def main() -> Split:
    return Split("main")


@pytest.fixture(scope="module")
def holdout() -> Split:
    return Split("holdout")


@pytest.fixture(scope="module", params=["main", "holdout"])
def split(request) -> Split:
    return Split(request.param)


# --------------------------------------------------------------------------
# Stage 1
# --------------------------------------------------------------------------


def test_stage1_resolves_every_clean_match(split: Split) -> None:
    """The boring high-volume majority (spec section 4 Stage 1): a quoted
    reference plus an exact amount, resolved deterministically."""
    cases = split.linked_cases("clean_match")
    assert cases, "no clean_match cases in this split"
    for c in cases:
        r = split.results[c["expected_link"]["bank_txn_id"]]
        assert r.stage == M.STAGE_DETERMINISTIC, (c["case_id"], r.stage, r.reason)
        assert r.resolution == M.MATCHED, (c["case_id"], r.reason)
        assert set(r.settlement_ids) == set(c["expected_link"]["covers_settlement_ids"])
        assert r.residual_minor == c["expected_link"]["residual_minor"]
        assert r.confidence >= Decimal("0.95")


def test_stage1_emits_the_evidence_it_used(main: Split) -> None:
    """Spec section 8: which fields were compared and whether each agreed has
    to come back as data, not be reconstructible only by re-running."""
    c = main.linked_cases("clean_match")[0]
    r = main.results[c["expected_link"]["bank_txn_id"]]
    fields = {e.field: e for e in r.evidence}
    assert {"currency", "amount_minor"} <= set(fields)
    assert fields["amount_minor"].agreed
    assert fields["currency"].agreed
    # ...and at least one reference field, which is what made it Stage 1.
    assert set(fields) & set(M._REFERENCE_FIELDS)
    assert all(isinstance(e.agreed, bool) for e in r.evidence)


def test_stage1_reference_match_is_whole_token_not_substring() -> None:
    """A narration that merely *contains* the reference's characters inside a
    longer token is not a match. Substring luck is the cheap way to
    manufacture a false match out of a reference matcher."""
    s = CanonicalRecord(
        source="razorpay_settlement", record_id="setl_A", counterparty_name="",
        narration="", amount_minor=500_000, currency="INR",
        settled_at=date(2026, 8, 10), utr="ABCDEF123456",
    )
    embedded = CanonicalRecord(
        source="bank_credit", record_id="BNK1", counterparty_name="",
        narration="NEFT CR-XXABCDEF123456YY-PAYMENT", amount_minor=500_000,
        currency="INR", value_date=date(2026, 8, 11),
    )
    assert M.match_deterministic(embedded, [s]) is None

    quoted = replace(embedded, narration="NEFT CR-RATN0000088-ABCDEF123456-INV-2026-M1")
    r = M.match_deterministic(quoted, [s])
    assert r is not None and r.resolution == M.MATCHED and r.settlement_ids == ("setl_A",)


def test_stage1_rejects_a_reference_that_is_not_discriminating() -> None:
    """A value carried by two settlements identifies neither."""
    a = CanonicalRecord(
        source="razorpay_settlement", record_id="setl_A", counterparty_name="",
        narration="", amount_minor=100, currency="INR", order_id="SHARED-REF-01",
    )
    b = replace(a, record_id="setl_B", amount_minor=200)
    credit = CanonicalRecord(
        source="bank_credit", record_id="BNK1", counterparty_name="",
        narration="CR SHARED-REF-01", amount_minor=100, currency="INR",
    )
    assert M.match_deterministic(credit, [a, b]) is None


# --------------------------------------------------------------------------
# Partial payments -- reported as PARTIAL, not mis-resolved and not dropped
# --------------------------------------------------------------------------


def test_partial_payments_are_reported_as_partial(split: Split) -> None:
    for c in split.linked_cases("partial_payment") + split.linked_cases("edpms_open"):
        r = split.results[c["expected_link"]["bank_txn_id"]]
        assert r.resolution == M.PARTIAL, (c["case_id"], r.resolution, r.reason)
        assert set(r.settlement_ids) == set(c["expected_link"]["covers_settlement_ids"])
        assert r.residual_minor == c["expected_link"]["residual_minor"] > 0
        # Identity is certain, coverage is not: below a full match, well above
        # a miss, so the abstention gate can separate the two.
        assert Decimal("0.5") < r.confidence < Decimal("0.95")


def test_a_credit_larger_than_its_referenced_settlement_is_not_a_partial() -> None:
    """Under-coverage is a partial payment. Over-coverage is something else in
    the credit, and this unit refuses rather than guessing."""
    s = CanonicalRecord(
        source="razorpay_settlement", record_id="setl_A", counterparty_name="",
        narration="", amount_minor=500_000, currency="INR", utr="ABCDEF123456",
    )
    credit = CanonicalRecord(
        source="bank_credit", record_id="BNK1", counterparty_name="",
        narration="CR ABCDEF123456", amount_minor=900_000, currency="INR",
    )
    r = M.match_deterministic(credit, [s])
    assert r is not None
    assert r.resolution == M.UNMATCHED
    assert r.settlement_ids == ("setl_A",)  # candidate still attached as evidence
    assert r.residual_minor == -400_000


# --------------------------------------------------------------------------
# Stage 2 earns its place
# --------------------------------------------------------------------------


def _naive_single_record_match(
    credit: CanonicalRecord, settlements, tolerance: int = M.AMOUNT_TOLERANCE_MINOR
) -> str | None:
    """The two-pass design spec section 4 Stage 2 says is not enough: for each
    credit, look for ONE settlement whose amount matches."""
    for s in settlements:
        if s.currency == credit.currency and abs(s.amount_minor - credit.amount_minor) <= tolerance:
            return s.record_id
    return None


def test_stage2_earns_its_place_naive_single_matcher_fails_on_every_bundle(
    split: Split,
) -> None:
    """The headline claim of Stage 2. A single-record matcher cannot resolve a
    swept credit at all -- and Stage 2 does, on the main set, in full."""
    bundles = split.linked_cases("subset_sum_bundle")
    assert bundles, "no subset_sum_bundle cases in this split"

    for c in bundles:
        credit = split.by_bid[c["expected_link"]["bank_txn_id"]]
        expected = set(c["expected_link"]["covers_settlement_ids"])
        assert len(expected) > 1, c["case_id"]
        naive = _naive_single_record_match(credit, split.settlements)
        # Either the naive matcher finds nothing, or it finds a single
        # settlement that is not the answer. It can never be right: the
        # answer is a set of two or more.
        assert naive is None or {naive} != expected, (c["case_id"], naive)

    resolved = {
        c["case_id"]
        for c in bundles
        if set(split.results[c["expected_link"]["bank_txn_id"]].settlement_ids)
        == set(c["expected_link"]["covers_settlement_ids"])
        and split.results[c["expected_link"]["bank_txn_id"]].resolution == M.MATCHED
    }
    if split.name == "main":
        assert len(resolved) == len(bundles), sorted(
            {c["case_id"] for c in bundles} - resolved
        )
    else:
        # Holdout is adversarial and was not tuned against; see
        # test_holdout_posts_no_false_match for the bound that actually holds.
        assert resolved


def test_stage2_reports_stage_residual_confidence_and_search_work(main: Split) -> None:
    for c in main.linked_cases("subset_sum_bundle"):
        r = main.results[c["expected_link"]["bank_txn_id"]]
        assert r.stage == M.STAGE_SUBSET_SUM
        assert r.residual_minor == 0
        assert Decimal("0") < r.confidence <= Decimal("0.95")
        assert r.pool_size > len(r.settlement_ids)
        assert r.subsets_examined > 0
        assert not r.truncated
        assert {e.field for e in r.evidence} >= {
            "currency", "subset_cardinality", "amount_minor", "runner_up_residual_minor"
        }


# --------------------------------------------------------------------------
# The decoy: min-residual is load-bearing, not decorative
# --------------------------------------------------------------------------


def _first_fit(credit: CanonicalRecord, pool, tolerance: int, max_cardinality: int):
    """A deliberately first-fit solver: same pool, same tolerance, same
    cardinality bound -- but it returns the first admissible subset it
    enumerates instead of the minimum-|residual| one. This exists to show the
    min-residual rule is what avoids the decoy."""
    amounts = [(s.record_id, s.amount_minor) for s in pool]
    for k in range(1, max_cardinality + 1):
        for combo in itertools.combinations(amounts, k):
            total = sum(a for _, a in combo)
            if abs(total - credit.amount_minor) <= tolerance:
                return tuple(sorted(i for i, _ in combo))
    return None


def test_stage2_picks_the_labelled_subset_and_never_the_decoy(split: Split) -> None:
    """Every bundle ships a decoy subset of the same pool summing to within a
    few minor units of the credit. Solved in isolation -- the true members and
    the decoy members all open -- the solver must land on the labelled subset,
    because it is the one with residual zero."""
    beaten = excluded = 0
    for c in split.linked_cases("subset_sum_bundle"):
        credit = split.by_bid[c["expected_link"]["bank_txn_id"]]
        expected = tuple(sorted(c["expected_link"]["covers_settlement_ids"]))
        decoy = tuple(sorted(c["details"]["decoy_settlement_ids"]))
        pool = [split.by_sid[s] for s in c["settlement_ids"]]

        # pool_window_days is deliberately wide open here. Some decoys have a
        # member settled after the credit landed, so the timing rule alone
        # would exclude them -- that is a real defence, but it would make this
        # test prove nothing about min-residual. Force the decoy into the
        # search space and make the residual comparison do the work.
        r = M.match_subset_sum(credit, pool, pool_window_days=3650)
        assert r.resolution == M.MATCHED, (c["case_id"], r.resolution, r.reason)
        assert r.settlement_ids == expected, (c["case_id"], r.settlement_ids)
        assert r.settlement_ids != decoy
        assert r.residual_minor == 0
        if r.pool_size == len(c["settlement_ids"]):
            # The whole decoy reached the search space, so it was enumerated
            # and beaten on residual -- not filtered out by luck.
            beaten += 1
            assert r.rival_residual_minor is not None, c["case_id"]
            assert 0 < r.rival_residual_minor <= M.AMOUNT_TOLERANCE_MINOR
            assert tuple(sorted(r.rival_settlement_ids[0])) == decoy, c["case_id"]
        else:
            # A decoy member had not settled when the credit landed, so the
            # timing rule removed it before any combinatorics ran. Also a
            # correct rejection, just a different one.
            excluded += 1
    assert beaten > excluded, (beaten, excluded)


def test_first_fit_variant_does_pick_decoys(main: Split) -> None:
    """The control for the test above. Given the same pool and tolerance, a
    first-fit solver posts wrong subsets -- so min-residual is doing real
    work rather than decorating a search that would have been right anyway."""
    wrong = 0
    for c in main.linked_cases("subset_sum_bundle"):
        credit = main.by_bid[c["expected_link"]["bank_txn_id"]]
        expected = tuple(sorted(c["expected_link"]["covers_settlement_ids"]))
        pool = [main.by_sid[s] for s in c["settlement_ids"]]
        got = _first_fit(credit, pool, M.AMOUNT_TOLERANCE_MINOR, M.MAX_CARDINALITY)
        assert got is not None, c["case_id"]
        if got != expected:
            wrong += 1
    assert wrong > 0, "first-fit got every bundle right; the decoys are not biting"


def test_a_genuine_tie_abstains_instead_of_guessing() -> None:
    """Two distinct subsets landing on the same absolute residual give the
    arithmetic no reason to prefer either, so the answer is AMBIGUOUS with
    both attached -- never a coin flip recorded as a match."""
    pool = [
        CanonicalRecord(
            source="razorpay_settlement", record_id=f"setl_{i}", counterparty_name="",
            narration="", amount_minor=amt, currency="INR", settled_at=date(2026, 8, 10),
        )
        for i, amt in enumerate([300, 700, 400, 600])
    ]
    credit = CanonicalRecord(
        source="bank_credit", record_id="BNK1", counterparty_name="", narration="",
        amount_minor=1000, currency="INR", value_date=date(2026, 8, 11),
    )
    r = M.match_subset_sum(credit, pool, amount_tolerance_minor=0)
    assert r.resolution == M.AMBIGUOUS
    assert len(r.rival_settlement_ids) >= 2
    assert r.confidence < Decimal("0.5")


# --------------------------------------------------------------------------
# Bounds
# --------------------------------------------------------------------------


def test_a_pathological_pool_truncates_and_says_so() -> None:
    """A pool engineered so that pruning cannot help: many equal amounts, an
    unreachable target. Without a bound this is C(n, k) forever. The contract
    is that it returns, and that the answer is labelled truncated rather than
    passed off as a clean miss."""
    pool = [
        CanonicalRecord(
            source="razorpay_settlement", record_id=f"setl_{i:03d}", counterparty_name="",
            narration="", amount_minor=1_000_000 + i, currency="INR",
            settled_at=date(2026, 8, 10),
        )
        for i in range(200)
    ]
    credit = CanonicalRecord(
        source="bank_credit", record_id="BNK1", counterparty_name="", narration="",
        amount_minor=7_500_000, currency="INR", value_date=date(2026, 8, 11),
    )
    r = M.match_subset_sum(credit, pool, node_budget=50_000, max_cardinality=8)
    assert r.truncated
    assert r.subsets_examined <= 50_001
    assert r.pool_size <= M.MAX_POOL  # pool cap bit as well
    assert r.resolution in (M.MATCHED, M.AMBIGUOUS, M.UNMATCHED)
    if r.resolution == M.UNMATCHED:
        assert "truncated" in r.reason


def test_dfs_cardinality_prune_is_not_a_per_node_resum(monkeypatch) -> None:
    """Regression test for a throughput cliff traced to `_enumerate`: the
    cardinality prune used to compute `sum(amounts[j + 1 : j + slots])` --
    a fresh slice-and-sum -- on every DFS node. That is O(slots) work per
    node, and once a pool is dense enough that the search runs into tens of
    thousands of nodes (which happens well within MAX_POOL, see the pool
    below), that per-node resum dominated wall time. The fix hoists a
    prefix-sum array out of the node loop so the same prune is an O(1)
    lookup. Assert the mechanism, not just the speed: builtin `sum()` calls
    must stay tied to the pool size, not to how many nodes get visited."""
    pool = [
        CanonicalRecord(
            source="razorpay_settlement", record_id=f"setl_{i:03d}", counterparty_name="",
            narration="", amount_minor=100_000 + i * 37, currency="INR",
            settled_at=date(2026, 8, 10),
        )
        for i in range(32)
    ]
    credit = CanonicalRecord(
        source="bank_credit", record_id="BNK1", counterparty_name="", narration="",
        amount_minor=500_555, currency="INR", value_date=date(2026, 8, 11),
    )

    sum_calls = []
    real_sum = sum

    def counting_sum(*a, **kw):
        sum_calls.append(1)
        return real_sum(*a, **kw)

    monkeypatch.setattr("builtins.sum", counting_sum)
    r = M.match_subset_sum(credit, pool, max_cardinality=8)

    # The search legitimately visits tens of thousands of nodes on a pool
    # this dense -- that cost is real and expected, not what this test
    # objects to.
    assert r.subsets_examined > 10_000
    # sum() must not be called once per node visited (the old O(slots)
    # per-node resum); a handful of calls (e.g. totalling the winning
    # subset's net once, after the search) is all that is expected.
    assert len(sum_calls) < 50


def test_the_pool_cap_is_reported_as_truncation() -> None:
    pool = [
        CanonicalRecord(
            source="razorpay_settlement", record_id=f"setl_{i:03d}", counterparty_name="",
            narration="", amount_minor=1_000 + i, currency="INR",
            settled_at=date(2026, 8, 10),
        )
        for i in range(M.MAX_POOL + 20)
    ]
    credit = CanonicalRecord(
        source="bank_credit", record_id="BNK1", counterparty_name="", narration="",
        amount_minor=9_999_999, currency="INR", value_date=date(2026, 8, 11),
    )
    r = M.match_subset_sum(credit, pool)
    assert r.pool_size == M.MAX_POOL
    assert r.truncated


def test_pooling_excludes_wrong_currency_and_out_of_window_settlements() -> None:
    """Candidate pooling is part of the design: currency, timing, magnitude.
    A settlement outside any of them must not reach the combinatorics."""
    credit = CanonicalRecord(
        source="bank_credit", record_id="BNK1", counterparty_name="", narration="",
        amount_minor=1000, currency="INR", value_date=date(2026, 8, 20),
    )

    def one(**kw) -> CanonicalRecord:
        base = dict(
            source="razorpay_settlement", record_id="setl_A", counterparty_name="",
            narration="", amount_minor=1000, currency="INR", settled_at=date(2026, 8, 19),
        )
        return CanonicalRecord(**{**base, **kw})

    assert M.match_subset_sum(credit, [one()]).resolution == M.MATCHED
    assert M.match_subset_sum(credit, [one(currency="USD")]).resolution == M.UNMATCHED
    # settled after the credit landed
    assert M.match_subset_sum(credit, [one(settled_at=date(2026, 8, 25))]).resolution == M.UNMATCHED
    # settled long before the window opens
    assert M.match_subset_sum(credit, [one(settled_at=date(2026, 5, 1))]).resolution == M.UNMATCHED
    # a member larger than the whole credit cannot be part of it
    big = M.match_subset_sum(credit, [one(amount_minor=5000)])
    assert big.resolution == M.UNMATCHED and big.pool_size == 0


# --------------------------------------------------------------------------
# Not matching things that should not match
# --------------------------------------------------------------------------


def _still_open(split: Split) -> list[CanonicalRecord]:
    claimed = {
        sid
        for r in split.results.values()
        if r.resolution in (M.MATCHED, M.PARTIAL)
        for sid in r.settlement_ids
    }
    return [s for s in split.settlements if s.record_id not in claimed]


def test_no_false_match_on_an_unrelated_credit(split: Split) -> None:
    """A credit quoting no known reference, for an amount no subset of the
    still-open settlements can reach, must come back UNMATCHED -- with the
    search work attached, not a shrug."""
    credit = CanonicalRecord(
        source="bank_credit", record_id="BNK-UNRELATED", counterparty_name="ACME",
        narration="NEFT CR-RATN0000088-NOTAREALUTR9999-MISC",
        # Larger than every settlement net in either split put together, so no
        # subset can reach it however dense the pool is.
        amount_minor=99_999_999_937, currency="INR", value_date=date(2026, 8, 20),
    )
    assert M.match_deterministic(credit, split.settlements) is None
    r = M.match_subset_sum(credit, _still_open(split))
    assert r.resolution == M.UNMATCHED
    assert r.settlement_ids == ()
    assert r.confidence == Decimal("0")
    assert r.stage == M.STAGE_SUBSET_SUM


def test_spurious_subset_risk_on_arbitrary_amounts_is_measured_not_assumed(
    main: Split,
) -> None:
    """The honest ceiling on Stage 2. Subset sums are dense: given a big
    enough pool, *some* subset explains almost any amount. This probes 40
    amounts that correspond to no real sweep and asserts the two things that
    keep that from becoming a false-match rate --

      1. against the settlements still open after Stage 1 (the pool Stage 2
         actually gets), the overwhelming majority come back UNMATCHED or
         AMBIGUOUS: the pooling rules are load-bearing, not an optimisation;
      2. whatever does slip through scores low, so Stage 5 has something to
         threshold on.

    If this test starts failing, the answer is not to loosen it -- it is that
    Stage 2 has become willing to explain anything.
    """
    rng = random.Random(7)
    probes = [rng.randrange(2_000_000, 40_000_000) for _ in range(40)]
    open_settlements = _still_open(main)
    spurious = []
    for amount in probes:
        credit = CanonicalRecord(
            source="bank_credit", record_id="PROBE", counterparty_name="",
            narration="", amount_minor=amount, currency="INR",
            value_date=date(2026, 8, 20),
        )
        r = M.match_subset_sum(credit, open_settlements)
        if r.resolution == M.MATCHED:
            spurious.append(r)
    assert len(spurious) <= len(probes) // 8, [r.credit_amount_minor for r in spurious]
    for r in spurious:
        assert r.confidence < Decimal("0.75"), (r.credit_amount_minor, r.confidence)


def test_settlement_is_claimed_by_at_most_one_credit(split: Split) -> None:
    """Cross-credit bookkeeping: nothing gets swept twice."""
    claimed: dict[str, str] = {}
    for r in split.results.values():
        if r.resolution not in (M.MATCHED, M.PARTIAL):
            continue
        for sid in r.settlement_ids:
            assert sid not in claimed, (sid, claimed.get(sid), r.bank_txn_id)
            claimed[sid] = r.bank_txn_id


# --------------------------------------------------------------------------
# Split-level headline metrics (spec section 9)
# --------------------------------------------------------------------------


def _scoreboard(split: Split) -> dict[str, int]:
    out = {"correct": 0, "false_match": 0, "abstained": 0}
    for c in split.linked_cases():
        r = split.results[c["expected_link"]["bank_txn_id"]]
        if r.resolution in (M.MATCHED, M.PARTIAL):
            same = set(r.settlement_ids) == set(c["expected_link"]["covers_settlement_ids"])
            if same and r.resolution == c["expected_link_resolution"]:
                out["correct"] += 1
            else:
                out["false_match"] += 1
        else:
            out["abstained"] += 1
    return out


def test_main_set_has_no_false_match_and_no_false_clear(main: Split) -> None:
    score = _scoreboard(main)
    assert score["false_match"] == 0
    assert score["abstained"] == 0
    assert score["correct"] == len(main.linked_cases())


def test_holdout_posts_no_false_match(holdout: Split) -> None:
    """Run with the defaults chosen from the main split -- nothing here was
    tuned against the holdout. The bound that must hold is the one spec
    section 9 leads with: a wrong match is worse than an honest miss. Some
    holdout bundles do abstain; that is the intended failure direction."""
    score = _scoreboard(holdout)
    assert score["false_match"] == 0, score
    assert score["correct"] >= len(holdout.linked_cases()) - 5, score


def test_timing_pending_settlements_are_simply_never_claimed(split: Split) -> None:
    """Spec section 5: a settlement still inside the T+2..T+7 window has no
    bank credit yet. A credit-driven matcher must not invent one for it. The
    TIMING_PENDING state itself is a later unit's call."""
    for c in split.cases("timing_pending"):
        assert not c["expected_link"]["bank_txn_id"]
        pending = set(c["expected_link"]["covers_settlement_ids"])
        for r in split.results.values():
            if r.resolution in (M.MATCHED, M.PARTIAL):
                assert not (set(r.settlement_ids) & pending), (c["case_id"], r.bank_txn_id)


def test_no_float_reaches_a_money_field(split: Split) -> None:
    for r in split.results.values():
        for v in (r.credit_amount_minor, r.settlement_net_sum_minor, r.residual_minor):
            assert isinstance(v, int) and not isinstance(v, bool)
        assert isinstance(r.confidence, Decimal)
