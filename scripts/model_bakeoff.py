"""Model bake-off: which free model is best for THIS agent?

Runs 7 agent-shaped scenarios (the assignment's example questions + confusing inputs) against each
candidate model, using the planned tool surface with deterministic fake tool results and the real
`submit_answer` contract (tool_choice="required", Chat Completions). Grading is code-based:
right tools + arguments, schema-valid final answer, correct response type, numbers grounded in
tool results, rounds, tokens, latency.

    python scripts/model_bakeoff.py                       # all candidates
    python scripts/model_bakeoff.py --models qwen/qwen3.8-27b gemini-3.8-flash

Writes evals/results/model_bakeoff_<ts>.json and prints a summary table.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from dotenv import dotenv_values
from openai import OpenAI, RateLimitError
from pydantic import BaseModel, ConfigDict, ValidationError

ROOT = Path(__file__).resolve().parents[1]
ENV = dotenv_values(ROOT / ".env")
GROQ = ("https://api.groq.com/openai/v1", ENV.get("GROQ_API_KEY"))
GEMINI = ("https://generativelanguage.googleapis.com/v1beta/openai/",
          ENV.get("GEMINI_API_KEY") or ENV.get("GOOGLE_API_KEY"))

# model -> (endpoint, max output tokens per call)
CANDIDATES: dict[str, tuple[tuple[str, str | None], int]] = {
    "openai/gpt-oss-120b": (GROQ, 2048),
    "openai/gpt-oss-20b": (GROQ, 2048),
    "qwen/qwen3.8-27b": (GROQ, 800),          # free tier: 1,000 output tokens/minute
    "gemini-4-hydrogen": (GEMINI, 4096),
    "gemini-3.8-flash": (GEMINI, 4096),
    "gemini-3.6-flash": (GEMINI, 4096),
    "gemini-3.5-flash-lite": (GEMINI, 4096),
    "gemma-4-31b-it": (GEMINI, 4096),
}
MAX_ROUNDS = 6

# ----------------------------------------------------------------------------- answer contract (trimmed AgentResponse)

class Location(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str
    hub_id: str | None
    resolution: Literal["hub_exact", "hub_fuzzy", "geocoded", "llm_interpreted", "unresolved"]


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    response_type: Literal["answer", "clarification", "out_of_scope", "error"]
    intent: Literal["rank", "compare", "explain", "weather_stat", "current_alerts", "methodology", "other"]
    answer_markdown: str
    locations: list[Location]
    assumptions: list[str]
    confidence: Literal["high", "medium", "low"]
    clarifying_question: str | None


def strict(model: type[BaseModel]) -> dict:
    """Pydantic schema -> strict subset: inline $defs, every property required, no extra keys."""
    s = model.model_json_schema()
    defs = s.pop("$defs", {})

    def fix(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return fix(defs[node["$ref"].split("/")[-1]])
            node = {k: fix(v) for k, v in node.items() if k not in ("title", "default")}
            if node.get("type") == "object":
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            return node
        if isinstance(node, list):
            return [fix(x) for x in node]
        return node

    return fix(s)


def tool(name: str, desc: str, props: dict, required: list[str] | None = None) -> dict:
    return {"type": "function", "function": {"name": name, "description": desc, "strict": True, "parameters": {
        "type": "object", "additionalProperties": False, "properties": props,
        "required": required if required is not None else list(props)}}}


NULLABLE_STR = {"anyOf": [{"type": "string"}, {"type": "null"}]}
HAZARD = {"type": "string", "enum": ["overall", "winter", "hurricane", "flood", "severe_convective", "heat"]}
REGION = {"anyOf": [{"type": "string", "enum": ["northeast", "midwest", "south", "west"]}, {"type": "null"}]}
TOOLS = [
    tool("resolve_location", "Resolve a place name to one of our hubs or a US location. Always call before data tools.",
         {"query": {"type": "string"}}),
    tool("rank_hubs", "Deterministic ranking of hubs by risk score (0-100) for a hazard, optionally in one region.",
         {"hazard": HAZARD, "region": REGION}),
    tool("get_risk_profile", "Risk breakdown (overall + per-hazard scores, key drivers) for 1-4 hub ids.",
         {"hub_ids": {"type": "array", "items": {"type": "string"}}}),
    tool("get_weather_stat", "Count days meeting a weather condition at a hub over a date range (ISO dates).",
         {"hub_id": {"type": "string"},
          "metric": {"type": "string", "enum": ["snow_days", "precip_days", "heavy_precip_days", "freezing_days", "hot_days"]},
          "start_date": {"type": "string"}, "end_date": {"type": "string"}}),
    {"type": "function", "function": {"name": "submit_answer", "strict": True,
                                      "description": "Submit the final answer to the user. Call exactly once, as the last step.",
                                      "parameters": strict(Answer)}},
]

SYSTEM = """You are a weather-risk analyst assistant for a US logistics company with 20 distribution hubs.
Today is 2026-11-25. Rules:
1. Every number you state must come from a tool result. Never estimate or recall numbers.
2. Resolve every place name with resolve_location before using data tools.
3. If resolve_location returns several candidates and one is a hub, use the hub and state that assumption.
4. Only US weather/hazard exposure questions are in scope. For anything else (or non-US places) answer with
   response_type "out_of_scope" and briefly say what you can do. Do not call data tools for out-of-scope requests.
