"""Step 1: verify every external API with a real call before we rely on it.

Usage:
    python scripts/verify_apis.py            # data APIs + every LLM target whose key is set
    python scripts/verify_apis.py --data     # data APIs only
    python scripts/verify_apis.py --llm      # LLM targets only

Saves small response samples to tests/fixtures/ (used later by offline unit tests)
and a machine-readable report to tests/fixtures/_verify_report.json.
API keys are read from .env / environment and are never printed or saved.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
USER_AGENT = "weather-risk-agent/0.1 (interview demo)"  # NWS requires a UA; no personal data in it
TIMEOUT = httpx.Timeout(45.0)

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
GHCN_URL = "https://www.ncei.noaa.gov/access/services/data/v1"
NRI_URL = ("https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/"
           "National_Risk_Index_Counties/FeatureServer/0")
NWS_URL = "https://api.weather.gov/alerts/active"

DAILY_VARS = ["snowfall_sum", "precipitation_sum", "temperature_2m_max", "temperature_2m_min",
              "apparent_temperature_max", "wind_gusts_10m_max"]
NRI_HAZARDS = ["WNTW", "ISTM", "CWAV", "HRCN", "IFLD", "CFLD", "TRND", "HAIL", "SWND", "HWAV"]

HUBS_SAMPLE = {  # metro-center coordinates, same as config/hubs.yaml
    "Houston": (29.7604, -95.3698),
    "Miami": (25.7617, -80.1918),
    "Dallas": (32.7767, -96.7970),
    "Denver": (39.7392, -104.9903),
}

results: list[dict[str, Any]] = []


def record(group: str, name: str, fn: Callable[[], str]) -> None:
    """Run one check; a check returns a one-line finding or raises on failure."""
    t0 = time.perf_counter()
    try:
        finding, ok = fn(), True
    except Exception as e:  # noqa: BLE001 - we want every failure in the report
        msg = re.sub(r"org_[A-Za-z0-9]+", "org_<redacted>", str(e))  # account id: not secret, but private
        finding, ok = f"{type(e).__name__}: {msg[:400]}", False
    ms = int((time.perf_counter() - t0) * 1000)
    results.append({"group": group, "check": name, "ok": ok, "ms": ms, "finding": finding})
    print(f"[{'PASS' if ok else 'FAIL'}] {group:<22} {name:<34} {ms:>6} ms  {finding}")


def save(name: str, data: Any) -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    (FIXTURES / name).write_text(json.dumps(data, indent=1, ensure_ascii=False))


def expect(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


# ----------------------------------------------------------------------------- data APIs

def check_geocoding(c: httpx.Client) -> str:
    queries = ["Portland", "Columbus", "Huston", "Dalas", "Toronto", "O'Hare", "ORD", "Space Needle"]
    out = {}
    for q in queries:
        r = c.get(GEOCODE_URL, params={"name": q, "count": 5, "language": "en", "format": "json"})
        r.raise_for_status()
        out[q] = r.json().get("results", [])
    save("geocoding.json", out)
    top = {q: (res[0]["country_code"], res[0].get("admin1")) if res else None for q, res in out.items()}
    expect(top["Portland"] == ("US", "Oregon"), f"Portland top={top['Portland']}")
    expect(top["Columbus"] == ("US", "Ohio"), f"Columbus top={top['Columbus']}")
    expect(top["Toronto"][0] == "CA", "Toronto should be CA first")
    return (f"ambiguity ordered by population OK; NO fuzzy matching: Huston->{top['Huston']}, "
            f"Dalas->{top['Dalas']}; ORD->{top['ORD']}; 'Space Needle'->{top['Space Needle']}")


def check_archive(c: httpx.Client) -> str:
    lat, lon = HUBS_SAMPLE["Denver"]
    r = c.get(ARCHIVE_URL, params={"latitude": lat, "longitude": lon, "start_date": "2025-01-01",
                                   "end_date": "2025-12-31", "daily": ",".join(DAILY_VARS),
                                   "timezone": "auto", "models": "era5_seamless"})
    r.raise_for_status()
    d = r.json()
    save("open_meteo_archive_denver_2025.json", d)
    daily = d["daily"]
    expect(len(daily["time"]) == 365, "expected 365 days")
    nulls = {k: v.count(None) for k, v in daily.items() if k != "time"}
    expect(not any(nulls.values()), f"nulls: {nulls}")
    snow = daily["snowfall_sum"]
    return (f"365 days, 0 nulls, grid=({d['latitude']:.3f},{d['longitude']:.3f}) elev={d['elevation']}m; "
            f"snow>=0.1cm {sum(s >= 0.1 for s in snow)} d, >=2.5cm {sum(s >= 2.5 for s in snow)} d; "
            f"X-RateLimit-Cost={r.headers.get('X-RateLimit-Cost')} "
            f"(1y x {len(DAILY_VARS)} vars; 10y measured at ~157)")


def check_ghcn(c: httpx.Client) -> str:
    r = c.get(GHCN_URL, params={"dataset": "daily-summaries", "stations": "USW00003017",
                                "startDate": "2025-01-01", "endDate": "2025-12-31",
                                "dataTypes": "SNOW,PRCP", "units": "metric", "format": "json"})
    r.raise_for_status()
    rows = r.json()
    save("ghcn_den_2025.json", rows)
    snow_mm = [float(x["SNOW"]) for x in rows if x.get("SNOW") not in (None, "")]
    return (f"DEN station: {len(snow_mm)} days reported, total {sum(snow_mm) / 10:.1f} cm, "
            f"days>=0.1cm {sum(s >= 1 for s in snow_mm)}, >=2.5cm {sum(s >= 25 for s in snow_mm)}")


def check_nri(c: httpx.Client) -> str:
    fields = (["STCOFIPS", "STATEABBRV", "COUNTY", "NRI_VER"]
              + [f"{h}_AFREQ" for h in NRI_HAZARDS] + [f"{h}_RISKR" for h in NRI_HAZARDS])
    sample = {}
    for name, (lat, lon) in HUBS_SAMPLE.items():
        r = c.get(f"{NRI_URL}/query", params={
            "geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint", "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects", "outFields": ",".join(fields),
            "returnGeometry": "false", "f": "json"})
        r.raise_for_status()
        d = r.json()
        expect("error" not in d, str(d.get("error")))
        sample[name] = d["features"][0]["attributes"]
    count = c.get(f"{NRI_URL}/query", params={"where": "1=1", "returnCountOnly": "true", "f": "json"}).json()
    save("nri_points.json", {"count": count.get("count"), "counties": sample})
    versions = {a["NRI_VER"] for a in sample.values()}
    # Null-semantics check: AFREQ is null exactly when the rating is "Not Applicable".
    pairs = {(a[f"{h}_RISKR"], a[f"{h}_AFREQ"] is None) for a in sample.values() for h in NRI_HAZARDS}
    bad = [p for p in pairs if (p[0] == "Not Applicable") != p[1]]
    expect(not bad, f"null semantics differ: {bad}")
    return (f"{count.get('count')} counties, version={versions}; county lookup by point OK; "
            f"null AFREQ <=> 'Not Applicable'; flood field is IFLD (no RFLD); "
            f"Miami CWAV={sample['Miami']['CWAV_AFREQ']:.2f}/yr (relative-to-local cold wave)")


def check_nws(c: httpx.Client) -> str:
    h = {"User-Agent": USER_AGENT, "Accept": "application/geo+json"}
    lat, lon = HUBS_SAMPLE["Houston"]
    r = c.get(NWS_URL, params={"point": f"{lat},{lon}"}, headers=h)
    r.raise_for_status()
    n_houston = len(r.json()["features"])
    bad = c.get(NWS_URL, params={"point": "43.6532,-79.3832"}, headers=h)  # Toronto
    expect(bad.status_code == 400, f"Toronto expected 400, got {bad.status_code}")
    no_ua = c.get(NWS_URL, params={"point": f"{lat},{lon}"}, headers={"User-Agent": ""})
    sample = c.get(NWS_URL, params={"status": "actual"}, headers=h).json()["features"][:3]
    save("nws_alerts_sample.json", {"houston_point_count": n_houston, "sample": sample})
    return (f"Houston point: {n_houston} active; non-US point -> 400; "
            f"missing User-Agent -> {no_ua.status_code}; saved {len(sample)} sample alerts")


# ----------------------------------------------------------------------------- LLM targets
# Wire API: Chat Completions (OpenAI-compatible). Groq's Responses API is beta and rejects
# `store`/`include`; Chat Completions is supported by every candidate provider.

class SampleAnswer(BaseModel):
    """Stand-in for AgentResponse: strict, all fields required, enum included."""
    model_config = ConfigDict(extra="forbid")
    summary: str
    cities: list[str]
    confidence: Literal["high", "medium", "low"]


def strict_schema(model: type[BaseModel]) -> dict:
    s = model.model_json_schema()
    s.pop("title", None)
    for p in s.get("properties", {}).values():
        p.pop("title", None)
    s["additionalProperties"] = False
    s["required"] = list(s["properties"])
    return s


def fn_tool(name: str, description: str, parameters: dict) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
                                             "parameters": parameters, "strict": True}}


WEATHER_TOOL = fn_tool("get_hub_weather", "Return a short weather summary for one logistics hub city.",
                       {"type": "object", "additionalProperties": False, "required": ["city"],
                        "properties": {"city": {"type": "string", "description": "City name"}}})
SUBMIT_TOOL = fn_tool("submit_answer", "Submit the final answer. Call exactly once, as the last step.",
                      strict_schema(SampleAnswer))
ANSWER_FORMAT = {"type": "json_schema",
                 "json_schema": {"name": "SampleAnswer", "strict": True, "schema": strict_schema(SampleAnswer)}}
FAKE_WEATHER = {"Houston": "hot, humid, hurricane season risk", "Miami": "hot, hurricane exposure high"}
RATE_HEADERS = ("x-ratelimit-limit-requests", "x-ratelimit-limit-tokens",
                "x-ratelimit-remaining-requests", "x-ratelimit-remaining-tokens")


def llm_checks(label: str, base_url: str, key_env: str, model: str, max_out: int) -> None:
    from openai import OpenAI  # imported here so --data works without the SDK configured

    key = os.getenv(key_env) or (os.getenv("GOOGLE_API_KEY") if key_env == "GEMINI_API_KEY" else None)
    if not key:
        record(label, "api key present", lambda: (_ for _ in ()).throw(RuntimeError(f"{key_env} not set - skipped")))
        return
    client = OpenAI(api_key=key, base_url=base_url, timeout=90, max_retries=1)
    state: dict[str, Any] = {"parallel": True}

    def create(**kw: Any) -> Any:
        raw = client.chat.completions.with_raw_response.create(model=model, max_completion_tokens=max_out, **kw)
        state["headers"] = {h: raw.headers.get(h) for h in RATE_HEADERS if raw.headers.get(h)}
        return raw.parse()

    def basic() -> str:
        r = create(messages=[{"role": "user", "content": "Reply with the single word: ready"}])
        text = (r.choices[0].message.content or "").strip()
        expect(bool(text), "empty output")
        return (f"model={r.model} reply={text[:20]!r} tokens in/out={r.usage.prompt_tokens}/"
                f"{r.usage.completion_tokens}; limits={state.get('headers')}")

    convo: list[dict[str, Any]] = [
        {"role": "system", "content": ("You are a logistics weather assistant. Use tools for facts. "
                                       "When you have the facts you need, finish by calling submit_answer "
                                       "exactly once.")},
        {"role": "user", "content": "Compare Houston and Miami weather. Look up both cities."}]

    def tools_required() -> str:
        kw: dict[str, Any] = dict(messages=convo, tools=[WEATHER_TOOL, SUBMIT_TOOL], tool_choice="required")
        try:
            r = create(parallel_tool_calls=True, **kw)
        except Exception as e:  # some models reject the parallel flag; retry without and note it
            if "parallel" not in str(e).lower():
                raise
            state["parallel"] = False
            r = create(**kw)
        msg = r.choices[0].message
        calls = msg.tool_calls or []
        expect(len(calls) >= 1, "no tool call despite tool_choice=required")
        convo.append({"role": "assistant", "content": msg.content,
                      "tool_calls": [{"id": c.id, "type": "function",
                                      "function": {"name": c.function.name, "arguments": c.function.arguments}}
                                     for c in calls]})
        for c in calls:
            city = json.loads(c.function.arguments).get("city")
            convo.append({"role": "tool", "tool_call_id": c.id,
                          "content": json.dumps({"city": city, "summary": FAKE_WEATHER.get(city, "n/a")})})
        state["called"] = True
        flag = "flag accepted" if state["parallel"] else "flag REJECTED"
        return (f"{len(calls)} call(s) in one turn ({'parallel' if len(calls) > 1 else 'single'}; {flag}): "
                + ", ".join(f"{c.function.name}({c.function.arguments})" for c in calls))

    def terminal_submit_answer() -> str:
        expect(state.get("called"), "depends on previous check")
        msgs = list(convo)
        trail: list[str] = []
        for _ in range(3):  # model may fetch the remaining city before submitting
            r = create(messages=msgs, tools=[WEATHER_TOOL, SUBMIT_TOOL], tool_choice="required")
            msg = r.choices[0].message
            calls = msg.tool_calls or []
            trail.append("+".join(f"{c.function.name}({c.function.arguments[:40]})" for c in calls) or "no-call")
            submit = [c for c in calls if c.function.name == "submit_answer"]
            if submit:
                ans = SampleAnswer.model_validate_json(submit[0].function.arguments)  # raises if schema violated
                return (f"submit_answer args validate after {len(trail)} round(s) [{' -> '.join(trail)}]: "
                        f"cities={ans.cities} confidence={ans.confidence}")
            msgs.append({"role": "assistant", "content": msg.content, "tool_calls": [
                {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
                for c in calls]})
            for c in calls:
                city = str(json.loads(c.function.arguments).get("city", "")).split(",")[0].strip()
                msgs.append({"role": "tool", "tool_call_id": c.id,
                             "content": json.dumps({"city": city, "summary": FAKE_WEATHER.get(city, "no data")})})
        raise AssertionError(f"never called submit_answer within 3 rounds: {' -> '.join(trail)}")

    def json_schema_output() -> str:
        expect(state.get("called"), "depends on previous check")
        r = create(messages=convo, response_format=ANSWER_FORMAT)
        ans = SampleAnswer.model_validate_json(r.choices[0].message.content or "")
        return f"strict json_schema (no tools) OK: cities={ans.cities}"

    def json_schema_with_tools() -> str:
        """Documented as unsupported on Groq; we record what actually happens."""
        try:
            r = create(messages=convo, tools=[WEATHER_TOOL], tool_choice="auto", response_format=ANSWER_FORMAT)
        except Exception as e:  # noqa: BLE001
            return f"rejected as documented -> use terminal submit_answer tool ({str(e)[:120]})"
        content = r.choices[0].message.content or ""
        return f"accepted (content starts {content[:60]!r})"

    record(label, "basic call + rate-limit headers", basic)
    record(label, "tool_choice=required (+parallel)", tools_required)
    record(label, "terminal submit_answer (strict)", terminal_submit_answer)
    record(label, "json_schema output (no tools)", json_schema_output)
    record(label, "json_schema + tools together", json_schema_with_tools)


def llm_targets() -> list[tuple[str, str, str, str, int]]:
    """(label, base_url, key env, model, max output tokens). Read after load_dotenv()."""
    groq_model = os.getenv("GROQ_MODEL") or "openai/gpt-oss-120b"
    gemini_model = os.getenv("GEMINI_MODEL") or "gemini-3.8-flash"
    return [
        (f"groq:{groq_model}", "https://api.groq.com/openai/v1", "GROQ_API_KEY", groq_model, 2048),
        # Groq free tier caps qwen at 1,000 output tokens/minute (measured 2026-11-25), so cap each call lower.
        ("groq:qwen/qwen3.8-27b", "https://api.groq.com/openai/v1", "GROQ_API_KEY", "qwen/qwen3.8-27b", 800),
        (f"gemini:{gemini_model}", "https://generativelanguage.googleapis.com/v1beta/openai/",
         "GEMINI_API_KEY", gemini_model, 2048),
    ]


# ----------------------------------------------------------------------------- main

def main() -> int:
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", action="store_true")
    ap.add_argument("--llm", action="store_true")
    a = ap.parse_args()
    run_data, run_llm = (a.data or not a.llm), (a.llm or not a.data)

    if run_data:
        with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}) as c:
            record("data", "open-meteo geocoding", lambda: check_geocoding(c))
            record("data", "open-meteo archive (ERA5)", lambda: check_archive(c))
            record("data", "noaa ghcn-daily (DEN station)", lambda: check_ghcn(c))
            record("data", "fema nri (arcgis)", lambda: check_nri(c))
            record("data", "nws alerts", lambda: check_nws(c))
    if run_llm:
        for label, base_url, key_env, model, max_out in llm_targets():
            llm_checks(label, base_url, key_env, model, max_out)

    name = "_verify_report.json" if run_data else "_verify_report_llm.json"
    save(name, {"run_at": datetime.now(timezone.utc).isoformat(), "results": results})
    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
