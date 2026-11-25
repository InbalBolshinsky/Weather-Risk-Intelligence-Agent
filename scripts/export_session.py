"""Export the Claude Code session logs of this project to session/ (deliverable: the full AI session).

    python scripts/export_session.py                      # redacted raw logs + readable transcript
    python scripts/export_session.py --redact "Jane Doe"  # also replace these strings (repeatable)

Writes session/raw/*.jsonl.gz (every record, redacted) and session/transcript.md (messages, tool calls in short).
Redactions: email addresses, Groq org IDs, API-key patterns, the values of secrets in .env, the home and repo
paths, embedded files (PDFs, images) and any --redact strings. Re-run it as the very last step: the session
keeps growing after an export.
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGS = Path.home() / ".claude" / "projects" / str(ROOT).replace("/", "-")
OUT = ROOT / "session"
KEEP_EMAILS = {"noreply@anthropic.com"}
SECRET_KEYS = ("GROQ_API_KEY", "GOOGLE_API_KEY", "ALERT_WEBHOOK_URL", "DEMO_TOKEN")
EXCLUDE = {"d6ab3cea-d4ec-4e98-ab54-c21e6e27196c"}   # an unrelated chat (auto-saving setup) started in this folder


def build_redactor(extra: list[str]):
    pairs: list[tuple[str, str]] = []
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() in SECRET_KEYS and len(v.strip()) >= 8:
                pairs.append((v.strip(), "<redacted-secret>"))
    home = str(Path.home())
    pairs += [(str(ROOT), "<repo>"), (str(ROOT).replace("/", "-"), "<project>"), (home, "~"),
              (home.replace("/", "-"), "<home>"), (home.lstrip("/"), "~")]
    pairs += [(s, "<redacted>") for s in extra if s]
    patterns = [(re.compile(r"gsk_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|\bsk-[A-Za-z0-9]{20,}"), "<redacted-key>"),
                (re.compile(r"org_[A-Za-z0-9]{10,}"), "org_<redacted>")]
    email = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

    def redact(text: str) -> str:
        for old, new in pairs:
            text = text.replace(old, new)
        for pat, new in patterns:
            text = pat.sub(new, text)
        return email.sub(lambda m: m[0] if m[0].lower() in KEEP_EMAILS else "<email>", text)
    return redact


def strip_media(node):
    """Embedded files (the assignment PDF, screenshots) are replaced by a placeholder."""
    if isinstance(node, dict):
        src = node.get("source")
        if node.get("type") in ("document", "image") and isinstance(src, dict) and src.get("type") == "base64":
            kb = len(src.get("data", "")) * 3 // 4 // 1024
            return {"type": "text", "text": f"[{src.get('media_type', node['type'])} omitted: {kb} KB]"}
        return {k: strip_media(v) for k, v in node.items()}
    if isinstance(node, list):
        return [strip_media(x) for x in node]
    return node


def load(path: Path, redact) -> list[dict]:
    records = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            records.append(json.loads(redact(json.dumps(strip_media(json.loads(line)), ensure_ascii=False))))
        except json.JSONDecodeError:
            continue
    return records


# ----------------------------------------------------------------------------- readable transcript

_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


def _short(value, n: int = 160) -> str:
    s = " ".join(str(value).split())
    return s if len(s) <= n else s[:n] + "…"


def _tool_line(block: dict) -> str:
    inp = block.get("input") or {}
    detail = inp.get("description") or inp.get("file_path") or inp.get("query") or inp.get("prompt") or inp
    return f"- `{block.get('name')}`: {_short(detail)}"


def _user_text(content) -> str:
    if isinstance(content, str):
        parts = [content]
    else:
        parts = [b.get("text", "") for b in content if b.get("type") == "text"]
    return _REMINDER.sub("", "\n".join(parts)).strip()


def transcript(name: str, records: list[dict]) -> list[str]:
    out = [f"## {name}", ""]
    for r in records:
        if r.get("type") not in ("user", "assistant") or r.get("isMeta"):
            continue
        content = (r.get("message") or {}).get("content")
        stamp = (r.get("timestamp") or "")[:16].replace("T", " ")
        if r["type"] == "user":
            text = _user_text(content) if content else ""
            if not text or text.startswith("[Request interrupted"):
                continue
            if r.get("isCompactSummary") or text.startswith("This session is being continued"):
                out += ["<details><summary>Context summary (conversation compacted)</summary>", "", text, "",
                        "</details>", ""]
            else:
                out += [f"### User · {stamp} UTC", "", text, ""]
        else:
            for b in content if isinstance(content, list) else []:
                if b.get("type") == "text" and b.get("text", "").strip():
                    out += [f"**Claude:** {b['text'].strip()}", ""]
                elif b.get("type") == "tool_use":
                    out.append(_tool_line(b))
            if out and out[-1].startswith("- `"):
                out.append("")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--redact", action="append", default=[], help="extra string to replace (repeatable)")
    a = ap.parse_args()
    if not LOGS.exists():
        print("no session logs found for this project under ~/.claude/projects/")
        return 1
    redact = build_redactor(a.redact)
    sessions = sorted((p for p in LOGS.glob("*.jsonl") if p.stem not in EXCLUDE), key=lambda p: p.stat().st_mtime)
    agents = sorted(p for p in LOGS.glob("*/subagents/*.jsonl") if p.parent.parent.name not in EXCLUDE)
    for old in (OUT / "raw").glob("*.jsonl.gz"):   # a re-run replaces the whole export
        old.unlink()
    (OUT / "raw").mkdir(parents=True, exist_ok=True)
    md = ["# AI session transcript", "",
          "The full Claude Code session that built this project, exported by `scripts/export_session.py`. Shown: "
          "every user message, every Claude reply, and each tool call in one line. Tool results, Claude's internal "
          "reasoning and system context are in the redacted raw logs (`session/raw/`). Redacted: email addresses, "
          "local paths, organization IDs, embedded files (the assignment PDF, screenshots). No API keys appear.", ""]
    for i, path in enumerate(sessions + agents, 1):
        recs = load(path, redact)
        kind = "Sub-agent" if path in agents else "Session"
        label = f"{kind} {i}: {path.stem}"
        with gzip.open(OUT / "raw" / f"{path.stem}.jsonl.gz", "wt", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n")
        if kind == "Session":
            md += transcript(label, recs)
        else:
            md += ["<details><summary>" + label + "</summary>", ""] + transcript(label, recs)[2:] + ["</details>", ""]
    (OUT / "transcript.md").write_text("\n".join(md))
    print(f"{len(sessions)} sessions, {len(agents)} sub-agents -> session/raw/*.jsonl.gz, session/transcript.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
