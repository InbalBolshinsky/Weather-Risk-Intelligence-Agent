# Design — Weather Risk Intelligence Agent

A chat agent that helps logistics analysts see which of 20 (synthetic) US hubs are most exposed to weather
disruption. **Code computes every score, resolves every place and sets confidence; the LLM only picks tools and
explains, and every number it writes is checked against the tool results.** Run instructions: [README](README.md).
Full decision log (D1–D78): [PLAN.md](PLAN.md).

## 1. System architecture

```
Browser (web/index.html) ──POST /api/chat──▶ FastAPI (app/main.py): access code, caps
                                                │
                                                ▼
                  Agent loop (app/agent/loop.py) ◀──▶ LLM layer (app/llm): OpenAI-compatible adapter,
                     │  tool calls / results            fallback chain Groq qwen3.8-27b → gpt-oss-20b
                     ▼
                  Tools (app/agent/tools.py) ──▶ Resolver (app/geo) · Scoring engine (app/scoring)
                                                  · Data clients (app/data): FEMA NRI, Open-Meteo, NOAA, NWS
                                                        │
                                                        ▼
                                          SQLite data/app.db (unpacked from data/snapshot.db.gz)
```

Data flows:
1. **Question:** page → `POST /api/chat` → loop sends prompt + history to the LLM (`tool_choice="required"`) →
   runs the tools it calls → the LLM ends with `submit_answer` → code validates (§6) → turn saved → answer text
   returned (raw tool results only in a `debug` field).
2. **Without the LLM:** `GET /api/rankings` reads the scoring engine directly; `GET /api/status` reports health,
   versions, models and questions left; `/docs` is FastAPI's API page.
3. **Data:** clients fetch through a cached HTTP layer; history is cached permanently. `scripts/refresh_data.py`
   re-downloads hub data; `scripts/make_snapshot.py` packs the cache into the committed snapshot.
4. **Score-change alerts (bonus):** after a data refresh, `scripts/check_scores.py` or `POST /api/alerts/check`,
   code compares hub scores with `data/score_baseline.json` and POSTs meaningful changes to `ALERT_WEBHOOK_URL`.
5. **Deploy:** one Render free web service (`render.yaml`), one uvicorn worker; the snapshot is unpacked on first
   start, so no upstream downloads at boot. Secrets are set only in Render.

## 2. Repository structure

```
app/
  main.py        FastAPI: page, chat, status, rankings, alerts check; access code, caps
  agent/         loop.py (rounds, validation, repair) · tools.py (5 data tools) · prompts.py · schemas.py (answer contract)
  llm/           base.py (neutral types, errors) · openai_compat.py (adapter) · fallback.py (chain, rate-limit waits)
  scoring/       kpis.py · normalize.py · engine.py (scores, confidence, flags, ranking) · service.py (memoized hub
                 scores) · alerts.py (score-change check)
  geo/           resolver.py (hub names, aliases, fuzzy, geocoder; US only) · states.py
  data/          http.py (cached, retrying HTTP) · open_meteo.py · fema_nri.py · ghcn.py · nws.py · series.py
  store/db.py    SQLite schema, counters, snapshot restore
  config.py      paths, hub registry
config/          hubs.yaml (20 hubs, aliases, airport stations) · scoring.yaml (every scoring number) · llm.yaml (chain)
web/             index.html (single-page chat) · icon.png
data/            snapshot.db.gz (committed cache, 2.3 MB) · score_baseline.json (last alerted scores) · app.db (git-ignored)
scripts/         chat_cli.py · refresh_data.py · check_scores.py · verify_apis.py · model_bakeoff.py · make_snapshot.py
tests/           92 offline tests in 6 files; fixtures/ (saved API responses); scripted fake model, no network
evals/           cases.py (38 cases + checks) · run_evals.py · merge_reports.py · results/ (reports per run + combined)
render.yaml      Render Blueprint · requirements.txt / requirements-dev.txt · .env.example
```

## 3. Data storage

| What | Where | Why |
|---|---|---|
| Upstream responses (`http_cache`), FEMA county table (`nri_counties`) | SQLite `data/app.db` | Fast, offline, no runtime dependency on FEMA or Open-Meteo |
| Conversations (`turns`), daily counters (`usage_counters`) | same SQLite file | Queried per session and per day |
| Pre-warmed cache | `data/snapshot.db.gz`, committed | A fresh host or clone answers at once, without ~60 upstream calls |
| Last alerted scores | `data/score_baseline.json`, committed | Must survive restarts; the free host wipes its disk |
| Hubs, scoring numbers, model chain | YAML in `config/` | Reviewed in git; outputs carry the scoring hash (`config_version`) |
| Secrets | `.env` (git-ignored) or Render's dashboard | Never in the repo |

