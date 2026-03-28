#!/usr/bin/env python3
"""
Worker Engine — VPS monitoring daemon.
Reads config.yaml, scrapes stock pages, parses RSS feeds,
sends alerts via Apprise. SQLite used only for dedup state.

Deploy: python worker.py  OR  docker run ...
"""
import asyncio
import hashlib
import logging
import logging.handlers
import os
import random
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import apprise
import feedparser
import yaml
from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession

# ─── Logging (rotating file — protects SSD on small VPS) ─────────────────────

def _setup_logging():
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

CONFIG_PATH         = Path(os.environ.get("CONFIG_PATH", "config.yaml"))
DB_PATH             = Path(os.environ["DB_PATH"]) if os.environ.get("DB_PATH") else None
PERSIST_STATE       = os.environ.get("PERSIST_STATE", "false").strip().lower() in ("1", "true", "yes")
MAX_BACKOFF         = 300   # 5 min ceiling on retry backoff
IMPERSONATE         = "chrome120"
SESSION_MAX_AGE_H   = 6     # recreate HTTP session every 6 hours regardless
MAX_CONCURRENT      = 4     # semaphore cap for 1C/2G VPS
SEEN_RETENTION_DAYS = 30    # prune seen/rss/hash dedup history older than this

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
    # selector_disappears | selector_appears | content_hash
    detection_mode: str     = "selector_disappears"
    css_selector: str       = ""
    proxy: str              = ""
    check_interval_minutes: Optional[int] = None
    enabled: bool           = True

@dataclass
class FeedConfig:
    name: str
    url: str
    keywords: list[str]     = field(default_factory=list)
    check_interval_minutes: Optional[int] = None
    enabled: bool           = True

@dataclass
class AppConfig:
    glob: GlobalConfig
    targets: list[TargetConfig]
    feeds: list[FeedConfig]
    _mtime: float = 0.0


def _parse_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_optional_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_bool(value, default=True):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return default


# ─── Config Loader ────────────────────────────────────────────────────────────

def load_config(path: Path) -> AppConfig:
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        log.warning("Config file is not a mapping; using empty configuration")
        raw = {}

    g = raw.get("global", {}) if isinstance(raw.get("global", {}), dict) else {}
    glob = GlobalConfig(
        apprise_urls              = g.get("apprise_urls", []),
        check_interval_minutes    = _parse_int(g.get("check_interval_minutes", 30), 30),
        heartbeat_interval_hours  = _parse_int(g.get("heartbeat_interval_hours", 24), 24),
        request_timeout           = _parse_int(g.get("request_timeout", 30), 30),
        randomize_delay_min       = _parse_float(g.get("randomize_delay_min", 1.0), 1.0),
        randomize_delay_max       = _parse_float(g.get("randomize_delay_max", 3.0), 3.0),
    )

    raw_targets = raw.get("targets") or []
    if isinstance(raw_targets, dict):
        raw_targets = [raw_targets]
    if not isinstance(raw_targets, list):
        raw_targets = []

    targets = []
    for t in raw_targets:
        if not isinstance(t, dict):
            continue
        targets.append(TargetConfig(
            name                   = t.get("name", "Unknown"),
            url                    = t.get("url", ""),
            detection_mode         = t.get("detection_mode", "selector_disappears"),
            css_selector           = t.get("css_selector", ""),
            proxy                  = t.get("proxy", ""),
            check_interval_minutes = _parse_optional_int(t.get("check_interval_minutes")),
            enabled                = _parse_bool(t.get("enabled", True)),
        ))

    raw_feeds = raw.get("rss_feeds") or []
    if isinstance(raw_feeds, dict):
        raw_feeds = [raw_feeds]
    if not isinstance(raw_feeds, list):
        raw_feeds = []

    feeds = []
    for f in raw_feeds:
        if not isinstance(f, dict):
            continue
        feeds.append(FeedConfig(
            name                   = f.get("name", "Unknown"),
            url                    = f.get("url", ""),
            keywords               = [k.lower() for k in (f.get("keywords", []) or []) if isinstance(k, str)],
            check_interval_minutes = _parse_optional_int(f.get("check_interval_minutes")),
            enabled                = _parse_bool(f.get("enabled", True)),
        ))

    cfg        = AppConfig(glob=glob, targets=targets, feeds=feeds)
    cfg._mtime = path.stat().st_mtime
    log.info(f"Config loaded — {len(targets)} targets, {len(feeds)} RSS feeds")
    return cfg

