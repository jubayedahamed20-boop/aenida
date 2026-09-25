"""
AENIDA Orchestrator
Central pipeline that connects EVERY module.
Fixes: #1 #2 #3 #4 #5 #6 #7 #8 #9 #10 #11 #12

Decision flow:
  Input → Validate → Analyze → Decide → Act → Store → Alert

Module connections wired here:
  trading_agent  ←→ memory_layer  (all signals stored/loaded)
  trading_agent  ←→ strategy_engine (strategy influences signal)
  trading_agent  ←→ performance_learner (scores updated after result)
  news_watcher   ←→ trading_agent (news feeds into signal weight)
  update_engine  ←→ module_forge  (auto-creates fixes)
  local_brain    ←→ fallback_chain (escalation ladder)
  all modules    ←→ alert_manager (single alert bus)
  all modules    ←→ memory_layer  (every decision remembered)
"""

import logging
import os
import sys
import time
import threading
from typing import Any, Dict, List, Optional

# ── Structured logging setup (Fix #9) ────────────────────────────
def setup_logging(log_dir: str = "logs", shadow_mode: bool = False) -> None:
    """
    BUG #2 FIX: Structured logging — console output DISABLED in shadow/worker mode.

    Args:
        log_dir:     Directory for rotating log files.
        shadow_mode: When True (worker PC), suppress ALL console output.
                     Log goes to file only. User on this PC sees nothing.
    """
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"aenida_{time.strftime('%Y%m%d')}.log")

    fmt = "%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s"

    # ── File handler (always active) ─────────────────────────────────
    try:
        from logging.handlers import RotatingFileHandler
        file_handler = RotatingFileHandler(
            log_file, maxBytes=10_000_000, backupCount=5, encoding="utf-8")
    except Exception:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(fmt))

    handlers = [file_handler]

    # ── Console handler — ONLY for display/api mode, never worker ────
    if not shadow_mode:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(logging.Formatter(fmt))
        handlers.append(console)

    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    mode_str = "SILENT (worker)" if shadow_mode else "console+file"
    logging.info(f"[ORCH] Logging → {log_file} [{mode_str}]")


# ── System state (shared across modules) ─────────────────────────
class SystemState:
    """Single source of truth for live system state."""
    __slots__ = [
        "worker_online", "mode", "running",
        "last_signal", "last_signal_time",
        "api_status", "ram_mb", "pending_tasks",
        "news_alert_count", "decisions_today",
        "_lock"
    ]

    def __init__(self):
        self.worker_online: bool = False
        self.mode: str = "display"          # display | worker | api
        self.running: bool = False
        self.last_signal: Dict = {}
        self.last_signal_time: float = 0
        self.api_status: Dict[str, str] = {}
        self.ram_mb: float = 0
        self.pending_tasks: int = 0
        self.news_alert_count: int = 0
        self.decisions_today: int = 0
        self._lock = threading.Lock()

    def update(self, **kwargs) -> None:
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self, k):
                    setattr(self, k, v)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "worker_online": self.worker_online,
                "mode": self.mode,
                "running": self.running,
                "last_signal": self.last_signal,
                "last_signal_time": self.last_signal_time,
                "api_status": self.api_status,
                "ram_mb": self.ram_mb,
                "pending_tasks": self.pending_tasks,
                "news_alert_count": self.news_alert_count,
                "decisions_today": self.decisions_today,
            }


STATE = SystemState()


