"""NWS active alerts (api.weather.gov). Real-time only; never part of the risk score (D6).

Verified 2026-11-25: a User-Agent is required (403 without one), and a point outside NWS
coverage returns 400 "out of bounds", which we surface as OutsideNwsCoverage.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.data.http import CachedHttp, UpstreamError

NWS_URL = "https://api.weather.gov/alerts/active"
TTL_S = 300  # alerts change quickly; cache 5 minutes


class OutsideNwsCoverage(Exception):
    pass


@dataclass(frozen=True)
class Alert:
    event: str
    severity: str | None
    urgency: str | None
    certainty: str | None
    headline: str | None
    onset: str | None
    expires: str | None
    area: str | None
    sender: str | None


def parse_alerts(data: dict[str, Any]) -> list[Alert]:
    out = []
    for f in data.get("features") or []:
        p = f.get("properties", {})
        out.append(Alert(event=p.get("event", "?"), severity=p.get("severity"), urgency=p.get("urgency"),
                         certainty=p.get("certainty"), headline=p.get("headline"), onset=p.get("onset"),
                         expires=p.get("expires") or p.get("ends"), area=p.get("areaDesc"),
                         sender=p.get("senderName")))
    return out


def active_alerts(http: CachedHttp, lat: float, lon: float) -> list[Alert]:
    params = {"point": f"{round(lat, 4)},{round(lon, 4)}"}
    try:
        res = http.get_json("nws-alerts", NWS_URL, params, ttl_s=TTL_S,
                            headers={"Accept": "application/geo+json"})
    except UpstreamError as e:
        if e.status == 400:
            raise OutsideNwsCoverage(e.message) from e
        raise
    return parse_alerts(res.data)
