"""Scoring engine tests: synthetic data for logic, plus golden sanity checks on the real cache."""
from __future__ import annotations

import copy

import pytest

from app.config import db_path, hub_registry
from app.data.fema_nri import HAZARDS as NRI_CODES, NriCounty
from app.data.series import DailySeries, calendar
from app.scoring import normalize as nz
from app.scoring.engine import (HAZARDS, ScoringConfig, cross_source_check, load_scoring_config, rank_locations,
                                score_location)

CFG = load_scoring_config()
SCALES = {k.id: 1.0 for k in CFG.nri_kpis()}   # synthetic: every NRI scale top = 1 event/yr


def series(days: int = 730, snow_days: int | None = None, **cols: float | None) -> DailySeries:
    """Constant daily values; `snow_days` = number of days with 5 cm snowfall (above the 1-inch threshold)."""
    dates = calendar("2016-01-01", "2017-12-30")[:days]
    defaults = {"snowfall": 0.0, "precipitation": 0.0, "tmax": 20.0, "tmin": 10.0, "apparent_tmax": 20.0, "gust_max": 20.0}
    defaults.update(cols)
    values = {k: [v] * days for k, v in defaults.items()}
    if snow_days is not None:
        values["snowfall"] = [5.0] * snow_days + [0.0] * (days - snow_days)
    return DailySeries("test", dates, values, {}, {"model": "test"})


def county(**afreq: float | None) -> NriCounty:
    vals = {c: 0.0 for c in NRI_CODES}
    vals.update(afreq)
    return NriCounty("00000", "XX", "Test", "December 2025", vals, {c: None for c in NRI_CODES})


# --- normalization & display -----------------------------------------------------

def test_piecewise_linear_interpolates_and_clips():
    a = [[0, 0], [20, 100]]
    assert nz.piecewise_linear(10, a) == 50
    assert nz.piecewise_linear(-5, a) == 0 and nz.piecewise_linear(99, a) == 100


def test_linear_to_scale_zero_is_zero_and_clips():
    assert nz.linear_to_scale(0, 0.24) == 0
    assert nz.linear_to_scale(0.12, 0.24) == pytest.approx(50)
    assert nz.linear_to_scale(0.5, 0.24) == 100


def test_percentile_value_nearest_rank():
    assert nz.percentile_value(list(range(1, 101)), 95) == 95


def test_display_precision():
    assert nz.fmt_value(0.18361075931, "events/yr") == "0.18 events/yr"
    assert nz.fmt_value(19.105, "events/yr") == "19 events/yr"
    assert nz.fmt_value(8.4, "days/yr") == "8.4 days/yr"
    assert nz.fmt_value(None, "days/yr") == "no data"


# --- hazard + WDRI ---------------------------------------------------------------

def test_hurricane_score_is_linear_in_frequency():
    s = score_location(series(), county(HRCN=0.5), SCALES, CFG, key="X", label="X")
    assert s.hazards["hurricane"].score == pytest.approx(50)


def test_flood_sums_inland_and_coastal_so_one_kind_is_not_diluted():
    inland_only = score_location(series(), county(IFLD=1.0, CFLD=0.0), SCALES, CFG, "A", "A")
    split = score_location(series(), county(IFLD=0.5, CFLD=0.5), SCALES, CFG, "B", "B")
    assert inland_only.hazards["flood"].score == pytest.approx(split.hazards["flood"].score)


def test_wdri_is_weighted_sum_of_hazard_scores():
    s = score_location(series(tmax=40.0, apparent_tmax=45.0), county(HRCN=1.0, HWAV=1.0), SCALES, CFG, "X", "X")
    expected = sum(CFG.hazard_weights[h] * s.hazards[h].score for h in HAZARDS)
    assert s.wdri == pytest.approx(expected)
    assert s.dominant_hazard in ("hurricane", "heat")


def test_cold_wave_is_not_scored():
    base = score_location(series(), county(), SCALES, CFG, "A", "A")
    with_cwav = score_location(series(), county(CWAV=5.0), SCALES, CFG, "B", "B")
    assert base.hazards["winter"].score == with_cwav.hazards["winter"].score


def test_missing_observed_data_reweights_and_lowers_confidence():
    s = score_location(series(snowfall=None), county(WNTW=1.0), SCALES, CFG, "X", "X")
    w = s.hazards["winter"]
    snow = next(k for k in w.kpis if k.id == "snow_days_1in")
    assert snow.normalized is None and w.confidence == "medium"
    assert w.score == pytest.approx(sum(k.contribution for k in w.kpis))


def test_hub_caveat_lowers_confidence_for_denver_winter():
    s = score_location(series(), county(), SCALES, CFG, "DEN", "Denver", hub_id="DEN")
    assert s.hazards["winter"].confidence == "medium"


