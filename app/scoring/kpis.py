"""Raw KPI computation (no normalization): observed-weather day counts and NRI frequencies."""
from __future__ import annotations

from typing import Literal

from app.data.fema_nri import NriCounty
from app.data.series import DailySeries

Op = Literal[">=", "<="]


def days_meeting(values: list[float | None], op: Op, threshold: float) -> tuple[int, int]:
    """(days meeting the condition, days with data). Missing days never count as meeting it."""
    present = [v for v in values if v is not None]
    if op == ">=":
        hits = sum(v >= threshold for v in present)
    else:
        hits = sum(v <= threshold for v in present)
    return hits, len(present)


def annual_days(series: DailySeries, var: str, op: Op, threshold: float) -> tuple[float | None, float]:
    """Average days per year meeting the condition, and data coverage (0-1).

    The average is computed over days with data and scaled to 365.25 days, so a small gap
    doesn't bias it downward. Returns (None, coverage) if there is no data at all.
    """
    hits, present = days_meeting(series.values.get(var, []), op, threshold)
    coverage = series.coverage(var)
    if present == 0:
        return None, coverage
    return hits / present * 365.25, coverage


def nri_frequency(county: NriCounty, codes: list[str]) -> float:
    """Sum of NRI annualized frequencies (events/yr) for the given hazard codes.

    Summing is deliberate for multi-part hazards like flood (inland + coastal): a hub exposed
    to only one kind of flooding must not be diluted by a zero for the other (Dallas has no coast).
    'Not Applicable' counts as 0 (D17).
    """
    return sum(county.frequency(c) for c in codes)