**Why SQLite:** one writer, tiny data, zero operations. Not Postgres (no scale or concurrency need), not JSON
files (sessions and counters are queried). Trade-off: on Render conversations and the daily counter reset on each
restart; scores don't (the snapshot is unpacked again).

## 4. Key decisions

| # | Decision | Alternatives considered | Why |
|---|---|---|---|
| 1 | Code owns every fact (scores, places, confidence); the LLM explains; every number is checked (D1, D24, D28, D44, D52) | LLM computes or ranks; a `resolve_location` tool; model-stated confidence | Answers can't contain invented numbers or guessed places |
| 2 | Own agent loop; final answer via a `submit_answer` tool (D12, D15) | LangChain/LangGraph; JSON response mode | The checks are custom anyway; Groq can't combine JSON mode with tools |
| 3 | Groq free tier, chain qwen3.8-27b → gpt-oss-20b (D53, D56) | gpt-oss-120b (5/16), Gemini 3.6 flash (0/16) and 3.5 flash-lite (1/16), paid models | 16-question comparison through the real agent: 14/16 and 9/16; free tier only |
| 4 | FEMA event frequency + observed weather, cross-checked; fixed scales, FEMA to the national p99 (D5, D30, D31, D33, D35) | FEMA risk score or dollar losses; percentile ranks; p95; hurricane intensity | FEMA risk includes social vulnerability and building value; ranks inflate tiny values; p95 capped 25 values at 100; intensity left out for time |
| 5 | Weighted mean + flags that never change a score (extreme ≥ 80, winter "rare here") (D34, D36, D37) | Worst hazard, power mean, equal weights, FEMA loss weights, region-relative scores | Additive and explainable; extremes and preparedness still visible |
| 6 | 20 hubs; any US place profiled on demand (D10) | A precomputed all-US city table | Open-Meteo's free 10K calls/day can't cover ~19K places |
| 7 | SQLite + YAML (D3) | Postgres; JSON files | §3 |
| 8 | Render free tier, committed snapshot, access code + caps, accept the 15-min sleep (D59–D61, D72) | HF Docker Spaces (paid), Fly/Railway/Koyeb (no free tier), keep-awake ping, user accounts | Free and simple; protects the LLM quota; the local run is the fallback |
| 9 | 38 evals graded in code, every answer printed for human review (D64, D65) | LLM-as-judge | Cost and free-tier quota; deterministic grading |
| 10 | Score alerts triggered by events, committed baseline, meaningful changes only (D76) | Schedule; SQLite baseline; any change | Scores change only on a data/config update; the host wipes SQLite |

## 5. Scope and assumptions

- **Hubs** (`config/hubs.yaml`): 20 synthetic hubs at metro centers, every Census region rankable (Midwest 6,
  South 7, Northeast 2, West 5), every hazard regime at least twice.
- **Exposure** = long-run hazard frequency at the hub (2016–2025 window), not financial loss. Live NWS alerts are a
  separate layer and never change a score. "Last year" = previous calendar year; regions = Census regions.
- **Out of scope:** forecasts beyond NWS alerts, costs/ROI, site specifics (flood zones, elevation), network
  effects, climate trends, wildfire/drought/earthquake, hurricane intensity, non-US places, non-English, voice.

## 6. Agent

**Tools** (place names passed as the user wrote them, ≤ 4 per call):

| Tool | Returns |
|---|---|
| `rank_hubs(hazard, region, top_n)` | Ranking (top 5 by default), confidence + reason, flags, tie groups |
| `get_risk_profile(places)` | WDRI, hazard scores, drivers, confidence, flags; 2+ places adds a computed comparison |
| `get_weather_stat(places, metric, start, end)` | Day counts and %: NOAA airport station for hubs (≤ 30 years), grid model alongside or for other places (≤ 10 years) |
| `get_active_alerts(places)` | Live NWS alerts |
| `get_methodology(topic)` | The method, read from `scoring.yaml` |

**Confusing input** (resolver, in code): exact hub name/alias/airport code → hub; typo → fuzzy ≥ 85 accept,
70–85 ask, none under 4 letters; shared name → the hub wins, a US namesake ≥ 50K is mentioned (Portland);
ambiguous US name → ask (Springfield); other US place → live profile, "not one of our hubs" (25/day); non-US →
out of scope unless a same-name US city ≥ 50K exists (Birmingham → AL); landmark → the model retries with the city.

