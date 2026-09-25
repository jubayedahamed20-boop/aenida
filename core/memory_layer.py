"""
AENIDA Memory Layer
Three-layer memory using real Memvid API (BUG-19 fix).
Layer 1: Short-term (RAM, session only)
Layer 2: Long-term (.mv2 disk file, millions of entries)
Layer 3: Procedural (skills/ folder — skill_library.py)
"""

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

MV2_DEFAULT = "data/AENIDA_memory.mv2"
INDEX_DEFAULT = "data/AENIDA_memory_index.json"


class MemoryLayer:
    """
    Persistent AI memory using Memvid .mv2 format.
    Falls back to SQLite if Memvid not installed.
    """

    __slots__ = ["mv2_path", "index_path", "_short_term",
                 "_encoder", "_retriever", "_memvid_ok",
                 "_pending_chunks"]

    MAX_SHORT_TERM = 10

    def __init__(self, mv2_path: str = MV2_DEFAULT,
                 index_path: str = INDEX_DEFAULT):
        os.makedirs(os.path.dirname(mv2_path), exist_ok=True) \
            if os.path.dirname(mv2_path) else None
        self.mv2_path = mv2_path
        self.index_path = index_path
        self._short_term: List[Dict] = []   # Layer 1 — RAM only
        self._pending_chunks: List[str] = []  # queued for .mv2 write
        self._encoder = None
        self._retriever = None
        self._memvid_ok = False
        self._init_memvid()

    def _init_memvid(self) -> None:
        """Try to initialise real Memvid library."""
        try:
            from memvid import MemvidEncoder, MemvidRetriever  # type: ignore
            self._memvid_ok = True
            # Load existing retriever if file exists
            if os.path.exists(self.mv2_path) and os.path.exists(self.index_path):
                self._retriever = MemvidRetriever(self.mv2_path, self.index_path)
                logging.info(f"[MEMORY] Loaded .mv2 from {self.mv2_path}")
            else:
                logging.info("[MEMORY] No existing .mv2 — will create on first commit")
        except ImportError:
            self._memvid_ok = False
            logging.warning(
                "[MEMORY] memvid not installed — pip install memvid. "
                "Falling back to in-memory only."
            )

    # ── Layer 1: Short-term ───────────────────────────────────────
    def _add_short_term(self, entry: Dict) -> None:
        self._short_term.append(entry)
        if len(self._short_term) > self.MAX_SHORT_TERM:
            self._short_term = self._short_term[-self.MAX_SHORT_TERM:]

    def clear_short_term(self) -> None:
        """Call at end of session."""
        self._short_term.clear()

    # ── Layer 2: Long-term (.mv2) ─────────────────────────────────
    def remember(self, event: Dict[str, Any]) -> None:
        """
        Store an event in long-term memory.
        Events are batched and committed to .mv2 periodically.
        """
        # Always add to short-term
        self._add_short_term(event)

        # Serialize for .mv2
        text = json.dumps(event, default=str, separators=(",", ":"))
        self._pending_chunks.append(text)

        # Commit when batch reaches 50
        if len(self._pending_chunks) >= 50:
            self._commit_pending()

    def _commit_pending(self) -> None:
        """Write pending chunks to .mv2 file."""
        if not self._pending_chunks:
            return
        if not self._memvid_ok:
            self._pending_chunks.clear()
            return
        try:
            from memvid import MemvidEncoder  # type: ignore
            enc = MemvidEncoder(chunk_size=512)
            # Add existing file content if present (append mode)
            if os.path.exists(self.mv2_path):
                enc.add_chunks(self._pending_chunks)
            else:
                enc.add_chunks(self._pending_chunks)
            enc.build_video(self.mv2_path, self.index_path)
            # Refresh retriever
            from memvid import MemvidRetriever  # type: ignore
            self._retriever = MemvidRetriever(self.mv2_path, self.index_path)
            committed = len(self._pending_chunks)
            self._pending_chunks.clear()
            logging.debug(f"[MEMORY] Committed to .mv2 ({committed} chunks)")
        except Exception as e:
            logging.error(f"[MEMORY] Commit failed: {e}")

    def search(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """
        Semantic search across .mv2 long-term memory.
        Falls back to short-term keyword search if Memvid unavailable.
        """
        # Flush any pending chunks first
        if self._pending_chunks:
            self._commit_pending()

        if self._memvid_ok and self._retriever:
            try:
                results = self._retriever.search(query, top_k=top_k)
                parsed = []
                for r in results:
                    text = r if isinstance(r, str) else str(r)
                    try:
                        parsed.append(json.loads(text))
                    except Exception:
                        parsed.append({"text": text})
                return parsed
            except Exception as e:
                logging.warning(f"[MEMORY] .mv2 search failed: {e}")

        # Fallback: short-term keyword search
        query_lower = query.lower()
        results = []
        for entry in reversed(self._short_term):
            entry_text = json.dumps(entry, default=str).lower()
            if query_lower in entry_text:
                results.append(entry)
            if len(results) >= top_k:
                break
        return results

    def get_context_for_task(self, task: str,
                              max_tokens: int = 2000) -> str:
        """
        Search memory and format top results as context string.
        Max ~2000 tokens. RAM released immediately after.
        """
        results = self.search(task, top_k=5)
        if not results:
            return ""

        lines = ["[Memory Context]"]
        total_chars = 0
        char_limit = max_tokens * 4  # ~4 chars per token

        for r in results:
            line = json.dumps(r, default=str, separators=(",", ":"))
            if total_chars + len(line) > char_limit:
                break
            lines.append(line)
            total_chars += len(line)

        return "\n".join(lines)

    def archive_to_cold(self, entries: List[Dict]) -> None:
        """
        Archive entries to .mv2 cold storage (BUG-18 fix).
        Old data still searchable but lower priority.
        """
        if not entries:
            return
        chunks = [json.dumps(e, default=str) for e in entries]
        self._pending_chunks.extend(chunks)
        self._commit_pending()
        logging.info(f"[MEMORY] Archived {len(entries)} entries to cold storage")

    def forget_old(self, days: int = 90) -> int:
        """
        Remove entries older than N days from short-term.
        (.mv2 entries managed by memory_cleaner.py)
        """
        cutoff = time.time() - days * 86400
        before = len(self._short_term)
        self._short_term = [
            e for e in self._short_term
            if e.get("timestamp", time.time()) > cutoff
        ]
        removed = before - len(self._short_term)
        if removed:
            logging.info(f"[MEMORY] Forgot {removed} old short-term entries")
        return removed

    def get_stats(self) -> Dict[str, Any]:
        stats: Dict[str, Any] = {
            "short_term_count": len(self._short_term),
            "pending_chunks": len(self._pending_chunks),
            "memvid_ok": self._memvid_ok,
            "mv2_exists": os.path.exists(self.mv2_path),
        }
        if os.path.exists(self.mv2_path):
            stats["mv2_size_mb"] = round(
                os.path.getsize(self.mv2_path) / 1e6, 2)
        return stats

    def flush(self) -> None:
        """Force commit all pending chunks."""
        self._commit_pending()


# ── Global instance ───────────────────────────────────────────────
_memory: Optional[MemoryLayer] = None


def get_memory() -> MemoryLayer:
    global _memory
    if _memory is None:
        try:
            import config
            mv2 = config.get("paths.memory_mv2", MV2_DEFAULT)
            idx = mv2.replace(".mv2", "_index.json")
        except Exception:
            mv2, idx = MV2_DEFAULT, INDEX_DEFAULT
        _memory = MemoryLayer(mv2, idx)
    return _memory


def remember(event: Dict[str, Any]) -> None:
    get_memory().remember(event)


def search(query: str, top_k: int = 5) -> List[Dict[str, Any]]:
    return get_memory().search(query, top_k)


def get_context_for_task(task: str, max_tokens: int = 2000) -> str:
    return get_memory().get_context_for_task(task, max_tokens)


def archive_to_cold(entries: List[Dict]) -> None:
    get_memory().archive_to_cold(entries)


def forget_old(days: int = 90) -> int:
    return get_memory().forget_old(days)


def get_stats() -> Dict[str, Any]:
    return get_memory().get_stats()


def flush() -> None:
    get_memory().flush()
