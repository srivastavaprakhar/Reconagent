"""The canonical record model -- the one shape every source (Razorpay
settlement, bank credit, invoice) collapses into. Every downstream unit
(matcher, subset-sum solver, FX validator, eval harness) consumes lists of
these; nothing downstream should need to know which source a record came
from beyond reading `.source`.

Plain dataclasses, frozen (a parsed record shouldn't mutate under a
downstream stage). No ORM, no pydantic, no base-class hierarchy -- three
sources, one shape, distinguished by a `source` string is all this needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from reconagent.money import reject_float


_ROW_MONEY_FIELDS = (
    "debit_minor",
    "credit_minor",
    "amount_minor",
    "fee_minor",
    "tax_minor",
    "base_amount_minor",
    "conversion_rate",
)


@dataclass(frozen=True)
class SettlementRow:
    """One row of razorpay_settlements.csv -- the per-transaction detail a
    settlement-level CanonicalRecord aggregates over. Kept around (on
    CanonicalRecord.rows) because a refund row carries its own conversion
    rate and base amount, which the refund-FX-asymmetry logic downstream
    needs and a settlement-level aggregate alone would discard.
    """

    entity_id: str
    type: str  # "payment" | "refund"
    debit_minor: int
    credit_minor: int
    amount_minor: int  # gross, in `currency` (may be foreign)
    currency: str
    fee_minor: int
    tax_minor: int
    on_hold: bool
    settled: bool
    created_at: date | None
    settled_at: date | None
    settlement_id: str
    settlement_utr: str
    description: str
    notes: str
    payment_id: str
    order_id: str
    order_receipt: str
    method: str
    international: bool
    conversion_rate: Decimal | None
    base_amount_minor: int | None
    base_currency: str | None
    refund_id: str | None

    def __post_init__(self) -> None:
        for f in _ROW_MONEY_FIELDS:
            reject_float(f, getattr(self, f))


@dataclass(frozen=True)
class CanonicalRecord:
    """The normalized shape every source collapses into.

    `source` is one of "razorpay_settlement", "bank_credit", "invoice".
    Fields that don't apply to a given source are left at their default
    (None / empty) rather than the shape growing per-source variants.
    """

    source: str
    record_id: str  # stable id: settlement_id / bank NtryRef or :20: / invoice_id
    counterparty_name: str
    narration: str
    amount_minor: int
    currency: str

    # Timing (spec §5: preserve what makes T+2..T+7 nostro timing computable).
    booking_date: date | None = None
    value_date: date | None = None
    created_at: date | None = None
    settled_at: date | None = None

    # Cross-reference fields the matching cascade keys on.
    utr: str | None = None
    end_to_end_id: str | None = None
    invoice_id: str | None = None
    order_id: str | None = None
    payment_id: str | None = None

    # Cross-border: the applied rate and the two legs it relates.
    conversion_rate: Decimal | None = None
    foreign_amount_minor: int | None = None
    foreign_currency: str | None = None
    base_amount_minor: int | None = None

    channel: str | None = None  # "SWIFT_MT103" | "camt.053" | "DOMESTIC_NEFT" | None
    rows: tuple[SettlementRow, ...] = ()  # razorpay_settlement only

    def __post_init__(self) -> None:
        # The parsers route every amount through money.parse_minor/parse_rate,
        # but downstream units (matcher, FX validator, eval harness) construct
        # these directly. Guarding here is what stops the float rule decaying
        # into "the parsers are careful" -- the rule belongs to the type.
        for f in _RECORD_MONEY_FIELDS:
            reject_float(f, getattr(self, f))


_RECORD_MONEY_FIELDS = (
    "amount_minor",
    "foreign_amount_minor",
    "base_amount_minor",
    "conversion_rate",
)
