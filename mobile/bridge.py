"""
AENIDA Bridge
Laptop ↔ Office PC transport over Tailscale.
All payloads zlib-compressed + SHA-256 verified.
Dynamic timeout: max(5s, min(30s, Avg_RTT × 2))   [BUG-16 fix]
"""

import time
import json
import zlib
import hashlib
import logging
import os
import threading
from typing import Dict, Any, Optional, List
from collections import deque

try:
    import requests
    REQUESTS_OK = True
except ImportError:
    REQUESTS_OK = False
    logging.warning("requests not installed — bridge HTTP calls disabled")


# ── RTT sliding window ────────────────────────────────────────────
class _RTTWindow:
    """Sliding window for average RTT calculation (BUG-16)."""
    __slots__ = ["_window", "_lock"]

    def __init__(self, size: int = 10):
        self._window: deque = deque(maxlen=size)
        self._lock = threading.Lock()

    def record(self, rtt_seconds: float) -> None:
        with self._lock:
            self._window.append(rtt_seconds)

    def avg(self) -> float:
        with self._lock:
            if not self._window:
                return 5.0  # sensible default
            return sum(self._window) / len(self._window)

    def dynamic_timeout(self) -> float:
        """BUG-16: max(5s, min(30s, Avg_RTT × 2))"""
        return max(5.0, min(30.0, self.avg() * 2))


_rtt = _RTTWindow()


# ── Payload helpers ───────────────────────────────────────────────
def _compress(data: Dict[str, Any]) -> bytes:
    raw = json.dumps(data, separators=(",", ":")).encode()
    return zlib.compress(raw, level=6)


def _decompress(data: bytes) -> Dict[str, Any]:
    raw = zlib.decompress(data)
    return json.loads(raw.decode())


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── Mother-side functions ─────────────────────────────────────────
def _worker_url() -> str:
    """Get worker base URL from env or config."""
    try:
        import config
        url = config.get("worker.url", "")
        if url:
            return url.rstrip("/")
    except Exception:
        pass
    return os.environ.get("WORKER_URL", "http://localhost:8000")


def _api_key() -> str:
    try:
        import config
        return config.get("api_keys.worker_api_key", "") or \
               os.environ.get("WORKER_API_KEY", "")
    except Exception:
        return os.environ.get("WORKER_API_KEY", "")


def send_task(task: Dict[str, Any]) -> Dict[str, Any]:
    """
    Send task to Worker PC.
    Compresses payload, attaches SHA-256, dynamic timeout.

    Returns:
        Worker response dict, or fallback dict on error.
    """
    if not REQUESTS_OK:
        return {"error": "requests not available", "degraded": True}

    url = f"{_worker_url()}/task"
    headers = {
        "X-AENIDA-KEY": _api_key(),
        "Content-Type": "application/octet-stream",
    }
    compressed = _compress(task)
    checksum = _sha256(compressed)
    headers["X-Checksum"] = checksum

    timeout = _rtt.dynamic_timeout()
    t0 = time.time()
    try:
        resp = requests.post(url, data=compressed, headers=headers,
                             timeout=timeout)
        rtt = time.time() - t0
        _rtt.record(rtt)

        if resp.status_code == 200:
            # Verify response checksum
            resp_checksum = resp.headers.get("X-Checksum", "")
            if resp_checksum and _sha256(resp.content) != resp_checksum:
                logging.error("[BRIDGE] Response checksum mismatch — discarding")
                return {"error": "checksum_mismatch", "degraded": True}
            return _decompress(resp.content)

        logging.error(f"[BRIDGE] Worker returned {resp.status_code}")
        return {"error": f"http_{resp.status_code}", "degraded": True}

    except requests.exceptions.Timeout:
        logging.warning(f"[BRIDGE] Timeout after {timeout:.1f}s — falling back")
        return {"error": "timeout", "degraded": True}
    except Exception as e:
        logging.error(f"[BRIDGE] send_task failed: {e}")
        return {"error": str(e), "degraded": True}


def check_worker_online() -> bool:
    """
    Ping worker /health endpoint.
    Result cached 30 s to avoid ping floods.
    """
    return _OnlineCache.check()


def get_worker_status() -> Dict[str, Any]:
    """Return full worker stats dict."""
    if not REQUESTS_OK:
        return {"online": False, "error": "requests not available"}
    try:
        resp = requests.get(f"{_worker_url()}/status",
                            headers={"X-AENIDA-KEY": _api_key()},
                            timeout=5)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        logging.debug(f"[BRIDGE] status check failed: {e}")
    return {"online": False}


