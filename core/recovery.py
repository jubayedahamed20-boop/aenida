"""
AENIDA Recovery
Startup crash recovery — runs FIRST on every boot.
Resets stale tasks, syncs with workers, catches up missed jobs.
"""

import time
import logging
import os
import sys
from typing import Dict, Any, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
import path_setup  # noqa: E402


def run_recovery(db_path: str = os.path.join(_ROOT, "data", "task_queue.db"),
                 ledger_path: str = os.path.join(_ROOT, "data", "task_ledger.db")) -> Dict[str, Any]:
    """
    Full startup recovery sequence.

    Returns:
        Summary dict with counts of what was recovered.
    """
    logging.info("[RECOVERY] Starting recovery sequence...")
    report: Dict[str, Any] = {
        "stale_reset": 0,
        "pending_ghosts": 0,
        "catchup_tasks": 0,
        "errors": []
    }

    # Step 1 — Reset stale RUNNING tasks back to QUEUED
    try:
        from task_queue import TaskQueue
        q = TaskQueue(db_path)
        reset = q.reset_stale_running(stale_seconds=300)
        report["stale_reset"] = reset
        if reset:
            logging.warning(f"[RECOVERY] Reset {reset} stale RUNNING tasks → QUEUED (PC crashed?)")
    except Exception as e:
        report["errors"].append(f"task_queue reset: {e}")
        logging.error(f"[RECOVERY] task_queue reset failed: {e}")

    # Step 2 — Count pending ghost steps in task_ledger
    try:
        from persistence_layer import PersistenceLayer
        pl = PersistenceLayer(ledger_path)
        ghosts = pl.count_pending()
        report["pending_ghosts"] = ghosts
        if ghosts:
            logging.info(f"[RECOVERY] Found {ghosts} pending ghost steps in task_ledger")
    except Exception as e:
        report["errors"].append(f"persistence_layer: {e}")
        logging.warning(f"[RECOVERY] Could not check ghost steps: {e}")

    # Step 3 — Catch up all past-due queued tasks
    try:
        from task_queue import TaskQueue
        q = TaskQueue(db_path)
        ready = q.dequeue_ready(limit=50)
        report["catchup_tasks"] = len(ready)
        if ready:
            logging.info(f"[RECOVERY] {len(ready)} past-due tasks will run now")
        # Re-enqueue them back as QUEUED so the main loop picks them up
        for task in ready:
            q._conn.execute(
                "UPDATE tasks SET status='queued', started_at=NULL WHERE task_id=?",
                (task["task_id"],)
            )
        q._conn.commit()
    except Exception as e:
        report["errors"].append(f"catchup: {e}")
        logging.error(f"[RECOVERY] catchup failed: {e}")

    logging.info(
        f"[RECOVERY] Complete — stale_reset={report['stale_reset']} "
        f"ghosts={report['pending_ghosts']} catchup={report['catchup_tasks']} "
        f"errors={len(report['errors'])}"
    )
    return report


def generate_recovery_report(report: Dict[str, Any]) -> str:
    """Format recovery report as human-readable string."""
    lines = [
        "═" * 50,
        "AENIDA RECOVERY REPORT",
        "═" * 50,
        f"  Stale tasks reset : {report.get('stale_reset', 0)}",
        f"  Pending ghosts    : {report.get('pending_ghosts', 0)}",
        f"  Catch-up tasks    : {report.get('catchup_tasks', 0)}",
    ]
    errors = report.get("errors", [])
    if errors:
        lines.append(f"  Errors            : {len(errors)}")
        for e in errors:
            lines.append(f"    ⚠ {e}")
    else:
        lines.append("  Errors            : 0 ✓")
    lines.append("═" * 50)
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    result = run_recovery()
    print(generate_recovery_report(result))
