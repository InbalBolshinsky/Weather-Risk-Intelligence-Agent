"""Paths, settings, and the hub registry (loaded from config/hubs.yaml)."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"

Region = Literal["northeast", "midwest", "south", "west"]

# Baseline window for observed-weather KPIs (PLAN.md §1.2): 10 full calendar years.
BASELINE_START = "2016-01-01"
BASELINE_END = "2025-12-31"

# Generic client name for upstream APIs (NWS requires a User-Agent; no personal data).
USER_AGENT = "weather-risk-agent/0.1"


def db_path() -> Path:
    return Path(os.getenv("WRA_DB_PATH", DATA_DIR / "app.db"))


class Hub(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    state: str
    region: Region
    lat: float
    lon: float
    station: str
    station_name: str
    aliases: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return f"{self.name}, {self.state}"


class HubRegistry(BaseModel):
    hubs: tuple[Hub, ...]
    regions: dict[Region, tuple[str, ...]]

    def get(self, hub_id: str) -> Hub | None:
        return next((h for h in self.hubs if h.id == hub_id.upper()), None)

    def in_region(self, region: Region | None) -> list[Hub]:
        return [h for h in self.hubs if region is None or h.region == region]


@lru_cache(maxsize=1)
def hub_registry() -> HubRegistry:
    raw = yaml.safe_load((CONFIG_DIR / "hubs.yaml").read_text())
    reg = HubRegistry.model_validate(raw)
    # Consistency check: every hub's state must belong to its declared Census region.
    for h in reg.hubs:
        if h.state not in reg.regions[h.region]:
            raise ValueError(f"hub {h.id}: state {h.state} is not in region {h.region}")
    ids = [h.id for h in reg.hubs]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate hub ids")
    return reg