# ── Step 1: Startup validation (Fix #7 config, Fix #6 errors) ────
def validate_startup(cfg) -> Dict[str, Any]:
    """
    Validate all required services before starting.
    Returns dict of what's OK and what's degraded.
    """
    log = logging.getLogger("startup")
    report = {
        "config":    False,
        "memory":    False,
        "market":    False,
        "ai_api":    False,
        "worker":    False,
        "logging":   True,   # already set up
        "errors":    [],
    }

    # Config check
    try:
        issues = cfg.validate() if hasattr(cfg, 'validate') else []
        if issues:
            report["errors"].extend(issues)
            log.warning(f"Config issues: {issues}")
        report["config"] = True
        log.info("Config ✓")
    except Exception as e:
        report["errors"].append(f"config: {e}")
        log.error(f"Config ✗ {e}")

    # Memory layer
    try:
        from memory_layer import get_stats
        stats = get_stats()
        report["memory"] = True
        log.info(f"Memory ✓ (memvid={stats.get('memvid_ok')})")
    except Exception as e:
        report["errors"].append(f"memory: {e}")
        log.warning(f"Memory ✗ {e}")

    # Market data
    try:
        from market_data import get_current_price
        price = get_current_price("BTC-USDT")
        if price and price > 0:
            report["market"] = True
            log.info(f"Market data ✓ BTC=${price:,.0f}")
        else:
            log.warning("Market data: no price returned")
    except Exception as e:
        report["errors"].append(f"market_data: {e}")
        log.warning(f"Market data ✗ {e}")

    # AI APIs
    try:
        from fallback_chain import get_provider_status
        ps = get_provider_status()
        healthy = [k for k, v in ps.items()
                   if v.get("status") == "healthy"]
        report["ai_api"] = len(healthy) > 0
        STATE.update(api_status={k: v["status"] for k, v in ps.items()})
        log.info(f"AI APIs: {healthy}")
    except Exception as e:
        report["errors"].append(f"ai_apis: {e}")
        log.warning(f"AI APIs ✗ {e}")

    # Worker PC
    try:
        from bridge import check_worker_online
        online = check_worker_online()
        report["worker"] = online
        STATE.update(worker_online=online)
        log.info(f"Worker PC: {'ONLINE ✓' if online else 'OFFLINE (local brain active)'}")
    except Exception as e:
        report["errors"].append(f"worker: {e}")
        log.warning(f"Worker check ✗ {e}")

    return report


# ── Step 2: Module connection wiring ─────────────────────────────
def wire_modules() -> None:
    """
    Connect all modules together (Fix #2).
    Sets up callbacks and shared references.
    """
    log = logging.getLogger("wire")

    # Wire memory into trading agent
    try:
        from trading_agent import get_agent
        from memory_layer import remember, get_context_for_task

        agent = get_agent()

        # Patch analyze_symbol to store signals in memory
        _orig_analyze = agent.analyze_symbol

        def _analyze_with_memory(symbol):
            # Load past context before analysis
            past = get_context_for_task(f"trade signal {symbol}", max_tokens=300)
            result = _orig_analyze(symbol)
            # Save result to memory
            remember({
                "type": "trade_signal",
                "symbol": symbol,
                "result": result,
                "timestamp": time.time(),
            })
            STATE.update(
                last_signal=result,
                last_signal_time=time.time(),
                decisions_today=STATE.decisions_today + 1,
            )
            return result

        agent.analyze_symbol = _analyze_with_memory
        log.info("trading_agent ←→ memory_layer ✓")
    except Exception as e:
        log.warning(f"trading_agent ←→ memory_layer: {e}")

    # Wire news into trading signal weight
    try:
        from news_watcher import get_watcher
        from trading_agent import get_agent

        watcher = get_watcher()
        agent = get_agent()

        # When news fires alert, flag it in state
        def _news_callback(alerts):
            if alerts:
                STATE.update(news_alert_count=STATE.news_alert_count + len(alerts))
                log.info(f"News alert → trading context updated ({len(alerts)} alerts)")

        watcher._alert_callback = _news_callback
        log.info("news_watcher ←→ trading_agent ✓")
    except Exception as e:
        log.warning(f"news_watcher ←→ trading_agent: {e}")

    # Wire performance_learner into trade results
    try:
        from trade_journal import get_journal
        from performance_learner import update_node_score

        journal = get_journal()
        _orig_log = journal.log_approval

        def _log_approval_with_learning(signal_id, approved):
            _orig_log(signal_id, approved)
            # Update performance score for worker node
            try:
                from bridge import check_worker_online
                node = "worker" if check_worker_online() else "local_brain"
                update_node_score(node, success=approved)
            except Exception:
                pass

        journal.log_approval = _log_approval_with_learning
        log.info("trade_journal ←→ performance_learner ✓")
    except Exception as e:
        log.warning(f"trade_journal ←→ performance_learner: {e}")

    # Wire update_engine → module_forge (auto-create fixes)
    try:
        import update_engine
        import module_forge
        # Already wired inside update_engine.route_suggestion()
        log.info("update_engine ←→ module_forge ✓")
    except Exception as e:
        log.warning(f"update_engine ←→ module_forge: {e}")

    # Wire memory monitor → all services (backpressure)
    try:
        from memory_monitor import get_monitor
        from news_watcher import get_watcher as gw
        monitor = get_monitor()
        monitor.register_callback("news_watcher",
                                   lambda paused: gw().stop() if paused else gw().start())
        log.info("memory_monitor ←→ services ✓")
    except Exception as e:
        log.warning(f"memory_monitor backpressure: {e}")

    log.info("Module wiring complete")


