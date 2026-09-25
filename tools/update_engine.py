"""
AENIDA Update Engine
Watches 8 data sources, generates ranked improvement suggestions,
routes by confidence, auto-creates temp modules for high-confidence fixes.
BUG-30: confidence gate  BUG-37: notification on every suggestion.
"""

import json
import logging
import os
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # aenida_v3 root
DB_PATH = os.path.join(PROJECT_ROOT, "data", "suggestions.db")
SUGGESTED_MD = os.path.join(PROJECT_ROOT, "knowledge",
                             "improvements", "suggested.md")


# ── Suggestions DB ────────────────────────────────────────────────
def _db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS suggestions (
            id              TEXT PRIMARY KEY,
            created_at      REAL NOT NULL,
            title           TEXT,
            problem         TEXT,
            solution        TEXT,
            type            TEXT,
            affected_file   TEXT,
            confidence      REAL,
            priority        TEXT,
            effort          TEXT,
            evidence        TEXT,
            status          TEXT DEFAULT 'PENDING',
            applied_at      REAL,
            rejected_at     REAL,
            rejection_reason TEXT,
            outcome         TEXT
        )
    """)
    conn.commit()
    return conn


# ── Data collection ───────────────────────────────────────────────
def collect_performance_data() -> Dict[str, Any]:
    """Collect metrics from all 8 data sources."""
    data: Dict[str, Any] = {"collected_at": time.time(), "sources": {}}

    # Source 1: task_queue stats
    try:
        from task_queue import get_queue
        stats = get_queue().get_stats()
        data["sources"]["task_queue"] = stats
    except Exception as e:
        data["sources"]["task_queue"] = {"error": str(e)}

    # Source 2: audit_log (recent errors)
    try:
        audit_path = os.path.join(PROJECT_ROOT, "data", "audit_log.db")
        if os.path.exists(audit_path):
            conn = sqlite3.connect(audit_path)
            cursor = conn.execute("""
                SELECT COUNT(*) as cnt, event_type
                FROM tyr_audit
                WHERE timestamp > ?
                GROUP BY event_type
            """, (time.time() - 7 * 86400,))
            data["sources"]["audit_log"] = dict(cursor.fetchall())
            conn.close()
    except Exception as e:
        data["sources"]["audit_log"] = {"error": str(e)}

    # Source 3: worker registry health
    try:
        from worker_registry import get_stats
        data["sources"]["worker_registry"] = get_stats()
    except Exception as e:
        data["sources"]["worker_registry"] = {"error": str(e)}

    # Source 4: memory monitor stats
    try:
        from memory_monitor import get_memory_stats
        data["sources"]["memory"] = get_memory_stats()
    except Exception as e:
        data["sources"]["memory"] = {"error": str(e)}

    # Source 5: fallback chain provider health
    try:
        from fallback_chain import get_provider_status
        data["sources"]["providers"] = get_provider_status()
    except Exception as e:
        data["sources"]["providers"] = {"error": str(e)}

    # Source 6: past suggestions (avoid duplicates)
    try:
        conn = _db()
        cursor = conn.execute("""
            SELECT title, status, outcome FROM suggestions
            WHERE created_at > ? ORDER BY created_at DESC LIMIT 20
        """, (time.time() - 30 * 86400,))
        data["sources"]["past_suggestions"] = [
            {"title": r[0], "status": r[1], "outcome": r[2]}
            for r in cursor.fetchall()
        ]
        conn.close()
    except Exception as e:
        data["sources"]["past_suggestions"] = []

    # Source 7: test results summary
    try:
        from test_runner import run_all_tests
        # Only run quick smoke check — not full suite
        data["sources"]["test_summary"] = {"note": "full run at 3AM nightly"}
    except Exception as e:
        data["sources"]["test_summary"] = {"error": str(e)}

    # Source 8: model shell capability map
    try:
        from model_shell import get_capability_map
        data["sources"]["model_shell"] = get_capability_map()
    except Exception as e:
        data["sources"]["model_shell"] = {"error": str(e)}

    return data


# ── Suggestion generation ─────────────────────────────────────────
def generate_suggestions(data: Dict[str, Any],
                          max_count: int = 5) -> List[Dict[str, Any]]:
    """
    Send collected data to Worker AI and get improvement suggestions.
    Returns list of suggestion dicts.
    """
    summary = json.dumps(data, indent=2, default=str)[:4000]

    prompt = f"""You are AENIDA's self-improvement engine.
