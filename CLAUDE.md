# CLAUDE.md

## Project

Weather Risk Intelligence Agent: answers analysts' questions about weather-disruption risk for 20 (synthetic) US
logistics hubs. Code computes; the LLM only chooses tools and explains. Design: DESIGN.md; decision log (D1…):
PLAN.md; run, test and deploy commands: README.md.

## How we work

- Act as a senior engineer. Discuss every design or scope decision BEFORE coding: options, trade-offs, a
  recommendation, then wait for my decision. One decision at a time.
- Only trivial implementation details (naming, file layout inside an agreed design) may be decided without asking;
  log each in one line in the decision log.
- Judge choices by what makes the best agent (clarity, control, reliability, simple to run), not interview signal.
- Discussions happen in chat; PLAN.md records decisions, not open deliberation.
- After each file or step, report in 3 lines: what was built and why.
- One commit per completed step, on main, no branches.

## Stack

Python 3.14, FastAPI, SQLite, YAML config; own agent loop on Chat Completions via the `openai` SDK (no LangChain).
LLM: Groq free tier, chain qwen3.8-27b → gpt-oss-20b (`config/llm.yaml`). Data: FEMA NRI, Open-Meteo, NOAA
GHCN-Daily, NWS. Deployed on Render's free tier.

## Rules

- The LLM never computes a score or any derived number (D1, D23).
- Every number in an answer must come from a tool result, the conversation or the prompt (D44); one repair retry,
  then code appends an "unverified" note (D50).
- Confidence is set by code; the answer's closing "Confidence:" line is validated (D28, D43, D52).
- Place names are passed exactly as written; resolution happens in code (D24, D25, D48).
- Scope: US, English. Scoring weights live in `config/scoring.yaml` and are never changed from chat.
- Flags (extreme ≥ 80, winter "rare here") never change a score (D34, D37).
- The UI shows only the answer text; raw tool results go in `debug` (D41).
- Free tier only: Groq 8K tokens/min and 200K/day per model; Open-Meteo 10K calls/day.
- Secrets live only in the git-ignored `.env` or Render. Never commit, print or paste them; never send personal
  data to external services (generic User-Agent).