# ─── Database — asyncio.Lock-protected dedup store ────────────────────────────
#
# WHY asyncio.Lock instead of just INSERT OR IGNORE?
#
#   asyncio is cooperative, but two coroutines can still produce a TOCTOU race:
#
#     Coro A: is_seen("X") → False     ← A reads before B's write
#     Coro B: is_seen("X") → False     ← B reads before A's write
#     Coro A: mark_seen("X") → alert ✓
#     Coro B: mark_seen("X") → DUPLICATE alert ✗   ← the bug
#
#   The Lock serialises check+write into one atomic step.
#   check_same_thread=False is safe because our Lock guarantees single-writer.
#   PRAGMA WAL allows concurrent readers without blocking writers.
#
class Database:
    def __init__(self, path: Path):
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._lock = asyncio.Lock()
        self._init_schema()
        log.info(f"Database ready: {path}")

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

    async def check_and_mark(self, key: str) -> bool:
        """
        Atomically check-then-insert.
        Returns True  → key is NEW  → caller SHOULD send alert.
        Returns False → already seen → skip.
        """
        async with self._lock:
            if self._conn.execute(
                "SELECT 1 FROM seen WHERE key = ?", (key,)
            ).fetchone():
                return False
            self._conn.execute(
                "INSERT OR IGNORE INTO seen (key, first_seen) VALUES (?, ?)",
                (key, datetime.utcnow().isoformat()),
            )
            self._conn.commit()
            return True

    async def delete_key(self, key: str):
        """Remove key (e.g. item went back out-of-stock → reset so we re-alert)."""
        async with self._lock:
            self._conn.execute("DELETE FROM seen WHERE key = ?", (key,))
            self._conn.commit()

    async def check_and_swap_hash(self, url: str, new_hash: str) -> tuple[bool, bool]:
        """
        content_hash mode: atomically replace old hash with new one.
        Returns (hash_changed: bool, had_previous_hash: bool).
        """
        hash_key = f"hashval:{url}:{new_hash}"
        pattern  = f"hashval:{url}:%"
        async with self._lock:
            if self._conn.execute(
                "SELECT 1 FROM seen WHERE key = ?", (hash_key,)
            ).fetchone():
                return False, True   # same as last time

            had_prev = bool(self._conn.execute(
                "SELECT 1 FROM seen WHERE key LIKE ?", (pattern,)
            ).fetchone())

            self._conn.execute("DELETE FROM seen WHERE key LIKE ?", (pattern,))
            self._conn.execute(
                "INSERT INTO seen (key, first_seen) VALUES (?, ?)",
                (hash_key, datetime.utcnow().isoformat()),
            )
            self._conn.commit()
            return True, had_prev

    async def get_last_heartbeat(self) -> Optional[datetime]:
        async with self._lock:
            row = self._conn.execute(
                "SELECT ts FROM last_heartbeat WHERE id = 1"
            ).fetchone()
            return datetime.fromisoformat(row[0]) if row else None

    async def update_heartbeat(self):
        async with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO last_heartbeat (id, ts) VALUES (1, ?)",
                (datetime.utcnow().isoformat(),),
            )
            expiry = datetime.utcnow() - timedelta(days=SEEN_RETENTION_DAYS)
            self._conn.execute(
                "DELETE FROM seen WHERE first_seen < ?",
                (expiry.isoformat(),),
            )
            self._conn.commit()

    def close(self):
        self._conn.close()


class MemoryState:
    def __init__(self):
        self._seen: set[str] = set()
        self._hashes: dict[str, str] = {}
        self._last_heartbeat: Optional[datetime] = None
        self._lock = asyncio.Lock()
        log.info("Memory state ready: no local history or dedup data is persisted.")

    async def check_and_mark(self, key: str) -> bool:
        async with self._lock:
            if key in self._seen:
                return False
            self._seen.add(key)
            return True

    async def delete_key(self, key: str):
        async with self._lock:
            self._seen.discard(key)

    async def check_and_swap_hash(self, url: str, new_hash: str) -> tuple[bool, bool]:
        async with self._lock:
            old_hash = self._hashes.get(url)
            if old_hash == new_hash:
                return False, old_hash is not None
            had_prev = old_hash is not None
            self._hashes[url] = new_hash
            return True, had_prev

    async def get_last_heartbeat(self) -> Optional[datetime]:
        async with self._lock:
            return self._last_heartbeat

    async def update_heartbeat(self):
        async with self._lock:
            self._last_heartbeat = datetime.utcnow()

    def close(self):
        pass