**Validation** of every `submit_answer`: strict schema, then semantics (hub IDs exist, clarifications have a
question); **grounding** (every number must appear in a tool result, the conversation or the prompt); the
**confidence line** must state the code's level (lowest of the turn) and, if not high, the conflicting facts. One
repair retry; then code appends an "unverified numbers" note or writes the confidence line itself. Max 8 rounds.
When all models are rate-limited: wait once (≤ 30 s), or tell the user the real wait.

## 7. Scoring (`config/scoring.yaml`)

**WDRI (0–100)** = weighted mean of five hazard scores; each hazard = weighted mean of its KPIs.

| Hazard (weight) | KPIs: FEMA NRI county frequency + observed weather at the hub (ERA5) |
|---|---|
| Winter (0.25) | FEMA winter weather, ice storm; days ≥ 1 in snow; ice days (high ≤ 32°F) |
| Hurricane (0.25) | FEMA hurricane (counts events, not intensity) |
| Flood (0.20) | FEMA inland + coastal flooding; days ≥ 2 in rain |
| Severe storms (0.20) | FEMA tornado, hail, strong wind; days with gusts ≥ 47 mph |
| Heat (0.10) | FEMA heat wave; days ≥ 95°F; days feels-like ≥ 105°F |

- **Scales:** observed KPIs on fixed documented scales; FEMA linear to the national 99th-percentile county. Both
  absolute: adding a hub changes no score.
- **Flags** (never change a score): extreme hazard ≥ 80; winter "rare here" (few snow/ice days, so less prepared);
  weight-sensitive (rank moves ≥ 3 under equal weights); ties within 5 points.
- **Confidence:** lowered for missing data, known location caveats, or when FEMA and observed weather rank a hub
  ≥ 10 places apart (e.g. Phoenix flood: FEMA 8.5 events/yr in the county vs 0.0 heavy-rain days/yr at the hub).

## 8. Data sources

| Source | Use | Notes |
|---|---|---|
| FEMA National Risk Index | Hazard frequencies, 3,232 counties | December 2025 release |
| Open-Meteo archive (ERA5, `era5_seamless`) + geocoding | Observed KPIs, grid stats, non-hub places | Free tier: 10K calls/day, non-commercial; ~157 calls per 10-year place |
| NOAA GHCN-Daily | Airport station stats for hubs | 17 of 20 stations report snow (MIA, PHX, LAX use the grid) |
| NWS alerts | Live alerts | US only |

## 9. Evaluation

38 end-to-end cases in 5 groups (the brief's questions, follow-ups, confusing input, scope/safety, coverage),
graded in code with facts computed live from the engine, plus pairing checks (hub and number in one sentence).
**Result: 34/38 after human review** ([report](evals/results/eval_combined.md)). It combines, per case, the latest
valid result of the runs on the shipping chain on 2026-11-25: run 1 (log only, its answers weren't saved) for 23
cases, run 14:42 UTC for 6, run 19:39 for 8, run 19:49 for 1. Single-model runs (15:03, 15:11, 19:05) are kept as
comparisons, not counted. Failures: C11 and E8 (gpt-oss-20b skipped a rule), E4 (qwen called inland hubs "Gulf
Coast"), D7 (passed the checks but stated a wrong limit; failed on review). Later code changes (D75, D77, D78) were
verified with deterministic checks only; after D75, case D3 passed once on each model.

## 10. Known limitations

- **Free-tier LLMs:** qwen3.8-27b is a Groq preview model; the backup gpt-oss-20b follows rules less well. Groq
  allows 200K tokens/day per model, about 6–8K tokens per answer in the evals.
- **Grounding is lexical:** a number must exist in the data, but the check can't tell whether it's attached to the
  right hub; claims without numbers aren't checked. Numbers in the user's own message count as grounded.
- **Unvalidated scores:** no real closure data, so thresholds and weights are documented judgment calls. FEMA data
  is county-level; hurricane scores count events (Boston's county equals Houston's).
- **Prompt-only rules:** staying in scope and ignoring instructions inside tool data (covered by evals).
- **Free hosting:** sleeps after 15 idle minutes (~1 min wake-up, the page retries); restarts clear conversations.
- **Operations:** manual data refresh; Open-Meteo's free tier is non-commercial.
