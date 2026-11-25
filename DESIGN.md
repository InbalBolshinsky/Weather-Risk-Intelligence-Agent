# Design — Weather Risk Intelligence Agent

An agent that helps logistics analysts decide which of 20 (synthetic) US distribution hubs are most exposed
to weather disruption. **A deterministic engine computes every score; the LLM only chooses tools and explains
the results, and every number it states is checked against the tool results.** Decisions and their reasons
are logged in [PLAN.md](PLAN.md) (D1…).

## 1. System architecture

```
 Browser: web/index.html ──POST /api/chat──▶ FastAPI (app/main.py)
                                               │  access code, caps (30/conversation, ~80/day)
                                               ▼
 Agent loop (app/agent/loop.py)
   1. send system prompt + history + question to the LLM (tool_choice = "required")
   2. run the tools the model calls ──▶ Tools (app/agent/tools.py)
   3. the model ends with submit_answer          ├─ Resolver (app/geo): hub names/aliases → fuzzy → geocoder, US only
   4. validate: schema, grounding,                ├─ Scoring engine (app/scoring): KPIs → 0–100 → hazards → WDRI
      confidence line → one repair if needed      └─ Data clients (app/data): FEMA NRI, Open-Meteo, NOAA, NWS
   5. save the turn, return the answer text                       │
        │                                                         ▼
        └─ LLM layer (app/llm): one OpenAI-compatible adapter   SQLite (data/app.db)
           + fallback chain: Groq qwen3.8-27b → gpt-oss-20b
```

- **Separation of concerns.** The scoring engine knows nothing about the LLM: it is a set of pure functions,
  tested on its own, and also reachable without the LLM via `GET /api/rankings`.
- **Tools** take place names exactly as the user typed them; one shared resolver turns them into hubs or US
  locations in code (typos, airport codes, ambiguous and non-US names), so the model can't skip or guess it.
- **Own agent loop, no framework.** The trust mechanisms (grounding, repair, fallback rules) are custom either
  way, and the whole control flow fits in one readable file.
- **LLM layer.** One adapter for any OpenAI-compatible API. Fallback to the next model only when one is
  unavailable (rate limit, timeout) or makes malformed tool calls twice, never on a bad answer. On the Groq free
  tier (8K tokens/min per model) a turn uses ~5K tokens: prompt 769 + tool definitions 827, plus results.
- **API.** `GET /` (chat page), `POST /api/chat` (needs the access code when deployed), `GET /api/status`,
  `GET /api/rankings` (engine only, no LLM), and FastAPI's interactive `/docs`. The page shows only the answer
  text; raw tool results go in a `debug` field.
- **Deployment.** One Render free web service (`render.yaml`), native Python, one uvicorn worker (the SQLite
  cache and in-memory scores are per process). The host's disk starts empty, so the committed data snapshot
  is unpacked on first start: no upstream downloads at boot. Secrets (`GROQ_API_KEY`, `DEMO_TOKEN`) are set
  only in Render's dashboard.

## 2. Repository structure

```
config/        hubs.yaml (20 hubs, aliases, airport stations) · scoring.yaml (all scoring numbers) · llm.yaml (model chain)
app/
  data/        API clients with a cached, retrying HTTP layer (Open-Meteo, FEMA NRI, NOAA GHCN, NWS)
  store/       SQLite schema and helpers
  scoring/     kpis.py · normalize.py · engine.py (scores, confidence, flags, ranking) · service.py
  geo/         resolver.py (deterministic place resolution)
  llm/         base.py (neutral types) · openai_compat.py (adapter) · fallback.py (chain)
  agent/       tools.py · prompts.py · schemas.py (answer contract) · loop.py
  main.py      FastAPI app
web/           index.html (single-page chat)
data/          snapshot.db.gz (committed, pre-warmed cache) · app.db (local, git-ignored)
scripts/       verify_apis.py · refresh_data.py · chat_cli.py · model_bakeoff.py · make_snapshot.py
tests/         76 offline tests (fixtures, fake geocoder, scripted fake model)
evals/         38-case end-to-end eval set + runner + report merge; reports in evals/results/
render.yaml    deployment (Render Blueprint)
```

## 3. Data storage

**SQLite (one file) + YAML config in git.**

