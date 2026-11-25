"""The agent's tools: strict JSON-schema specs + deterministic implementations (PLAN.md §3.1, D24).

Every data tool takes place names exactly as the user wrote them and resolves them with the shared
resolver; every result is compact, display-ready JSON (numbers already rounded, with units, D23).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
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
LIVE_PROFILE_DAILY_CAP = 25    # D49 (measured): each live non-hub profile costs 156.6 Open-Meteo calls (10k/day free)
MAX_HUB_STAT_DAYS = 10958      # D46: hubs up to 30 years from the NOAA station (no quota)
MAX_GRID_STAT_DAYS = 3653      # D46: places without a station (grid model only) up to 10 years
STATION_MIN_COVERAGE = 0.95
# D45: no data-lag constant. Windows are trimmed to the last day the source actually has data.
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
    top_n: int | None = None


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

DEFAULT_TOP_N = 5   # D54: all-hub rankings return the top 5 unless the model asks for more


def confidence_line(level: str, reason: str | None, basis_if_high: str) -> str:
    """D54(1): a ready line the model can copy; the validator checks it either way (D43, D52)."""
    return f"Confidence: {level} - {reason if level != 'high' and reason else basis_if_high}"


def rank_hubs(ctx: ToolContext, a: RankArgs) -> dict:
    r = ctx.scoring.rank(a.hazard, a.region)
    n = a.top_n or (DEFAULT_TOP_N if a.region is None else len(r.entries))
    shown = r.entries[:n]
    entries = []
    for e in shown:
        item: dict[str, Any] = {"rank": e.rank, "hub": e.key, "score": fmt_score(e.score),
                                "confidence": e.confidence, "driver": e.top_driver}
        if a.hazard == "overall":
            item["dominant_hazard"] = e.dominant_hazard
        if e.extreme_hazards:
            item["extreme_hazards"] = e.extreme_hazards
        if e.preparedness_note:
            item["preparedness_note"] = e.preparedness_note
        if e.confidence_reason:
            item["confidence_reason"] = e.confidence_reason
        if e.weight_sensitive:
            item["weight_sensitive"] = f"rank {e.rank_equal_weights} with equal weights"
        entries.append(item)
    keys = {e.key for e in shown}
    out: dict[str, Any] = {"hazard": a.hazard, "region": a.region or "all hubs",
                           "shown": f"top {len(shown)} of {len(r.entries)}", "ranking": entries}
    ties = [[k for k in g if k in keys] for g in r.tie_groups]
    if any(len(g) > 1 for g in ties):
        out["tie_groups"] = [g for g in ties if len(g) > 1]
    level = min((e["confidence"] for e in entries[:3]), key=lambda c: _LEVELS[c], default="high")
    reason = next((f"{e['hub']}: {e['confidence_reason']}" for e in entries[:3]
                   if e["confidence"] == level and e.get("confidence_reason")), None)
    out["confidence"] = level
    if reason and level != "high":
        out["confidence_reason"] = reason
    out["confidence_line"] = confidence_line(level, reason, "FEMA county data and observed weather agree for the top hubs")
    return out


def _profile(s: LocationScore, rank_overall: dict[str, str]) -> dict:
    hazards = {}
    for h in HAZARDS:
        hr = s.hazards[h]
        hazards[h] = {"score": fmt_score(hr.score)}
        if hr.confidence != "high":
            hazards[h]["confidence"] = hr.confidence
            if hr.reasons:
                hazards[h]["confidence_reason"] = "; ".join(hr.reasons)   # D52: the conflicting facts
        if fmt_score(hr.score) > 0:  # drivers of a zero score are noise (and cost tokens)
            hazards[h]["drivers"] = [f"{k.label}: {k.display}" for k in hr.top_drivers(2)]
    out: dict[str, Any] = {"wdri": fmt_score(s.wdri), "dominant_hazard": s.dominant_hazard,
                           "confidence": s.confidence, "hazards": hazards}
    if s.extreme_hazards:
        out["extreme_hazards"] = s.extreme_hazards     # D37: score >= 80, targeted-upgrade candidate
    if s.preparedness_notes:
        out["preparedness_notes"] = s.preparedness_notes  # D34/H1: explanation only, never changes the score
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
    out: dict[str, Any] = {"profiles": profiles, "data": f"{ctx.scoring.cfg.baseline_window['start'][:4]}-"
                                                         f"{ctx.scoring.cfg.baseline_window['end'][:4]}"}
    if len(scored) >= 2:   # computed comparison, one line per hazard, linear in the number of places (D24)
        comp = {}
        for h in ("wdri",) + HAZARDS:
            vals = [(name, fmt_score(s.wdri if h == "wdri" else s.hazards[h].score)) for name, s in scored]
            hi, lo = max(vals, key=lambda x: x[1]), min(vals, key=lambda x: x[1])
            comp[h] = f"highest {hi[0]} {hi[1]}, lowest {lo[0]} {lo[1]}, gap {hi[1] - lo[1]}"
        out["comparison"] = comp
    if scored:
        weakest_name, weakest = min(scored, key=lambda x: _LEVELS[x[1].confidence])
        level = weakest.confidence
        reason = f"{weakest_name}: {weakest.confidence_reasons[0]}" if weakest.confidence_reasons and level != "high" else None
        out["confidence"] = level
        if reason:
            out["confidence_reason"] = reason
        out["confidence_line"] = confidence_line(level, reason, "FEMA county data and observed weather agree")
    return out


def trim_to_data(series: DailySeries, var: str) -> tuple[DailySeries, str | None]:
    """D45: cut trailing days that have no data yet (sources lag real time by ~7-10 days).
    Returns (trimmed series, last date with data) — (unchanged, None) if there is no data at all."""
    vals = series.values.get(var, [])
    last = max((i for i, v in enumerate(vals) if v is not None), default=None)
    if last is None:
        return series, None
    return series.window(series.dates[0], series.dates[last]), series.dates[last]


def covered_tail(series: DailySeries, var: str, min_cov: float = STATION_MIN_COVERAGE,
                 min_days: int = 365) -> DailySeries | None:
    """D70: the longest recent span (starting on a year boundary) where the station really measured `var`.
    E.g. Denver airport has no snowfall records 1996-2005, so a 1996-2025 question uses 2006-2025."""
    vals = series.values.get(var, [])
    for year in sorted({d[:4] for d in series.dates}):
        i = next(k for k, d in enumerate(series.dates) if d[:4] == year)
        tail = vals[i:]
        if len(tail) >= min_days and sum(v is not None for v in tail) / len(tail) >= min_cov:
            return series.window(series.dates[i], series.dates[-1])
    return None


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
    end = min(end, ctx.today)
    if start > end:
        raise ToolError("start_date is after end_date (or in the future)")
    days = (end - start).days + 1
    if days > MAX_HUB_STAT_DAYS:
        raise ToolError("window too long: max 30 years for hubs, 10 years for other places")
    s, e = start.isoformat(), end.isoformat()
    in_baseline = BASELINE_START <= s and e <= BASELINE_END
    m = METRICS[a.metric]
    reg = hub_registry()
    results = []
    worst = "high"
    reason: str | None = None
    for r in _resolve_places(ctx, a.places):
        if not r.usable:
            results.append(_unresolved_entry(r))
            continue
        hub = reg.get(r.hub_id) if r.hub_id else None
        lat, lon = (hub.lat, hub.lon) if hub else (r.lat, r.lon)
        entry: dict[str, Any] = {"place": r.public()}
        # Grid model: free from the cached 2016-2025 baseline; outside it only for non-hubs (<= 10 years), because
        # hubs have the station (D46) and a long grid download costs ~470 Open-Meteo calls for 30 years.
        grid_block = None
        if in_baseline:
            grid = open_meteo.archive_daily(ctx.http, lat, lon, BASELINE_START, BASELINE_END).window(s, e)  # type: ignore[arg-type]
        elif hub is None and days <= MAX_GRID_STAT_DAYS:
            grid = open_meteo.archive_daily(ctx.http, lat, lon, s, e)  # type: ignore[arg-type]
        else:
            grid = None
        if grid is not None:
            grid, grid_last = trim_to_data(grid, m.var)
            if grid_last:
                grid_block = {"source": "Open-Meteo ERA5 grid model (~10-25 km cells)",
                              "data_through": grid_last, "counts": _counts(grid, m, False)}
        primary = None
        if hub and m.station:
            stn, stn_last = trim_to_data(ghcn.station_daily(ctx.http, hub.station, s, e), m.var)
            cov = stn.coverage(m.var)
            if stn_last and cov < STATION_MIN_COVERAGE:
                tail = covered_tail(stn, m.var)      # D70: use only the years the station actually measured
                if tail is not None:
                    entry["station_note"] = (f"{hub.station_name} has no {m.var} records before {tail.dates[0]}; "
                                             f"counts cover {tail.dates[0]} to {stn_last} only")
                    stn, cov = tail, tail.coverage(m.var)
            if stn_last and cov >= STATION_MIN_COVERAGE:
                primary = {"source": f"NOAA weather station: {hub.station_name} ({hub.station})",
                           "period": f"{stn.dates[0]} to {stn_last}", "data_coverage": fmt_percent(100 * cov, 0),
                           "counts": _counts(stn, m, True)}
            else:
                entry["station_note"] = (f"{hub.station_name} reports this on only {fmt_percent(100 * cov, 0)} of days; "
                                         "using the grid model")
        if hub is None and days > MAX_GRID_STAT_DAYS:
            results.append({**entry, "note": "max 10 years for places that aren't hubs (no weather station data)"})
            continue
        if primary:
            entry["primary"] = primary
            entry["grid_model_for_comparison"] = grid_block or "not available outside 2016-2025"
        elif grid_block:
            entry["primary"] = grid_block
            if a.metric == "snow_days":
                worst = "medium"
                reason = ("no station snowfall measurements here, so the count comes from the grid model, which "
                          "tends to overcount light-snow days")
        else:
            results.append({**entry, "note": "NO DATA for this window: this is not zero; do not report a count"})
            continue
        entry["window"] = {"start": s, "requested_end": e}
        if a.metric == "snow_days":
            entry["caveat"] = ("grid-model snow converts snow water to depth at a fixed 7:1 ratio and tends to "
                               "overcount light-snow days compared with station measurements")
        results.append(entry)
    out: dict[str, Any] = {"metric": a.metric, "results": results, "confidence": worst}
    if reason:
        out["confidence_reason"] = reason
    out["confidence_line"] = confidence_line(worst, reason, "measured at the airport weather station")
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
            "confidence_rules": ("lowered when data is missing, when FEMA county data and observed weather rank a hub "
                           f">= {cfg.confidence.disagreement_rank_gap} places apart, or for known location caveats"),
            "ties": f"hubs within {fmt_score(cfg.ranking.tie_points)} points are not meaningfully different",
            "flags": (f"extreme hazard = any hazard scoring >= {fmt_score(cfg.flags.extreme_hazard_score)} "
                      "(targeted-upgrade candidate); winter 'rare here' note = the region is less prepared for "
                      "winter, so each event likely disrupts more. Flags never change a score."),
            "known_weaknesses": ["hurricane frequency counts events, not intensity: Boston's county equals "
                                 "Houston's (storm-track intensity data not included)",
                                 "FEMA data is county-level: large or mountainous counties can inflate flood or "
                                 "winter frequencies (flagged as low confidence)"],
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

PLACES_SCHEMA = {   # D54(5): descriptions kept short; the prompt carries the usage rules
    "type": "array",
    "items": {"type": "object", "additionalProperties": False, "required": ["query", "interpreted_from"],
              "properties": {"query": {"type": "string"}, "interpreted_from": {"type": ["string", "null"]}}},
}
NULLABLE_REGION = {"type": ["string", "null"], "enum": ["northeast", "midwest", "south", "west", None]}
HAZARD_ENUM = {"type": "string", "enum": ["overall", *HAZARDS]}


def _fn(name: str, desc: str, props: dict) -> dict:
    return {"type": "function", "function": {"name": name, "description": desc, "strict": True, "parameters": {
        "type": "object", "additionalProperties": False, "properties": props, "required": list(props)}}}


TOOL_SPECS = [
    _fn("rank_hubs", "Rank hubs by score for a hazard or overall; optional Census region; top_n (default 5 for all hubs).",
        {"hazard": HAZARD_ENUM, "region": NULLABLE_REGION, "top_n": {"type": ["integer", "null"]}}),
    _fn("get_risk_profile", "Scores, drivers and confidence for 1-4 places; 2+ places adds a comparison.",
        {"places": PLACES_SCHEMA}),
    _fn("get_weather_stat", "Days meeting a weather condition at 1-4 places in a date range (YYYY-MM-DD). "
        "Max 30 years for hubs, 10 otherwise.",
        {"places": PLACES_SCHEMA, "metric": {"type": "string", "enum": list(METRICS)},
         "start_date": {"type": "string"}, "end_date": {"type": "string"}}),
    _fn("get_active_alerts", "Live NWS alerts for 1-4 places.", {"places": PLACES_SCHEMA}),
    _fn("get_methodology", "How scores are calculated (weights, KPIs, scales, confidence).", {"topic": HAZARD_ENUM}),
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
