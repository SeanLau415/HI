#!/usr/bin/env python3
"""
Worker Engine — VPS monitoring daemon.
Reads config.yaml, scrapes stock pages, parses RSS feeds,
sends alerts via Apprise. SQLite used only for dedup state.

Deploy: python worker.py  OR  docker run ...

All bugs fixed (cumulative):
  BUG-1   : Database/MemoryState unified interface
  BUG-2   : TOCTOU-free atomic check_and_mark
  BUG-4   : CancelledError calls stop()
  BUG-5   : Semaphore wraps HTTP fetch only (not full task)
  BUG-6   : Composite scheduler key (name+url)
  BUG-7   : Windows platform guard
  BUG-8   : Response size limit (OOM defence)
  BUG-9   : None-aware interval fallback
  BUG-10  : content_hash two-step get/set
  BUG-11  : Hot-reload clears all timers
  BUG-A   : notify() captures Apprise return value
  BUG-B   : CancelledError calls stop()
  BUG-C   : RSS proxy in GUI + YAML
  BUG-NEW1: Empty Apprise guard (no infinite retry storm)
  BUG-NEW2: heartbeat_interval_hours <= 0 disables heartbeat
  BUG-NEW3: GUI interval=0 preserved on edit
  BUG-CPU : BeautifulSoup ops in thread-pool executor
  BUG-SEM : Semaphore wraps ONLY session.get() — released before
            backoff sleeps to prevent 429-deadlock starvation
  BUG-STOP: stop() idempotent via asyncio.Event + trigger_stop()
  BUG-PRUNE: DB prune decoupled from heartbeat (independent 24h timer)
  BUG-PRUNE2: Active records refreshed so they are never wrongly pruned
  BUG-LRU : MemoryState.get_content_hash refreshes LRU on cache hit
  BUG-CANCEL: asyncio.Event replaces task.cancel() for clean shutdown
"""
import asyncio
import hashlib
import json
import logging
import logging.handlers
import os
import platform
import random
import re
import signal
import sqlite3
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Union
from urllib.parse import urlparse

import apprise
import feedparser
import yaml
from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession

# ─── Logging ──────────────────────────────────────────────────────────────────

def _setup_logging():
    # Some Windows consoles still expose a legacy stdout encoding such as cp1252.
    # Without a safe error handler, emoji or CJK titles can trigger logging
    # traceback noise when alerts are written to the terminal stream.
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="backslashreplace")
        except Exception:
            pass
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    # Rotate at 2 MB, keep 3 files → max ~6 MB on disk
    fh = logging.handlers.RotatingFileHandler(
        "worker.log", maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

_setup_logging()
log = logging.getLogger("worker")

# ─── Constants ────────────────────────────────────────────────────────────────

CONFIG_PATH           = Path(os.environ.get("CONFIG_PATH", "config.yaml"))
_DB_ENV               = os.environ.get("DB_PATH", "")
DB_PATH: Optional[Path] = Path(_DB_ENV) if _DB_ENV else None
PERSIST_STATE         = os.environ.get("PERSIST_STATE", "false").strip().lower() in ("1","true","yes")

MAX_BACKOFF           = 300
IMPERSONATE           = "chrome120"
SESSION_MAX_AGE_H     = 6
MAX_CONCURRENT        = 4
_MAX_PARSE_ENV        = os.environ.get("MAX_PARSE_CONCURRENT", "1")
SEEN_RETENTION_DAYS   = 30
MEMORY_MAX_SEEN       = 5000
MEMORY_MAX_HASHES     = 1000
MAX_RESPONSE_BYTES    = 5 * 1024 * 1024
CONTENT_HASH_VERSION  = 2

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) "
    "Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_2) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.2 Safari/605.1.15",
]

# ─── Data Classes ─────────────────────────────────────────────────────────────

@dataclass
class GlobalConfig:
    apprise_urls: list[str]
    check_interval_minutes: int   = 30
    heartbeat_interval_hours: int = 24
    request_timeout: int          = 30
    randomize_delay_min: float    = 1.0
    randomize_delay_max: float    = 3.0

@dataclass
class TargetConfig:
    name: str
    url: str
    open_url: str                  = ""
    detection_mode: str           = "selector_disappears"
    css_selector: str             = ""
    proxy: str                    = ""
    check_interval_minutes: Optional[int] = None
    enabled: bool                 = True

@dataclass
class FeedConfig:
    name: str
    url: str
    keywords: list[str]           = field(default_factory=list)
    proxy: str                    = ""
    check_interval_minutes: Optional[int] = None
    enabled: bool                 = True

@dataclass
class AppConfig:
    glob: GlobalConfig
    targets: list[TargetConfig]
    feeds: list[FeedConfig]
    _mtime: float = 0.0

# ─── Config helpers ───────────────────────────────────────────────────────────

def _pi(v, d=0):
    try:    return int(v)
    except: return d

def _poi(v):
    try:    return int(v)
    except: return None

def _pf(v, d=0.0):
    try:    return float(v)
    except: return d

def _pb(v, d=True):
    if isinstance(v, bool): return v
    if isinstance(v, str):  return v.strip().lower() in ("1","true","yes","on")
    return d

