"""Talk to the agent from the terminal (no web server needed).

    python scripts/chat_cli.py                       # interactive, default fallback chain
    python scripts/chat_cli.py --only groq-qwen3.8-27b "Compare Miami and Houston"
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

from app.agent.loop import Agent  # noqa: E402
from app.agent.tools import ToolContext  # noqa: E402
from app.config import ROOT  # noqa: E402
from app.data.http import CachedHttp  # noqa: E402
from app.llm.fallback import build_chain  # noqa: E402
from app.scoring.service import ScoringService  # noqa: E402
from app.store.db import connect  # noqa: E402


def main() -> int:
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("message", nargs="*")
    ap.add_argument("--only", help="use only this chain entry from config/llm.yaml")
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()
    http = CachedHttp(connect())
    agent = Agent(build_chain(only=a.only), ToolContext(http, ScoringService(http), date.today()))
    session = None
    queue = [" ".join(a.message)] if a.message else []
    while True:
        msg = queue.pop(0) if queue else (input("\nyou> ").strip() if not a.message else "")
        if not msg:
            return 0
        r = agent.chat(msg, session)
        session = r.session_id
        ans = r.answer
        print(f"\n[{ans['response_type']} | confidence: {r.confidence} | {'UNVERIFIED ' + str(r.ungrounded_numbers) if r.unverified else 'grounded'}]")
        print(ans["answer_markdown"])
        if ans.get("clarifying_question"):
            print(f"? {ans['clarifying_question']} {ans.get('clarification_options') or ''}")
        d = r.debug
        print(f"(tools: {[c['name'] for c in d['tool_calls']]} | rounds {d['rounds']} | tokens {d['tokens']} | "
              f"{d['latency_ms']} ms | {d['providers'][-1] if d['providers'] else '-'})")
        if a.debug:
            print(json.dumps(d, indent=1)[:3000])
        if a.message and not queue:
            return 0


if __name__ == "__main__":
    sys.exit(main())
