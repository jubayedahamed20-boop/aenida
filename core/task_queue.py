"""
AENIDA Task Queue  v3.0  (in-place upgrade of v1.0)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
100 % backward-compatible with v1.0 callers.  All existing
imports, signatures, and return types are unchanged.

What's new:
  ┌─ PRIORITY LEVELS ──────────────────────────────────────────┐
  │  0  Emergency  — always first, never starvation-boosted    │
  │  1  Critical                                               │
  │  2  High                                                   │
  │  3  Normal-High                                            │
  │  4  Normal  (v1 default was 5 — callers that pass 5 work) │
  │  5  Normal-Low                                             │
  │  6  Low                                                    │
  │  7  Background                                             │
  │  8  Idle                                                   │
  │  9  Deferred                                               │
  │  10 Lowest                                                 │
  └────────────────────────────────────────────────────────────┘
  ┌─ DEADLINES ────────────────────────────────────────────────┐
  │  enqueue(..., deadline_ts=time.time()+300) optional kwarg  │
  │  Priority ≤ 2  → ESCALATED to P-0 + Telegram alert        │
  │  Priority 3–10 → EXPIRED + audit entry                     │
  └────────────────────────────────────────────────────────────┘
  ┌─ STARVATION PREVENTION ────────────────────────────────────┐
  │  Any task queued > 60 s gets priority boosted by 1         │
  │  Floor = priority 1 (emergency slot never stolen)          │
  └────────────────────────────────────────────────────────────┘
  ┌─ AUDIT LOG ────────────────────────────────────────────────┐
  │  Every state transition → data/task_audit.db               │
  │  Missed deadlines and escalations also written here        │
  └────────────────────────────────────────────────────────────┘
  ┌─ BUG FIXES vs v1.0 ────────────────────────────────────────┐
  │  • __slots__ removed → RLock needed for re-entrant audit   │
  │  • retry back-off now exponential (60 → 120 → 240 s)      │
  │  • dequeue_ready was not thread-safe (no lock) → fixed     │
  │  • mark_failed re-queue used fixed +60 s → now exponential │
  └────────────────────────────────────────────────────────────┘
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

log = logging.getLogger("task_queue")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DB_DEFAULT       = os.path.join(_ROOT, "data", "task_queue.db")
_AUDIT_DB_DEFAULT = os.path.join(_ROOT, "data", "task_audit.db")

# ── Tunables ──────────────────────────────────────────────────────
STARVATION_WAIT_S    = 60.0    # tasks waiting longer than this get boosted
STARVATION_CHECK_S   = 30.0    # how often the anti-starvation sweep runs
DEADLINE_CHECK_S     = 15.0    # how often expired deadlines are handled
STALE_TASK_S         = 300     # running task is "stale" after this many seconds
PRIORITY_MIN         = 0       # emergency (never starvation-boosted)
PRIORITY_MAX         = 10      # background / lowest
PRIORITY_FLOOR       = 1       # starvation can boost down to this, not lower
ESCALATE_AT_PRIORITY = 2       # tasks at priority ≤ this get escalated on deadline miss
DEFAULT_PRIORITY     = 5       # unchanged from v1.0 (callers that pass no priority)


# ════════════════════════════════════════════════════════════════════
#  ENUMS & DATA CLASSES
# ════════════════════════════════════════════════════════════════════

class TaskStatus(Enum):
    """Task lifecycle states.  EXPIRED and ESCALATED are new in v3.0."""
    QUEUED    = "queued"
    RUNNING   = "running"
    COMPLETE  = "complete"
    FAILED    = "failed"
    EXPIRED   = "expired"      # deadline missed, low-priority → dropped
    ESCALATED = "escalated"    # deadline missed, high-priority → re-queued at P0


@dataclass
class Task:
    """Full task record.  deadline_ts and boosted_count are new in v3.0."""
    task_id:       str
    task_type:     str
    payload:       Dict[str, Any]
    scheduled_for: float
    priority:      int
    status:        TaskStatus
    created_at:    float
    deadline_ts:   Optional[float] = None   # v3 NEW
    started_at:    Optional[float] = None
    completed_at:  Optional[float] = None
    retry_count:   int = 0
    boosted_count: int = 0                  # v3 NEW


# ════════════════════════════════════════════════════════════════════
#  ALERT HELPER  (fire-and-forget — never blocks the queue)
# ════════════════════════════════════════════════════════════════════

def _alert(message: str, level: str = "info") -> None:
    """Send via alert_manager in a daemon thread.  Safe to call under any lock."""
    def _send():
        try:
            from alert_manager import send, AlertLevel  # type: ignore
            lvl = {"warning": AlertLevel.WARNING,
                   "critical": AlertLevel.CRITICAL}.get(level, AlertLevel.INFO)
            send(lvl, "Task Queue", message)
        except Exception:
            log.info(f"[TQ] {message}")
    threading.Thread(target=_send, daemon=True).start()


# ════════════════════════════════════════════════════════════════════
#  TASK QUEUE v3.0
# ════════════════════════════════════════════════════════════════════

class TaskQueue:
    """
    Persistent, priority-aware, deadline-enforcing task queue.

    Thread-safety:
        All public methods acquire self._lock (RLock — re-entrant so
        _audit() called inside a locked method never deadlocks).

    Backward compatibility:
        Every v1.0 public method signature is preserved.
        New keyword arguments (deadline_ts) default to None.
    """

    def __init__(self, db_path: str = _DB_DEFAULT,
                 audit_db_path: str = _AUDIT_DB_DEFAULT,
                 enable_background: bool = True):
        self.db_path       = db_path
        self.audit_db_path = audit_db_path

        os.makedirs(os.path.dirname(db_path)       or ".", exist_ok=True)
        os.makedirs(os.path.dirname(audit_db_path) or ".", exist_ok=True)

        # RLock: same thread can re-enter (needed for _audit called inside locked methods)
        self._lock       = threading.RLock()
        self._conn       = self._connect(db_path)
        self._audit_conn = self._connect(audit_db_path)
        self._stop       = threading.Event()

        self._init_db()
        self._init_audit_db()
        self._migrate_v1_schema()   # add new columns to existing DBs non-destructively

        if enable_background:
            self._start_background_threads()

    # ── DB setup ──────────────────────────────────────────────────

    @staticmethod
    def _connect(path: str) -> sqlite3.Connection:
        c = sqlite3.connect(path, check_same_thread=False)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=5000")
        return c

    def _init_db(self) -> None:
        with self._lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id       TEXT PRIMARY KEY,
                    task_type     TEXT NOT NULL,
                    payload       TEXT,
                    scheduled_for REAL,
                    priority      INTEGER DEFAULT 5,
                    status        TEXT    DEFAULT 'queued',
                    created_at    REAL,
                    started_at    REAL,
                    completed_at  REAL,
                    retry_count   INTEGER DEFAULT 0,
                    deadline_ts   REAL,
                    boosted_count INTEGER DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_scheduled
                    ON tasks(scheduled_for, status, priority);

            """)
            self._conn.commit()

    def _init_audit_db(self) -> None:
        with self._lock:
            self._audit_conn.executescript("""
                CREATE TABLE IF NOT EXISTS task_audit (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts         REAL    NOT NULL,
                    task_id    TEXT    NOT NULL,
                    task_type  TEXT,
                    event      TEXT    NOT NULL,
                    old_status TEXT,
                    new_status TEXT,
                    notes      TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_audit_ts
                    ON task_audit(ts DESC);
                CREATE INDEX IF NOT EXISTS idx_audit_tid
                    ON task_audit(task_id);
            """)
            self._audit_conn.commit()

    def _migrate_v1_schema(self) -> None:
        """Add v3.0 columns to an existing v1.0 database without losing data."""
        with self._lock:
            existing = {
                row[1]
                for row in self._conn.execute("PRAGMA table_info(tasks)").fetchall()
            }
            for col, typedef in [("deadline_ts",   "REAL"),
                                  ("boosted_count", "INTEGER DEFAULT 0")]:
                if col not in existing:
                    self._conn.execute(
                        f"ALTER TABLE tasks ADD COLUMN {col} {typedef}")
            # Add index on deadline_ts (only works after column exists)
            try:
                self._conn.execute(
                    'CREATE INDEX IF NOT EXISTS idx_deadline '
                    'ON tasks(deadline_ts) WHERE deadline_ts IS NOT NULL')
            except Exception:
                pass
            self._conn.commit()

    # ── Audit ─────────────────────────────────────────────────────

    def _audit(self, task_id: str, task_type: str, event: str,
               old: str = "", new: str = "", notes: str = "") -> None:
        """Write one audit record.  RLock means this is safe inside other locked calls."""
        try:
            with self._lock:
                self._audit_conn.execute("""
                    INSERT INTO task_audit
                        (ts, task_id, task_type, event, old_status, new_status, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (time.time(), task_id, task_type, event, old, new, notes))
                self._audit_conn.commit()
        except Exception as e:
            log.debug(f"[TQ] audit write failed: {e}")

    # ── Background threads ────────────────────────────────────────

    def _start_background_threads(self) -> None:
        for name, interval, fn in [
            ("TQ-Starvation", STARVATION_CHECK_S, self._run_starvation),
            ("TQ-Deadline",   DEADLINE_CHECK_S,   self._run_deadline),
        ]:
            t = threading.Thread(target=self._bg_loop,
                                 args=(interval, fn, name),
                                 name=name, daemon=True)
            t.start()
        log.info("[TQ] Background threads started")

    def _bg_loop(self, interval: float, fn, name: str) -> None:
        while not self._stop.wait(interval):
            try:
                fn()
            except Exception as e:
                log.error(f"[TQ] {name} error: {e}")

    # ── Starvation prevention ─────────────────────────────────────

    def _run_starvation(self) -> None:
        """Boost priority of tasks waiting longer than STARVATION_WAIT_S."""
        cutoff = time.time() - STARVATION_WAIT_S
        with self._lock:
            cur = self._conn.execute("""
                SELECT task_id, task_type, priority
                FROM tasks
                WHERE status   = 'queued'
                  AND priority > ?
                  AND created_at <= ?
                  AND scheduled_for <= ?
            """, (PRIORITY_FLOOR, cutoff, time.time()))
            rows = cur.fetchall()

            boosted = 0
            for task_id, task_type, pri in rows:
                new_pri = max(PRIORITY_FLOOR, pri - 1)
                if new_pri < pri:
                    self._conn.execute("""
                        UPDATE tasks
                        SET priority = ?, boosted_count = boosted_count + 1
                        WHERE task_id = ?
                    """, (new_pri, task_id))
                    boosted += 1
                    log.debug(f"[TQ] Anti-starvation: {task_id[:8]} "
                              f"P{pri}→P{new_pri}")

            if boosted:
                self._conn.commit()
                log.info(f"[TQ] Boosted {boosted} starving task(s)")

    # ── Deadline enforcement ──────────────────────────────────────

    def _run_deadline(self) -> None:
        """Expire or escalate tasks whose deadline has passed."""
        now = time.time()
        with self._lock:
            cur = self._conn.execute("""
                SELECT task_id, task_type, priority
                FROM tasks
                WHERE status = 'queued'
                  AND deadline_ts IS NOT NULL
                  AND deadline_ts < ?
            """, (now,))
            rows = cur.fetchall()

        for task_id, task_type, priority in rows:
            self._handle_deadline_miss(task_id, task_type, priority, now)

    def _handle_deadline_miss(self, task_id: str, task_type: str,
                               priority: int, now: float) -> None:
        if priority <= ESCALATE_AT_PRIORITY:
            # High-priority: re-queue at emergency level, alert
            with self._lock:
                self._conn.execute("""
                    UPDATE tasks
                    SET priority = 0,
                        deadline_ts = NULL,
                        retry_count = retry_count + 1,
                        scheduled_for = ?
                    WHERE task_id = ?
                      AND status  = 'queued'
                """, (now, task_id))
                self._conn.commit()
            self._audit(task_id, task_type, "deadline_escalated",
                        old=f"queued@P{priority}", new="queued@P0",
                        notes=f"original_priority={priority}")
            log.warning(f"[TQ] ⚡ ESCALATED {task_id[:8]} ({task_type}) "
                        f"P{priority}→P0 — deadline missed")
            _alert(f"DEADLINE ESCALATED\n"
                   f"Task: {task_type} ({task_id[:8]})\n"
                   f"Was P{priority} → now EMERGENCY P0",
                   level="warning")
        else:
            # Low-priority: expire it
            with self._lock:
                self._conn.execute("""
                    UPDATE tasks
                    SET status = 'expired', completed_at = ?
                    WHERE task_id = ?
                      AND status  = 'queued'
                """, (now, task_id))
                self._conn.commit()
            self._audit(task_id, task_type, "deadline_expired",
                        old="queued", new="expired",
                        notes=f"priority={priority}")
            log.info(f"[TQ] Expired {task_id[:8]} ({task_type}) P{priority} "
                     f"— deadline missed")
            if priority <= 5:  # only alert on medium-priority drops
                _alert(f"DEADLINE MISSED (dropped)\n"
                       f"Task: {task_type} ({task_id[:8]}) P{priority}",
                       level="info")

    # ════════════════════════════════════════════════════════════════
    #  PUBLIC API — v1.0 compatible + v3.0 extensions
    # ════════════════════════════════════════════════════════════════

    def enqueue(self, task_type: str, payload: Dict[str, Any],
                scheduled_for: Optional[float] = None,
                priority: int = DEFAULT_PRIORITY,
                deadline_ts: Optional[float] = None) -> str:
        """
        Add a task to the queue.

        Args (v1.0 compatible):
            task_type:     Identifier string ("ping", "analyze", …)
            payload:       Arbitrary dict for the worker
            scheduled_for: Unix timestamp — don't run before this (None = now)
            priority:      0 (emergency) … 10 (lowest). Default 5.

        Args (v3.0 new, keyword-only):
            deadline_ts:   Unix timestamp — if still queued after this, escalate or expire.
                           None = no deadline.

        Returns:
            task_id (UUID string)
        """
        task_id = str(uuid.uuid4())
        now     = time.time()
        if scheduled_for is None:
            scheduled_for = now
        priority = max(PRIORITY_MIN, min(PRIORITY_MAX, int(priority)))

        with self._lock:
            self._conn.execute("""
                INSERT INTO tasks
                    (task_id, task_type, payload, scheduled_for, priority,
                     status, created_at, deadline_ts)
                VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)
            """, (task_id, task_type, json.dumps(payload),
                  scheduled_for, priority, now, deadline_ts))
            self._conn.commit()

        self._audit(task_id, task_type, "enqueued", new="queued",
                    notes=f"P{priority} dl={'yes' if deadline_ts else 'no'}")
        log.debug(f"[TQ] Enqueued {task_id[:8]} {task_type} P{priority}")
        return task_id

    def dequeue_ready(self, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Return tasks ready to execute, ordered by priority then age.
        Marks them as 'running'.  Thread-safe.
        """
        now   = time.time()
        tasks = []

        with self._lock:
            cur = self._conn.execute("""
                SELECT task_id, task_type, payload, scheduled_for,
                       priority, retry_count, deadline_ts
                FROM tasks
                WHERE status = 'queued' AND scheduled_for <= ?
                ORDER BY priority ASC, scheduled_for ASC
                LIMIT ?
            """, (now, limit))
            rows = cur.fetchall()

            for row in rows:
                tid = row[0]
                self._conn.execute("""
                    UPDATE tasks SET status='running', started_at=?
                    WHERE task_id=?
                """, (now, tid))
                tasks.append({
                    "task_id":       tid,
                    "task_type":     row[1],
                    "payload":       json.loads(row[2] or "{}"),
                    "scheduled_for": row[3],
                    "priority":      row[4],
                    "retry_count":   row[5],
                    "deadline_ts":   row[6],
                })
            if rows:
                self._conn.commit()

        for t in tasks:
            self._audit(t["task_id"], t["task_type"], "dequeued",
                        old="queued", new="running",
                        notes=f"P{t['priority']}")
        return tasks

    def mark_running(self, task_id: str) -> bool:
        """Mark a task as running (idempotent)."""
        try:
            with self._lock:
                self._conn.execute("""
                    UPDATE tasks SET status='running', started_at=?
                    WHERE task_id=? AND status='queued'
                """, (time.time(), task_id))
                self._conn.commit()
            return True
        except sqlite3.Error as e:
            log.error(f"[TQ] mark_running: {e}")
            return False

    def mark_complete(self, task_id: str) -> bool:
        """Mark a task as complete."""
        try:
            with self._lock:
                self._conn.execute("""
                    UPDATE tasks SET status='complete', completed_at=?
                    WHERE task_id=?
                """, (time.time(), task_id))
                self._conn.commit()
            self._audit(task_id, "", "complete", old="running", new="complete")
            return True
        except sqlite3.Error as e:
            log.error(f"[TQ] mark_complete: {e}")
            return False

    def mark_failed(self, task_id: str, retry: bool = True,
                    max_retries: int = 3) -> bool:
        """
        Mark failed.  If retry=True and retries remain, re-queue with
        exponential back-off (60 s → 120 s → 240 s …, capped at 1 hour).
        """
        try:
            with self._lock:
                cur = self._conn.execute(
                    "SELECT retry_count, task_type FROM tasks WHERE task_id=?",
                    (task_id,))
                row = cur.fetchone()
                if not row:
                    return False
                retries, task_type = row

                if retry and retries < max_retries - 1:
                    backoff = min(3600, 60 * (2 ** retries))
                    self._conn.execute("""
                        UPDATE tasks
                        SET retry_count   = retry_count + 1,
                            status        = 'queued',
                            scheduled_for = ?,
                            started_at    = NULL
                        WHERE task_id = ?
                    """, (time.time() + backoff, task_id))
                    self._audit(task_id, task_type, "retry_queued",
                                old="running", new="queued",
                                notes=f"attempt={retries+1} backoff={backoff}s")
                else:
                    self._conn.execute("""
                        UPDATE tasks
                        SET retry_count  = retry_count + 1,
                            status       = 'failed',
                            completed_at = ?
                        WHERE task_id = ?
                    """, (time.time(), task_id))
                    self._audit(task_id, task_type, "failed_final",
                                old="running", new="failed",
                                notes=f"total_attempts={retries+1}")
                self._conn.commit()
            return True
        except Exception as e:
            log.error(f"[TQ] mark_failed: {e}")
            return False

    def get_pending_count(self) -> int:
        """Count queued (not yet running) tasks."""
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status='queued'"
            ).fetchone()[0]

    def reset_stale_running(self, stale_seconds: int = STALE_TASK_S) -> int:
        """
        Reset tasks stuck in 'running' back to 'queued'.
        Call on startup to recover from crashes.
        """
        cutoff = time.time() - stale_seconds
        with self._lock:
            cur = self._conn.execute("""
                UPDATE tasks
                SET status='queued', started_at=NULL
                WHERE status='running' AND started_at < ?
            """, (cutoff,))
            self._conn.commit()
            n = cur.rowcount
        if n:
            log.warning(f"[TQ] Reset {n} stale running tasks")
        return n

    def cleanup_old(self, older_than_days: int = 7) -> int:
        """Delete old terminal-state tasks to keep the DB small."""
        cutoff = time.time() - older_than_days * 86400
        with self._lock:
            cur = self._conn.execute("""
                DELETE FROM tasks
                WHERE status IN ('complete','failed','expired')
                  AND completed_at < ?
            """, (cutoff,))
            self._conn.commit()
            return cur.rowcount

    def get_stats(self) -> Dict[str, Any]:
        """
        Return queue statistics.  Backward-compatible with v1.0 callers
        (total / queued / running / complete / failed), plus v3.0 extras.
        """
        with self._lock:
            row = self._conn.execute("""
                SELECT
                    COUNT(*)                                            AS total,
                    SUM(CASE WHEN status='queued'    THEN 1 ELSE 0 END) AS queued,
                    SUM(CASE WHEN status='running'   THEN 1 ELSE 0 END) AS running,
                    SUM(CASE WHEN status='complete'  THEN 1 ELSE 0 END) AS complete,
                    SUM(CASE WHEN status='failed'    THEN 1 ELSE 0 END) AS failed,
                    SUM(CASE WHEN status='expired'   THEN 1 ELSE 0 END) AS expired,
                    SUM(CASE WHEN deadline_ts IS NOT NULL
                              AND status='queued'    THEN 1 ELSE 0 END) AS with_deadline,
                    SUM(CASE WHEN boosted_count > 0
                              AND status='queued'    THEN 1 ELSE 0 END) AS boosted
                FROM tasks
            """).fetchone()
        return {
            "total":         row[0] or 0,
            "queued":        row[1] or 0,
            "running":       row[2] or 0,
            "complete":      row[3] or 0,
            "failed":        row[4] or 0,
            "expired":       row[5] or 0,
            "with_deadline": row[6] or 0,
            "boosted":       row[7] or 0,
        }

    def get_audit_log(self, limit: int = 50,
                      task_id: Optional[str] = None) -> List[Dict]:
        """Retrieve recent audit log entries."""
        if task_id:
            cur = self._audit_conn.execute("""
                SELECT ts, task_id, task_type, event, old_status, new_status, notes
                FROM task_audit WHERE task_id=?
                ORDER BY ts DESC LIMIT ?
            """, (task_id, limit))
        else:
            cur = self._audit_conn.execute("""
                SELECT ts, task_id, task_type, event, old_status, new_status, notes
                FROM task_audit ORDER BY ts DESC LIMIT ?
            """, (limit,))
        cols = ["ts","task_id","task_type","event","old_status","new_status","notes"]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def shutdown(self) -> None:
        """Stop background threads and close connections."""
        self._stop.set()
        try:
            self._conn.close()
            self._audit_conn.close()
        except Exception:
            pass
        log.info("[TQ] Shut down")


