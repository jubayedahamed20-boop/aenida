"""
AENIDA Mother Router
Central routing hub. FastAPI + rule-based intent (NO local LLM).
E_score dispatch with auto-calibrated baselines (BUG-11 fix).
"""

import re
import time
import logging
import os
from typing import Dict, Any, Optional, Tuple

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import JSONResponse
    import uvicorn
    FASTAPI_OK = True
except ImportError:
    FASTAPI_OK = False
    logging.warning("fastapi/uvicorn not installed — HTTP API disabled")


# ── Intent patterns (rule-based, no LLM) ─────────────────────────
_INTENTS: Dict[str, re.Pattern] = {
    "code":     re.compile(
        r"\b(write|function|class|bug|fix|refactor|debug|code|script|implement)\b",
        re.I),
    "analyze":  re.compile(
        r"\b(analyze|analyse|compare|evaluate|research|explain|review)\b", re.I),
    "write":    re.compile(
        r"\b(draft|email|essay|blog|report|document|letter|write)\b", re.I),
    "forecast": re.compile(
        r"\b(forecast|predict|prediction|next|tomorrow|future|timesfm)\b", re.I),
    "chart":    re.compile(
        r"\b(chart|trend|volume|signal|bullish|bearish|candlestick|indicator)\b", re.I),
    "search":   re.compile(
        r"\b(find|search|lookup|what is|who is|price|news|latest)\b", re.I),
}


def classify_intent(task: str) -> Tuple[str, float]:
    """
    Rule-based intent classification — no LLM on Laptop (R03).
    Returns (intent_label, confidence 0-1).
    """
    scores: Dict[str, int] = {}
    for label, pattern in _INTENTS.items():
        matches = pattern.findall(task)
        scores[label] = len(matches)

    if not scores or max(scores.values()) == 0:
        return "chat", 0.3

    best = max(scores, key=lambda k: scores[k])
    total = sum(scores.values()) or 1
    confidence = min(1.0, scores[best] / total + 0.2)
    return best, round(confidence, 2)


# ── E_score formula (BUG-11 fix) ─────────────────────────────────
class EScoreCalculator:
    """
    E_score = (T_local - T_remote) / (L_net + S_data / B_width)
    T_local  = tokens / local_baseline  (tokens/sec for 4GB CPU)
    T_remote = tokens / remote_baseline (tokens/sec for Worker GPU)
    Baselines auto-calibrated every 10 tasks by performance_learner.
    """

    __slots__ = ["local_baseline", "remote_baseline", "_rtt_ms", "_bw_bps"]

    def __init__(self):
        self.local_baseline: float = 50.0    # tokens/sec (4GB CPU)
        self.remote_baseline: float = 800.0  # tokens/sec (Worker GPU)
        self._rtt_ms: float = 50.0           # network RTT ms
        self._bw_bps: float = 1_000_000.0   # bandwidth bytes/sec (1 MB/s)

    def calculate(self, payload_size_bytes: int,
                  task_tokens: int = 200) -> float:
        """
        Returns E_score.
          > 1.0 → route to Worker (faster there)
          ≤ 1.0 → handle locally
        """
        try:
            t_local = task_tokens / max(1.0, self.local_baseline)
            t_remote = task_tokens / max(1.0, self.remote_baseline)
            l_net = self._rtt_ms / 1000.0
            s_bw = payload_size_bytes / max(1.0, self._bw_bps)
            denom = l_net + s_bw
            if denom <= 0:
                return 0.0
            return (t_local - t_remote) / denom
        except Exception:
            return 0.0

    def update_measurement(self, rtt_ms: float, bw_bps: float) -> None:
        self._rtt_ms = rtt_ms
        self._bw_bps = bw_bps

    def update_baselines(self, local_tps: float, remote_tps: float) -> None:
        """Called by performance_learner after calibration."""
        self.local_baseline = max(1.0, local_tps)
        self.remote_baseline = max(1.0, remote_tps)
        logging.info(f"[ROUTER] E_score baselines updated: "
                     f"local={local_tps:.0f} remote={remote_tps:.0f} tokens/s")


