"""
AENIDA News Watcher
Crypto/market news from CoinGecko + CryptoCompare (both free).
Rate-limited, deduplicated, keyword-alerting.
"""

import json
import logging
import os
import sqlite3
import time
import threading
from typing import Dict, Any, List, Optional

try:
    import requests
    REQUESTS_OK = True
except ImportError:
    REQUESTS_OK = False

DB_DEFAULT = "data/news_cache.db"
DEFAULT_KEYWORDS = [
    "hack", "crash", "ban", "SEC", "regulation", "pump", "dump",
    "rug", "liquidation", "exploit", "scam", "arrest", "shutdown",
    "war", "sanction",
]
DEFAULT_WATCHLIST = ["BTC", "ETH", "BNB"]


class NewsWatcher:
    """Polls news, deduplicates, alerts on keywords."""

    __slots__ = ["db_path", "_conn", "watchlist", "alert_keywords",
                 "poll_interval_min", "_running", "_thread", "_lock",
                 "_alert_callback"]  # callback wired by orchestrator

    def __init__(self,
                 db_path: str = DB_DEFAULT,
                 watchlist: Optional[List[str]] = None,
                 alert_keywords: Optional[List[str]] = None,
                 poll_interval_minutes: int = 30):
        os.makedirs(os.path.dirname(db_path), exist_ok=True) if os.path.dirname(db_path) else None
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self.watchlist = watchlist or DEFAULT_WATCHLIST
        self.alert_keywords = [k.lower() for k in (alert_keywords or DEFAULT_KEYWORDS)]
        self.poll_interval_min = poll_interval_minutes
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._alert_callback = None   # set by orchestrator.wire_modules()
        self._init_db()

    def _init_db(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS news (
                id          TEXT PRIMARY KEY,
                source      TEXT,
                title       TEXT NOT NULL,
                url         TEXT,
                published_at REAL,
                coins       TEXT,
                alerted     INTEGER DEFAULT 0,
                cached_at   REAL NOT NULL
            )
        """)
        self._conn.commit()

    # ── Fetching ──────────────────────────────────────────────────
    def fetch_coingecko_news(self) -> List[Dict[str, Any]]:
        """CoinGecko /news — free, no API key."""
        if not REQUESTS_OK:
            return []
        try:
            resp = requests.get(
                "https://api.coingecko.com/api/v3/news",
                timeout=10
            )
            if resp.status_code == 200:
                data = resp.json()
                items = data if isinstance(data, list) else data.get("data", [])
                return [
                    {
                        "id": str(item.get("id", "")),
                        "source": "coingecko",
                        "title": item.get("title", ""),
                        "url": item.get("url", ""),
                        "published_at": item.get("updated_at", time.time()),
                    }
                    for item in items[:20]
                ]
        except Exception as e:
            logging.warning(f"[NEWS] CoinGecko fetch failed: {e}")
        return []

    def fetch_cryptocompare_news(self) -> List[Dict[str, Any]]:
        """CryptoCompare news — free tier."""
        if not REQUESTS_OK:
            return []
        try:
            api_key = os.environ.get("CRYPTOCOMPARE_API_KEY", "")
            params = {"lang": "EN"}
            if api_key:
                params["api_key"] = api_key
            resp = requests.get(
                "https://min-api.cryptocompare.com/data/v2/news/",
                params=params, timeout=10
            )
            if resp.status_code == 200:
                items = resp.json().get("Data", [])
                return [
                    {
                        "id": f"cc_{item.get('id', '')}",
                        "source": "cryptocompare",
                        "title": item.get("title", ""),
                        "url": item.get("url", ""),
                        "published_at": float(item.get("published_on", time.time())),
                    }
                    for item in items[:20]
                ]
        except Exception as e:
            logging.warning(f"[NEWS] CryptoCompare fetch failed: {e}")
        return []

    # ── Processing ────────────────────────────────────────────────
    def filter_by_watchlist(self, news: List[Dict]) -> List[Dict]:
        """Keep only news mentioning watchlist coins."""
        filtered = []
        for item in news:
            title_upper = item.get("title", "").upper()
            coins = [c for c in self.watchlist if c in title_upper]
            if coins:
                item["coins"] = ",".join(coins)
                filtered.append(item)
            else:
                # Keep anyway — macro news affects all coins
                item["coins"] = ""
                filtered.append(item)
        return filtered

    def deduplicate(self, news: List[Dict]) -> List[Dict]:
        """Remove items already in cache."""
        new_items = []
        for item in news:
            nid = item.get("id", "")
            cursor = self._conn.execute(
                "SELECT 1 FROM news WHERE id=?", (nid,))
            if not cursor.fetchone():
                new_items.append(item)
        return new_items

    def cache_items(self, news: List[Dict]) -> None:
        """Save new items to cache, enforce 100-item limit."""
        now = time.time()
        for item in news:
            try:
                self._conn.execute("""
                    INSERT OR IGNORE INTO news
                    (id, source, title, url, published_at, coins, cached_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (item.get("id", ""), item.get("source", ""),
                      item.get("title", ""), item.get("url", ""),
                      item.get("published_at", now),
                      item.get("coins", ""), now))
            except Exception:
                pass
        self._conn.commit()

        # Enforce 100-item limit
        cursor = self._conn.execute("SELECT COUNT(*) FROM news")
        count = cursor.fetchone()[0]
        if count > 100:
            self._conn.execute("""
                DELETE FROM news WHERE id IN (
                    SELECT id FROM news ORDER BY cached_at ASC
                    LIMIT ?
                )
            """, (count - 100,))
            self._conn.commit()

        # Delete items older than 7 days
        self._conn.execute(
            "DELETE FROM news WHERE cached_at < ?",
            (now - 7 * 86400,))
        self._conn.commit()

    def check_alert_keywords(self, news: List[Dict]) -> List[str]:
        """Return list of titles that triggered alert keywords."""
        triggered = []
        for item in news:
            title_lower = item.get("title", "").lower()
            for kw in self.alert_keywords:
                if kw in title_lower:
                    triggered.append(item.get("title", ""))
                    break
        return triggered

    def summarize_news(self, headlines: List[str]) -> str:
        """Summarize headlines via AI."""
        if not headlines:
            return "No new headlines."
        joined = "\n".join(f"- {h}" for h in headlines[:10])
        prompt = (
            "Summarize these crypto headlines in 3 bullet points. "
            "Be direct. Flag if any is critical market-moving news.\n"
            f"{joined}"
        )
        try:
            from fallback_chain import call_ai
            result = call_ai(prompt)
            return result.get("result", joined)
        except Exception:
            return joined

    # ── Main poll cycle ───────────────────────────────────────────
    def poll_once(self) -> Dict[str, Any]:
        """Run a single poll cycle."""
        all_news: List[Dict] = []
        all_news.extend(self.fetch_coingecko_news())
        all_news.extend(self.fetch_cryptocompare_news())

        if not all_news:
            return {"new": 0, "alerts": [], "summary": "News unavailable"}

        # Sanitize all titles
        try:
            from sanitizer_shield import scrub
            for item in all_news:
                item["title"] = scrub(item.get("title", ""))
        except Exception:
            pass

        filtered = self.filter_by_watchlist(all_news)
        new_items = self.deduplicate(filtered)
        self.cache_items(new_items)

        alerts = self.check_alert_keywords(new_items)
        headlines = [i.get("title", "") for i in new_items]
        summary = self.summarize_news(headlines) if headlines else "No new crypto news."

        # Fire alerts
        if alerts:
            try:
                from alert_manager import send, AlertLevel
                for title in alerts[:3]:
                    send(AlertLevel.WARNING, "🚨 Crypto News Alert", title)
            except Exception as e:
                logging.warning(f"[NEWS] Alert send failed: {e}")

        return {"new": len(new_items), "alerts": alerts, "summary": summary}

    def start(self) -> None:
        """Start background polling thread."""
        self._running = True
        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="news_watcher")
        self._thread.start()
        logging.info(f"[NEWS] Polling every {self.poll_interval_min} min")

    def stop(self) -> None:
        self._running = False

    def _poll_loop(self) -> None:
        while self._running:
            try:
                result = self.poll_once()
                alerts = result.get("alerts", [])
                logging.info(
                    f"[NEWS] Cycle complete: {result['new']} new, "
                    f"{len(alerts)} alerts"
                )
                # Fire registered callback (wired by orchestrator)
                if alerts and self._alert_callback is not None:
                    try:
                        self._alert_callback(alerts)
                    except Exception as cb_err:
                        logging.warning(f"[NEWS] alert_callback error: {cb_err}")
            except Exception as e:
                logging.error(f"[NEWS] Poll cycle failed: {e}")
            # Sleep in small increments so stop() works quickly
            for _ in range(self.poll_interval_min * 60):
                if not self._running:
                    break
                time.sleep(1)

    def add_coin(self, symbol: str) -> None:
        symbol = symbol.upper()
        if symbol not in self.watchlist:
            self.watchlist.append(symbol)
            logging.info(f"[NEWS] Added coin: {symbol}")

    def remove_coin(self, symbol: str) -> None:
        symbol = symbol.upper()
        if symbol in self.watchlist:
            self.watchlist.remove(symbol)
            logging.info(f"[NEWS] Removed coin: {symbol}")

    def get_recent(self, limit: int = 20) -> List[Dict]:
        cursor = self._conn.execute("""
            SELECT id, source, title, url, published_at, coins, alerted
            FROM news ORDER BY published_at DESC LIMIT ?
        """, (limit,))
        cols = ["id", "source", "title", "url", "published_at", "coins", "alerted"]
        return [dict(zip(cols, r)) for r in cursor.fetchall()]


# ── Global instance ───────────────────────────────────────────────
_watcher: Optional[NewsWatcher] = None


def get_watcher() -> NewsWatcher:
    global _watcher
    if _watcher is None:
        _watcher = NewsWatcher()
    return _watcher


def start() -> None:
    get_watcher().start()


def stop() -> None:
    get_watcher().stop()


def poll_once() -> Dict[str, Any]:
    return get_watcher().poll_once()


def add_coin(symbol: str) -> None:
    get_watcher().add_coin(symbol)


def remove_coin(symbol: str) -> None:
    get_watcher().remove_coin(symbol)


def get_recent(limit: int = 20) -> List[Dict]:
    return get_watcher().get_recent(limit)