def test_cross_source_check_flags_rank_disagreement_only():
    # 12 locations where both sources agree, plus one where FEMA says highest and observed says lowest.
    pop = [score_location(series(snow_days=2 * i + 1), county(WNTW=(i + 1) / 13), SCALES, CFG, f"L{i}", f"L{i}")
           for i in range(12)]
    odd = score_location(series(snow_days=0), county(WNTW=1.0), SCALES, CFG, "ODD", "ODD")
    cross_source_check(pop + [odd], CFG)
    assert odd.hazards["winter"].confidence == "low"
    assert all(p.hazards["winter"].confidence == "high" for p in pop)
    reason = odd.hazards["winter"].reasons[-1]   # D52: states the conflicting facts, not just ranks
    assert "events/yr" in reason and "days/yr" in reason and "#1 of 13" in reason


def test_extreme_hazard_flag_at_80_and_never_changes_score():
    s = score_location(series(), county(HRCN=1.0), SCALES, CFG, "X", "X")      # hurricane 100
    low = score_location(series(), county(HRCN=0.5), SCALES, CFG, "Y", "Y")    # hurricane 50
    assert s.extreme_hazards == ["hurricane"] and low.extreme_hazards == []
    assert s.wdri == pytest.approx(CFG.hazard_weights["hurricane"] * 100)


def test_winter_rarity_note_for_rare_but_present_winter():
    rare = score_location(series(snow_days=1), county(ISTM=1.0), SCALES, CFG, "DAL", "Dallas")
    snowy = score_location(series(snow_days=40), county(ISTM=1.0), SCALES, CFG, "MSP", "Minneapolis")
    never = score_location(series(), county(), SCALES, CFG, "MIA", "Miami")
    assert "winter" in rare.preparedness_notes
    assert "winter" not in snowy.preparedness_notes      # common winter: prepared
    assert "winter" not in never.preparedness_notes      # no winter events at all: nothing to flag
    base = score_location(series(snow_days=1), county(ISTM=1.0), SCALES, CFG, "Z", "Z")
    assert rare.hazards["winter"].score == base.hazards["winter"].score


def test_severe_convective_excluded_from_cross_check():
    assert CFG.hazards["severe_convective"].cross_check is False


# --- ranking ---------------------------------------------------------------------

def test_ranking_order_ties_and_weight_sensitivity():
    cfg = copy.deepcopy(CFG)
    a = score_location(series(), county(HRCN=1.0), SCALES, cfg, "A", "A")
    b = score_location(series(), county(HRCN=0.97), SCALES, cfg, "B", "B")   # within 5 points of A
    c = score_location(series(), county(HRCN=0.2), SCALES, cfg, "C", "C")
    r = rank_locations([c, b, a], "hurricane", cfg)
    assert [e.key for e in r.entries] == ["A", "B", "C"]
    assert r.tie_groups == [["A", "B"]]


def test_unknown_hazard_rejected():
    with pytest.raises(ValueError):
        rank_locations([], "earthquake", CFG)


def test_config_rejects_weights_not_summing_to_one():
    raw = CFG.model_dump()
    raw["hazard_weights"]["heat"] = 0.5
    with pytest.raises(ValueError):
        ScoringConfig.model_validate(raw)


# --- golden sanity checks on the real cache (skipped if not refreshed) -----------

def _cache_ready() -> bool:
    try:
        from app.store.db import connect
        return db_path().exists() and connect().execute("SELECT COUNT(*) FROM nri_counties").fetchone()[0] > 3000
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(scope="module")
def svc():
    from app.data.http import CachedHttp
    from app.scoring.service import ScoringService
    from app.store.db import connect
    return ScoringService(CachedHttp(connect()))


@pytest.mark.skipif(not _cache_ready(), reason="run scripts/refresh_data.py to build the cache")
class TestGoldenOnRealData:
    def test_all_20_hubs_score(self, svc):
        assert len(svc.all_hub_scores()) == 20

    def test_midwest_winter_led_by_minneapolis(self, svc):
        assert svc.rank("winter", "midwest").entries[0].key == "MSP"

    def test_miami_tops_hurricane_and_phoenix_tops_heat(self, svc):
        assert svc.rank("hurricane").entries[0].key == "MIA"
        assert svc.rank("heat").entries[0].key == "PHX"

    def test_dallas_driven_by_severe_storms(self, svc):
        dal = svc.hub_score(hub_registry().get("DAL"))
        assert dal.dominant_hazard == "severe_convective"

    def test_miami_has_no_winter_exposure(self, svc):
        assert svc.hub_score(hub_registry().get("MIA")).hazards["winter"].score < 5

    def test_extreme_hazards_on_real_data(self, svc):
        flagged = {s.key: s.extreme_hazards for s in svc.all_hub_scores() if s.extreme_hazards}
        assert flagged == {"MIA": ["hurricane"], "PHX": ["heat"], "HOU": ["flood"]}

    def test_winter_rarity_flags_sun_belt_and_pacific_northwest(self, svc):
        flagged = {s.key for s in svc.all_hub_scores() if "winter" in s.preparedness_notes}
        assert flagged == {"ATL", "CLT", "DAL", "HOU", "MEM", "SEA", "PDX"}