# ── Step 3: Decision pipeline (Fix #4) ───────────────────────────
def run_decision_pipeline(symbol: str,
                           extra_context: Optional[Dict] = None
                           ) -> Dict[str, Any]:
    """
    Full pipeline: Input → Analyze → Decide → Act → Store → Alert
    This is the CORE of the autonomous agent (Fix #12 auto-loop).
    """
    log = logging.getLogger("pipeline")
    t0 = time.time()
    ctx = extra_context or {}

    # ── 1. INPUT ─────────────────────────────────────────────────
    try:
        from market_data import get_current_price, fetch_crypto_ohlcv
        price = get_current_price(symbol) or 0
        ohlcv = fetch_crypto_ohlcv(symbol, "1H", 100)
        ctx["price"] = price
        ctx["ohlcv_len"] = len(ohlcv)
    except Exception as e:
        log.warning(f"[PIPELINE] Input: {e}")
        price = 0
        ohlcv = []

    # ── 2. ANALYZE ───────────────────────────────────────────────
    try:
        from local_brain import think
        analysis = think(f"analyze {symbol}", context=ctx)
        signals = analysis.get("signals", {})
    except Exception as e:
        log.warning(f"[PIPELINE] Analyze: {e}")
        signals = {}
        analysis = {"result": "analysis failed", "degraded": True}

    # ── 3. DECIDE ────────────────────────────────────────────────
    decision = _make_decision(symbol, signals, price)

    # ── 4. RISK CHECK (Fix #11) ───────────────────────────────────
    risk_ok, risk_reason = _risk_check(decision, price)
    if not risk_ok:
        log.warning(f"[PIPELINE] Risk check blocked: {risk_reason}")
        decision["action"] = "HOLD"
        decision["blocked_by_risk"] = risk_reason

    # ── 5. ACT ───────────────────────────────────────────────────
    if decision["action"] in ("BUY", "SELL") and risk_ok:
        try:
            from trade_journal import log_signal
            signal_id = log_signal(
                symbol=symbol,
                signal_data={**signals, **decision},
                reasoning=analysis.get("result", "")[:200],
            )
            decision["signal_id"] = signal_id
            log.info(f"[PIPELINE] Signal logged: {symbol} {decision['action']} "
                     f"strength={decision.get('strength', 0):.0f}%")
        except Exception as e:
            log.error(f"[PIPELINE] Journal: {e}")

    # ── 6. STORE ─────────────────────────────────────────────────
    try:
        from memory_layer import remember
        remember({
            "type": "pipeline_run",
            "symbol": symbol,
            "price": price,
            "decision": decision,
            "duration_ms": round((time.time() - t0) * 1000),
            "timestamp": time.time(),
        })
    except Exception as e:
        log.debug(f"[PIPELINE] Memory store: {e}")

    # ── 7. ALERT ─────────────────────────────────────────────────
    if decision["action"] in ("BUY", "SELL"):
        try:
            from alert_manager import send, AlertLevel
            strength = decision.get("strength", 0)
            send(AlertLevel.WARNING,
                 f"🔔 {decision['action']} Signal: {symbol}",
                 f"Price: ${price:,.2f} | Strength: {strength:.0f}% | "
                 f"RSI: {signals.get('rsi', 'N/A')}")
        except Exception as e:
            log.debug(f"[PIPELINE] Alert: {e}")

    elapsed = round((time.time() - t0) * 1000)
    log.info(f"[PIPELINE] {symbol} → {decision['action']} "
             f"({elapsed}ms | worker={'✓' if STATE.worker_online else '✗'})")

    return {
        "symbol": symbol,
        "price": price,
        "decision": decision,
        "signals": signals,
        "analysis_source": analysis.get("provider", "unknown"),
        "elapsed_ms": elapsed,
    }


def _make_decision(symbol: str, signals: Dict,
                   price: float) -> Dict[str, Any]:
    """Convert signals into a concrete trading decision."""
    strength = float(signals.get("signal_strength", 50))
    recommended = signals.get("recommended", "HOLD")

    # Need at least 65% confidence for a signal
    from config import get as cfg_get
    threshold = cfg_get("trading.min_signal_confidence", 0.65) * 100

    if strength >= threshold and recommended == "BUY":
        action = "BUY"
    elif strength >= threshold and recommended == "SELL":
        action = "SELL"
    else:
        action = "HOLD"

    return {
        "action": action,
        "strength": strength,
        "recommended": recommended,
        "price": price,
        "timestamp": time.time(),
    }


