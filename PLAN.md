# PLAN — Weather Risk Intelligence Agent

Living plan; decisions are recorded in the **Decision log** (D1…). Working mode and rules: `CLAUDE.md`.

**Status (2026-11-25):** steps 0–4 done and committed. Open: LLM chain order (comparison of free models
running). Next: step 5 (API + chat page).

**Guiding principle:** the best agent for this assignment, judged by clarity, control, reliability, and
simplicity to run. Free-tier services only (D53).

---

## 0. Summary

A Python service with a **deterministic scoring engine** that turns public hazard and weather data into a
per-hub **Weather Disruption Risk Index (WDRI, 0–100)**, with per-hazard scores, confidence, and flags. An
**LLM agent** (Groq free tier, own loop) picks tools, explains the results in plain text, and asks clarifying
questions. It never computes a number: every number in an answer is checked against tool results.

---

## 1. Plan and scope

### 1.1 Steps

| # | Step | Status |
|---|---|---|
| 0 | Plan and decisions | ✅ |
| 1 | Verify every external API with real calls; save fixtures (`scripts/verify_apis.py`) | ✅ |
| 2 | Data clients, SQLite cache, refresh script (`app/data`, `app/store`, `config/hubs.yaml`) | ✅ |
| 3 | Scoring engine + config + tests (`app/scoring`, `config/scoring.yaml`), reviewed (D33–D37) | ✅ |
| 4 | Agent: resolver, tools, prompt, loop, validation, provider chain (`app/agent`, `app/geo`, `app/llm`) | ✅ (chain order open) |
| 5 | FastAPI + single-page chat UI | next |
| 6 | Eval set (~25–30 questions) + runner | |
| 7 | Deploy to a URL | |
| 8 | README, ARCHITECTURE.md, export AI session | |
| — | Stretch: score-change webhook; hurricane-intensity KPI | |

Time is flexible (D20). If something must slip: observed-gust KPI, non-hub scoring, weight-sensitivity
check. Evals, deploy, and docs are never cut.

### 1.2 Assumptions

- **20 synthetic hubs** in major US logistics metros; coordinates = metro center, not a real facility.
- **Exposure = long-run historical hazard frequency at the hub**, not financial loss. The WDRI uses
  2016–2025; current NWS alerts are a separate layer and never change it (D6).
- Regions = US Census Bureau regions. "Last year" = previous calendar year (computed in code).
- The grid weather data (Open-Meteo ERA5) is modeled, not measured; we say so and lower confidence where it matters.
- Snow days are reported at two thresholds: ≥ 0.1 in (NWS "measurable") and ≥ 1 in (NOAA normals) (D14).
- Factual hub weather questions use the hub's airport station (NOAA GHCN), with the grid value alongside;
  scores use the grid for every hub (D19).

### 1.3 Out of scope

Voice; user accounts (a demo token + caps only); costs, ROI, downtime losses (D55); forecasts beyond NWS
alerts; site-level analysis (flood zones, elevation); hub network effects; climate projections; wildfire,
drought, earthquake; hurricane intensity (D33); non-US places; non-English conversations; a precomputed
all-US city table (§4.6); LLM-as-judge evals.

### 1.4 Hubs (20)

The smallest set where every Census region can be ranked (≥ 5 in the Midwest), every hazard regime appears
at least twice, the assignment's cities and some ambiguous names (Portland, Columbus) are included, and every
score can be checked by hand. Defined in `config/hubs.yaml` with aliases and airport stations.

| Region | Hubs |
|---|---|
| Midwest (6) | Chicago, Indianapolis, Columbus OH, Detroit, Minneapolis, Kansas City MO |
| South (7) | Dallas, Houston, Miami, Atlanta, Memphis, Charlotte, Louisville |
| Northeast (2) | Newark NJ, Boston |
| West (5) | Denver, Phoenix, Los Angeles, Seattle, Portland OR |

---

## 2. Architecture

### 2.1 Components

