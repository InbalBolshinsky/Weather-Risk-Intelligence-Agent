"""Deterministic scoring engine: KPIs -> hazard scores -> WDRI, confidence, ranking (PLAN.md §4).

Pure functions over already-fetched data (a grid DailySeries + an NRI county + national scales),
so everything here is testable offline and identical with or without the LLM (D1).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Literal

import yaml
from pydantic import BaseModel, model_validator

from app.config import CONFIG_DIR
from app.data.fema_nri import NriCounty
from app.data.series import DailySeries
from app.scoring.kpis import annual_days, nri_frequency
from app.scoring.normalize import fmt_value, linear_to_scale, percentile_value, piecewise_linear

HAZARDS = ("winter", "hurricane", "flood", "severe_convective", "heat")
Hazard = Literal["winter", "hurricane", "flood", "severe_convective", "heat"]
Level = Literal["high", "medium", "low"]
_LEVEL_ORDER = {"high": 2, "medium": 1, "low": 0}


def worst(levels: list[Level]) -> Level:
    return min(levels, key=lambda lv: _LEVEL_ORDER[lv]) if levels else "high"


# ----------------------------------------------------------------------------- config

class KpiDef(BaseModel):
    id: str
    label: str
    source: Literal["observed", "nri"]
    unit: str
    weight: float
    var: str | None = None
    op: Literal[">=", "<="] | None = None
    threshold: float | None = None
    anchors: list[list[float]] | None = None
    codes: list[str] | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "KpiDef":
        if self.source == "observed" and not (self.var and self.op and self.threshold is not None and self.anchors):
            raise ValueError(f"observed KPI {self.id} needs var, op, threshold, anchors")
        if self.source == "nri" and not self.codes:
            raise ValueError(f"NRI KPI {self.id} needs codes")
        return self


class HazardDef(BaseModel):
    label: str
    kpis: list[KpiDef]
    cross_check: bool = True   # run the FEMA-vs-observed rank check for this hazard


class ConfidenceCfg(BaseModel):
    min_coverage: float
    disagreement_rank_gap: int
    overall_min_contribution: float
    hub_caveats: dict[str, dict[str, str]] = {}


class RankingCfg(BaseModel):
    tie_points: float
    weight_sensitive_rank_shift: int


class WinterRarityCfg(BaseModel):
    max_snow_days: float
    max_ice_days: float


class FlagsCfg(BaseModel):
    extreme_hazard_score: float
    winter_rarity: WinterRarityCfg


class DisplayCfg(BaseModel):
    score: int
    days_per_year: int
    percent: int
    frequency_sig_figs: int


class ScoringConfig(BaseModel):
    baseline_window: dict[str, str]
    hazard_weights: dict[str, float]
    hazards: dict[str, HazardDef]
    nri_scale_percentile: float
    flags: FlagsCfg
    confidence: ConfidenceCfg
    ranking: RankingCfg
    display: DisplayCfg
    version: str = ""

    @model_validator(mode="after")
    def _check(self) -> "ScoringConfig":
        if set(self.hazard_weights) != set(HAZARDS) or set(self.hazards) != set(HAZARDS):
            raise ValueError(f"hazards must be exactly {HAZARDS}")
        if abs(sum(self.hazard_weights.values()) - 1.0) > 1e-9:
            raise ValueError("hazard weights must sum to 1")
        return self

    def nri_kpis(self) -> list[KpiDef]:
        return [k for h in self.hazards.values() for k in h.kpis if k.source == "nri"]


@lru_cache(maxsize=1)
def load_scoring_config() -> ScoringConfig:
    text = (CONFIG_DIR / "scoring.yaml").read_text()
    cfg = ScoringConfig.model_validate(yaml.safe_load(text))
    cfg.version = hashlib.sha256(text.encode()).hexdigest()[:8]
    return cfg


def nri_scales(counties: list[NriCounty], cfg: ScoringConfig) -> dict[str, float]:
    """Scale top per NRI KPI = national percentile (default p95) over all counties (D4, refined)."""
    return {k.id: percentile_value([nri_frequency(c, k.codes or []) for c in counties], cfg.nri_scale_percentile)
            for k in cfg.nri_kpis()}


# ----------------------------------------------------------------------------- results

@dataclass
class KpiResult:
    id: str
    label: str
    source: str
    raw: float | None
    unit: str
    display: str            # display-ready value with unit, e.g. "0.18 events/yr"
    normalized: float | None
    weight: float
    contribution: float     # points this KPI adds to the hazard score


@dataclass
class HazardResult:
    hazard: str
    label: str
    score: float
    confidence: Level
    reasons: list[str]
    kpis: list[KpiResult]
    nri_part: float | None = None   # weighted score of the FEMA KPIs alone (for the cross-source check)
    obs_part: float | None = None   # weighted score of the observed-weather KPIs alone

    def top_drivers(self, n: int = 2) -> list[KpiResult]:
        return sorted((k for k in self.kpis if k.normalized is not None), key=lambda k: -k.contribution)[:n]


@dataclass
class LocationScore:
    key: str                # hub id, or a stable key for a non-hub point
    label: str
    hub_id: str | None
    wdri: float
    hazards: dict[str, HazardResult]
    dominant_hazard: str
    confidence: Level
    confidence_reasons: list[str]
    county: str
    data_window: str
    sources: list[str]
    config_version: str
    data_version: str
    wdri_equal_weights: float = 0.0
    extreme_hazards: list[str] = field(default_factory=list)       # D37: hazards scoring >= 80
    preparedness_notes: dict[str, str] = field(default_factory=dict)  # D34/H1: hazard -> "rare here" note
    meta: dict = field(default_factory=dict)


# ----------------------------------------------------------------------------- scoring

def _score_kpi(k: KpiDef, grid: DailySeries, county: NriCounty, scales: dict[str, float],
               cfg: ScoringConfig) -> tuple[float | None, float | None, float]:
    """(raw, normalized, coverage) for one KPI."""
    if k.source == "observed":
        raw, coverage = annual_days(grid, k.var or "", k.op or ">=", k.threshold or 0.0)
        norm = None if raw is None else piecewise_linear(raw, k.anchors or [[0, 0], [1, 100]])
        return raw, norm, coverage
    raw = nri_frequency(county, k.codes or [])
    return raw, linear_to_scale(raw, scales.get(k.id, 0.0)), 1.0


def score_hazard(hazard: str, grid: DailySeries, county: NriCounty, scales: dict[str, float],
                 cfg: ScoringConfig, hub_id: str | None = None) -> HazardResult:
    hdef = cfg.hazards[hazard]
    disp = cfg.display
    results: list[KpiResult] = []
    levels: list[Level] = ["high"]
    reasons: list[str] = []
    for k in hdef.kpis:
        raw, norm, coverage = _score_kpi(k, grid, county, scales, cfg)
        if raw is None:
            levels.append("medium")
            reasons.append(f"{k.label}: no data (excluded, weights re-balanced)")
        elif coverage < cfg.confidence.min_coverage:
            levels.append("medium")
            reasons.append(f"{k.label}: only {coverage:.0%} of days have data")
        results.append(KpiResult(k.id, k.label, k.source, raw, k.unit,
                                 fmt_value(raw, k.unit, disp.frequency_sig_figs, disp.days_per_year),
                                 norm, k.weight, 0.0))
    avail = [r for r in results if r.normalized is not None]
    total_w = sum(r.weight for r in avail)
    for r in avail:
        r.contribution = r.weight * (r.normalized or 0.0) / total_w if total_w else 0.0
    score = sum(r.contribution for r in avail)

    # Per-source parts, used by the rank-based cross-source check (cross_source_check), which needs
    # the whole set of locations and so runs after every location is scored.
    def part(src: str) -> float | None:
        rs = [r for r in avail if r.source == src]
        w = sum(r.weight for r in rs)
        return sum(r.weight * (r.normalized or 0.0) for r in rs) / w if w else None

    caveat = cfg.confidence.hub_caveats.get(hub_id or "", {}).get(hazard)
    if caveat:
        levels.append("medium")
        reasons.append(caveat)
    return HazardResult(hazard, hdef.label, score, worst(levels), reasons, results, part("nri"), part("observed"))


def _ranks(values: dict[str, float]) -> dict[str, int]:
    """1 = highest. Ties share the better rank."""
    ordered = sorted(values.values(), reverse=True)
    return {k: ordered.index(v) + 1 for k, v in values.items()}


def cross_source_check(population: list["LocationScore"], cfg: ScoringConfig,
                       subjects: list["LocationScore"] | None = None) -> None:
    """Flag hazards where FEMA county data and observed weather rank a location far apart.

    Rank-based on purpose: the two sources use different scales (national county percentile vs.
    extreme-day anchors), so comparing raw points flags almost everything. Mutates `subjects`
    (default: the whole population) and refreshes their overall confidence.
    """
    gap_limit = cfg.confidence.disagreement_rank_gap
    for h in HAZARDS:
        if not cfg.hazards[h].cross_check:
            continue
        nri = {s.key: s.hazards[h].nri_part for s in population if s.hazards[h].nri_part is not None}
        obs = {s.key: s.hazards[h].obs_part for s in population if s.hazards[h].obs_part is not None}
        both = set(nri) & set(obs)
        if len(both) < 2:
            continue
        rn = _ranks({k: nri[k] for k in both})  # type: ignore[misc]
        ro = _ranks({k: obs[k] for k in both})  # type: ignore[misc]
        for s in subjects or population:
            if s.key in both and abs(rn[s.key] - ro[s.key]) >= gap_limit:
                hr = s.hazards[h]
                hr.confidence = "low"
                hr.reasons.append(_disagreement_reason(hr, s.county, rn[s.key], ro[s.key], len(both)))
    for s in subjects or population:
        _finalize_overall(s, cfg)


def _disagreement_reason(hr: HazardResult, county: str, rank_nri: int, rank_obs: int, n: int) -> str:
    """D52: state the conflicting facts, not just ranks. Uses each source's main (highest-weight) KPI."""
    def main(src: str) -> KpiResult | None:
        ks = [k for k in hr.kpis if k.source == src and k.normalized is not None]
        return max(ks, key=lambda k: k.weight) if ks else None

    nri, obs = main("nri"), main("observed")
    if not (nri and obs):
        return f"FEMA county data and observed weather disagree (#{rank_nri} vs #{rank_obs} of {n})"
    return (f"FEMA records {nri.label.replace(' (FEMA NRI)', '')}: {nri.display} in {county} (#{rank_nri} of {n}), "
            f"but at the hub {obs.label}: {obs.display} (#{rank_obs} of {n})")


