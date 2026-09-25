"""
AENIDA Trade Journal
Immutable SHA-256 hash-chained decision log.
BUG-36: Non-blocking approval system.
"""

import sqlite3
import hashlib
import time
import json
import os
import threading
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
from enum import Enum

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DB_DEFAULT = os.path.join(_ROOT, "data", "trade_journal.db")


class SignalStatus(Enum):
    """Signal status values."""
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass
class Signal:
    """Trading signal data."""
    signal_id: str
    symbol: str
    signal_data: Dict[str, Any]
    reasoning: str
    timestamp: float
    status: SignalStatus
    prev_hash: str
    this_hash: str


class TradeJournal:
    """
    Immutable trade journal with SHA-256 hash chaining.
    BUG-36: Non-blocking approval with threading.Event.
    """
    
    __slots__ = ['db_path', '_conn', '_approval_pending', 
                 '_approval_result', '_approval_lock']
    
    def __init__(self, db_path: str = _DB_DEFAULT):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._approval_pending = threading.Event()
        self._approval_result: Dict[str, Any] = {}
        self._approval_lock = threading.Lock()
        self._init_db()
    
    def _init_db(self) -> None:
        """Initialize database with WAL mode."""
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        
        # Create signals table with hash chain
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                signal_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                signal_data TEXT,
                reasoning TEXT,
                timestamp REAL,
                status TEXT,
                prev_hash TEXT,
                this_hash TEXT,
                approved_at REAL,
                outcome TEXT
            )
        """)
        
        self._conn.commit()
    
    def _get_last_hash(self) -> str:
        """Get hash of last entry for chain."""
        cursor = self._conn.execute("""
            SELECT this_hash FROM signals
            ORDER BY timestamp DESC
            LIMIT 1
        """)
        
        row = cursor.fetchone()
        return row[0] if row else "0" * 64
    
    def _compute_hash(self, data: Dict[str, Any]) -> str:
        """Compute SHA-256 hash of signal data."""
        canonical = json.dumps(data, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(canonical.encode()).hexdigest()
    
    def log_signal(self, symbol: str, signal_data: Dict[str, Any],
                   reasoning: str) -> str:
        """
        Log a new trading signal.
        
        Args:
            symbol: Trading pair
            signal_data: Signal details
            reasoning: Why this signal was generated
            
        Returns:
            Signal ID
        """
        import uuid
        
        signal_id = str(uuid.uuid4())
        timestamp = time.time()
        prev_hash = self._get_last_hash()
        
        # Convert signal_data to JSON for consistent hashing
        signal_data_json = json.dumps(signal_data, sort_keys=True, separators=(',', ':'))
        
        # Compute hash including prev_hash for chain
        # Use same format as verification
        hash_data = {
            "signal_id": signal_id,
            "signal_data": signal_data_json,
            "timestamp": timestamp,
            "prev_hash": prev_hash
        }
        this_hash = self._compute_hash(hash_data)
        
        try:
            self._conn.execute("""
                INSERT INTO signals
                (signal_id, symbol, signal_data, reasoning, timestamp,
                 status, prev_hash, this_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                signal_id, symbol, signal_data_json,
                reasoning, timestamp, SignalStatus.PENDING.value,
                prev_hash, this_hash
            ))
            self._conn.commit()
        except sqlite3.Error as e:
            # Retry
            time.sleep(0.1)
            self._conn.execute("""
                INSERT INTO signals
                (signal_id, symbol, signal_data, reasoning, timestamp,
                 status, prev_hash, this_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                signal_id, symbol, json.dumps(signal_data),
                reasoning, timestamp, SignalStatus.PENDING.value,
                prev_hash, this_hash
            ))
            self._conn.commit()
        
        return signal_id
    
    def request_approval(self, signal_id: str, timeout: int = 120) -> bool:
        """
        BUG-36: Request non-blocking approval.
        
        Args:
            signal_id: Signal to approve
            timeout: Auto-reject after this many seconds
            
        Returns:
            True if approved, False otherwise
        """
        with self._approval_lock:
            self._approval_pending.set()
            self._approval_result[signal_id] = None
        
        # In real implementation, this would trigger overlay display
        # and keyboard listener. For now, auto-approve after timeout.
        
        # Wait for approval or timeout
        approved = self._approval_pending.wait(timeout=timeout)
        
        with self._approval_lock:
            result = self._approval_result.get(signal_id)
            self._approval_pending.clear()
        
        if result is None:
            # Timeout - auto-reject
            self.log_approval(signal_id, False)
            return False
        
        return result
    
    def approve_signal(self, signal_id: str) -> None:
        """Approve signal (called by keyboard handler)."""
        with self._approval_lock:
            self._approval_result[signal_id] = True
            self._approval_pending.set()
    
    def reject_signal(self, signal_id: str) -> None:
        """Reject signal (called by keyboard handler)."""
        with self._approval_lock:
            self._approval_result[signal_id] = False
            self._approval_pending.set()
    
    def log_approval(self, signal_id: str, approved: bool) -> None:
        """Log approval/rejection decision."""
        status = SignalStatus.APPROVED if approved else SignalStatus.REJECTED
        
        try:
            self._conn.execute("""
                UPDATE signals
                SET status = ?, approved_at = ?
                WHERE signal_id = ?
            """, (status.value, time.time(), signal_id))
            self._conn.commit()
        except sqlite3.Error:
            pass
    
    def log_result(self, signal_id: str, outcome: str) -> None:
        """Log signal outcome (profit/loss)."""
        try:
            self._conn.execute("""
                UPDATE signals
                SET outcome = ?
                WHERE signal_id = ?
            """, (outcome, signal_id))
            self._conn.commit()
        except sqlite3.Error:
            pass
    
    def verify_chain_integrity(self) -> bool:
        """
        Verify hash chain integrity.
        
        Returns:
            True if chain is valid
        """
        cursor = self._conn.execute("""
            SELECT signal_id, signal_data, timestamp, prev_hash, this_hash
            FROM signals
            ORDER BY timestamp
        """)
        
        prev_hash = "0" * 64
        
        for row in cursor:
            signal_id, signal_data_json, timestamp, stored_prev, stored_this = row
            
            # Verify prev_hash matches
            if stored_prev != prev_hash:
                return False
            
            # Recompute this_hash using canonical JSON to ensure consistency
            # Use the raw JSON string from DB to avoid parsing differences
            hash_data = {
                "signal_id": signal_id,
                "signal_data": signal_data_json,  # Use raw JSON string
                "timestamp": timestamp,
                "prev_hash": prev_hash
            }
            computed_hash = self._compute_hash(hash_data)
            
            if computed_hash != stored_this:
                return False
            
            prev_hash = stored_this
        
        return True
    
    def get_signal_accuracy(self, strategy_name: str) -> Dict[str, Any]:
        """Get accuracy stats for a strategy."""
        cursor = self._conn.execute("""
            SELECT status, outcome FROM signals
            WHERE signal_data LIKE ?
        """, (f'%"strategy": "{strategy_name}"%',))
        
        total = 0
        approved = 0
        profitable = 0
        
        for row in cursor:
            total += 1
            if row[0] == SignalStatus.APPROVED.value:
                approved += 1
            if row[1] and "profit" in row[1].lower():
                profitable += 1
        
        return {
            "total_signals": total,
            "approved": approved,
            "profitable": profitable,
            "accuracy": profitable / approved if approved > 0 else 0
        }
    
    def export_journal_report(self, days: int = 30) -> str:
        """Export journal report as markdown."""
        cutoff = time.time() - (days * 86400)
        
        cursor = self._conn.execute("""
            SELECT symbol, signal_data, reasoning, timestamp, status, outcome
            FROM signals
            WHERE timestamp > ?
            ORDER BY timestamp DESC
        """, (cutoff,))
        
        lines = [f"# Trade Journal Report (Last {days} days)", ""]
        
        for row in cursor:
            symbol, signal_data_json, reasoning, ts, status, outcome = row
            signal_data = json.loads(signal_data_json)
            
            date_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
            lines.append(f"## {symbol} - {date_str}")
            lines.append(f"**Status:** {status}")
            lines.append(f"**Signal:** {signal_data.get('recommended', 'N/A')}")
            lines.append(f"**Strength:** {signal_data.get('signal_strength', 'N/A')}%")
            lines.append(f"**Reasoning:** {reasoning}")
            if outcome:
                lines.append(f"**Outcome:** {outcome}")
            lines.append("")
        
        return "\n".join(lines)
    
    def get_stats(self) -> Dict[str, int]:
        """Get journal statistics."""
        cursor = self._conn.execute("""
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending,
                SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END) as approved,
                SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) as rejected
            FROM signals
        """)
        
        row = cursor.fetchone()
        return {
            "total": row[0] or 0,
            "pending": row[1] or 0,
            "approved": row[2] or 0,
            "rejected": row[3] or 0
        }


# Global instance
_journal: Optional[TradeJournal] = None


def get_journal() -> TradeJournal:
    """Get or create global trade journal."""
    global _journal
    if _journal is None:
        _journal = TradeJournal()
    return _journal


def log_signal(symbol: str, signal_data: Dict[str, Any], reasoning: str) -> str:
    """Log a signal."""
    return get_journal().log_signal(symbol, signal_data, reasoning)


def request_approval(signal_id: str, timeout: int = 120) -> bool:
    """Request approval."""
    return get_journal().request_approval(signal_id, timeout)


def log_approval(signal_id: str, approved: bool) -> None:
    """Log approval."""
    get_journal().log_approval(signal_id, approved)


def verify_chain_integrity() -> bool:
    """Verify chain integrity."""
    return get_journal().verify_chain_integrity()