# ── Mother Router ─────────────────────────────────────────────────
class MotherRouter:
    """Central routing hub."""

    __slots__ = ["_escore", "_task_log"]

    def __init__(self):
        self._escore = EScoreCalculator()
        self._task_log: list = []

    def dispatch(self, task: str,
                 payload_size: int = 512) -> Dict[str, Any]:
        """
        Classify intent, calculate E_score, route to best destination.
        """
        intent, confidence = classify_intent(task)
        score = self._escore.calculate(payload_size)

        # Determine destination
        if score > 1.0:
            # Worker is faster — send there
            destination = "worker"
            result = self._send_to_worker(task, intent)
        else:
            # Handle locally via fallback_chain
            destination = "local"
            result = self._handle_locally(task, intent)

        # Log routing decision
        entry = {
            "task": task[:80],
            "intent": intent,
            "confidence": confidence,
            "e_score": round(score, 3),
            "destination": destination,
            "timestamp": time.time(),
        }
        self._task_log.append(entry)
        if len(self._task_log) > 500:
            self._task_log = self._task_log[-500:]

        result["routing"] = entry
        return result

    def _send_to_worker(self, task: str, intent: str) -> Dict[str, Any]:
        try:
            from bridge import send_task, check_worker_online
            if not check_worker_online():
                logging.warning("[ROUTER] Worker offline, falling back to local")
                return self._handle_locally(task, intent)
            return send_task({"task": task, "intent": intent})
        except Exception as e:
            logging.error(f"[ROUTER] Worker dispatch failed: {e}")
            return self._handle_locally(task, intent)

    def _handle_locally(self, task: str, intent: str) -> Dict[str, Any]:
        try:
            from fallback_chain import call_ai
            result = call_ai(task)
            return result
        except Exception as e:
            return {"result": f"[Router error: {e}]", "provider": "none",
                    "degraded": True}

    def get_worker_health(self) -> Dict[str, Any]:
        try:
            from bridge import check_worker_online, get_worker_status
            online = check_worker_online()
            if online:
                return {**get_worker_status(), "online": True}
            return {"online": False}
        except Exception:
            return {"online": False}

    def get_routing_log(self, last_n: int = 20) -> list:
        return self._task_log[-last_n:]


# ── FastAPI app ───────────────────────────────────────────────────
def create_app() -> Optional[Any]:
    if not FASTAPI_OK:
        return None

    app = FastAPI(title="AENIDA Mother Router")
    router_instance = MotherRouter()

    @app.post("/task")
    async def submit_task(body: dict):
        task = body.get("task", "")
        if not task:
            raise HTTPException(status_code=400, detail="task required")
        result = router_instance.dispatch(task,
                                          payload_size=len(str(body)))
        return JSONResponse(result)

    @app.get("/task/{task_id}/status")
    async def task_status(task_id: str):
        return {"task_id": task_id, "status": "unknown"}

    @app.get("/health")
    async def health():
        worker = router_instance.get_worker_health()
        try:
            import psutil
            ram = psutil.virtual_memory()
            mem = {"used_mb": round(ram.used / 1e6, 1),
                   "percent": ram.percent}
        except ImportError:
            mem = {}
        return {"status": "ok", "worker": worker, "memory": mem,
                "tasks_routed": len(router_instance._task_log)}

    @app.get("/workers")
    async def workers():
        try:
            from worker_registry import get_active_workers
            return {"workers": get_active_workers()}
        except Exception:
            return {"workers": []}

    return app


# ── Global instance ───────────────────────────────────────────────
_router: Optional[MotherRouter] = None


def get_mother_router() -> MotherRouter:
    global _router
    if _router is None:
        _router = MotherRouter()
    return _router


def dispatch(task: str, payload_size: int = 512) -> Dict[str, Any]:
    return get_mother_router().dispatch(task, payload_size)


def classify_intent_fn(task: str) -> Tuple[str, float]:
    return classify_intent(task)


def update_escore_baselines(local_tps: float, remote_tps: float) -> None:
    get_mother_router()._escore.update_baselines(local_tps, remote_tps)


# ── Standalone run ────────────────────────────────────────────────
if __name__ == "__main__":
    app = create_app()
    if app and FASTAPI_OK:
        port = int(os.environ.get("MOTHER_PORT", "8001"))
        print(f"[ROUTER] Starting on port {port}")
        uvicorn.run(app, host="0.0.0.0", port=port)
    else:
        print("FastAPI not available. pip install fastapi uvicorn")
