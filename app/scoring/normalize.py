"""KPI -> 0-100 normalization and display-ready formatting (PLAN.md §4.2, D23)."""
from __future__ import annotations

import math


def piecewise_linear(x: float, anchors: list[list[float]]) -> float:
    """Map x through [[raw, score], ...] anchors (sorted by raw), clipped at both ends."""
    pts = sorted((float(a), float(b)) for a, b in anchors)
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pts[-1][1]  # unreachable


def linear_to_scale(x: float, scale_top: float) -> float:
    """0 -> 0, scale_top -> 100, linear, clipped. Zero frequency always scores 0."""
    if x <= 0 or scale_top <= 0:
        return 0.0
    return min(100.0, 100.0 * x / scale_top)


def percentile_value(values: list[float], pct: float) -> float:
    """Nearest-rank percentile of a list (used for the national NRI scale top)."""
    if not values:
        return 0.0
    v = sorted(values)
    k = max(0, min(len(v) - 1, math.ceil(pct / 100 * len(v)) - 1))
    return v[k]


# ----------------------------------------------------------------------------- display-ready numbers

def sig_figs(x: float, n: int) -> str:
    if x == 0:
        return "0"
    digits = n - int(math.floor(math.log10(abs(x)))) - 1
    rounded = round(x, digits)
    return f"{rounded:.{max(digits, 0)}f}"


def fmt_score(x: float) -> int:
    return int(round(x))


def fmt_value(x: float | None, unit: str, freq_sig_figs: int = 2, day_decimals: int = 1) -> str:
    """Format a raw KPI value with its unit, at display precision."""
    if x is None:
        return "no data"
    if unit == "events/yr":
        return f"{sig_figs(x, freq_sig_figs)} {unit}"
    return f"{x:.{day_decimals}f} {unit}"


def fmt_percent(x: float, decimals: int = 1) -> str:
    return f"{x:.{decimals}f}%"
