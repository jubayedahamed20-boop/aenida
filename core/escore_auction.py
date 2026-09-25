"""
AENIDA E-Score Task Auction  v1.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Concept check & correction
──────────────────────────
Your original idea: "Mother broadcasts a task to ALL workers,
highest E-score wins."  That has three real problems:

  PROBLEM 1 — NAT / firewall
    Workers are on home PCs behind a router.  Mother cannot
    make direct HTTP calls TO workers.  The current design
    has workers PULL from Mother.  Reversing this would need
    port-forwarding on every worker PC — fragile and unsafe.

  PROBLEM 2 — Race condition
    If Mother broadcasts to 40 workers and they all respond,
    two workers may both believe they won before the lock
    resolves.  Results in duplicate work.

  PROBLEM 3 — "Decay 10% per assigned task" is permanent
    0.9 ^ (total tasks ever completed) → approaches 0 forever,
    even when the worker is now idle.  The worker never
    recovers its score no matter how free it becomes.

Corrected design: "Pull-with-Bid"
──────────────────────────────────
  1. Each worker sends its live E-score inside every heartbeat
     (workers already heartbeat to Mother every 30 s).
  2. Mother stores the latest E-score per worker in the registry.
  3. When a task needs assigning, Mother picks the worker with
     the highest current E-score — no broadcast, no race.
  4. The selected worker finds the task in its assigned queue
     (normal pull, no NAT traversal needed).
  5. E-score decays naturally: the decay factor is
     0.9 ^ active_task_count, where active_task_count = tasks
     currently running on that worker.  When they finish, the
     count drops to 0, the decay vanishes, and the worker is
     fully competitive again automatically.

E-Score formula
───────────────
  raw = (cpu_budget_pct / 100)        # 0.0–1.0 based on free CPU
      × ram_factor                     # free_ram_mb / total_ram_mb
      × power_weight                   # LOW=0.5  MEDIUM=1.0  HIGH=2.0
  escore = raw × (0.90 ^ active_tasks) # decay per running task

  Examples (HIGH class, idle):
    cpu_budget=80%, ram_factor=0.75, power=2.0, active=0
    → 0.80 × 0.75 × 2.0 × 0.9^0 = 1.20

  Same worker, 4 tasks running:
    → 0.80 × 0.75 × 2.0 × 0.9^4 = 0.787

  LOW class, heavy use, 2 tasks:
    cpu_budget=20%, ram_factor=0.40, power=0.5, active=2
    → 0.20 × 0.40 × 0.50 × 0.9^2 = 0.032  (almost never wins)

Usage
─────
  Mother PC:
      from escore_auction import EScoreAuction
      auction = EScoreAuction()
      best = auction.select_worker("compute")
      if best:
          auction.assign_task(best["node_id"], task_id)

  Worker PC (in smart_worker.py heartbeat):
      from escore_auction import compute_escore
      score = compute_escore(cpu_budget=sample.cpu_budget_pct,
                             free_ram_mb=sample.free_ram_mb,
                             total_ram_mb=sample.total_ram_mb,
                             power_class=profile.power_class.value,
                             active_tasks=manager.total_active())
      # include score in heartbeat payload
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import json
import logging
import math
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("escore_auction")

# ── Paths ──────────────────────────────────────────────────────────
_BASE    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH  = os.path.join(_BASE, "data", "escore_auction.db")

# ── Tuning constants ───────────────────────────────────────────────
POWER_WEIGHTS: Dict[str, float] = {
    "HIGH":    2.0,
    "MEDIUM":  1.0,
    "LOW":     0.5,
    "UNKNOWN": 0.7,
}
DECAY_PER_TASK   = 0.90   # E-score multiplied by this per active task
MIN_ESCORE       = 0.001  # workers below this are not eligible
SCORE_TTL_S      = 90.0   # ignore E-scores older than this (worker offline)
TASK_AFFINITY: Dict[str, List[str]] = {
    # task_type → preferred power classes (first = most preferred)
    "brain_query":      ["HIGH", "MEDIUM"],
    "model_inference":  ["HIGH", "MEDIUM"],
    "trading_analysis": ["HIGH", "MEDIUM", "LOW"],
    "backtest":         ["HIGH", "MEDIUM"],
    "ping":             ["LOW", "MEDIUM", "HIGH"],
    "sys_info":         ["LOW", "MEDIUM", "HIGH"],
    "fetch":            ["LOW", "MEDIUM", "HIGH"],
    "download":         ["LOW", "MEDIUM", "HIGH"],
}
# Affinity bonus: matching preferred class boosts score by this factor
AFFINITY_BONUS   = 1.25


# ════════════════════════════════════════════════════════════════════
#  E-SCORE FORMULA  (pure function — runs on every worker)
# ════════════════════════════════════════════════════════════════════

def compute_escore(cpu_budget_pct: float,
                   free_ram_mb: float,
                   total_ram_mb: float,
                   power_class: str = "MEDIUM",
                   active_tasks: int = 0) -> float:
    """
    Compute the current E-score for one worker.

    Called by the worker itself (in smart_worker.py) and embedded in
    each heartbeat so the Mother always has a fresh score.

    Args:
        cpu_budget_pct: AENIDA's CPU headroom in percent (0–100).
                        From ResourceMonitor.get_current().cpu_budget_pct
        free_ram_mb:    Available RAM in MB.
        total_ram_mb:   Total system RAM in MB (for normalisation).
        power_class:    "LOW" | "MEDIUM" | "HIGH" | "UNKNOWN"
        active_tasks:   Number of tasks currently running on this worker.

    Returns:
        E-score ≥ 0.0.  Typical range: 0.0 (fully loaded) to ~2.4 (HIGH idle).
    """
    cpu_factor   = max(0.0, min(1.0, cpu_budget_pct / 100.0))
    ram_factor   = max(0.0, min(1.0, free_ram_mb / max(1.0, total_ram_mb)))
    power_weight = POWER_WEIGHTS.get(power_class, POWER_WEIGHTS["UNKNOWN"])
    decay        = DECAY_PER_TASK ** max(0, active_tasks)

    raw    = cpu_factor * ram_factor * power_weight
    escore = raw * decay
    return round(max(0.0, escore), 6)


# ════════════════════════════════════════════════════════════════════
#  WORKER BID RECORD
# ════════════════════════════════════════════════════════════════════

@dataclass
class WorkerBid:
    """One worker's current capability snapshot."""
    node_id:      str
    hostname:     str
    power_class:  str
    escore:       float
    cpu_budget:   float
    free_ram_mb:  float
    active_tasks: int
    submitted_at: float = field(default_factory=time.time)

    @property
    def is_fresh(self) -> bool:
        return (time.time() - self.submitted_at) < SCORE_TTL_S

    @property
    def is_eligible(self) -> bool:
        return self.is_fresh and self.escore >= MIN_ESCORE


