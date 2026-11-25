"""FEMA National Risk Index (county level) via FEMA's ArcGIS feature service.

Verified 2026-11-25 (PLAN.md §7.1): NRI_VER "December 2025", 3,232 counties, max 2,000 rows per
page. `<HAZ>_AFREQ` is null exactly when the hazard rating is "Not Applicable" (D17), so callers
can treat null as zero frequency. The flood field is IFLD (inland flooding), not RFLD.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.data.http import CachedHttp

NRI_URL = ("https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/"
           "National_Risk_Index_Counties/FeatureServer/0")
# CWAV is stored for transparency but excluded from scoring (D18: relative to local norms).
HAZARDS = ("WNTW", "ISTM", "CWAV", "HRCN", "IFLD", "CFLD", "TRND", "HAIL", "SWND", "HWAV")
ID_FIELDS = ("STCOFIPS", "STATEABBRV", "COUNTY", "NRI_VER")
FIELDS = ID_FIELDS + tuple(f"{h}_AFREQ" for h in HAZARDS) + tuple(f"{h}_RISKR" for h in HAZARDS)
PAGE = 2000
TTL_S = 30 * 86_400


@dataclass(frozen=True)
class NriCounty:
    stcofips: str
    state: str
    county: str
    nri_ver: str
    afreq: dict[str, float | None]      # hazard code -> annualized frequency (events/yr); None = not applicable
    rating: dict[str, str | None]       # hazard code -> NRI risk rating (used only for null semantics)

    def frequency(self, hazard: str) -> float:
        """Annualized frequency, with 'Not Applicable' mapped to 0 (D17)."""
        v = self.afreq.get(hazard)
        return 0.0 if v is None else float(v)


def parse_county(a: dict[str, Any]) -> NriCounty:
    return NriCounty(
        stcofips=str(a["STCOFIPS"]), state=a["STATEABBRV"], county=a["COUNTY"], nri_ver=a["NRI_VER"],
        afreq={h: a.get(f"{h}_AFREQ") for h in HAZARDS},
        rating={h: a.get(f"{h}_RISKR") for h in HAZARDS})


def county_for_point(http: CachedHttp, lat: float, lon: float) -> NriCounty:
    params = {"geometry": f"{round(lon, 5)},{round(lat, 5)}", "geometryType": "esriGeometryPoint",
              "inSR": 4326, "spatialRel": "esriSpatialRelIntersects", "outFields": ",".join(FIELDS),
              "returnGeometry": "false", "f": "json"}
    res = http.get_json("fema-nri", f"{NRI_URL}/query", params, ttl_s=TTL_S)
    if "error" in res.data:
        raise RuntimeError(f"NRI query error: {res.data['error']}")
    feats = res.data.get("features") or []
    if not feats:
        raise LookupError(f"no NRI county contains point ({lat}, {lon})")
    return parse_county(feats[0]["attributes"])


def fetch_all_counties(http: CachedHttp) -> list[NriCounty]:
    out: list[NriCounty] = []
    offset = 0
    while True:
        params = {"where": "1=1", "outFields": ",".join(FIELDS), "returnGeometry": "false",
                  "orderByFields": "STCOFIPS", "resultOffset": offset, "resultRecordCount": PAGE, "f": "json"}
        res = http.get_json("fema-nri", f"{NRI_URL}/query", params, ttl_s=TTL_S)
        if "error" in res.data:
            raise RuntimeError(f"NRI query error: {res.data['error']}")
        feats = res.data.get("features") or []
        out.extend(parse_county(f["attributes"]) for f in feats)
        if not res.data.get("exceededTransferLimit") or not feats:
            return out
        offset += len(feats)


def store_counties(conn: sqlite3.Connection, counties: list[NriCounty]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        "INSERT OR REPLACE INTO nri_counties(stcofips, state, county, nri_ver, attrs, loaded_at) VALUES (?,?,?,?,?,?)",
        [(c.stcofips, c.state, c.county, c.nri_ver, json.dumps({"afreq": c.afreq, "rating": c.rating}), now)
         for c in counties])
    conn.commit()


def load_counties(conn: sqlite3.Connection) -> list[NriCounty]:
    rows = conn.execute("SELECT stcofips, state, county, nri_ver, attrs FROM nri_counties").fetchall()
    out = []
    for r in rows:
        attrs = json.loads(r["attrs"])
        out.append(NriCounty(r["stcofips"], r["state"], r["county"], r["nri_ver"], attrs["afreq"], attrs["rating"]))
    return out
