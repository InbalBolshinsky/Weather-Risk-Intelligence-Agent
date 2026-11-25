"""Step 1: verify every external API with a real call before we rely on it.

Usage:
    python scripts/verify_apis.py            # data APIs + any LLM provider whose key is set
    python scripts/verify_apis.py --data     # data APIs only
    python scripts/verify_apis.py --llm      # LLM providers only

Saves small response samples to tests/fixtures/ (used later by offline unit tests)
and a machine-readable report to tests/fixtures/_verify_report.json.
API keys are read from .env / environment and are never printed or saved.
"""
from __future__ import annotations

import argparse
import json
import os
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

HUBS_SAMPLE = {  # metro-center coordinates, same as the planned hubs.yaml
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
        finding, ok = f"{type(e).__name__}: {e}", False
    ms = int((time.perf_counter() - t0) * 1000)
    results.append({"group": group, "check": name, "ok": ok, "ms": ms, "finding": finding})
    print(f"[{'PASS' if ok else 'FAIL'}] {group:<10} {name:<34} {ms:>6} ms  {finding}")


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
    expect("RFLD_AFREQ" not in fields, "")
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


# ----------------------------------------------------------------------------- LLM providers

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


WEATHER_TOOL = {
    "type": "function", "name": "get_hub_weather", "strict": True,
    "description": "Return a short weather summary for one logistics hub city.",
    "parameters": {"type": "object", "additionalProperties": False, "required": ["city"],
                   "properties": {"city": {"type": "string", "description": "City name"}}},
}
SUBMIT_TOOL = {
    "type": "function", "name": "submit_answer", "strict": True,
    "description": "Submit the final answer. Call exactly once, as the last step.",
    "parameters": strict_schema(SampleAnswer),
}
FAKE_WEATHER = {"Houston": "hot, humid, hurricane season risk", "Miami": "hot, hurricane exposure high"}


def to_input(item: Any) -> dict:
    """Turn a Responses output item back into an input item (we resend history; store=False)."""
    return item.to_dict() if hasattr(item, "to_dict") else item.model_dump(by_alias=True, exclude_none=True)


def llm_checks(provider: str, base_url: str | None, key_env: str, model_env: str, default_model: str) -> None:
    from openai import OpenAI  # imported here so --data works without the SDK configured

    key = os.getenv(key_env)
    model = os.getenv(model_env) or default_model
    group = f"{provider}"
    if not key:
        record(group, "api key present", lambda: (_ for _ in ()).throw(RuntimeError(f"{key_env} not set - skipped")))
        return
    client = OpenAI(api_key=key, base_url=base_url, timeout=90, max_retries=1)
    state: dict[str, Any] = {"include": ["reasoning.encrypted_content"]}

    def create(**kw: Any) -> Any:
        try:
            return client.responses.create(model=model, store=False, include=state["include"], **kw)
        except Exception as e:  # provider may reject `include`; retry once without it and note it
            if state["include"] and "include" in str(e).lower():
                state["include"] = None
                return client.responses.create(model=model, store=False, **kw)
            raise

    def basic() -> str:
        r = create(input="Reply with the single word: ready")
        expect(bool(r.output_text.strip()), "empty output")
        return f"model={r.model} reply={r.output_text.strip()[:20]!r} tokens in/out={r.usage.input_tokens}/{r.usage.output_tokens}"

    convo: list[Any] = [{"role": "system", "content": "You are a logistics weather assistant. Use tools for facts."},
                        {"role": "user", "content": "Compare Houston and Miami weather. Look up both cities."}]

    def tools_required_parallel() -> str:
        r = create(input=convo, tools=[WEATHER_TOOL, SUBMIT_TOOL], tool_choice="required", parallel_tool_calls=True)
        calls = [o for o in r.output if o.type == "function_call"]
        expect(len(calls) >= 1, "no function call despite tool_choice=required")
        for o in r.output:
            convo.append(to_input(o))
        for call in calls:
            args = json.loads(call.arguments)
            convo.append({"type": "function_call_output", "call_id": call.call_id,
                          "output": json.dumps({"city": args.get("city"), "summary": FAKE_WEATHER.get(args.get("city"), "n/a")})})
        state["calls"] = calls
        names = [f"{c.name}({c.arguments})" for c in calls]
        return f"{len(calls)} call(s) in one turn {'(parallel OK)' if len(calls) > 1 else '(NOT parallel)'}: {names}; include={'ok' if state['include'] else 'rejected'}"

    def terminal_submit_answer() -> str:
        expect("calls" in state, "depends on previous check")
        msgs = list(convo)
        for _ in range(3):  # model may fetch remaining cities before submitting
            r = create(input=msgs, tools=[WEATHER_TOOL, SUBMIT_TOOL], tool_choice="required")
            calls = [o for o in r.output if o.type == "function_call"]
            submit = [c for c in calls if c.name == "submit_answer"]
            if submit:
                ans = SampleAnswer.model_validate_json(submit[0].arguments)  # raises if schema violated
                return f"submit_answer args validate: cities={ans.cities} confidence={ans.confidence}"
            for o in r.output:
                msgs.append(to_input(o))
            for c in calls:
                msgs.append({"type": "function_call_output", "call_id": c.call_id, "output": '{"summary":"n/a"}'})
        raise AssertionError("never called submit_answer within 3 rounds")

    def structured_output_with_tools() -> str:
        expect("calls" in state, "depends on previous check")
        r = create(input=convo, tools=[WEATHER_TOOL], tool_choice="auto",
                   text={"format": {"type": "json_schema", "name": "SampleAnswer",
                                    "schema": strict_schema(SampleAnswer), "strict": True}})
        if any(o.type == "function_call" for o in r.output):
            return "model chose another tool call before answering (format not exercised) - inconclusive"
        ans = SampleAnswer.model_validate_json(r.output_text)
        return f"text.format json_schema + tools in same request OK: cities={ans.cities}"

    record(group, "basic call", basic)
    record(group, "tool_choice=required + parallel", tools_required_parallel)
    record(group, "terminal submit_answer (strict)", terminal_submit_answer)
    record(group, "json_schema output with tools", structured_output_with_tools)


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
        llm_checks("xai", "https://api.x.ai/v1", "XAI_API_KEY", "XAI_MODEL", "grok-4.3")
        llm_checks("openai", None, "OPENAI_API_KEY", "OPENAI_MODEL", "gpt-6-luna")

    save("_verify_report.json", {"run_at": datetime.now(timezone.utc).isoformat(), "results": results})
    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
