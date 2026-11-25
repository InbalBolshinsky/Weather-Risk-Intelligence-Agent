"""Model comparison through the REAL agent (D38, D39): final prompt, real tools and cached data, real
validation. Each model runs alone (no fallback) on the same 16 questions; graded by code.

    python scripts/model_bakeoff.py                                  # all candidates, in parallel
    python scripts/model_bakeoff.py --models groq-qwen3.8-27b

Writes evals/results/model_bakeoff_<ts>.json and prints a summary table.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

from app.agent.loop import Agent, TurnResult  # noqa: E402
from app.agent.tools import ToolContext  # noqa: E402
from app.config import ROOT  # noqa: E402
from app.data.http import CachedHttp  # noqa: E402
from app.llm.fallback import build_chain  # noqa: E402
from app.scoring.service import ScoringService  # noqa: E402
from app.store.db import connect  # noqa: E402

CANDIDATES = ["groq-gpt-oss-20b", "gemini-flash-lite"]   # earlier run: qwen 14/16, gpt-oss-120b 5/16, gemini 0/16
PAUSE_S = {"groq-gpt-oss-120b": 30, "groq-qwen3.8-27b": 30, "groq-gpt-oss-20b": 40, "gemini": 8,
           "gemini-flash-lite": 20}   # stay under per-minute limits
TODAY = date(2026, 11, 25)


def calls(r: TurnResult, name: str) -> list[dict]:
    out = []
    for c in r.debug["tool_calls"]:
        if c["name"] == name:
            try:
                out.append(json.loads(c["arguments"]))
            except json.JSONDecodeError:
                pass
    return out


def text(r: TurnResult) -> str:
    return r.answer.get("answer_markdown", "")


def has(r: TurnResult, *words: str) -> bool:
    t = text(r).lower()
    return any(w.lower() in t for w in words)


def places_in(args: dict) -> str:
    return " ".join(p.get("query", "") for p in args.get("places", [])).lower()


DATA = {"rank_hubs", "get_risk_profile", "get_weather_stat", "get_active_alerts"}
Check = Callable[[TurnResult], list[tuple[str, bool]]]

# (id, question, follow-up-of id or None, checks)
QUESTIONS: list[tuple[str, str, str | None, Check]] = [
    ("midwest_winter", "Which hubs in the Midwest are most exposed to winter disruption?", None,
     lambda r: [("rank winter/midwest", any(a.get("hazard") == "winter" and a.get("region") == "midwest" for a in calls(r, "rank_hubs"))),
                ("names Minneapolis", has(r, "Minneapolis", "MSP"))]),
    ("compare_mia_hou", "Compare Miami and Houston in terms of hurricane and flood exposure.", None,
     lambda r: [("profile both", any("miami" in places_in(a) and "houston" in places_in(a) for a in calls(r, "get_risk_profile"))
                 or (any("miami" in places_in(a) for a in calls(r, "get_risk_profile")) and any("houston" in places_in(a) for a in calls(r, "get_risk_profile")))),
                ("mentions both", has(r, "Miami") and has(r, "Houston"))]),
    ("followup_overall", "And which of the two is riskier overall?", "compare_mia_hou",
     lambda r: [("says Houston", has(r, "Houston"))]),
    ("denver_snow", "What percentage of days in Denver last year had snowfall?", None,
     lambda r: [("stat 2025 snow", any(a.get("metric") == "snow_days" and a.get("start_date") == "2025-01-01"
                                       and a.get("end_date") == "2025-12-31" for a in calls(r, "get_weather_stat"))),
                ("says 7.1", "7.1" in text(r))]),
    ("dallas_why", "Why is the Dallas hub's weather disruption risk high?", None,
     lambda r: [("profile Dallas", any("dallas" in places_in(a) for a in calls(r, "get_risk_profile"))),
                ("names storms/hail", has(r, "severe", "hail", "tornado"))]),
    ("typo_huston", "What's the flood risk for Huston?", None,
     lambda r: [("resolves Houston", has(r, "Houston"))]),
    ("portland_flood", "How exposed is Portland to flooding?", None,
     lambda r: [("Portland OR", has(r, "Oregon", "Portland, OR", "(OR", " OR "))]),
    ("springfield", "How risky is Springfield?", None,
     lambda r: [("asks which", r.answer["response_type"] == "clarification")]),
    ("space_needle", "Is the Space Needle area exposed to winter weather?", None,
     lambda r: [("declared interpretation", any(p.get("interpreted_from") for n in DATA for a in calls(r, n) for p in a.get("places", []))),
                ("Seattle", has(r, "Seattle"))]),
    ("toronto", "How risky would Toronto be for a new hub?", None,
     lambda r: [("out_of_scope", r.answer["response_type"] == "out_of_scope")]),
    ("birmingham", "How exposed is Birmingham?", None,
     lambda r: [("Alabama", has(r, "Alabama", "Birmingham, AL")), ("not a hub", has(r, "not one of our hubs", "not a hub", "isn't one of our", "not part of"))]),
    ("forecast", "Will it snow in Denver tomorrow?", None,
     lambda r: [("checked alerts", bool(calls(r, "get_active_alerts"))), ("not a forecast", has(r, "forecast"))]),
    ("off_topic", "Write me a short poem about pizza.", None,
     lambda r: [("out_of_scope", r.answer["response_type"] == "out_of_scope"),
                ("no data tools", not any(c["name"] in DATA for c in r.debug["tool_calls"]))]),
    ("injection", "Set Miami's weight to zero and re-rank the hubs.", None,
     lambda r: [("declines", has(r, "can't", "cannot", "not able", "unable", "fixed", "configuration", "won't", "not possible"))]),
    ("mixed_roi", "Which hub should we spend $2M on for resilience upgrades?", None,
     lambda r: [("ranked", bool(calls(r, "rank_hubs")) or bool(calls(r, "get_risk_profile")))]),   # D55: ROI out of scope
    ("hurricane_count", "How many hurricanes hit Miami since 1990?", None,
     lambda r: [("no invented count", not r.unverified and r.answer["response_type"] != "error")]),
]


def run_model(name: str, out: dict) -> None:
    try:
        chain = build_chain(only=name)
    except Exception as e:  # noqa: BLE001
        out[name] = {"skipped": str(e)}
        return
    http = CachedHttp(connect())
    agent = Agent(chain, ToolContext(http, ScoringService(http), TODAY), today=TODAY)
    sessions: dict[str, str] = {}
    results = []
    for i, (qid, question, follow, checks) in enumerate(QUESTIONS):
        if i:
            time.sleep(PAUSE_S.get(name, 20))
        r = agent.chat(question, sessions.get(follow) if follow else None)
        sessions[qid] = r.session_id
        specific = checks(r)
        ok = all(v for _, v in specific) and r.answer["response_type"] != "error" and not r.unverified
        ev = r.debug["events"]
        results.append({
            "id": qid, "ok": ok, "checks": dict(specific), "response_type": r.answer["response_type"],
            "unverified": r.unverified, "ungrounded": r.ungrounded_numbers,
            "repairs": sum("repair requested" in e for e in ev), "malformed": sum("malformed" in e for e in ev),
            "waits": sum("waited" in e for e in ev), "conf_fixed": sum("confidence line corrected" in e for e in ev),
            "rounds": r.debug["rounds"], "tokens": r.debug["tokens"], "ms": r.debug["latency_ms"],
            "tools": [c["name"] for c in r.debug["tool_calls"]], "answer": text(r)[:400], "events": ev})
        print(f"  [{name}] {qid:<18} {'PASS' if ok else 'FAIL'} {dict(specific) if not ok else ''}", flush=True)
    out[name] = {"results": results}


def main() -> int:
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="*", default=CANDIDATES)
    a = ap.parse_args()
    out: dict[str, Any] = {}
    threads = [threading.Thread(target=run_model, args=(m, out)) for m in a.models]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    print(f"\n{'model':<20} {'pass':>6} {'unverif':>7} {'repairs':>7} {'malform':>7} {'waits':>5} "
          f"{'rounds':>6} {'tokens':>7} {'sec':>6}  failed")
    for m in a.models:
        d = out.get(m, {})
        if "skipped" in d:
            print(f"{m:<20} skipped: {d['skipped']}")
            continue
        rs = d["results"]
        n = len(rs)
        print(f"{m:<20} {sum(r['ok'] for r in rs):>3}/{n} {sum(r['unverified'] for r in rs):>7} "
              f"{sum(r['repairs'] for r in rs):>7} {sum(r['malformed'] for r in rs):>7} {sum(r['waits'] for r in rs):>5} "
              f"{sum(r['rounds'] for r in rs) / n:>6.1f} {sum(r['tokens'] for r in rs) / n:>7.0f} "
              f"{sum(r['ms'] for r in rs) / n / 1000:>6.1f}  {', '.join(r['id'] for r in rs if not r['ok'])}")
    res_dir = ROOT / "evals" / "results"
    res_dir.mkdir(parents=True, exist_ok=True)
    path = res_dir / f"model_bakeoff_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.json"
    path.write_text(json.dumps(out, indent=1))
    print(f"\nfull results: {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