# ─── HTTP Session Manager ─────────────────────────────────────────────────────
#
# ROOT CAUSE of the "days-later stale session" bug:
#
#   OLD code:
#       async with AsyncSession(...) as session:   ← session born once
#           while self._running:                   ← loop runs for days
#               await asyncio.sleep(60)            ← session never renewed
#
#   After days of idle periods the underlying TCP/TLS connection is silently
#   dropped by the remote server or your VPS's conntrack table.  The session
#   object still exists in memory but all subsequent requests raise CurlError
#   or Timeout, and there's no code path that ever recreates it.
#
# FIX — SessionManager with two renewal triggers:
#   1. Age-based:  unconditionally recreate every SESSION_MAX_AGE_H hours.
#   2. Error-based: any transport-level error (not HTTP errors) marks the
#      session stale immediately via invalidate(), forcing recreation on
#      the very next request.
#
class SessionManager:
    def __init__(self):
        self._session: Optional[AsyncSession] = None
        self._born_at: float = 0.0          # monotonic timestamp of creation
        self._stale:   bool  = False         # error-triggered immediate reset

    async def get(self) -> AsyncSession:
        """Return a live session, creating or recreating as needed."""
        age_h = (time.monotonic() - self._born_at) / 3600
        if self._session is None or self._stale or age_h >= SESSION_MAX_AGE_H:
            await self._close()
            reason = "initial" if self._session is None else \
                     "error-triggered reset" if self._stale else \
                     f"age {age_h:.1f}h ≥ {SESSION_MAX_AGE_H}h limit"
            log.info(f"[Session] Creating new HTTP session ({reason})")
            self._session = AsyncSession(impersonate=IMPERSONATE)
            self._born_at = time.monotonic()
            self._stale   = False
        return self._session

    def invalidate(self):
        """Call this on transport-level errors to force recreation on next request."""
        self._stale = True

    async def _close(self):
        if self._session:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None

    async def close(self):
        await self._close()

# ─── Notifications ────────────────────────────────────────────────────────────

def build_apprise(urls: list[str]) -> apprise.Apprise:
    ap = apprise.Apprise()
    for url in urls:
        if url:
            ap.add(url)
    return ap

async def notify(
    ap: apprise.Apprise,
    title: str,
    body: str,
    notify_type: str = apprise.NotifyType.INFO,
):
    try:
        loop = asyncio.get_running_loop()   # 3.10+-safe (get_event_loop deprecated)
        await loop.run_in_executor(
            None, lambda: ap.notify(title=title, body=body, notify_type=notify_type)
        )
        log.info(f"Alert sent: {title}")
    except Exception as exc:
        log.error(f"Alert failed: {exc}")

# ─── HTTP Fetcher — anti-ban + exponential backoff ────────────────────────────

async def fetch_with_retry(
    sm: SessionManager,
    url: str,
    *,
    timeout: int                  = 30,
    max_retries: int              = 4,
    proxy: str                    = "",
    delay_range: tuple[float, float] = (1.0, 3.0),
) -> Optional[str]:
    """
    Fetches URL with full anti-ban stack:
      - curl_cffi Chrome impersonation (beats Cloudflare JA3/TLS fingerprinting)
      - Rotating User-Agent headers
      - Randomised inter-retry delays
      - Exponential backoff on 429 / 403 / 5xx
      - Transport-error detection → marks session stale for immediate recreation
    """
    backoff = 5.0
    proxies = {"https": proxy, "http": proxy} if proxy else None

    for attempt in range(max_retries):
        if attempt > 0:
            delay = random.uniform(*delay_range)
            log.debug(f"Retry {attempt+1}/{max_retries} in {delay:.1f}s → {url}")
            await asyncio.sleep(delay)

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

        try:
            session = await sm.get()
            resp = await session.get(
                url,
                headers=headers,
                proxies=proxies,
                timeout=timeout,
                impersonate=IMPERSONATE,
                allow_redirects=True,
            )

            if resp.status_code == 200:
                return resp.text

            # ── HTTP-level errors (session itself is still healthy) ────────
            if resp.status_code in (429, 503):
                wait = min(int(resp.headers.get("Retry-After", backoff)), MAX_BACKOFF)
                log.warning(f"Rate-limited {resp.status_code} on {url} — waiting {wait}s")
                await asyncio.sleep(wait)
                backoff = min(backoff * 2, MAX_BACKOFF)

            elif resp.status_code == 403:
                log.warning(f"403 Forbidden {url} (attempt {attempt+1})")
                await asyncio.sleep(min(backoff, MAX_BACKOFF))
                backoff *= 2

            elif resp.status_code >= 500:
                log.warning(f"Server error {resp.status_code} {url}")
                await asyncio.sleep(min(backoff, MAX_BACKOFF))
                backoff *= 2

            else:
                log.warning(f"Unexpected HTTP {resp.status_code} for {url}")
                return None

        except Exception as exc:
            # ── Transport-level error (timeout, CURL, TCP reset, stale TLS) ──
            # Mark the session stale → SessionManager will recreate on next call.
            sm.invalidate()
            log.error(f"Transport error {url} attempt {attempt+1}: {exc}")
            await asyncio.sleep(min(backoff, MAX_BACKOFF))
            backoff *= 2

    log.error(f"All {max_retries} attempts failed for {url}")
    return None

