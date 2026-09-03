"""The money boundary is the single rule most likely to rot silently
(CLAUDE.md), so it gets tested directly rather than only through the
parsers that happen to use it.
"""

from decimal import Decimal

import pytest

from reconagent.money import FloatMoneyError, parse_minor, parse_rate


def test_parse_minor_from_string():
    assert parse_minor("123.45") == 12345
    assert parse_minor("0.01") == 1
    assert parse_minor("1000000.00") == 100000000


def test_parse_minor_from_int_passthrough():
    assert parse_minor(12345) == 12345


def test_parse_minor_from_decimal():
    assert parse_minor(Decimal("99.99")) == 9999


def test_parse_minor_rejects_float():
    with pytest.raises(FloatMoneyError):
        parse_minor(123.45)


def test_parse_minor_rejects_float_zero():
    # 0.0 is falsy but must still be rejected -- a truthiness check would miss it.
    with pytest.raises(FloatMoneyError):
        parse_minor(0.0)


def test_parse_minor_rejects_bool():
    with pytest.raises(FloatMoneyError):
        parse_minor(True)


def test_parse_minor_rejects_empty_string():
    with pytest.raises(ValueError):
        parse_minor("")


def test_parse_minor_rejects_malformed_string():
    with pytest.raises(ValueError):
        parse_minor("not-a-number")


def test_parse_minor_rejects_wrong_decimal_separator():
    # SWIFT comma leaking into a plain decimal-string field must not be
    # silently accepted as some other magnitude.
    with pytest.raises(ValueError):
        parse_minor("1234,56")


def test_parse_minor_rejects_excess_precision():
    # Third decimal place would be silently rounded away by naive code --
    # reject instead of guessing which way to round a money value.
    with pytest.raises(ValueError):
        parse_minor("1.005")


def test_parse_minor_accepts_fewer_than_exp_decimals():
    assert parse_minor("5") == 500
    assert parse_minor("5.1") == 510


def test_parse_rate_rejects_float():
    with pytest.raises(FloatMoneyError):
        parse_rate(87.34)


def test_parse_rate_from_string():
    assert parse_rate("87.3396") == Decimal("87.3396")


def test_parse_rate_rejects_empty_string():
    with pytest.raises(ValueError):
        parse_rate("")


def test_parse_rate_rejects_malformed_string():
    with pytest.raises(ValueError):
        parse_rate("eighty-seven")


def test_float_in_canonical_record_construction_raises():
    """A float must be impossible to get into a CanonicalRecord's money fields
    -- including by constructing the record directly, which is what every
    downstream unit does. Passing the float to parse_minor first would only
    re-test parse_minor."""
    from reconagent.records import CanonicalRecord

    base = dict(
        source="invoice",
        record_id="INV-1",
        counterparty_name="Someone",
        narration="",
        amount_minor=12345,
        currency="INR",
    )
    CanonicalRecord(**base)  # the int form is fine

    for field, bad in [
        ("amount_minor", 123.45),
        ("foreign_amount_minor", 1.0),
        ("base_amount_minor", 0.0),
        ("conversion_rate", 111.678),
        ("amount_minor", True),
    ]:
        with pytest.raises(FloatMoneyError):
            CanonicalRecord(**{**base, field: bad})


def test_float_in_settlement_row_construction_raises():
    from reconagent.records import SettlementRow

    base = dict(
        entity_id="pay_x", type="payment", debit_minor=0, credit_minor=100,
        amount_minor=100, currency="INR", fee_minor=0, tax_minor=0,
        on_hold=False, settled=True, created_at=None, settled_at=None,
        settlement_id="setl_x", settlement_utr="U", description="", notes="",
        payment_id="pay_x", order_id="", order_receipt="", method="upi",
        international=False, conversion_rate=None, base_amount_minor=None,
        base_currency=None, refund_id=None,
    )
    SettlementRow(**base)

    for field in ("debit_minor", "credit_minor", "amount_minor", "fee_minor", "tax_minor"):
        with pytest.raises(FloatMoneyError):
            SettlementRow(**{**base, field: 1.5})


def test_rate_built_from_a_float_is_rejected():
    """Decimal(some_float) is a float in all but type -- it arrives carrying the
    binary rounding error the string path exists to avoid, and parse_rate has no
    decimal-places ceiling of its own to catch it the way parse_minor does."""
    with pytest.raises(ValueError, match="decimal places"):
        parse_rate(Decimal(111.678))

    parse_rate(Decimal("111.6780"))  # the string form is fine
    parse_rate("111.678000")         # 6dp from a provider feed is fine