def _finalize_overall(s: "LocationScore", cfg: ScoringConfig) -> None:
    """Overall confidence = worst among hazards contributing >= 20% of the WDRI (all if WDRI is 0)."""
    w = cfg.hazard_weights
    material = [h for h in HAZARDS
                if s.wdri == 0 or w[h] * s.hazards[h].score / s.wdri >= cfg.confidence.overall_min_contribution]
    s.confidence = worst([s.hazards[h].confidence for h in material])
    s.confidence_reasons = [f"{s.hazards[h].label}: {r}" for h in material for r in s.hazards[h].reasons]


def score_location(grid: DailySeries, county: NriCounty, scales: dict[str, float], cfg: ScoringConfig,
                   key: str, label: str, hub_id: str | None = None) -> LocationScore:
    hazards = {h: score_hazard(h, grid, county, scales, cfg, hub_id) for h in HAZARDS}
    weights = cfg.hazard_weights
    wdri = sum(weights[h] * hazards[h].score for h in HAZARDS)
    equal = sum(hazards[h].score for h in HAZARDS) / len(HAZARDS)
    dominant = max(HAZARDS, key=lambda h: hazards[h].score)

    start, end = cfg.baseline_window["start"], cfg.baseline_window["end"]
    window = f"{start[:4]}-{end[:4]}"
    sources = [f"FEMA National Risk Index ({county.nri_ver}), county: {county.county}, {county.state}",
               f"Open-Meteo historical ({grid.meta.get('model', 'era5')}), {window}"]
    s = LocationScore(key=key, label=label, hub_id=hub_id, wdri=wdri, hazards=hazards,
                      dominant_hazard=dominant, confidence="high", confidence_reasons=[],
                      county=f"{county.county}, {county.state}", data_window=window, sources=sources,
                      config_version=cfg.version, data_version=f"NRI {county.nri_ver}; ERA5 {window}",
                      wdri_equal_weights=equal)
    _finalize_overall(s, cfg)
    s.extreme_hazards = [h for h in HAZARDS if hazards[h].score >= cfg.flags.extreme_hazard_score]
    note = winter_rarity_note(hazards["winter"], cfg)
    if note:
        s.preparedness_notes["winter"] = note
    return s