```
 Browser (web/index.html)  ── POST /api/chat ──▶  FastAPI (app/main.py)          [step 5]
                                                     │
                                                     ▼
 Agent loop (app/agent/loop.py): model ⇄ tools → submit_answer → validate → (repair) → persist turn
     ├── LLM layer (app/llm/): one OpenAI-compatible adapter + fallback chain (config/llm.yaml)
     └── Tools (app/agent/tools.py)
           ├── Resolver (app/geo/): hub names/aliases → fuzzy → geocoder, US only
           ├── Scoring (app/scoring/): KPIs → 0–100 → hazard scores → WDRI, confidence, flags
           └── Data clients (app/data/): Open-Meteo, FEMA NRI, NOAA GHCN, NWS
                    ▼
 SQLite (data/app.db): http_cache, nri_counties, usage_counters, turns
```

The scoring engine doesn't know an LLM exists, so it's testable on its own, and the numbers are the same
with or without the agent (D1).

### 2.2 Key choices

| Choice | Decision | Why |
|---|---|---|
| Language | Python + FastAPI | Assignment prefers Python; Pydantic models |
| Agent loop | Own loop, no LangChain/LangGraph (D15) | The trust mechanisms (grounding, repair, fallback rules, resolution policy) are custom anyway; the whole control flow fits in one readable file; fewer dependencies |
| LLM | Groq free tier via the `openai` SDK, Chat Completions (D21) | Free; one adapter covers any OpenAI-compatible vendor |
| Conversation state | Server-side in SQLite, neutral message format (D27) | Follow-ups need prior results; a session can switch providers |
| Storage | SQLite + YAML config (D3) | Single writer, tiny data, zero ops, ships in the image. Not Postgres (no need), not JSON files (queries) |
| Freshness | `refresh_data.py` pre-warms all hubs; history cached permanently (D16) | The live demo doesn't depend on ~60 upstream calls |
| Hosting | Container on a free PaaS with the cache baked in (D11) | URL preferred; works even if upstream APIs are slow |

### 2.3 Repository

```
config/      hubs.yaml, scoring.yaml, llm.yaml
app/         config.py; data/ (clients, cache); store/ (SQLite); scoring/; geo/ (resolver);
             llm/ (adapter, fallback); agent/ (tools, prompts, schemas, loop)
scripts/     verify_apis.py, refresh_data.py, chat_cli.py, model_bakeoff.py
tests/       offline pytest (fixtures, fake geocoder, scripted fake model)
evals/       results/ (git-ignored); eval set in step 6
web/, app/main.py, Dockerfile, README.md, docs/ARCHITECTURE.md, session/   [steps 5–8]
```

### 2.4 API (step 5, decision pending)

`POST /api/chat` ({message, session_id?} → {session_id, answer, debug}) and `GET /api/health`; optionally one
LLM-free `GET /api/rankings` to show scores come from code. The UI shows only the answer text (D41).

---

## 3. Agent

### 3.1 LLM layer and limits

- **One adapter** (`app/llm/openai_compat.py`) maps vendor errors to: unavailable → next model; malformed tool
  call → retry once, then next; other errors → next (D13). Validation failures never trigger fallback.
- **All models rate-limited at once:** wait once for the shortest reported retry time (≤ 30 s), then retry the
  chain (D40). gpt-oss runs with `reasoning_effort: low`.
- **Final answer = a terminal `submit_answer` tool** with `tool_choice="required"` (D12). On Groq it's the only
  option: JSON-schema output can't be combined with tools (400).
- **Groq free limits (measured):** 1,000 requests/day and 8,000 tokens/min per model; qwen also 1,000 output
  tokens/min. Prompt caching works on gpt-oss but isn't guaranteed; not relied on (D23).
- **Token budget (D54):** system prompt 769 tokens, tool definitions 827 (measured). Tools return a ready
  confidence line, top-5 rankings, and lean results.

**Model comparison (16 questions through the real agent, each model alone):**

