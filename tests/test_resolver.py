"""Resolver tests with a fake geocoder (offline). Covers D24, D25, D48, D49 and the alias fix (D47)."""
from __future__ import annotations

import pytest

from app.data.open_meteo import GeoCandidate
from app.geo import resolver


def place(name, admin1, cc, pop, lat=40.0, lon=-90.0, code="PPL"):
    return GeoCandidate(name, admin1, None, cc, lat, lon, pop, code)


FAKE = {
    "portland": [place("Portland", "Oregon", "US", 652503, 45.5, -122.7), place("Portland", "Maine", "US", 66881, 43.7, -70.3)],
    "columbus": [place("Columbus", "Ohio", "US", 913175, 40.0, -83.0), place("Columbus", "Georgia", "US", 206922, 32.5, -85.0)],
    "huston": [place("Huston", "Idaho", "US", None, 43.6, -116.8)],
    "springfield": [place("Springfield", "Missouri", "US", 170188), place("Springfield", "Massachusetts", "US", 154341),
                    place("Springfield", "Illinois", "US", 114394)],
    "toronto": [place("Toronto", "Ontario", "CA", 2794356), place("Toronto", "Ohio", "US", 4882)],
    "birmingham": [place("Birmingham", "England", "GB", 1144919), place("Birmingham", "Alabama", "US", 200733, 33.5, -86.8)],
    "boise": [place("Boise", "Idaho", "US", 235684, 43.6, -116.2)],
    "austin": [place("Austin", "Texas", "US", 974447), place("Austin", "Minnesota", "US", 24563)],
    "space needle": [],
}


@pytest.fixture(autouse=True)
def fake_geocoder(monkeypatch):
    monkeypatch.setattr(resolver, "geocode", lambda http, name, count=10: FAKE.get(name.strip().lower(), []))


def r(q, **kw):
    return resolver.resolve(None, q, **kw)  # type: ignore[arg-type]


def test_hub_exact_with_notable_namesake():
    res = r("Portland")
    assert (res.status, res.hub_id, res.also_matches) == ("hub_exact", "PDX", "Portland, ME")


def test_state_qualifier_overrides_hub():
    res = r("Portland, ME")
    assert res.status == "geocoded" and res.hub_id is None and "Portland, ME" in res.label


def test_alias_and_airport_code():
    assert r("ORD").hub_id == "CHI" and r("ORD").status == "hub_alias"
    assert r("Logan airport").hub_id == "BOS"


def test_bare_logan_is_not_boston():
    assert r("Logan").hub_id != "BOS"


def test_typo_fuzzy_with_tiny_namesake_mentioned():
    res = r("Huston")
    assert (res.status, res.hub_id, res.also_matches) == ("hub_fuzzy", "HOU", "Huston, ID")


def test_short_strings_never_fuzzy_match():
    assert r("seal").hub_id is None   # 'seal' vs alias 'sea' scores 86: would pass without the 4-letter rule


def test_ambiguous_us_names_ask():
    res = r("Springfield")
    assert res.status == "ambiguous" and len(res.candidates) == 3


def test_dominant_us_name_resolves_directly():
    res = r("Austin")
    assert res.status == "geocoded" and "TX" in res.label


def test_non_us_is_out_of_scope_without_namesake_offer():
    res = r("Toronto")
    assert res.status == "non_us" and not res.candidates


def test_big_us_namesake_beats_foreign_city():   # D48
    res = r("Birmingham")
    assert res.status == "geocoded" and "AL" in res.label and "outside the US" in (res.also_matches or "")


def test_landmark_not_found_then_declared_interpretation():
    assert r("Space Needle").status == "not_found"
    res = r("Seattle", interpreted_from="Space Needle")
    assert (res.status, res.hub_id, res.interpreted_from) == ("llm_interpreted", "SEA", "Space Needle")
