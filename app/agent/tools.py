"""The agent's tools: strict JSON-schema specs + deterministic implementations (PLAN.md §3.1, D24).

Every data tool takes place names exactly as the user wrote them and resolves them with the shared
resolver; every result is compact, display-ready JSON (numbers already rounded, with units, D23).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import BASELINE_END, BASELINE_START, hub_registry
from app.data import ghcn, nws, open_meteo
from app.data.http import CachedHttp
from app.data.series import GUST_MAX, PRECIP, SNOWFALL, TMAX, TMIN, DailySeries
from app.geo.resolver import Resolution, resolve
from app.scoring.engine import HAZARDS, LocationScore
from app.scoring.kpis import days_meeting
from app.scoring.normalize import fmt_percent, fmt_score
from app.scoring.service import ScoringService
from app.store.db import bump_counter

MAX_PLACES = 4                 # D24: measured token budget (~280 tokens per profile)
LIVE_PROFILE_DAILY_CAP = 25    # each live non-hub profile costs ~157 Open-Meteo calls (10k/day free)
MAX_STAT_DAYS = 3653           # weather-stat window <= 10 years (bounds cost)
DATA_LAG_DAYS = 6              # ERA5 and station data trail real time by several days
STATION_MIN_COVERAGE = 0.95
_LEVELS = {"high": 2, "medium": 1, "low": 0}

HazardArg = Literal["overall", "winter", "hurricane", "flood", "severe_convective", "heat"]
RegionArg = Literal["northeast", "midwest", "south", "west"]
MetricArg = Literal["snow_days", "precip_days", "heavy_precip_days", "freezing_days", "hot_days", "high_gust_days"]
TopicArg = Literal["overall", "winter", "hurricane", "flood", "severe_convective", "heat"]


# ----------------------------------------------------------------------------- argument models (validation)

class Place(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str
    interpreted_from: str | None


class RankArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hazard: HazardArg
    region: RegionArg | None


class ProfileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    places: list[Place]


class StatArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    places: list[Place]
    metric: MetricArg
    start_date: str = Field(description="ISO date YYYY-MM-DD")
    end_date: str = Field(description="ISO date YYYY-MM-DD")


class AlertArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    places: list[Place]


class MethodArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topic: TopicArg


# ----------------------------------------------------------------------------- weather-stat metric definitions
# (label, station threshold in station units, grid threshold in grid units)

@dataclass(frozen=True)
class Metric:
    var: str
    op: Literal[">=", "<="]
    thresholds: tuple[tuple[str, float | None, float], ...]
    station: bool = True


METRICS: dict[str, Metric] = {
    "snow_days": Metric(SNOWFALL, ">=", (("measurable snow (>= 0.1 in)", 0.1, 0.254), ("snow >= 1 in", 1.0, 2.54))),
    "precip_days": Metric(PRECIP, ">=", (("measurable precipitation (>= 0.01 in)", 0.01, 0.254),)),
    "heavy_precip_days": Metric(PRECIP, ">=", (("rain >= 2 in", 2.0, 50.8),)),
    "freezing_days": Metric(TMIN, "<=", (("low <= 32F", 32.0, 0.0),)),
    "hot_days": Metric(TMAX, ">=", (("high >= 95F", 95.0, 35.0),)),
    "high_gust_days": Metric(GUST_MAX, ">=", (("gusts >= 47 mph", None, 75.0),), station=False),
}


# ----------------------------------------------------------------------------- context

@dataclass
class ToolContext:
    http: CachedHttp
    scoring: ScoringService
    today: date
    clock_day: Callable[[], str] = field(default=lambda: date.today().isoformat())


class ToolError(Exception):
    """An error the model should see and can act on (bad arguments, limits)."""


def _resolve_places(ctx: ToolContext, places: list[Place]) -> list[Resolution]:
    if not places:
        raise ToolError("give at least one place")
    if len(places) > MAX_PLACES:
        raise ToolError(f"max {MAX_PLACES} places per call; use rank_hubs for a whole region, or split the call")
    return [resolve(ctx.http, p.query, p.interpreted_from) for p in places]


def _unresolved_entry(r: Resolution) -> dict:
    hint = {"ambiguous": "ask the user which one they mean",
            "not_found": "not found; you may interpret a landmark (set interpreted_from) or ask for a US city",
            "non_us": "outside the US: out of scope"}.get(r.status, "")
    return {"place": r.public(), "note": hint}


# ----------------------------------------------------------------------------- tools

def rank_hubs(ctx: ToolContext, a: RankArgs) -> dict:
    r = ctx.scoring.rank(a.hazard, a.region)
    entries = []
    for e in r.entries:
        item = {"rank": e.rank, "hub": e.key, "label": e.label, "score": fmt_score(e.score),
                "confidence": e.confidence, "driver": e.top_driver}
        if a.hazard == "overall":
            item["dominant_hazard"] = e.dominant_hazard
        entries.append(item)
    out: dict[str, Any] = {
        "hazard": a.hazard, "region": a.region or "all hubs",
        "region_definition": "US Census Bureau regions" if a.region else None,
        "scale": "0-100, higher = more exposed",
        "ranking": entries,
        "tie_groups": r.tie_groups,
        "tie_rule": f"within {fmt_score(ctx.scoring.cfg.ranking.tie_points)} points = not meaningfully different",
    }
    if a.hazard == "overall":
        sens = [f"{e.key} (rank {e.rank}; {e.rank_equal_weights} with equal weights)" for e in r.entries if e.weight_sensitive]
        out["weight_sensitive"] = sens
    out["confidence"] = min((e["confidence"] for e in entries[:3]), key=lambda c: _LEVELS[c], default="high")
    return {k: v for k, v in out.items() if v is not None}


def _profile(s: LocationScore, rank_overall: dict[str, str]) -> dict:
    hazards = {}
    for h in HAZARDS:
        hr = s.hazards[h]
        hazards[h] = {"score": fmt_score(hr.score), "confidence": hr.confidence}
        if fmt_score(hr.score) > 0:  # drivers of a zero score are noise (and cost tokens)
            hazards[h]["drivers"] = [f"{k.label}: {k.display}" for k in hr.top_drivers(2)]
    out = {"wdri": fmt_score(s.wdri), "dominant_hazard": s.dominant_hazard, "confidence": s.confidence,
           "confidence_reasons": s.confidence_reasons[:3], "hazards": hazards,
           "county": s.county, "data_window": s.data_window}
    if s.hub_id:
        out["rank_overall"] = rank_overall.get(s.hub_id)
    return out


def get_risk_profile(ctx: ToolContext, a: ProfileArgs) -> dict:
    resolutions = _resolve_places(ctx, a.places)
    overall = ctx.scoring.rank("overall")
    rank_overall = {e.key: f"{e.rank} of {len(overall.entries)}" + (" (weight-sensitive)" if e.weight_sensitive else "")
                    for e in overall.entries}
    reg = hub_registry()
    profiles, scored = [], []
    for r in resolutions:
        if not r.usable:
            profiles.append(_unresolved_entry(r))
            continue
        if r.hub_id:
            s = ctx.scoring.hub_score(reg.get(r.hub_id))  # type: ignore[arg-type]
        else:
            if not bump_counter(ctx.http.conn, ctx.clock_day(), "live_profile", LIVE_PROFILE_DAILY_CAP):
                profiles.append({"place": r.public(), "note": "daily limit for non-hub profiles reached; try tomorrow"})
                continue
            s = ctx.scoring.score_point(r.lat, r.lon, f"{r.lat:.3f},{r.lon:.3f}", r.label or r.query)  # type: ignore[arg-type]
        profiles.append({"place": r.public(), **_profile(s, rank_overall)})
        scored.append((r.hub_id or (r.label or r.query), s))
    out: dict[str, Any] = {"profiles": profiles, "scale": "0-100, higher = more exposed",
                           "weights": {h: w for h, w in ctx.scoring.cfg.hazard_weights.items()}}
    if len(scored) >= 2:
        comp = {}
        for h in ("wdri",) + HAZARDS:
            vals = [(name, fmt_score(s.wdri if h == "wdri" else s.hazards[h].score)) for name, s in scored]
            hi, lo = max(vals, key=lambda x: x[1]), min(vals, key=lambda x: x[1])
            comp[h] = {"highest": f"{hi[0]} {hi[1]}", "lowest": f"{lo[0]} {lo[1]}", "gap": hi[1] - lo[1]}
        out["comparison"] = comp
    levels = [p["confidence"] for p in profiles if "confidence" in p]
    out["confidence"] = min(levels, key=lambda c: _LEVELS[c]) if levels else None
    return out


def _counts(series: DailySeries, m: Metric, station: bool) -> list[dict]:
    rows = []
    for label, st_thr, grid_thr in m.thresholds:
        thr = st_thr if station else grid_thr
        if thr is None:
            continue
        hits, present = days_meeting(series.values.get(m.var, []), m.op, thr)
        rows.append({"threshold": label, "days": hits,
                     "percent": fmt_percent(100 * hits / present) if present else "no data"})
    return rows


def get_weather_stat(ctx: ToolContext, a: StatArgs) -> dict:
    try:
        start, end = date.fromisoformat(a.start_date), date.fromisoformat(a.end_date)
    except ValueError as e:
        raise ToolError(f"dates must be YYYY-MM-DD: {e}") from e
    notes = []
    latest = ctx.today - timedelta(days=DATA_LAG_DAYS)
    if end > latest:
        end = latest
        notes.append(f"end date moved to {end.isoformat()} (data trails real time by ~{DATA_LAG_DAYS} days)")
    if start > end:
        raise ToolError("start_date is after the latest available data or after end_date")
    if (end - start).days + 1 > MAX_STAT_DAYS:
        raise ToolError("window too long: max 10 years per call")
    s, e = start.isoformat(), end.isoformat()
    m = METRICS[a.metric]
    reg = hub_registry()
    results = []
    worst = "high"
    for r in _resolve_places(ctx, a.places):
        if not r.usable:
            results.append(_unresolved_entry(r))
            continue
        hub = reg.get(r.hub_id) if r.hub_id else None
        # Grid model: reuse the cached 10-year baseline when the window fits inside it (no extra API calls).
        lat, lon = (hub.lat, hub.lon) if hub else (r.lat, r.lon)
        if BASELINE_START <= s and e <= BASELINE_END:
            grid = open_meteo.archive_daily(ctx.http, lat, lon, BASELINE_START, BASELINE_END).window(s, e)  # type: ignore[arg-type]
        else:
            grid = open_meteo.archive_daily(ctx.http, lat, lon, s, e)  # type: ignore[arg-type]
        entry: dict[str, Any] = {"place": r.public(), "window": {"start": s, "end": e, "days": len(grid.dates)}}
        grid_block = {"source": "Open-Meteo ERA5 grid model (~10-25 km cells)", "counts": _counts(grid, m, False)}
        primary = None
        if hub and m.station:
            stn = ghcn.station_daily(ctx.http, hub.station, s, e)
            cov = stn.coverage(m.var)
            if cov >= STATION_MIN_COVERAGE:
                primary = {"source": f"NOAA weather station: {hub.station_name} ({hub.station})",
                           "data_coverage": fmt_percent(100 * cov, 0), "counts": _counts(stn, m, True)}
            else:
                entry["station_note"] = (f"{hub.station_name} reports this on only {fmt_percent(100 * cov, 0)} of days; "
                                         "using the grid model")
        if primary:
            entry["primary"], entry["grid_model_for_comparison"] = primary, grid_block
        else:
            entry["primary"] = grid_block
            worst = "medium" if a.metric == "snow_days" else worst
        if a.metric == "snow_days":
            entry["caveat"] = ("grid-model snow converts snow water to depth at a fixed 7:1 ratio and tends to "
                               "overcount light-snow days compared with station measurements")
        results.append(entry)
    out: dict[str, Any] = {"metric": a.metric, "results": results}
    if notes:
        out["notes"] = notes
    out["confidence"] = worst
    return out


def get_active_alerts(ctx: ToolContext, a: AlertArgs) -> dict:
    results = []
    for r in _resolve_places(ctx, a.places):
        if not r.usable:
            results.append(_unresolved_entry(r))
            continue
        try:
            alerts = nws.active_alerts(ctx.http, r.lat, r.lon)  # type: ignore[arg-type]
        except nws.OutsideNwsCoverage:
            results.append({"place": r.public(), "note": "outside NWS coverage"})
            continue
        results.append({"place": r.public(), "active_alert_count": len(alerts), "alerts": [
            {"event": al.event, "severity": al.severity, "urgency": al.urgency,
             "onset": (al.onset or "")[:16], "expires": (al.expires or "")[:16], "headline": (al.headline or "")[:140]}
            for al in alerts[:5]]})
    return {"source": "NWS active alerts (api.weather.gov), live", "results": results,
            "note": "Current alerts never change the long-term risk score."}


def get_methodology(ctx: ToolContext, a: MethodArgs) -> dict:
    cfg = ctx.scoring.cfg
    if a.topic == "overall":
        return {
            "index": "Weather Disruption Risk Index (WDRI), 0-100 = weighted sum of 5 hazard scores",
            "hazard_weights": cfg.hazard_weights,
            "weights_rationale": "by shutdown potential: heat mostly slows operations and rarely closes a hub",
            "normalization": ("observed-weather KPIs: fixed scales set at roughly the worst plausible US metro; "
                              "FEMA KPIs: linear from 0 to the national 95th-percentile county"),
            "confidence": ("lowered when data is missing, when FEMA county data and observed weather rank a hub "
                           f">= {cfg.confidence.disagreement_rank_gap} places apart, or for known location caveats"),
            "ties": f"hubs within {fmt_score(cfg.ranking.tie_points)} points are not meaningfully different",
            "data": f"FEMA National Risk Index (county level) + Open-Meteo ERA5 daily weather, "
                    f"{cfg.baseline_window['start'][:4]}-{cfg.baseline_window['end'][:4]}",
            "not_covered": ["financial loss or downtime cost", "facility specifics (flood zone, elevation)",
                            "hurricane intensity", "future climate trends", "wildfire, earthquake, drought"],
            "config_version": cfg.version}
    h = cfg.hazards[a.topic]
    return {"hazard": a.topic, "label": h.label, "hazard_weight": cfg.hazard_weights[a.topic],
            "kpis": [{"kpi": k.label, "source": "FEMA NRI county frequency" if k.source == "nri" else "observed weather (ERA5)",
                      "weight_in_hazard": k.weight, **({"note": k.note} if k.note else {})} for k in h.kpis],
            "config_version": cfg.version}


# ----------------------------------------------------------------------------- specs + dispatch

PLACES_SCHEMA = {
    "type": "array", "description": f"1-{MAX_PLACES} places, exactly as the user wrote them",
    "items": {"type": "object", "additionalProperties": False, "required": ["query", "interpreted_from"],
              "properties": {"query": {"type": "string"},
                             "interpreted_from": {"type": ["string", "null"],
                                                  "description": "only if you interpreted a not_found place: the user's original words"}}},
}
NULLABLE_REGION = {"type": ["string", "null"], "enum": ["northeast", "midwest", "south", "west", None]}
HAZARD_ENUM = {"type": "string", "enum": ["overall", *HAZARDS]}


def _fn(name: str, desc: str, props: dict) -> dict:
    return {"type": "function", "function": {"name": name, "description": desc, "strict": True, "parameters": {
        "type": "object", "additionalProperties": False, "properties": props, "required": list(props)}}}


TOOL_SPECS = [
    _fn("rank_hubs", "Rank our hubs by risk score (0-100) for one hazard or overall, optionally within a US Census region.",
        {"hazard": HAZARD_ENUM, "region": NULLABLE_REGION}),
    _fn("get_risk_profile", "Risk breakdown for 1-4 places: overall and per-hazard scores, top drivers, confidence. "
        "With 2+ places also returns a computed comparison.", {"places": PLACES_SCHEMA}),
    _fn("get_weather_stat", "Count days meeting a weather condition at 1-4 places over a date range (max 10 years).",
        {"places": PLACES_SCHEMA, "metric": {"type": "string", "enum": list(METRICS)},
         "start_date": {"type": "string", "description": "YYYY-MM-DD"},
         "end_date": {"type": "string", "description": "YYYY-MM-DD"}}),
    _fn("get_active_alerts", "Current NWS weather alerts for 1-4 places (live; never part of the risk score).",
        {"places": PLACES_SCHEMA}),
    _fn("get_methodology", "How the risk score is calculated: weights, KPIs, scales, confidence rules.",
        {"topic": HAZARD_ENUM}),
]

_TOOLS: dict[str, tuple[type[BaseModel], Callable[[ToolContext, Any], dict]]] = {
    "rank_hubs": (RankArgs, rank_hubs),
    "get_risk_profile": (ProfileArgs, get_risk_profile),
    "get_weather_stat": (StatArgs, get_weather_stat),
    "get_active_alerts": (AlertArgs, get_active_alerts),
    "get_methodology": (MethodArgs, get_methodology),
}
DATA_TOOLS = set(_TOOLS)


def run_tool(ctx: ToolContext, name: str, arguments: str) -> tuple[dict, bool]:
    """Execute a tool call. Returns (result, is_error). Errors are returned to the model, never raised."""
    if name not in _TOOLS:
        return {"error": f"unknown tool {name!r}"}, True
    model, fn = _TOOLS[name]
    try:
        args = model.model_validate(json.loads(arguments or "{}"))
    except (json.JSONDecodeError, ValidationError) as e:
        return {"error": f"invalid arguments: {str(e)[:300]}"}, True
    try:
        return fn(ctx, args), False
    except ToolError as e:
        return {"error": str(e)}, True
    except Exception as e:  # noqa: BLE001 - upstream outage etc.: tell the model, don't crash the turn
        return {"error": f"data source unavailable ({type(e).__name__}); say so and suggest retrying later"}, True
