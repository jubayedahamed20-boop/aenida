"""
AENIDA × TradingView MCP Bridge  (FIXED v2)
=============================================
Runs ONLY on the Worker PC (main brain) where TradingView Desktop is installed.

Architecture:
  Laptop (Mother)  ──task──▶  Worker PC  ──stdio──▶  MCP Node process
                                                           └▶  TradingView Desktop

BUGS FIXED vs v1:
  BUG-TV-1  sys.path.insert inside every function → moved to module top level
  BUG-TV-2  (CRITICAL) HTTP POST /mcp is wrong transport. tradingview-mcp uses
            JSON-RPC over stdin/stdout (MCP stdio protocol). Entire transport
            layer rewritten as _MCPProcess class using subprocess pipes.
  BUG-TV-3  is_available() called non-existent "ping" method.
            Now checks via MCP "initialize" handshake (is_alive() poll).
  BUG-TV-4  Hardcoded sleep(3) in start() → replaced with poll loop (10s max).
  BUG-TV-5  import json unused → now used by stdio protocol serialisation.
  BUG-TV-6  List imported but never used → removed.
  BUG-TV-7  os.path.dirname(__file__) without abspath → broke with CWD changes.
            All paths now anchored to os.path.abspath(__file__).

Worker PC Setup:
  1. git clone https://github.com/mac-/tradingview-mcp ~/tradingview-mcp
  2. cd ~/tradingview-mcp && npm install && npm run build
  3. Open TradingView Desktop (must be running)
  4. AENIDA spawns the MCP subprocess automatically on first call.
"""

import json
import logging
import os
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ── Module-level path setup (BUG-TV-1 & BUG-TV-7 fix) ──────────────────────
# Anchored to this file's real location — safe regardless of CWD.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_CORE_DIR = os.path.abspath(os.path.join(_THIS_DIR, "..", "core"))
if _CORE_DIR not in sys.path:
    sys.path.insert(0, _CORE_DIR)


def _get_config(key: str, default: Any = None) -> Any:
    """Safe config access — returns default if config not loaded yet."""
    try:
        import config as cfg          # noqa: PLC0415
        return cfg.get(key, default)
    except Exception:
        return default


def _mcp_dir() -> str:
    """Absolute path to the tradingview-mcp project on this Worker PC."""
    raw = _get_config("tradingview.mcp_project_dir",
                      os.path.expanduser("~/tradingview-mcp"))
    return os.path.expanduser(str(raw))


def _mcp_entry() -> str:
    """Path to the compiled MCP entry-point (dist/index.js)."""
    return os.path.join(_mcp_dir(), "dist", "index.js")


# ── MCP stdio transport (BUG-TV-2 fix) ──────────────────────────────────────
#
# The Model Context Protocol transmits newline-delimited JSON-RPC over
# stdin / stdout.  tradingview-mcp only supports this stdio mode — there
# is no HTTP endpoint at /mcp.
#
# Message flow:
#   1. Spawn:  node dist/index.js
#   2. Send initialize request, recv capabilities
#   3. Send "notifications/initialized" (no response)
#   4. Call tools with method="tools/call"

_MCP_CLIENT_INFO = {"name": "AENIDA", "version": "3.0"}
_MCP_CAPABILITIES: Dict = {}


