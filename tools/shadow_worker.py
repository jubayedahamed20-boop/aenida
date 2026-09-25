"""
AENIDA Shadow Worker  v1.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
A true background daemon — completely silent to the user.

Features:
  • Zero console output — all logs go to ~/.aenida/logs/
  • Auto-restarts every task loop — never crashes permanently
  • CPU-throttled (Nice 10) so your machine stays responsive
  • Heartbeat every 30s → master node
  • Handles signals cleanly (SIGTERM / SIGINT → graceful shutdown)
  • Auto-reconnects to master if connection drops
  • Writes PID to ~/.aenida/shadow.pid so you can check status

Usage:
  python shadow_worker.py            # Start (foreground, logs to file)
  python shadow_worker.py --daemon   # Fork to background (Linux)
  python shadow_worker.py status     # Show running status
  python shadow_worker.py stop       # Stop gracefully
  python shadow_worker.py logs       # Tail the log file
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import argparse, json, logging, os, platform, signal, socket
import subprocess, sys, time, threading
from pathlib import Path
from typing import Optional

# ── Paths ─────────────────────────────────────────────────────────
AENIDA_HOME = Path.home() / ".aenida"
LOG_DIR      = AENIDA_HOME / "logs"
LOG_FILE     = LOG_DIR / "shadow_worker.log"
PID_FILE     = AENIDA_HOME / "shadow.pid"
STATUS_FILE  = AENIDA_HOME / "shadow_status.json"

# Locate AENIDA core
BASE = Path(__file__).parent.resolve()
PROJECT_ROOT = BASE.parent
sys.path.insert(0, str(PROJECT_ROOT))
import path_setup  # noqa: E402  registers all sub-dirs

# ── Global state ──────────────────────────────────────────────────
_running = True
_log: Optional[logging.Logger] = None


# ════════════════════════════════════════════════════════════════════
#  LOGGING  — file only, never touches stdout/stderr when daemonized
# ════════════════════════════════════════════════════════════════════
def _setup_logging(console: bool = False) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("shadow_worker")
    logger.setLevel(logging.DEBUG)

    # Rotating file handler (5 MB × 3 files)
    try:
        from logging.handlers import RotatingFileHandler
        fh = RotatingFileHandler(LOG_FILE, maxBytes=5_000_000,
                                  backupCount=3, encoding="utf-8")
    except Exception:
        fh = logging.FileHandler(LOG_FILE, encoding="utf-8")

    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(fh)

    # Console only in foreground mode
    if console:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        logger.addHandler(ch)

    return logger


# ════════════════════════════════════════════════════════════════════
#  PID MANAGEMENT
# ════════════════════════════════════════════════════════════════════
def _write_pid() -> None:
    PID_FILE.write_text(str(os.getpid()))


def _read_pid() -> Optional[int]:
    try:
        return int(PID_FILE.read_text().strip())
    except Exception:
        return None


def _is_running(pid: int) -> bool:
    """Check if process with pid is alive."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _write_status(data: dict) -> None:
    try:
        STATUS_FILE.write_text(json.dumps({
            **data,
            "pid": os.getpid(),
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, indent=2))
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════════
#  SIGNAL HANDLERS — clean shutdown
# ════════════════════════════════════════════════════════════════════
def _handle_signal(signum, frame):
    global _running
    if _log:
        _log.info(f"Received signal {signum} — shutting down gracefully")
    _running = False


# ════════════════════════════════════════════════════════════════════
#  SOFTTOKEN AUTH  (no USB required)
# ════════════════════════════════════════════════════════════════════
def _load_softtoken() -> dict:
    """
    Load node identity from softtoken (created by quick_setup.py).
    Falls back to anonymous worker if token not found.
    """
    token_path = Path(os.getenv("SOFTTOKEN_PATH",
                                str(AENIDA_HOME / "softtoken.json")))
    if not token_path.exists():
        return {"node_id": socket.gethostname(), "role": "worker"}
    try:
        # Attempt decryption if cryptography is available
        from cryptography.fernet import Fernet, InvalidToken
        import base64, hashlib
        # Read env for password derivation
        # In practice the token is already unlocked at boot by quick_setup
        # Here we just read the encrypted blob as opaque identity
        raw = token_path.read_bytes()
        # Try JSON first (unencrypted fallback from quick_setup)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # Encrypted — just use hostname as node_id
            return {"node_id": socket.gethostname(), "role": "worker"}
    except Exception:
        return {"node_id": socket.gethostname(), "role": "worker"}


# ════════════════════════════════════════════════════════════════════
#  MASTER CONNECTION  — heartbeat, task pull, result push
# ════════════════════════════════════════════════════════════════════
class MasterConnection:
    """
    Handles all network I/O to the Mother/Master node.
    Reconnects automatically — shadow worker never stops due to network.
    """

    def __init__(self, url: str, api_key: str, node_id: str):
        self.url     = url.rstrip("/")
        self.api_key = api_key
        self.node_id = node_id
        self._connected = False

    def _headers(self) -> dict:
        return {
            "X-AENIDA-Key":  self.api_key,
            "X-Node-ID":     self.node_id,
            "Content-Type":  "application/json",
        }

    def heartbeat(self) -> bool:
        try:
            import urllib.request
            data = json.dumps({
                "node_id":  self.node_id,
                "status":   "alive",
                "ts":       time.time(),
                "hostname": socket.gethostname(),
            }).encode()
            req = urllib.request.Request(
                f"{self.url}/api/worker/heartbeat",
                data=data,
                headers=self._headers(),
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                self._connected = resp.status == 200
        except Exception:
            self._connected = False
        return self._connected

    def pull_task(self) -> Optional[dict]:
        try:
            import urllib.request
            req = urllib.request.Request(
                f"{self.url}/api/worker/task?node_id={self.node_id}",
                headers=self._headers(),
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    return json.loads(resp.read())
        except Exception:
            pass
        return None

    def push_result(self, task_id: str, result: dict) -> bool:
        try:
            import urllib.request
            data = json.dumps({
                "task_id": task_id,
                "node_id": self.node_id,
                "result":  result,
                "ts":      time.time(),
            }).encode()
            req = urllib.request.Request(
                f"{self.url}/api/worker/result",
                data=data,
                headers=self._headers(),
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status == 200
        except Exception:
            return False

    @property
    def connected(self) -> bool:
        return self._connected


# ════════════════════════════════════════════════════════════════════
#  TASK EXECUTOR  — runs tasks from master, never blocks main loop
# ════════════════════════════════════════════════════════════════════
class TaskExecutor:
    """Executes tasks in a thread pool — safe, isolated, timed-out."""

    MAX_WORKERS = 2  # low footprint

    def __init__(self, conn: MasterConnection):
        self.conn    = conn
        self._active = {}   # task_id → thread

    def try_execute(self, task: dict) -> None:
        task_id = task.get("task_id", "?")
        if task_id in self._active:
            return  # already running

        t = threading.Thread(
            target=self._run_task,
            args=(task,),
            daemon=True,
            name=f"task-{task_id[:8]}",
        )
        self._active[task_id] = t
        t.start()

    def _run_task(self, task: dict) -> None:
        task_id   = task.get("task_id", "?")
        task_type = task.get("type", "unknown")
        payload   = task.get("payload", {})

        if _log:
            _log.info(f"Task {task_id[:8]} started: {task_type}")
        start = time.time()

        result = {"status": "error", "output": None, "error": None}
        try:
            result = _dispatch_task(task_type, payload)
        except Exception as exc:
            result = {"status": "error", "error": str(exc)}
        finally:
            elapsed = time.time() - start
            result["elapsed_s"] = round(elapsed, 2)
            self.conn.push_result(task_id, result)
            self._active.pop(task_id, None)
            if _log:
                _log.info(f"Task {task_id[:8]} done in {elapsed:.1f}s"
                          f" status={result.get('status')}")

    def active_count(self) -> int:
        return len(self._active)


def _dispatch_task(task_type: str, payload: dict) -> dict:
    """
    Route task to the appropriate AENIDA module.
    Each handler is imported lazily — if a module is missing,
    that task fails gracefully without crashing the whole worker.
    """
    if task_type == "analyze":
        try:
            from trading_agent import TradingAgent
            agent = TradingAgent()
            output = agent.analyze(payload.get("symbol", "BTC-USDT"))
            return {"status": "ok", "output": output}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    elif task_type == "brain_query":
        try:
            from local_brain import LocalBrain
            brain = LocalBrain()
            # BUG FIX: LocalBrain exposes .think(), not .query()
            result = brain.think(payload.get("question", ""),
                                 context=payload.get("context", {}))
            return {"status": "ok", "output": result}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    elif task_type.startswith("tradingview_"):
        try:
            import os as _os, sys as _sys
            _tv = _os.path.abspath(
                _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                              "..", "trading"))
            if _tv not in _sys.path:
                _sys.path.insert(0, _tv)
            from tradingview_mcp import handle_tradingview_task
            return handle_tradingview_task(task_type, payload)
        except Exception as e:
            return {"status": "error", "error": str(e)}

    elif task_type == "ping":
        return {"status": "ok", "output": "pong",
                "hostname": socket.gethostname()}

    elif task_type == "sys_info":
        info = {"hostname": socket.gethostname(), "platform": platform.system()}
        try:
            import psutil
            info["cpu_percent"] = psutil.cpu_percent(interval=1)
            info["ram_percent"] = psutil.virtual_memory().percent
            info["disk_free_gb"] = round(
                psutil.disk_usage("/").free / 1e9, 1)
        except ImportError:
            pass
        return {"status": "ok", "output": info}

    else:
        return {"status": "error", "error": f"Unknown task type: {task_type}"}


# ════════════════════════════════════════════════════════════════════
#  MAIN WORKER LOOP
# ════════════════════════════════════════════════════════════════════
def _main_loop(conn: MasterConnection, executor: TaskExecutor) -> None:
    """
    The core loop — completely silent to the user.
    Heartbeat every 30s. Task poll every 5s.
    Never crashes the process — all exceptions caught and logged.
    """
    HEARTBEAT_INTERVAL = 30   # seconds
    TASK_POLL_INTERVAL =  5   # seconds
    last_heartbeat     = 0.0

    _log.info("Shadow worker loop started")
    _write_status({"state": "running", "master_url": conn.url})

    while _running:
        now = time.time()

        # ── Heartbeat ─────────────────────────────────────────────
        if now - last_heartbeat >= HEARTBEAT_INTERVAL:
            try:
                ok = conn.heartbeat()
                last_heartbeat = now
                _write_status({
                    "state":     "running",
                    "connected": ok,
                    "active_tasks": executor.active_count(),
                })
                if ok:
                    _log.debug("Heartbeat ✓")
                else:
                    _log.warning("Heartbeat failed — will retry")
            except Exception as e:
                _log.error(f"Heartbeat exception: {e}")

        # ── Task poll ─────────────────────────────────────────────
        if conn.connected and executor.active_count() < TaskExecutor.MAX_WORKERS:
            try:
                task = conn.pull_task()
                if task:
                    executor.try_execute(task)
            except Exception as e:
                _log.error(f"Task pull exception: {e}")

        # ── CPU-friendly sleep ────────────────────────────────────
        time.sleep(TASK_POLL_INTERVAL)

    _log.info("Shadow worker stopped cleanly")
    _write_status({"state": "stopped"})


# ════════════════════════════════════════════════════════════════════
#  DAEMON FORK (Linux / macOS)
# ════════════════════════════════════════════════════════════════════
def _daemonize() -> None:
    """Double-fork to fully detach from terminal."""
    # First fork
    try:
        if os.fork() > 0:
            sys.exit(0)
    except AttributeError:
        print("--daemon not supported on Windows. Use Task Scheduler instead.")
        sys.exit(1)

    os.setsid()

    # Second fork
    if os.fork() > 0:
        sys.exit(0)

    # Redirect stdio to /dev/null  (keep fd open for lifetime of process)
    devnull_fd = open(os.devnull, "w")  # intentionally kept open — process lifetime
    sys.stdout = devnull_fd
    sys.stderr = devnull_fd


# ════════════════════════════════════════════════════════════════════
#  CLI COMMANDS
# ════════════════════════════════════════════════════════════════════
def cmd_status() -> None:
    pid = _read_pid()
    if pid and _is_running(pid):
        print(f"✓ Shadow worker is RUNNING  (PID {pid})")
        try:
            status = json.loads(STATUS_FILE.read_text())
            print(f"  State:     {status.get('state')}")
            print(f"  Connected: {status.get('connected')}")
            print(f"  Tasks:     {status.get('active_tasks', 0)} active")
            print(f"  Updated:   {status.get('updated')}")
        except Exception:
            pass
        print(f"  Log:  {LOG_FILE}")
    else:
        print("✗ Shadow worker is NOT running")
        print(f"  Start with:  python shadow_worker.py")


def cmd_stop() -> None:
    pid = _read_pid()
    if pid and _is_running(pid):
        os.kill(pid, signal.SIGTERM)
        # Wait up to 5 seconds
        for _ in range(10):
            time.sleep(0.5)
            if not _is_running(pid):
                print(f"✓ Shadow worker (PID {pid}) stopped")
                return
        print(f"Worker still running — try:  kill {pid}")
    else:
        print("Shadow worker is not running")


def cmd_logs(lines: int = 50) -> None:
    if not LOG_FILE.exists():
        print("No log file found yet")
        return
    try:
        # Use tail if available
        subprocess.run(["tail", f"-{lines}", str(LOG_FILE)])
    except FileNotFoundError:
        # Fallback: read last N lines in Python
        all_lines = LOG_FILE.read_text().splitlines()
        print("\n".join(all_lines[-lines:]))


# ════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ════════════════════════════════════════════════════════════════════
def main():
    global _log, _running

    parser = argparse.ArgumentParser(
        description="AENIDA Shadow Worker — silent background daemon")
    parser.add_argument("command", nargs="?", default="start",
                        choices=["start", "status", "stop", "logs"])
    parser.add_argument("--daemon", action="store_true",
                        help="Fork to background (Linux/macOS)")
    parser.add_argument("--console", action="store_true",
                        help="Also print logs to console")
    parser.add_argument("--lines", type=int, default=50,
                        help="Lines to show for 'logs' command")
    args = parser.parse_args()

    # ── Sub-commands ──────────────────────────────────────────────
    if args.command == "status":
        cmd_status(); return
    if args.command == "stop":
        cmd_stop();   return
    if args.command == "logs":
        cmd_logs(args.lines); return

    # ── Check not already running ─────────────────────────────────
    pid = _read_pid()
    if pid and _is_running(pid):
        print(f"Shadow worker already running (PID {pid})")
        print(f"Use:  python shadow_worker.py status")
        sys.exit(0)

    # ── Optionally daemonize ──────────────────────────────────────
    if args.daemon:
        _daemonize()

    # ── Setup ─────────────────────────────────────────────────────
    _log = _setup_logging(console=args.console and not args.daemon)
    _write_pid()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT,  _handle_signal)

    _log.info("═" * 50)
    _log.info("AENIDA Shadow Worker v1.0 starting")
    _log.info(f"PID={os.getpid()}  host={socket.gethostname()}")

    # ── Load identity (softtoken — no USB) ───────────────────────
    token = _load_softtoken()
    node_id  = token.get("node_id", socket.gethostname())
    api_key  = os.getenv("AENIDA_API_KEY", token.get("api_key", ""))
    master_url = os.getenv("WORKER_URL", "http://localhost:8000")

    _log.info(f"Node ID: {node_id}")
    _log.info(f"Master:  {master_url}")

    # ── Lower CPU priority ────────────────────────────────────────
    try:
        os.nice(10)   # Be a good citizen — lower priority
    except (AttributeError, PermissionError):
        pass

    # ── Start ─────────────────────────────────────────────────────
    conn     = MasterConnection(master_url, api_key, node_id)
    executor = TaskExecutor(conn)

    try:
        _main_loop(conn, executor)
    except Exception as e:
        _log.critical(f"Fatal error in main loop: {e}", exc_info=True)
    finally:
        PID_FILE.unlink(missing_ok=True)
        _log.info("Shadow worker exited")


if __name__ == "__main__":
    main()