MAX_PARSE_CONCURRENT = max(1, _pi(_MAX_PARSE_ENV, 1))

# ─── Config Loader ────────────────────────────────────────────────────────────

def load_config(path: Path) -> AppConfig:
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        log.warning("Config is not a mapping; using empty config")
        raw = {}

    g = raw.get("global", {}) if isinstance(raw.get("global",{}), dict) else {}
    raw_apprise_urls = g.get("apprise_urls", [])
    if isinstance(raw_apprise_urls, str):
        apprise_urls = [raw_apprise_urls.strip()] if raw_apprise_urls.strip() else []
    elif isinstance(raw_apprise_urls, list):
        apprise_urls = [str(u).strip() for u in raw_apprise_urls if str(u).strip()]
    else:
        log.warning("global.apprise_urls is not a list/string; using empty list")
        apprise_urls = []

    glob = GlobalConfig(
        apprise_urls             = apprise_urls,
        check_interval_minutes   = _pi(g.get("check_interval_minutes",30), 30),
        heartbeat_interval_hours = _pi(g.get("heartbeat_interval_hours",24), 24),
        request_timeout          = _pi(g.get("request_timeout",30), 30),
        randomize_delay_min      = _pf(g.get("randomize_delay_min",1.0), 1.0),
        randomize_delay_max      = _pf(g.get("randomize_delay_max",3.0), 3.0),
    )

    raw_targets = raw.get("targets") or []
    if isinstance(raw_targets, dict): raw_targets = [raw_targets]
    if not isinstance(raw_targets, list): raw_targets = []

    targets = [
        TargetConfig(
            name                   = str(t.get("name","Unknown")),
            url                    = str(t.get("url","")),
            open_url               = str(t.get("open_url","")),
            detection_mode         = str(t.get("detection_mode","selector_disappears")),
            css_selector           = str(t.get("css_selector","")),
            proxy                  = str(t.get("proxy","")),
            check_interval_minutes = _poi(t.get("check_interval_minutes")),
            enabled                = _pb(t.get("enabled",True)),
        )
        for t in raw_targets if isinstance(t, dict)
    ]

    raw_feeds = raw.get("rss_feeds") or []
    if isinstance(raw_feeds, dict): raw_feeds = [raw_feeds]
    if not isinstance(raw_feeds, list): raw_feeds = []

    feeds = [
        FeedConfig(
            name                   = str(f.get("name","Unknown")),
            url                    = str(f.get("url","")),
            keywords               = [str(k).lower() for k in (f.get("keywords",[]) or []) if str(k).strip()],
            proxy                  = str(f.get("proxy","")),
            check_interval_minutes = _poi(f.get("check_interval_minutes")),
            enabled                = _pb(f.get("enabled",True)),
        )
        for f in raw_feeds if isinstance(f, dict)
    ]

    cfg        = AppConfig(glob=glob, targets=targets, feeds=feeds)
    cfg._mtime = path.stat().st_mtime
    log.info(f"Config loaded — {len(targets)} targets, {len(feeds)} RSS feeds")
    return cfg

# ─── State Backend ────────────────────────────────────────────────────────────
#
# UNIFIED INTERFACE: both Database and MemoryState expose:
#   check_and_mark(key)             → bool
#   delete_key(key)
#   get_last_heartbeat()            → Optional[datetime]
#   update_heartbeat()
#   get_content_hash(url)           → Optional[str]
#   set_content_hash(url, hash)
#   prune()
#   close()