def winter_rarity_note(winter: HazardResult, cfg: ScoringConfig) -> str | None:
    """H1 (D34): flag winter as 'rare here' (less prepared region). Explanation only; never changes a score."""
    k = {r.id: r for r in winter.kpis}
    snow, ice, istm = k.get("snow_days_1in"), k.get("ice_days"), k.get("nri_ice_storm")
    if not (snow and ice and snow.raw is not None and ice.raw is not None):
        return None
    rule = cfg.flags.winter_rarity
    occurs = snow.raw > 0 or ice.raw > 0 or (istm is not None and (istm.raw or 0) > 0)
    if snow.raw < rule.max_snow_days and ice.raw < rule.max_ice_days and occurs:
        return (f"Winter events are rare here ({snow.display} with >= 1 in snow, {ice.display} staying at or "
                f"below freezing), so the region is less prepared: each event likely disrupts more than "
                f"this score suggests.")
    return None


# ----------------------------------------------------------------------------- ranking

@dataclass
class RankEntry:
    rank: int
    key: str
    label: str
    score: float
    confidence: Level
    dominant_hazard: str
    top_driver: str
    weight_sensitive: bool = False
    rank_equal_weights: int | None = None
    extreme_hazards: list[str] = field(default_factory=list)
    preparedness_note: str | None = None
    confidence_reason: str | None = None   # D52: why confidence isn't high (conflicting facts)