# ─── Stock Check Logic ────────────────────────────────────────────────────────

def _visible_text_hash(html: str) -> str:
    """SHA-256 of visible page text — ignores scripts/styles/nav chrome."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "meta", "link", "noscript", "head"]):
        tag.decompose()
    text = " ".join(soup.get_text(separator=" ").split())
    return hashlib.sha256(text.encode()).hexdigest()

def _check_selector(html: str, selector: str, mode: str) -> tuple[bool, str]:
    soup  = BeautifulSoup(html, "lxml")
    found = bool(soup.select(selector))
    if mode == "selector_disappears":
        ok     = not found
        detail = f"Selector `{selector}` {'disappeared ✓' if ok else 'still present'}"
    else:  # selector_appears
        ok     = found
        detail = f"Selector `{selector}` {'appeared ✓' if ok else 'not found'}"
    return ok, detail

def _page_title(html: str, fallback: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    tag  = soup.find("title")
    return tag.get_text(strip=True) if tag else fallback

async def check_target(
    target: TargetConfig,
    cfg: AppConfig,
    sm: SessionManager,
    db: Database,
    ap: apprise.Apprise,
):
    log.info(f"[Target] Checking: {target.name}")
    html = await fetch_with_retry(
        sm, target.url,
        timeout     = cfg.glob.request_timeout,
        proxy       = target.proxy,
        delay_range = (cfg.glob.randomize_delay_min, cfg.glob.randomize_delay_max),
    )
    if html is None:
        return

    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if target.detection_mode == "content_hash":
            changed, had_prev = await db.check_and_swap_hash(
                target.url, _visible_text_hash(html)
            )
            if changed and had_prev:
                await notify(
                    ap,
                    title = f"🔔 Page Changed: {target.name}",
                    body  = (
                        f"**{target.name}** content has changed — check for stock!\n\n"
                        f"🌐 {target.url}\n"
                        f"📄 {_page_title(html, target.url)}\n"
                        f"⏰ {ts}\n\n"
                        f"[Open page]({target.url})"
                    ),
                    notify_type = apprise.NotifyType.SUCCESS,
                )
            elif changed and not had_prev:
                log.info(f"[Target] First-run baseline stored: {target.name}")
            else:
                log.debug(f"[Target] No change: {target.name}")

        else:
            if not target.css_selector:
                log.warning(f"[Target] {target.name}: selector mode set but no css_selector — skip")
                return

            in_stock, detail = _check_selector(
                html, target.css_selector, target.detection_mode
            )
            db_key = f"stock:{target.url}:in_stock"

            if in_stock:
                is_new = await db.check_and_mark(db_key)
                if is_new:
                    await notify(
                        ap,
                        title = f"✅ IN STOCK: {target.name}",
                        body  = (
                            f"**{target.name}** is now available!\n\n"
                            f"🌐 {target.url}\n"
                            f"📄 {_page_title(html, target.url)}\n"
                            f"🔍 {detail}\n"
                            f"⏰ {ts}\n\n"
                            f"👉 [Buy Now]({target.url})"
                        ),
                        notify_type = apprise.NotifyType.SUCCESS,
                    )
                else:
                    log.debug(f"[Target] Still in stock (already notified): {target.name}")
            else:
                # Reset dedup key so we re-alert next time it comes back in stock
                await db.delete_key(db_key)
                log.debug(f"[Target] Out of stock: {target.name} — {detail}")

    except Exception as exc:
        log.error(f"[Target] Error in {target.name}: {exc}", exc_info=True)

# ─── RSS Logic ────────────────────────────────────────────────────────────────

def _entry_matches(entry: dict, keywords: list[str]) -> bool:
    if not keywords:
        return True
    text = (entry.get("title", "") + " " + entry.get("summary", "")).lower()
    return any(kw in text for kw in keywords)

def _strip_html(raw: str, limit: int = 300) -> str:
    soup = BeautifulSoup(raw, "lxml")
    return soup.get_text(separator=" ")[:limit].strip()

async def check_feed(
    feed: FeedConfig,
    db: Database,
    ap: apprise.Apprise,
):
    log.info(f"[RSS] Checking: {feed.name}")
    try:
        loop   = asyncio.get_running_loop()
        parsed = await loop.run_in_executor(None, lambda: feedparser.parse(feed.url))
    except Exception as exc:
        log.error(f"[RSS] Parse error {feed.name}: {exc}")
        return

    if parsed.bozo and not parsed.entries:
        log.warning(f"[RSS] Malformed feed {feed.name}: {parsed.bozo_exception}")
        return

    new_count = 0
    for entry in parsed.entries[:20]:
        guid = entry.get("id") or entry.get("link") or entry.get("title", "")
        if not guid:
            continue

        if not _entry_matches(entry, feed.keywords):
            log.debug(f"[RSS] Keyword miss: {entry.get('title','')[:60]}")
            continue

        db_key = f"rss:{feed.url}:{hashlib.md5(guid.encode()).hexdigest()}"
        is_new = await db.check_and_mark(db_key)
        if not is_new:
            continue

        new_count += 1
        title   = entry.get("title", "No title")
        link    = entry.get("link", "")
        summary = _strip_html(entry.get("summary", ""))
        pub     = (entry.get("published") or entry.get("updated") or "")[:25]
        kw_line = f"🏷 `{', '.join(feed.keywords)}`\n" if feed.keywords else ""

        await notify(
            ap,
            title = f"📡 [{feed.name}] {title[:60]}",
            body  = (
                f"**{title}**\n\n"
                f"{kw_line}"
                f"📅 {pub}\n"
                f"📝 {summary}\n\n"
                f"🔗 [Read more]({link})"
            ),
            notify_type = apprise.NotifyType.INFO,
        )
        await asyncio.sleep(1.5)   # respect Telegram 30 msg/s limit

    if new_count:
        log.info(f"[RSS] {new_count} new alerts sent for: {feed.name}")
    else:
        log.debug(f"[RSS] No new items for: {feed.name}")

# ─── Heartbeat ────────────────────────────────────────────────────────────────

async def maybe_send_heartbeat(cfg: AppConfig, db: Database, ap: apprise.Apprise):
    last = await db.get_last_heartbeat()
    if last and (datetime.utcnow() - last) < timedelta(hours=cfg.glob.heartbeat_interval_hours):
        return
    await db.update_heartbeat()
    await notify(
        ap,
        title = "💓 Worker Heartbeat",
        body  = (
            f"Worker is alive.\n\n"
            f"🖥 Targets: {sum(1 for t in cfg.targets if t.enabled)}\n"
            f"📡 RSS feeds: {sum(1 for f in cfg.feeds if f.enabled)}\n"
            f"⏱ Interval: {cfg.glob.check_interval_minutes} min\n"
            f"♻️ Session max age: {SESSION_MAX_AGE_H}h\n"
            f"⏰ {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
        ),
    )
    log.info("[Heartbeat] Sent")

# ─── Jitter helper (defined once at module level, not re-created per loop) ────

async def _run_jittered(coro, delay: float):
    await asyncio.sleep(delay)
    await coro

# ─── Main Scheduler ───────────────────────────────────────────────────────────

class Worker:
    """
    Main control loop.  Key design:

    - SessionManager handles HTTP session lifecycle with age-based + error-based renewal.
      The session is created ONCE on start and lives OUTSIDE the while loop,
      but SessionManager transparently recreates it every SESSION_MAX_AGE_H hours
      and immediately on transport errors.

    - Database wraps SQLite with asyncio.Lock for atomic check-then-write
      to prevent duplicate notifications under concurrent coroutines.

    - Apprise object is built once per config load (not every 60-second loop tick).

    - asyncio.Semaphore caps concurrent HTTP requests at MAX_CONCURRENT.
    """

    def __init__(self):
        self.cfg: Optional[AppConfig]     = None
        self.db:  Optional[Database]      = None
        self._sm  = SessionManager()
        self._ap: Optional[apprise.Apprise] = None
        self._running    = True
        self._next_check: dict[str, float] = {}
        self._sem = asyncio.Semaphore(MAX_CONCURRENT)
        self._last_cfg_mtime: float = 0.0

    # ── Config hot-reload ─────────────────────────────────────────────────────

    def _load_or_reload(self) -> bool:
        if not CONFIG_PATH.exists():
            log.error(f"Config not found: {CONFIG_PATH}")
            return False
        mtime = CONFIG_PATH.stat().st_mtime
        if self.cfg and mtime == self._last_cfg_mtime:
            return True
        try:
            self.cfg              = load_config(CONFIG_PATH)
            self._last_cfg_mtime  = mtime
            self._ap              = build_apprise(self.cfg.glob.apprise_urls)
            return True
        except Exception as exc:
            log.error(f"Config reload failed: {exc}")
            return self.cfg is not None

    # ── Per-item scheduling ───────────────────────────────────────────────────

    def _is_due(self, key: str, interval_min: int) -> bool:
        now = time.monotonic()
        if now >= self._next_check.get(key, 0):
            self._next_check[key] = now + interval_min * 60
            return True
        return False

    # ── Semaphore-wrapped task runners ────────────────────────────────────────

    async def _run_target(self, target: TargetConfig):
        async with self._sem:
            await check_target(target, self.cfg, self._sm, self.db, self._ap)

    async def _run_feed(self, feed: FeedConfig):
        async with self._sem:
            await check_feed(feed, self.db, self._ap)

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self):
        if PERSIST_STATE and DB_PATH:
            self.db = Database(DB_PATH)
        else:
            if PERSIST_STATE and not DB_PATH:
                log.warning("PERSIST_STATE=true set but DB_PATH is empty; using in-memory state instead.")
            self.db = MemoryState()

        if not self._load_or_reload():
            log.critical("Cannot start without config.yaml — exiting.")
            sys.exit(1)

        log.info(f"Worker started (session max age: {SESSION_MAX_AGE_H}h, "
                 f"concurrency: {MAX_CONCURRENT}). Ctrl+C / SIGTERM to stop.")

        # SessionManager lives here — OUTSIDE the while loop.
        # It transparently handles recreation; the loop simply calls sm.get().
        while self._running:
            # Hot-reload config if file changed (also rebuilds Apprise if changed)
            self._load_or_reload()

            await maybe_send_heartbeat(self.cfg, self.db, self._ap)

            tasks = []

            for target in self.cfg.targets:
                if not target.enabled:
                    continue
                interval = (target.check_interval_minutes
                            or self.cfg.glob.check_interval_minutes)
                if self._is_due(f"target:{target.url}", interval):
                    tasks.append(self._run_target(target))

            for feed in self.cfg.feeds:
                if not feed.enabled:
                    continue
                interval = (feed.check_interval_minutes
                            or self.cfg.glob.check_interval_minutes)
                if self._is_due(f"feed:{feed.url}", interval):
                    tasks.append(self._run_feed(feed))

            if tasks:
                n          = len(tasks)
                max_jitter = min(n * 0.8, 10.0)
                # Spread task starts over [0, max_jitter] seconds
                jitter_coros = [
                    _run_jittered(t, random.uniform(0, max_jitter * i / max(n - 1, 1)))
                    for i, t in enumerate(tasks)
                ]
                await asyncio.gather(*jitter_coros, return_exceptions=True)

            log.debug("Loop done — sleeping 60s")
            await asyncio.sleep(60)

    async def stop(self):
        log.info("Shutdown signal received.")
        self._running = False
        await self._sm.close()
        if self.db:
            self.db.close()

# ─── Entry Point ──────────────────────────────────────────────────────────────

async def main():
    worker = Worker()
    loop   = asyncio.get_running_loop()

    import signal

    def _on_signal():
        asyncio.create_task(worker.stop())
        for task in asyncio.all_tasks(loop):
            if task != asyncio.current_task():
                task.cancel()

    loop.add_signal_handler(signal.SIGINT,  _on_signal)
    loop.add_signal_handler(signal.SIGTERM, _on_signal)

    try:
        await worker.run()
    except asyncio.CancelledError:
        log.info("Worker cancelled — goodbye.")

if __name__ == "__main__":
    asyncio.run(main())