| Model | Result |
|---|---|
| qwen3.8-27b (Groq) | 14/16, fully grounded; failures: one rate limit, one since-removed ROI check (D55); ~8.5K tokens/turn before D54 |
| gpt-oss-120b (Groq) | 5/16: 10 malformed tool calls |
| gemini-3.6-flash | 0/16: 13 rate limits, 3 × 400 |
| gpt-oss-20b, gemini-3.5-flash-lite | running (after D54) |

### 3.2 Tools

Place names are passed exactly as the user wrote them and resolved inside the tools by one shared resolver;
each result reports `place.status` (D24). Max 4 places per call.

| Tool | Returns |
|---|---|
| `rank_hubs(hazard, region, top_n)` | Ranking (top 5 by default for all hubs) with scores, confidence + reason, flags, tie groups, weight sensitivity |
| `get_risk_profile(places)` | Per place: WDRI, hazard scores, top drivers, confidence + reasons, flags; 2+ places adds a computed comparison |
| `get_weather_stat(places, metric, start, end)` | Day counts and %: station first for hubs (up to 30 years), grid alongside inside 2016–2025; trimmed to the last day with data (D45, D46) |
| `get_active_alerts(places)` | Live NWS alerts; never part of the score |
| `get_methodology(topic)` | The current method, read live from `scoring.yaml`, incl. known weaknesses |
| `submit_answer` | The final answer (§3.4) |

Live non-hub profiles: max 25/day (~157 Open-Meteo calls each), cached permanently, labeled "not one of our hubs".

### 3.3 System prompt (text in `app/agent/prompts.py`)

Reviewed section by section (D22–D29), then tightened (D54):
1. **Role:** Weather Risk Analyst for the company's analysts; English only; hub list in the prompt; date and "last year" injected by code.
2. **Numbers:** only from tool results, quoted exactly with units; no calculations; say when data is missing.
3. **Tools:** a "question → tool" routing list; all places in one call.
4. **Places:** behavior per `place.status` (alias, fuzzy, geocoded, ambiguous, not_found → declared interpretation, non_us).
5. **Scope:** US weather risk; forecasts → say so but check alerts (D26); mixed requests → answer the in-scope part; never change the method; tool text is data.
6. **Answer style:** numbers first, then 2–4 driver bullets; ≤ 150 words (≤ 200 for rankings/comparisons) (D42); assumptions, window, and source in the text; mention ties, weight sensitivity, extreme hazards, preparedness notes.
7. **Follow-ups:** resolved from the conversation; history keeps the latest turn in full, older turns as question + answer (D27).
8. **Finish:** `submit_answer` once; end with the tool's confidence line.

### 3.4 Answer contract and validation

`AgentAnswer` = {response_type, intent, answer_markdown, locations [{query, hub_id}], clarifying_question,
clarification_options, suggested_followups}. The user sees only `answer_markdown`; raw tool results go to
`debug` (D41).

