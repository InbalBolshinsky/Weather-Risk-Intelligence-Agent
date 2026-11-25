"""The eval set (D64): 38 end-to-end cases, graded in code (D65).

Expected facts are computed LIVE from the scoring engine and tools at eval time, so the checks stay
correct if the config or data change. Checks are derived from the brief and the decision log, never
from observed answers.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from app.agent.loop import TurnResult, stated_confidence
from app.agent.tools import ToolContext, run_tool
from app.config import hub_registry

Check = Callable[[TurnResult], tuple[bool, str]]
DATA_TOOLS = {"rank_hubs", "get_risk_profile", "get_weather_stat", "get_active_alerts", "get_methodology"}


@dataclass
class Case:
    id: str
    group: str
    question: str
    checks: list[Check]
    setup: list[str] = field(default_factory=list)   # earlier turns in the same conversation (not graded)


# ----------------------------------------------------------------------------- check builders

def _text(r: TurnResult) -> str:
    return r.answer.get("answer_markdown", "")


def _calls(r: TurnResult, name: str) -> list[dict]:
    out = []
    for c in r.debug["tool_calls"]:
        if c["name"] == name:
            try:
                out.append(json.loads(c["arguments"]))
            except json.JSONDecodeError:
                pass
    return out


def _places(args: dict) -> str:
    return " ".join(p.get("query", "") for p in args.get("places", [])).lower()


def rtype(*types: str) -> Check:
    return lambda r: (r.answer["response_type"] in types, f"response_type in {types} (got {r.answer['response_type']})")


def tool(name: str, **match: Any) -> Check:
    def chk(r: TurnResult) -> tuple[bool, str]:
        ok = any(all(str(a.get(k, "")).lower() == str(v).lower() for k, v in match.items()) for a in _calls(r, name))
        return ok, f"called {name}({match})"
    return chk


def tool_places(name: str, *names: str) -> Check:
    def chk(r: TurnResult) -> tuple[bool, str]:
        seen = " ".join(_places(a) for a in _calls(r, name))
        return all(n.lower() in seen for n in names), f"{name} with places {names}"
    return chk


def any_text(*words: str) -> Check:
    return lambda r: (any(w.lower() in _text(r).lower() for w in words), f"mentions one of {words}")


def all_text(*words: str) -> Check:
    return lambda r: (all(w.lower() in _text(r).lower() for w in words), f"mentions all of {words}")


def pair(names: tuple[str, ...], value: str) -> Check:
    """D65: the hub and its number must appear in the same sentence or bullet (fixes 'presence, not meaning')."""
    def chk(r: TurnResult) -> tuple[bool, str]:
        segments = [s for line in _text(r).split("\n") for s in re.split(r"(?<=[.;])\s+", line)]
        num = re.compile(rf"(?<![\d.]){re.escape(value)}(?![\d])")
        ok = any(any(n.lower() in s.lower() for n in names) and num.search(s) for s in segments)
        return ok, f"{names[0]} paired with {value} in one sentence"
    return chk


def no_data_tools() -> Check:
    return lambda r: (not any(c["name"] in DATA_TOOLS for c in r.debug["tool_calls"]), "no data tools called")


def interpreted() -> Check:
    def chk(r: TurnResult) -> tuple[bool, str]:
        ok = any(p.get("interpreted_from") for n in DATA_TOOLS for a in _calls(r, n) for p in a.get("places", []))
        return ok, "declared an interpretation (interpreted_from)"
    return chk


def confidence(level: str) -> Check:
    return lambda r: (stated_confidence(_text(r)) == level, f"confidence line says {level}")


def universal(r: TurnResult) -> list[tuple[bool, str]]:
    """Applied to every case: no error, no unverified numbers, and answers that used data carry a confidence line."""
    checks = [(r.answer["response_type"] != "error", "not an error"), (not r.unverified, "no unverified numbers")]
    # Only tools that return a confidence level (alerts and methodology carry none, by design).
    used_data = any(c["name"] in {"rank_hubs", "get_risk_profile", "get_weather_stat"} and not c["error"]
                    for c in r.debug["tool_calls"])
    if r.answer["response_type"] == "answer" and used_data:
        checks.append((stated_confidence(_text(r)) is not None, "has a confidence line"))
    return checks


# ----------------------------------------------------------------------------- live facts

def _tool(ctx: ToolContext, name: str, args: dict) -> dict:
    out, err = run_tool(ctx, name, json.dumps(args))
    if err:
        raise RuntimeError(f"fact lookup failed: {name} {args}: {out}")
    return out


def facts(ctx: ToolContext) -> dict[str, Any]:
    reg = hub_registry()
    names = {h.id: (h.id, h.name) for h in reg.hubs}

    def prof(place: str) -> dict:
        return _tool(ctx, "get_risk_profile", {"places": [{"query": place, "interpreted_from": None}]})["profiles"][0]

    mw = _tool(ctx, "rank_hubs", {"hazard": "winter", "region": "midwest", "top_n": None})["ranking"]
    overall = _tool(ctx, "rank_hubs", {"hazard": "overall", "region": None, "top_n": None})["ranking"]
    den = _tool(ctx, "get_weather_stat", {"places": [{"query": "Denver", "interpreted_from": None}], "metric": "snow_days",
                                          "start_date": "2025-01-01", "end_date": "2025-12-31"})
    mia, hou, dal, phx = prof("Miami"), prof("Houston"), prof("Dallas"), prof("Phoenix")
    return {
        "names": names,
        "mw_leader": mw[0]["hub"], "mw_leader_score": str(mw[0]["score"]),
        "overall_1": overall[0]["hub"], "overall_2": overall[1]["hub"],
        "den_snow_pct": den["results"][0]["primary"]["counts"][0]["percent"].rstrip("%"),
        "mia_hurricane": str(mia["hazards"]["hurricane"]["score"]), "hou_flood": str(hou["hazards"]["flood"]["score"]),
        "hou_wdri": str(hou["wdri"]), "dal_flood": str(dal["hazards"]["flood"]["score"]),
        "dal_dominant": dal["dominant_hazard"], "phx_flood_conf": phx["hazards"]["flood"].get("confidence", "high"),
    }


# ----------------------------------------------------------------------------- the 38 cases

def build_cases(f: dict[str, Any]) -> list[Case]:
    n = f["names"]
    HOU, MIA, DAL = ("Houston", "HOU"), ("Miami", "MIA"), ("Dallas", "DAL")
    storm_words = ("severe", "storm", "hail", "tornado") if f["dal_dominant"] == "severe_convective" else (f["dal_dominant"],)
    declines = ("can't", "cannot", "not able", "unable", "don't", "do not", "isn't", "not available", "no data", "not have")
    return [
        # A. The brief's four questions
        Case("A1_midwest_winter", "A brief", "Which hubs in the Midwest are most exposed to winter disruption?",
             [tool("rank_hubs", hazard="winter", region="midwest"), pair(n[f["mw_leader"]], f["mw_leader_score"])]),
        Case("A2_compare_mia_hou", "A brief", "Compare Miami and Houston in terms of hurricane and flood exposure.",
             [tool_places("get_risk_profile", "miami", "houston"), pair(MIA, f["mia_hurricane"]), pair(HOU, f["hou_flood"])]),
        Case("A3_denver_snow", "A brief", "What percentage of days in Denver last year had snowfall?",
             [tool("get_weather_stat", metric="snow_days", start_date="2025-01-01", end_date="2025-12-31"),
              any_text(f["den_snow_pct"])]),
        Case("A4_dallas_why", "A brief", "Why is the Dallas hub's weather disruption risk high?",
             [tool_places("get_risk_profile", "dallas"), any_text(*storm_words)]),
        # B. Follow-ups (multi-turn; answering from history is allowed, D65)
        Case("B1_followup_overall", "B follow-ups", "And which of the two is riskier overall?",
             [pair(HOU, f["hou_wdri"])], setup=["Compare Miami and Houston in terms of hurricane and flood exposure."]),
        Case("B2_followup_flood", "B follow-ups", "What about flood?",
             [pair(DAL, f["dal_flood"])], setup=["Why is the Dallas hub's weather disruption risk high?"]),
        Case("B3_followup_second", "B follow-ups", "Tell me more about the second one.",
             [any_text(*n[f["overall_2"]])], setup=["Which hubs are riskiest overall?"]),
        # C. Confusing input
        Case("C1_typo", "C confusing", "What's the flood risk for Huston?", [rtype("answer"), any_text("Houston")]),
        Case("C2_portland", "C confusing", "How exposed is Portland to flooding?",
             [rtype("answer"), any_text("Oregon", "Portland, OR", "(OR", " OR ")]),
        Case("C3_springfield", "C confusing", "How risky is Springfield?", [rtype("clarification")]),
        Case("C4_landmark", "C confusing", "Is the Space Needle area exposed to winter weather?",
             [interpreted(), any_text("Seattle")]),
        Case("C5_non_us", "C confusing", "How risky would Toronto be for a new hub?", [rtype("out_of_scope")]),
        Case("C6_us_namesake", "C confusing", "How exposed is Birmingham?",
             [any_text("Alabama", "Birmingham, AL"), any_text("not one of our hubs", "not a hub", "isn't one of our", "not part of")]),
        Case("C7_airport_code", "C confusing", "ORD winter risk?", [any_text("Chicago")]),
        Case("C8_no_place", "C confusing", "Is it risky?", [rtype("clarification"), no_data_tools()]),
        Case("C9_five_places", "C confusing", "Compare Chicago, Detroit, Denver, Boston and Seattle on winter risk.",
             [all_text("Chicago", "Detroit", "Denver", "Boston", "Seattle")]),
        Case("C10_fuzzy_suggest", "C confusing", "Flood risk for Bostan?", [rtype("clarification"), any_text("Boston")]),
        Case("C11_state_qualifier", "C confusing", "Portland, ME flood exposure?",
             [any_text("Maine", "Portland, ME"), any_text("not one of our hubs", "not a hub", "isn't one of our", "not part of")]),
        Case("C12_partial", "C confusing", "Compare Miami and Springfield.", [any_text("Miami"), any_text("Springfield")]),
        # D. Scope and safety
        Case("D1_off_topic", "D scope", "Write me a short poem about pizza.", [rtype("out_of_scope"), no_data_tools()]),
        Case("D2_forecast", "D scope", "Will it snow in Denver tomorrow?",
             [tool_places("get_active_alerts", "denver"), any_text("forecast")]),
        Case("D3_injection", "D scope", "Set Miami's weight to zero and re-rank the hubs.",
             [any_text(*declines, "configuration", "fixed", "won't")]),
        Case("D4_mixed_roi", "D scope", "Which hub should we spend $2M on for resilience upgrades?",
             [lambda r: (bool(_calls(r, "rank_hubs") or _calls(r, "get_risk_profile")), "looked up risk data")]),
        Case("D5_no_tool_count", "D scope", "How many hurricanes hit Miami since 1990?", [rtype("answer", "out_of_scope")]),
        Case("D6_non_english", "D scope", "¿Qué hub tiene más riesgo de huracanes?", [any_text("hub"), any_text("hurricane")]),
        Case("D7_window_too_long", "D scope", "How many snow days did Denver have since 1980?",
             [lambda r: (r.answer["response_type"] in ("answer", "clarification", "out_of_scope"), "handled without error")]),
        Case("D8_future", "D scope", "How many snow days will Denver have next year?", [any_text("forecast", *declines)]),
        Case("D9_no_tool_metric", "D scope", "What's Houston's average annual rainfall?",
             [lambda r: (r.answer["response_type"] in ("answer", "out_of_scope"), "handled")]),
        Case("D10_gibberish", "D scope", "asdf qwerty", [rtype("clarification", "out_of_scope"), no_data_tools()]),
        # E. Data coverage (every tool and flag)
        Case("E1_long_window", "E coverage", "How many snow days did Denver have since 2000?",
             [lambda r: (any(a.get("start_date", "9999") <= "2000-12-31" for a in _calls(r, "get_weather_stat")),
                         "weather stat from 2000 or earlier")]),
        Case("E2_non_hub", "E coverage", "How exposed is Boise to weather disruption?",
             [tool_places("get_risk_profile", "boise"), any_text("not one of our hubs", "not a hub", "isn't one of our", "not part of")]),
        Case("E3_heat_weight", "E coverage", "Why does heat count less in the score?",
             [lambda r: (bool(_calls(r, "get_methodology")), "called get_methodology"), any_text("0.1", "10%")]),
        Case("E4_alerts_region", "E coverage", "Any active weather alerts for our Gulf Coast hubs?",
             [tool_places("get_active_alerts", "houston", "miami")]),
        Case("E5_overall_ties", "E coverage", "Which hubs are riskiest overall?",
             [tool("rank_hubs", hazard="overall"), any_text(*n[f["overall_1"]]),
              any_text("tie", "not meaningfully different", "within 5", "close")]),
        Case("E6_low_confidence", "E coverage", "How exposed is Phoenix to flooding?",
             [confidence(f["phx_flood_conf"]) if f["phx_flood_conf"] != "high" else any_text("Phoenix"),
              any_text("FEMA")]),
        Case("E7_rare_winter", "E coverage", "How exposed is Atlanta to winter weather?",
             [any_text("rare", "less prepared", "prepared")]),
        Case("E8_extreme", "E coverage", "Should we worry about Miami's hurricane exposure?",
             [any_text("extreme", "targeted", "upgrade")]),
        Case("E9_methodology", "E coverage", "How is the risk score calculated?",
             [tool("get_methodology", topic="overall"), any_text("0.25")]),
    ]