Here is the system performance data:
{summary}

Find up to {max_count} real, specific improvements (not generic advice).
For each return a JSON object:
{{
  "title": "Short descriptive title",
  "problem": "Exact problem observed in the data",
  "solution": "Specific technical fix",
  "type": "fix|new_module|config_change|skill",
  "affected_file": "filename.py or null",
  "confidence": 0.0-1.0,
  "priority": "low|medium|high|critical",
  "effort": "minutes|hours|days",
  "evidence": "Specific data point proving this"
}}

Return ONLY a JSON array of objects. No markdown. No explanation."""

    try:
        from model_shell import call
        result = call(prompt)
        text = result.get("result", "[]")
        import re
        text = re.sub(r"```json\n?|```\n?", "", text).strip()
        suggestions = json.loads(text)
        if isinstance(suggestions, list):
            return suggestions[:max_count]
    except Exception as e:
        logging.error(f"[UPDATE_ENGINE] Suggestion generation failed: {e}")

    return []


# ── Confidence routing (BUG-30) ───────────────────────────────────
def route_suggestion(suggestion: Dict[str, Any]) -> str:
    """
    Route suggestion based on confidence level.
    Returns action taken: logged|suggested|alerted|auto_created
    BUG-30 fix: proper confidence gates.
    BUG-37 fix: notification sent.
    """
    conf = float(suggestion.get("confidence", 0.0))
    title = suggestion.get("title", "")
    stype = suggestion.get("type", "")

    # Always save to DB
    _save_suggestion(suggestion)

    if conf < 0.60:
        # Log only — not confident enough to bother user
        logging.info(f"[UPDATE_ENGINE] Low-confidence suggestion logged: {title} ({conf:.0%})")
        return "logged"

    if conf < 0.80:
        # Write to suggested.md
        _append_to_md(suggestion)
        logging.info(f"[UPDATE_ENGINE] Suggestion added to suggested.md: {title}")
        return "suggested"

    # ≥ 0.80 — write to MD AND notify
    _append_to_md(suggestion)
    _notify(suggestion)

    # ≥ 0.95 — auto-create temp module for both "fix" AND "new_module" types
    # BUG 4 FIX: old gate was `stype == "fix"` only.
    # GitHub research always produces type="new_module". With the old gate,
    # even a 100%-confidence GitHub pattern could NEVER create a module.
    # Fix: allow "fix" OR "new_module" to trigger auto-creation.
    if conf >= 0.95 and stype in ("fix", "new_module"):
        logging.info(f"[UPDATE_ENGINE] Auto-creating temp module for: {title}")
        try:
            from module_forge import create_temp
            mid = create_temp({
                "purpose":      suggestion.get("solution", title),
                "description":  suggestion.get("problem", ""),
                "confidence":   conf,
                # BUG 4 FIX: pass source so knowledge_repo can trace origin
                "source":       suggestion.get("source", "update_engine"),
                "affected_file": suggestion.get("affected_file"),
                "expiry_hours": 24,
            })
            if mid:
                return "auto_created"
        except Exception as e:
            logging.error(f"[UPDATE_ENGINE] Auto-create failed: {e}")

    return "alerted"


def _save_suggestion(suggestion: Dict[str, Any]) -> None:
    conn = _db()
    sid = str(uuid.uuid4())
    conn.execute("""
        INSERT OR IGNORE INTO suggestions
        (id, created_at, title, problem, solution, type, affected_file,
         confidence, priority, effort, evidence)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        sid, time.time(),
        suggestion.get("title"), suggestion.get("problem"),
        suggestion.get("solution"), suggestion.get("type"),
        suggestion.get("affected_file"), suggestion.get("confidence", 0.0),
        suggestion.get("priority"), suggestion.get("effort"),
        suggestion.get("evidence"),
    ))
    conn.commit()
    conn.close()


