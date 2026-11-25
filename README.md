# Weather Risk Intelligence Agent

A chat agent for weather-disruption risk at 20 US logistics hubs. Design: [DESIGN.md](DESIGN.md) ·
decision log: [PLAN.md](PLAN.md) · eval report: [evals/results/eval_combined.md](evals/results/eval_combined.md).

## Live demo

**https://weather-risk-agent-pzle.onrender.com**

- Open `https://weather-risk-agent-pzle.onrender.com/#code=<access code>` once (the code is sent with the
  submission); the page remembers it.
- The first load can take about a minute: the free server sleeps after 15 idle minutes, and the page retries.
- Limits: 30 questions per conversation, 80 per day.

## Run locally

Needs Python 3.14 and a free Groq API key (https://console.groq.com/keys).

```
git clone https://github.com/InbalBolshinsky/Weather-Risk-Intelligence-Agent.git
cd Weather-Risk-Intelligence-Agent
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env                          # then set GROQ_API_KEY in .env
.venv/bin/uvicorn app.main:app --port 8000    # open http://localhost:8000
```

On first start the committed snapshot is unpacked into `data/app.db`; no downloads are needed.

| Variable | Required | Purpose |
|---|---|---|
| `GROQ_API_KEY` | yes | The LLM (both models in the chain) |
| `DEMO_TOKEN` | no | Access code; if unset, no code is asked |
| `ALERT_WEBHOOK_URL` | no | Where score-change alerts are POSTed |
| `DAILY_CAP`, `CONVERSATION_CAP` | no | Question limits (default 80 per day, 30 per conversation) |
| `WRA_DB_PATH` | no | SQLite path (default `data/app.db`) |
| `GOOGLE_API_KEY`, `GROQ_MODEL`, `GEMINI_MODEL` | no | Used only by `scripts/verify_apis.py` |

| Command (`.venv/bin/python …`) | What it does |
|---|---|
| `scripts/chat_cli.py "Why is the Dallas hub's risk high?"` | One question in the terminal (no question: interactive; `--debug`: tool calls) |
| `-m pytest -q` | 92 offline tests (no network, no LLM) |
| `evals/run_evals.py [--only A1_midwest_winter]` | End-to-end evals against the real model (uses LLM quota) |
| `scripts/refresh_data.py` | Re-download hub data (~6 min), then run the score-change check |
| `scripts/check_scores.py [--no-save]` | Score-change check against `data/score_baseline.json` |
| `scripts/verify_apis.py` | One real call to every external API |

API (docs at `/docs`): `POST /api/chat`, `GET /api/status`, `GET /api/rankings` (no LLM), `POST /api/alerts/check`.
The chat and alerts endpoints need the access code when `DEMO_TOKEN` is set.

## Try the score-change alert

1. Set `ALERT_WEBHOOK_URL` in `.env` (a free test URL from https://webhook.site works).
2. In `config/scoring.yaml`, set `hazard_weights` winter 0.10 and hurricane 0.40.
3. Run `.venv/bin/python scripts/check_scores.py --no-save`: six hubs change, Miami overtakes Houston as #1.
4. Revert: `git checkout config/scoring.yaml`.

## Deploy your own

Create a Render Blueprint from this repo (`render.yaml`, free plan), then set `GROQ_API_KEY` and `DEMO_TOKEN`
(any random string, e.g. `python3 -c "import secrets; print(secrets.token_urlsafe(12))"`) in Render's dashboard;
`ALERT_WEBHOOK_URL` is optional. Health check: `/api/status`.
