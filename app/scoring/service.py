"""Scoring service: wires cached data to the engine for hubs and ad-hoc US points.

Hub scores are computed from the SQLite cache (pre-warmed by scripts/refresh_data.py) and memoized
in-process; ranking a region is then a pure in-memory operation.
"""
from __future__ import annotations

import threading

from app.config import BASELINE_END, BASELINE_START, Hub, hub_registry
from app.data import fema_nri, open_meteo
from app.data.http import CachedHttp
from app.scoring.engine import (LocationScore, Ranking, ScoringConfig, cross_source_check, load_scoring_config,
                                nri_scales, rank_locations, score_location)


class ScoringService:
    def __init__(self, http: CachedHttp, cfg: ScoringConfig | None = None):
        self.http = http
        self.cfg = cfg or load_scoring_config()
        self._lock = threading.Lock()
        self._scales: dict[str, float] | None = None
        self._hub_scores: dict[str, LocationScore] = {}

    def scales(self) -> dict[str, float]:
        with self._lock:
            if self._scales is None:
                counties = fema_nri.load_counties(self.http.conn)
                if not counties:
                    raise RuntimeError("NRI county table is empty; run scripts/refresh_data.py first")
                self._scales = nri_scales(counties, self.cfg)
            return self._scales

    def _raw_score(self, lat: float, lon: float, key: str, label: str, hub_id: str | None) -> LocationScore:
        grid = open_meteo.archive_daily(self.http, lat, lon, BASELINE_START, BASELINE_END)
        county = fema_nri.county_for_point(self.http, lat, lon)
        return score_location(grid, county, self.scales(), self.cfg, key=key, label=label, hub_id=hub_id)

    def _ensure_hubs(self) -> None:
        """Score every hub once, then run the cross-source check over the hub set."""
        with self._lock:
            if self._hub_scores:
                return
        scores = {h.id: self._raw_score(h.lat, h.lon, h.id, h.label, h.id) for h in hub_registry().hubs}
        cross_source_check(list(scores.values()), self.cfg)
        with self._lock:
            self._hub_scores = scores

    def hub_score(self, hub: Hub) -> LocationScore:
        self._ensure_hubs()
        return self._hub_scores[hub.id]

    def all_hub_scores(self) -> list[LocationScore]:
        self._ensure_hubs()
        return [self._hub_scores[h.id] for h in hub_registry().hubs]

    def score_point(self, lat: float, lon: float, key: str, label: str) -> LocationScore:
        """Ad-hoc (non-hub) US point; its cross-source check is relative to the hubs."""
        s = self._raw_score(lat, lon, key, label, None)
        cross_source_check(self.all_hub_scores() + [s], self.cfg, subjects=[s])
        return s

    def rank(self, hazard: str, region: str | None = None) -> Ranking:
        reg = hub_registry()
        hubs = reg.in_region(region)  # type: ignore[arg-type]
        return rank_locations([self.hub_score(h) for h in hubs], hazard, self.cfg, region)
