"""Open-Meteo: geocoding (location resolution) and historical archive (ERA5 reanalysis).

Verified 2026-11-25 (PLAN.md §7.1):
- Geocoding has no fuzzy matching ("Huston" -> Huston, Idaho), so hub matching runs first.
- Archive cost is reported per request in X-RateLimit-* headers (~days/14 x vars/10 calls;
  10 years x 6 vars ~= 157 calls) against 600 calls/minute on the free tier -> we throttle.
- We pin models=era5_seamless so the whole baseline window comes from one model family.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

import httpx

from app.data.http import DAY_S, CachedHttp, history_ttl
from app.data.series import (APPARENT_TMAX, GUST_MAX, PRECIP, SNOWFALL, TMAX, TMIN, DailySeries,
                             calendar)

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
ARCHIVE_MODEL = "era5_seamless"

# Open-Meteo variable -> canonical name
DAILY_VARS = {
    "snowfall_sum": SNOWFALL,
    "precipitation_sum": PRECIP,
    "temperature_2m_max": TMAX,
    "temperature_2m_min": TMIN,
    "apparent_temperature_max": APPARENT_TMAX,
    "wind_gusts_10m_max": GUST_MAX,
}


@dataclass(frozen=True)
class GeoCandidate:
    name: str
    admin1: str | None          # state / province
    admin2: str | None          # county
    country_code: str | None
    lat: float
    lon: float
    population: int | None
    feature_code: str | None


class RateBudget:
    """Tracks Open-Meteo's per-minute budget from response headers and waits when needed."""

    def __init__(self, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time):
        self.remaining: float | None = None
        self.reset_at: float | None = None
        self.sleep, self.clock = sleep, clock
        self._lock = threading.Lock()

    @staticmethod
    def estimate_cost(params: dict[str, Any]) -> float:
        if "start_date" not in params:
            return 1.0
        days = (date.fromisoformat(params["end_date"]) - date.fromisoformat(params["start_date"])).days + 1
        n_vars = len(str(params.get("daily", "")).split(","))
        return max(1.0, days / 14 * n_vars / 10)

    def before(self, params: dict[str, Any]) -> None:
        with self._lock:
            need = self.estimate_cost(params)
            if self.remaining is not None and self.reset_at is not None and self.remaining < need:
                wait = max(0.0, self.reset_at - self.clock()) + 1.0
                self.sleep(wait)
                self.remaining = None

    def after(self, r: httpx.Response) -> None:
        with self._lock:
            try:
                self.remaining = float(r.headers["X-RateLimit-Remaining"])
                self.reset_at = float(r.headers["X-RateLimit-Reset"])
            except (KeyError, ValueError):
                pass


BUDGET = RateBudget()


def geocode(http: CachedHttp, name: str, count: int = 10) -> list[GeoCandidate]:
    params = {"name": name.strip(), "count": count, "language": "en", "format": "json"}
    res = http.get_json("open-meteo-geocoding", GEOCODE_URL, params, ttl_s=30 * DAY_S)
    return [GeoCandidate(name=r["name"], admin1=r.get("admin1"), admin2=r.get("admin2"),
                         country_code=r.get("country_code"), lat=r["latitude"], lon=r["longitude"],
                         population=r.get("population"), feature_code=r.get("feature_code"))
            for r in res.data.get("results") or []]


def parse_archive(data: dict[str, Any], start: str, end: str) -> DailySeries:
    daily, units = data["daily"], data.get("daily_units", {})
    by_date = {d: i for i, d in enumerate(daily["time"])}
    dates = calendar(start, end)
    values: dict[str, list[float | None]] = {}
    out_units: dict[str, str] = {}
    for om_var, canon in DAILY_VARS.items():
        if om_var not in daily:
            continue
        col = daily[om_var]
        values[canon] = [col[by_date[d]] if d in by_date else None for d in dates]
        out_units[canon] = units.get(om_var, "")
    meta = {"grid_lat": data.get("latitude"), "grid_lon": data.get("longitude"),
            "elevation_m": data.get("elevation"), "model": ARCHIVE_MODEL}
    return DailySeries(f"open-meteo:{ARCHIVE_MODEL}", dates, values, out_units, meta)


def archive_daily(http: CachedHttp, lat: float, lon: float, start: str, end: str) -> DailySeries:
    params = {"latitude": round(lat, 4), "longitude": round(lon, 4), "start_date": start, "end_date": end,
              "daily": ",".join(DAILY_VARS), "timezone": "auto", "models": ARCHIVE_MODEL}
    res = http.get_json("open-meteo-archive", ARCHIVE_URL, params, ttl_s=history_ttl(end),
                        before=BUDGET.before, after=BUDGET.after)
    series = parse_archive(res.data, start, end)
    series.meta.update(fetched_at=res.fetched_at, from_cache=res.from_cache)
    return series