class Database:
    """SQLite-backed persistent dedup store (PERSIST_STATE=true)."""

    def __init__(self, path: Path):
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._lock = asyncio.Lock()
        self._init_schema()
        log.info(f"Database (SQLite) ready: {path}")

    def _init_schema(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS seen (
                key        TEXT PRIMARY KEY,
                first_seen TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS last_heartbeat (
                id  INTEGER PRIMARY KEY CHECK (id = 1),
                ts  TEXT NOT NULL
            );
        """)
        self._conn.commit()

    def _sync_check_and_mark(self, key: str) -> bool:
        """
        BUG-PRUNE2 FIX: Active items must never be wrongly pruned.
        On cache hit, refresh first_seen so the record stays alive.
        Without this, a continuously-in-stock item is deleted after 30 days
        and triggers a spurious "new stock" notification.
        """
        if self._conn.execute("SELECT 1 FROM seen WHERE key=?", (key,)).fetchone():
            # Refresh timestamp — item is still active, keep it alive
            self._conn.execute(
                "UPDATE seen SET first_seen=? WHERE key=?",
                (datetime.utcnow().isoformat(), key)
            )
            self._conn.commit()
            return False
        self._conn.execute(
            "INSERT INTO seen (key, first_seen) VALUES (?,?)",
            (key, datetime.utcnow().isoformat()),
        )
        self._conn.commit()
        return True

    def _sync_delete_key(self, key: str):
        self._conn.execute("DELETE FROM seen WHERE key=?", (key,))
        self._conn.commit()

    def _sync_get_last_heartbeat(self) -> Optional[datetime]:
        row = self._conn.execute(
            "SELECT ts FROM last_heartbeat WHERE id=1"
        ).fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    def _sync_update_heartbeat(self):
        self._conn.execute(
            "INSERT OR REPLACE INTO last_heartbeat (id, ts) VALUES (1,?)",
            (datetime.utcnow().isoformat(),),
        )
        self._conn.commit()

    def _sync_prune(self):
        """
        BUG-PRUNE FIX: Prune runs independently of heartbeat (called from
        Worker.run() on a 24h timer regardless of heartbeat setting).
        Only stale (inactive) records are deleted — active ones are refreshed
        by _sync_check_and_mark and _sync_get_content_hash.
        """
        expiry = datetime.utcnow() - timedelta(days=SEEN_RETENTION_DAYS)
        self._conn.execute("DELETE FROM seen WHERE first_seen < ?", (expiry.isoformat(),))
        self._conn.commit()

    def _sync_get_content_hash(self, url: str) -> Optional[str]:
        url_md5  = hashlib.md5(url.encode()).hexdigest()
        pattern  = f"hashval:v{CONTENT_HASH_VERSION}:{url_md5}:%"
        row = self._conn.execute(
            "SELECT key FROM seen WHERE key LIKE ?", (pattern,)
        ).fetchone()
        if row is None:
            return None
        # BUG-PRUNE2 FIX: refresh timestamp so active baselines are never pruned
        self._conn.execute(
            "UPDATE seen SET first_seen=? WHERE key=?",
            (datetime.utcnow().isoformat(), row[0])
        )
        self._conn.commit()
        parts = row[0].split(":", 3)
        return parts[3] if len(parts) == 4 else None

    def _sync_set_content_hash(self, url: str, new_hash: str):
        url_md5  = hashlib.md5(url.encode()).hexdigest()
        hash_key = f"hashval:v{CONTENT_HASH_VERSION}:{url_md5}:{new_hash}"
        pattern  = f"hashval:v{CONTENT_HASH_VERSION}:{url_md5}:%"
        self._conn.execute("DELETE FROM seen WHERE key LIKE ?", (pattern,))
        self._conn.execute(
            "INSERT INTO seen (key, first_seen) VALUES (?,?)",
            (hash_key, datetime.utcnow().isoformat()),
        )
        self._conn.commit()

    async def check_and_mark(self, key: str) -> bool:
        async with self._lock:
            return await asyncio.get_running_loop().run_in_executor(
                None, self._sync_check_and_mark, key)

    async def delete_key(self, key: str):
        async with self._lock:
            await asyncio.get_running_loop().run_in_executor(
                None, self._sync_delete_key, key)

    async def get_last_heartbeat(self) -> Optional[datetime]:
        async with self._lock:
            return await asyncio.get_running_loop().run_in_executor(
                None, self._sync_get_last_heartbeat)

    async def update_heartbeat(self):
        async with self._lock:
            await asyncio.get_running_loop().run_in_executor(
                None, self._sync_update_heartbeat)

    async def prune(self):
        async with self._lock:
            await asyncio.get_running_loop().run_in_executor(
                None, self._sync_prune)

    async def get_content_hash(self, url: str) -> Optional[str]:
        async with self._lock:
            return await asyncio.get_running_loop().run_in_executor(
                None, self._sync_get_content_hash, url)

    async def set_content_hash(self, url: str, new_hash: str):
        async with self._lock:
            await asyncio.get_running_loop().run_in_executor(
                None, self._sync_set_content_hash, url, new_hash)

    def close(self):
        self._conn.close()


class MemoryState:
    """In-memory dedup store (PERSIST_STATE=false, default). LRU-capped."""

    def __init__(self):
        self._seen:   OrderedDict[str, bool] = OrderedDict()
        self._hashes: OrderedDict[str, str]  = OrderedDict()
        self._last_heartbeat: Optional[datetime] = None
        self._lock = asyncio.Lock()
        log.info("MemoryState ready — no data persisted to disk")

    async def check_and_mark(self, key: str) -> bool:
        """Atomic check+insert — TOCTOU-safe. Refreshes LRU on hit."""
        async with self._lock:
            if key in self._seen:
                self._seen.move_to_end(key)  # refresh LRU position
                return False
            self._seen[key] = True
            self._seen.move_to_end(key)
            if len(self._seen) > MEMORY_MAX_SEEN:
                self._seen.popitem(last=False)
            return True

    async def delete_key(self, key: str):
        async with self._lock:
            self._seen.pop(key, None)

    async def get_last_heartbeat(self) -> Optional[datetime]:
        async with self._lock:
            return self._last_heartbeat

    async def update_heartbeat(self):
        async with self._lock:
            self._last_heartbeat = datetime.utcnow()

    async def prune(self):
        """No-op — LRU OrderedDict handles memory footprint natively."""
        pass

    async def get_content_hash(self, url: str) -> Optional[str]:
        async with self._lock:
            val = self._hashes.get(url)
            # BUG-LRU FIX: refresh LRU position on every read (even cache hits)
            # so frequently-monitored but stable pages are never wrongly evicted
            if val is not None:
                self._hashes.move_to_end(url)
            return val

    async def set_content_hash(self, url: str, new_hash: str):
        async with self._lock:
            self._hashes[url] = new_hash
            self._hashes.move_to_end(url)
            if len(self._hashes) > MEMORY_MAX_HASHES:
                self._hashes.popitem(last=False)

    def close(self):
        pass

StateBackend = Union[Database, MemoryState]

# ─── HTTP Session Manager ─────────────────────────────────────────────────────

class SessionManager:
    """
    Manages curl_cffi AsyncSession lifecycle.
    Renewal triggers:
      1. Age >= SESSION_MAX_AGE_H (prevents stale TLS after long idle)
      2. Transport error -> invalidate() (immediate recreation on next request)
    """

    def __init__(self):
        self._session: Optional[AsyncSession] = None
        self._born_at: float = 0.0
        self._stale: bool    = False
        self._lock = asyncio.Lock()

    async def get(self) -> AsyncSession:
        async with self._lock:
            age_h = (time.monotonic() - self._born_at) / 3600
            if self._session is None or self._stale or age_h >= SESSION_MAX_AGE_H:
                await self._close_inner()
                reason = ("initial" if self._session is None else
                          "error-triggered reset" if self._stale else
                          f"age {age_h:.1f}h >= {SESSION_MAX_AGE_H}h")
                log.info(f"[Session] Creating new HTTP session ({reason})")
                self._session = AsyncSession(impersonate=IMPERSONATE)
                self._born_at = time.monotonic()
                self._stale   = False
            return self._session

    def invalidate(self):
        self._stale = True

    async def _close_inner(self):
        if self._session:
            try:   await self._session.close()
            except Exception: pass
            self._session = None

    async def close(self):
        async with self._lock:
            await self._close_inner()

# ─── Notifications ────────────────────────────────────────────────────────────

def build_apprise(urls: list[str]) -> apprise.Apprise:
    ap = apprise.Apprise()
    for url in urls:
        if url: ap.add(url)
    return ap


def target_open_url(target: TargetConfig) -> str:
    open_url = (target.open_url or "").strip()
    return open_url if open_url else target.url

async def notify(
    ap: apprise.Apprise,
    title: str,
    body: str,
    notify_type: str = apprise.NotifyType.INFO,
) -> bool:
    """
    Send notification via Apprise.
    BUG-NEW1 FIX: Empty Apprise returns False silently → infinite rollback storm.
      Guard: if no URLs configured, log and return True so state is committed.
    BUG-A FIX: Capture ap.notify() real bool return value (not hardcoded True).
    """
    if len(ap) == 0:
        log.info(f"Alert generated (no Apprise URLs configured — skipped): {title}")
        return True

    try:
        success = await asyncio.get_running_loop().run_in_executor(
            None, lambda: ap.notify(title=title, body=body, notify_type=notify_type)
        )
        if success:
            log.info(f"Alert sent: {title}")
        else:
            log.error(f"Alert failed (Apprise returned False — check URLs/network): {title}")
        return bool(success)
    except Exception as exc:
        log.error(f"Alert failed with exception: {exc}")
        return False

# ─── HTTP Fetcher ─────────────────────────────────────────────────────────────

async def fetch_with_retry(
    worker: 'Worker',
    sm: SessionManager,
    sem: asyncio.Semaphore,
    url: str,
    *,
    timeout: int                     = 30,
    max_retries: int                 = 4,
    proxy: str                       = "",
    delay_range: tuple[float, float] = (1.0, 3.0),
) -> Optional[str]:
    """
    Anti-ban HTTP fetch.

    BUG-SEM FIX: Semaphore is placed INSIDE this function, wrapping ONLY the
    session.get() call. It is released immediately after the network response
    arrives — before any backoff sleep. This prevents 429/503 rate-limit sleeps
    from holding semaphore slots, which would deadlock all concurrent tasks.

    BUG-CANCEL FIX: All sleeps use worker.async_sleep() which wakes instantly
    when worker.trigger_stop() is called, enabling zero-delay clean shutdown.
    """
    backoff = 5.0
    proxies = {"https": proxy, "http": proxy} if proxy else None

    def build_headers(target_url: str) -> dict:
        headers = {
            "User-Agent":                random.choice(USER_AGENTS),
            "Accept":                    "text/html,application/xhtml+xml,application/xml;"
                                         "q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language":           "en-US,en;q=0.5",
            "Accept-Encoding":           "gzip, deflate, br",
            "Connection":                "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest":            "document",
            "Sec-Fetch-Mode":            "navigate",
            "Sec-Fetch-Site":            "none",
        }

        parsed = urlparse(target_url)
        host = (parsed.hostname or "").lower()

        # Public Nube APIs need site-origin headers to return the real JSON body.
        if (
            (host == "api.nube.sh" and parsed.path == "/product/v1/common/regions")
            or (host.endswith("-api.nube.sh") and parsed.path == "/order/v1/order/product/info")
        ):
            headers.update({
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://nube.sh",
                "Referer": "https://nube.sh/",
                "st-auth-mode": "cookie",
                "rid": "anti-csrf",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "cross-site",
            })

        return headers

    for attempt in range(max_retries):
        if attempt > 0:
            delay = random.uniform(*delay_range)
            log.debug(f"Retry {attempt+1}/{max_retries} in {delay:.1f}s -> {url}")
            await worker.async_sleep(delay)
            if not worker._running:
                return None

        headers = build_headers(url)

        try:
            # BUG-SEM FIX: semaphore wraps ONLY the network round-trip.
            # Released immediately after response — never held during sleeps.
            async with sem:
                session = await sm.get()
                resp = await session.get(
                    url, headers=headers, proxies=proxies,
                    timeout=timeout, impersonate=IMPERSONATE, allow_redirects=True,
                )

            # ── Process response OUTSIDE the semaphore ────────────────────────
            if resp.status_code == 200:
                cl_header = resp.headers.get("Content-Length", "")
                if cl_header.isdigit() and int(cl_header) > MAX_RESPONSE_BYTES:
                    log.error(f"Content-Length {int(cl_header)//1024}KB > limit — refused: {url}")
                    return None
                raw = resp.content
                if len(raw) > MAX_RESPONSE_BYTES:
                    log.error(f"Response body {len(raw)//1024}KB > limit — refused: {url}")
                    return None
                return resp.text

            if resp.status_code in (429, 503):
                wait = min(int(resp.headers.get("Retry-After", backoff)), MAX_BACKOFF)
                log.warning(f"Rate-limited {resp.status_code} {url} — waiting {wait}s")
                await worker.async_sleep(wait)   # sem already released
                if not worker._running: return None
                backoff = min(backoff * 2, MAX_BACKOFF)
            elif resp.status_code == 403:
                log.warning(f"403 Forbidden {url} (attempt {attempt+1})")
                await worker.async_sleep(min(backoff, MAX_BACKOFF))
                backoff *= 2
            elif resp.status_code >= 500:
                log.warning(f"Server error {resp.status_code} {url}")
                await worker.async_sleep(min(backoff, MAX_BACKOFF))
                backoff *= 2
            else:
                log.warning(f"Unexpected HTTP {resp.status_code} for {url}")
                return None

        except Exception as exc:
            sm.invalidate()
            level = logging.ERROR if attempt == max_retries - 1 else logging.WARNING
            log.log(level, f"Transport error {url} attempt {attempt+1}: {exc}")
            await worker.async_sleep(min(backoff, MAX_BACKOFF))
            backoff *= 2

    log.error(f"All {max_retries} attempts failed for {url}")
    return None

# ─── Stock Check Logic ────────────────────────────────────────────────────────
# BUG-CPU FIX: All BeautifulSoup ops offloaded to thread-pool executor.
# Parsing 5MB HTML in the event loop blocks all coroutines for 0.5-1.5s,
# causing cascading timeouts on a single-core VPS.

def _visible_text_hash(html: str) -> str:
    """SHA-256 of visible text. CPU-intensive — via run_in_executor."""
    stripped = html.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            obj = json.loads(stripped)
            volatile_keys = {
                "versionname", "versioncode", "updatetime", "timestamp",
                "requestid", "traceid", "nonce", "token", "sign", "signature",
                "sessionid", "session", "rayid", "requesttime", "servertime",
            }

            def normalize_json(value):
                if isinstance(value, dict):
                    out = {}
                    for key, item in value.items():
                        key_l = str(key).lower()
                        if key_l in volatile_keys or key_l.endswith("time"):
                            continue
                        out[key] = normalize_json(item)
                    return out
                if isinstance(value, list):
                    return [normalize_json(item) for item in value]
                return value

            normalized = normalize_json(obj)
            payload = json.dumps(
                normalized,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            return hashlib.sha256(payload.encode()).hexdigest()
        except Exception:
            pass

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script","style","meta","link","noscript","head"]):
        tag.decompose()
    text = " ".join(soup.get_text(separator=" ").split())
    # Ignore obvious anti-bot/session tokens that can rotate between requests
    # without representing a meaningful content change.
    text = re.sub(r"\b[0-9a-f]{32,64}\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()

def _check_selector(html: str, selector: str, mode: str) -> tuple[bool, str]:
    """CSS selector check. CPU-intensive — via run_in_executor."""
    found = bool(BeautifulSoup(html, "lxml").select(selector))
    if mode == "selector_disappears":
        ok     = not found
        detail = f"Selector `{selector}` {'disappeared' if ok else 'still present'}"
    else:
        ok     = found
        detail = f"Selector `{selector}` {'appeared' if ok else 'not found'}"
    return ok, detail

def _page_title(html: str, fallback: str) -> str:
    """Extract page <title>. CPU-intensive — via run_in_executor."""
    tag = BeautifulSoup(html, "lxml").find("title")
    return tag.get_text(strip=True) if tag else fallback

def _is_waf_challenge(html: str) -> bool:
    """Fast string scan — safe to run in event loop (no BS4)."""
    text = html.lower()
    strong_signals = (
        "just a moment",
        "attention required",
        "checking your browser",
        "please enable javascript and cookies",
        "cloudflare ray id",
        "__cf_chl_",
    )
    if any(signal in text for signal in strong_signals):
        return True

    # Generic terms such as analytics/recaptcha script variables can appear on
    # legitimate WHMCS storefronts, so require both a human-verification phrase
    # and a captcha/WAF vendor hint before classifying as a challenge page.
    verify_hits = sum(signal in text for signal in (
        "security check",
        "if you are human",
        "verify you are human",
        "verify that you are human",
        "one more step",
    ))
    vendor_hits = sum(signal in text for signal in (
        "captcha",
        "recaptcha",
        "hcaptcha",
        "turnstile",
        "cloudflare",
        "challenge",
        "ddos protection",
        "/cdn-cgi/challenge-platform/",
    ))
    return verify_hits >= 1 and vendor_hits >= 1

def _strip_html(raw: str, limit: int = 300) -> str:
    """Strip HTML. CPU-intensive — via run_in_executor."""
    return BeautifulSoup(raw, "lxml").get_text(separator=" ")[:limit].strip()

async def _run_parse_job(worker: 'Worker', func, *args):
    """
    Limit concurrent HTML/RSS parsing so cheap single-core VPS instances do
    not see multiple parser jobs spike CPU at the same time.
    """
    async with worker._parse_sem:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, func, *args)

async def check_target(
    worker: 'Worker',
    target: TargetConfig,
    cfg: AppConfig,
    sm: SessionManager,
    db: StateBackend,
    ap: apprise.Apprise,
    html: Optional[str] = None,
):
    log.info(f"[Target] Checking: {target.name}")
    if html is None:
        html = await fetch_with_retry(
            worker, sm, worker._sem, target.url,
            timeout=cfg.glob.request_timeout, proxy=target.proxy,
            delay_range=(cfg.glob.randomize_delay_min, cfg.glob.randomize_delay_max),
        )
    if html is None or not worker._running:
        return
    if _is_waf_challenge(html):
        log.warning(f"[Target] WAF challenge for {target.name} — skipping")
        return

    try:
        ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if target.detection_mode == "content_hash":
            new_hash = await _run_parse_job(worker, _visible_text_hash, html)
            old_hash = await db.get_content_hash(target.url)

            if old_hash is None:
                await db.set_content_hash(target.url, new_hash)
                log.info(f"[Target] First-run baseline stored: {target.name}")
            elif old_hash == new_hash:
                log.debug(f"[Target] No change: {target.name}")
            else:
                page_title = await _run_parse_job(worker, _page_title, html, target.name)
                open_url = target_open_url(target)
                success = await notify(ap,
                    title=f"🔔 Page Changed: {target.name}",
                    body=(f"{target.name} content has changed — check for stock!\n\n"
                          f"🌐 {open_url}\n📄 {page_title}\n⏰ {ts}"),
                    notify_type=apprise.NotifyType.SUCCESS)
                if success:
                    await db.set_content_hash(target.url, new_hash)
                else:
                    log.error(f"[Target] Notify failed for {target.name} (hash mode) — will retry next cycle")

        else:
            if not target.css_selector:
                log.warning(f"[Target] {target.name}: selector mode but no css_selector — skip")
                return

            in_stock, detail = await _run_parse_job(
                worker, _check_selector, html, target.css_selector, target.detection_mode
            )

            db_key = f"stock:{target.name}:{target.url}:in_stock"
            if in_stock:
                is_new = await db.check_and_mark(db_key)
                if is_new:
                    page_title = await _run_parse_job(worker, _page_title, html, target.name)
                    open_url = target_open_url(target)
                    success = await notify(ap,
                        title=f"✅ IN STOCK: {target.name}",
                        body=(f"{target.name} is now available!\n\n"
                              f"🌐 {open_url}\n📄 {page_title}\n"
                              f"🔍 {detail}\n⏰ {ts}"),
                        notify_type=apprise.NotifyType.SUCCESS)
                    if not success:
                        await db.delete_key(db_key)
                        log.error(f"[Target] Notification failed for {target.name}; will retry")
                else:
                    log.debug(f"[Target] Still in stock (already notified): {target.name}")
            else:
                await db.delete_key(db_key)
                log.debug(f"[Target] Out of stock: {target.name} — {detail}")

    except Exception as exc:
        log.error(f"[Target] Error in {target.name}: {exc}", exc_info=True)

# ─── RSS Logic ────────────────────────────────────────────────────────────────

def _entry_matches(entry: dict, keywords: list[str]) -> bool:
    if not keywords: return True
    text = (entry.get("title","") + " " + entry.get("summary","")).lower()
    return any(kw in text for kw in keywords)

async def check_feed(
    worker: 'Worker',
    feed: FeedConfig,
    sm: SessionManager,
    db: StateBackend,
    ap: apprise.Apprise,
    raw_xml: Optional[str] = None,
    timeout: int = 30,
    delay_range: tuple[float,float] = (1.0, 3.0),
):
    log.info(f"[RSS] Checking: {feed.name}")
    try:
        if raw_xml is None:
            raw_xml = await fetch_with_retry(
                worker, sm, worker._sem, feed.url,
                timeout=timeout, proxy=feed.proxy, delay_range=delay_range
            )
        if raw_xml is None or not worker._running:
            return
        parsed = await _run_parse_job(worker, feedparser.parse, raw_xml)
    except Exception as exc:
        log.error(f"[RSS] Parse error {feed.name}: {exc}")
        return

    if parsed.bozo and not parsed.entries:
        log.warning(f"[RSS] Malformed feed {feed.name}: {parsed.bozo_exception}")
        return

    init_key = f"rss_init:{feed.url}"
    is_first_run = await db.check_and_mark(init_key)
    new_count = 0
    baseline_count = 0
    for entry in parsed.entries[:20]:
        guid = entry.get("id") or entry.get("link") or entry.get("title","")
        if not guid: continue

        if not _entry_matches(entry, feed.keywords):
            log.debug(f"[RSS] Keyword miss: {entry.get('title','')[:60]}")
            continue

        db_key = f"rss:{feed.url}:{hashlib.md5(guid.encode()).hexdigest()}"
        is_new = await db.check_and_mark(db_key)
        if not is_new:
            continue

        if is_first_run:
            baseline_count += 1
            continue

        new_count += 1
        title   = entry.get("title","No title")
        link    = entry.get("link","")
        summary = await _run_parse_job(worker, _strip_html, entry.get("summary",""))
        pub     = (entry.get("published") or entry.get("updated") or "")[:25]
        kw_line = f"🏷 {', '.join(feed.keywords)}\n" if feed.keywords else ""

        success = await notify(ap,
            title=f"📡 [{feed.name}] {title[:60]}",
            body=(f"{title}\n\n{kw_line}📅 {pub}\n📝 {summary}\n\n🔗 {link}"),
            notify_type=apprise.NotifyType.INFO)
        if not success:
            await db.delete_key(db_key)
            log.error(f"[RSS] Notification failed for {title[:40]}; will retry")

        await worker.async_sleep(1.5)   # respect Telegram 30 msg/s
        if not worker._running:
            break

    if is_first_run:
        log.info(f"[RSS] First-run baseline stored: {feed.name} ({baseline_count} matched items)")
    elif new_count:
        log.info(f"[RSS] {new_count} new alerts sent for: {feed.name}")
    else:
        log.debug(f"[RSS] No new items for: {feed.name}")

# ─── Heartbeat ────────────────────────────────────────────────────────────────

async def maybe_send_heartbeat(cfg: AppConfig, db: StateBackend, ap: apprise.Apprise):
    # BUG-NEW2 FIX: <= 0 disables heartbeat entirely
    if cfg.glob.heartbeat_interval_hours <= 0:
        return
    last = await db.get_last_heartbeat()
    if last and (datetime.utcnow()-last) < timedelta(hours=cfg.glob.heartbeat_interval_hours):
        return

    success = await notify(ap,
        title="💓 Worker Heartbeat",
        body=(f"Worker is alive.\n\n"
              f"🖥 Targets: {sum(1 for t in cfg.targets if t.enabled)}\n"
              f"📡 RSS feeds: {sum(1 for f in cfg.feeds if f.enabled)}\n"
              f"⏱ Interval: {cfg.glob.check_interval_minutes} min\n"
              f"♻️ Session max age: {SESSION_MAX_AGE_H}h\n"
              f"⏰ {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"))
    if success:
        await db.update_heartbeat()
        log.info("[Heartbeat] Sent")
    else:
        log.warning("[Heartbeat] Send failed; timestamp not updated so it can retry")


# ─── Jitter helper ────────────────────────────────────────────────────────────

async def _run_jittered(worker: 'Worker', coro, delay: float):
    await worker.async_sleep(delay)
    if worker._running:
        return await coro

# ─── Main Scheduler ───────────────────────────────────────────────────────────

class Worker:
    """
    Key design decisions:

    BUG-CANCEL FIX: asyncio.Event() replaces task.cancel() for shutdown.
      - trigger_stop() sets _running=False AND wakes all async_sleep() calls.
      - No coroutine is forcibly cancelled mid-operation.
      - SQLite WAL checkpoint completes cleanly.

    BUG-SEM FIX: Semaphore lives in fetch_with_retry, wraps only session.get().
      Released before any backoff sleep — prevents 429-deadlock starvation.

    BUG-PRUNE FIX: prune() runs every 24h independently of heartbeat.
    BUG-PRUNE2 FIX: Active records refreshed in check_and_mark/_sync_get_content_hash.
    """

    def __init__(self):
        self.cfg: Optional[AppConfig]        = None
        self.db:  Optional[StateBackend]     = None
        self._sm  = SessionManager()
        self._ap: Optional[apprise.Apprise]  = None
        self._running     = True
        self._stop_event  = asyncio.Event()
        self._next_check: dict[str, float]   = {}
        self._sem = asyncio.Semaphore(MAX_CONCURRENT)
        self._parse_sem = asyncio.Semaphore(MAX_PARSE_CONCURRENT)
        self._last_cfg_mtime: float          = 0.0
        self._last_prune: float              = time.monotonic()

    def trigger_stop(self):
        """
        BUG-CANCEL FIX: Set the stop flag AND wake all async_sleep() calls.
        Idempotent — safe to call multiple times.
        """
        self._running = False
        self._stop_event.set()

    async def async_sleep(self, delay: float):
        """
        Interruptible sleep. Wakes instantly when trigger_stop() is called.
        Replaces all asyncio.sleep() calls in the hot path so Ctrl+C
        responds within milliseconds instead of waiting out the full delay.
        """
        if delay <= 0 or not self._running:
            return
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    # ── Config hot-reload ─────────────────────────────────────────────────────

    def _load_or_reload(self) -> bool:
        if not CONFIG_PATH.exists():
            log.error(f"Config not found: {CONFIG_PATH}")
            return False
        mtime = CONFIG_PATH.stat().st_mtime
        if self.cfg and mtime == self._last_cfg_mtime:
            return True
        try:
            self.cfg             = load_config(CONFIG_PATH)
            self._last_cfg_mtime = mtime
            self._ap             = build_apprise(self.cfg.glob.apprise_urls)
            # BUG-11 FIX: clear timers so new intervals take effect immediately
            self._next_check.clear()
            log.info("Config changed — all check timers reset, next cycle runs immediately")
            return True
        except Exception as exc:
            log.error(f"Config reload failed: {exc}")
            return self.cfg is not None

    def _is_due(self, key: str, interval_min: int) -> bool:
        now = time.monotonic()
        if now >= self._next_check.get(key, 0):
            self._next_check[key] = now + interval_min * 60
            return True
        return False

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self):
        if PERSIST_STATE:
            if DB_PATH:
                self.db = Database(DB_PATH)
            else:
                log.warning("PERSIST_STATE=true but DB_PATH not set — using MemoryState")
                self.db = MemoryState()
        else:
            self.db = MemoryState()

        if not self._load_or_reload():
            log.critical("Cannot start without config.yaml — exiting.")
            sys.exit(1)

        state_desc = (f"SQLite@{DB_PATH}" if isinstance(self.db, Database) else "memory")
        log.info(f"Worker started | state={state_desc} | "
                 f"session_max_age={SESSION_MAX_AGE_H}h | concurrency={MAX_CONCURRENT} | "
                 f"parse_concurrency={MAX_PARSE_CONCURRENT} | "
                 f"os={platform.system()}")

        while self._running:
            try:
                self._load_or_reload()
                await maybe_send_heartbeat(self.cfg, self.db, self._ap)

                # BUG-PRUNE FIX: independent 24h prune timer
                now_mono = time.monotonic()
                if now_mono - self._last_prune >= 86400:
                    try:
                        await self.db.prune()
                        self._last_prune = now_mono
                        log.debug("DB prune completed")
                    except Exception as exc:
                        log.warning(f"DB prune failed: {exc}")

                tasks = []
                for target in self.cfg.targets:
                    if not target.enabled: continue
                    interval = (target.check_interval_minutes
                                if target.check_interval_minutes is not None
                                else self.cfg.glob.check_interval_minutes)
                    # BUG-6 FIX: composite key prevents same-URL starvation
                    if self._is_due(f"target:{target.name}:{target.url}", interval):
                        tasks.append(check_target(self, target, self.cfg, self._sm, self.db, self._ap))

                for feed in self.cfg.feeds:
                    if not feed.enabled: continue
                    interval = (feed.check_interval_minutes
                                if feed.check_interval_minutes is not None
                                else self.cfg.glob.check_interval_minutes)
                    if self._is_due(f"feed:{feed.name}:{feed.url}", interval):
                        tasks.append(check_feed(self, feed, self._sm, self.db, self._ap))

                if tasks:
                    n          = len(tasks)
                    max_jitter = min(n * 0.8, 10.0)
                    jitter_coros = [
                        _run_jittered(self, t, random.uniform(0, max_jitter * i / max(n-1,1)))
                        for i, t in enumerate(tasks)
                    ]
                    await asyncio.gather(*jitter_coros, return_exceptions=True)

            except Exception as exc:
                log.error(f"Fatal error in main loop: {exc}", exc_info=True)

            if not self._running:
                break
            log.debug("Loop done — sleeping 60s")
            await self.async_sleep(60)

    async def stop(self):
        """
        BUG-CANCEL FIX: Uses trigger_stop() which is idempotent and wakes
        all async_sleep() calls. Safe to call multiple times (no double-close).
        """
        self.trigger_stop()
        log.info("Shutdown initiated — cleaning up.")
        await self._sm.close()
        if self.db:
            try:
                self.db.close()
            except Exception as e:
                log.warning(f"DB close warning: {e}")
            self.db = None
        log.info("Cleanup complete.")

# ─── Entry Point ──────────────────────────────────────────────────────────────

async def main():
    worker = Worker()
    loop   = asyncio.get_running_loop()

    def _on_signal():
        """
        BUG-CANCEL FIX: trigger_stop() instead of task.cancel().
        Sets _stop_event → all async_sleep() calls wake immediately →
        worker.run() loop exits cleanly → finally block calls stop().
        No coroutine is forcibly cancelled mid-operation.
        """
        log.info("OS signal received — triggering graceful shutdown")
        worker.trigger_stop()

    # BUG-7 FIX: add_signal_handler not supported on Windows
    if platform.system() != "Windows":
        loop.add_signal_handler(signal.SIGINT,  _on_signal)
        loop.add_signal_handler(signal.SIGTERM, _on_signal)
    else:
        log.warning("Windows OS detected: graceful shutdown via Ctrl+C may be limited. "
                    "Deploy worker on Linux/macOS for production use.")

    try:
        await worker.run()
    except asyncio.CancelledError:
        log.info("Worker cancelled via OS signal.")
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt — shutting down.")
    finally:
        await worker.stop()

if __name__ == "__main__":
    asyncio.run(main())