class _OnlineCache:
    """30-second cache for worker online check."""
    _last_check: float = 0
    _last_result: bool = False
    _TTL: float = 30.0
    _lock = threading.Lock()

    @classmethod
    def check(cls) -> bool:
        with cls._lock:
            if time.time() - cls._last_check < cls._TTL:
                return cls._last_result
        # Outside lock for slow network call
        result = cls._ping()
        with cls._lock:
            cls._last_check = time.time()
            cls._last_result = result
        return result

    @classmethod
    def _ping(cls) -> bool:
        if not REQUESTS_OK:
            return False
        try:
            url = f"{_worker_url()}/health"
            resp = requests.get(url,
                                headers={"X-AENIDA-KEY": _api_key()},
                                timeout=5)
            return resp.status_code == 200
        except Exception:
            return False


# ── Worker-side FastAPI app ───────────────────────────────────────
def _dispatch_task_real(task: Dict[str, Any]) -> Dict[str, Any]:
    """
    BUG #5 FIX — Real task dispatcher for the worker /task endpoint.

    Routes to cognition_router first, then falls back through the
    AENIDA module chain. Every error is caught independently so one
    bad module never kills the whole worker process.

    Returns a result dict with at minimum:
        {"status": "ok"|"error", "task_id": ..., "worker": ..., ...}
    """
    task_id   = task.get("task_id", "")
    task_type = task.get("type") or task.get("task_type", "general")
    payload   = task.get("payload", task)   # some callers embed payload at top level
    worker_id = os.uname().nodename if hasattr(os, "uname") else "worker"

    base = {"task_id": task_id, "worker": worker_id, "task_type": task_type}

    # ── Route 1: cognition_router (fast reflex match) ─────────────
    try:
        from cognition_router import CognitionRouter
        router = CognitionRouter()
        query  = payload.get("query") or payload.get("task") or task_type
        result = router.route(query, payload)
        if result and not result.get("fallback_needed"):
            return {**base, "status": "ok", "source": "cognition_router",
                    "output": result}
    except Exception as e:
        logging.warning(f"[BRIDGE] cognition_router failed ({e}) — trying local_brain")

    # ── Route 2: local_brain (LLM inference) ──────────────────────
    try:
        from local_brain import LocalBrain
        brain  = LocalBrain()
        query  = payload.get("query") or payload.get("task") or ""
        ctx    = payload.get("context", {})
        if query:
            answer = brain.think(query, context=ctx)  # FIX NF-3: explicit keyword arg
            return {**base, "status": "ok", "source": "local_brain",
                    "output": answer}
    except Exception as e:
        logging.warning(f"[BRIDGE] local_brain failed ({e}) — trying task-specific dispatch")

    # ── Route 3: Task-type specific handlers ──────────────────────
    try:
        if task_type in ("analyze", "trade_analysis"):
            from trading_agent import TradingAgent
            symbol = payload.get("symbol", "BTC-USDT")
            output = TradingAgent().analyze(symbol)
            return {**base, "status": "ok", "source": "trading_agent", "output": output}

        elif task_type in ("update_check", "self_improve"):
            from update_engine import run_update_check
            output = run_update_check()
            return {**base, "status": "ok", "source": "update_engine", "output": output}

        elif task_type in ("nightly_tests", "run_tests"):
            from test_runner import run_all_tests
            output = run_all_tests()
            return {**base, "status": "ok", "source": "test_runner", "output": output}

        elif task_type == "memory_query":
            from knowledge_repo import KnowledgeRepo
            repo   = KnowledgeRepo()
            output = repo.search(payload.get("query", ""))
            return {**base, "status": "ok", "source": "knowledge_repo", "output": output}

        elif task_type == "ping":
            return {**base, "status": "ok", "source": "bridge", "output": "pong"}

        # ── TradingView MCP Bridge (Worker PC only) ───────────────
        # Handles: tradingview_analyze, tradingview_chart_data,
        #          tradingview_pine, tradingview_alert,
        #          tradingview_screenshot, tradingview_set_symbol,
        #          tradingview_status
        elif task_type.startswith("tradingview_"):
            try:
                import sys, os
                # BUG-BR-1 fix: use abspath so the dedup check works even when
                # sys.path already holds the absolute version of the same dir.
                _tv_path = os.path.abspath(
                    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trading")
                )
                if _tv_path not in sys.path:
                    sys.path.insert(0, _tv_path)
                from tradingview_mcp import handle_tradingview_task
                output = handle_tradingview_task(task_type, payload)
                return {**base, **output}
            except Exception as tv_err:
                logging.error(f"[BRIDGE] TradingView task failed: {tv_err}")
                return {**base, "status": "error",
                        "error": f"tradingview_mcp: {tv_err}", "output": None}

        else:
            # Unknown task type — log it and return degraded (not error)
            # so master knows we're alive but couldn't handle this type
            logging.warning(f"[BRIDGE] Unknown task_type={task_type!r} — returning degraded")
            return {**base, "status": "degraded",
                    "error": f"No handler for task_type={task_type!r}",
                    "output": None}

    except Exception as e:
        logging.error(f"[BRIDGE] Task dispatch failed task_type={task_type}: {e}",
                      exc_info=True)
        return {**base, "status": "error", "error": str(e), "output": None}


