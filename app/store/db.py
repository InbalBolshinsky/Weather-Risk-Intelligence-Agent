"""SQLite storage: one file, created on first use (PLAN.md §2.3)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from app.config import db_path

SCHEMA = """
-- Raw upstream responses. expires_at NULL = never expires (immutable history).
CREATE TABLE IF NOT EXISTS http_cache (
    key         TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    url         TEXT NOT NULL,
    params      TEXT NOT NULL,
    body        TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    expires_at  REAL
);

-- FEMA National Risk Index, all US counties (loaded at refresh; ~3,200 rows).
CREATE TABLE IF NOT EXISTS nri_counties (
    stcofips    TEXT PRIMARY KEY,
    state       TEXT NOT NULL,
    county      TEXT NOT NULL,
    nri_ver     TEXT NOT NULL,
    attrs       TEXT NOT NULL,     -- JSON: {HAZ_AFREQ: float|null, HAZ_RISKR: str, ...}
    loaded_at   TEXT NOT NULL
);

-- Daily counters for budget caps (e.g. live non-hub profiles cost ~157 Open-Meteo calls each).
CREATE TABLE IF NOT EXISTS usage_counters (
    day    TEXT NOT NULL,
    key    TEXT NOT NULL,
    count  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, key)
);

-- Conversation turns (D27 history policy needs both the full and the compact form).
CREATE TABLE IF NOT EXISTS turns (
    session_id   TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    user_message TEXT NOT NULL,
    full_json    TEXT NOT NULL,    -- every message of the turn (assistant tool calls + tool results)
    answer_json  TEXT NOT NULL,    -- the final validated answer
    created_at   TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
);
"""


def bump_counter(conn: sqlite3.Connection, day: str, key: str, limit: int) -> bool:
    """Increment a daily counter if it's below `limit`. Returns False (no increment) at the limit."""
    row = conn.execute("SELECT count FROM usage_counters WHERE day=? AND key=?", (day, key)).fetchone()
    if row and row["count"] >= limit:
        return False
    conn.execute("INSERT INTO usage_counters(day, key, count) VALUES (?,?,1) "
                 "ON CONFLICT(day, key) DO UPDATE SET count = count + 1", (day, key))
    conn.commit()
    return True


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn
