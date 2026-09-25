"""
AENIDA Cognition Router
Two-tier intelligence routing:
  Tier 1 — Reflex Layer: cosine similarity < 50ms, < 100MB RAM
  Tier 2 — Worker offload: when similarity < threshold

Self-tuning threshold (BUG-14 fix): starts 0.72, adjusts weekly.
Circuit breaker Limp Mode: Worker unreachable → Safe Default < 50ms.
"""

import json
import math
import logging
import os
import time
import threading
from typing import Dict, Any, List, Optional, Tuple


class ReflexRouter:
    """
    Cosine-similarity reflex matcher.
    __slots__ keeps RAM footprint minimal.
    """

    __slots__ = ["vectors", "templates", "labels",
                 "threshold", "_lock", "_limp_mode",
                 "_accuracy_hits", "_accuracy_total",
                 "_worker_load", "_last_tune"]

    def __init__(self, threshold: float = 0.72):
        self.vectors: List[List[float]] = []
        self.templates: List[str] = []
        self.labels: List[str] = []
        self.threshold: float = threshold
        self._lock = threading.Lock()
        self._limp_mode: bool = False
        self._accuracy_hits: int = 0
        self._accuracy_total: int = 0
        self._worker_load: float = 0.0
        self._last_tune: float = time.time()

    # ── Vector math ───────────────────────────────────────────────
    @staticmethod
    def _tokenize(text: str) -> Dict[str, int]:
        words = text.lower().split()
        freq: Dict[str, int] = {}
        for w in words:
            freq[w] = freq.get(w, 0) + 1
        return freq

    @staticmethod
    def _cosine(a: Dict[str, int], b: Dict[str, int]) -> float:
        keys = set(a) & set(b)
        if not keys:
            return 0.0
        dot = sum(a[k] * b[k] for k in keys)
        mag_a = math.sqrt(sum(v ** 2 for v in a.values()))
        mag_b = math.sqrt(sum(v ** 2 for v in b.values()))
        if mag_a == 0 or mag_b == 0:
            return 0.0
        return dot / (mag_a * mag_b)

    # ── Library loading ───────────────────────────────────────────
    def load_reflex_library(self, path: str) -> None:
        """Load reflex templates from JSON file.
        Supports both list format and {"templates": [...]} dict format.
        """
        if not os.path.exists(path):
            logging.warning(f"[COGNITION] Reflex library not found: {path}")
            return
        with open(path) as f:
            raw = json.load(f)
        # Handle both formats
        if isinstance(raw, dict) and "templates" in raw:
            entries = raw["templates"]
        elif isinstance(raw, list):
            entries = raw
        else:
            entries = []
        with self._lock:
            self.vectors.clear()
            self.templates.clear()
            self.labels.clear()
            for entry in entries:
                if isinstance(entry, dict):
                    text = entry.get("template_text", "")
                    label = entry.get("label", "unknown")
                else:
                    text = str(entry)
                    label = "unknown"
                self.templates.append(text)
                self.labels.append(label)
                self.vectors.append(self._tokenize(text))
        logging.info(f"[COGNITION] Loaded {len(self.labels)} reflex templates")

    # ── Matching ──────────────────────────────────────────────────
    def match_reflex(self, text: str) -> Tuple[str, float]:
        """
        Find best cosine match for input text.
        Returns (label, similarity_score).
        """
        if not self.vectors:
            return "unknown", 0.0
        query = self._tokenize(text)
        best_label = "unknown"
        best_score = 0.0
        with self._lock:
            for label, vec in zip(self.labels, self.vectors):
                score = self._cosine(query, vec)
                if score > best_score:
                    best_score = score
                    best_label = label
        return best_label, best_score

    # ── Threshold self-tuning (BUG-14) ────────────────────────────
    def tune_threshold(self) -> float:
        """
        Adjust threshold weekly:
          accuracy < 80% → raise by 0.02
          worker_load > 70% → lower by 0.01
          Bounds: 0.60 – 0.90
        """
        now = time.time()
        if now - self._last_tune < 7 * 86400:
            return self.threshold  # not time yet

        with self._lock:
            if self._accuracy_total > 0:
                accuracy = self._accuracy_hits / self._accuracy_total
                if accuracy < 0.80:
                    self.threshold = min(0.90, self.threshold + 0.02)
                    logging.info(f"[COGNITION] Threshold raised to {self.threshold:.2f}")
            if self._worker_load > 0.70:
                self.threshold = max(0.60, self.threshold - 0.01)
                logging.info(f"[COGNITION] Threshold lowered to {self.threshold:.2f}")
            self._last_tune = now
            self._accuracy_hits = 0
            self._accuracy_total = 0

        return self.threshold

    def record_accuracy(self, correct: bool) -> None:
        with self._lock:
            self._accuracy_total += 1
            if correct:
                self._accuracy_hits += 1

    def set_worker_load(self, load_fraction: float) -> None:
        self._worker_load = load_fraction

    # ── Circuit breaker ───────────────────────────────────────────
    def enter_limp_mode(self) -> None:
        if not self._limp_mode:
            self._limp_mode = True
            logging.warning("[COGNITION] Entering Limp Mode — Worker unreachable")

    def exit_limp_mode(self) -> None:
        if self._limp_mode:
            self._limp_mode = False
            logging.info("[COGNITION] Exited Limp Mode")

    def is_limp(self) -> bool:
        return self._limp_mode


class CognitionRouter:
    """
    Full two-tier router.
    Tier 1: local reflex match (< 50ms)
    Tier 2: Worker offload (when similarity < threshold)
    """

    __slots__ = ["_reflex", "_lib_path"]

    def __init__(self, lib_path: str = "data/reflex_library.json",
                 threshold: float = 0.72):
        self._reflex = ReflexRouter(threshold=threshold)
        self._lib_path = lib_path
        self._reflex.load_reflex_library(lib_path)

    def route(self, task: str,
              context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Route a task:
          similarity ≥ threshold → local reflex
          similarity <  threshold → Worker offload
          Worker unreachable      → Limp Mode safe default
        """
        self._reflex.tune_threshold()

        label, score = self._reflex.match_reflex(task)

        if score >= self._reflex.threshold and not self._reflex.is_limp():
            # Tier 1 — local
            return {
                "tier": 1,
                "label": label,
                "score": round(score, 4),
                "mode": "local_reflex",
                "task": task,
            }

        # Tier 2 — try offload
        if self._reflex.is_limp():
            return self._safe_default(task, label)

        result = self.offload_to_worker(task, context or {})
        if result.get("error"):
            self._reflex.enter_limp_mode()
            return self._safe_default(task, label)

        self._reflex.exit_limp_mode()
        return result

    def offload_to_worker(self, task: str,
                          context: Dict[str, Any]) -> Dict[str, Any]:
        """Send task to Worker for deep analysis."""
        try:
            from context_harvester import harvest_minimal
            snapshot = harvest_minimal()
        except Exception:
            snapshot = {"timestamp": time.time()}

        payload = {"task": task, "context": {**context, **snapshot},
                   "tier": 2}

        try:
            from bridge import send_task, check_worker_online
            if not check_worker_online():
                return {"error": "worker_offline", "tier": 2}
            result = send_task(payload)
            result["tier"] = 2
            result["mode"] = "worker_offload"
            return result
        except Exception as e:
            logging.error(f"[COGNITION] offload failed: {e}")
            return {"error": str(e), "tier": 2}

    def _safe_default(self, task: str, label: str) -> Dict[str, Any]:
        """Limp mode — queue or basic local response."""
        return {
            "tier": 1,
            "label": label or "queue_task",
            "score": 0.0,
            "mode": "limp_mode_safe_default",
            "task": task,
            "message": "Worker unreachable — task queued for later",
        }

    def check_memory_guard(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        """
        If plan's estimated RAM > free RAM → prune steps.
        """
        try:
            import psutil
            free_mb = psutil.virtual_memory().available / 1e6
            plan_mb = plan.get("estimated_ram_mb", 0)
            if plan_mb > free_mb:
                steps = plan.get("steps", [])
                # Drop lowest-priority steps
                steps_sorted = sorted(steps,
                                      key=lambda s: s.get("priority", 5),
                                      reverse=True)
                plan["steps"] = steps_sorted[:max(1, len(steps_sorted) // 2)]
                plan["pruned"] = True
                logging.warning(f"[COGNITION] Memory guard pruned plan "
                                f"({plan_mb:.0f}MB > {free_mb:.0f}MB free)")
        except Exception:
            pass
        return plan


# ── Global instance ───────────────────────────────────────────────
_router: Optional[CognitionRouter] = None


def get_router() -> CognitionRouter:
    global _router
    if _router is None:
        _router = CognitionRouter()
    return _router


def route(task: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return get_router().route(task, context)


def offload_to_worker(task: str, context: Dict[str, Any]) -> Dict[str, Any]:
    return get_router().offload_to_worker(task, context)


def enter_limp_mode() -> None:
    get_router()._reflex.enter_limp_mode()


def exit_limp_mode() -> None:
    get_router()._reflex.exit_limp_mode()


def tune_threshold() -> float:
    return get_router()._reflex.tune_threshold()
