"""The money boundary.

Every raw value that will become a money-path field (an amount or a
conversion rate) must pass through `parse_minor` or `parse_rate` before it is
allowed into a `CanonicalRecord`. That is what makes the "never a float"
rule (CLAUDE.md, spec §3) enforceable rather than aspirational: a float
reaching either function raises immediately, with no silent coercion path.

`Decimal(some_float)` is exactly the accident this exists to prevent -- it
looks like a safe conversion but silently inherits the float's binary
rounding error. Money must arrive as a string, an int (already minor units),
or a `Decimal` that was itself built from a string.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation


class FloatMoneyError(TypeError):
    """A float (or bool) reached a money-path boundary. Not a warning."""


# A rate that legitimately comes off a provider feed has a handful of decimal
# places (FBIL publishes 4). A Decimal built from a float carries ~20-50. This
# is the line between the two: high enough not to reject a real 6dp provider
# rate, low enough that float contamination cannot slip through as a Decimal.
MAX_RATE_DP = 8


def reject_float(field: str, value: object) -> None:
    """Raise if `value` is a float or bool. For guarding money-path fields on
    records that are constructed directly rather than through a parser."""
    if isinstance(value, bool):
        raise FloatMoneyError(f"bool {value!r} reached money field {field!r}")
    if isinstance(value, float):
        raise FloatMoneyError(
            f"float {value!r} reached money field {field!r} -- "
            "use an int of minor units, or a Decimal parsed from a string"
        )


def parse_minor(raw: str | int | Decimal, *, exp: int = 2) -> int:
    """Parse `raw` into an integer count of minor units (paise/cents).

    Accepts an int (assumed already minor units), a `Decimal`, or a decimal
    string using '.' as the separator. Rejects floats outright. Rejects
    strings/Decimals carrying more than `exp` fractional digits rather than
    silently rounding them away -- that precision loss is a data problem to
    surface, not paper over.
    """
    if isinstance(raw, bool):
        raise FloatMoneyError(f"bool {raw!r} reached a money field")
    if isinstance(raw, float):
        raise FloatMoneyError(
            f"float {raw!r} reached a money field -- parse from a string or Decimal instead"
        )
    if isinstance(raw, int):
        return raw
    if isinstance(raw, Decimal):
        d = raw
    elif isinstance(raw, str):
        s = raw.strip()
        if not s:
            raise ValueError("empty amount string")
        try:
            d = Decimal(s)
        except InvalidOperation as exc:
            raise ValueError(f"malformed amount: {raw!r}") from exc
    else:
        raise TypeError(f"unsupported type for a money field: {type(raw).__name__}")

    exponent = d.as_tuple().exponent
    if not isinstance(exponent, int):
        raise ValueError(f"non-finite amount: {raw!r}")
    if exponent < -exp:
        raise ValueError(f"amount has more than {exp} decimal places: {raw!r}")
    return int(d.scaleb(exp))


def parse_rate(raw: str | Decimal) -> Decimal:
    """Parse a conversion rate. Not a monetary amount, but the same float ban
    applies -- an FX rate is exactly the kind of value someone "helpfully"
    hands you as a float."""
    if isinstance(raw, bool):
        raise FloatMoneyError(f"bool {raw!r} reached a rate field")
    if isinstance(raw, float):
        raise FloatMoneyError(f"float {raw!r} reached a rate field")
    if isinstance(raw, Decimal):
        d = raw
    elif isinstance(raw, str):
        s = raw.strip()
        if not s:
            raise ValueError("empty rate string")
        try:
            d = Decimal(s)
        except InvalidOperation as exc:
            raise ValueError(f"malformed rate: {raw!r}") from exc
    else:
        raise TypeError(f"unsupported type for a rate field: {type(raw).__name__}")

    exponent = d.as_tuple().exponent
    if not isinstance(exponent, int):
        raise ValueError(f"non-finite rate: {raw!r}")
    # Catches Decimal(some_float), which is a float in all but type: it arrives
    # carrying the binary rounding error the string path exists to avoid.
    if exponent < -MAX_RATE_DP:
        raise ValueError(
            f"rate has more than {MAX_RATE_DP} decimal places, which means it was "
            f"almost certainly built from a float: {raw!r}"
        )
    return d