class _MCPProcess:
    """
    Persistent MCP stdio subprocess — one instance per Worker session.
    Thread-safe: uses a lock around every write→read pair.
    """

    def __init__(self) -> None:
        self._proc:   Optional[subprocess.Popen] = None
        self._lock    = threading.Lock()
        self._req_id  = 0
        self._ready   = False

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> bool:
        """Spawn node process and complete MCP handshake."""
        entry = _mcp_entry()
        if not os.path.exists(entry):
            logger.error(
                "[TV_MCP] dist/index.js not found at %s. "
                "Run: cd ~/tradingview-mcp && npm install && npm run build",
                entry,
            )
            return False

        try:
            self._proc = subprocess.Popen(
                ["node", entry],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=_mcp_dir(),
            )
        except FileNotFoundError:
            logger.error("[TV_MCP] 'node' not found — install Node.js 18+")
            return False
        except Exception as exc:
            logger.error("[TV_MCP] Popen failed: %s", exc)
            return False

        # MCP handshake
        try:
            result = self._request("initialize", {
                "protocolVersion": "2024-11-05",
                "clientInfo":      _MCP_CLIENT_INFO,
                "capabilities":    _MCP_CAPABILITIES,
            }, timeout=10.0)

            if "error" in result:
                logger.error("[TV_MCP] initialize failed: %s", result["error"])
                self.stop()
                return False

            # "initialized" is a notification (no id → no response expected)
            self._notify("notifications/initialized", {})
            self._ready = True
            logger.info("[TV_MCP] MCP process ready")
            return True

        except Exception as exc:
            logger.error("[TV_MCP] Handshake failed: %s", exc)
            self.stop()
            return False

    def stop(self) -> None:
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                pass
            self._proc = None
        self._ready = False

    def is_alive(self) -> bool:
        return (
            self._proc is not None
            and self._proc.poll() is None
            and self._ready
        )

    # ── Low-level transport ───────────────────────────────────────────────

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def _write(self, msg: Dict) -> None:
        assert self._proc and self._proc.stdin
        line = json.dumps(msg, separators=(",", ":")) + "\n"
        self._proc.stdin.write(line.encode())
        self._proc.stdin.flush()

    def _read_one(self, timeout: float = 10.0) -> Dict:
        """Read one JSON-RPC message from stdout with timeout."""
        assert self._proc and self._proc.stdout
        result: Dict = {}
        exc_box: list = []

        def _reader() -> None:
            try:
                raw = self._proc.stdout.readline()   # type: ignore[union-attr]
                result.update(json.loads(raw.decode().strip()))
            except Exception as exc:
                exc_box.append(exc)

        thr = threading.Thread(target=_reader, daemon=True)
        thr.start()
        thr.join(timeout=timeout)

        if thr.is_alive():
            return {"error": f"Read timed out after {timeout}s"}
        if exc_box:
            return {"error": str(exc_box[0])}
        return result

    def _notify(self, method: str, params: Dict) -> None:
        """JSON-RPC notification — no id, no response."""
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: Dict,
                 timeout: float = 10.0) -> Dict:
        """Send a JSON-RPC request and return the result or error dict."""
        req_id = self._next_id()
        self._write({
            "jsonrpc": "2.0",
            "id":      req_id,
            "method":  method,
            "params":  params,
        })
        response = self._read_one(timeout=timeout)

        rpc_err = response.get("error")
        if rpc_err:
            return {"error": rpc_err}
        return response.get("result", response)

    # ── Public API ────────────────────────────────────────────────────────

    def call_tool(self, tool_name: str,
                  arguments: Dict,
                  timeout: float = 15.0) -> Dict[str, Any]:
        """Call one MCP tool by name and return its result."""
        with self._lock:
            if not self.is_alive():
                return {"error": "MCP process not running"}
            return self._request(
                "tools/call",
                {"name": tool_name, "arguments": arguments},
                timeout=timeout,
            )

    def list_tools(self) -> Dict[str, Any]:
        with self._lock:
            if not self.is_alive():
                return {"error": "MCP process not running"}
            return self._request("tools/list", {}, timeout=10.0)


# ── Module-level process singleton ───────────────────────────────────────────

_process:      Optional[_MCPProcess] = None
_process_lock  = threading.Lock()


def _get_process() -> _MCPProcess:
    """Return the live MCP process, starting it if needed."""
    global _process
    with _process_lock:
        if _process is None or not _process.is_alive():
            proc = _MCPProcess()
            if not proc.start():
                raise RuntimeError(
                    "Could not start TradingView MCP process. "
                    "Ensure: (1) Node.js 18+ installed, "
                    "(2) cd ~/tradingview-mcp && npm install && npm run build, "
                    "(3) TradingView Desktop is open."
                )
            _process = proc
    return _process


