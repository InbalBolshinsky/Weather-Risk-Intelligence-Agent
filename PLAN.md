# PLAN — Weather Risk Intelligence Agent

Living document. Updated as decisions are made; see **Decision log** at the bottom.
Status: **v1.4 — steps 1 and 2 done (data APIs + Groq verified; data layer built, all 20 hubs cached). Open:
model order on Groq and backup provider (§2.6). Next: step 3 (scoring engine).**

**Guiding principle for every choice:** what makes the best agent for this assignment, judged by
**clarity, control, reliability, and how simple it is to run**.

---

## 0. One-paragraph summary

A Python service with a **deterministic scoring engine** that turns public hazard + weather data into a
per-hub **Weather Disruption Risk Index (WDRI, 0–100)** with per-hazard sub-scores, confidence, and
explicit assumptions. An **LLM agent** (Groq free plan, with a fallback chain behind one provider interface)
sits on top: it resolves locations, calls the engine through a small set of typed tools, and returns a
**schema-validated JSON response** that the chat UI renders. The LLM never computes or invents numbers.
It routes, explains, and asks clarifying questions. Deployed to a public URL.

---

## 1. Plan, assumptions, and scope

### 1.1 Work plan (time is flexible; budgets are estimates)

| # | Step | Budget | Output |
|---|------|--------|--------|
| 0 | Plan + decisions (this doc) | 0:30 | PLAN.md |
| 1 | **Verify every external API with a real call** (data APIs + both LLM providers), save sample responses as fixtures | 1:00 | `scripts/verify_apis.py`, `tests/fixtures/*.json`, findings in §7 |
| 2 | Data clients (Open-Meteo, NRI, NWS, **NOAA station**) + SQLite cache + `refresh` script (pre-warm all hubs, verify each hub's station coverage) | 1:15 | `app/data/*`, `app/store/*`, `config/hubs.yaml` |
| 3 | Scoring engine + `scoring.yaml` + unit tests | 1:15 | `app/scoring/*`, `tests/test_scoring.py` |
| 4 | Agent: provider layer, tools, system prompt, JSON schema, validation/repair loop | 1:30 | `app/agent/*`, `app/llm/*` |
| 5 | FastAPI endpoints + single-page chat UI | 0:45 | `app/main.py`, `web/index.html` |
| 6 | Eval set + runner (on both providers) | 0:45 | `evals/*` |
| 7 | Deploy to a URL | 0:30 | Dockerfile / platform config, URL in README |
| 8 | ARCHITECTURE.md, README run instructions, export AI session | 0:30 | `docs/`, `session/` |
| — | *Stretch, in order:* score-change webhook → hurricane-track stat | — | — |

**Time is flexible** (user, 2026-11-25), so nothing is cut in advance. The order above is still the priority
order, and if something must slip, these go first: (1) the observed-gust KPI, (2) ad-hoc non-hub scoring,
(3) the weight-sensitivity check. Evals, the deploy, and the docs are never cut.

### 1.2 Assumptions (stated in the README and returned in agent output where relevant)

- **Hubs are synthetic: 20 illustrative hubs** in major US logistics metros (`config/hubs.yaml`, list in §1.4).
  Coordinates = metro center, not a real facility address.
- **"Exposure" = long-run historical hazard frequency/intensity at the hub location**, not financial loss.
- **Investment prioritization is structural, not today's weather.** The WDRI uses multi-year history. Active
  NWS alerts are a separate "current conditions" layer and **never** change the WDRI.
- **Regions = US Census Bureau regions** (Midwest = IL, IN, MI, OH, WI, IA, KS, MN, MO, NE, ND, SD).
- **"Last year" = previous calendar year relative to today** (today = 2026-11-25 → 2025).
- **Baseline window = 10 full calendar years (2016–2025)** for observed weather KPIs.
- **Reanalysis is not a weather station.** Open-Meteo's archive is ERA5-based (gridded). Snowfall, gusts, and
  apparent temperature are model estimates; we say so and lower confidence where it matters.
- **Snow-day questions report two thresholds:** "measurable" = **≥ 0.1 in** (NWS definition; less is a "trace")
  and **≥ 1 in** (a standard NOAA climate-normals threshold, not an official "disruptive" definition). What counts
  as disruptive snow depends on the region (1" barely matters in Minneapolis but can stop Dallas), so we show both
  and don't call either one "significant".
- **Factual history questions about a hub use its airport weather station** (NOAA GHCN-Daily), with the
  grid-model value alongside it. The station is at the airport, not the metro center, and answers name the
  station. Risk scores use the grid model for every hub (one consistent method).
- **Units:** metric internally; the UI shows imperial alongside it.
- Hazards in scope: **winter, hurricane/tropical, flood, severe convective (tornado/hail/wind), heat**.

### 1.3 Out of scope (explicitly NOT doing)

- Voice. User accounts / multi-user auth (only a shared demo token + request cap, because the URL is public and LLM calls cost money).
- Financial loss modeling, ROI of upgrades, downtime cost.
- Climate projections; forecasts beyond what NWS alerts already say.
- Site-level analysis (FEMA flood zones at the facility, elevation, drainage).
- Network effects between hubs.
- Hazards: wildfire, drought, earthquake (NRI has them; adding one = config + KPI mapping).
- Non-US locations.
- **A precomputed table of every US city** (see §4.6 for why, and for what "expand to all US" means instead).
- LLM-as-judge evals at scale.
- Scheduled alerts: stretch only (manual `POST /api/alerts/check` + webhook).

### 1.4 Hub list (20) — why 20

There's nothing special about 20; it's a config file. It's the smallest set that does all of these:
- **Every Census region has enough hubs to rank**: "Midwest" questions need ≥ 5 candidates for a ranking to mean anything.
- **Every hazard regime is covered at least twice**, so each hazard ranking has contrast: Gulf hurricanes
  (Houston, Miami), Plains tornado/hail (Dallas, Kansas City), Great Lakes snow (Chicago, Detroit), mountain snow
  (Denver), desert heat (Phoenix, Dallas), Pacific NW rain/flood (Seattle, Portland).
- **Includes the assignment's named cities**, plus deliberately ambiguous names for the confusing-input evals (Portland, Columbus).
- **Small enough** to pre-warm in about a minute, far inside the free-tier API limits, and to check every score by hand.

| Region | Hubs |
|---|---|
| Midwest (6) | Chicago IL, Indianapolis IN, Columbus OH, Detroit MI, Minneapolis MN, Kansas City MO |
| South (7) | Dallas TX, Houston TX, Miami FL, Atlanta GA, Memphis TN, Charlotte NC, Louisville KY |
| Northeast (2) | Newark NJ, Boston MA |
| West (5) | Denver CO, Phoenix AZ, Los Angeles CA, Seattle WA, Portland OR |

---

## 2. Architecture

### 2.1 Components and data flow

```
 Browser (web/index.html, vanilla JS)
     │  POST /api/chat {session_id, message}      (X-Demo-Token header)
     ▼
 FastAPI (app/main.py) ───────────── GET /api/hubs, /api/rankings, /api/hubs/{id}/risk  (no LLM)
     │
     ▼
 Agent loop (app/agent/loop.py)  — provider-agnostic; works on a neutral message format
     │
     ├── LLM provider layer (app/llm/)     LLMProvider interface → GrokProvider (primary), OpenAIProvider (backup)
     │                                     FallbackProvider wraps both; the only code that knows a vendor exists
     │
     ▼
 Tools (app/agent/tools.py)  — thin, typed wrappers, no business logic
     ├── Location resolver (app/geo/)      hub alias/fuzzy match → Open-Meteo geocoding (US-only)
     ├── Scoring engine (app/scoring/)     pure functions: raw KPIs → normalized → hazard → WDRI
     └── Data clients (app/data/)          Open-Meteo archive, FEMA NRI, NWS alerts
              │
              ▼
          SQLite (data/app.db): http_cache, nri_counties, kpi_snapshots, score_snapshots, sessions/messages
```

The scoring engine doesn't know an LLM exists. It is exposed to the agent (through tools) and directly
through REST, so it's testable without an LLM, and the numbers are identical with or without the agent.

### 2.2 Decisions and trade-offs

| Decision | Choice | Why | Alternative considered |
|---|---|---|---|
| Language / server | Python 3.11 + FastAPI | Assignment prefers Python; Pydantic models + OpenAPI for free | Flask, Django |
| LLM | **Groq (free plan)** through the `openai` Python SDK (`base_url=https://api.groq.com/openai/v1`), Chat Completions API. Backup provider: open (§2.6) | User's choice (an earlier "Grok" was a typo for Groq); free; OpenAI-compatible, so one adapter covers any OpenAI-compatible vendor | Vendor SDKs per provider: more code, no gain |
| Agent loop | **Our own loop** (~80 lines) on a neutral message format | Full control over validation/repair, table hydration, provider fallback, and eval determinism; the whole control flow is readable in one file. Decided in §2.7 | LangChain / LangGraph (§2.7) |
| Chat UI | One static `web/index.html` served by FastAPI | One process, one deploy, clearly uses the API; renders the structured fields | Streamlit: a second process |
| Conversation state | Server-side, by `session_id`, in SQLite, **in our own neutral format** (not provider-native) | Follow-ups need prior tool results, and a neutral format lets a session switch providers mid-conversation | Client sends history; provider-side state (`store=true`), which would lock a session to one vendor |
| Freshness | `refresh_data.py` pre-warms all hubs; runtime reads the cache and falls back to live | The live demo must not depend on 60 upstream calls succeeding in the moment | Fully live |
| Hosting | Container on a free/cheap PaaS (Render or Fly; pick and verify at step 7), with a pre-built SQLite snapshot baked into the image | URL preferred by the assignment; a baked snapshot = the demo works even if upstream APIs are slow | Bare VM: more ops |

### 2.3 Data storage: SQLite (one file)

- `http_cache(source, request_key, response_json, fetched_at)`: raw upstream responses (reproducibility, offline demo).
- `nri_counties(county_fips, state, county, nri_version, <hazard AFREQ columns…>)`: **all ~3,200 US counties**, loaded once from the NRI county table (§4.6). Small, and it removes the runtime dependency on ArcGIS.
- `kpi_snapshots(hub_id, kpi, value, unit, window, source, computed_at)`
- `score_snapshots(hub_id, hazard, score, confidence, config_version, data_version, computed_at)`: "what changed" and the stretch alert.
- `sessions(id, created_at)`, `messages(session_id, seq, role, content_json, provider, model)`.

Config (hubs, weights, anchors, thresholds) is **YAML in git**; `config_version` = hash of the YAML.

**Why SQLite:** single writer, tiny data, zero ops, stdlib driver, ships inside the container image.
**Not Postgres:** no concurrency or scale need; it would add a managed service to the deploy.
**Not flat JSON:** we query snapshots over time and sessions. **Caveat:** PaaS disks are ephemeral, so sessions
reset on redeploy. That's acceptable for a demo; hub data is baked into the image.

### 2.4 Repository structure

```
.
├── README.md                  # how to run, URL, assumptions, limitations
├── PLAN.md                    # this file
├── docs/ARCHITECTURE.md       # deliverable: architecture, repo structure, storage choice
├── config/
│   ├── hubs.yaml              # hub_id, name, state, region, lat, lon, aliases (incl. IATA codes)
│   ├── scoring.yaml           # KPIs, anchors, weights, thresholds, confidence rules
│   └── llm.yaml               # provider order, model IDs, timeouts, retries (keys come from env only)
├── app/
│   ├── main.py                # FastAPI app, routes, static UI, demo-token check
│   ├── llm/
│   │   ├── base.py            # LLMProvider protocol + neutral types (Message, ToolCall, LLMTurn)
│   │   ├── openai_compat.py   # one adapter for any OpenAI-compatible endpoint (xAI, OpenAI)
│   │   └── fallback.py        # FallbackProvider: primary → backup on availability errors
│   ├── agent/
│   │   ├── loop.py            # tool-use loop, repair retry, iteration cap
│   │   ├── prompts.py         # system prompt (templated with today's date + hub list)
│   │   ├── tools.py           # tool specs (strict JSON schemas) + dispatch
│   │   └── schemas.py         # Pydantic: AgentResponse + tool I/O models
│   ├── scoring/               # kpis.py, normalize.py, engine.py
│   ├── geo/resolver.py        # hub matching, geocoding, ambiguity detection
│   ├── data/                  # open_meteo.py, fema_nri.py, nws.py, http.py (cache + retries)
│   └── store/db.py
├── web/index.html
├── scripts/                   # verify_apis.py, refresh_data.py
├── evals/                     # cases.yaml, run_evals.py, results/
├── tests/                     # pytest, offline, fixture-based (incl. a FakeProvider for the loop)
├── Dockerfile
└── session/                   # exported AI-assistant session (deliverable)
```

### 2.5 API used by the chat

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/chat` | `{session_id?, message}` → `{session_id, response: AgentResponse, debug: {tool_calls, provider, model, latency_ms, tokens}}` |
| GET | `/api/hubs` | Hub list with region |
| GET | `/api/rankings?hazard=overall&region=midwest` | Deterministic ranking, no LLM |
| GET | `/api/hubs/{hub_id}/risk` | Full breakdown, no LLM |
| GET | `/api/methodology` | Weights/anchors/assumptions + `config_version` |
| GET | `/api/health` | Liveness, data freshness, which LLM providers are configured |
| POST | `/api/alerts/check` | *(stretch)* recompute, diff vs last snapshot, webhook if Δ ≥ threshold |

### 2.6 LLM provider layer (isolation + fallback)

```python
class LLMProvider(Protocol):
    name: str
    def complete(self, messages: list[Message], tools: list[ToolSpec],
                 tool_choice: Literal["auto", "required"]) -> LLMTurn: ...
# LLMTurn = {text, tool_calls: [ToolCall(id, name, args)], finish_reason, usage, provider, model}
```

- **One adapter, two configs.** Both vendors are reached through the `openai` SDK; the adapter differs only in
  `base_url`, API key env var, and model ID. Vendor quirks (e.g. schema dialect) are handled inside the adapter.
- **Wire API: Chat Completions (decided from step-1 evidence, D21).** Groq's Responses API is beta and rejects
  `store`/`include`; Chat Completions is supported by Groq and every free alternative we checked (Gemini's
  OpenAI-compatible endpoint, Cerebras, OpenRouter).
- **Fallback policy (`FallbackProvider`):** switch to the backup **only on availability errors**: 429 after
  2 backoff retries, 5xx, timeout, or connection error. The switch covers the rest of that turn, which is
  possible because history is stored in our neutral format. **No fallback on schema/validation failures**; those
  go to the repair retry, because a different model wouldn't fix a bad prompt. `debug.provider` records who answered.
- **Evals run per provider** (`--provider grok|openai`), so the comparison table is the evidence for which
  model is primary.

**Verified with real calls (2026-11-25, `scripts/verify_apis.py --llm`, Groq free plan):**

| | `openai/gpt-oss-120b` (production model) | `qwen/qwen3.8-27b` (preview model) |
|---|---|---|
| Free limits (from response headers) | 1,000 requests/day, 8,000 tokens/min (docs: 200K tokens/day) | Same, **plus 1,000 output tokens/min** (a request asking for 2,048 max tokens is refused) |
| `tool_choice="required"` | ✅ | ✅ |
| Parallel tool calls | ❌ one call per turn (flag accepted, ignored), as documented | ✅ two calls in one turn |
| Terminal `submit_answer` (strict schema, Pydantic-validated) | ✅ after 2 rounds | ✅ after 1 round |
| Strict `json_schema` output, no tools | ⚠️ failed once the history contained tool calls ("Tool choice is none, but model called a tool") | ✅ |
| `json_schema` + tools in one request | ❌ 400 "json mode cannot be combined with tool/function calling", as documented | ❌ same |

Earlier candidates **xAI (Grok) and OpenAI were dropped** (the user meant Groq; neither has a usable free tier).
Free alternatives checked in official docs: **Gemini API** (ongoing free tier; free-tier content is used to
improve Google products), Cerebras (no permanent free tier; $5 trial credits), OpenRouter `:free` (20 requests/min,
50/day), Mistral (API free tier not verifiable: docs 404).

**Final-answer mechanism (confirmed, D12):** the agent ends by calling a terminal tool `submit_answer(AgentResponse)`,
with `tool_choice="required"` on every model call; the turn ends when it calls `submit_answer`. Step 1 showed
this is the **only** working option on Groq, because JSON-schema output can't be combined with tools there.

**Token budget (the binding constraint, replaces the cost estimate):** money is $0, but the free plan allows
8,000 tokens/min and ~200K tokens/day per model. A turn is about 3 calls, so the design must:
(1) keep the system prompt + tool definitions compact (target ≤ 2.5K tokens), (2) return trimmed JSON from tools,
not raw series, (3) cap output tokens per call (qwen: 800), (4) treat a 429 as an availability error, which moves
the call to the next model in the chain, and (5) have the eval runner record tokens per case. Limits are per
model, so two Groq models on one key double the capacity. The public URL still gets a demo token +
per-session cap, now to protect the daily token budget rather than money.

### 2.7 Decision: our own agent loop, no LangChain / LangGraph

**Decided (user, 2026-11-25): option A, our own loop.** Options considered: (A) own loop, (B) LangChain
`create_agent`, (C) LangGraph explicit graph, (D) LangChain model clients only, inside our loop.

Reasoning, judged on clarity, control, reliability, and simplicity to run:
- **What a framework would save is small and generic:** the provider clients + fallback (~150 lines), tool-schema
  generation (Pydantic already does it), and a final structured answer (our `submit_answer` is the same pattern).
- **What makes this agent trustworthy is custom either way:** table hydration by tool-call ID, the grounding
  check, semantic validation + one repair retry, the deterministic ambiguity policy, and fallback only on
  availability errors (LangChain's `with_fallbacks` fires on any exception by default). In a framework these
  become middleware or custom nodes inside a lifecycle we don't own.
- **Control and reliability:** vendor quirks (xAI's schema dialect, Responses vs Chat Completions) stay in our
  ~100-line adapter, where we can see and test them, instead of inside an integration package.
- **Clarity:** the whole control flow (call model → run tools → validate → repair → hydrate) is readable in one file.
- **Simple to run:** fewer dependencies (no 4–5 framework packages with a history of breaking changes); one
  SQLite schema we own, instead of a framework checkpointer's serialized format next to ours.

**What we give up:** ready-made tracing (LangSmith) and ready-made memory. We cover these with `debug` in the API
response (tool calls, provider, tokens, latency) and our own `messages` table.

**When to revisit:** multi-step workflows, human approval steps, or long-running jobs → LangGraph, whose explicit
graph would map 1:1 onto our pipeline. Migration stays cheap: tools are plain functions, schemas are Pydantic,
history is in a neutral format.

---

## 3. Agent tools and system prompt

### 3.1 Tools (6 core + terminal + 1 optional)

Every tool has a strict JSON schema (`additionalProperties: false`, all fields required with nullable optionals,
enums for hazard/region/metric). This is the common subset that both vendors enforce.

| Tool | Input | Returns | Why it exists |
|---|---|---|---|
| `resolve_location` | `query` | `status: hub_match \| single_match \| ambiguous \| not_found \| non_us`, candidates (name, state, lat, lon, hub_id, population), `match_method` | The LLM never guesses coordinates or silently picks "Portland" |
| `list_hubs` | `region?` | hubs | "Midwest" is config, not the model's geography |
| `rank_hubs` | `hazard: overall\|winter\|hurricane\|flood\|severe_convective\|heat`, `region?`, `top_n?` | ordered list: score, confidence, dominant hazard, `tie_groups`, `weight_sensitive` flag | The ranking is **the** deterministic KPI; the LLM only narrates it |
| `get_risk_profile` | `targets` (1–4 hub_ids or resolved points) | WDRI, hazard scores, KPI raw/normalized/weight/contribution, confidence + reasons, sources | "Why is Dallas high?", "compare Miami vs Houston" |
| `get_weather_stat` | `target`, `metric: snow_days\|precip_days\|heavy_precip_days\|freezing_days\|high_gust_days\|hot_days`, `start_date`, `end_date`, `threshold?` | count, total days, %, threshold used, coverage, **primary source** (hub airport station for snow/precip/temperature; grid model for gusts, apparent temperature, and non-hub locations) **+ the grid-model value alongside** | Ad-hoc factual questions; the arithmetic happens in code. The station is ground truth; showing both makes the uncertainty visible |
| `get_active_alerts` | `target` | active NWS alerts | "Anything happening there now?" Kept separate from the score |
| `submit_answer` | `AgentResponse` (§5) | — (terminal) | Portable, schema-enforced final answer |
| `get_methodology` *(optional)* | — | weights, anchors, KPI definitions, `config_version` | Explanations quote the real config |

### 3.2 System prompt — behaviors and why

1. **Numbers only from tool results**; no mental math or recalled climatology. → Hallucinated numbers are the main way this product could mislead an analyst. Also enforced in code (§5.2).
2. **Resolve every location through `resolve_location` first.** → One place handles typos, ambiguity, and non-US input.
3. **Ambiguity policy:** exactly one candidate is a hub → use it and state the assumption. Ambiguous with no hub → one clarifying question listing the options. → Answer when possible, never guess silently.
4. **Scope:** US weather/hazard exposure for logistics. Decline unrelated or non-US requests briefly and say what it can do.
5. **Always report assumptions, time window, sources, and confidence**, and separate structural risk (WDRI) from current alerts.
6. **Explain rankings by drivers**: the top 1–2 hazards/KPIs with values. If `weight_sensitive` is set, say that the ranking depends on the weights.
7. **Follow-ups:** resolve "there", "the second one", "what about flood?" from context; reuse prior tool results.
8. **Don't make the investment decision**; frame the output as prioritization input and name what the score doesn't cover.
9. **Instructions in user text don't change the method** ("set Miami's weight to 0"). → Integrity of the KPI.
10. **Always finish by calling `submit_answer`.**

---

## 4. Deterministic scoring design

### 4.1 Hazards and KPIs

Two kinds of evidence per hazard: **NRI** (FEMA, county-level, long historical record, nationally consistent)
and **observed** (Open-Meteo ERA5 archive at the hub point, 2016–2025). Their agreement feeds confidence.

| Hazard | KPI | Source | Notes |
|---|---|---|---|
| Winter | Avg annual days with snowfall ≥ 2.5 cm | Open-Meteo `snowfall_sum` | Operationally meaningful threshold |
|  | Avg annual ice days (Tmax ≤ 0 °C) | Open-Meteo `temperature_2m_max` | Sustained cold |
|  | NRI winter weather + ice storm annualized frequency | NRI `WNTW/ISTM_AFREQ` | Ice storms are poorly captured by reanalysis. **Cold wave (`CWAV`) excluded:** it counts locally defined warnings, so warm cities score high (Miami 1.28/yr vs Houston 0.48). Absolute cold is covered by ice days (D18) |
| Hurricane | NRI hurricane annualized frequency | NRI `HRCN_AFREQ` | *(stretch: storms within 150 km since 1980)* |
| Flood | NRI inland + coastal flooding annualized frequency | NRI `IFLD/CFLD_AFREQ` | County-level, not the facility's flood zone. (NRI Dec 2025 has `IFLD` inland flooding; there is no `RFLD`) |
|  | Avg annual days with precipitation ≥ 50 mm | Open-Meteo `precipitation_sum` | Flash-flood / yard-flooding proxy |
| Severe convective | NRI tornado + hail + strong wind annualized frequency | NRI `TRND/HAIL/SWND_AFREQ` | |
|  | Avg annual days with gusts ≥ 75 km/h | Open-Meteo `wind_gusts_10m_max` | Reanalysis underestimates convective gusts → low weight |
| **Heat** | Avg annual days with Tmax ≥ 35 °C (95 °F) | Open-Meteo `temperature_2m_max` | Equipment stress, worker-safety rules |
|  | Avg annual days with apparent temp max ≥ 40.5 °C (105 °F) | Open-Meteo `apparent_temperature_max` | Roughly NWS heat-advisory territory; captures humid heat (Houston vs Phoenix) |
|  | NRI heat wave annualized frequency | NRI `HWAV_AFREQ` | |

*NRI field names confirmed in step 1 against NRI "December 2025" (see §7.1).*

**Why NRI annualized frequency and not `RISK_SCORE` or EAL:** `RISK_SCORE` mixes in Social Vulnerability and
Community Resilience (they describe the county's population, not our hub). EAL ($) scales with how much
building value is in the county. Frequency is the closest to "how often does this hazard hit here."

### 4.2 Normalization: fixed anchors, not min-max

Each KPI → 0–100 via a piecewise-linear function between anchors in `scoring.yaml`, clipped.
- **Why not min-max/z-score across hubs:** with 20 hubs it's relative and unstable. Adding a hub changes
  everyone's score, which would also fire false "score changed" alerts.
- **Cost:** anchors are judgment calls. **Mitigation now that we store all ~3,200 NRI counties:** NRI KPIs use
  **national county percentiles** (data-driven, not hand-picked). Hand-set anchors remain only for the
  Open-Meteo KPIs, documented with a rationale and set after we've seen the hub spread in step 2.

### 4.3 Aggregation and weights (all in `scoring.yaml`)

```
hazard_score(h) = Σ_k w_k · norm(KPI_k) / Σ_k w_k        (over available KPIs)
WDRI            = Σ_h W_h · hazard_score(h)              (Σ W_h = 1)
```

**Why a weighted arithmetic mean** (and not max, a mean/max blend, or a geometric mean):
1. **Additive = explainable.** "Dallas = 0.20×winter 31 + 0.25×hurricane 12 + … " Each hazard's contribution is
   a number the analyst can see and add up. "Why is Dallas high?" has an exact, checkable answer.
   With `max` or a blend, the explanation becomes "because of whichever term happened to win."
2. **Predictable sensitivity.** Change a weight by 0.05 and the effect on every hub is linear and easy to state. That matters when finance wants to tune it.
3. **Robust to one noisy input.** Any max-based rule lets a single KPI (often from a single coarse source)
   decide the ranking. The mean needs corroboration across hazards.
4. **There's no loss data to calibrate anything fancier.** Every aggregator here is a judgment call, and
   the simplest judgment call is the easiest to defend and to replace later.

**Its real weakness:** it dilutes single-hazard hubs (Miami: extreme hurricane, no winter). Mitigations:
(a) every ranking shows the **dominant hazard** and its score, (b) per-hazard rankings are first-class, and
resilience investments are hazard-specific anyway, (c) a **weight-sensitivity check**: each ranking is also
computed under equal weights, and hubs whose rank moves by ≥ 3 get `weight_sensitive: true`, which the agent must mention.

**Default hazard weights (judgment, documented, by shutdown potential):**

| Hazard | W | Rationale |
|---|---|---|
| Winter | 0.25 | Multi-day closures of roads and yards; frequent in the north |
| Hurricane | 0.25 | Longest full closures (evacuation, power, debris), though rarer |
| Flood | 0.20 | Access and facility damage; localized |
| Severe convective | 0.20 | Short but sudden closures; facility damage |
| Heat | 0.10 | Mostly degrades throughput and labor (work-rest rules), and rarely closes a hub. Equal weighting would treat it as equally disruptive to a hurricane, which contradicts the premise "severe weather **shuts hubs down**" |

- **Missing KPI** → re-weight within the hazard, lower confidence, never impute silently.
- **Ties:** hubs within 5 points form a `tie_group` ("not meaningfully different under this method").

### 4.4 Confidence (deterministic, per hazard, then overall)

Starts at **high**; each rule downgrades it and adds a human-readable reason:
- Observed data coverage < 95% of days → medium.
- Only one source available for a hazard → medium.
- NRI and observed normalized scores disagree by > 40 points → low ("sources disagree").
- Static caveats from config (reanalysis gusts; terrain-sensitive snowfall near mountains) → medium.
- Location resolved by LLM interpretation (landmark) → overall medium.
- *(weather stats)* Station data with ≥ 95% of days present → high. Station gaps → fall back to the grid model.
  A grid-model snow-day count → medium, with the caveat "the grid model overcounts light-snow days" (Denver 2025:
  40 vs 26) and converts snow water to depth at a fixed 7:1 ratio.
- Hub is `weight_sensitive` in the ranking being discussed → stated (doesn't change per-hazard confidence).

Overall = the lowest confidence among hazards contributing ≥ 20% of the WDRI.

### 4.5 What every score output carries

`score`, `hazard_scores`, per-KPI `{raw, unit, normalized, weight, contribution, source}`, `confidence` +
`confidence_reasons[]`, `assumptions[]`, `data_window`, `sources[{name, url, retrieved_at}]`,
`config_version`, `data_version`.

### 4.6 Expanding beyond the 20 hubs — no table of all US cities

**Recommendation: don't precompute every US city.** Expand by **scoring on demand + caching**, and precompute only the sources that are cheap and national.

| Source | All-US coverage? | Precompute for all US? |
|---|---|---|
| FEMA NRI | **Yes**: every county (~3,200) and census tract, as one downloadable table | **Yes**, all counties: one small download, and it gives national percentiles (§4.2) |
| Open-Meteo archive | Yes, any lat/lon (gridded) | **No.** Free tier is < 10,000 calls/day, 5,000/hour, 600/min, and **non-commercial only**. There are ~19,000+ incorporated US places, and multi-year requests likely count as several calls each (verify in step 1). It would take days and violate the spirit of the free tier. Fetch on demand, cache by grid cell. |
| NWS alerts | Yes, any US point | No, real-time only by nature |
| Geocoding | Yes | No, on demand |

So "expand to all US" in the product means: **any US location can be profiled live** (resolve → NRI from the
local county table → one Open-Meteo fetch → cached), labeled "not a hub" and kept out of hub rankings.
A "rank every US county" screen for *new site selection* could run on NRI alone with no API calls, but it's a
different product question. Out of scope today, noted as future work.

---

## 5. JSON schema between the LLM and the code

### 5.1 `AgentResponse` (Pydantic v2 is the source of truth; JSON Schema generated from it)

```jsonc
{
  "response_type": "answer | clarification | out_of_scope | error",
  "intent": "rank | compare | explain | weather_stat | current_alerts | methodology | other",
  "answer_markdown": "string — narrative for the analyst",
  "locations": [{
    "query": "Huston", "resolved_label": "Houston, TX", "hub_id": "HOU | null",
    "resolution": "hub_exact | hub_fuzzy | geocoded | llm_interpreted | unresolved"
  }],
  "table_refs": ["call_…"],          // tool calls whose results the UI renders as tables (server hydrates)
  "key_drivers": [{ "hub_id": "DAL", "hazard": "severe_convective", "kpi": "nri_hail_afreq", "explanation": "…" }],
  "clarifying_question": "string | null",
  "clarification_options": ["Portland, OR", "Portland, ME"],
  "assumptions": ["…"],
  "confidence": "high | medium | low",
  "confidence_reason": "string",
  "suggested_followups": ["…"]
}
```

The schema is written in the **common strict subset** both vendors enforce: every property required (optional ⇒
nullable), `additionalProperties: false`, no regex/length constraints, non-empty enums only.

**The LLM never writes the numbers in tables.** It returns `table_refs` (IDs of the tool calls it cites), and the
server **hydrates** rankings and breakdowns straight from those tool results.

### 5.2 Validation pipeline

1. **Shape at generation time:** `submit_answer` is a strict tool (xAI: always strict; OpenAI: `strict: true`).
2. **Pydantic parse** (catches adapter/vendor drift).
3. **Semantic checks:** `clarification` ⇒ `clarifying_question` set; hub_ids exist; every `table_refs` ID exists in this turn; rank/compare/explain answers called at least one data tool.
4. **Grounding check:** every number in `answer_markdown` must appear in some tool result (with a rounding tolerance). At runtime, failures show a UI warning; in evals it's a graded metric.
5. **On failure:** one repair retry with the errors fed back → otherwise a typed `error` response, logged. Loop capped at 8 iterations.

Constrained decoding guarantees the **shape**, not the **truth**. Steps 3–4 are there for the truth part.

---

## 6. Behavior on confusing input

| Input | Detection (deterministic first) | Agent behavior |
|---|---|---|
| Outside US ("Toronto") | Geocoder `country_code != US` for all top candidates | `out_of_scope`; if a US namesake exists (London, KY), offer it |
| Airport ("ORD", "O'Hare") | IATA/airport aliases in `hubs.yaml` → hub; else geocoder | Resolve to the hub; state the interpretation |
| Landmark ("Space Needle") | Not in aliases; geocoder fails | LLM interprets → `resolve_location("Seattle")`; `llm_interpreted`; confidence ≤ medium |
| Ambiguous ("Portland", "Columbus", "Springfield") | Resolver `ambiguous`, ≥ 2 US candidates | One is a hub → use it and state it; else clarification with options by population |
| Typo ("Huston", "Dalas") | Fuzzy match (rapidfuzz) on hub names/aliases ≥ 85 | Proceed (`hub_fuzzy`) and say so; lower score → clarification |
| Unrelated | Prompt rule 4 | `out_of_scope` + what it can do |
| No location ("is it risky?") | No location and no prior context | Use the context if there is any; else ask, or offer the overall ranking |
| Non-hub US city ("Boise") | `single_match`, `hub_id = null` | Live profile labeled "not one of our hubs"; never in hub rankings |
| Bad time range ("2030", "1850") | Tool validates against the archive's range | Explain the limit; offer the nearest valid range |
| Injection / "change the weights" | Prompt rule 9 | Decline; point to the methodology/config |

Each row becomes at least one eval case.

---

## 7. Data sources

| Source | Role | Status / notes |
|---|---|---|
| Open-Meteo Geocoding | Non-hub location resolution | Filter `country_code=US`; `admin1` + population order the ambiguous options. Our alias/fuzzy layer runs first. |
| Open-Meteo Historical | Observed KPIs + `get_weather_stat` | ERA5 reanalysis. **Free tier verified: < 10k calls/day, 5k/hour, 600/min, non-commercial only** ([terms](https://open-meteo.com/en/terms)). A real deployment needs the paid tier; flagged in the README. Call weighting for long ranges: verify in step 1. |
| NWS alerts | `get_active_alerts` only | Real-time only; needs a generic `User-Agent`. |
| FEMA NRI | Hazard frequencies for 5 hazards | Load the **full county table** once into SQLite (record the NRI version); the ArcGIS point query is used only to find the county for a point, or replaced by the geocoder's county / a FIPS lookup. Verify field names. |
| NOAA IBTrACS / HURDAT2 | Stretch: observed hurricane-track count | NRI's hurricane frequency already derives from tracks. |
| NOAA GHCN-Daily (NCEI Access Data Service) | **Runtime:** primary source for factual hub questions (snow, precip, temperature) from each hub's airport station | No token. Station coverage is checked per hub at refresh; gaps fall back to the grid model. Fetched in standard units (inches, °F), the native measurement units, so the 0.1 in / 1 in thresholds apply exactly. |
| NOAA 1991–2020 Climate Normals (same service) | Eval-only: independent check of the grid model's winter KPI where a station has snow normals (e.g. O'Hare: 27.8 days ≥ 0.1 in, 11.4 days ≥ 1 in per year) | Not every station has snow normals (DEN doesn't; its record starts in 1995). |

### 7.1 Step 1 results — verified with real calls (2026-11-25, `scripts/verify_apis.py`)

All five data APIs pass; fixtures saved in `tests/fixtures/`. LLM checks are written and waiting for API keys.

| Source | Result | Consequences for the design |
|---|---|---|
| Open-Meteo Geocoding | Works; ambiguous names ordered by population (Portland → OR, then ME; Columbus → OH first). **No fuzzy matching:** "Huston" → Huston, **Idaho**; "Dalas" → a village in **Iran**. IATA codes fail ("ORD" → Ordu, Turkey). "Space Needle" → no result. Returns `admin2` (county name), not FIPS. | Our hub alias + fuzzy layer **must run before** the geocoder, and must beat an exact geocoder hit on a tiny place (state the alternative). IATA codes go in `hubs.yaml` aliases. Landmarks → LLM-interpreted path. |
| Open-Meteo Archive | Works; 6 daily variables, 365/365 days, 0 nulls; ~0.5 s. **Cost is reported in headers:** `X-RateLimit-Cost` ≈ days/14 × vars/10 → **1 year × 6 vars ≈ 15.6 calls; 10 years ≈ 157 calls**, against **600/min**, 5,000/hour, 10,000/day. Default model "best match" mixes IFS (2017+) with ERA5; an unpinned 10-year request once hung for > 2 min. | Pin `models=era5_seamless` (one consistent model family over the whole window; always use a timeout). Refresh must **throttle to ≤ 3 hub-decades/minute** using `X-RateLimit-Remaining`. A full 20-hub refresh ≈ 3,100 calls (~31% of the daily budget), so **historical data is cached permanently** (it doesn't change) and shipped in the image. A live non-hub profile costs ~157 calls, so cap it per day. |
| NOAA GHCN-Daily (NCEI) | Works, no token, < 1 s. DEN station 2025: 75.5 cm total; snow ≥ 0.1 cm on **26** days (7.1%), ≥ 2.5 cm on **12**. | Same year, ERA5: 73.3 cm; ≥ 0.1 cm on **40** days (11.0%), ≥ 2.5 cm on **9**. Totals agree; **reanalysis overcounts light-snow days by ~50%**. Model choice alone moves ≥ 2.5 cm days between 6 and 10. |
| FEMA NRI (ArcGIS `NRI_Counties_Prod`) | Works; **NRI_VER = "December 2025"**, **3,232 counties**, 467 fields. Point query → county + all hazards in ~1 s. Full table = 2 pages × 2,000 rows (~1 MB each, ~2 s). `*_AFREQ` is null **exactly when** the hazard rating is "Not Applicable". Flood field is `IFLD` (inland), not `RFLD`. | Load the full county table at refresh (cheap). Score null `AFREQ` as 0 frequency (the hazard isn't applicable there), not as missing data. **Caveat found:** `CWAV` (cold wave) is relative to local norms: Miami 1.28/yr ("Very High") vs Houston 0.48. |
| NWS Alerts | Works, ~0.4 s. Non-US point → **400 "out of bounds"**; no `User-Agent` → **403**. 674 active alerts nationwide today, mostly marine. | Always send a generic UA. The 400 can serve as a secondary non-US signal. |

---

## 8. Questions — round 1 answers and open items

| # | Question | Answer |
|---|---|---|
| Q1 | LLM provider | **Grok primary, OpenAI backup**, isolated behind `app/llm/`. Verified facts in §2.6. |
| Q2 | Why 20 hubs | Explained in §1.4. |
| Q3 | Heat | **Added** (§4.1), weight 0.10 (§4.3). |
| Q4 | Expand to all US | Start with 20; expansion = on-demand profiles + national NRI table, **not** a city table (§4.6). |
| Q5 | Deployment | **URL** (step 7). |
| Q6 | Aggregation | Weighted mean, reasons in §4.3; weights now **non-equal** because heat was added. |
| Q7 | Snow threshold | Both thresholds (§1.2). |

**Open (need from you before step 1):**
- **API keys:** an xAI key with prepaid credit (no free tier exists) and an OpenAI key on Build tier ($5).
  Put them in `.env` (git-ignored) as `XAI_API_KEY` / `OPENAI_API_KEY`. I will never print or log them.
- **Weights in §4.3:** OK with 0.25 / 0.25 / 0.20 / 0.20 / 0.10, or do you want equal weights?

---

## Decision log

| # | Date | Decision | Status |
|---|---|---|---|
| D1 | 2026-11-25 | Deterministic engine separate from the agent; LLM never produces displayed numbers (server hydrates tables) | Accepted |
| D2 | 2026-11-25 | Own tool-use loop; Grok primary + OpenAI backup behind one `LLMProvider` interface, through the `openai` SDK | Accepted (user) — wire API chosen in step 1 |
| D15 | 2026-11-25 | No LangChain / LangGraph (option A of 4); reasoning in §2.7 | Accepted (user) |
| D16 | 2026-11-25 | Pin Open-Meteo `models=era5_seamless`; cache historical data permanently; throttle refresh by `X-RateLimit-Remaining` | Accepted (technical, from step 1 evidence §7.1) |
| D17 | 2026-11-25 | Null NRI `AFREQ` = hazard not applicable → frequency 0 (not missing data) | Accepted (verified rule §7.1) |
| D18 | 2026-11-25 | Drop NRI cold wave (`CWAV`) from the winter score; it's relative to local norms (Miami 1.28/yr > Houston 0.48) | Accepted (user) |
| D19 | 2026-11-25 | Factual hub weather questions use the hub's airport station (GHCN-Daily) as primary, with the grid model alongside; scoring stays on the grid model for consistency | Accepted (user) |
| D20 | 2026-11-25 | Timeline flexible; no pre-emptive cuts; priority order unchanged | Accepted (user) |
| D21 | 2026-11-25 | LLM provider = Groq free plan (not xAI Grok — typo); xAI/OpenAI dropped. Wire API = Chat Completions (Groq's Responses API is beta and rejects `store`/`include`) | Accepted (user: Groq; wire API from step-1 evidence) |
| D3 | 2026-11-25 | SQLite for cache, NRI county table, snapshots, sessions; YAML for config | Accepted |
| D4 | 2026-11-25 | Fixed anchors for Open-Meteo KPIs; national county percentiles for NRI KPIs | Accepted (refined) |
| D5 | 2026-11-25 | NRI `*_AFREQ` rather than `RISK_SCORE`/EAL | Accepted — verify fields in step 1 |
| D6 | 2026-11-25 | NWS alerts = current layer only, never in the WDRI | Accepted |
| D7 | 2026-11-25 | IBTrACS demoted to stretch | Accepted |
| D8 | 2026-11-25 | Weighted mean; weights 0.25/0.25/0.20/0.20/0.10 + dominant-hazard display + weight-sensitivity flag | Proposed — confirm weights |
| D9 | 2026-11-25 | Heat added as 5th hazard | Accepted (user) |
| D10 | 2026-11-25 | 20 hubs; expansion via on-demand scoring, no all-US city table | Accepted (user) |
| D11 | 2026-11-25 | Deploy to a URL; demo token + per-session/daily caps because the URL is public and costs money | Accepted (user) |
| D12 | 2026-11-25 | Final answer via terminal strict `submit_answer` tool + `tool_choice=required` (portable across vendors) | Accepted — confirmed in step 1: the only option on Groq (json_schema + tools → 400) |
| D13 | 2026-11-25 | Fallback to the backup provider only on availability errors, never on validation errors | Proposed |
| D14 | 2026-11-25 | Report snow days at two thresholds: ≥ 0.1 in (NWS "measurable") and ≥ 1 in (NOAA normals threshold). Revised from 0.1 cm / 2.5 cm after verifying the definitions | Accepted (user) |
