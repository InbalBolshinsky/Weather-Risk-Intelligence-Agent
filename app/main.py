"""FastAPI app: the chat page, the agent API, status, and an LLM-free ranking (PLAN.md §2.4; D59-D63).

Run:  .venv/bin/uvicorn app.main:app --port 8000      (then open http://localhost:8000)
Interactive API docs: /docs. Only POST /api/chat requires the access code, and only when DEMO_TOKEN is set.
"""
from __future__ import annotations

import os
import re
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Callable

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from app.agent.loop import Agent
from app.agent.tools import HazardArg, RankArgs, RegionArg, ToolContext, rank_hubs
from app.config import ROOT
from app.data import fema_nri
from app.data.http import CachedHttp
from app.llm.base import LLMProvider
from app.llm.fallback import build_chain
from app.scoring.alerts import check_score_changes
from app.scoring.service import ScoringService
from app.store.db import bump_counter, connect, unbump_counter

load_dotenv(ROOT / ".env")

DAILY_CAP = int(os.getenv("DAILY_CAP", "80"))               # D61: below Groq's daily quotas
CONVERSATION_CAP = int(os.getenv("CONVERSATION_CAP", "30"))  # D60
WEB_PAGE = ROOT / "web" / "index.html"
_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
DAILY_LIMIT_MSG = ("Today's demo limit is reached (free-tier quotas). It resets at 00:00 UTC. "
                   "To run without limits, see the README.")

# ----------------------------------------------------------------------------- per-thread DB (D58)

_local = threading.local()


def db_http() -> CachedHttp:
    """One SQLite connection per worker thread: no connection is ever shared between threads."""
    http = getattr(_local, "http", None)
    if http is None:
        http = _local.http = CachedHttp(connect())
    return http


def utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def questions_left(http: CachedHttp, day: str) -> int:
    row = http.conn.execute("SELECT count FROM usage_counters WHERE day=? AND key='chat'", (day,)).fetchone()
    return max(0, DAILY_CAP - (row["count"] if row else 0))


# ----------------------------------------------------------------------------- app factory

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=1000)
    session_id: str | None = None


access_header = APIKeyHeader(name="X-Access-Code", auto_error=False, description="Demo access code (D59)")


def create_app(provider_factory: Callable[[], LLMProvider] = build_chain) -> FastAPI:
    scoring = ScoringService(db_http())

    def cache_ready(http: CachedHttp) -> bool:
        """Only score hubs from a pre-warmed cache; never trigger ~60 live downloads from inside a request."""
        return http.conn.execute("SELECT COUNT(*) FROM nri_counties").fetchone()[0] > 3000

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if cache_ready(db_http()):
            scoring.all_hub_scores()   # warm the in-memory hub scores so the first question isn't slow
        yield

    app = FastAPI(title="Weather Risk Intelligence Agent", version="1.0", lifespan=lifespan,
                  description="Ask about weather-disruption risk for 20 US logistics hubs. "
                              "Scores come from a deterministic engine; the LLM only explains them.")

    def require_access(code: str | None = Depends(access_header)) -> None:
        expected = os.getenv("DEMO_TOKEN")
        # bytes: compare_digest raises TypeError (a 500) on non-ASCII strings
        if expected and not (code and secrets.compare_digest(code.encode(), expected.encode())):
            raise HTTPException(401, detail={"error": "access_code", "message": "Missing or wrong access code."})

    @app.get("/", include_in_schema=False)
    def page() -> FileResponse:
        return FileResponse(WEB_PAGE)

    @app.get("/icon.png", include_in_schema=False)
    def icon() -> FileResponse:
        return FileResponse(WEB_PAGE.parent / "icon.png", media_type="image/png")

    @app.post("/api/chat", dependencies=[Depends(require_access)])
    def chat(req: ChatRequest) -> dict[str, Any]:
        """Ask the agent a question. Returns the answer text plus a `debug` field (tool calls and raw results)."""
        http = db_http()
        if req.session_id is not None and not _SESSION_RE.match(req.session_id):
            raise HTTPException(400, detail={"error": "session_id", "message": "Invalid session id."})
        if req.session_id:
            n = http.conn.execute("SELECT COUNT(*) FROM turns WHERE session_id=?", (req.session_id,)).fetchone()[0]
            if n >= CONVERSATION_CAP:
                raise HTTPException(429, detail={"error": "conversation_limit",
                                                 "message": "This conversation reached its limit. Start a new chat."})
        day = utc_today()
        if not bump_counter(http.conn, day, "chat", DAILY_CAP):
            raise HTTPException(429, detail={"error": "daily_limit", "questions_left_today": 0,
                                             "message": DAILY_LIMIT_MSG})
        ctx = ToolContext(http, scoring.with_http(http), datetime.now(timezone.utc).date())
        result = Agent(provider_factory(), ctx).chat(req.message, req.session_id)
        if result.provider_failed:   # no model answered: don't use up a question of the daily cap
            unbump_counter(http.conn, day, "chat")
        a = result.answer
        return {"session_id": result.session_id, "answer": a["answer_markdown"], "response_type": a["response_type"],
                "clarifying_question": a.get("clarifying_question"),
                "clarification_options": a.get("clarification_options", []),
                "suggested_followups": a.get("suggested_followups", []),
                "questions_left_today": questions_left(http, day), "debug": result.debug}

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        """Health check: data cache, versions, configured models, and questions left today."""
        http = db_http()
        counties = fema_nri.load_counties(http.conn)
        hubs_ready = len(scoring.with_http(http).all_hub_scores()) if cache_ready(http) else 0
        try:
            models = [p.name for p in build_chain().providers]
        except Exception:  # noqa: BLE001 - no key configured
            models = []
        return {"ok": bool(counties) and hubs_ready == 20 and bool(models),
                "hubs_scored": hubs_ready, "nri_counties": len(counties),
                "nri_version": counties[0].nri_ver if counties else None,
                "config_version": scoring.cfg.version, "models": models,
                "questions_left_today": questions_left(http, utc_today()), "daily_cap": DAILY_CAP,
                "conversation_cap": CONVERSATION_CAP, "access_code_required": bool(os.getenv("DEMO_TOKEN")),
                "daily_limit_message": DAILY_LIMIT_MSG}

    @app.post("/api/alerts/check", dependencies=[Depends(require_access)])
    def alerts_check() -> dict[str, Any]:
        """Score-change alert (D76): compare hub scores with the last alerted baseline and POST meaningful
        changes (>= 5 points, or a hazard crossing 80) to ALERT_WEBHOOK_URL. No LLM involved."""
        http = db_http()
        if not cache_ready(http):
            raise HTTPException(503, detail={"error": "data_not_loaded", "message": "Hub data isn't loaded yet."})
        return check_score_changes(scoring.with_http(http), os.getenv("ALERT_WEBHOOK_URL"))

    @app.get("/api/rankings")
    def rankings(hazard: HazardArg = "overall", region: RegionArg | None = Query(None)) -> dict[str, Any]:
        """The scoring engine's ranking, with no LLM involved: compare it with the agent's answers."""
        http = db_http()
        ctx = ToolContext(http, scoring.with_http(http), datetime.now(timezone.utc).date())
        return rank_hubs(ctx, RankArgs(hazard=hazard, region=region, top_n=20))

    return app


app = create_app()
