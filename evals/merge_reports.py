"""Combine eval runs into one report (D68): for each case, the latest VALID result from runs on the shipping chain.

Valid = not a quota/infrastructure failure and not a runner crash. Single-model comparison runs (--model) are
excluded because they don't measure the shipped chain. Run 1 crashed before saving, so only its log line
(PASS/FAIL, model, failed checks) exists; the report says so for those cases.

    python evals/merge_reports.py      -> evals/results/eval_combined.{md,json}
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml  # noqa: E402

from app.config import CONFIG_DIR, ROOT  # noqa: E402
from evals.cases import build_cases  # noqa: E402

RESULTS = ROOT / "evals" / "results"
SHIPPING_CHAIN = [e["name"] for e in yaml.safe_load((CONFIG_DIR / "llm.yaml").read_text())["chain"]
                  if e.get("enabled", True)]
LINE = re.compile(r"^\[(PASS|FAIL)\] (\S+)\s+(\S+)\s*(.*)$")

# Human review of the printed answers (D65): where reading the answer contradicts the code checks.
REVIEW_NOTES = {
    "D7_window_too_long": ("incorrect", "Passes the code checks but says 'exceeds the 10-year limit'; Denver is a hub "
                           "(30-year limit; the tool would answer 2006-2025). The grounding check accepted '10' because "
                           "it appears in the tool description: the known lexical-grounding weakness."),
}


def run1_rows() -> list[dict]:
    path = RESULTS / "run1_stdout.txt"
    if not path.exists():
        return []
    rows = []
    for ln in path.read_text().splitlines():
        m = LINE.match(ln)
        if m:
            rows.append({"id": m[2], "passed": m[1] == "PASS", "model": m[3] if m[3] != "None" else None,
                         "failed_checks": [c for c in m[4].split("; ") if c], "answer": None,
                         "infra_failure": False, "response_type": "?", "source": "run 1 (log only: answers not saved)"})
    return rows


def json_runs() -> list[tuple[str, list[dict]]]:
    runs = []
    for p in sorted(RESULTS.glob("eval_2*.json")):
        d = json.loads(p.read_text())
        if d.get("meta", {}).get("chain") != SHIPPING_CHAIN:
            continue
        stamp = p.stem.removeprefix("eval_")
        runs.append((f"run {stamp}", [{**r, "source": f"run {stamp}"} for r in d["cases"]]))
    return runs


def valid(r: dict) -> bool:
    return not r.get("infra_failure") and r.get("response_type") != "crash"


def main() -> int:
    order = [c for c in build_cases(_dummy_facts())]   # case ids, groups and questions only
    history: dict[str, list[dict]] = {}
    for r in run1_rows():
        history.setdefault(r["id"], []).append(r)
    for _, rows in json_runs():
        for r in rows:
            history.setdefault(r["id"], []).append(r)

    final = []
    for c in order:
        tries = history.get(c.id, [])
        pick = next((r for r in reversed(tries) if valid(r)), tries[-1] if tries else None)
        status = "no result" if pick is None else ("PASS" if pick["passed"] else
                                                   ("NO VALID RESULT" if not valid(pick) else "FAIL"))
        review = REVIEW_NOTES.get(c.id)
        if review and status == "PASS" and review[0] == "incorrect":
            status = "PASS (checks) / FAIL (review)"
        final.append({"id": c.id, "group": c.group, "question": c.question, "status": status,
                      "review_note": review[1] if review else None,
                      "source": pick["source"] if pick else None, "model": pick.get("model") if pick else None,
                      "failed_checks": pick.get("failed_checks", []) if pick else [],
                      "answer": pick.get("answer") if pick else None, "attempts": len(tries)})

    (RESULTS / "eval_combined.json").write_text(json.dumps(final, indent=1))
    (RESULTS / "eval_combined.md").write_text(_md(final))
    passed = sum(r["status"] == "PASS" for r in final)
    print(f"{passed}/{len(final)} passed -> evals/results/eval_combined.md")
    return 0


def _dummy_facts() -> dict:
    """build_cases needs facts only for its check closures; the merge uses ids/groups/questions."""
    from app.config import hub_registry
    names = {h.id: (h.id, h.name) for h in hub_registry().hubs}
    return {"names": names, "mw_leader": "MSP", "mw_leader_score": "0", "overall_1": "HOU", "overall_2": "MIA",
            "den_snow_pct": "0", "mia_hurricane": "0", "hou_flood": "0", "hou_wdri": "0", "dal_flood": "0",
            "dal_dominant": "severe_convective", "phx_flood_conf": "low"}


def _md(rows: list[dict]) -> str:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["group"], []).append(r)
    n, passed = len(rows), sum(r["status"] == "PASS" for r in rows)
    checks_only = sum(r["status"].startswith("PASS") for r in rows)
    out = [f"# Eval report (combined) — {date.today().isoformat()}", "",
           f"Shipping chain: {', '.join(SHIPPING_CHAIN)}. For each case, the latest valid result (not a quota "
           f"failure) across the runs on this chain. **Caveat:** three small fixes landed between runs (non-hub "
           f"prompt line, tool error messages as grounding sources, tool-name prefix normalization); earlier passes "
           f"were not re-run after them. Run 1 crashed before saving, so its answers are not available.", "",
           f"**Passed: {passed}/{n}** after human review of the answers ({checks_only}/{n} on code checks alone).",
           "", "| Group | Passed |", "|---|---|"]
    out += [f"| {g} | {sum(r['status'] == 'PASS' for r in rs)}/{len(rs)} |" for g, rs in groups.items()]
    out += ["", "| Case | Status | Source | Model |", "|---|---|---|---|"]
    out += [f"| {r['id']} | {r['status']} | {r['source']} | {r['model'] or '—'} |" for r in rows]
    out += ["", "## Details", ""]
    for r in rows:
        out.append(f"### {r['id']} — {r['status']}")
        out.append(f"**Q:** {r['question']}  \n*source: {r['source']} · attempts: {r['attempts']}*")
        if r["failed_checks"]:
            out.append("Failed checks: " + "; ".join(r["failed_checks"]))
        if r.get("review_note"):
            out.append("Human review: " + r["review_note"])
        out.append("")
        out.append("\n".join("> " + ln for ln in r["answer"].split("\n")) if r["answer"] else
                   "> _(answer text not saved for this run)_")
        out.append("")
    return "\n".join(out)


if __name__ == "__main__":
    sys.exit(main())