@dataclass
class Ranking:
    hazard: str
    region: str | None
    entries: list[RankEntry]
    tie_groups: list[list[str]]


def _score_for(s: LocationScore, hazard: str) -> float:
    return s.wdri if hazard == "overall" else s.hazards[hazard].score


def rank_locations(scores: list[LocationScore], hazard: str, cfg: ScoringConfig,
                   region: str | None = None) -> Ranking:
    if hazard != "overall" and hazard not in HAZARDS:
        raise ValueError(f"unknown hazard {hazard}")
    ordered = sorted(scores, key=lambda s: (-_score_for(s, hazard), s.key))
    equal_rank = {s.key: i + 1 for i, s in enumerate(sorted(scores, key=lambda s: (-s.wdri_equal_weights, s.key)))}
    entries = []
    for i, s in enumerate(ordered, start=1):
        if hazard == "overall":
            h = s.hazards[s.dominant_hazard]
            conf = s.confidence
        else:
            h = s.hazards[hazard]
            conf = h.confidence
        top = h.top_drivers(1)
        driver = f"{top[0].label}: {top[0].display}" if top else "no driver"
        e = RankEntry(i, s.key, s.label, _score_for(s, hazard), conf,
                      s.dominant_hazard, f"{h.label} - {driver}" if hazard == "overall" else driver)
        if hazard == "overall":
            e.rank_equal_weights = equal_rank[s.key]
            e.weight_sensitive = abs(i - equal_rank[s.key]) >= cfg.ranking.weight_sensitive_rank_shift
            e.extreme_hazards = list(s.extreme_hazards)
            if s.confidence != "high" and s.confidence_reasons:
                e.confidence_reason = s.confidence_reasons[0]
        else:
            e.extreme_hazards = [hazard] if hazard in s.extreme_hazards else []
            e.preparedness_note = s.preparedness_notes.get(hazard)
            if h.confidence != "high" and h.reasons:
                e.confidence_reason = h.reasons[0]
        entries.append(e)

    # Tie groups: consecutive entries within tie_points of the group's first (highest) member.
    ties: list[list[str]] = []
    i = 0
    while i < len(entries):
        j = i + 1
        while j < len(entries) and entries[i].score - entries[j].score < cfg.ranking.tie_points:
            j += 1
        if j - i > 1:
            ties.append([e.key for e in entries[i:j]])
        i = j
    return Ranking(hazard, region, entries, ties)
