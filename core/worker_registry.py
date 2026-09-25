"""
AENIDA Worker Registry
Auto-discovery and health tracking for 2-40 worker nodes.
Nodes self-register on startup; dead nodes retired automatically.
"""

import sqlite3
import time
import json
import logging
import os
from typing import Dict, Any, List, Optional

DB_DEFAULT = "data/worker_registry.db"


class WorkerRegistry:
    """
    Tracks all worker nodes with health scoring 0-100.

    Node lifecycle (BUG-25 fix):
      Not seen 5 min  → DEGRADED + WARNING alert
      Not seen 7 days → RETIRED  (excluded from routing)
      Not seen 30 days→ ARCHIVED (cold storage)
    """

    __slots__ = ["db_path", "_conn"]

    RETIRE_DAYS = 7
    ARCHIVE_DAYS = 30
    HEARTBEAT_TIMEOUT_MIN = 5

    def __init__(self, db_path: str = DB_DEFAULT):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True) if os.path.dirname(db_path) else None
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_db()

    def _init_db(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS workers (
                id            TEXT PRIMARY KEY,
                tailscale_ip  TEXT NOT NULL,
                grpc_port     INTEGER DEFAULT 50051,
                hostname      TEXT,
                capabilities  TEXT DEFAULT '{}',
                health_score  REAL DEFAULT 100.0,
                last_seen     REAL,
                status        TEXT DEFAULT 'ACTIVE',
                registered_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_status ON workers(status);
            CREATE INDEX IF NOT EXISTS idx_last_seen ON workers(last_seen);
        """)
        self._conn.commit()

    def register_worker(self, info: Dict[str, Any]) -> str:
        """
        Register a new worker or update existing one.

        Args:
            info: dict with keys: id, tailscale_ip, grpc_port,
                  hostname, capabilities (dict)
        Returns:
            worker id
        """
        worker_id = info.get("id") or info.get("tailscale_ip", "unknown")
        now = time.time()
        caps = json.dumps(info.get("capabilities", {}))

        self._conn.execute("""
            INSERT INTO workers (id, tailscale_ip, grpc_port, hostname,
                                 capabilities, last_seen, status, registered_at)
            VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE', ?)
            ON CONFLICT(id) DO UPDATE SET
                tailscale_ip=excluded.tailscale_ip,
                grpc_port=excluded.grpc_port,
                hostname=excluded.hostname,
                capabilities=excluded.capabilities,
                last_seen=excluded.last_seen,
                status='ACTIVE'
        """, (worker_id, info.get("tailscale_ip", ""),
              info.get("grpc_port", 50051), info.get("hostname", ""),
              caps, now, now))
        self._conn.commit()
        logging.info(f"[REGISTRY] Worker registered: {worker_id}")
        return worker_id

    def update_heartbeat(self, worker_id: str) -> None:
        """Update last_seen timestamp for worker."""
        self._conn.execute(
            "UPDATE workers SET last_seen=?, status='ACTIVE' WHERE id=?",
            (time.time(), worker_id)
        )
        self._conn.commit()

    def update_health_score(self, worker_id: str, success: bool) -> float:
        """
        Update health score based on task outcome.
        Success +0.5 (max 100), Failure -2.0 (floor 0).
        Returns new score.
        """
        delta = 0.5 if success else -2.0
        self._conn.execute("""
            UPDATE workers
            SET health_score = MAX(0, MIN(100, health_score + ?))
            WHERE id = ?
        """, (delta, worker_id))
        self._conn.commit()

        cursor = self._conn.execute(
            "SELECT health_score FROM workers WHERE id=?", (worker_id,)
        )
        row = cursor.fetchone()
        return row[0] if row else 0.0

    def get_active_workers(self) -> List[Dict[str, Any]]:
        """Return all ACTIVE workers."""
        cursor = self._conn.execute("""
            SELECT id, tailscale_ip, grpc_port, hostname,
                   capabilities, health_score, last_seen, status
            FROM workers
            WHERE status = 'ACTIVE'
            ORDER BY health_score DESC
        """)
        return [self._row_to_dict(r) for r in cursor.fetchall()]

    def get_best_worker_for_task(self, task_type: str = "general") -> Optional[Dict[str, Any]]:
        """
        Return the highest-health ACTIVE worker.
        Returns None if no workers available.
        """
        self.retire_dead_workers()
        workers = self.get_active_workers()
        if not workers:
            return None
        # Filter by recent heartbeat (within 5 min)
        cutoff = time.time() - (self.HEARTBEAT_TIMEOUT_MIN * 60)
        live = [w for w in workers if (w.get("last_seen") or 0) > cutoff]
        return live[0] if live else None

    def retire_dead_workers(self) -> int:
        """
        BUG-25 fix: retire workers based on last_seen.
        Returns count of retired workers.
        """
        now = time.time()
        degraded_cutoff = now - (self.HEARTBEAT_TIMEOUT_MIN * 60)
        retire_cutoff = now - (self.RETIRE_DAYS * 86400)
        archive_cutoff = now - (self.ARCHIVE_DAYS * 86400)

        # DEGRADED
        self._conn.execute("""
            UPDATE workers SET status='DEGRADED'
            WHERE status='ACTIVE' AND last_seen < ?
        """, (degraded_cutoff,))

        # RETIRED
        cur = self._conn.execute("""
            UPDATE workers SET status='RETIRED'
            WHERE status IN ('ACTIVE','DEGRADED') AND last_seen < ?
        """, (retire_cutoff,))
        retired = cur.rowcount

        # ARCHIVED
        self._conn.execute("""
            UPDATE workers SET status='ARCHIVED'
            WHERE status='RETIRED' AND last_seen < ?
        """, (archive_cutoff,))

        self._conn.commit()
        if retired:
            logging.warning(f"[REGISTRY] Retired {retired} dead workers")
        return retired

    def get_all_workers(self) -> List[Dict[str, Any]]:
        """Return all workers regardless of status."""
        cursor = self._conn.execute("""
            SELECT id, tailscale_ip, grpc_port, hostname,
                   capabilities, health_score, last_seen, status
            FROM workers ORDER BY health_score DESC
        """)
        return [self._row_to_dict(r) for r in cursor.fetchall()]

    def _row_to_dict(self, row: tuple) -> Dict[str, Any]:
        return {
            "id": row[0], "tailscale_ip": row[1],
            "grpc_port": row[2], "hostname": row[3],
            "capabilities": json.loads(row[4] or "{}"),
            "health_score": row[5], "last_seen": row[6], "status": row[7]
        }

    def get_stats(self) -> Dict[str, Any]:
        cursor = self._conn.execute("""
            SELECT status, COUNT(*) FROM workers GROUP BY status
        """)
        return dict(cursor.fetchall())


# ── Global instance ──────────────────────────────────────────────
_registry: Optional[WorkerRegistry] = None


def get_registry(db_path: str = DB_DEFAULT) -> WorkerRegistry:
    global _registry
    if _registry is None:
        _registry = WorkerRegistry(db_path)
    return _registry


def register_worker(info: Dict[str, Any]) -> str:
    return get_registry().register_worker(info)


def update_heartbeat(worker_id: str) -> None:
    get_registry().update_heartbeat(worker_id)


def update_health_score(worker_id: str, success: bool) -> float:
    return get_registry().update_health_score(worker_id, success)


def get_active_workers() -> List[Dict[str, Any]]:
    return get_registry().get_active_workers()


def get_best_worker_for_task(task_type: str = "general") -> Optional[Dict[str, Any]]:
    return get_registry().get_best_worker_for_task(task_type)


def retire_dead_workers() -> int:
    return get_registry().retire_dead_workers()


def get_stats() -> Dict[str, Any]:
    return get_registry().get_stats()
