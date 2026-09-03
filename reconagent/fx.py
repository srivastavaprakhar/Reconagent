"""The cross-border intelligence layer (spec §5): FX-tolerance validation,
variance decomposition, refund FX asymmetry, and nostro/vostro timing.

Three things are worth reading before the code.

**The tolerance band is this module's, not the answer key's.** Spec §5 says the
band is "calibrated from labelled data". `DEFAULT_FX_TOLERANCE_BPS` below was
derived from the main-set label distribution -- the derivation is written out
at the constant and re-run as an assertion in `tests/test_fx.py`. Nothing here
reads `ground_truth.json`; the band is a parameter on every entry point so a
caller can override it.

**The attribution is arithmetic, never a guess** (spec §6). The decomposition
solves one identity and then asks which single term, if restated, closes the
residual exactly. Each candidate cause leaves a different fingerprint: a GST
leg that isn't 18% of the MDR, a base amount that isn't the foreign gross times
the stated rate, a net that is a power-of-ten shift or a digit transposition of
the expected net. If no candidate closes the residual, or if more than one
does, the answer is `UNRESOLVED`. Returning `UNRESOLVED` is a result, not a
failure -- forcing every residual into a named bucket is how a reconciliation
engine gets trusted right up until it is catastrophically wrong.

**Missing reference rate: refuse, don't carry forward.** See
`ReferenceRates.rate`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import csv

from reconagent.money import parse_rate
from reconagent.records import CanonicalRecord, SettlementRow


# GST on payment-gateway MDR is 18% under the CGST/SGST schedule. This is a
# statutory rate, not a tunable -- if it ever changes it changes by legislation
# and this line changes with it. It is not a configuration knob.
GST_RATE_ON_MDR = Decimal("18") / Decimal("100")

# ---------------------------------------------------------------------------
# The FX tolerance band. Derived, not copied.
#
# Population: every international settlement leg in the MAIN set (30 legs: 28
# captures + 2 refunds) whose labelled defect class is NOT fx_drift_flagged --
# i.e. the 25 legs the labels call legitimate. Deviation is computed by this
# module's own `check_fx_rate` against fx_reference_rates.csv, not read out of
# the answer key.
#
#   n = 25
#   deviations (bps): -43.62 -41.11 -36.48 -27.93 -19.75 -19.49 -19.13 -18.58
#                     -18.38 -13.99 -13.51  -4.31  -2.23   2.42   2.66   4.58
#                       9.56  11.58  13.18  13.35  14.92  14.98  30.82  37.34
#                      43.00
#   max |deviation|            = 43.62 bps
#   sigma about zero (RMS)     = 22.96 bps   <- the reference rate IS the
#                                               centre; a sample mean of
#                                               -3.20 bps on n=25 is not a
#                                               real bias, so the band is
#                                               centred on 0, not on the mean.
#   3 sigma                    = 68.87 bps
#   round DOWN to nearest 5    = 65 bps
#
# Rounding direction is deliberate. Spec §9 leads with false-clear rate: the
# expensive error is passing a genuinely bad rate as benign, not sending an
# extra borderline case to a review queue. When a 3-sigma band lands between
# two round numbers you take the tighter one.
#
# Separation on the main set: the band sits 21.4 bps above the largest
# legitimate deviation and 149.5 bps below the smallest flagged one (214.45).
#
# `tests/test_fx.py::test_tolerance_default_is_reproducible_from_main_labels`
# re-runs this derivation from the data and asserts it lands here.
DEFAULT_FX_TOLERANCE_BPS = Decimal("65")

# Slack on the decomposition residual. Every amount in this system is an
# integer of minor units and the two conversions in the identity each round
# half-up once, so one minor unit is the entire legitimate error budget. This
# is NOT ground_truth.json's `amount_tolerance_minor: 100`, which describes how
# the bank credits were labelled, not how exact this arithmetic is.
DEFAULT_AMOUNT_TOLERANCE_MINOR = 1

# Cross-border settlements land T+2..T+7 (spec §5).
DEFAULT_SETTLEMENT_WINDOW_DAYS = (2, 7)

_BPS = Decimal("10000")
_TWO_DP = Decimal("0.01")


def _round_minor(value: Decimal) -> int:
    """Minor units are integers; the only rounding in this module is half-up
    on a conversion, matching how the settlement feed itself rounds."""
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------------------
# 1. Reference rates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReferenceRates:
    """FBIL daily reference rates, keyed by (value date, currency pair)."""

    rates: dict[tuple[date, str], Decimal]
    sources: dict[tuple[date, str], str]

    def rate(self, value_date: date, currency_pair: str) -> Decimal | None:
        """The published rate for exactly this value date, or None.

        No carry-forward. FBIL publishes a rate *for a value date*; on a day it
        did not publish, there is no reference rate, and inventing one by
        reusing Friday's has two costs that both land on the metrics spec §9
        leads with. A stale reference drifts away from the market, so a rate
        that was fine gets flagged (false exception) and a rate that was
        manipulated by less than the weekend's drift gets cleared (false
        clear) -- and neither is visible to the reader of the finding, because
        the output looks identical to a genuine validation.

        Refusing is louder and cheaper: the finding carries
        `NO_REFERENCE_RATE`, the settlement is not silently blessed, and a
        human sees that the gap is in the rate feed rather than in the
        merchant's books. If a real business-day-only FBIL feed is wired in
        later, the fix is to source weekend rates properly (or to defer the
        check to the next published date), not to fabricate one here.
        """
        return self.rates.get((value_date, currency_pair))

    def source(self, value_date: date, currency_pair: str) -> str | None:
        return self.sources.get((value_date, currency_pair))


def load_reference_rates(path: str | Path) -> ReferenceRates:
    """Parse fx_reference_rates.csv. Rates go through `money.parse_rate` for
    the same reason amounts go through `parse_minor`: a rate handed over as a
    float is the exact accident that silently poisons a basis-point
    comparison."""
    rates: dict[tuple[date, str], Decimal] = {}
    sources: dict[tuple[date, str], str] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for raw in csv.DictReader(f):
            key = (date.fromisoformat(raw["value_date"].strip()), raw["currency_pair"].strip())
            rates[key] = parse_rate(raw["reference_rate"])
            sources[key] = raw["source"].strip()
    return ReferenceRates(rates=rates, sources=sources)


# ---------------------------------------------------------------------------
# 2. FX tolerance validation
# ---------------------------------------------------------------------------

BENIGN_FX_DRIFT = "BENIGN_FX_DRIFT"
FLAGGED_FX_DRIFT = "FLAGGED_FX_DRIFT"
NO_REFERENCE_RATE = "NO_REFERENCE_RATE"


@dataclass(frozen=True)
class FxCheck:
    """An applied rate measured against the FBIL reference for its value date.

    `verdict` is BENIGN_FX_DRIFT / FLAGGED_FX_DRIFT / NO_REFERENCE_RATE. Note
    what a flag means: spec §5 is explicit that a rate outside the band "isn't
    automatically wrong" -- it is the input to the decomposition, not a
    conclusion about fraud.
    """

    currency_pair: str
    value_date: date
    applied_rate: Decimal
    reference_rate: Decimal | None
    reference_source: str | None
    deviation_bps: Decimal | None
    tolerance_bps: Decimal
    within_tolerance: bool | None
    verdict: str


def check_fx_rate(
    reference: ReferenceRates,
    *,
    value_date: date,
    currency_pair: str,
    applied_rate: Decimal,
    tolerance_bps: Decimal = DEFAULT_FX_TOLERANCE_BPS,
) -> FxCheck:
    """deviation_bps = (applied - reference) / reference * 10_000, to 2dp.

    Signed: a positive deviation means the merchant was converted at more INR
    per foreign unit than FBIL published, a negative one at less. Both
    directions matter -- an implausibly *favourable* rate is as much a data
    problem as an unfavourable one -- so the band is on the absolute value.
    """
    ref = reference.rate(value_date, currency_pair)
    if ref is None or ref == 0:
        return FxCheck(
            currency_pair=currency_pair,
            value_date=value_date,
            applied_rate=applied_rate,
            reference_rate=None,
            reference_source=None,
            deviation_bps=None,
            tolerance_bps=tolerance_bps,
            within_tolerance=None,
            verdict=NO_REFERENCE_RATE,
        )
    deviation = ((applied_rate - ref) / ref * _BPS).quantize(_TWO_DP, rounding=ROUND_HALF_UP)
    within = abs(deviation) <= tolerance_bps
    return FxCheck(
        currency_pair=currency_pair,
        value_date=value_date,
        applied_rate=applied_rate,
        reference_rate=ref,
        reference_source=reference.source(value_date, currency_pair),
        deviation_bps=deviation,
        tolerance_bps=tolerance_bps,
        within_tolerance=within,
        verdict=BENIGN_FX_DRIFT if within else FLAGGED_FX_DRIFT,
    )


def _pair(foreign_currency: str, base_currency: str) -> str:
    return f"{foreign_currency}/{base_currency}"


def check_settlement_row_fx(
    reference: ReferenceRates,
    row: SettlementRow,
    *,
    tolerance_bps: Decimal = DEFAULT_FX_TOLERANCE_BPS,
) -> FxCheck | None:
    """The FX check for one settlement row, or None if the row isn't a
    cross-border conversion. The row's `settled_at` is the value date -- that
    is the day the conversion event actually priced."""
    if row.conversion_rate is None or row.settled_at is None:
        return None
    base = row.base_currency or "INR"
    if row.currency == base:
        return None
    return check_fx_rate(
        reference,
        value_date=row.settled_at,
        currency_pair=_pair(row.currency, base),
        applied_rate=row.conversion_rate,
        tolerance_bps=tolerance_bps,
    )


# ---------------------------------------------------------------------------
# 3. Variance decomposition
# ---------------------------------------------------------------------------

NO_VARIANCE = "NO_VARIANCE"
FEE_MISMATCH = "FEE_MISMATCH"
DATA_ENTRY_ERROR = "DATA_ENTRY_ERROR"
UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True)
class VarianceDecomposition:
    """Every term of `net = gross - MDR - GST - FX_spread - refunds`, the
    residual it leaves, and the arithmetic that named the cause.

    `signature` is the one line a reviewer reads to check the machine's work.
    `candidates` is every cause whose restatement closes the residual exactly;
    `attribution` is that cause only when there is exactly one of them.
    """

    settlement_id: str
    base_currency: str
    foreign_currency: str | None

    gross_reference_minor: int | None  # foreign gross at the FBIL reference rate
    gross_applied_minor: int  # foreign gross at the applied rate == the booked base amount
    fx_spread_minor: int  # reference gross - applied gross
    mdr_minor: int
    gst_minor: int
    refund_adjustment_minor: int
    refund_leg_minor: int  # INR value of refund rows, settled separately (see note)

    expected_net_minor: int
    actual_net_minor: int
    residual_minor: int

    fx: FxCheck | None
    attribution: str
    signature: str
    candidates: tuple[str, ...]


def _explains(residual: int, correction: int, tolerance: int) -> bool:
    """A candidate cause explains the residual when restating that one term by
    `correction` closes the gap to within the rounding budget."""
    return correction != 0 and abs(residual - correction) <= tolerance


def _decimal_shift(expected: int, actual: int) -> int | None:
    """A misplaced decimal point: actual is expected times a power of ten.
    Returns the exponent, or None. The classic keying slip on an amount."""
    for k in (-3, -2, -1, 1, 2, 3):
        if k > 0 and expected * 10**k == actual:
            return k
        if k < 0 and actual * 10 ** (-k) == expected:
            return k
    return None


def _is_transposition(expected: int, actual: int) -> bool:
    """Two adjacent digits swapped. The fingerprint is exact and well known:
    the difference is a non-zero multiple of 9 and the two numbers are digit
    permutations of each other. This is the reason accountants have chased
    out-by-a-multiple-of-nine errors for a century."""
    if expected == actual or expected < 0 or actual < 0:
        return False
    diff = actual - expected
    if diff % 9 != 0:
        return False
    return sorted(str(expected)) == sorted(str(actual))


def decompose_variance(
    record: CanonicalRecord,
    reference: ReferenceRates,
    *,
    tolerance_bps: Decimal = DEFAULT_FX_TOLERANCE_BPS,
    amount_tolerance_minor: int = DEFAULT_AMOUNT_TOLERANCE_MINOR,
) -> VarianceDecomposition:
    """Solve the settlement identity and attribute whatever is left over.

    The identity, in the spec's own terms:

        net = gross - MDR - GST_on_MDR - FX_spread - refund_adjustments

    with `gross` taken at the FBIL reference rate and `FX_spread` the term that
    carries it to the rate actually applied:

        gross_reference = round(foreign_gross * reference_rate)
        gross_applied   = round(foreign_gross * applied_rate)   [ == base_amount ]
        FX_spread       = gross_reference - gross_applied

    so the identity collapses to `gross_applied - MDR - GST`, which is exactly
    what the settlement feed should have credited. The collapse is the point:
    the FX spread cancels out of the *amount* arithmetic entirely, which is why
    a benign drift and a flagged drift both leave residual zero and are
    separated by the rate check rather than by the money.

    `refund_adjustments` is zero here by the parser's contract: a refund
    converts at its own FX event and settles as its own bank movement, so it is
    never netted into the capture. Its INR value is reported as
    `refund_leg_minor` so a reader can see the term did not go missing;
    `reconcile_refund_fx` is where that leg is reconciled.
    """
    capture_rows = [r for r in record.rows if r.type != "refund"]
    refund_rows = [r for r in record.rows if r.type == "refund"]
    base_currency = record.currency

    mdr = sum(r.fee_minor for r in capture_rows)
    gst = sum(r.tax_minor for r in capture_rows)
    gross_applied = sum(
        r.base_amount_minor if r.base_amount_minor is not None else r.amount_minor
        for r in capture_rows
    )
    refund_leg = sum(
        r.base_amount_minor if r.base_amount_minor is not None else r.amount_minor
        for r in refund_rows
    )

    intl = [r for r in capture_rows if r.conversion_rate is not None and r.currency != base_currency]
    fx = check_settlement_row_fx(reference, intl[0], tolerance_bps=tolerance_bps) if intl else None
    foreign_currency = intl[0].currency if intl else None
    foreign_gross = sum(r.amount_minor for r in intl)

    if fx is not None and fx.reference_rate is not None:
        gross_reference: int | None = _round_minor(Decimal(foreign_gross) * fx.reference_rate)
        fx_spread = gross_reference - gross_applied
    else:
        gross_reference = None
        fx_spread = 0

    # gross_reference - MDR - GST - fx_spread, written so the cancellation is
    # visible rather than pre-simplified away.
    expected_net = (
        (gross_reference if gross_reference is not None else gross_applied)
        - mdr
        - gst
        - fx_spread
        - 0  # refund_adjustments; see docstring
    )
    actual_net = record.amount_minor
    residual = actual_net - expected_net

    fields = dict(
        settlement_id=record.record_id,
        base_currency=base_currency,
        foreign_currency=foreign_currency,
        gross_reference_minor=gross_reference,
        gross_applied_minor=gross_applied,
        fx_spread_minor=fx_spread,
        mdr_minor=mdr,
        gst_minor=gst,
        refund_adjustment_minor=0,
        refund_leg_minor=refund_leg,
        expected_net_minor=expected_net,
        actual_net_minor=actual_net,
        residual_minor=residual,
        fx=fx,
    )

    if abs(residual) <= amount_tolerance_minor:
        # The money ties out. The only open question is whether the rate that
        # produced it was reasonable, and that is the FX check's answer.
        if fx is None:
            return VarianceDecomposition(
                **fields,
                attribution=NO_VARIANCE,
                signature=(
                    f"net {actual_net} = gross {gross_applied} - MDR {mdr} - GST {gst}; "
                    f"residual {residual}; no FX leg"
                ),
                candidates=(),
            )
        if fx.verdict == NO_REFERENCE_RATE:
            return VarianceDecomposition(
                **fields,
                attribution=UNRESOLVED,
                signature=(
                    f"residual {residual} within {amount_tolerance_minor}, but no "
                    f"{fx.reference_source or 'FBIL'} reference published for "
                    f"{fx.currency_pair} on {fx.value_date}: applied rate "
                    f"{fx.applied_rate} not validated"
                ),
                candidates=(),
            )
        return VarianceDecomposition(
            **fields,
            attribution=fx.verdict,
            signature=(
                f"net {actual_net} = gross {gross_applied} - MDR {mdr} - GST {gst} "
                f"(FX spread {fx_spread} cancels); residual {residual}; applied "
                f"{fx.applied_rate} vs {fx.reference_source} {fx.reference_rate} = "
                f"{fx.deviation_bps} bps, band +/-{fx.tolerance_bps} bps"
            ),
            candidates=(),
        )

    # Residual is real. Ask each candidate cause whether restating its own term
    # -- and only its own term -- closes the gap exactly.
    candidates: list[str] = []
    notes: list[str] = []

    # Restating GST to its statutory value moves expected_net by +gst_error
    # (expected_net subtracts GST, so an overstatement of E depresses it by E
    # and inflates the residual by E). If that lands on the residual, the whole
    # gap is the fee block and nothing else.
    expected_gst = _round_minor(Decimal(mdr) * GST_RATE_ON_MDR)
    gst_error = gst - expected_gst
    if _explains(residual, gst_error, amount_tolerance_minor):
        candidates.append(FEE_MISMATCH)
        notes.append(
            f"GST {gst} != 18% of MDR {mdr} (= {expected_gst}), out by {gst_error}, "
            f"which is exactly the residual {residual}"
        )

    if intl:
        stated = _round_minor(Decimal(foreign_gross) * intl[0].conversion_rate)
        conversion_error = gross_applied - stated
        if _explains(residual, conversion_error, amount_tolerance_minor):
            implied_rate = (Decimal(gross_applied) / Decimal(foreign_gross)).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
            implied = check_fx_rate(
                reference,
                value_date=fx.value_date if fx else intl[0].settled_at,
                currency_pair=_pair(intl[0].currency, base_currency),
                applied_rate=implied_rate,
                tolerance_bps=tolerance_bps,
            )
            shift = _decimal_shift(stated, gross_applied)
            if shift is not None or _is_transposition(stated, gross_applied):
                candidates.append(DATA_ENTRY_ERROR)
                how = f"decimal shift of 10^{shift}" if shift is not None else "digit transposition"
                notes.append(
                    f"base amount {gross_applied} is a {how} of foreign gross "
                    f"{foreign_gross} x rate {intl[0].conversion_rate} = {stated}"
                )
            elif implied.verdict in (BENIGN_FX_DRIFT, FLAGGED_FX_DRIFT):
                candidates.append(implied.verdict)
                notes.append(
                    f"base amount {gross_applied} implies rate {implied_rate} "
                    f"({implied.deviation_bps} bps from reference {implied.reference_rate}), "
                    f"not the stated {intl[0].conversion_rate}"
                )

    shift = _decimal_shift(expected_net, actual_net)
    if shift is not None:
        candidates.append(DATA_ENTRY_ERROR)
        notes.append(f"net {actual_net} is expected net {expected_net} x 10^{shift}")
    elif _is_transposition(expected_net, actual_net):
        candidates.append(DATA_ENTRY_ERROR)
        notes.append(
            f"net {actual_net} is a digit transposition of expected net {expected_net} "
            f"(difference {residual} is a multiple of 9)"
        )

    unique = tuple(dict.fromkeys(candidates))
    stem = (
        f"net {actual_net} != gross {gross_applied} - MDR {mdr} - GST {gst} "
        f"= {expected_net}; residual {residual}. "
    )
    if len(unique) == 1:
        return VarianceDecomposition(
            **fields, attribution=unique[0], signature=stem + notes[0], candidates=unique
        )
    if len(unique) > 1:
        return VarianceDecomposition(
            **fields,
            attribution=UNRESOLVED,
            signature=stem
            + f"{len(unique)} causes each close it exactly ({', '.join(unique)}): "
            + "; ".join(notes)
            + ". Arithmetic does not single one out.",
            candidates=unique,
        )
    return VarianceDecomposition(
        **fields,
        attribution=UNRESOLVED,
        signature=stem + "no single term closes it: "
        f"GST is {'' if gst_error else 'exactly '}18% of MDR"
        + (f" (off by {gst_error})" if gst_error else "")
        + (
            f", base amount ties to rate {intl[0].conversion_rate}"
            if intl and gross_applied == _round_minor(Decimal(foreign_gross) * intl[0].conversion_rate)
            else ""
        )
        + ", residual is neither a decimal shift nor a transposition of the expected net",
        candidates=(),
    )


# ---------------------------------------------------------------------------
# 4. Refund FX asymmetry
# ---------------------------------------------------------------------------

REFUND_FX_ASYMMETRY = "REFUND_FX_ASYMMETRY"
REFUND_AMOUNT_BREAK = "REFUND_AMOUNT_BREAK"


@dataclass(frozen=True)
class FxLeg:
    value_date: date | None
    rate: Decimal | None
    reference_rate: Decimal | None
    foreign_minor: int
    inr_minor: int


@dataclass(frozen=True)
class RefundFxReconciliation:
    """A refund reconciled against its own conversion event, not the capture's.

    A "full" refund is full in the *foreign* currency -- that is the amount the
    cardholder gets back. The two legs price on different days, so the INR
    residual is non-zero by construction. That is not a break, and the point of
    this type is to say so with the two rates attached rather than emit an
    unexplained difference.
    """

    settlement_id: str
    refund_id: str
    currency: str
    capture: FxLeg
    refund: FxLeg
    foreign_residual_minor: int
    inr_residual_minor: int
    expected_asymmetry: bool
    verdict: str
    signature: str


def reconcile_refund_fx(
    record: CanonicalRecord,
    reference: ReferenceRates,
    *,
    tolerance_bps: Decimal = DEFAULT_FX_TOLERANCE_BPS,
) -> list[RefundFxReconciliation]:
    """One reconciliation per refund row on the settlement.

    Each refund row names the capture it reverses via `payment_id`; the two
    rows carry their own `conversion_rate` and `base_amount_minor`, which is
    the whole reason the parser keeps `.rows` around.
    """
    by_entity = {r.entity_id: r for r in record.rows}
    out: list[RefundFxReconciliation] = []
    for row in record.rows:
        if row.type != "refund":
            continue
        capture = by_entity.get(row.payment_id)
        if capture is None:
            continue

        def leg(r: SettlementRow) -> FxLeg:
            check = check_settlement_row_fx(reference, r, tolerance_bps=tolerance_bps)
            return FxLeg(
                value_date=r.settled_at,
                rate=r.conversion_rate,
                reference_rate=check.reference_rate if check else None,
                foreign_minor=r.amount_minor,
                inr_minor=r.base_amount_minor if r.base_amount_minor is not None else r.amount_minor,
            )

        cap, ref_leg = leg(capture), leg(row)
        foreign_residual = cap.foreign_minor - ref_leg.foreign_minor
        inr_residual = cap.inr_minor - ref_leg.inr_minor
        expected = foreign_residual == 0 and inr_residual != 0
        if expected:
            verdict = REFUND_FX_ASYMMETRY
            sig = (
                f"refund {row.entity_id} returns {ref_leg.foreign_minor} "
                f"{row.currency} minor against a capture of {cap.foreign_minor}: "
                f"foreign residual {foreign_residual}, so the refund is full. "
                f"Capture converted {cap.value_date} @ {cap.rate}, refund "
                f"{ref_leg.value_date} @ {ref_leg.rate}; INR residual "
                f"{inr_residual} is the two conversion events, not a shortfall."
            )
        elif foreign_residual == 0:
            verdict = REFUND_FX_ASYMMETRY
            sig = (
                f"refund {row.entity_id} is full in {row.currency} and both legs "
                f"priced identically: INR residual 0."
            )
        else:
            verdict = REFUND_AMOUNT_BREAK
            sig = (
                f"refund {row.entity_id} returns {ref_leg.foreign_minor} against a "
                f"capture of {cap.foreign_minor} {row.currency} minor: foreign "
                f"residual {foreign_residual} != 0, so this is a genuine amount "
                f"difference, not an FX effect."
            )
        out.append(
            RefundFxReconciliation(
                settlement_id=record.record_id,
                refund_id=row.entity_id,
                currency=row.currency,
                capture=cap,
                refund=ref_leg,
                foreign_residual_minor=foreign_residual,
                inr_residual_minor=inr_residual,
                expected_asymmetry=expected,
                verdict=verdict,
                signature=sig,
            )
        )
    return out


# ---------------------------------------------------------------------------
# 5. Nostro/vostro timing
# ---------------------------------------------------------------------------

TIMING_PENDING = "TIMING_PENDING"
TIMING_OVERDUE = "TIMING_OVERDUE"


@dataclass(frozen=True)
class TimingCheck:
    """Whether an unmatched settlement is a break or just waiting on the clock.

    Verdict is TIMING_PENDING right up to the far edge of the window -- a
    settlement that is *early* is not a break either, so the lower bound is
    reported (`inside_expected_window`) but does not by itself promote anything
    to an exception.
    """

    settlement_id: str
    settled_at: date | None
    as_of: date
    days_outstanding: int | None
    expected_window_days: tuple[int, int]
    inside_expected_window: bool
    verdict: str
    signature: str


def check_settlement_timing(
    record: CanonicalRecord,
    *,
    as_of: date,
    window_days: tuple[int, int] = DEFAULT_SETTLEMENT_WINDOW_DAYS,
) -> TimingCheck:
    """For a settlement with no matching bank credit. `as_of` is the statement
    date and is required -- `date.today()` would make this untestable and would
    silently re-age a closed period every time it ran."""
    low, high = window_days
    settled = record.settled_at
    if settled is None:
        return TimingCheck(
            settlement_id=record.record_id,
            settled_at=None,
            as_of=as_of,
            days_outstanding=None,
            expected_window_days=window_days,
            inside_expected_window=False,
            verdict=TIMING_OVERDUE,
            signature="settlement has no settled_at; nothing to age against",
        )
    days = (as_of - settled).days
    inside = low <= days <= high
    pending = days <= high
    return TimingCheck(
        settlement_id=record.record_id,
        settled_at=settled,
        as_of=as_of,
        days_outstanding=days,
        expected_window_days=window_days,
        inside_expected_window=inside,
        verdict=TIMING_PENDING if pending else TIMING_OVERDUE,
        signature=(
            f"settled {settled}, as of {as_of} = {days} days outstanding; "
            f"cross-border window T+{low}..T+{high}: "
            + ("still inside, not a break" if pending else f"past T+{high}, genuine break")
        ),
    )
