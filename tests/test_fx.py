"""Tests for the cross-border intelligence layer, run against ground truth on
both the main split and the holdout.

Ground truth is read HERE and only here -- `reconagent/fx.py` and
`reconagent/edpms.py` never open it. The tolerance band the validator uses is
its own; `test_tolerance_default_is_reproducible_from_main_labels` re-derives
it from the main-set labels and asserts it lands on the published default.

The holdout is scored, never tuned on: the band was fixed from the main set
before the holdout was run, and the holdout results are asserted at whatever
they came out to (see `test_holdout_fx_verdicts_reported_as_is`).
"""

import json
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pytest

from reconagent.edpms import (
    AGING,
    OPEN_EDPMS_LINKAGE,
    load_export_receipts,
    open_edpms_exceptions,
)
from reconagent.fx import (
    BENIGN_FX_DRIFT,
    DATA_ENTRY_ERROR,
    DEFAULT_FX_TOLERANCE_BPS,
    FEE_MISMATCH,
    FLAGGED_FX_DRIFT,
    NO_REFERENCE_RATE,
    NO_VARIANCE,
    REFUND_AMOUNT_BREAK,
    REFUND_FX_ASYMMETRY,
    TIMING_OVERDUE,
    TIMING_PENDING,
    UNRESOLVED,
    ReferenceRates,
    check_fx_rate,
    check_settlement_row_fx,
    check_settlement_timing,
    decompose_variance,
    load_reference_rates,
    reconcile_refund_fx,
)
from reconagent.razorpay import parse_razorpay_settlements
from reconagent.records import CanonicalRecord, SettlementRow

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
HOLDOUT = DATA / "holdout"

MAIN = {
    "rates": DATA / "fx_reference_rates.csv",
    "settlements": DATA / "razorpay_settlements.csv",
    "ledger": DATA / "invoice_ledger.csv",
    "truth": DATA / "ground_truth.json",
}
HOLD = {
    "rates": HOLDOUT / "HOLDOUT_fx_reference_rates.csv",
    "settlements": HOLDOUT / "HOLDOUT_razorpay_settlements.csv",
    "ledger": HOLDOUT / "HOLDOUT_invoice_ledger.csv",
    "truth": HOLDOUT / "HOLDOUT_ground_truth.json",
}
SPLITS = {"main": MAIN, "holdout": HOLD}


def load(split):
    paths = SPLITS[split]
    truth = json.loads(paths["truth"].read_text())
    return {
        "rates": load_reference_rates(paths["rates"]),
        "records": {r.record_id: r for r in parse_razorpay_settlements(paths["settlements"])},
        "ledger": paths["ledger"],
        "truth": truth,
        "as_of": date.fromisoformat(truth["conventions"]["statement_as_of"]),
        "cases": truth["cases"],
    }


@pytest.fixture(scope="module")
def main():
    return load("main")


@pytest.fixture(scope="module")
def holdout():
    return load("holdout")


def cases_of(bundle, defect_class):
    return [c for c in bundle["cases"] if c["defect_class"] == defect_class]


# ---------------------------------------------------------------------------
# Reference rates
# ---------------------------------------------------------------------------


def test_reference_rates_match_the_ground_truth_rate_table(main):
    """The parser is the only path to FBIL rates, so it has to agree with the
    rate table the generator published, exactly and as Decimals."""
    published = main["truth"]["fx_reference_rates"]
    assert published
    for row in published:
        got = main["rates"].rate(date.fromisoformat(row["value_date"]), row["currency_pair"])
        assert got == Decimal(row["reference_rate"])
        assert main["rates"].source(
            date.fromisoformat(row["value_date"]), row["currency_pair"]
        ) == row["source"]


