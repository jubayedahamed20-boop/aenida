"""
AENIDA Model Shell
Honest AI model picker with hardware awareness.
Dynamic capability map (BUG-26 fix): TTL=300s per provider.
Laptop NEVER runs local LLM — API-only.
"""

import time
import logging
import os
import threading
from typing import Dict, Any, Optional

# ── Capability map entry ──────────────────────────────────────────
class _ProviderEntry:
    __slots__ = ["available", "last_checked", "ttl", "fail_streak",
                 "banned_until", "location"]

    def __init__(self, available: bool, location: str, ttl: int = 300):
        self.available = available
        self.last_checked = time.time()
        self.ttl = ttl
        self.fail_streak = 0
        self.banned_until: float = 0.0
        self.location = location  # "api" | "worker" | "local"


class ModelShell:
    """
    Selects the right AI model for each task.
    Hardware scan runs once on init; capability map refreshes via TTL.
    """

    __slots__ = ["_cap", "_lock", "_hardware", "_timesfm_available"]

    def __init__(self):
        self._lock = threading.Lock()
        self._hardware: Dict[str, Any] = {}
        self._timesfm_available: bool = False
        self._cap: Dict[str, _ProviderEntry] = {}
        self._scan_hardware()
        self._build_capability_map()

    # ── Hardware scan ─────────────────────────────────────────────
    def _scan_hardware(self) -> None:
        """Scan once on startup. Laptop = API-only (4GB rule)."""
        try:
            import psutil
            ram_gb = psutil.virtual_memory().total / 1e9
            self._hardware["ram_gb"] = round(ram_gb, 1)
        except ImportError:
            self._hardware["ram_gb"] = 4.0  # assume worst case

        self._hardware["laptop_can_run_llm"] = False  # ALWAYS False (R02)

        # Check if Worker is reachable
        try:
            from bridge import check_worker_online
            worker_online = check_worker_online()
        except Exception:
            worker_online = False
        self._hardware["worker_online"] = worker_online

        # Check TimesFM on Worker
        if worker_online:
            try:
                from bridge import get_worker_status
                st = get_worker_status()
                self._timesfm_available = st.get("timesfm_loaded", False)
            except Exception:
                self._timesfm_available = False
        self._hardware["timesfm_available"] = self._timesfm_available

        logging.info(
            f"[MODEL_SHELL] Hardware: RAM={self._hardware['ram_gb']}GB "
            f"worker={self._hardware['worker_online']} "
            f"timesfm={self._timesfm_available}"
        )

    def _build_capability_map(self) -> None:
        """Build initial capability map from hardware scan."""
        with self._lock:
            self._cap = {
                "groq_small":    _ProviderEntry(True,  "api"),
                "groq_large":    _ProviderEntry(True,  "api"),
                "gemini_flash":  _ProviderEntry(True,  "api"),
                "together_ai":   _ProviderEntry(True,  "api"),
                "worker_qwen32": _ProviderEntry(
                    self._hardware.get("worker_online", False), "worker"),
                "worker_timesfm": _ProviderEntry(
                    self._timesfm_available, "worker"),
                "local_llm":     _ProviderEntry(
                    False, "local"),   # ALWAYS blocked on Laptop
                "rule_engine":   _ProviderEntry(True,  "local"),
            }

    # ── Provider health management (BUG-26) ──────────────────────
    def update_provider_health(self, provider: str, success: bool) -> None:
        """Update provider health. Ban after 3 consecutive failures."""
        with self._lock:
            entry = self._cap.get(provider)
            if entry is None:
                return
            if success:
                entry.fail_streak = 0
                entry.available = True
            else:
                entry.fail_streak += 1
                if entry.fail_streak >= 3:
                    entry.available = False
                    entry.banned_until = time.time() + 600  # 10 min
                    logging.warning(f"[MODEL_SHELL] Banned {provider} for 10 min")

    def _recheck_if_needed(self, provider: str) -> None:
        """Recheck availability if TTL expired."""
        entry = self._cap.get(provider)
        if entry is None:
            return
        now = time.time()
        if now - entry.last_checked < entry.ttl:
            return
        entry.last_checked = now
        # Auto-recover from ban if ban window expired
        if not entry.available and entry.banned_until and now > entry.banned_until:
            entry.available = True
            entry.fail_streak = 0
            logging.info(f"[MODEL_SHELL] Auto-recovered {provider}")

    # ── Model selection ───────────────────────────────────────────
    def select_model(self, task: str, context: Optional[Dict] = None) -> str:
        """
        Pick the best available model for a task type.
        Returns model key string.
        """
        task_l = task.lower()
        context = context or {}

        with self._lock:
            for name, entry in self._cap.items():
                self._recheck_if_needed(name)

            def ok(name: str) -> bool:
                e = self._cap.get(name)
                return bool(e and e.available)

            # Price forecast → Worker TimesFM
            if "forecast" in task_l or "predict" in task_l or "timesfm" in task_l:
                if ok("worker_timesfm"):
                    return "worker_timesfm"

            # Deep analysis / code → Worker Qwen-32B
            if any(k in task_l for k in ["deep_analysis", "code", "backtest",
                                          "strategy", "analyze_full"]):
                if ok("worker_qwen32"):
                    return "worker_qwen32"

            # Summarization → Gemini Flash
            if any(k in task_l for k in ["summarize", "summary", "news"]):
                if ok("gemini_flash"):
                    return "gemini_flash"

            # Chart / vision → Groq large (vision model)
            if any(k in task_l for k in ["chart", "vision", "image", "screenshot"]):
                if ok("groq_large"):
                    return "groq_large"

            # Speed-critical / simple → Groq small
            if ok("groq_small"):
                return "groq_small"

            # All APIs down — degraded options
            if ok("together_ai"):
                return "together_ai"

            return "rule_engine"

    def call(self, task: str, image=None,
             context: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Select model and call AI. Injects memory context automatically.
        Returns: {result, provider, degraded}
        """
        model = self.select_model(task, context)

        # Worker models → delegate via bridge
        if model in ("worker_qwen32", "worker_timesfm"):
            try:
                from bridge import send_task, check_worker_online
                if not check_worker_online():
                    logging.warning("[MODEL_SHELL] Worker offline — falling back to API")
                    self.update_provider_health(model, False)
                    model = self.select_model(task + " api_only", context)
                else:
                    result = send_task({
                        "task": task, "model": model,
                        "context": context or {}
                    })
                    success = not result.get("degraded", False)
                    self.update_provider_health(model, success)
                    return result
            except Exception as e:
                logging.error(f"[MODEL_SHELL] Worker call failed: {e}")
                self.update_provider_health(model, False)
                model = self.select_model("api_fallback", context)

        # API models → fallback_chain handles routing
        try:
            from fallback_chain import call_ai
            result = call_ai(task, image)
            provider = result.get("provider", model)
            success = not result.get("degraded", False)
            # Update health of the API we tried
            api_map = {
                "groq": "groq_small", "gemini": "gemini_flash",
                "together_ai": "together_ai"
            }
            self.update_provider_health(
                api_map.get(provider, model), success)
            return result
        except Exception as e:
            logging.error(f"[MODEL_SHELL] call failed: {e}")
            return {"result": "[Model shell error]", "provider": "none",
                    "degraded": True}

    def get_capability_map(self) -> Dict[str, Any]:
        """Return current capability map state."""
        with self._lock:
            return {
                name: {
                    "available": e.available,
                    "location": e.location,
                    "fail_streak": e.fail_streak,
                    "banned_until": e.banned_until,
                    "last_checked": e.last_checked,
                }
                for name, e in self._cap.items()
            }

    def scan_hardware(self) -> Dict[str, Any]:
        """Re-run hardware scan and rebuild map."""
        self._scan_hardware()
        self._build_capability_map()
        return self._hardware.copy()


# ── Global instance ───────────────────────────────────────────────
_shell: Optional[ModelShell] = None
_shell_lock = threading.Lock()


def get_shell() -> ModelShell:
    global _shell
    if _shell is None:
        with _shell_lock:
            if _shell is None:
                _shell = ModelShell()
    return _shell


def select_model(task: str, context: Optional[Dict] = None) -> str:
    return get_shell().select_model(task, context)


def call(task: str, image=None, context: Optional[Dict] = None) -> Dict[str, Any]:
    return get_shell().call(task, image, context)


def update_provider_health(provider: str, success: bool) -> None:
    get_shell().update_provider_health(provider, success)


def get_capability_map() -> Dict[str, Any]:
    return get_shell().get_capability_map()


def scan_hardware() -> Dict[str, Any]:
    return get_shell().scan_hardware()