| Table | Contents |
|---|---|
| `http_cache` | Raw upstream responses; history is cached permanently, recent data for a day |
| `nri_counties` | FEMA National Risk Index for all 3,232 US counties (national scales, no runtime dependency on FEMA's service) |
| `usage_counters` | Daily counters for the caps |
| `turns` | Conversation history (full and compact forms) |

**Why SQLite:** one writer, tiny data, zero operations, and the pre-warmed cache ships in the repo as a 2.3 MB
gzipped snapshot, so the demo doesn't depend on ~60 upstream calls. **Not Postgres:** no concurrency or scale
need. **Not JSON files:** we query sessions and counters. Each request thread uses its own connection.
Trade-off: on a free host the disk is ephemeral, so conversations and the daily counter reset on every restart
(scores don't: the snapshot is unpacked again).
All scoring numbers live in `scoring.yaml`; every output carries a hash of it (`config_version`).

## 4. Scoring

**Weather Disruption Risk Index (WDRI, 0–100)** = weighted mean of five hazard scores, weights by shutdown
potential: winter 0.25, hurricane 0.25, flood 0.20, severe storms 0.20, heat 0.10.

- **Each hazard combines two independent sources:** FEMA NRI county event frequency (long national record)
  and observed weather at the hub (Open-Meteo ERA5, 2016–2025: e.g. days ≥ 1 in snow, days ≥ 95°F).
- **Scales:** observed measures on fixed, documented scales; FEMA frequencies linear up to the national
  99th-percentile county. Both are absolute, so adding a hub changes no other score.
- **Weighted mean, not "worst hazard":** additive, so every score breaks down exactly. A single extreme
  hazard isn't hidden: any hazard ≥ 80 is flagged as a targeted-upgrade candidate. Other flags: winter
  "rare here" (less-prepared regions), weight-sensitive rankings, ties within 5 points. Flags never change a score.
- **Confidence** (high/medium/low) is computed in code: lowered for missing data, known location caveats, or
  when FEMA and observed weather rank a hub ≥ 10 places apart. A low rating always states the conflicting facts
  (e.g. Phoenix flood: FEMA 8.5 events/yr in the county vs 0.0 heavy-rain days/yr at the hub).

## 5. Grounding and validation

The model answers by calling a strict `submit_answer` tool. Each answer is checked in code:
1. **Schema** (constrained by the provider, then parsed with Pydantic) and semantic rules.
2. **Grounding:** every number in the text must appear in a tool result, the conversation, or the prompt.
   Tools return display-ready numbers and computed comparisons, so the model never calculates.
3. **Confidence line:** the stated level must equal the code-computed one and, if low/medium, include the
   conflicting facts.
4. **One repair retry.** If numbers are still unverified, code appends a warning naming them; if the confidence
   line is still wrong, code writes it. Previously unverified numbers never count as sources later.

**Evaluation:** 38 end-to-end cases (the brief's questions, follow-ups, confusing input, scope/safety, data
coverage), graded in code with facts computed live from the engine; every answer is printed for human review.
Result: **34/38** (all 4 of the brief's questions pass). Failures: one wrong limit stated despite grounded
numbers, one geography slip ("Gulf Coast" hubs), two rule-following slips by the fallback model
([evals/results/eval_combined.md](evals/results/eval_combined.md)).

## 6. Known limitations

- **Free-tier LLMs:** the main model (qwen3.8-27b, 14/16 in our comparison) is a Groq preview model; the backup
  (gpt-oss-20b) scored 9/16. Back-to-back questions may wait up to ~30 s for capacity.
- **Grounding is lexical:** it checks that a number exists in the data, not that it's attached to the right hub;
  claims without numbers aren't checked. The eval set covers this; the code doesn't. Example from the evals: an
  answer said "exceeds the 10-year limit" (the limit is 30 years); "10" passed because it appears in a tool description.
- **Eval result 34/38:** besides the case above, one geography slip (inland hubs called "Gulf Coast") and two
  rule-following slips by the fallback model (not naming a non-hub place; not flagging an extreme hazard).
- **At least two model calls per data answer** (tool call, then `submit_answer`): Groq can't combine JSON-schema
  output with tool use.
- **Unvalidated scores:** no real closure data, so thresholds and weights are documented judgment calls. FEMA
  data is county-level; hurricane scores count events, not intensity (Boston's county equals Houston's); hubs
  sit at metro centers.
- **Prompt-only rules:** staying in scope and ignoring instructions inside tool data rely on the model (tested by evals).
- **Free hosting sleeps:** after 15 idle minutes the first request takes about a minute (the page shows a
  wake-up message and retries); each restart clears conversations and the daily counter.
- **Operations:** manual cache refresh; Open-Meteo's free tier is non-commercial and capped at 10K calls/day;
  a change in FEMA's service fields makes the refresh fail loudly.