def test_missing_value_date_refuses_rather_than_carrying_forward(main):
    """A date with no published rate returns None, and the check that depends
    on it says NO_REFERENCE_RATE instead of quietly reusing an earlier day."""
    gap = date(2020, 1, 1)
    assert main["rates"].rate(gap, "GBP/INR") is None
    check = check_fx_rate(
        main["rates"], value_date=gap, currency_pair="GBP/INR", applied_rate=Decimal("111.5310")
    )
    assert check.verdict == NO_REFERENCE_RATE
    assert check.within_tolerance is None
    assert check.deviation_bps is None


def test_deviation_bps_is_signed_and_two_dp(main):
    """13.18 bps in the answer key must come out of our own arithmetic, not be
    read from it -- same value date, same pair, same applied rate."""
    check = check_fx_rate(
        main["rates"],
        value_date=date(2026, 8, 5),
        currency_pair="GBP/INR",
        applied_rate=Decimal("111.6780"),
    )
    assert check.reference_rate == Decimal("111.5310")
    assert check.deviation_bps == Decimal("13.18")
    below = check_fx_rate(
        main["rates"],
        value_date=date(2026, 8, 9),
        currency_pair="EUR/INR",
        applied_rate=Decimal("92.0546"),
    )
    assert below.deviation_bps == Decimal("-333.14")


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


def _main_split_deviations(bundle):
    """Every international settlement leg in the split, paired with whether the
    labels call it a flagged drift. Deviations are computed by our own
    validator, not read out of `details.deviation_bps`."""
    flagged_ids = {s for c in cases_of(bundle, "fx_drift_flagged") for s in c["settlement_ids"]}
    out = []
    for record in bundle["records"].values():
        for row in record.rows:
            check = check_settlement_row_fx(bundle["rates"], row)
            if check is None or check.deviation_bps is None:
                continue
            out.append((record.record_id, check.deviation_bps, record.record_id in flagged_ids))
    return out