# ── Bridge class ─────────────────────────────────────────────────────────────

class TradingViewMCPBridge:
    """
    AENIDA adapter for TradingView MCP Bridge.
    Worker PC only.  All methods return a dict; errors never raised.
    """

    def is_available(self) -> bool:
        """BUG-TV-3 fix: poll process liveness, not a fake 'ping' RPC call."""
        try:
            return _get_process().is_alive()
        except Exception:
            return False

    def _call(self, tool: str,
              args: Optional[Dict] = None,
              timeout: float = 15.0) -> Dict[str, Any]:
        try:
            return _get_process().call_tool(tool, args or {}, timeout)
        except RuntimeError as exc:
            return {"error": str(exc)}
        except Exception as exc:
            logger.error("[TV_MCP] %s: %s", tool, exc)
            return {"error": str(exc)}

    # ── Chart ─────────────────────────────────────────────────────────────

    def get_chart_data(self, symbol: str = "BTCUSDT",
                       interval: str = "1H") -> Dict[str, Any]:
        return self._call("get_chart_data",
                          {"symbol": symbol, "interval": interval})

    def analyze_chart(self, symbol: str = "BTCUSDT",
                      interval: str = "1H",
                      question: str = "What is the trend?") -> Dict[str, Any]:
        return self._call("analyze_chart", {
            "symbol": symbol, "interval": interval, "question": question,
        }, timeout=30.0)

    def get_drawings(self) -> Dict[str, Any]:
        return self._call("get_drawings")

    def add_indicator(self, name: str,
                      params: Optional[Dict] = None) -> Dict[str, Any]:
        return self._call("add_indicator", {"name": name, "params": params or {}})

    def set_symbol(self, symbol: str,
                   interval: str = "1H") -> Dict[str, Any]:
        return self._call("set_symbol",
                          {"symbol": symbol, "interval": interval})

    # ── Pine Script ───────────────────────────────────────────────────────

    def run_pine_script(self, script: str,
                        symbol: Optional[str] = None) -> Dict[str, Any]:
        args: Dict[str, Any] = {"script": script}
        if symbol:
            args["symbol"] = symbol
        return self._call("run_pine_script", args, timeout=20.0)

    def save_pine_script(self, name: str, script: str) -> Dict[str, Any]:
        return self._call("save_script", {"name": name, "script": script})

    def list_pine_scripts(self) -> Dict[str, Any]:
        return self._call("list_scripts")

    # ── Alerts ────────────────────────────────────────────────────────────

    def create_alert(self, symbol: str, condition: str,
                     price: float, message: str = "") -> Dict[str, Any]:
        return self._call("create_alert", {
            "symbol":    symbol,
            "condition": condition,
            "price":     price,
            "message":   message or f"AENIDA: {symbol} {condition} {price}",
        })

    def list_alerts(self) -> Dict[str, Any]:
        return self._call("list_alerts")

    # ── Screenshot ────────────────────────────────────────────────────────

    def screenshot_chart(self,
                         save_path: Optional[str] = None) -> Dict[str, Any]:
        args: Dict[str, Any] = {}
        if save_path:
            args["save_path"] = save_path
        return self._call("screenshot", args, timeout=20.0)


# ── AENIDA task dispatcher ────────────────────────────────────────────────────

