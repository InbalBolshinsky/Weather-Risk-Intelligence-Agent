"""NOAA GHCN-Daily station observations via the NCEI Access Data Service (no token).

Used as ground truth for factual hub questions (PLAN.md D19). Fetched in standard units
(inches, deg F): US stations measure in those units, so the NWS 0.1 in / 1 in snowfall
thresholds apply exactly. Days without a report are None (not 0). Trace amounts come
back as 0, which matches the NWS rule that a trace is not measurable.
"""
from __future__ import annotations

from typing import Any

from app.data.http import CachedHttp, history_ttl
from app.data.series import PRECIP, SNOWFALL, TMAX, TMIN, DailySeries, calendar

GHCN_URL = "https://www.ncei.noaa.gov/access/services/data/v1"
ELEMENTS = {"SNOW": SNOWFALL, "PRCP": PRECIP, "TMAX": TMAX, "TMIN": TMIN}
UNITS = {SNOWFALL: "inch", PRECIP: "inch", TMAX: "°F", TMIN: "°F"}


def _num(v: Any) -> float | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_station(rows: list[dict[str, Any]], station: str, start: str, end: str) -> DailySeries:
    by_date = {r["DATE"]: r for r in rows}
    dates = calendar(start, end)
    values = {canon: [_num(by_date.get(d, {}).get(el)) for d in dates] for el, canon in ELEMENTS.items()}
    first = rows[0] if rows else {}
    meta = {"station": station, "station_name": first.get("NAME"),
            "station_lat": _num(first.get("LATITUDE")), "station_lon": _num(first.get("LONGITUDE"))}
    return DailySeries(f"ghcn:{station}", dates, values, dict(UNITS), meta)


def station_daily(http: CachedHttp, station: str, start: str, end: str) -> DailySeries:
    params = {"dataset": "daily-summaries", "stations": station, "startDate": start, "endDate": end,
              "dataTypes": ",".join(ELEMENTS), "units": "standard", "format": "json",
              "includeStationName": "true", "includeStationLocation": "1"}
    res = http.get_json("noaa-ghcn", GHCN_URL, params, ttl_s=history_ttl(end))
    series = parse_station(res.data, station, start, end)
    series.meta.update(fetched_at=res.fetched_at, from_cache=res.from_cache)
    return series