def test_tolerance_default_is_reproducible_from_main_labels(main):
    """Re-derive the band from the main-set labels and assert it is the module
    default. Rule, fixed in advance: three sigma of the legitimate population,
    sigma taken about zero (the reference rate is the centre; the sample mean
    of -3.20 bps on n=25 is not a real bias), then rounded DOWN to the nearest
    5 bps because spec §9 leads with false-clear rate and the tighter of two
    round numbers is the one that does not clear a bad rate."""
    devs = _main_split_deviations(main)
    benign = [d for _, d, flagged in devs if not flagged]
    flagged = [abs(d) for _, d, is_flagged in devs if is_flagged]

    assert len(benign) == 25, benign
    assert len(flagged) == 5, flagged

    sigma = (sum(d * d for d in benign) / len(benign)).sqrt()
    assert sigma.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) == Decimal("22.96")

    three_sigma = (3 * sigma).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    assert three_sigma == Decimal("68.87")

    band = (three_sigma // 5) * 5
    assert band == DEFAULT_FX_TOLERANCE_BPS == Decimal("65")

    # And it separates the two labelled classes on the set it was derived from,
    # with room on both sides rather than sitting on a boundary.
    assert max(abs(d) for d in benign) == Decimal("43.62") < band
    assert band < min(flagged) == Decimal("214.45")


def test_band_is_a_parameter_not_a_constant(main):
    """The band is owned by the caller. Same rate, three bands, three answers."""
    record = main["records"]["setl_0fpKGvBwx0Nj4m"]  # 13.18 bps
    row = record.rows[0]
    assert check_settlement_row_fx(main["rates"], row).verdict == BENIGN_FX_DRIFT
    tight = check_settlement_row_fx(main["rates"], row, tolerance_bps=Decimal("10"))
    assert tight.verdict == FLAGGED_FX_DRIFT
    assert tight.deviation_bps == Decimal("13.18")
    loose = check_settlement_row_fx(main["rates"], row, tolerance_bps=Decimal("500"))
    assert loose.verdict == BENIGN_FX_DRIFT


# ---------------------------------------------------------------------------
# FX drift classification, both splits
# ---------------------------------------------------------------------------


def fx_verdicts(bundle, defect_class):
    got = {}
    for case in cases_of(bundle, defect_class):
        sid = case["settlement_ids"][0]
        decomposition = decompose_variance(bundle["records"][sid], bundle["rates"])
        got[case["case_id"]] = (case, decomposition)
    return got


@pytest.mark.parametrize("split", ["main", "holdout"])
@pytest.mark.parametrize(
    "defect_class,expected", [("fx_drift_benign", BENIGN_FX_DRIFT), ("fx_drift_flagged", FLAGGED_FX_DRIFT)]
)
def test_fx_drift_cases_classified(split, defect_class, expected, main, holdout):
    bundle = main if split == "main" else holdout
    results = fx_verdicts(bundle, defect_class)
    assert results
    wrong = []
    for case_id, (case, dec) in results.items():
        # Our own deviation must reproduce the labelled one exactly -- if it
        # does not, the classification agreeing is luck.
        assert dec.fx is not None
        assert dec.fx.deviation_bps == Decimal(case["details"]["deviation_bps"]), case_id
        assert dec.fx.reference_rate == Decimal(case["details"]["reference_rate"]), case_id
        assert dec.residual_minor == 0, case_id  # the money ties out; only the rate is in question
        if dec.attribution != expected:
            wrong.append((case_id, dec.fx.deviation_bps, dec.attribution))
    assert not wrong, f"{split}/{defect_class} misclassified: {wrong}"


def test_holdout_fx_verdicts_reported_as_is(holdout):
    """The holdout deliberately hugs the boundary: its benign cases run to
    ±40.87 bps and its flagged ones start at 67.84, against a band of 65 fixed
    on the main set alone. This asserts the actual outcome so a regression in
    either direction shows up as a diff rather than as a quietly moved band."""
    benign = fx_verdicts(holdout, "fx_drift_benign")
    flagged = fx_verdicts(holdout, "fx_drift_flagged")
    assert len(benign) == 8 and len(flagged) == 3

    benign_devs = sorted(abs(d.fx.deviation_bps) for _, d in benign.values())
    flagged_devs = sorted(abs(d.fx.deviation_bps) for _, d in flagged.values())
    assert benign_devs[-1] == Decimal("40.87")
    assert flagged_devs[0] == Decimal("67.84")

    # 8/8 benign and 3/3 flagged, with the closest call 24.13 bps under the
    # band on one side and 2.84 bps over it on the other.
    assert all(d.attribution == BENIGN_FX_DRIFT for _, d in benign.values())
    assert all(d.attribution == FLAGGED_FX_DRIFT for _, d in flagged.values())
    assert benign_devs[-1] < DEFAULT_FX_TOLERANCE_BPS < flagged_devs[0]


@pytest.mark.parametrize("split", ["main", "holdout"])
def test_no_false_clears_or_false_flags_across_every_labelled_fx_case(split, main, holdout):
    """Headline metrics, spec §9. Both are asserted at zero on both splits."""
    bundle = main if split == "main" else holdout
    false_clear = false_flag = 0
    for defect_class, expected in (
        ("fx_drift_benign", BENIGN_FX_DRIFT),
        ("fx_drift_flagged", FLAGGED_FX_DRIFT),
    ):
        for _, (case, dec) in fx_verdicts(bundle, defect_class).items():
            got_within = dec.fx.within_tolerance
            want_within = case["details"]["expected_within_tolerance"]
            if want_within is False and got_within is True:
                false_clear += 1
            if want_within is True and got_within is False:
                false_flag += 1
            assert (dec.attribution == expected) == (got_within == want_within)
    assert (false_clear, false_flag) == (0, 0)


# ---------------------------------------------------------------------------
# Variance decomposition
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("split", ["main", "holdout"])
def test_identity_holds_for_every_settlement_in_the_split(split, main, holdout):
    """`net = gross - MDR - GST - FX_spread - refunds` must close to zero on
    every record in the file, except the settlements whose ground-truth
    defect class is itself a deliberate break in that identity
    (fee_mismatch, data_entry_error) -- exactly the two categories
    `test_fee_mismatch_and_data_entry_error_cases_are_labelled_correctly`
    below exists to cover. If a non-exempt record fails this, every
    attribution downstream is built on a broken identity."""
    bundle = main if split == "main" else holdout
    exempt = {
        sid
        for c in bundle["cases"]
        if c["defect_class"] in ("fee_mismatch", "data_entry_error")
        for sid in c["settlement_ids"]
    }
    for record in bundle["records"].values():
        if record.record_id in exempt:
            continue
        dec = decompose_variance(record, bundle["rates"])
        assert dec.residual_minor == 0, (record.record_id, dec.signature)
        assert dec.attribution in (NO_VARIANCE, BENIGN_FX_DRIFT, FLAGGED_FX_DRIFT), record.record_id
        assert dec.expected_net_minor == dec.gross_applied_minor - dec.mdr_minor - dec.gst_minor


def test_fee_mismatch_and_data_entry_error_cases_are_labelled_correctly(main):
    """The main split's one FEE_MISMATCH case and one DATA_ENTRY_ERROR case
    (the settlements `test_identity_holds_for_every_settlement_in_the_split`
    exempts above): confirm `decompose_variance` actually lands on each
    category, uniquely, with the residual ground truth records."""
    fee_case = cases_of(main, "fee_mismatch")[0]
    fee_rec = main["records"][fee_case["settlement_ids"][0]]
    fee_dec = decompose_variance(fee_rec, main["rates"])
    assert fee_dec.attribution == FEE_MISMATCH
    assert fee_dec.candidates == (FEE_MISMATCH,)
    assert fee_dec.residual_minor == fee_case["details"]["gst_residual_minor"]

    der_case = cases_of(main, "data_entry_error")[0]
    der_rec = main["records"][der_case["settlement_ids"][0]]
    der_dec = decompose_variance(der_rec, main["rates"])
    assert der_dec.attribution == DATA_ENTRY_ERROR
    assert der_dec.candidates == (DATA_ENTRY_ERROR,)
    assert der_dec.residual_minor == der_case["details"]["residual_minor"]


def test_decomposition_terms_match_the_answer_key(main):
    """Spot-check every term against a labelled case's `details`."""
    case = next(c for c in cases_of(main, "fx_drift_benign") if c["case_id"] == "MAIN-00030")
    d = case["details"]
    dec = decompose_variance(main["records"]["setl_0fpKGvBwx0Nj4m"], main["rates"])
    assert dec.foreign_currency == "GBP"
    assert dec.gross_applied_minor == d["base_amount_minor"] == 587147308
    assert dec.mdr_minor == d["fee_minor"] == 17614419
    assert dec.gst_minor == d["tax_minor"] == 3170595
    assert dec.actual_net_minor == case["expected_link"]["settlement_net_sum_minor"]
    # The FX spread is real but cancels: gross at the reference rate minus the
    # spread is gross at the applied rate, which is what was actually booked.
    assert dec.gross_reference_minor - dec.fx_spread_minor == dec.gross_applied_minor
    assert dec.fx_spread_minor != 0
    assert "13.18 bps" in dec.signature and "band +/-65 bps" in dec.signature


def test_domestic_settlement_has_no_variance_and_no_fx_leg(main):
    dec = decompose_variance(main["records"]["setl_lBfnclOpWerO10"], main["rates"])
    assert dec.fx is None
    assert dec.foreign_currency is None
    assert dec.attribution == NO_VARIANCE
    assert dec.residual_minor == 0


# ---- constructed defects ----------------------------------------------------
#
# Neither split contains a fee-mismatch or a data-entry case (the defect
# classes are clean_match, edpms_open, fx_drift_benign/flagged,
# missing_remitter, partial_payment, refund_fx_asymmetry, subset_sum_bundle,
# timing_pending), so these signatures are exercised by mutating a real record
# from the file. The base record is genuine; exactly one term is corrupted.


def mutate(record: CanonicalRecord, *, row_updates=None, amount_minor=None) -> CanonicalRecord:
    from dataclasses import replace

    rows = record.rows
    if row_updates:
        rows = (replace(rows[0], **row_updates),) + rows[1:]
    return replace(
        record,
        rows=rows,
        amount_minor=record.amount_minor if amount_minor is None else amount_minor,
    )


def test_fee_mismatch_detected_by_the_gst_signature(main):
    """GST is 18% of MDR by statute. Overstate it by 50 000 paise while the net
    actually credited stays right, and the identity no longer closes: the whole
    residual is the fee block. One term, one restatement, one cause."""
    record = main["records"]["setl_0fpKGvBwx0Nj4m"]
    broken = mutate(record, row_updates={"tax_minor": record.rows[0].tax_minor + 50_000})
    dec = decompose_variance(broken, main["rates"])
    assert dec.residual_minor == 50_000
    assert dec.attribution == FEE_MISMATCH
    assert dec.candidates == (FEE_MISMATCH,)
    assert "!= 18% of MDR" in dec.signature


def test_data_entry_error_detected_by_decimal_shift(main):
    """Net keyed in with the decimal point one place out."""
    record = main["records"]["setl_lBfnclOpWerO10"]
    broken = mutate(record, amount_minor=record.amount_minor * 10)
    dec = decompose_variance(broken, main["rates"])
    assert dec.attribution == DATA_ENTRY_ERROR
    assert "x 10^1" in dec.signature


def test_data_entry_error_detected_by_digit_transposition(main):
    """2488114 -> 2488141: two adjacent digits swapped. The difference is a
    multiple of 9 and the digits are a permutation -- the classic fingerprint,
    and the reason this is a cause and not an unexplained residual."""
    record = main["records"]["setl_lBfnclOpWerO10"]
    assert record.amount_minor == 2488114
    broken = mutate(record, amount_minor=2488141)
    dec = decompose_variance(broken, main["rates"])
    assert dec.residual_minor == 27
    assert dec.attribution == DATA_ENTRY_ERROR
    assert "transposition" in dec.signature


def test_unresolved_when_no_single_term_explains_the_residual(main):
    """A residual that is not the GST error, not a rate restatement, not a
    decimal shift and not a transposition gets no name. This is the honest
    answer, and asserting it is the point of the test."""
    record = main["records"]["setl_0fpKGvBwx0Nj4m"]
    broken = mutate(record, amount_minor=record.amount_minor - 137)
    dec = decompose_variance(broken, main["rates"])
    assert dec.residual_minor == -137
    assert dec.attribution == UNRESOLVED
    assert dec.candidates == ()
    assert "no single term closes it" in dec.signature


def test_unresolved_when_two_causes_each_explain_the_residual(main):
    """Ambiguity is not resolved by picking the first rule that fires.

    Understate GST by 27 paise. Now two stories close the gap exactly and the
    arithmetic cannot choose between them: the fee block is out by 27, *and*
    the expected net 2 488 141 is a digit transposition of the net actually
    credited, 2 488 114 (adjacent digits swapped, difference a multiple of 9).
    Both are real signatures; neither is the answer.
    """
    record = main["records"]["setl_lBfnclOpWerO10"]
    assert record.amount_minor == 2488114
    broken = mutate(record, row_updates={"tax_minor": record.rows[0].tax_minor - 27})
    dec = decompose_variance(broken, main["rates"])
    assert dec.expected_net_minor == 2488141
    assert dec.residual_minor == -27
    assert set(dec.candidates) == {FEE_MISMATCH, DATA_ENTRY_ERROR}
    assert dec.attribution == UNRESOLVED
    assert "does not single one out" in dec.signature


def test_unresolved_when_the_reference_rate_is_missing(main):
    """Money ties out, but with no published FBIL rate for the value date the
    applied rate is unvalidated -- so the finding is UNRESOLVED, not benign."""
    record = main["records"]["setl_0fpKGvBwx0Nj4m"]
    empty = ReferenceRates(rates={}, sources={})
    dec = decompose_variance(record, empty)
    assert dec.residual_minor == 0
    assert dec.fx.verdict == NO_REFERENCE_RATE
    assert dec.attribution == UNRESOLVED
    assert "no FBIL reference published" in dec.signature


def test_decomposition_rejects_a_float_at_construction():
    """The float ban is enforced by the record types this module consumes, so a
    float cannot reach the decomposition arithmetic in the first place."""
    with pytest.raises(TypeError):
        CanonicalRecord(
            source="razorpay_settlement",
            record_id="x",
            counterparty_name="",
            narration="",
            amount_minor=1.0,
            currency="INR",
        )


# ---------------------------------------------------------------------------
# Refund FX asymmetry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("split", ["main", "holdout"])
def test_refund_fx_asymmetry_against_ground_truth(split, main, holdout):
    bundle = main if split == "main" else holdout
    cases = cases_of(bundle, "refund_fx_asymmetry")
    assert cases
    for case in cases:
        d = case["details"]
        sid = case["settlement_ids"][0]
        results = reconcile_refund_fx(bundle["records"][sid], bundle["rates"])
        assert len(results) == 1
        r = results[0]
        assert r.refund_id == d["refund_id"]
        assert r.currency == d["currency"]

        # The refund is full in the original currency ...
        assert r.foreign_residual_minor == d["foreign_residual_minor"] == 0
        # ... and still does not net to zero in INR.
        assert r.inr_residual_minor == d["inr_residual_minor"] != 0

        assert r.capture.rate == Decimal(d["capture"]["rate"])
        assert r.capture.reference_rate == Decimal(d["capture"]["reference_rate"])
        assert r.capture.value_date == date.fromisoformat(d["capture"]["value_date"])
        assert r.capture.inr_minor == d["capture"]["inr_minor"]
        assert r.refund.rate == Decimal(d["refund"]["rate"])
        assert r.refund.reference_rate == Decimal(d["refund"]["reference_rate"])
        assert r.refund.value_date == date.fromisoformat(d["refund"]["value_date"])
        assert r.refund.inr_minor == d["refund"]["inr_minor"]
        assert r.refund.value_date > r.capture.value_date  # its own, later, FX event

        # Reported as expected, not as a break.
        assert r.expected_asymmetry is True
        assert r.verdict == REFUND_FX_ASYMMETRY == case["expected_exception_category"]
        assert "not a shortfall" in r.signature


def test_refund_inr_residual_is_exactly_the_two_conversion_events(main):
    """The INR residual is arithmetic, not an unexplained difference: it is the
    same foreign amount priced at two different rates."""
    r = reconcile_refund_fx(main["records"]["setl_LxDAt9A9b6LsuX"], main["rates"])[0]
    foreign = r.capture.foreign_minor
    expected = round(foreign * r.capture.rate) - round(foreign * r.refund.rate)
    assert r.inr_residual_minor == expected == -637167


def test_short_refund_is_a_break_not_asymmetry(main):
    """A refund that is *not* full in the foreign currency has a non-zero
    foreign residual, which no FX story explains. That is a real break and must
    not be excused as asymmetry."""
    from dataclasses import replace

    record = main["records"]["setl_LxDAt9A9b6LsuX"]
    rows = tuple(
        replace(row, amount_minor=row.amount_minor - 1000) if row.type == "refund" else row
        for row in record.rows
    )
    r = reconcile_refund_fx(replace(record, rows=rows), main["rates"])[0]
    assert r.foreign_residual_minor == 1000
    assert r.expected_asymmetry is False
    assert r.verdict == REFUND_AMOUNT_BREAK


def test_settlements_without_refunds_produce_no_reconciliations(main):
    assert reconcile_refund_fx(main["records"]["setl_0fpKGvBwx0Nj4m"], main["rates"]) == []


# ---------------------------------------------------------------------------
# EDPMS / shipping-bill aging
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("split", ["main", "holdout"])
def test_edpms_aging_against_ground_truth(split, main, holdout):
    bundle = main if split == "main" else holdout
    receipts = {r.invoice_id: r for r in load_export_receipts(bundle["ledger"], as_of=bundle["as_of"])}
    cases = cases_of(bundle, "edpms_open")
    assert len(cases) == 2
    for case in cases:
        d = case["details"]
        r = receipts[case["invoice_ids"][0]]
        assert r.shipping_bill_no == d["shipping_bill_no"]
        assert r.shipping_bill_date == date.fromisoformat(d["shipping_bill_date"])
        assert r.purpose_code == d["purpose_code"]
        assert r.currency == d["currency"]
        assert r.realisation_deadline == date.fromisoformat(d["realisation_deadline"])
        assert r.as_of == date.fromisoformat(d["statement_as_of"])
        assert r.invoiced_foreign_minor == d["invoiced_foreign_minor"]
        assert r.realised_foreign_minor == d["realised_foreign_minor"]
        assert r.outstanding_foreign_minor == d["outstanding_foreign_minor"]
        assert r.days_to_deadline == d["days_to_deadline"]
        assert r.overdue is d["overdue"]
        assert r.status == OPEN_EDPMS_LINKAGE == case["expected_exception_category"]


@pytest.mark.parametrize("split", ["main", "holdout"])
def test_open_edpms_exceptions_are_exactly_the_labelled_ones(split, main, holdout):
    """No over-reporting: every export invoice is aged, but only the labelled
    ones are exceptions. A freshly issued bill with months to run is AGING."""
    bundle = main if split == "main" else holdout
    receipts = load_export_receipts(bundle["ledger"], as_of=bundle["as_of"])
    labelled = {c["invoice_ids"][0] for c in cases_of(bundle, "edpms_open")}
    assert {r.invoice_id for r in open_edpms_exceptions(receipts)} == labelled
    assert len(receipts) > len(labelled)
    assert all(r.status == AGING for r in receipts if r.invoice_id not in labelled)


def test_edpms_exceptions_sorted_most_urgent_first(holdout):
    exceptions = open_edpms_exceptions(
        load_export_receipts(holdout["ledger"], as_of=holdout["as_of"])
    )
    assert [r.days_to_deadline for r in exceptions] == [2, 4]


def test_overdue_flips_when_the_as_of_date_passes_the_deadline(main):
    """Neither split contains an overdue case as of 2026-08-31 -- the nearest
    deadline is 2 days out. Aging is a function of the supplied date, so the
    overdue path is exercised by moving that date, which is exactly why it is a
    parameter and not `date.today()`."""
    invoice = "INV-2026-M00010"  # deadline 2026-09-25, 1 749 139 GBP minor outstanding

    on_time = {r.invoice_id: r for r in load_export_receipts(MAIN["ledger"], as_of=date(2026, 9, 25))}[invoice]
    assert on_time.days_to_deadline == 0
    assert on_time.overdue is False  # the deadline day itself is not yet breached

    late = {r.invoice_id: r for r in load_export_receipts(MAIN["ledger"], as_of=date(2026, 10, 15))}[invoice]
    assert late.days_to_deadline == -20
    assert late.overdue is True
    assert late.status == OPEN_EDPMS_LINKAGE
    assert late.outstanding_foreign_minor == 1749139
    assert "20 days past deadline" in late.signature


def test_unrealised_bill_becomes_an_exception_once_overdue(main):
    """An AGING bill that nobody realises turns into an exception on its own
    the day the deadline passes -- no partial realisation required."""
    invoice = "INV-2026-M00007"  # ISSUED, nothing realised, deadline 2027-05-12
    before = {r.invoice_id: r for r in load_export_receipts(MAIN["ledger"], as_of=date(2027, 5, 12))}[invoice]
    after = {r.invoice_id: r for r in load_export_receipts(MAIN["ledger"], as_of=date(2027, 5, 13))}[invoice]
    assert before.status == AGING and before.overdue is False
    assert after.status == OPEN_EDPMS_LINKAGE and after.overdue is True


def test_domestic_invoices_carry_no_edpms_obligation(main):
    """Rows without a shipping bill are not export receipts and are not aged."""
    import csv

    with open(MAIN["ledger"], newline="", encoding="utf-8") as f:
        expected = sum(1 for r in csv.DictReader(f) if r["shipping_bill_no"].strip())
    assert len(load_export_receipts(MAIN["ledger"], as_of=main["as_of"])) == expected
    assert all(r.shipping_bill_no for r in load_export_receipts(MAIN["ledger"], as_of=main["as_of"]))


def test_realisation_deadline_is_nine_months_from_the_shipping_bill(main):
    """FEMA's nine-month rule, checked against the file rather than assumed."""
    for r in load_export_receipts(MAIN["ledger"], as_of=main["as_of"]):
        assert (r.realisation_deadline - r.shipping_bill_date).days == 270


# ---------------------------------------------------------------------------
# Nostro/vostro timing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("split", ["main", "holdout"])
def test_timing_pending_is_held_not_flagged(split, main, holdout):
    bundle = main if split == "main" else holdout
    cases = cases_of(bundle, "timing_pending")
    assert cases
    for case in cases:
        d = case["details"]
        record = bundle["records"][case["settlement_ids"][0]]
        check = check_settlement_timing(
            record, as_of=bundle["as_of"], window_days=tuple(d["expected_window_days"])
        )
        assert check.settled_at == date.fromisoformat(d["settled_at"])
        assert check.as_of == date.fromisoformat(d["statement_as_of"])
        assert check.days_outstanding == d["days_outstanding"]
        assert check.inside_expected_window is d["inside_expected_window"] is True
        assert check.verdict == TIMING_PENDING == case["expected_exception_category"]
        assert "not a break" in check.signature
        # And these are genuinely unmatched -- the point is that being unmatched
        # is not by itself an exception inside the window.
        assert case["expected_link_resolution"] == "UNMATCHED"


def test_timing_window_bounds_are_parameters(main):
    record = main["records"]["setl_K4eUwcny1zWJOh"]  # settled 2026-08-28
    as_of = main["as_of"]  # 2026-08-31 -> 3 days
    assert check_settlement_timing(record, as_of=as_of).days_outstanding == 3
    assert check_settlement_timing(record, as_of=as_of).verdict == TIMING_PENDING
    tight = check_settlement_timing(record, as_of=as_of, window_days=(0, 2))
    assert tight.verdict == TIMING_OVERDUE
    assert tight.inside_expected_window is False


def test_past_the_window_is_a_genuine_break(main):
    record = main["records"]["setl_K4eUwcny1zWJOh"]
    late = check_settlement_timing(record, as_of=date(2026, 9, 10))
    assert late.days_outstanding == 13
    assert late.verdict == TIMING_OVERDUE
    assert "genuine break" in late.signature


def test_early_settlement_is_not_a_break_either(main):
    """Below the lower bound the window is reported as not-yet-entered, but the
    verdict is still pending -- nothing is a break before the window closes."""
    record = main["records"]["setl_K4eUwcny1zWJOh"]
    early = check_settlement_timing(record, as_of=date(2026, 8, 29))
    assert early.days_outstanding == 1
    assert early.inside_expected_window is False
    assert early.verdict == TIMING_PENDING


def test_settlement_with_no_settled_at_cannot_be_aged():
    record = CanonicalRecord(
        source="razorpay_settlement",
        record_id="setl_x",
        counterparty_name="",
        narration="",
        amount_minor=100,
        currency="INR",
    )
    check = check_settlement_timing(record, as_of=date(2026, 8, 31))
    assert check.days_outstanding is None
    assert check.verdict == TIMING_OVERDUE