def create_worker_app():
    """
    Create FastAPI app for the worker node.
    Run with: uvicorn bridge:app --host 0.0.0.0 --port 8000
    """
    try:
        from fastapi import FastAPI, Request, HTTPException, Response
        import asyncio
    except ImportError:
        logging.error("fastapi not installed — worker mode unavailable")
        return None

    app = FastAPI(title="AENIDA Worker Node")
    _task_log: List[Dict] = []  # in-memory log (use SQLite in production)

    def _validate_key(request: Request) -> None:
        key = request.headers.get("X-AENIDA-KEY", "")
        expected = _api_key()
        if not expected or key != expected:
            raise HTTPException(status_code=401, detail="Invalid API key")

    @app.post("/task")
    async def receive_task(request: Request) -> Response:
        _validate_key(request)
        body = await request.body()

        # Verify checksum
        expected_cs = request.headers.get("X-Checksum", "")
        if expected_cs and _sha256(body) != expected_cs:
            raise HTTPException(status_code=400, detail="Checksum mismatch")

        task = _decompress(body)
        t0 = time.time()

        # ── BUG #5 FIX: Actually dispatch to AENIDA modules ───────
        # The placeholder just returned a fake "processed" status.
        # Now we route to cognition_router (with fallback chain).
        result = await asyncio.to_thread(_dispatch_task_real, task)

        _task_log.append({**task, "processed_at": t0,
                          "duration_ms": (time.time() - t0) * 1000,
                          "status": result.get("status", "unknown")})

        resp_bytes = _compress(result)
        checksum   = _sha256(resp_bytes)
        return Response(content=resp_bytes,
                        media_type="application/octet-stream",
                        headers={"X-Checksum": checksum})

    @app.get("/health")
    async def health():
        # ── BUG #5 FIX: Return real system health, not just task count ──
        info: Dict[str, Any] = {
            "status":          "ok",
            "tasks_completed": len(_task_log),
            "tasks_failed":    sum(1 for t in _task_log if t.get("status") == "error"),
            "worker":          os.uname().nodename if hasattr(os, "uname") else "worker",
            "uptime_seconds":  round(time.time(), 0),
        }
        try:
            import psutil
            vm = psutil.virtual_memory()
            info["ram"]  = {"available_gb": round(vm.available / 1e9, 2),
                            "percent": vm.percent}
            info["cpu_percent"] = psutil.cpu_percent(interval=0)
            info["disk_free_gb"] = round(psutil.disk_usage("/").free / 1e9, 1)
        except ImportError:
            pass
        # Surface any recent errors to master
        recent_errors = [t.get("status") for t in _task_log[-20:] if t.get("status") == "error"]
        if len(recent_errors) >= 5:
            info["status"] = "degraded"
            info["warning"] = f"{len(recent_errors)} failures in last 20 tasks"
        return info

    @app.post("/recover")
    async def recover(request: Request):
        """
        BUG #5 FIX: Remote self-healing endpoint.
        Master can POST here to trigger recovery on this worker PC.
        Called automatically by mother_router when health score drops below 40.
        """
        _validate_key(request)
        try:
            from recovery import run_recovery
            report = await asyncio.to_thread(run_recovery)
            logging.info(f"[BRIDGE] Remote recovery triggered: {report}")
            return {"status": "ok", "recovery": report}
        except Exception as e:
            logging.error(f"[BRIDGE] Remote recovery failed: {e}")
            return {"status": "error", "error": str(e)}

    @app.get("/status")
    async def status():
        return {"status": "ok", "tasks_total": len(_task_log),
                "worker_url": _worker_url()}

    return app


# ── CLI entry point ───────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="AENIDA Bridge")
    parser.add_argument("--mode", choices=["worker", "test"],
                        default="worker")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if args.mode == "worker":
        try:
            import uvicorn
            app = create_worker_app()
            if app:
                print(f"[BRIDGE] Starting worker on port {args.port}")
                uvicorn.run(app, host="0.0.0.0", port=args.port)
        except ImportError:
            print("uvicorn not installed. pip install uvicorn fastapi")
    elif args.mode == "test":
        online = check_worker_online()
        print(f"Worker online: {online}")
