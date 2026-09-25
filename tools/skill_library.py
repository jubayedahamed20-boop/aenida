"""
AENIDA Skill Library
Hermes-inspired skill learning.
FTS5 full-text search, conflict detection (BUG-22), skill evolution.
15 pre-built trading skills included.
"""

import json
import logging
import os
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional

DB_DEFAULT = "data/skills.db"
SKILLS_DIR = "skills"

BUILT_IN_SKILLS = [
    {"task_type": "technical_analysis",
     "prompt": "Run all 15 technical indicators on this data and return a trading signal",
     "confidence": 0.85, "description": "Full TA indicator suite"},
    {"task_type": "crypto_momentum",
     "prompt": "RSI + MACD + Volume momentum strategy signal for {symbol}",
     "confidence": 0.80, "description": "Momentum strategy"},
    {"task_type": "mean_reversion",
     "prompt": "Bollinger Bands + RSI oversold/overbought mean reversion signal",
     "confidence": 0.78, "description": "Mean reversion signal"},
    {"task_type": "news_sentiment_trade",
     "prompt": "Combine news sentiment with price action for trade signal",
     "confidence": 0.72, "description": "News + price signal"},
    {"task_type": "btc_market_structure",
     "prompt": "Analyse BTC support/resistance and market structure",
     "confidence": 0.82, "description": "Market structure analysis"},
    {"task_type": "funding_rate_signal",
     "prompt": "OKX funding rate extreme signal — contrarian",
     "confidence": 0.75, "description": "Funding rate contrarian"},
    {"task_type": "vwap_intraday",
     "prompt": "VWAP deviation intraday trading signal",
     "confidence": 0.77, "description": "VWAP intraday"},
    {"task_type": "volume_breakout",
     "prompt": "Volume spike + price breakout detection",
     "confidence": 0.79, "description": "Volume breakout"},
    {"task_type": "ichimoku_cloud",
     "prompt": "Ichimoku cloud breakout and TK cross signal",
     "confidence": 0.76, "description": "Ichimoku signal"},
    {"task_type": "macd_divergence",
     "prompt": "MACD hidden/regular divergence detection and signal",
     "confidence": 0.74, "description": "MACD divergence"},
    {"task_type": "rsi_divergence",
     "prompt": "RSI divergence from price — reversal signal",
     "confidence": 0.73, "description": "RSI divergence"},
    {"task_type": "multi_timeframe",
     "prompt": "Align 1H + 4H + 1D timeframes before generating signal",
     "confidence": 0.83, "description": "Multi-timeframe analysis"},
    {"task_type": "portfolio_check",
     "prompt": "Check open positions before new signal — max 3 concurrent",
     "confidence": 0.90, "description": "Position check"},
    {"task_type": "pre_market_scan",
     "prompt": "Morning scan: overnight price + news for {symbol}",
     "confidence": 0.81, "description": "Pre-market scan"},
    {"task_type": "eod_summary",
     "prompt": "End of day: all positions P&L and journal export",
     "confidence": 0.88, "description": "EOD summary"},
]


