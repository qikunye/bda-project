"""Small statistics helpers: rates with Wilson intervals."""

import math


def wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson interval for a rate. None when there are no cases."""

    if total <= 0:
        return None
    p = successes / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)


def rate(successes: int, total: int) -> dict:
    """Counts, rate and interval, so a small n is never shown as a bare percentage."""

    return {
        "n": total,
        "positives": successes,
        "rate": round(successes / total, 4) if total else None,
        "ci95": wilson(successes, total),
    }