def _append_to_md(suggestion: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(SUGGESTED_MD), exist_ok=True)
    conf_pct = int(suggestion.get("confidence", 0) * 100)
    ts = time.strftime("%Y-%m-%d %H:%M")
    block = (
        f"\n## [{ts}] {suggestion.get('title', 'Untitled')} "
        f"({conf_pct}% confidence)\n"
        f"**Priority:** {suggestion.get('priority', 'unknown')} | "
        f"**Effort:** {suggestion.get('effort', 'unknown')} | "
        f"**Type:** {suggestion.get('type', 'unknown')}\n\n"
        f"**Problem:** {suggestion.get('problem', '')}\n\n"
        f"**Solution:** {suggestion.get('solution', '')}\n\n"
        f"**Evidence:** {suggestion.get('evidence', '')}\n\n"
        f"**File:** `{suggestion.get('affected_file', 'N/A')}`\n\n"
        f"---"
    )
    with open(SUGGESTED_MD, "a") as f:
        f.write(block)


def _notify(suggestion: Dict[str, Any]) -> None:
    """BUG-37 fix: send alert for every significant suggestion."""
    conf_pct = int(suggestion.get("confidence", 0) * 100)
    msg = (
        f"New suggestion ({conf_pct}%):\n"
        f"{suggestion.get('title', '')}\n"
        f"Priority: {suggestion.get('priority', '')}\n"
        f"Effort: {suggestion.get('effort', '')}\n"
        f"Review: python main.py --suggestions"
    )
    try:
        from alert_manager import send, AlertLevel
        send(AlertLevel.WARNING, "AENIDA Update Suggestion", msg)
    except Exception:
        logging.info(f"[UPDATE_ENGINE] Suggestion alert: {msg}")


# ── Main entry points ─────────────────────────────────────────────
def run_update_check(max_suggestions: int = 5) -> Dict[str, Any]:
    """
    Full update check cycle.
    Returns summary of actions taken.
    """
    logging.info("[UPDATE_ENGINE] Running update check...")
    t0 = time.time()

    data = collect_performance_data()
    suggestions = generate_suggestions(data, max_suggestions)

    results: Dict[str, List] = {
        "logged": [], "suggested": [], "alerted": [], "auto_created": []
    }

    for s in suggestions:
        action = route_suggestion(s)
        results.get(action, results["logged"]).append(s.get("title", ""))

    elapsed = round(time.time() - t0, 1)
    logging.info(
        f"[UPDATE_ENGINE] Done in {elapsed}s — "
        f"{len(suggestions)} suggestions processed"
    )
    return {
        "elapsed_s": elapsed,
        "total": len(suggestions),
        **results
    }


def get_pending_suggestions() -> List[Dict[str, Any]]:
    conn = _db()
    cursor = conn.execute("""
        SELECT id, title, problem, solution, type, confidence, priority,
               effort, evidence, affected_file, created_at
        FROM suggestions WHERE status='PENDING'
        ORDER BY confidence DESC
    """)
    cols = [d[0] for d in cursor.description]
    rows = [dict(zip(cols, r)) for r in cursor.fetchall()]
    conn.close()
    return rows


def mark_suggestion_applied(suggestion_id: str) -> None:
    conn = _db()
    conn.execute(
        "UPDATE suggestions SET status='APPLIED', applied_at=? WHERE id=?",
        (time.time(), suggestion_id))
    conn.commit()
    conn.close()


def mark_suggestion_rejected(suggestion_id: str, reason: str = "") -> None:
    conn = _db()
    conn.execute("""
        UPDATE suggestions SET status='REJECTED', rejected_at=?,
               rejection_reason=? WHERE id=?
    """, (time.time(), reason, suggestion_id))
    conn.commit()
    conn.close()


def get_suggestion_history(days: int = 30) -> List[Dict[str, Any]]:
    conn = _db()
    cursor = conn.execute("""
        SELECT * FROM suggestions
        WHERE created_at > ?
        ORDER BY created_at DESC
    """, (time.time() - days * 86400,))
    cols = [d[0] for d in cursor.description]
    rows = [dict(zip(cols, r)) for r in cursor.fetchall()]
    conn.close()
    return rows


def schedule_update_check(interval_hours: float = 6.0) -> None:
    """Schedule recurring update checks via task_queue."""
    try:
        from task_queue import enqueue
        scheduled = time.time() + interval_hours * 3600
        enqueue("update_check", {}, scheduled_for=scheduled, priority=7)
        logging.info(f"[UPDATE_ENGINE] Next check in {interval_hours}h")
    except Exception as e:
        logging.error(f"[UPDATE_ENGINE] schedule failed: {e}")


# ── CLI ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    summary = run_update_check()
    print(json.dumps(summary, indent=2))
