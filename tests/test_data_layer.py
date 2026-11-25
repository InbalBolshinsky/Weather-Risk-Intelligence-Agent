"""Offline tests for the data layer (no network): cache, retries, parsing, rate budget, hubs."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.config import hub_registry
from app.data import fema_nri, ghcn, nws, open_meteo
from app.data.http import CachedHttp, UpstreamError
from app.data.series import SNOWFALL
from app.store.db import connect

FIX = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIX / name).read_text())


def make_http(tmp_path, handler) -> tuple[CachedHttp, list]:
    calls: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request, len(calls))

    client = httpx.Client(transport=httpx.MockTransport(wrapped))
    return CachedHttp(connect(tmp_path / "t.db"), client=client, sleep=lambda s: None), calls


# --- HTTP cache ------------------------------------------------------------------

def test_cache_hit_avoids_second_network_call(tmp_path):
    http, calls = make_http(tmp_path, lambda req, n: httpx.Response(200, json={"n": n}))
    a = http.get_json("t", "https://x.test/a", {"q": 1}, ttl_s=None)
    b = http.get_json("t", "https://x.test/a", {"q": 1}, ttl_s=None)
    assert (a.data, a.from_cache) == ({"n": 1}, False)
    assert (b.data, b.from_cache) == ({"n": 1}, True)
    assert len(calls) == 1


def test_expired_entry_is_refetched(tmp_path):
    http, calls = make_http(tmp_path, lambda req, n: httpx.Response(200, json={"n": n}))
    http.get_json("t", "https://x.test/a", {}, ttl_s=-1)       # already expired
    again = http.get_json("t", "https://x.test/a", {}, ttl_s=-1)
    assert again.data == {"n": 2} and len(calls) == 2


def test_retries_transient_errors_then_succeeds(tmp_path):
    http, calls = make_http(tmp_path, lambda req, n: httpx.Response(503) if n < 3 else httpx.Response(200, json={}))
    assert http.get_json("t", "https://x.test/a", {}, ttl_s=None).data == {}
    assert len(calls) == 3


def test_non_retryable_4xx_raises_immediately_and_is_not_cached(tmp_path):
    http, calls = make_http(tmp_path, lambda req, n: httpx.Response(400, text="out of bounds"))
    with pytest.raises(UpstreamError) as e:
        http.get_json("t", "https://x.test/a", {}, ttl_s=None)
    assert e.value.status == 400 and len(calls) == 1
    with pytest.raises(UpstreamError):
        http.get_json("t", "https://x.test/a", {}, ttl_s=None)
    assert len(calls) == 2  # failure was not cached


# --- Open-Meteo ------------------------------------------------------------------

def test_parse_archive_fixture_denver_2025():
    s = open_meteo.parse_archive(load("open_meteo_archive_denver_2025.json"), "2025-01-01", "2025-12-31")
    assert len(s.dates) == 365 and s.coverage(SNOWFALL) == 1.0
    assert s.units[SNOWFALL] == "cm" and s.source == "open-meteo:era5_seamless"


def test_parse_archive_marks_missing_days_as_none():
    data = {"daily": {"time": ["2025-01-01"], "snowfall_sum": [1.0]}, "daily_units": {"snowfall_sum": "cm"}}
    s = open_meteo.parse_archive(data, "2025-01-01", "2025-01-03")
    assert s.values[SNOWFALL] == [1.0, None, None]
    assert s.coverage(SNOWFALL) == pytest.approx(1 / 3)


def test_rate_budget_waits_for_reset_when_cost_exceeds_remaining():
    slept: list[float] = []
    b = open_meteo.RateBudget(sleep=slept.append, clock=lambda: 1000.0)
    b.remaining, b.reset_at = 50.0, 1030.0
    ten_years = {"start_date": "2016-01-01", "end_date": "2025-12-31", "daily": ",".join(["v"] * 6)}
    assert open_meteo.RateBudget.estimate_cost(ten_years) == pytest.approx(156.56, abs=0.1)  # measured: 156.557
    b.before(ten_years)
    assert slept == [31.0]


def test_rate_budget_no_wait_when_budget_unknown_or_sufficient():
    slept: list[float] = []
    b = open_meteo.RateBudget(sleep=slept.append, clock=lambda: 0.0)
    b.before({"start_date": "2025-01-01", "end_date": "2025-12-31", "daily": "a,b"})
    b.remaining, b.reset_at = 500.0, 60.0
    b.before({"start_date": "2025-01-01", "end_date": "2025-12-31", "daily": "a,b"})
    assert slept == []


# --- NOAA station ----------------------------------------------------------------

def test_parse_station_fills_gaps_and_keeps_trace_as_zero():
    rows = [{"DATE": "2025-01-01", "SNOW": " 1.5", "PRCP": "0.10", "TMAX": "30", "TMIN": "10", "NAME": "X"},
            {"DATE": "2025-01-03", "SNOW": "0.0", "PRCP": "", "TMAX": "35", "TMIN": "20"}]
    s = ghcn.parse_station(rows, "USW0", "2025-01-01", "2025-01-03")
    assert s.values[SNOWFALL] == [1.5, None, 0.0]
    assert s.values["precipitation"] == [0.10, None, None]
    assert s.units[SNOWFALL] == "inch" and s.meta["station_name"] == "X"


# --- FEMA NRI --------------------------------------------------------------------

def test_nri_not_applicable_maps_to_zero_frequency():
    attrs = load("nri_points.json")["counties"]
    miami = fema_nri.parse_county(attrs["Miami"])
    assert miami.rating["ISTM"] == "Not Applicable" and miami.afreq["ISTM"] is None
    assert miami.frequency("ISTM") == 0.0
    assert miami.frequency("HRCN") > 0.25 and miami.nri_ver == "December 2025"


def test_nri_store_and_load_roundtrip(tmp_path):
    conn = connect(tmp_path / "t.db")
    c = fema_nri.parse_county(load("nri_points.json")["counties"]["Dallas"])
    fema_nri.store_counties(conn, [c])
    assert fema_nri.load_counties(conn) == [c]


# --- NWS -------------------------------------------------------------------------

def test_nws_parse_sample_alerts():
    alerts = nws.parse_alerts({"features": load("nws_alerts_sample.json")["sample"]})
    assert alerts and all(a.event for a in alerts)


def test_nws_out_of_bounds_becomes_outside_coverage(tmp_path):
    http, _ = make_http(tmp_path, lambda req, n: httpx.Response(400, json={"title": "Invalid Parameter"}))
    with pytest.raises(nws.OutsideNwsCoverage):
        nws.active_alerts(http, 43.65, -79.38)


# --- Hub registry ----------------------------------------------------------------

def test_hub_registry_is_consistent():
    reg = hub_registry()
    assert len(reg.hubs) == 20
    assert len(reg.in_region("midwest")) == 6
    assert reg.get("den").station == "USW00003017"
    aliases = [a for h in reg.hubs for a in h.aliases]
    assert len(aliases) == len(set(aliases)), "an alias maps to two hubs"