class SkillLibrary:
    """
    Hermes-inspired skill system.
    Stores skills in SQLite with FTS5 for fast search.
    """

    __slots__ = ["db_path", "_conn"]

    def __init__(self, db_path: str = DB_DEFAULT):
        os.makedirs(os.path.dirname(db_path), exist_ok=True) \
            if os.path.dirname(db_path) else None
        os.makedirs(SKILLS_DIR, exist_ok=True)
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_db()
        self._seed_built_in()

    def _init_db(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS skills (
                skill_id      TEXT PRIMARY KEY,
                task_type     TEXT NOT NULL,
                version       INTEGER DEFAULT 1,
                prompt        TEXT NOT NULL,
                confidence    REAL DEFAULT 0.5,
                success_count INTEGER DEFAULT 0,
                fail_count    INTEGER DEFAULT 0,
                avg_speed_ms  REAL DEFAULT 0.0,
                created_at    REAL NOT NULL,
                last_used     REAL,
                archived      INTEGER DEFAULT 0,
                description   TEXT
            );
            CREATE TABLE IF NOT EXISTS skill_usage (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                skill_id  TEXT,
                success   INTEGER,
                speed_ms  REAL,
                ts        REAL
            );
        """)
        # FTS5 virtual table for fast search
        self._conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS skills_fts
            USING fts5(skill_id, task_type, prompt, description,
                       content=skills, content_rowid=rowid)
        """)
        self._conn.commit()

    def _seed_built_in(self) -> None:
        """Insert built-in skills if not present."""
        for s in BUILT_IN_SKILLS:
            cursor = self._conn.execute(
                "SELECT 1 FROM skills WHERE task_type=? AND archived=0",
                (s["task_type"],))
            if not cursor.fetchone():
                self._insert_skill(
                    task_type=s["task_type"],
                    prompt=s["prompt"],
                    confidence=s["confidence"],
                    description=s.get("description", ""),
                    version=1
                )

    def _insert_skill(self, task_type: str, prompt: str,
                       confidence: float, description: str,
                       version: int = 1) -> str:
        skill_id = f"{task_type}_v{version}_{uuid.uuid4().hex[:6]}"
        now = time.time()
        self._conn.execute("""
            INSERT INTO skills
            (skill_id, task_type, version, prompt, confidence,
             created_at, description)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (skill_id, task_type, version, prompt,
              confidence, now, description))
        self._conn.commit()
        # Update FTS
        self._conn.execute("""
            INSERT INTO skills_fts(rowid, skill_id, task_type, prompt, description)
            SELECT rowid, skill_id, task_type, prompt, description
            FROM skills WHERE skill_id=?
        """, (skill_id,))
        self._conn.commit()
        return skill_id

    # ── Conflict detection (BUG-22) ───────────────────────────────
    def check_duplicate(self, task_type: str) -> Optional[Dict]:
        """Return existing active skill for task_type or None."""
        cursor = self._conn.execute("""
            SELECT skill_id, confidence, version FROM skills
            WHERE task_type=? AND archived=0
            ORDER BY confidence DESC, version DESC LIMIT 1
        """, (task_type,))
        row = cursor.fetchone()
        if row:
            return {"skill_id": row[0], "confidence": row[1],
                    "version": row[2]}
        return None

    # ── Create skill ──────────────────────────────────────────────
    def create_skill(self, task: str, result: str,
                      duration_ms: float) -> str:
        """
        Create a new skill from a successful task.
        BUG-22: check duplicate first — keep higher confidence.
        """
        # Estimate confidence from duration (faster = more confident)
        confidence = max(0.50, min(0.99, 1.0 - duration_ms / 10000.0))

        existing = self.check_duplicate(task)
        if existing:
            if confidence <= existing["confidence"]:
                logging.debug(f"[SKILLS] Duplicate {task} — existing wins")
                return existing["skill_id"]
            # New one is better — archive old
            self._conn.execute(
                "UPDATE skills SET archived=1 WHERE skill_id=?",
                (existing["skill_id"],))
            self._conn.commit()
            version = existing["version"] + 1
        else:
            version = 1

        skill_id = self._insert_skill(
            task_type=task,
            prompt=f"Task: {task}\nExpected result: {result[:200]}",
            confidence=confidence,
            description=f"Auto-created from successful task",
            version=version
        )
        logging.info(f"[SKILLS] Created skill: {task} v{version} ({confidence:.0%})")
        return skill_id

    # ── Get skill ─────────────────────────────────────────────────
    def get_skill(self, task_type: str) -> Optional[Dict]:
        """Return best active skill for task_type."""
        cursor = self._conn.execute("""
            SELECT skill_id, task_type, version, prompt, confidence,
                   success_count, avg_speed_ms, description
            FROM skills
            WHERE task_type=? AND archived=0
            ORDER BY confidence DESC, version DESC LIMIT 1
        """, (task_type,))
        row = cursor.fetchone()
        if not row:
            return None
        cols = ["skill_id", "task_type", "version", "prompt",
                "confidence", "success_count", "avg_speed_ms", "description"]
        return dict(zip(cols, row))

    # ── FTS5 search (faster than cosine for exact recall) ─────────
    def search_skills(self, query: str, limit: int = 10) -> List[Dict]:
        """FTS5 full-text search across all skills."""
        try:
            cursor = self._conn.execute("""
                SELECT s.skill_id, s.task_type, s.confidence,
                       s.description, s.version
                FROM skills s
                JOIN skills_fts fts ON s.rowid = fts.rowid
                WHERE skills_fts MATCH ? AND s.archived=0
                ORDER BY s.confidence DESC
                LIMIT ?
            """, (query, limit))
            cols = ["skill_id", "task_type", "confidence",
                    "description", "version"]
            return [dict(zip(cols, r)) for r in cursor.fetchall()]
        except Exception:
            # FTS5 not available — fallback LIKE search
            cursor = self._conn.execute("""
                SELECT skill_id, task_type, confidence,
                       description, version
                FROM skills
                WHERE (task_type LIKE ? OR description LIKE ?)
                  AND archived=0
                ORDER BY confidence DESC LIMIT ?
            """, (f"%{query}%", f"%{query}%", limit))
            cols = ["skill_id", "task_type", "confidence",
                    "description", "version"]
            return [dict(zip(cols, r)) for r in cursor.fetchall()]

    # ── Record usage ──────────────────────────────────────────────
    def record_usage(self, skill_id: str,
                      success: bool, speed_ms: float = 0.0) -> None:
        now = time.time()
        self._conn.execute("""
            INSERT INTO skill_usage (skill_id, success, speed_ms, ts)
            VALUES (?, ?, ?, ?)
        """, (skill_id, 1 if success else 0, speed_ms, now))

        if success:
            self._conn.execute("""
                UPDATE skills
                SET success_count = success_count + 1,
                    last_used = ?,
                    avg_speed_ms = (avg_speed_ms * success_count + ?) / (success_count + 1)
                WHERE skill_id = ?
            """, (now, speed_ms, skill_id))
        else:
            self._conn.execute("""
                UPDATE skills SET fail_count = fail_count + 1,
                                  last_used = ?
                WHERE skill_id=?
            """, (now, skill_id))
        self._conn.commit()

        # Check if evolution threshold reached (10 successes)
        cursor = self._conn.execute(
            "SELECT success_count FROM skills WHERE skill_id=?", (skill_id,))
        row = cursor.fetchone()
        if row and row[0] > 0 and row[0] % 10 == 0:
            self._trigger_evolution(skill_id)

    # ── Skill evolution ───────────────────────────────────────────
    def _trigger_evolution(self, skill_id: str) -> None:
        """After 10 successful uses, request Worker AI to improve skill."""
        cursor = self._conn.execute(
            "SELECT task_type, prompt, success_count, confidence FROM skills WHERE skill_id=?",
            (skill_id,))
        row = cursor.fetchone()
        if not row:
            return
        task_type, prompt, count, conf = row
        logging.info(
            f"[SKILLS] Evolution triggered for {task_type} "
            f"(used {count} times, conf={conf:.0%})"
        )
        try:
            from model_shell import call
            improved_prompt = call(
                f"This skill has run {count} times successfully.\n"
                f"Task type: {task_type}\n"
                f"Current prompt: {prompt}\n\n"
                f"Write an improved prompt for this skill that would "
                f"produce more accurate results. Return only the new prompt."
            ).get("result", "")
            if improved_prompt and len(improved_prompt) > 20:
                version_cursor = self._conn.execute(
                    "SELECT MAX(version) FROM skills WHERE task_type=?",
                    (task_type,))
                max_v = version_cursor.fetchone()[0] or 0
                self._conn.execute(
                    "UPDATE skills SET archived=1 WHERE skill_id=?",
                    (skill_id,))
                self._insert_skill(
                    task_type=task_type,
                    prompt=improved_prompt,
                    confidence=min(0.99, conf + 0.05),
                    description=f"Evolved from v{max_v} after {count} uses",
                    version=max_v + 1
                )
                logging.info(f"[SKILLS] Evolved {task_type} to v{max_v+1}")
        except Exception as e:
            logging.warning(f"[SKILLS] Evolution failed: {e}")

    def evolve_skill(self, skill_id: str) -> bool:
        """Manually trigger evolution for a skill."""
        self._trigger_evolution(skill_id)
        return True

    def get_skill_stats(self) -> Dict[str, Any]:
        cursor = self._conn.execute("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN archived=0 THEN 1 ELSE 0 END) as active,
                   AVG(confidence) as avg_conf,
                   SUM(success_count) as total_uses
            FROM skills
        """)
        row = cursor.fetchone()
        return {
            "total": row[0], "active": row[1],
            "avg_confidence": round(row[2] or 0, 3),
            "total_uses": row[3] or 0,
        }

    def list_all(self) -> List[Dict]:
        cursor = self._conn.execute("""
            SELECT skill_id, task_type, version, confidence,
                   success_count, description, archived
            FROM skills ORDER BY task_type, version DESC
        """)
        cols = ["skill_id", "task_type", "version", "confidence",
                "success_count", "description", "archived"]
        return [dict(zip(cols, r)) for r in cursor.fetchall()]


# ── Global instance ───────────────────────────────────────────────
_library: Optional[SkillLibrary] = None


def get_library() -> SkillLibrary:
    global _library
    if _library is None:
        try:
            import config
            db = config.get("paths.skills_db", DB_DEFAULT)
        except Exception:
            db = DB_DEFAULT
        _library = SkillLibrary(db)
    return _library


def create_skill(task: str, result: str, duration_ms: float) -> str:
    return get_library().create_skill(task, result, duration_ms)


def get_skill(task_type: str) -> Optional[Dict]:
    return get_library().get_skill(task_type)


def check_duplicate(task_type: str) -> Optional[Dict]:
    return get_library().check_duplicate(task_type)


def search_skills(query: str, limit: int = 10) -> List[Dict]:
    return get_library().search_skills(query, limit)


def record_usage(skill_id: str, success: bool,
                  speed_ms: float = 0.0) -> None:
    get_library().record_usage(skill_id, success, speed_ms)


def evolve_skill(skill_id: str) -> bool:
    return get_library().evolve_skill(skill_id)


def get_skill_stats() -> Dict[str, Any]:
    return get_library().get_skill_stats()