def handle_tradingview_task(task_type: str,
                             payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Called by bridge._dispatch_task_real() on the Worker PC.
    Returns a dict with at minimum {"status": "ok"|"error", "source": "tradingview_mcp"}.

    task_type values:
        tradingview_status        check MCP process health
        tradingview_analyze       AI chart analysis + signal
        tradingview_chart_data    raw OHLCV + indicator data
        tradingview_pine          run / save / list Pine Scripts
        tradingview_alert         create or list TradingView alerts
        tradingview_screenshot    capture current chart image
        tradingview_set_symbol    switch chart to symbol / timeframe
    """
    bridge   = TradingViewMCPBridge()
    symbol   = payload.get("symbol", "BTCUSDT")
    interval = payload.get("interval", "1H")

    if task_type == "tradingview_status":
        available = bridge.is_available()
        return {
            "status":        "ok" if available else "degraded",
            "mcp_available": available,
            "mcp_dir":       _mcp_dir(),
            "mcp_entry":     _mcp_entry(),
            "source":        "tradingview_mcp",
        }

    if not bridge.is_available():
        return {
            "status": "error",
            "error":  (
                "TradingView MCP process could not start. "
                "Run on Worker PC: cd ~/tradingview-mcp && npm install && npm run build "
                "and make sure TradingView Desktop is open."
            ),
            "source": "tradingview_mcp",
        }

    if task_type == "tradingview_analyze":
        result = bridge.analyze_chart(
            symbol, interval,
            payload.get("question", "Analyze trend and give a signal."),
        )
        return {"status": "ok", "source": "tradingview_mcp",
                "symbol": symbol, "interval": interval, "analysis": result}

    if task_type == "tradingview_chart_data":
        result = bridge.get_chart_data(symbol, interval)
        return {"status": "ok", "source": "tradingview_mcp",
                "symbol": symbol, "interval": interval, "chart": result}

    if task_type == "tradingview_pine":
        action = payload.get("action", "run")
        if action == "run":
            script = payload.get("script", "")
            if not script:
                return {"status": "error",
                        "error": "payload.script is required for action=run",
                        "source": "tradingview_mcp"}
            result = bridge.run_pine_script(script, symbol)
        elif action == "save":
            result = bridge.save_pine_script(
                payload.get("name", "AENIDA_script"), payload.get("script", ""))
        elif action == "list":
            result = bridge.list_pine_scripts()
        else:
            return {"status": "error",
                    "error": f"Unknown pine action '{action}'. Use: run|save|list",
                    "source": "tradingview_mcp"}
        return {"status": "ok", "source": "tradingview_mcp",
                "action": action, "result": result}

    if task_type == "tradingview_alert":
        action = payload.get("action", "list")
        if action == "create":
            result = bridge.create_alert(
                symbol,
                payload.get("condition", "crossing_up"),
                float(payload.get("price", 0.0)),
                payload.get("message", ""),
            )
        else:
            result = bridge.list_alerts()
        return {"status": "ok", "source": "tradingview_mcp",
                "action": action, "result": result}

    if task_type == "tradingview_screenshot":
        result = bridge.screenshot_chart(payload.get("save_path"))
        return {"status": "ok", "source": "tradingview_mcp", "result": result}

    if task_type == "tradingview_set_symbol":
        result = bridge.set_symbol(symbol, interval)
        return {"status": "ok", "source": "tradingview_mcp",
                "symbol": symbol, "interval": interval, "result": result}

    return {"status": "error",
            "error":  f"Unknown tradingview task_type: '{task_type}'",
            "source": "tradingview_mcp"}


# ── MCPServerManager (optional pre-warm helper) ───────────────────────────────

class MCPServerManager:
    """Pre-warm the MCP process at Worker boot time."""

    def start(self) -> bool:
        try:
            return _get_process().is_alive()
        except Exception as exc:
            logger.error("[TV_MCP] Manager.start failed: %s", exc)
            return False

    def stop(self) -> None:
        global _process
        with _process_lock:
            if _process:
                _process.stop()
                _process = None

    def is_running(self) -> bool:
        with _process_lock:
            return _process is not None and _process.is_alive()

    def wait_ready(self, timeout_s: float = 10.0) -> bool:
        """BUG-TV-4 fix: poll-based wait replaces hardcoded sleep(3)."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.is_running():
                return True
            time.sleep(0.25)
        return False


def get_manager() -> MCPServerManager:
    return MCPServerManager()
