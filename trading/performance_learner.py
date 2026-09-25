"""
AENIDA Performance Learner
Node scoring via Weighted Moving Average, failure fingerprinting,
E_score baseline auto-calibration (BUG-11 fix).
Node expiry system (BUG-25 fix).
"""

import json
import logging
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional

try:
    import numpy as np
    NUMPY_OK = True
except ImportError:
    NUMPY_OK = False

DB_DEFAULT = "data/performance.db"


class _Observation:
    """Single task observation. __slots__ keeps RAM low."""
    __slots__ = ["node_id", "success", "latency_ms",
                 "ram_free_mb", "hour_of_day", "timestamp"]

    def __init__(self, node_id: str, success: bool,
                 latency_ms: float = 0.0, ram_free_mb: float = 0.0):
        self.node_id = node_id
        self.success = success
        self.latency_ms = latency_ms
        self.ram_free_mb = ram_free_mb
        self.hour_of_day = time.localtime().tm_hour
        self.timestamp = time.time()


class PerformanceLearner:
    """
    Learns from task outcomes to improve routing decisions.
    Stores ONE float per node (WMA) instead of 10 000 raw rows.
    """

    __slots__ = ["db_path", "_conn", "alpha", "fingerprint_threshold"]

    def __init__(self, db_path: str = DB_DEFAULT,
                 wma_alpha: float = 0.3,
                 fingerprint_threshold: float = 0.90):
        os.makedirs(os.path.dirname(db_path), exist_ok=True) \
            if os.path.dirname(db_path) else None
        self.db_path = db_path
        self.alpha = wma_alpha
        self.fingerprint_threshold = fingerprint_threshold
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_db()

    def _init_db(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS node_scores (
                node_id    TEXT PRIMARY KEY,
                mu         REAL DEFAULT 1.0,
                updated_at REAL
            );
            CREATE TABLE IF NOT EXISTS fingerprints (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id    TEXT,
                latency_ms REAL,
                ram_free_mb REAL,
                hour_of_day INTEGER,
                timestamp  REAL,
                weight     REAL DEFAULT 1.0
            );
            CREATE TABLE IF NOT EXISTS escore_baselines (
                id             INTEGER PRIMARY KEY,
                local_tps      REAL DEFAULT 50.0,
                remote_tps     REAL DEFAULT 800.0,
                updated_at     REAL,
                sample_count   INTEGER DEFAULT 0
            );
        """)
        # Ensure one baseline row exists
        self._conn.execute("""
            INSERT OR IGNORE INTO escore_baselines
            (id, local_tps, remote_tps, updated_at, sample_count)
            VALUES (1, 50.0, 800.0, ?, 0)
        """, (time.time(),))
        self._conn.commit()

    # ── WMA node scoring ──────────────────────────────────────────
    def update_node_score(self, node_id: str, success: bool) -> float:
        """
        μ_new = μ_old × (1 - α) + (success × α)
        Returns new score.
        """
        cursor = self._conn.execute(
            "SELECT mu FROM node_scores WHERE node_id=?", (node_id,))
        row = cursor.fetchone()
        mu_old = row[0] if row else 1.0
        mu_new = mu_old * (1 - self.alpha) + (1.0 if success else 0.0) * self.alpha

        self._conn.execute("""
            INSERT INTO node_scores (node_id, mu, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(node_id) DO UPDATE SET mu=excluded.mu,
                                               updated_at=excluded.updated_at
        """, (node_id, mu_new, time.time()))
        self._conn.commit()
        return mu_new

    def get_node_score(self, node_id: str) -> float:
        cursor = self._conn.execute(
            "SELECT mu FROM node_scores WHERE node_id=?", (node_id,))
        row = cursor.fetchone()
        return row[0] if row else 1.0

    def get_all_scores(self) -> Dict[str, float]:
        cursor = self._conn.execute("SELECT node_id, mu FROM node_scores")
        return {r[0]: r[1] for r in cursor.fetchall()}

    # ── Failure fingerprinting ────────────────────────────────────
    def record_failure_fingerprint(self, obs) -> None:
        """Record failure context. Accepts dict or _Observation."""
        if isinstance(obs, dict):
            node_id  = obs.get("node_id", "unknown")
            latency  = float(obs.get("latency_ms", 0))
            ram_free = float(obs.get("ram_free_mb", 0))
            hour     = float(time.localtime().tm_hour)
            ts       = float(obs.get("timestamp", time.time()))
        else:
            node_id  = obs.node_id
            latency  = obs.latency_ms
            ram_free = obs.ram_free_mb
            hour     = float(obs.hour_of_day)
            ts       = obs.timestamp
        self._conn.execute("""
            INSERT INTO fingerprints
            (node_id, latency_ms, ram_free_mb, hour_of_day, timestamp)
            VALUES (?, ?, ?, ?, ?)
        """, (node_id, latency, ram_free, hour, ts))
        self._conn.commit()

    def check_fingerprint_match(self, node_id: str,
                                 latency_ms: float,
                                 ram_free_mb: float) -> bool:
        """
        Returns True (issue pre-emptive veto) if current environment
        matches a known failure fingerprint > threshold.
        """
        cursor = self._conn.execute("""
            SELECT latency_ms, ram_free_mb, hour_of_day
            FROM fingerprints
            WHERE node_id=? AND weight > 0.1
            ORDER BY timestamp DESC LIMIT 50
        """, (node_id,))
        rows = cursor.fetchall()
        if not rows:
            return False

        hour = time.localtime().tm_hour
        current = [latency_ms / 1000.0, ram_free_mb / 1000.0, hour / 24.0]

        if NUMPY_OK:
            current_arr = np.array(current)
            for row in rows:
                fp = np.array([row[0] / 1000.0, row[1] / 1000.0,
                                row[2] / 24.0])
                # Cosine similarity
                denom = np.linalg.norm(current_arr) * np.linalg.norm(fp)
                if denom == 0:
                    continue
                sim = float(np.dot(current_arr, fp) / denom)
                if sim > self.fingerprint_threshold:
                    return True
        else:
            # Fallback: simple range check
            for row in rows:
                lat_match = abs(latency_ms - row[0]) < 200
                ram_match = abs(ram_free_mb - row[1]) < 500
                if lat_match and ram_match:
                    return True
        return False

    def record_observation(self, node_id: str, success: bool,
                            latency_ms: float = 0.0,
                            ram_free_mb: float = 0.0) -> None:
        """Convenience: update score + record fingerprint on failure."""
        self.update_node_score(node_id, success)
        if not success:
            obs = _Observation(node_id, success, latency_ms, ram_free_mb)
            self.record_failure_fingerprint(obs)

    # ── E_score baseline calibration (BUG-11) ────────────────────
    def update_escore_baselines(self, local_tps: float,
                                 remote_tps: float) -> None:
        """
        Auto-calibrate T_local and T_remote baselines.
        Called by mother_router after every 10 tasks.
        """
        self._conn.execute("""
            UPDATE escore_baselines
            SET local_tps=?, remote_tps=?, updated_at=?,
                sample_count=sample_count+1
            WHERE id=1
        """, (max(1.0, local_tps), max(1.0, remote_tps), time.time()))
        self._conn.commit()

        # Propagate to mother_router
        try:
            from mother_router import update_escore_baselines
            update_escore_baselines(local_tps, remote_tps)
        except Exception:
            pass

        logging.info(
            f"[PERF] E_score baselines updated: "
            f"local={local_tps:.0f} remote={remote_tps:.0f} tok/s"
        )

    def get_escore_baselines(self) -> Dict[str, float]:
        cursor = self._conn.execute(
            "SELECT local_tps, remote_tps FROM escore_baselines WHERE id=1")
        row = cursor.fetchone()
        return {"local_tps": row[0], "remote_tps": row[1]} if row else \
               {"local_tps": 50.0, "remote_tps": 800.0}

    # ── Trend matrix (for nightly_synthesizer) ────────────────────
    def get_trend_matrix(self):
        """Return node score matrix for nightly synthesis."""
        scores = self.get_all_scores()
        if not scores:
            if NUMPY_OK:
                return np.zeros((1, 1))
            return [[0.0]]
        vals = list(scores.values())
        if NUMPY_OK:
            return np.array(vals).reshape(-1, 1)
        return [[v] for v in vals]

    # ── Node expiry (BUG-25) ──────────────────────────────────────
    def retire_dead_nodes(self) -> int:
        """Delegate to worker_registry for lifecycle management."""
        try:
            from worker_registry import retire_dead_workers
            return retire_dead_workers()
        except Exception:
            return 0

    # ── Decay for fingerprints ────────────────────────────────────
    def apply_decay(self, lambda_val: float = 0.1) -> int:
        """Apply Ebbinghaus decay to fingerprints. Prune if weight < 0.1."""
        now = time.time()
        cursor = self._conn.execute(
            "SELECT id, timestamp, weight FROM fingerprints")
        rows = cursor.fetchall()
        pruned = 0
        for fid, ts, w in rows:
            t_hours = (now - ts) / 3600.0
            import math
            w_new = w * math.exp(-lambda_val * t_hours)
            if w_new < 0.1:
                self._conn.execute(
                    "DELETE FROM fingerprints WHERE id=?", (fid,))
                pruned += 1
            else:
                self._conn.execute(
                    "UPDATE fingerprints SET weight=? WHERE id=?",
                    (w_new, fid))
        self._conn.commit()
        return pruned

    def get_stats(self) -> Dict[str, Any]:
        cursor = self._conn.execute(
            "SELECT COUNT(*) FROM fingerprints")
        fp_count = cursor.fetchone()[0]
        cursor2 = self._conn.execute(
            "SELECT COUNT(*) FROM node_scores")
        node_count = cursor2.fetchone()[0]
        baselines = self.get_escore_baselines()
        return {
            "node_count": node_count,
            "fingerprint_count": fp_count,
            "escore_baselines": baselines,
        }


# ── Global instance ───────────────────────────────────────────────
_learner: Optional[PerformanceLearner] = None


def get_learner() -> PerformanceLearner:
    global _learner
    if _learner is None:
        try:
            import config
            db = config.get("paths.performance_db", DB_DEFAULT)
            alpha = config.get("performance_learner.wma_alpha", 0.3)
            thresh = config.get(
                "performance_learner.fingerprint_match_threshold", 0.90)
        except Exception:
            db, alpha, thresh = DB_DEFAULT, 0.3, 0.90
        _learner = PerformanceLearner(db, alpha, thresh)
    return _learner


def update_node_score(node_id: str, success: bool) -> float:
    return get_learner().update_node_score(node_id, success)


def get_node_score(node_id: str) -> float:
    return get_learner().get_node_score(node_id)


def record_observation(node_id: str, success: bool,
                        latency_ms: float = 0.0,
                        ram_free_mb: float = 0.0) -> None:
    get_learner().record_observation(node_id, success, latency_ms, ram_free_mb)


def check_fingerprint_match(node_id: str, latency_ms: float,
                             ram_free_mb: float) -> bool:
    return get_learner().check_fingerprint_match(
        node_id, latency_ms, ram_free_mb)


def update_escore_baselines(local_tps: float, remote_tps: float) -> None:
    get_learner().update_escore_baselines(local_tps, remote_tps)


def get_trend_matrix():
    return get_learner().get_trend_matrix()


def retire_dead_nodes() -> int:
    return get_learner().retire_dead_nodes()


def apply_decay(lambda_val: float = 0.1) -> int:
    return get_learner().apply_decay(lambda_val)


def get_stats() -> Dict[str, Any]:
    return get_learner().get_stats()
