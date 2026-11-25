"""A daily time series from one source, with explicit units and gaps (None)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

# Canonical variable names used across sources.
SNOWFALL, PRECIP, TMAX, TMIN, APPARENT_TMAX, GUST_MAX = (
    "snowfall", "precipitation", "tmax", "tmin", "apparent_tmax", "gust_max")


def calendar(start: str, end: str) -> list[str]:
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    return [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]


@dataclass(frozen=True)
class DailySeries:
    source: str                                  # e.g. "open-meteo:era5_seamless", "ghcn:USW00003017"
    dates: list[str]                             # every calendar day in the requested window
    values: dict[str, list[float | None]]        # canonical var -> one value per date (None = missing)
    units: dict[str, str]                        # canonical var -> unit string
    meta: dict[str, Any] = field(default_factory=dict)

    def coverage(self, var: str) -> float:
        vals = self.values.get(var)
        if not vals:
            return 0.0
        return sum(v is not None for v in vals) / len(vals)

    def window(self, start: str, end: str) -> "DailySeries":
        idx = [i for i, d in enumerate(self.dates) if start <= d <= end]
        return DailySeries(self.source, [self.dates[i] for i in idx],
                           {k: [v[i] for i in idx] for k, v in self.values.items()}, self.units, self.meta)