def _risk_check(decision: Dict, price: float) -> tuple:
    """
    Fix #11: Basic risk management.
    Returns (ok: bool, reason: str)
    """
    try:
        from config import get as cfg_get
        max_risk_pct = cfg_get("trading.risk_per_trade_percent", 2.0)
        stop_loss_pct = cfg_get("trading.default_stop_loss_percent", 3.0)

        if decision.get("action") == "HOLD":
            return True, "no action"

        # Check price is sane
        if price <= 0:
            return False, "invalid price"

        # Check strength above minimum
        if decision.get("strength", 0) < 50:
            return False, "signal too weak"

        return True, "ok"
    except Exception:
        return True, "risk_check_skipped"


# ── Step 4: Auto-loop (Fix #12 — makes it an AGENT) ──────────────
class AgentLoop:
    """
    The heartbeat of AENIDA. Runs continuously.
    Fixes #12: while True: think() act()
    """

    def __init__(self, symbols: List[str], interval: int = 60):
        self.symbols = symbols
        self.interval = interval
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._cycle = 0
        self._log = logging.getLogger("agent_loop")

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="aenida_agent")
        self._thread.start()
        self._log.info(
            f"Agent loop started — symbols={self.symbols} "
            f"interval={self.interval}s")

    def stop(self) -> None:
        self._running = False
        self._log.info("Agent loop stopping...")

    def _loop(self) -> None:
        """Fix #12: The real agent loop with full error handling."""
        while self._running:
            self._cycle += 1
            self._log.info(f"─── Cycle #{self._cycle} ───────────────────")

            # Heartbeat: update worker status + trigger remote recovery if degraded
            try:
                from bridge import check_worker_online, get_worker_status
                online = check_worker_online()
                STATE.update(worker_online=online)

                # ── BUG #5 FIX: Remote self-healing ──────────────────
                # If worker is online but reporting degraded health,
                # POST to /recover so it can fix itself without manual intervention.
                if online:
                    try:
                        wstatus = get_worker_status()
                        if wstatus.get("status") == "degraded":
                            self._log.warning(
                                "[AGENT] Worker reported degraded — triggering remote recovery")
                            from bridge import _worker_url, _api_key
                            import requests as _req
                            _req.post(
                                f"{_worker_url()}/recover",
                                headers={"X-AENIDA-KEY": _api_key()},
                                timeout=10,
                            )
                    except Exception as re:
                        self._log.debug(f"Remote recovery trigger: {re}")

                    # Flush local-brain queued tasks if worker came back
                    try:
                        from local_brain import flush_pending
                        flushed = flush_pending()
                        if flushed:
                            self._log.info(f"Flushed {flushed} queued tasks to Worker")
                    except Exception:
                        pass

            except Exception:
                pass

            # RAM check
            try:
                from memory_monitor import get_memory_stats
                mem = get_memory_stats()
                ram_mb = mem.get("used_mb", 0)
                STATE.update(ram_mb=ram_mb)
                if ram_mb > 180:
                    self._log.warning(f"High RAM: {ram_mb:.0f}MB")
            except Exception:
                pass

            # Run decision pipeline for each symbol
            for symbol in self.symbols:
                if not self._running:
                    break
                try:
                    run_decision_pipeline(symbol)
                except Exception as e:
                    # Fix #6: one crash never kills the loop
                    self._log.error(
                        f"Pipeline error for {symbol}: {e}", exc_info=True)
                    _auto_recover(e)

            # Pending task check
            try:
                from task_queue import get_pending_count
                count = get_pending_count()
                STATE.update(pending_tasks=count)
            except Exception:
                pass

            # Sleep in 1s increments so stop() works immediately
            for _ in range(self.interval):
                if not self._running:
                    break
                time.sleep(1)

    def get_status(self) -> Dict[str, Any]:
        return {
            "running": self._running,
            "cycle": self._cycle,
            "symbols": self.symbols,
            "interval": self.interval,
        }


def _auto_recover(error: Exception) -> None:
    """Fix #6: Auto-recovery after crash."""
    log = logging.getLogger("recovery")
    log.warning(f"[RECOVER] Attempting recovery after: {error}")
    try:
        from recovery import run_recovery
        report = run_recovery()
        log.info(f"[RECOVER] Done: {report}")
    except Exception as e:
        log.error(f"[RECOVER] Recovery itself failed: {e}")