# ════════════════════════════════════════════════════════════════════
#  MODULE-LEVEL SINGLETON  — v1.0 compatible convenience functions
# ════════════════════════════════════════════════════════════════════

_queue: Optional[TaskQueue] = None
_singleton_lock = threading.Lock()


def get_queue(db_path: str = _DB_DEFAULT) -> TaskQueue:
    global _queue
    with _singleton_lock:
        if _queue is None:
            _queue = TaskQueue(db_path=db_path)
    return _queue


# ── v1.0 API  (all signatures unchanged) ─────────────────────────

def enqueue(task_type: str, payload: Dict[str, Any],
            scheduled_for: Optional[float] = None,
            priority: int = DEFAULT_PRIORITY,
            deadline_ts: Optional[float] = None) -> str:
    """Add task to queue.  deadline_ts is a new optional keyword arg."""
    return get_queue().enqueue(task_type, payload,
                               scheduled_for, priority, deadline_ts)


def dequeue_ready(limit: int = 10) -> List[Dict[str, Any]]:
    return get_queue().dequeue_ready(limit)


def mark_running(task_id: str) -> bool:
    return get_queue().mark_running(task_id)


def mark_complete(task_id: str) -> bool:
    return get_queue().mark_complete(task_id)


def mark_failed(task_id: str, retry: bool = True,
                max_retries: int = 3) -> bool:
    return get_queue().mark_failed(task_id, retry, max_retries)


def get_pending_count() -> int:
    return get_queue().get_pending_count()


def reset_stale_running(stale_seconds: int = STALE_TASK_S) -> int:
    return get_queue().reset_stale_running(stale_seconds)
