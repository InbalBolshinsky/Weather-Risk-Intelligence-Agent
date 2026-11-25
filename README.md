# Weather Risk Intelligence Agent

A chat agent that helps logistics analysts decide which of 20 (synthetic) US distribution hubs are most exposed
to weather disruption. A deterministic engine computes every score from public data (FEMA National Risk Index,
Open-Meteo ERA5, NOAA stations, NWS alerts); the LLM only picks tools and explains the results.

- **Design and architecture, repository structure, data storage, known limitations:** [DESIGN.md](DESIGN.md)
- **Decision log (D1…):** [PLAN.md](PLAN.md)
- **Eval results (34/38):** [evals/results/eval_combined.md](evals/results/eval_combined.md)

## Live demo

**https://weather-risk-agent-pzle.onrender.com**

- Asking questions needs the **access code sent with the submission**. Open
  `https://weather-risk-agent-pzle.onrender.com/?code=<access code>` once; the page remembers it.
- **The first load can take about a minute:** the free server sleeps after 15 minutes without visitors. The page
  shows "Waking up the server…" and retries on its own.
- Limits: 30 questions per conversation, 80 per day (free-tier LLM quota).

Try:

- Which hubs in the Midwest are most exposed to winter disruption?
- Compare Miami and Houston in terms of hurricane and flood exposure.
- What percentage of days in Denver last year had snowfall?
- Why is the Dallas hub's weather disruption risk high?

## Run locally

Needs Python 3.14 and a free [Groq API key](https://console.groq.com/keys).

```
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env                        # set GROQ_API_KEY (the only required value)
.venv/bin/uvicorn app.main:app --port 8000  # then open http://localhost:8000
```

On first start the committed data snapshot (`data/snapshot.db.gz`) is unpacked into `data/app.db`, so the app
answers right away without downloading anything. No access code is needed unless you set `DEMO_TOKEN` in `.env`.

Other ways to run it:

| Command | What it does |
|---|---|
| `.venv/bin/python scripts/chat_cli.py "Why is the Dallas hub's risk high?"` | One question in the terminal (omit the question for an interactive session; `--debug` prints tool calls) |
| `.venv/bin/python -m pytest -q` | 76 offline tests (scripted fake model, no network or quota) |
| `.venv/bin/python evals/run_evals.py` | 38-case end-to-end eval against the real model (uses LLM quota; `--only A1_midwest_winter` runs one case) |
| `.venv/bin/python scripts/refresh_data.py` | Re-download all data for the hubs (~6 min, throttled for Open-Meteo's free tier) |
| `.venv/bin/python scripts/verify_apis.py` | One real call to every external API |

API (interactive docs at `/docs`): `POST /api/chat`, `GET /api/status`, `GET /api/rankings` (scores straight
from the engine, no LLM).

## Deploy your own

`render.yaml` is a Render Blueprint (free web service): create a Blueprint from this repo, then set
`GROQ_API_KEY` and `DEMO_TOKEN` (any random string, e.g. `python3 -c "import secrets; print(secrets.token_urlsafe(12))"`)
in the dashboard. Health check: `/api/status`.
