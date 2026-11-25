"""Pre-warm the SQLite cache for all hubs so the app (and the live demo) never depends on
20+ upstream calls succeeding in the moment (PLAN.md §2.2).

    python scripts/refresh_data.py            # NRI county table + every hub
    python scripts/refresh_data.py --hub DEN  # one hub

Open-Meteo history costs ~157 free-tier calls per hub-decade against 600/minute, so a full
run is throttled to roughly 3 hubs per minute; cached history is never re-fetched.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

from app.config import BASELINE_END, BASELINE_START, ROOT, hub_registry  # noqa: E402
from app.scoring.alerts import check_score_changes  # noqa: E402
from app.scoring.service import ScoringService  # noqa: E402
from app.data import fema_nri, ghcn, open_meteo  # noqa: E402
from app.data.http import CachedHttp  # noqa: E402
from app.data.series import SNOWFALL  # noqa: E402
from app.store.db import connect  # noqa: E402

STATION_MIN_COVERAGE = 0.95


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hub", help="refresh only this hub id")
    args = ap.parse_args()

    conn = connect()
    http = CachedHttp(conn)
    reg = hub_registry()
    hubs = [reg.get(args.hub)] if args.hub else list(reg.hubs)
    if None in hubs:
        print(f"unknown hub {args.hub}")
        return 2

    t0 = time.perf_counter()
    counties = fema_nri.fetch_all_counties(http)
    fema_nri.store_counties(conn, counties)
    versions = sorted({c.nri_ver for c in counties})
    print(f"NRI: {len(counties)} counties stored, version(s) {versions}")

    failures = 0
    print(f"\n{'hub':<4} {'county':<24} {'grid elev':>9} {'archive':>8} {'station':<12} "
          f"{'snow cov':>8} {'prcp cov':>8} {'tmax cov':>8}  note")
    for h in hubs:
        try:
            county = fema_nri.county_for_point(http, h.lat, h.lon)
            grid = open_meteo.archive_daily(http, h.lat, h.lon, BASELINE_START, BASELINE_END)
            stn = ghcn.station_daily(http, h.station, BASELINE_START, BASELINE_END)
        except Exception as e:  # noqa: BLE001 - report every hub, don't stop the run
            failures += 1
            print(f"{h.id:<4} FAILED: {type(e).__name__}: {e}")
            continue
        grid_nulls = sum(v is None for vals in grid.values.values() for v in vals)
        snow_cov = stn.coverage(SNOWFALL)
        note = "" if snow_cov >= STATION_MIN_COVERAGE else "snow -> grid-model fallback"
        if grid_nulls:
            note += f" grid nulls={grid_nulls}"
        print(f"{h.id:<4} {county.county + ', ' + county.state:<24} {grid.meta['elevation_m']:>8}m "
              f"{'cache' if grid.meta.get('from_cache') else 'fetched':>8} {h.station:<12} "
              f"{snow_cov:>8.0%} {stn.coverage('precipitation'):>8.0%} {stn.coverage('tmax'):>8.0%}  {note}")

    print(f"\ndone in {time.perf_counter() - t0:.0f}s; {failures} hub(s) failed")
    if failures or args.hub:
        return 1 if failures else 0
    # D76: fresh data can change scores: alert on meaningful changes and update the baseline
    from check_scores import report  # noqa: E402 - same folder
    load_dotenv(ROOT / ".env")
    print("\nscore-change check:")
    report(check_score_changes(ScoringService(http), os.getenv("ALERT_WEBHOOK_URL")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