# ════════════════════════════════════════════════════════════════════
#  AUCTION STORE  (SQLite, runs on Mother)
# ════════════════════════════════════════════════════════════════════

class AuctionStore:
    """
    Persists latest E-score bids from all workers.
    One row per worker — UPSERT on every heartbeat.
    Also tracks assignment history for load analysis.
    """

    def __init__(self, db_path: str = DB_PATH):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=3000")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS worker_bids (
                    node_id      TEXT PRIMARY KEY,
                    hostname     TEXT,
                    power_class  TEXT DEFAULT 'UNKNOWN',
                    escore       REAL DEFAULT 0.0,
                    cpu_budget   REAL DEFAULT 0.0,
                    free_ram_mb  REAL DEFAULT 0.0,
                    active_tasks INTEGER DEFAULT 0,
                    submitted_at REAL NOT NULL,
                    total_wins   INTEGER DEFAULT 0,
                    total_tasks  INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS assignments (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts           REAL NOT NULL,
                    task_id      TEXT NOT NULL,
                    task_type    TEXT,
                    node_id      TEXT NOT NULL,
                    escore_at_win REAL,
                    rival_count  INTEGER DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_bids_escore
                    ON worker_bids(escore DESC);
                CREATE INDEX IF NOT EXISTS idx_assign_ts
                    ON assignments(ts DESC);
            """)
            self._conn.commit()

    def submit_bid(self, bid: WorkerBid) -> None:
        """Worker calls this via heartbeat — upserts its current state."""
        with self._lock:
            self._conn.execute("""
                INSERT INTO worker_bids
                    (node_id, hostname, power_class, escore, cpu_budget,
                     free_ram_mb, active_tasks, submitted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                    hostname     = excluded.hostname,
                    power_class  = excluded.power_class,
                    escore       = excluded.escore,
                    cpu_budget   = excluded.cpu_budget,
                    free_ram_mb  = excluded.free_ram_mb,
                    active_tasks = excluded.active_tasks,
                    submitted_at = excluded.submitted_at
            """, (bid.node_id, bid.hostname, bid.power_class,
                  bid.escore, bid.cpu_budget, bid.free_ram_mb,
                  bid.active_tasks, bid.submitted_at))
            self._conn.commit()

    def get_eligible_bids(self,
                          task_type: str = "general") -> List[WorkerBid]:
        """
        Return all fresh, eligible bids sorted by effective score descending.
        Applies affinity bonus for task_type-matched power classes.
        """
        now = time.time()
        with self._lock:
            cur = self._conn.execute("""
                SELECT node_id, hostname, power_class, escore,
                       cpu_budget, free_ram_mb, active_tasks, submitted_at
                FROM worker_bids
                WHERE submitted_at > ?
                  AND escore       > ?
                ORDER BY escore DESC
            """, (now - SCORE_TTL_S, MIN_ESCORE))
            rows = cur.fetchall()

        preferred = TASK_AFFINITY.get(task_type, [])
        bids: List[Tuple[float, WorkerBid]] = []

        for r in rows:
            bid = WorkerBid(
                node_id=r[0], hostname=r[1], power_class=r[2],
                escore=r[3], cpu_budget=r[4], free_ram_mb=r[5],
                active_tasks=r[6], submitted_at=r[7])

            # Apply affinity bonus
            effective = bid.escore
            if preferred:
                try:
                    rank = preferred.index(bid.power_class)
                    # First preferred class gets full bonus, rest get partial
                    bonus = AFFINITY_BONUS ** (1.0 / (rank + 1))
                    effective = bid.escore * bonus
                except ValueError:
                    pass  # power_class not in preferred list — no bonus

            bids.append((effective, bid))

        bids.sort(key=lambda x: x[0], reverse=True)
        return [b for _, b in bids]

    def record_assignment(self, task_id: str, task_type: str,
                          winner: WorkerBid, rival_count: int) -> None:
        """Log who won the auction and how many rivals there were."""
        with self._lock:
            self._conn.execute("""
                INSERT INTO assignments
                    (ts, task_id, task_type, node_id, escore_at_win, rival_count)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (time.time(), task_id, task_type,
                  winner.node_id, winner.escore, rival_count))
            self._conn.execute("""
                UPDATE worker_bids
                SET total_wins  = total_wins  + 1,
                    total_tasks = total_tasks + 1
                WHERE node_id = ?
            """, (winner.node_id,))
            self._conn.commit()

    def get_leaderboard(self) -> List[Dict[str, Any]]:
        """Return all workers sorted by total wins for dashboard display."""
        with self._lock:
            cur = self._conn.execute("""
                SELECT node_id, hostname, power_class, escore,
                       cpu_budget, free_ram_mb, active_tasks,
                       submitted_at, total_wins, total_tasks
                FROM worker_bids
                ORDER BY escore DESC
            """)
            cols = ["node_id","hostname","power_class","escore",
                    "cpu_budget","free_ram_mb","active_tasks",
                    "submitted_at","total_wins","total_tasks"]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            n_workers = self._conn.execute(
                "SELECT COUNT(*) FROM worker_bids").fetchone()[0]
            n_eligible = self._conn.execute(
                "SELECT COUNT(*) FROM worker_bids WHERE submitted_at > ? AND escore > ?",
                (time.time() - SCORE_TTL_S, MIN_ESCORE)).fetchone()[0]
            n_assign = self._conn.execute(
                "SELECT COUNT(*) FROM assignments").fetchone()[0]
        return {
            "total_workers":    n_workers,
            "eligible_workers": n_eligible,
            "total_assignments": n_assign,
        }


# ════════════════════════════════════════════════════════════════════
#  E-SCORE AUCTION  (Mother-side orchestrator)
# ════════════════════════════════════════════════════════════════════

class EScoreAuction:
    """
    Mother-side task auction engine.

    Typical integration in orchestrator.py:

        from escore_auction import EScoreAuction
        _auction = EScoreAuction()

        # In heartbeat handler (when worker POSTs its heartbeat):
        _auction.receive_bid(heartbeat_payload)

        # When assigning a task to a worker:
        winner = _auction.select_worker(task_type="brain_query")
        if winner:
            _auction.assign_task(winner.node_id, task_id, task_type)
            # Route task to winner.node_id via task queue
        else:
            # No eligible workers — keep task in queue
            pass
    """

    def __init__(self, db_path: str = DB_PATH):
        self._store = AuctionStore(db_path)

    # ── Called from heartbeat handler ─────────────────────────────

    def receive_bid(self, heartbeat: Dict[str, Any]) -> None:
        """
        Extract E-score from a worker heartbeat payload and store it.
        Call this every time a worker heartbeats to the Mother.

        Expected heartbeat keys (all optional with safe defaults):
            node_id, hostname, power_class, escore,
            cpu_budget_pct, free_ram_mb, active_tasks
        """
        node_id = heartbeat.get("node_id", "unknown")

        # Support two heartbeat formats:
        # (a) worker already computed escore — use it directly
        # (b) worker sent raw metrics — compute here
        if "escore" in heartbeat and heartbeat["escore"] is not None:
            escore = float(heartbeat["escore"])
        else:
            escore = compute_escore(
                cpu_budget_pct = float(heartbeat.get("cpu_budget_pct", 20.0)),
                free_ram_mb    = float(heartbeat.get("free_ram_mb",    512.0)),
                total_ram_mb   = float(heartbeat.get("total_ram_mb",  4096.0)),
                power_class    = str(heartbeat.get("power_class", "MEDIUM")),
                active_tasks   = int(heartbeat.get("active_tasks", 0)),
            )

        bid = WorkerBid(
            node_id      = node_id,
            hostname     = heartbeat.get("hostname", node_id),
            power_class  = heartbeat.get("power_class", "UNKNOWN"),
            escore       = escore,
            cpu_budget   = float(heartbeat.get("cpu_budget_pct", 0.0)),
            free_ram_mb  = float(heartbeat.get("free_ram_mb",    0.0)),
            active_tasks = int(heartbeat.get("active_tasks",     0)),
        )
        self._store.submit_bid(bid)
        log.debug(f"[AUCTION] Bid received: {node_id} "
                  f"escore={escore:.4f} active={bid.active_tasks}")

    # ── Called from task dispatcher ───────────────────────────────

    def select_worker(self, task_type: str = "general",
                      exclude: Optional[List[str]] = None
                      ) -> Optional[WorkerBid]:
        """
        Run the auction for one task.  Returns the winning WorkerBid,
        or None if no eligible workers exist.

        Args:
            task_type: Used for affinity scoring.
            exclude:   Node IDs to skip (e.g. workers that already failed).
        """
        exclude = set(exclude or [])
        bids    = self._store.get_eligible_bids(task_type)
        eligible = [b for b in bids if b.node_id not in exclude]

        if not eligible:
            log.info(f"[AUCTION] No eligible workers for '{task_type}'")
            return None

        winner = eligible[0]
        log.info(f"[AUCTION] Winner for '{task_type}': "
                 f"{winner.node_id} ({winner.power_class}) "
                 f"escore={winner.escore:.4f} "
                 f"rivals={len(eligible)-1}")
        return winner

    def assign_task(self, node_id: str, task_id: str,
                    task_type: str = "general") -> None:
        """
        Record the assignment in the DB after a worker wins.
        This is audit-only — actual task routing is via task_queue.
        """
        bids   = self._store.get_eligible_bids(task_type)
        winner = next((b for b in bids if b.node_id == node_id), None)
        rivals = len(bids) - 1

        if winner:
            self._store.record_assignment(task_id, task_type, winner, rivals)
        log.debug(f"[AUCTION] Assigned {task_id[:8]} → {node_id}")

    # ── Inspection ────────────────────────────────────────────────

    def leaderboard(self) -> List[Dict[str, Any]]:
        """Live E-score leaderboard for all workers (used by dashboard)."""
        return self._store.get_leaderboard()

    def stats(self) -> Dict[str, Any]:
        return self._store.get_stats()


# ════════════════════════════════════════════════════════════════════
#  HEARTBEAT PATCHER  — thin wrapper for smart_worker.py
# ════════════════════════════════════════════════════════════════════

def build_escore_heartbeat_payload(cpu_budget_pct: float,
                                   free_ram_mb:    float,
                                   total_ram_mb:   float,
                                   power_class:    str,
                                   active_tasks:   int,
                                   node_id:        str = "",
                                   hostname:       str = "") -> Dict[str, Any]:
    """
    Build the E-score section to embed in a smart_worker heartbeat payload.

    Usage in smart_worker.py's _main_loop:
        from escore_auction import build_escore_heartbeat_payload
        extra = {
            ...existing payload...,
            **build_escore_heartbeat_payload(
                cpu_budget_pct = sample.cpu_budget_pct,
                free_ram_mb    = sample.free_ram_mb,
                total_ram_mb   = sample.total_ram_mb,
                power_class    = profile.power_class.value,
                active_tasks   = manager.total_active(),
            )
        }
        conn.heartbeat(extra=extra)
    """
    escore = compute_escore(
        cpu_budget_pct = cpu_budget_pct,
        free_ram_mb    = free_ram_mb,
        total_ram_mb   = total_ram_mb,
        power_class    = power_class,
        active_tasks   = active_tasks,
    )
    return {
        "escore":        escore,
        "cpu_budget_pct":cpu_budget_pct,
        "free_ram_mb":   free_ram_mb,
        "total_ram_mb":  total_ram_mb,
        "power_class":   power_class,
        "active_tasks":  active_tasks,
    }


# ════════════════════════════════════════════════════════════════════
#  MODULE-LEVEL SINGLETON
# ════════════════════════════════════════════════════════════════════

_auction_instance: Optional[EScoreAuction] = None
_auction_lock = threading.Lock()


def get_auction(db_path: str = DB_PATH) -> EScoreAuction:
    """Get or create the module-level auction singleton."""
    global _auction_instance
    with _auction_lock:
        if _auction_instance is None:
            _auction_instance = EScoreAuction(db_path)
    return _auction_instance