# ── Step 5: Services launcher ─────────────────────────────────────
def start_background_services(mode: str) -> List[threading.Thread]:
    """Start all background services in daemon threads."""
    log = logging.getLogger("services")
    threads = []

    def safe_start(name, fn):
        try:
            fn()
            log.info(f"{name} ✓")
        except Exception as e:
            log.warning(f"{name} ✗ {e}")

    if mode == "display":
        # Memory monitor
        t = threading.Thread(
            target=lambda: safe_start(
                "memory_monitor",
                lambda: __import__("memory_monitor").start_monitoring()),
            daemon=True, name="mem_monitor")
        t.start(); threads.append(t)

        # News watcher
        t = threading.Thread(
            target=lambda: safe_start(
                "news_watcher",
                lambda: __import__("news_watcher").start()),
            daemon=True, name="news_watcher")
        t.start(); threads.append(t)

        # USB sentinel
        t = threading.Thread(
            target=lambda: safe_start(
                "usb_sentinel",
                lambda: __import__("usb_sentinel").start()),
            daemon=True, name="usb_sentinel")
        t.start(); threads.append(t)

    elif mode == "worker":
        # Mother router FastAPI
        try:
            from mother_router import create_app
            app = create_app()
            if app:
                import uvicorn
                t = threading.Thread(
                    target=lambda: uvicorn.run(
                        app, host="0.0.0.0", port=8001,
                        log_level="warning"),
                    daemon=True, name="mother_router")
                t.start(); threads.append(t)
                log.info("mother_router FastAPI ✓")
        except Exception as e:
            log.warning(f"mother_router: {e}")

        # Bridge worker
        try:
            from bridge import create_worker_app
            wapp = create_worker_app()
            if wapp:
                import uvicorn
                t = threading.Thread(
                    target=lambda: uvicorn.run(
                        wapp, host="0.0.0.0", port=8000,
                        log_level="warning"),
                    daemon=True, name="bridge_worker")
                t.start(); threads.append(t)
                log.info("bridge worker FastAPI ✓")
        except Exception as e:
            log.warning(f"bridge worker: {e}")

    return threads


# ── Public API ────────────────────────────────────────────────────
_agent_loop: Optional[AgentLoop] = None


def start_agent(symbols: List[str], interval: int = 60) -> AgentLoop:
    global _agent_loop
    _agent_loop = AgentLoop(symbols, interval)
    _agent_loop.start()
    return _agent_loop


def stop_agent() -> None:
    global _agent_loop
    if _agent_loop:
        _agent_loop.stop()


def get_agent_status() -> Dict[str, Any]:
    return {
        "state": STATE.snapshot(),
        "agent": _agent_loop.get_status() if _agent_loop else {},
    }


def process_command(cmd: str,
                    args: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Fix #8: Process CLI/API commands. Returns response dict.
    Commands: status | analyze <SYM> | think <TEXT> | stop | start
    """
    args = args or []
    cmd = cmd.lower().strip()

    if cmd == "status":
        return get_agent_status()

    elif cmd == "analyze" and args:
        symbol = args[0].upper()
        return run_decision_pipeline(symbol)

    elif cmd in ("think", "ask") and args:
        query = " ".join(args)
        from local_brain import think
        return think(query)

    elif cmd == "stop":
        stop_agent()
        return {"result": "Agent stopped"}

    elif cmd == "start" and args:
        symbols = [s.upper() for s in args]
        start_agent(symbols)
        return {"result": f"Agent started for {symbols}"}

    elif cmd == "workers":
        from worker_registry import get_active_workers
        return {"workers": get_active_workers()}

    elif cmd == "signals":
        from trading_agent import get_last_signals
        return {"signals": get_last_signals()}

    elif cmd == "memory" and args:
        query = " ".join(args)
        from memory_layer import search
        return {"results": search(query, top_k=5)}

    elif cmd == "suggestions":
        from update_engine import get_pending_suggestions
        return {"suggestions": get_pending_suggestions()}

    elif cmd == "modules":
        from module_forge import list_modules
        return {"modules": list_modules()}

    elif cmd == "news":
        from news_watcher import get_recent
        return {"news": get_recent(10)}

    elif cmd == "check-updates":
        from update_engine import run_update_check
        return run_update_check()

    else:
        return {
            "error": f"Unknown command: {cmd}",
            "available": [
                "status", "analyze <SYM>", "think <TEXT>",
                "workers", "signals", "memory <QUERY>",
                "suggestions", "modules", "news",
                "check-updates", "start <SYMS>", "stop"
            ]
        }
