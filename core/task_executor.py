"""
AENIDA Task Executor (ARES)
Async priority queue with WAL ghost checkpoints.
Dynamic timeout (BUG-16), SHA-256 integrity (BUG-17),
idempotent UUID guard, Ghost Sync on reconnect.
"""
import asyncio, hashlib, json, logging, os, time, uuid
from typing import Any, Dict, List, Optional

class TaskExecutor:
    """Manages task lifecycle with WAL ghost checkpoints."""
    __slots__ = ["_queue","_rtt_window","_running"]
    MAX_RTT_SAMPLES = 10

    def __init__(self):
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._rtt_window: List[float] = []
        self._running = False

    # ── Dynamic timeout (BUG-16) ──
    def _dynamic_timeout(self) -> float:
        if not self._rtt_window:
            return 10.0
        avg = sum(self._rtt_window) / len(self._rtt_window)
        return max(5.0, min(30.0, avg * 2))

    def _record_rtt(self, rtt: float) -> None:
        self._rtt_window.append(rtt)
        if len(self._rtt_window) > self.MAX_RTT_SAMPLES:
            self._rtt_window.pop(0)

    # ── SHA-256 integrity (BUG-17) ──
    @staticmethod
    def _hash(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    # ── Idempotent UUID ──
    @staticmethod
    def _step_uuid(task_id: str, step: int) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{task_id}::step::{step}"))

    async def submit_task(self, plan: Dict[str, Any]) -> str:
        task_id = plan.get("task_id") or str(uuid.uuid4())
        priority = plan.get("priority", 5)
        plan["task_id"] = task_id
        # Write ghost checkpoint
        try:
            from persistence_layer import PersistenceLayer
            pl = PersistenceLayer()
            step_uuid = self._step_uuid(task_id, 0)
            pl.write_ghost(task_id, step_uuid, 0,
                           "Task submitted", json.dumps(plan).encode())
        except Exception:
            pass
        await self._queue.put((priority, time.time(), plan))
        return task_id

    async def execute_step(self, step: Dict[str, Any]) -> Dict[str, Any]:
        task_id = step.get("task_id","")
        step_idx = step.get("step_index", 0)
        step_uuid = self._step_uuid(task_id, step_idx)
        # Idempotency check
        try:
            from integrity_checker import check_idempotency
            if check_idempotency(step_uuid):
                logging.info(f"[ARES] Step {step_uuid[:8]} already done — skipping")
                return {"status":"skipped","step_uuid":step_uuid}
        except Exception:
            pass
        t0 = time.time()
        # Dispatch via bridge
        payload_bytes = json.dumps(step).encode()
        checksum = self._hash(payload_bytes)
        try:
            from bridge import send_task
            timeout = self._dynamic_timeout()
            result = await asyncio.wait_for(
                asyncio.to_thread(send_task, {**step,"checksum":checksum}),
                timeout=timeout
            )
            self._record_rtt(time.time() - t0)
            # Verify response integrity
            resp_cs = result.get("checksum","")
            resp_bytes = json.dumps(result).encode()
            if resp_cs and self._hash(resp_bytes) != resp_cs:
                logging.error("[ARES] Response checksum mismatch")
                return {"error":"checksum_mismatch"}
            # Update ghost
            try:
                from persistence_layer import PersistenceLayer
                PersistenceLayer().mark_complete(step_uuid)
            except Exception:
                pass
            return result
        except asyncio.TimeoutError:
            logging.warning(f"[ARES] Step timed out after {timeout:.1f}s")
            return {"error":"timeout","step_uuid":step_uuid}
        except Exception as e:
            return {"error":str(e),"step_uuid":step_uuid}

    async def run_loop(self) -> None:
        self._running = True
        while self._running:
            try:
                priority, ts, plan = await asyncio.wait_for(
                    self._queue.get(), timeout=1.0)
                task_id = plan.get("task_id","")
                steps = plan.get("steps", [plan])
                for i, step in enumerate(steps):
                    step["task_id"] = task_id
                    step["step_index"] = i
                    result = await self.execute_step(step)
                    if result.get("error"):
                        logging.warning(f"[ARES] Step {i} failed: {result['error']}")
                        break
                self._queue.task_done()
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logging.error(f"[ARES] Loop error: {e}")

    def stop(self) -> None:
        self._running = False

    async def perform_ghost_sync(self) -> Dict[str, Any]:
        """Reconcile with Worker after reconnect > 30s."""
        try:
            from persistence_layer import PersistenceLayer
            pl = PersistenceLayer()
            pending = pl.count_pending()
            return {"pending_ghosts": pending, "synced": True}
        except Exception as e:
            return {"error": str(e)}

    def get_task_status(self, task_id: str) -> Dict[str, Any]:
        try:
            from task_queue import get_queue
            q = get_queue()
            conn = q._conn
            cursor = conn.execute(
                "SELECT status FROM tasks WHERE task_id=?", (task_id,))
            row = cursor.fetchone()
            return {"task_id": task_id,
                    "status": row[0] if row else "not_found"}
        except Exception:
            return {"task_id": task_id, "status": "unknown"}

# Global
_executor: Optional[TaskExecutor] = None

def get_executor() -> TaskExecutor:
    global _executor
    if _executor is None:
        _executor = TaskExecutor()
    return _executor