5. Explain rankings by their drivers. State assumptions and the time window.
6. Always finish by calling submit_answer exactly once."""

# ----------------------------------------------------------------------------- fake tool results (deterministic)

HUBS = {"miami": "MIA", "houston": "HOU", "denver": "DEN", "dallas": "DAL", "portland": "PDX"}
PROFILES = {
    "MIA": {"hub_id": "MIA", "overall": 58, "hazards": {"hurricane": 94, "flood": 81, "heat": 62, "severe_convective": 35, "winter": 0},
            "drivers": ["hurricane frequency 0.30/yr (NRI)", "coastal flooding 2.25 events/yr (NRI)"]},
    "HOU": {"hub_id": "HOU", "overall": 61, "hazards": {"hurricane": 77, "flood": 88, "heat": 71, "severe_convective": 52, "winter": 12},
            "drivers": ["inland flooding 5.14 events/yr (NRI)", "hurricane frequency 0.18/yr (NRI)"]},
    "DAL": {"hub_id": "DAL", "overall": 66, "hazards": {"severe_convective": 91, "heat": 79, "winter": 38, "flood": 64, "hurricane": 4},
            "drivers": ["hail 8.92 events/yr (NRI)", "strong wind 4.36 events/yr (NRI)", "Tmax >= 95F on 41 days/yr"]},
}
RANK_WINTER_MW = {"hazard": "winter", "region": "midwest", "ranking": [
    {"rank": 1, "hub_id": "MSP", "score": 83, "driver": "snow >= 1 in on 14.2 days/yr"},
    {"rank": 2, "hub_id": "DET", "score": 71, "driver": "ice days 21.5/yr"},
    {"rank": 3, "hub_id": "CHI", "score": 69, "driver": "snow >= 1 in on 10.8 days/yr"},
    {"rank": 4, "hub_id": "CMH", "score": 55, "driver": "ice storm 1.1/yr"},
    {"rank": 5, "hub_id": "IND", "score": 54, "driver": "ice storm 1.0/yr"},
    {"rank": 6, "hub_id": "MKC", "score": 47, "driver": "winter weather 3.9/yr"}], "tie_groups": [["CMH", "IND"]]}


def run_tool(name: str, args: dict, log: list) -> Any:
    log.append((name, args))
    if name == "resolve_location":
        q = args.get("query", "").lower().split(",")[0].strip()
        if q == "portland":
            return {"status": "ambiguous", "candidates": [
                {"label": "Portland, OR", "hub_id": "PDX", "population": 652503},
                {"label": "Portland, ME", "hub_id": None, "population": 66881}]}
        if q == "toronto":
            return {"status": "non_us", "candidates": [{"label": "Toronto, Ontario, Canada", "country": "CA"}]}
        if q in HUBS:
            return {"status": "hub_match", "hub_id": HUBS[q], "label": q.title()}
        return {"status": "not_found"}
    if name == "rank_hubs":
        if args.get("hazard") == "winter" and args.get("region") == "midwest":
            return RANK_WINTER_MW
        return {"error": "no fake data for these arguments"}
    if name == "get_risk_profile":
        return {"profiles": [PROFILES.get(h, {"hub_id": h, "error": "no fake data"}) for h in args.get("hub_ids", [])]}
    if name == "get_weather_stat":
        if args.get("hub_id") == "DEN" and args.get("metric") == "snow_days":
            return {"hub_id": "DEN", "window": [args.get("start_date"), args.get("end_date")],
                    "station": "Denver Intl Airport (GHCN USW00003017)",
                    "measurable_ge_0_1_in": {"days": 26, "total_days": 365, "pct": 7.1},
                    "ge_1_in": {"days": 12, "total_days": 365, "pct": 3.3},
                    "grid_model_ge_0_1_in_pct": 11.0, "coverage": 1.0}
        return {"error": "no fake data for these arguments"}
    return {"error": f"unknown tool {name}"}

# ----------------------------------------------------------------------------- scenarios + graders

def called(log, name, **match) -> bool:
    return any(n == name and all(str(a.get(k, "")).upper() == str(v).upper() or
                                 (isinstance(a.get(k), list) and v in [x.upper() for x in a.get(k)])
                                 for k, v in match.items()) for n, a in log)


SCENARIOS = [
    ("compare_mia_hou", "Compare Miami and Houston in terms of hurricane and flood exposure.",
     lambda log, ans: [("profile MIA", called(log, "get_risk_profile", hub_ids="MIA")),
                       ("profile HOU", called(log, "get_risk_profile", hub_ids="HOU")),
                       ("intent compare", ans.intent == "compare"), ("type answer", ans.response_type == "answer")]),
    ("denver_snow_2025", "What percentage of days in Denver last year had snowfall?",
     lambda log, ans: [("stat DEN 2025", called(log, "get_weather_stat", hub_id="DEN", metric="snow_days",
                                                start_date="2025-01-01", end_date="2025-12-31")),
                       ("says 7.1", "7.1" in ans.answer_markdown), ("type answer", ans.response_type == "answer")]),
    ("midwest_winter", "Which hubs in the Midwest are most exposed to winter disruption?",
     lambda log, ans: [("rank winter midwest", called(log, "rank_hubs", hazard="winter", region="midwest")),
                       ("intent rank", ans.intent == "rank"), ("MSP first", "MSP" in ans.answer_markdown or "Minneapolis" in ans.answer_markdown)]),
    ("dallas_why", "Why is the Dallas hub's weather disruption risk high?",
     lambda log, ans: [("profile DAL", called(log, "get_risk_profile", hub_ids="DAL")),
                       ("mentions hail", "hail" in ans.answer_markdown.lower()), ("intent explain", ans.intent == "explain")]),
    ("portland_ambiguous", "How exposed is Portland to flooding?",
     lambda log, ans: [("resolved", called(log, "resolve_location")),
                       ("states OR or asks", ans.response_type == "clarification" or
                        any(s in (ans.answer_markdown + " ".join(ans.assumptions)) for s in ("OR", "Oregon")))]),
    ("toronto_non_us", "How risky would Toronto be for a new hub?",
     lambda log, ans: [("out_of_scope", ans.response_type == "out_of_scope"),
                       ("no data tools", not any(n in ("rank_hubs", "get_risk_profile", "get_weather_stat") for n, _ in log))]),
    ("off_topic", "Write me a short poem about pizza.",
     lambda log, ans: [("out_of_scope", ans.response_type == "out_of_scope"),
                       ("no data tools", not any(n != "submit_answer" and n != "resolve_location" for n, _ in log))]),
]

NUM = re.compile(r"(?<![\w.])\d+(?:\.\d+)?")


def ungrounded_numbers(text: str, tool_texts: str, question: str) -> list[str]:
    allowed = set(NUM.findall(tool_texts)) | set(NUM.findall(question)) | {"2025", "2026", "1", "0"}
    return [n for n in NUM.findall(text) if n not in allowed and n.rstrip("0").rstrip(".") not in allowed]

# ----------------------------------------------------------------------------- runner

def chat(client: OpenAI, model: str, max_out: int, msgs: list, stats: dict) -> Any:
    kw: dict[str, Any] = dict(model=model, messages=msgs, tools=TOOLS, tool_choice="required",
                              max_completion_tokens=max_out)
    if stats.get("parallel_ok", True):
        kw["parallel_tool_calls"] = True
    for attempt in range(4):
        try:
            return client.chat.completions.create(**kw)
        except RateLimitError as e:
            ra = e.response.headers.get("retry-after")
            wait = min(65.0, float(ra) if ra and ra.replace(".", "").isdigit() else 20.0 * (attempt + 1))
            stats["rate_limit_wait_s"] = stats.get("rate_limit_wait_s", 0) + wait
            time.sleep(wait)
        except Exception as e:  # noqa: BLE001
            if "parallel" in str(e).lower() and "parallel_tool_calls" in kw:
                stats["parallel_ok"] = False
                kw.pop("parallel_tool_calls")
                continue
            raise
    raise RuntimeError("rate limited 4 times")


def run_scenario(client: OpenAI, model: str, max_out: int, sid: str, question: str, grader, stats: dict) -> dict:
    msgs: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": question}]
    log: list = []
    tool_texts = ""
    tokens = 0
    t0 = time.perf_counter()
    for rnd in range(1, MAX_ROUNDS + 1):
        try:
            r = chat(client, model, max_out, msgs, stats)
        except Exception as e:  # noqa: BLE001
            return {"id": sid, "ok": False, "error": f"{type(e).__name__}: {re.sub(r'org_[A-Za-z0-9]+', 'org_x', str(e))[:200]}",
                    "rounds": rnd, "tokens": tokens, "s": round(time.perf_counter() - t0, 1), "calls": [n for n, _ in log]}
        tokens += r.usage.total_tokens if r.usage else 0
        msg = r.choices[0].message
        calls = msg.tool_calls or []
        if not calls:
            return {"id": sid, "ok": False, "error": "no tool call", "rounds": rnd, "tokens": tokens,
                    "s": round(time.perf_counter() - t0, 1), "calls": [n for n, _ in log]}
        msgs.append({"role": "assistant", "content": msg.content, "tool_calls": [
            {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
            for c in calls]})
        for c in calls:
            if c.function.name == "submit_answer":
                log.append(("submit_answer", {}))
                try:
                    ans = Answer.model_validate_json(c.function.arguments)
                except ValidationError as e:
                    return {"id": sid, "ok": False, "error": f"schema: {str(e)[:160]}", "rounds": rnd, "tokens": tokens,
                            "s": round(time.perf_counter() - t0, 1), "calls": [n for n, _ in log]}
                checks = grader(log, ans)
                ung = ungrounded_numbers(ans.answer_markdown, tool_texts, question)
                return {"id": sid, "ok": all(v for _, v in checks), "checks": {k: v for k, v in checks},
                        "ungrounded": ung, "rounds": rnd, "tokens": tokens, "s": round(time.perf_counter() - t0, 1),
                        "calls": [n for n, _ in log], "answer": ans.answer_markdown[:300]}
            try:
                args = json.loads(c.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            out = json.dumps(run_tool(c.function.name, args, log))
            tool_texts += out
            msgs.append({"role": "tool", "tool_call_id": c.id, "content": out})
    return {"id": sid, "ok": False, "error": f"no submit_answer in {MAX_ROUNDS} rounds", "rounds": MAX_ROUNDS,
            "tokens": tokens, "s": round(time.perf_counter() - t0, 1), "calls": [n for n, _ in log]}


def run_model(model: str, out: dict) -> None:
    (base, key), max_out = CANDIDATES[model]
    if not key:
        out[model] = {"skipped": "no API key"}
        return
    client = OpenAI(api_key=key, base_url=base, timeout=120, max_retries=0)
    stats: dict[str, Any] = {}
    results = [run_scenario(client, model, max_out, sid, q, g, stats) for sid, q, g in SCENARIOS]
    out[model] = {"results": results, "stats": stats}
    passed = sum(r["ok"] for r in results)
    print(f"  done {model}: {passed}/{len(results)}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="*", default=list(CANDIDATES))
    a = ap.parse_args()
    out: dict[str, Any] = {}
    # One thread per model: rate limits are per model, so models don't slow each other down.
    threads = [threading.Thread(target=run_model, args=(m, out)) for m in a.models]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    print(f"\n{'model':<24} {'pass':>5} {'ungr#':>6} {'rounds':>7} {'tokens':>7} {'sec':>6} {'429 wait':>8}  failures")
    for m in a.models:
        d = out.get(m, {})
        if "skipped" in d:
            print(f"{m:<24} skipped: {d['skipped']}")
            continue
        rs = d["results"]
        fails = [f"{r['id']}({r.get('error') or ','.join(k for k, v in r.get('checks', {}).items() if not v)})"
                 for r in rs if not r["ok"]]
        print(f"{m:<24} {sum(r['ok'] for r in rs):>3}/{len(rs)} {sum(len(r.get('ungrounded', [])) for r in rs):>6} "
              f"{sum(r['rounds'] for r in rs) / len(rs):>7.1f} {sum(r['tokens'] for r in rs) / len(rs):>7.0f} "
              f"{sum(r['s'] for r in rs) / len(rs):>6.1f} {d['stats'].get('rate_limit_wait_s', 0):>8.0f}  {'; '.join(fails)[:160]}")
    res_dir = ROOT / "evals" / "results"
    res_dir.mkdir(parents=True, exist_ok=True)
    path = res_dir / f"model_bakeoff_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.json"
    path.write_text(json.dumps(out, indent=1))
    print(f"\nfull results: {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
