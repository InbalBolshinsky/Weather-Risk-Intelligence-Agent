"""Cached, retrying HTTP GET for upstream JSON APIs.

- Successful (200) responses are stored in SQLite `http_cache`, keyed by URL + sorted params.
- `ttl_s=None` caches forever (historical data never changes); otherwise entries expire.
- Retries transient failures (timeouts, connection errors, 429, 5xx) with exponential backoff.
- Non-retryable 4xx responses raise UpstreamError with the status, so callers can map
  them to meaning (e.g. NWS 400 "out of bounds" = location outside the US).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

import httpx

from app.config import USER_AGENT

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
DAY_S = 86_400


def history_ttl(end: str) -> float | None:
    """Cache TTL for a historical query ending on `end` (ISO date).

    Data older than ~30 days is immutable -> cache forever (None). Recent days can still be
    revised (ERA5T preliminary data, late station reports) -> cache for one day.
    """
    recent = date.fromisoformat(end) >= datetime.now(timezone.utc).date() - timedelta(days=30)
    return DAY_S if recent else None


class UpstreamError(RuntimeError):
    def __init__(self, source: str, status: int | None, message: str):
        super().__init__(f"{source}: HTTP {status}: {message}")
        self.source, self.status, self.message = source, status, message


@dataclass(frozen=True)
class Fetched:
    data: Any
    fetched_at: str
    from_cache: bool


# Hooks let a source enforce its own rate limits (see open_meteo.py).
BeforeHook = Callable[[dict[str, Any]], None]
AfterHook = Callable[[httpx.Response], None]


class CachedHttp:
    def __init__(self, conn: sqlite3.Connection, client: httpx.Client | None = None,
                 max_attempts: int = 3, backoff_s: float = 1.0, sleep: Callable[[float], None] = time.sleep):
        self.conn = conn
        self.client = client or httpx.Client(timeout=httpx.Timeout(60.0), headers={"User-Agent": USER_AGENT})
        self.max_attempts = max_attempts
        self.backoff_s = backoff_s
        self.sleep = sleep
        self._lock = threading.Lock()

    @staticmethod
    def cache_key(url: str, params: dict[str, Any]) -> str:
        canon = url + "?" + json.dumps(params, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canon.encode()).hexdigest()

    def get_json(self, source: str, url: str, params: dict[str, Any], ttl_s: float | None,
                 headers: dict[str, str] | None = None,
                 before: BeforeHook | None = None, after: AfterHook | None = None) -> Fetched:
        key = self.cache_key(url, params)
        cached = self._read(key)
        if cached is not None:
            return cached
        data = self._fetch(source, url, params, headers, before, after)
        now = datetime.now(timezone.utc)
        expires = None if ttl_s is None else now.timestamp() + ttl_s
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO http_cache(key, source, url, params, body, fetched_at, expires_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (key, source, url, json.dumps(params, sort_keys=True), json.dumps(data), now.isoformat(), expires))
            self.conn.commit()
        return Fetched(data=data, fetched_at=now.isoformat(), from_cache=False)

    def _read(self, key: str) -> Fetched | None:
        row = self.conn.execute("SELECT body, fetched_at, expires_at FROM http_cache WHERE key=?", (key,)).fetchone()
        if row is None or (row["expires_at"] is not None and row["expires_at"] < time.time()):
            return None
        return Fetched(data=json.loads(row["body"]), fetched_at=row["fetched_at"], from_cache=True)

    def _fetch(self, source: str, url: str, params: dict[str, Any], headers: dict[str, str] | None,
               before: BeforeHook | None, after: AfterHook | None) -> Any:
        last_err: str = ""
        for attempt in range(1, self.max_attempts + 1):
            if before:
                before(params)
            try:
                r = self.client.get(url, params=params, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_err = f"{type(e).__name__}: {e}"
                status = None
            else:
                if after:
                    after(r)
                if r.status_code == 200:
                    return r.json()
                status = r.status_code
                last_err = r.text[:300]
                if status not in RETRYABLE_STATUS:
                    raise UpstreamError(source, status, last_err)
                retry_after = r.headers.get("Retry-After")
                if retry_after and retry_after.isdigit() and attempt < self.max_attempts:
                    self.sleep(min(float(retry_after), 60.0))
                    continue
            if attempt < self.max_attempts:
                self.sleep(self.backoff_s * 2 ** (attempt - 1))
        raise UpstreamError(source, status, f"gave up after {self.max_attempts} attempts: {last_err}")
