"""
AENIDA Persistence Layer
SQLite WAL ghost checkpoints for task_executor.
"""

import os
import sqlite3
import time
import json
from typing import Dict, Any, List, Optional
from dataclasses import dataclass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DB_DEFAULT = os.path.join(_ROOT, "data", "task_ledger.db")


@dataclass
class GhostEntry:
    """Ghost checkpoint entry."""
    task_id: str
    step_id: str
    step_index: int
    description: str
    payload: Dict[str, Any]
    created_at: float
    status: str = "pending"


class PersistenceLayer:
    """
    SQLite WAL ghost checkpoints for ARES task execution.
    Ensures tasks survive mid-execution interruption.
    """
    
    __slots__ = ['db_path', '_conn']
    
    def __init__(self, db_path: str = _DB_DEFAULT):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()
    
    def _init_db(self) -> None:
        """Initialize database with WAL mode."""
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        
        # Create ghost table
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS ghost_checkpoints (
                step_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                step_index INTEGER NOT NULL,
                description TEXT,
                payload TEXT,
                created_at REAL,
                status TEXT DEFAULT 'pending',
                completed_at REAL
            )
        """)
        
        # Create index for task lookups
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_task_id 
            ON ghost_checkpoints(task_id)
        """)
        
        self._conn.commit()
    
    def write_ghost(self, task_id: str, step_id: str, step_index: int,
                    description: str, payload: Dict[str, Any]) -> bool:
        """
        Write ghost checkpoint before step execution.
        
        Args:
            task_id: Parent task ID
            step_id: Unique step ID
            step_index: Step sequence number
            description: Step description
            payload: Step data
            
        Returns:
            True if written successfully
        """
        try:
            self._conn.execute("""
                INSERT OR REPLACE INTO ghost_checkpoints
                (step_id, task_id, step_index, description, payload, created_at, status)
                VALUES (?, ?, ?, ?, ?, ?, 'pending')
            """, (
                step_id, task_id, step_index, description,
                json.dumps(payload), time.time()
            ))
            self._conn.commit()
            return True
        except sqlite3.Error as e:
            # Retry up to 3 times
            for _ in range(2):
                try:
                    time.sleep(0.1)
                    self._conn.execute("""
                        INSERT OR REPLACE INTO ghost_checkpoints
                        (step_id, task_id, step_index, description, payload, created_at, status)
                        VALUES (?, ?, ?, ?, ?, ?, 'pending')
                    """, (
                        step_id, task_id, step_index, description,
                        json.dumps(payload), time.time()
                    ))
                    self._conn.commit()
                    return True
                except sqlite3.Error:
                    continue
            return False
    
    def mark_complete(self, step_id: str) -> bool:
        """Mark ghost checkpoint as complete."""
        try:
            self._conn.execute("""
                UPDATE ghost_checkpoints 
                SET status = 'complete', completed_at = ?
                WHERE step_id = ?
            """, (time.time(), step_id))
            self._conn.commit()
            return True
        except sqlite3.Error:
            return False
    
    def mark_failed(self, step_id: str, error: str) -> bool:
        """Mark ghost checkpoint as failed."""
        try:
            self._conn.execute("""
                UPDATE ghost_checkpoints 
                SET status = 'failed', completed_at = ?, description = description || ' | ERROR: ' || ?
                WHERE step_id = ?
            """, (time.time(), error, step_id))
            self._conn.commit()
            return True
        except sqlite3.Error:
            return False
    
    def get_pending_steps(self, task_id: str) -> List[Dict[str, Any]]:
        """Get all pending steps for a task."""
        cursor = self._conn.execute("""
            SELECT step_id, step_index, description, payload, created_at
            FROM ghost_checkpoints
            WHERE task_id = ? AND status = 'pending'
            ORDER BY step_index
        """, (task_id,))
        
        rows = cursor.fetchall()
        return [
            {
                "step_id": row[0],
                "step_index": row[1],
                "description": row[2],
                "payload": json.loads(row[3]),
                "created_at": row[4]
            }
            for row in rows
        ]
    
    def get_highest_common_step(self, task_id: str) -> Optional[str]:
        """Get highest completed step ID for task."""
        cursor = self._conn.execute("""
            SELECT step_id FROM ghost_checkpoints
            WHERE task_id = ? AND status = 'complete'
            ORDER BY step_index DESC
            LIMIT 1
        """, (task_id,))
        
        row = cursor.fetchone()
        return row[0] if row else None
    
    def cleanup_completed(self, older_than_hours: int = 24) -> int:
        """Clean up old completed checkpoints."""
        cutoff = time.time() - (older_than_hours * 3600)
        
        cursor = self._conn.execute("""
            DELETE FROM ghost_checkpoints
            WHERE status = 'complete' AND completed_at < ?
        """, (cutoff,))
        
        self._conn.commit()
        return cursor.rowcount
    
    def get_stats(self) -> Dict[str, Any]:
        """Get persistence layer statistics."""
        cursor = self._conn.execute("""
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending,
                SUM(CASE WHEN status = 'complete' THEN 1 ELSE 0 END) as complete,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed
            FROM ghost_checkpoints
        """)
        
        row = cursor.fetchone()
        return {
            "total": row[0],
            "pending": row[1],
            "complete": row[2],
            "failed": row[3]
        }


# Global instance
_layer: Optional[PersistenceLayer] = None


def get_layer() -> PersistenceLayer:
    """Get or create global persistence layer."""
    global _layer
    if _layer is None:
        _layer = PersistenceLayer()
    return _layer


def write_ghost(task_id: str, step_id: str, step_index: int,
                description: str, payload: Dict[str, Any]) -> bool:
    """Write ghost checkpoint."""
    return get_layer().write_ghost(task_id, step_id, step_index, description, payload)


def mark_complete(step_id: str) -> bool:
    """Mark step complete."""
    return get_layer().mark_complete(step_id)


def mark_failed(step_id: str, error: str) -> bool:
    """Mark step failed."""
    return get_layer().mark_failed(step_id, error)


def get_pending_steps(task_id: str) -> List[Dict[str, Any]]:
    """Get pending steps."""
    return get_layer().get_pending_steps(task_id)


def get_highest_common_step(task_id: str) -> Optional[str]:
    """Get highest completed step."""
    return get_layer().get_highest_common_step(task_id)


def cleanup_completed(older_than_hours: int = 24) -> int:
    """Clean up old checkpoints."""
    return get_layer().cleanup_completed(older_than_hours)
