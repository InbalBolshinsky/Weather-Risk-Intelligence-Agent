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
  `https://weather-risk-agent-pzle.onrender.com/#code=<access code>` once; the page remembers it (the part after `#` never reaches the server, so the code stays out of logs).
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
| `.venv/bin/python -m pytest -q` | 85 offline tests (scripted fake model, no network or quota) |
| `.venv/bin/python evals/run_evals.py` | 38-case end-to-end eval against the real model (uses LLM quota; `--only A1_midwest_winter` runs one case) |
| `.venv/bin/python scripts/refresh_data.py` | Re-download all data for the hubs (~6 min, throttled for Open-Meteo's free tier) |
| `.venv/bin/python scripts/verify_apis.py` | One real call to every external API |
| `.venv/bin/python scripts/check_scores.py` | Score-change alert: compare hub scores with `data/score_baseline.json` and POST meaningful changes to `ALERT_WEBHOOK_URL` (`--no-save` keeps the baseline) |

API (interactive docs at `/docs`): `POST /api/chat`, `GET /api/status`, `GET /api/rankings` (scores straight
from the engine, no LLM), `POST /api/alerts/check` (score-change alert, access code required).

### Score-change alerts (bonus)

Scores change only when the data is refreshed or the scoring config changes. After either, a check compares
every hub with the last alerted scores and sends the **meaningful** changes (a score moving 5+ points, or a
hazard crossing the extreme mark of 80) to a webhook. The payload works with Slack (`text`), Discord
(`content`) or any JSON receiver (`changes`). To try it:

1. Put a webhook URL in `.env` as `ALERT_WEBHOOK_URL` (a free test URL from https://webhook.site works).
2. In `config/scoring.yaml`, change `hazard_weights` (e.g. winter 0.10, hurricane 0.40).
3. Run `.venv/bin/python scripts/check_scores.py --no-save`: six hubs change, e.g. Miami overtakes Houston as #1.
4. Revert the weights: `git checkout config/scoring.yaml`.

## Deploy your own

`render.yaml` is a Render Blueprint (free web service): create a Blueprint from this repo, then set
`GROQ_API_KEY` and `DEMO_TOKEN` (any random string, e.g. `python3 -c "import secrets; print(secrets.token_urlsafe(12))"`)
in the dashboard (optionally `ALERT_WEBHOOK_URL`). Health check: `/api/status`.
