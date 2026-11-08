"""Finite numeric formatting at the presentation boundary; source values stay intact."""

from decimal import Decimal, InvalidOperation

UNAVAILABLE = "Unavailable"


def numeric_value(value) -> Decimal | None:
    """Accept numeric scalars and numeric strings without treating invalid data as zero."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def format_number(value, *, digits: int = 4, grouping: bool = False, missing: str = UNAVAILABLE) -> str:
    number = numeric_value(value)
    if number is None:
        return missing
    return format(number, f"{',' if grouping else ''}.{digits}f")