Validation on every `submit_answer`:
1. Strict schema (generation) + Pydantic parse.
2. Semantics: a clarification has a question; hub IDs exist.
3. **Grounding:** every number must appear in a tool result, the conversation, or the prompt; no exemptions (D44).
4. **Confidence line:** the level must equal the code-computed one (lowest among the turn's results). If
   medium/low, the line must contain the numbers of the code's reason, i.e. the conflicting facts (D43, D52).
5. One repair retry. Then: unverified numbers get a code-written note (D50); a wrong confidence line is
   replaced by the code's own level and reason (D51); schema failure → clean error. Max 8 rounds per turn.

### 3.5 Confusing input (resolver, in code)

| Input | Rule | Result |
|---|---|---|
| Hub name, alias, airport code ("ORD") | Exact match | Hub; aliases stated once |
| Typo ("Huston") | Fuzzy ≥ 85 → hub; 70–85 → ask; < 70 → geocoder; never for < 4 letters (D25, D49) | "Assuming you meant Houston" (+ "also Huston, ID") |
| Shared name, one is a hub ("Portland") | Hub wins; a namesake ≥ 50K people is mentioned (D49) | Portland, OR, also Portland, ME |
| Ambiguous US name ("Springfield") | 2nd match ≥ 20% of the 1st's population (D49) | Clarifying question |
| Non-hub US city ("Boise") | Geocoded | Live profile, "not one of our hubs" |
| Non-US ("Toronto") | Largest match non-US, unless a same-name US city ≥ 50K exists (Birmingham → AL) (D48) | Out of scope |
| Landmark ("Space Needle") | not_found → the model retries with `interpreted_from` | `llm_interpreted`, stated, confidence ≤ medium |
| No place, unrelated topic, method change | Prompt rules | Ask; out of scope; decline |

---

## 4. Scoring

### 4.1 Hazards and KPIs (`config/scoring.yaml`)

Each hazard combines **FEMA NRI** county event frequency (long, national record) with **observed** weather at
the hub (ERA5, 2016–2025); their agreement drives confidence (D33). FEMA's risk scores and dollar losses are not
used: they include social vulnerability and scale with county building value.

| Hazard (weight) | KPIs |
|---|---|
| Winter (0.25) | days ≥ 1 in snow; ice days (high ≤ 32°F); FEMA winter weather + ice storm. Cold wave excluded: relative to local norms (D18) |
| Hurricane (0.25) | FEMA hurricane frequency. Counts events, not intensity (D33) |
| Flood (0.20) | FEMA inland + coastal flooding (**summed**, D32); days ≥ 2 in rain |
| Severe storms (0.20) | FEMA tornado, hail, strong wind; days with gusts ≥ 47 mph (low weight: weak proxy) |
| Heat (0.10) | days ≥ 95°F; days feels-like ≥ 105°F; FEMA heat wave (D35a) |

### 4.2 Normalization

- Observed KPIs: fixed, documented scales set at roughly the worst plausible US metro. Absolute: adding a hub
  changes nobody's score.
- FEMA KPIs: linear from 0 to the **national 99th-percentile county** (D30, D35); 0 events → 0.
  Percentile ranks inflated tiny frequencies; a p95 top capped 25 hub values at 100, p99 caps 4.
- Null FEMA frequency = "not applicable" → 0 (D17).

### 4.3 Aggregation, weights, flags

- Hazard score = weighted mean of its KPIs (missing KPIs re-weighted, confidence lowered).
- **WDRI = weighted mean of hazard scores** with judgment weights by shutdown potential (D36, D37): additive and
  explainable. Rejected: worst hazard / blends (override the weights), power mean (loses the additive explanation),
  equal weights (heat = hurricane), FEMA loss weights (winter 1%, flood 62%).
- Explanation flags, which **never change a score**:
  - **extreme hazard** ≥ 80: targeted-upgrade candidate (D37);
  - **winter "rare here"**: < 3 snow days ≥ 1 in and < 5 ice days/yr, but events occur, so the region is less
    prepared (D34);
  - **weight-sensitive**: rank moves ≥ 3 under equal weights;
  - **tie groups**: within 5 points.

### 4.4 Confidence

Starts high per hazard. Lowered for: data coverage < 95% or a missing KPI (medium); location caveats, e.g.
Denver terrain (medium); FEMA vs observed ranked ≥ 10 places apart among hubs (low; rank-based, D31; severe
storms excluded). Every non-high level carries a reason with the conflicting facts (D52). Hub = weakest hazard
contributing ≥ 20% of the WDRI. Weather stats: station = high; grid snow counts = medium.

### 4.5 Results on real data (config `01bacc7b`)

- Overall: HOU 47, MIA 44, BOS 44, EWR 36, CHI 34, DAL 32 … PDX 11. Ties {HOU, MIA, BOS}, {EWR, CHI, DAL}.
- Midwest winter: MSP 55 > DET 44 > CHI 41. Hurricane: MIA 100 > BOS 60 = HOU 60. Heat: PHX 97 > DAL 53.
- Dallas: severe storms 65, heat 53, flood 48, plus a winter "rare here" note.
- Extreme hazards: MIA hurricane, PHX heat, HOU flood. Winter "rare here": ATL, CLT, DAL, HOU, MEM, SEA, PDX.
- Low confidence: 7 of 100 hazard scores (flood: PHX, EWR, BOS, ATL; winter: PDX, CLT, DEN), each with a reason,
  e.g. PHX: FEMA 8.5 flood events/yr in Maricopa (#3 of 20) vs 0.0 heavy-rain days/yr at the hub (#19).
- **Known weakness (D33):** hurricane counts events, not strength, so Boston equals Houston on hurricane. A fix
  needs storm-track data (~1–2 h); left out for time; reported by `get_methodology`.

### 4.6 Beyond 20 hubs

No all-US city table: Open-Meteo's free tier (10K calls/day, non-commercial) can't cover ~19K places. Any US
place is profiled on demand (FEMA from the local county table + one cached weather fetch), labeled "not a hub",
and kept out of hub rankings (D10).

---

## 5. Data sources (all verified with real calls, 2026-11-25)

| Source | Use | Key facts |
|---|---|---|
| FEMA NRI (ArcGIS) | Hazard frequencies | Dec 2025 version, 3,232 counties loaded at refresh; flood field `IFLD` (no `RFLD`) |
| Open-Meteo archive | Observed KPIs, grid stats | Pinned `era5_seamless` (D16); a 10-year × 6-variable request costs ~157 calls (header-measured) of 10K/day, 600/min; lags ~7 days; non-commercial free tier |
| Open-Meteo geocoding | Non-hub places | No fuzzy matching ("Huston" → Idaho, "ORD" → Turkey), so our resolver runs first |
| NOAA GHCN-Daily | Station stats for hubs | No token; lags ~10 days; 17 of 20 airport stations report snow (MIA, PHX, LAX → grid fallback). Denver 2025: 26 snow days (7.1%) vs grid 37–40 |
| NWS alerts | Current alerts | Needs a User-Agent; non-US point → 400 |

---

## Decision log

| # | Decision | Status |
|---|---|---|
| D1 | Deterministic engine separate from the agent; the LLM never produces numbers | Accepted |
| D2 | Own loop behind one provider interface (vendor later fixed by D21) | Superseded by D21 |
| D3 | SQLite for cache, NRI table, turns; YAML config | Accepted |
| D4 | Fixed anchors for observed KPIs; national percentiles for NRI | Refined by D30, D35 |
| D5 | NRI annualized frequency, not risk score / EAL | Accepted (confirmed by D33) |
| D6 | NWS alerts are a current layer only, never in the WDRI | Accepted |
| D7 | Storm-track data (IBTrACS/HURDAT2) demoted to stretch | Accepted |
| D8 | Weighted mean + dominant hazard + weight-sensitivity flag | Superseded by D36, D37 |
| D9 | Heat added as the 5th hazard | Accepted (user) |
| D10 | 20 hubs; expansion via on-demand scoring, no all-US city table | Accepted (user) |
| D11 | Deploy to a URL with a demo token and caps | Accepted (user) |
| D12 | Final answer via a strict terminal `submit_answer` tool + `tool_choice=required` | Accepted (confirmed on Groq) |
| D13 | Fall back to the next model only on availability errors, never on validation errors | Accepted |
| D14 | Snow days at ≥ 0.1 in (NWS) and ≥ 1 in (NOAA normals) | Accepted (user) |
| D15 | No LangChain/LangGraph; own loop (revisit for multi-step workflows) | Accepted (user) |
| D16 | Pin `era5_seamless`; cache history permanently; throttle refresh by rate-limit headers | Accepted |
| D17 | Null NRI frequency = not applicable → 0 | Accepted |
| D18 | Cold wave excluded from winter (relative to local norms) | Accepted (user) |
| D19 | Hub weather questions: airport station first, grid alongside; scoring on grid | Accepted (user) |
| D20 | Flexible timeline; no pre-emptive cuts | Accepted (user) |
| D21 | Groq free tier (Grok was a typo; xAI/OpenAI dropped); Chat Completions | Accepted (user) |
| D22 | Prompt §1: analyst persona, numbers first, English only, hub list in the prompt | Accepted (user) |
| D23 | Prompt §2: exact display-ready numbers; tools compute comparisons; caching not relied on | Accepted (user) |
| D24 | Prompt §3: no `resolve_location`; tools resolve names; max 4 places; `get_methodology(topic)`; non-hub cap | Accepted (user) |
| D25 | Prompt §4: status-driven place handling; fuzzy 85/70; non-US out of scope | Accepted (user); refined by D48 |
| D26 | Forecasts: say so, but check alerts; mixed requests: answer the in-scope part | Accepted (user) |
| D27 | History: latest turn in full, older turns = question + answer | Accepted (user) |
| D28 | Confidence level set by code | Accepted (user) |
| D29 | Prompt §6/§8 details (answer shape, field guidance, malformed-call retry) | Accepted; partly superseded by D41–D43 |
| D30 | NRI scaled linearly to a national percentile county (not ranks) | Accepted (user); p99 per D35 |
| D31 | Confidence cross-check is rank-based (≥ 10 places); severe storms excluded | Accepted (user) |
| D32 | Multi-part NRI hazards summed; 0 events → 0 | Accepted (user) |
| D33 | Scoring review 1: KPIs = NRI frequency + observed, cross-checked; hurricane intensity left out | Accepted (user) |
| D34 | Scoring review 2: fixed national thresholds + winter-only "rare here" note | Accepted (user) |
| D35 | Scoring review 3: NRI scale top = national p99 (was p95) | Accepted (user) |
| D35a | Heat stays in the WDRI | Accepted (user) |
| D36 | Scoring review 4: judgment weights 0.25/0.25/0.20/0.20/0.10 + weight-sensitivity flag | Accepted (user) |
| D37 | Scoring review 5: weighted mean + extreme-hazard callout (≥ 80) | Accepted (user) |
| D38 | Model order chosen by a comparison through the real agent | Accepted (user) |
| D39 | Run the comparison once, after step-4 decisions settle | Accepted (user) |
| D40 | All models rate-limited: wait once (≤ 30 s) and retry; gpt-oss low reasoning; waiting message in UI | Accepted (user) |
| D41 | UI shows only the answer text; raw tool results in `debug`; no `table_refs`/`key_drivers` | Accepted (user) |
| D42 | Answer length ≤ 150 words, ≤ 200 for rankings/comparisons | Accepted (user) |
| D43 | Confidence and assumptions written in the text; the stated level is validated; `assumptions`/`confidence_reason` fields dropped | Accepted (user) |
| D44 | Grounding has no small-number exemption | Accepted (user) |
| D45 | Weather-stat window trimmed to the last day with data (measured lags: grid 7 d, station 10 d) | Accepted (user) |
| D46 | Hub stats up to 30 years (station); grid only inside 2016–2025; non-hubs ≤ 10 years | Accepted (user) |
| D47 | Alias fix: "logan" → "logan airport"/"boston logan" (Logan, UT exists) | Accepted (user) |
| D48 | A same-name US city ≥ 50K beats a larger foreign one (Birmingham → AL) | Accepted (user) |
| D49 | Limits kept with evidence: 25 non-hub profiles/day (measured), 20% ambiguity (tested), 50K namesake (Census anchor), no fuzzy < 4 letters (tested) | Accepted (user) |
| D50 | Unverified numbers after repair → code appends a note | Accepted (user) |
| D51 | Wrong confidence line after repair → code writes the whole line (its level + reason) | Accepted (user) |
| D52 | Low/medium confidence must state the conflicting facts; validator checks their numbers | Accepted (user) |
| D53 | Free-tier models only (Cerebras out); comparison results in §3.1 | Accepted (user) |
| D54 | Token savings: ready confidence line, top-5 rankings, lean results, smaller contract, tighter prompt | Accepted (user) |
| D55 | Costs/ROI stay out of scope; no extra ROI rule | Accepted (user) |
