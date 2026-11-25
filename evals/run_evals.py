"""Run the eval set (D64-D66) through the shipping agent and model chain.

    python evals/run_evals.py                  # all 38 cases, then re-run failures once
    python evals/run_evals.py --only A1_midwest_winter,C3_springfield --no-rerun

Writes evals/results/eval_<ts>.json and eval_<ts>.md (the Markdown report includes every answer in full
for human review, D65). Uses real LLM quota: ~5K tokens per turn.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

from app.agent.loop import Agent  # noqa: E402
from app.agent.tools import ToolContext  # noqa: E402
from app.config import ROOT  # noqa: E402
from app.data.http import CachedHttp  # noqa: E402
from app.llm.fallback import build_chain  # noqa: E402
from app.scoring.service import ScoringService  # noqa: E402
from app.store.db import connect  # noqa: E402
from evals.cases import Case, build_cases, facts, universal  # noqa: E402


def run_case(agent: Agent, case: Case, pause: float) -> dict[str, Any]:
    sid = None
    for q in case.setup:
        sid = agent.chat(q, sid).session_id
        time.sleep(pause)
    r = agent.chat(case.question, sid)
    results = [chk(r) for chk in case.checks] + universal(r)
    events = r.debug["events"]
    return {
        "id": case.id, "group": case.group, "question": case.question, "setup": case.setup,
        "passed": all(ok for ok, _ in results), "failed_checks": [msg for ok, msg in results if not ok],
        "infra_failure": any("all providers failed" in e for e in events),
        "response_type": r.answer["response_type"], "answer": r.answer.get("answer_markdown", ""),
        "model": r.debug["providers"][-1] if r.debug["providers"] else None,
        "tokens": r.debug["tokens"], "latency_ms": r.debug["latency_ms"], "rounds": r.debug["rounds"],
        "repairs": sum("repair requested" in e for e in events), "malformed": sum("malformed" in e for e in events),
        "waits": sum("waited" in e for e in events), "unverified": r.ungrounded_numbers,
        "tools": [f"{c['name']}({c['arguments']})" for c in r.debug["tool_calls"]],
        "events": events,   # fallbacks, malformed calls, repairs, rate limits: needed to diagnose failures
    }


def report_md(rows: list[dict], meta: dict) -> str:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["group"], []).append(r)
    n = len(rows)
    lines = [f"# Eval report — {meta['run_at']}", "",
             f"Chain: {', '.join(meta['chain'])} · config {meta['config_version']} · {n} cases", "",
             f"**Passed: {sum(r['passed'] for r in rows)}/{n}** "
             f"(flaky: {sum(r.get('flaky', False) for r in rows)}; infrastructure failures: "
             f"{sum(r['infra_failure'] and not r['passed'] for r in rows)})", "",
             "| Group | Passed |", "|---|---|"]
    lines += [f"| {g} | {sum(r['passed'] for r in rs)}/{len(rs)} |" for g, rs in groups.items()]
    answered = [r for r in rows if r["model"]]
    lines += ["", "| Metric | Value |", "|---|---|",
              f"| Avg tokens per turn | {sum(r['tokens'] for r in answered) / max(len(answered), 1):.0f} |",
              f"| Avg latency | {sum(r['latency_ms'] for r in answered) / max(len(answered), 1) / 1000:.1f} s |",
              f"| Repairs / malformed calls / capacity waits | {sum(r['repairs'] for r in rows)} / "
              f"{sum(r['malformed'] for r in rows)} / {sum(r['waits'] for r in rows)} |",
              "| Answered by | " + ", ".join(f"{m}: {sum(r['model'] == m for r in rows)}"
                                              for m in sorted({r['model'] for r in rows if r['model']})) + " |",
              "", "## Cases", ""]
    for r in rows:
        status = "PASS" if r["passed"] else ("FAIL (infrastructure)" if r["infra_failure"] else "FAIL")
        if r.get("flaky"):
            status += " (flaky: passed on re-run)"
        lines += [f"### {r['id']} — {status}", f"**Q:** {r['question']}" +
                  (f"  _(after: {' / '.join(r['setup'])})_" if r["setup"] else ""),
                  f"*{r['response_type']} · {r['model']} · {r['tokens']} tokens · {r['latency_ms'] / 1000:.1f} s*"]
        if r["failed_checks"]:
            lines.append("Failed checks: " + "; ".join(r["failed_checks"]))
        lines += ["", "\n".join("> " + ln for ln in r["answer"].split("\n")), ""]
    return "\n".join(lines)


def main() -> int:
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated case ids")
    ap.add_argument("--pause", type=float, default=20.0, help="seconds between turns (free-tier token limits)")
    ap.add_argument("--no-rerun", action="store_true")
    ap.add_argument("--model", help="run a single chain entry from config/llm.yaml (no fallback), for comparisons")
    a = ap.parse_args()

    http = CachedHttp(connect())
    ctx = ToolContext(http, ScoringService(http), datetime.now(timezone.utc).date())
    cases = build_cases(facts(ctx))
    if a.only:
        wanted = set(a.only.split(","))
        cases = [c for c in cases if c.id in wanted]
    chain = build_chain(only=a.model)
    agent = Agent(chain, ctx)
    meta = {"run_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "chain": [p.name for p in chain.providers], "config_version": ctx.scoring.cfg.version}
    out = ROOT / "evals" / "results"
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")

    def save() -> None:   # after every case, so a crash or quota stop never loses the answers
        (out / f"eval_{stamp}.json").write_text(json.dumps({"meta": meta, "cases": rows}, indent=1))
        (out / f"eval_{stamp}.md").write_text(report_md(rows, meta))

    def safe_run(case: Case) -> dict[str, Any]:
        try:
            return run_case(agent, case, a.pause)
        except Exception as e:  # noqa: BLE001 - record the crash as a failed case and keep going
            return {"id": case.id, "group": case.group, "question": case.question, "setup": case.setup,
                    "passed": False, "failed_checks": [f"runner crash: {type(e).__name__}: {e}"], "infra_failure": False,
                    "response_type": "crash", "answer": "", "model": None, "tokens": 0, "latency_ms": 0, "rounds": 0,
                    "repairs": 0, "malformed": 0, "waits": 0, "unverified": [], "tools": []}

    rows: list[dict[str, Any]] = []
    for i, case in enumerate(cases):
        if i:
            time.sleep(a.pause)
        row = safe_run(case)
        rows.append(row)
        save()
        print(f"[{'PASS' if row['passed'] else 'FAIL'}] {case.id:<22} {row['model']}  {'; '.join(row['failed_checks'])}", flush=True)
    if not a.no_rerun:
        for row in [r for r in rows if not r["passed"]]:
            time.sleep(a.pause)
            case = next(c for c in cases if c.id == row["id"])
            again = safe_run(case)
            row["rerun"] = {k: again[k] for k in ("passed", "failed_checks", "model", "answer", "infra_failure")}
            row["flaky"] = again["passed"]
            save()
            print(f"  re-run {case.id:<20} {'PASS (flaky)' if again['passed'] else 'FAIL again'}", flush=True)
    print(f"\n{sum(r['passed'] for r in rows)}/{len(rows)} passed "
          f"({sum(r.get('flaky', False) for r in rows)} flaky) -> evals/results/eval_{stamp}.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
