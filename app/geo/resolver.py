"""Deterministic place-name resolver, shared by every data tool (PLAN.md §3.1, D24/D25).

Order: hub name/alias (exact) -> hub fuzzy match -> geocoder (US-only). The ambiguity policy lives
here, in code, so it behaves the same way every time and evals can test it. The model passes names
exactly as the user wrote them; it may only add `interpreted_from` after a not_found (landmarks).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Literal

from rapidfuzz import fuzz

from app.config import Hub, hub_registry
from app.data.http import CachedHttp
from app.data.open_meteo import GeoCandidate, geocode
from app.geo.states import STATES, state_code

Status = Literal["hub_exact", "hub_alias", "hub_fuzzy", "geocoded", "llm_interpreted",
                 "ambiguous", "not_found", "non_us"]

FUZZY_ACCEPT = 85         # D25
FUZZY_SUGGEST = 70        # D25
MIN_FUZZY_LEN = 4         # never fuzzy-match short aliases like "sea", "den", "ord"
NOTABLE_POP = 50_000      # a same-name US place this big is worth naming as also_matches / an ambiguity
AMBIGUOUS_RATIO = 0.2     # 2nd US candidate >= 20% of the 1st's population -> ambiguous


@dataclass
class Resolution:
    query: str
    status: Status
    hub_id: str | None = None
    label: str | None = None
    lat: float | None = None
    lon: float | None = None
    candidates: list[str] = field(default_factory=list)   # labels, for ambiguous
    also_matches: str | None = None
    interpreted_from: str | None = None

    @property
    def usable(self) -> bool:
        return self.lat is not None and self.status not in ("ambiguous", "not_found", "non_us")

    def public(self) -> dict:
        """Compact form returned to the model (no coordinates)."""
        d = {k: v for k, v in asdict(self).items() if v not in (None, [], "") and k not in ("lat", "lon")}
        return d


def _norm(s: str) -> str:
    s = s.lower().replace("’", "'")
    s = re.sub(r"[^\w\s',.-]", " ", s)
    s = re.sub(r"\b(usa|u\.s\.a\.|united states|us)\b\.?$", "", s.strip(" ,"))
    return re.sub(r"\s+", " ", s).strip(" ,.")


def _split_state(q: str) -> tuple[str, str | None]:
    """'Portland, ME' / 'Columbus Ohio' / 'Kansas City, KS' -> (name, state code)."""
    if "," in q:
        name, tail = q.rsplit(",", 1)
        code = state_code(tail)
        if code:
            return name.strip(), code
    parts = q.split()
    for n in (2, 1):  # "new jersey", "oh"
        if len(parts) > n:
            code = state_code(" ".join(parts[-n:]))
            if code:
                return " ".join(parts[:-n]), code
    return q, None


def _label(c: GeoCandidate) -> str:
    st = next((k for k, v in STATES.items() if v == c.admin1), c.admin1 or "")
    return f"{c.name}, {st}"


def _hub_resolution(query: str, hub: Hub, status: Status) -> Resolution:
    return Resolution(query, status, hub.id, f"{hub.label} (hub {hub.id})", hub.lat, hub.lon)


def _us_places(http: CachedHttp, name: str, state: str | None) -> tuple[list[GeoCandidate], GeoCandidate | None]:
    """US populated places matching `name` (sorted by population) and the overall top result."""
    results = geocode(http, name, count=10)
    places = [c for c in results if (c.feature_code or "").startswith("PPL")] or results
    top = max(places, key=lambda c: c.population or 0) if places else None
    us = [c for c in places if c.country_code == "US"]
    if state:
        us = [c for c in us if c.admin1 == STATES[state]]
    return sorted(us, key=lambda c: -(c.population or 0)), top


def _notable_namesake(http: CachedHttp, name: str, hub: Hub) -> str | None:
    """A sizeable US place with the same name in another state (Portland -> Portland, ME)."""
    try:
        us, _ = _us_places(http, name, None)
    except Exception:  # noqa: BLE001 - an also_matches hint is optional; never fail resolution for it
        return None
    for c in us:
        if c.name.lower() == name.lower() and c.admin1 != STATES.get(hub.state) and (c.population or 0) >= NOTABLE_POP:
            return _label(c)
    return None


def resolve(http: CachedHttp, query: str, interpreted_from: str | None = None) -> Resolution:
    res = _resolve(http, query)
    if interpreted_from and res.usable:
        res.status, res.interpreted_from = "llm_interpreted", interpreted_from
    return res


def _resolve(http: CachedHttp, query: str) -> Resolution:
    reg = hub_registry()
    q = _norm(query)
    if not q:
        return Resolution(query, "not_found")
    name, state = _split_state(q)

    # 1) Exact hub name or alias (unless an explicit state contradicts the hub: "Portland, ME").
    for hub in reg.hubs:
        if state and state != hub.state:
            continue
        if name == hub.name.lower():
            return _with_namesake(http, _hub_resolution(query, hub, "hub_exact"), name, hub, state)
        if name in (a.lower() for a in hub.aliases):
            return _hub_resolution(query, hub, "hub_alias")

    # 2) Fuzzy match against hub names and long aliases (typos: "Huston", "Dalas").
    if not state or any(h.state == state for h in reg.hubs):
        scored = []
        for hub in reg.hubs:
            if state and hub.state != state:
                continue
            keys = [hub.name.lower()] + [a.lower() for a in hub.aliases if len(a) >= MIN_FUZZY_LEN]
            scored.append((max(fuzz.ratio(name, k) for k in keys), hub))
        scored.sort(key=lambda x: -x[0])
        if scored and scored[0][0] >= FUZZY_ACCEPT and len(name) >= MIN_FUZZY_LEN:
            hub = scored[0][1]
            exact_us, _ = _us_places(http, name, state)
            exact = [c for c in exact_us if c.name.lower() == name]
            res = _hub_resolution(query, hub, "hub_fuzzy")
            if exact and (exact[0].population or 0) >= NOTABLE_POP:
                # A real, sizeable place has exactly this name: don't guess, ask.
                return Resolution(query, "ambiguous", candidates=[res.label or hub.label, _label(exact[0])])
            if exact:
                res.also_matches = _label(exact[0])
            return res
        if scored and scored[0][0] >= FUZZY_SUGGEST and len(name) >= MIN_FUZZY_LEN:
            near = [h for s, h in scored[:2] if s >= FUZZY_SUGGEST]
            return Resolution(query, "ambiguous", candidates=[f"{h.label} (hub {h.id})" for h in near])

    # 3) Geocoder: US-only, populated places, ambiguity by population.
    us, top = _us_places(http, name, state)
    foreign_note = None
    if top is not None and top.country_code != "US" and not state:
        # D48: a sizeable same-name US city wins over a larger foreign one (Birmingham -> AL, Vancouver -> WA);
        # otherwise the place is outside the US and out of scope (Toronto, London, Paris).
        big_us = [c for c in us if c.name.lower() == name and (c.population or 0) >= NOTABLE_POP]
        if not big_us:
            return Resolution(query, "non_us", label=f"{top.name}, {top.country_code}")
        us = big_us + [c for c in us if c not in big_us]
        foreign_note = f"{top.name}, {top.country_code} (outside the US)"
    if not us:
        return Resolution(query, "not_found")
    first = us[0]
    same_name = [c for c in us if c.name.lower() == first.name.lower()]
    if (not state and len(same_name) > 1
            and (same_name[1].population or 0) >= AMBIGUOUS_RATIO * max(first.population or 1, 1)):
        return Resolution(query, "ambiguous", candidates=[_label(c) for c in same_name[:3]])
    return Resolution(query, "geocoded", None, f"{_label(first)} (not one of our hubs)", first.lat, first.lon,
                      also_matches=foreign_note)


def _with_namesake(http: CachedHttp, res: Resolution, name: str, hub: Hub, state: str | None) -> Resolution:
    if not state:
        res.also_matches = _notable_namesake(http, name, hub)
    return res
