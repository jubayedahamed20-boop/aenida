"""
AENIDA Smart Worker  v1.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The single entry-point the systemd service (usb_installer.py) launches.

What it does:
  1. Sets up file-only logging (never pollutes the desktop)
  2. Pre-warms TradingView MCP subprocess (if configured)
  3. Starts the AENIDA FastAPI bridge on port 8000
     (Mother sends tasks here — bridge._dispatch_task_real handles routing)
  4. Handles SIGTERM / SIGINT for graceful shutdown

Why smart_worker.py exists (vs bridge.py):
  bridge.py is a library + a minimal CLI.
  smart_worker.py is the production supervisor:
    • sets up logging first (before any imports that log)
    • pre-warms expensive resources (TradingView MCP, model caches)
    • stays running via uvicorn's own event loop
    • tears everything down cleanly on stop

Usage:
  python smart_worker.py                  # foreground (systemd)
  python smart_worker.py --port 8000
  python smart_worker.py --no-tv-mcp     # skip TradingView pre-warm
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import argparse
import logging
import os
import signal
import sys
import time
import threading
from pathlib import Path
from typing import Optional

# ── Path setup (must happen before any AENIDA imports) ──────────────────────
_THIS  = Path(__file__).resolve()
_ROOT  = _THIS.parent.parent          # aenida_v3_working/
_DIRS  = ["core", "mobile", "trading", "tools", "modules"]

for _d in _DIRS:
    _p = str(_ROOT / _d)
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Also run path_setup if it exists (registers everything)
try:
    import path_setup  # noqa: F401
except ImportError:
    pass


# ── Logging (file-only in production, optional console in dev) ──────────────

def _setup_logging(log_dir: Path, console: bool = False) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"smart_worker_{time.strftime('%Y%m%d')}.log"

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(name)-22s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        from logging.handlers import RotatingFileHandler
        fh = RotatingFileHandler(
            log_file, maxBytes=10_000_000, backupCount=5, encoding="utf-8")
    except Exception:
        fh = logging.FileHandler(log_file, encoding="utf-8")

    fh.setFormatter(fmt)
    root.addHandler(fh)

    if console:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        root.addHandler(ch)

    return logging.getLogger("smart_worker")


# ── TradingView MCP pre-warm ─────────────────────────────────────────────────

def _prewarm_tradingview_mcp(log: logging.Logger) -> None:
    """
    Optionally start the TradingView MCP subprocess early so the first
    task request doesn't pay the cold-start penalty.
    Failure here is not fatal — tasks will start MCP on demand.
    """
    try:
        import config as cfg
        if not cfg.get("tradingview.auto_start_mcp", False):
            log.info("[TV_MCP] auto_start_mcp=false — skipping pre-warm")
            return
    except Exception:
        return

    try:
        from tradingview_mcp import get_manager
        mgr = get_manager()
        log.info("[TV_MCP] Pre-warming MCP process...")
        ok = mgr.start()
        if ok:
            log.info("[TV_MCP] Pre-warm complete ✓")
        else:
            log.warning("[TV_MCP] Pre-warm failed — will retry on first task")
    except Exception as exc:
        log.warning("[TV_MCP] Pre-warm skipped: %s", exc)


# ── Signal handling ──────────────────────────────────────────────────────────

_shutdown_event = threading.Event()


def _handle_signal(signum, _frame) -> None:
    logging.getLogger("smart_worker").info(
        "Received signal %s — shutting down", signum)
    _shutdown_event.set()


# ── Bridge server ────────────────────────────────────────────────────────────

def _start_bridge(host: str, port: int, log: logging.Logger) -> None:
    """Start the FastAPI bridge server (blocking — runs in main thread via uvicorn)."""
    try:
        import uvicorn
        from bridge import create_worker_app
    except ImportError as exc:
        log.critical("Cannot start bridge server: %s", exc)
        log.critical("Install with:  pip install fastapi uvicorn --break-system-packages")
        sys.exit(1)

    app = create_worker_app()
    if app is None:
        log.critical("create_worker_app() returned None — cannot start")
        sys.exit(1)

    log.info("Starting AENIDA bridge on %s:%s", host, port)
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="warning",          # uvicorn logs go to its own handler
        access_log=False,             # keep file clean
    )


# ── Health report thread ─────────────────────────────────────────────────────

def _health_reporter(log: logging.Logger) -> None:
    """
    Background thread: logs a health summary every 5 minutes.
    Useful for diagnosing systemd-managed workers via journalctl.
    """
    while not _shutdown_event.is_set():
        _shutdown_event.wait(timeout=300)   # 5 min
        if _shutdown_event.is_set():
            break
        try:
            import psutil
            vm  = psutil.virtual_memory()
            cpu = psutil.cpu_percent(interval=1)
            log.info(
                "[HEALTH] RAM %.0f%% used  CPU %.0f%%  uptime OK",
                vm.percent, cpu,
            )
        except ImportError:
            log.debug("[HEALTH] psutil not available — install for RAM stats")
        except Exception as exc:
            log.warning("[HEALTH] Report failed: %s", exc)


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="AENIDA Smart Worker — production Worker PC entry-point"
    )
    parser.add_argument("--host",       default="0.0.0.0",
                        help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port",       type=int, default=8000,
                        help="Listen port (default: 8000)")
    parser.add_argument("--log-dir",    default=str(_ROOT / "logs"),
                        help="Log directory")
    parser.add_argument("--console",    action="store_true",
                        help="Also print logs to stdout (dev mode)")
    parser.add_argument("--no-tv-mcp", action="store_true",
                        help="Skip TradingView MCP pre-warm")
    args = parser.parse_args()

    # ── Logging ──────────────────────────────────────────────────
    log = _setup_logging(Path(args.log_dir), console=args.console)

    log.info("=" * 60)
    log.info("AENIDA Smart Worker v1.0 starting")
    log.info("Root:    %s", _ROOT)
    log.info("Host:    %s:%s", args.host, args.port)
    log.info("Log dir: %s", args.log_dir)
    log.info("=" * 60)

    # ── Signal handlers ───────────────────────────────────────────
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT,  _handle_signal)

    # ── TradingView MCP pre-warm (background thread) ──────────────
    if not args.no_tv_mcp:
        tv_thread = threading.Thread(
            target=_prewarm_tradingview_mcp,
            args=(log,),
            daemon=True,
            name="tv-mcp-prewarm",
        )
        tv_thread.start()

    # ── Health reporter (background thread) ──────────────────────
    health_thread = threading.Thread(
        target=_health_reporter,
        args=(log,),
        daemon=True,
        name="health-reporter",
    )
    health_thread.start()

    # ── Bridge server (blocking — exits when uvicorn stops) ───────
    try:
        _start_bridge(args.host, args.port, log)
    except SystemExit as exc:
        log.info("Bridge exited with code %s", exc.code)
    except Exception as exc:
        log.critical("Bridge crashed: %s", exc, exc_info=True)
    finally:
        _shutdown_event.set()

        # Stop TradingView MCP cleanly
        try:
            from tradingview_mcp import get_manager
            get_manager().stop()
            log.info("[TV_MCP] MCP process stopped")
        except Exception:
            pass

        log.info("Smart Worker shut down cleanly")


if __name__ == "__main__":
    main()
