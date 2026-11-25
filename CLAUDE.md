# CLAUDE.md

## Project

Weather Risk Intelligence Agent: answers analysts' questions about weather-disruption risk for 20 (synthetic)
US logistics hubs, to help prioritize resilience investment. Deterministic scoring in code; the LLM only
chooses tools and explains results. PLAN.md is the living plan; its decision log (D1, D2, …) is the record
of every decision.

## How we work

- Act as a senior engineer. Discuss every design or scope decision with me BEFORE coding: options, trade-offs,
  a recommendation, then wait for my decision. One decision at a time.
- Only trivial implementation details (naming, file layout inside an agreed design) may be decided without
  asking; log each in one line in the decision log.
- Judge choices by what makes the best agent (clarity, control, reliability, simple to run), not interview signal.
- Discussions happen in chat; PLAN.md records decisions, not open deliberation.
- After each file or step, report in 3 lines: what was built and why.
- One commit per completed step, on main, no branches.

## Stack

- Python 3.14, SQLite (cache, NRI county table, conversation turns), YAML config. FastAPI chat API + single-page
  UI are step 5 (not built yet).
- Own agent loop on the OpenAI-compatible Chat Completions API via the `openai` SDK; no LangChain (D15, D21).
- LLM: Groq free tier. Candidates: `openai/gpt-oss-120b` and `qwen/qwen3.8-27b` (Groq), `gemini-3.6-flash`
  (separate quota). Fallback-chain order is decided by the model comparison (D38); `config/llm.yaml`.
- Data: FEMA National Risk Index (county), Open-Meteo ERA5 archive + geocoding, NOAA GHCN-Daily stations, NWS alerts.

## Rules

- The LLM never computes a score or any derived number. Code computes; the LLM explains (D1, D23).
- Every number in an answer must come from a tool result, the conversation, or the prompt; no exemptions (D44).
  On failure: one repair retry, then the code appends an "unverified" note (D50).
- Confidence level is set by code (lowest among the turn's tool results). The answer states it in a closing
  "Confidence:" line and the validator checks it (D28, D43).
- Place names are passed exactly as the user wrote them; resolution happens in code (D24, D25, D48).
- Scope: US locations, English only. Scoring weights/method live in `config/scoring.yaml`, never changed from chat.
- Explanation flags (extreme hazard >= 80, winter "rare here") never change a score (D34, D37).
- UI shows only the human-language answer; raw tool results go in a debug field (D41).
- Free-tier limits are a design constraint: Groq 8K tokens/min per model, Open-Meteo 10K calls/day.
- Secrets live only in the gitignored `.env`. Never commit, print, or paste key values; never send personal
  data to external services (generic User-Agent).

## Run

```
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env                                   # add GROQ_API_KEY (and GOOGLE_API_KEY for Gemini)
.venv/bin/python scripts/refresh_data.py               # pre-warm cache for all hubs (~6 min, throttled)
.venv/bin/python scripts/chat_cli.py "Why is the Dallas hub's risk high?"
.venv/bin/python -m pytest -q                          # offline; loop tests use a scripted fake model
.venv/bin/python scripts/verify_apis.py                # real calls to every external API
.venv/bin/python scripts/model_bakeoff.py              # 16-question model comparison (uses LLM quota)
```
