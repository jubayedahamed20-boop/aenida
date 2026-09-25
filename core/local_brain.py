"""
AENIDA Local Brain
Lightweight AI that runs on the laptop (4GB RAM) when
the Worker PC is offline or unreachable.

Capabilities when Worker is OFFLINE:
  - Rule-based intent parsing (no LLM needed)
  - Technical indicator analysis (pure math, < 20MB RAM)
  - Simple trade signal generation
  - Memory search (reads .mv2 file)
  - News summarisation (keyword-based fallback)
  - Task queuing for when Worker comes back

This is NOT as powerful as the Worker's Qwen-32B.
It is the survival brain — keeps you running 24/7.

When Worker comes BACK ONLINE:
  - Offloads all queued tasks
  - Switches to full-power Worker AI
  - Syncs any decisions made locally
"""

import logging
import os
import time
from typing import Any, Dict, List, Optional

# ── Capability check ──────────────────────────────────────────────
CAPABILITIES = {
    "technical_indicators": True,   # Pure Python math — always works
    "market_data":          True,   # Free APIs — always works
    "memory_search":        True,   # Local .mv2 file — always works
    "rule_intent":          True,   # No model needed
    "news_keywords":        True,   # No model needed
    "llm_groq":             False,  # Needs internet + API key
    "llm_gemini":           False,  # Needs internet + API key
    "worker_qwen32":        False,  # Worker is OFFLINE (that's why we're here)
    "timesfm":              False,  # Lives on Worker
    "deep_analysis":        False,  # Too RAM-heavy for 4GB
}


class LocalBrain:
    """
    Self-contained reasoning engine for the 4GB laptop.
    Falls back gracefully on every failure.
    """

    __slots__ = ["_worker_online", "_last_worker_check",
                 "_local_decisions", "_pending_worker_tasks"]

    WORKER_CHECK_INTERVAL = 30  # seconds

    def __init__(self):
        self._worker_online: bool = False
        self._last_worker_check: float = 0
        self._local_decisions: List[Dict] = []
        self._pending_worker_tasks: List[Dict] = []

    # ── Worker status ─────────────────────────────────────────────
    def check_worker(self) -> bool:
        """Check if Worker PC is online. Cached for 30s."""
        now = time.time()
        if now - self._last_worker_check < self.WORKER_CHECK_INTERVAL:
            return self._worker_online
        self._last_worker_check = now
        try:
            from bridge import check_worker_online
            self._worker_online = check_worker_online()
        except Exception:
            self._worker_online = False
        return self._worker_online

    def is_worker_online(self) -> bool:
        return self._worker_online

    # ── Core think() loop ─────────────────────────────────────────
    def think(self, task: str,
              context: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Main reasoning entry point.
        Tries Worker first; falls back to local brain.

        Decision flow:
          Input → Check Worker → Route appropriately
               → Analyze (local or remote)
               → Decide (signal, action, answer)
               → Store in memory
               → Return result
        """
        context = context or {}
        worker_up = self.check_worker()

        if worker_up:
            # Full power — delegate to Worker
            result = self._delegate_to_worker(task, context)
            if not result.get("error"):
                self._store_decision(task, result, source="worker")
                return result
            # Worker failed mid-call — fall through to local
            logging.warning("[LOCAL_BRAIN] Worker call failed — using local")

        # Local brain takes over
        result = self._think_locally(task, context)
        self._store_decision(task, result, source="local")

        # Queue for Worker when it comes back (if complex)
        if result.get("needs_worker"):
            self._queue_for_worker(task, context)

        return result

    # ── Local reasoning ───────────────────────────────────────────
    def _think_locally(self, task: str,
                       context: Dict) -> Dict[str, Any]:
        """
        Local reasoning pipeline:
          1. Parse intent (rules)
          2. Gather data (free APIs)
          3. Calculate signals (math)
          4. Search memory (past decisions)
          5. Generate answer
        """
        task_lower = task.lower()

        # Step 1 — Intent
        intent = self._parse_intent(task_lower)

        # Step 2 — Gather data if trading-related
        market_snapshot: Dict = {}
        signals: Dict = {}
        if intent in ("chart", "price", "signal", "trade", "analyze"):
            symbol = context.get("symbol", "BTC-USDT")
            market_snapshot = self._get_market_data(symbol)
            signals = self._calculate_signals(symbol)

        # Step 3 — Memory context
        memory_ctx = self._search_memory(task)

        # Step 4 — Build answer
        answer = self._build_answer(
            intent, task, market_snapshot, signals, memory_ctx)

        return {
            "result": answer,
            "intent": intent,
            "provider": "local_brain",
            "degraded": True,
            "signals": signals,
            "market": market_snapshot,
            "memory_context": bool(memory_ctx),
            "needs_worker": intent in ("deep_analysis", "backtest",
                                       "code", "write", "strategy_create"),
        }

    def _parse_intent(self, task: str) -> str:
        """Rule-based intent classification (no LLM needed)."""
        rules = [
            (["buy", "sell", "enter", "exit",
              "long", "short"], "trade"),          # trade first (specific)
            (["chart", "trend", "bullish", "bearish",
              "candlestick", "indicator"], "chart"),
            (["price", "how much", "current", "cost"], "price"),
            (["news", "latest", "happening", "update"], "news"),
            (["backtest", "historical", "test strategy"], "backtest"),
            (["trade", "signal", "buy signal", "sell signal"], "trade"),
            (["analyze", "analysis", "review", "evaluate"], "analyze"),
            (["code", "write", "implement", "build", "create"], "code"),
            (["memory", "remember", "past", "history", "last time"], "memory"),
            (["status", "health", "running", "online",
              "connected"], "status"),
            (["help", "what can", "commands"], "help"),
        ]
        for keywords, intent in rules:
            if any(k in task for k in keywords):
                return intent
        return "chat"

    def _get_market_data(self, symbol: str) -> Dict:
        """Pull real price data (free APIs, always available)."""
        try:
            from market_data import get_current_price, get_market_snapshot
            price = get_current_price(symbol)
            snapshot = get_market_snapshot([symbol])
            return {"price": price, **snapshot.get(symbol, {})}
        except Exception as e:
            logging.debug(f"[LOCAL_BRAIN] market_data: {e}")
            return {}

    def _calculate_signals(self, symbol: str) -> Dict:
        """Run technical indicators (pure Python math)."""
        try:
            from market_data import fetch_crypto_ohlcv
            from technical_indicators import generate_signals
            ohlcv = fetch_crypto_ohlcv(symbol, "1H", 60)
            if not ohlcv:
                return {}
            data = [{"open": c.open, "high": c.high, "low": c.low,
                     "close": c.close, "volume": c.volume} for c in ohlcv]
            return generate_signals(symbol, data)
        except Exception as e:
            logging.debug(f"[LOCAL_BRAIN] signals: {e}")
            return {}

    def _search_memory(self, task: str) -> str:
        """Search local .mv2 memory for relevant past decisions."""
        try:
            from memory_layer import get_context_for_task
            return get_context_for_task(task, max_tokens=500)
        except Exception:
            return ""

    def _build_answer(self, intent: str, task: str,
                      market: Dict, signals: Dict,
                      memory: str) -> str:
        """Compose answer from available local data."""

        if intent == "price":
            price = market.get("price")
            if price:
                return f"Current price: ${price:,.2f}"
            return "Price unavailable — market data connection issue."

        if intent in ("chart", "signal", "trade", "analyze"):
            if not signals or signals.get("status") != "ok":
                return ("Collecting market data — need more candles. "
                        "Come back in a few minutes.")
            rec = signals.get("recommended", "HOLD")
            strength = signals.get("signal_strength", 50)
            rsi = signals.get("rsi", "N/A")
            trend = signals.get("trend", "unknown")
            price = market.get("price", signals.get("price", "N/A"))
            answer = (
                f"[LOCAL ANALYSIS — Worker offline]\n"
                f"Price:     ${price:,.2f}" if isinstance(price, (int,float))
                else f"[LOCAL ANALYSIS — Worker offline]\nPrice: {price}"
            )
            answer += (
                f"\nTrend:     {trend}"
                f"\nRSI:       {rsi}"
                f"\nSignal:    {rec} ({strength:.0f}% strength)"
                f"\nNote:      Deep AI analysis available when Worker PC reconnects."
            )
            if memory:
                answer += f"\n\nMemory context:\n{memory[:200]}"
            return answer

        if intent == "news":
            try:
                from news_watcher import get_recent
                items = get_recent(5)
                if items:
                    headlines = "\n".join(
                        f"  • {i['title'][:80]}" for i in items[:5])
                    return f"Recent news:\n{headlines}"
            except Exception:
                pass
            return "News cache empty — fetching in background."

        if intent == "status":
            return self.get_status_text()

        if intent == "memory":
            ctx = self._search_memory(task)
            return ctx if ctx else "No relevant memories found."

        if intent == "help":
            return (
                "AENIDA Local Brain — Commands:\n"
                "  price BTC         — current price\n"
                "  analyze BTC       — technical signals\n"
                "  news              — latest crypto news\n"
                "  status            — system health\n"
                "  memory <topic>    — search past decisions\n"
                "\nNote: Worker PC is offline. Deep analysis queued."
            )

        # Default — try free APIs
        try:
            from fallback_chain import call_ai
            result = call_ai(task)
            if not result.get("degraded"):
                return result.get("result", "")
        except Exception:
            pass

        return (
            f"[Local Brain] Processed: {task[:60]}\n"
            "Worker PC is offline — complex analysis queued.\n"
            "Basic signals available. Type 'status' for details."
        )

    # ── Worker delegation ─────────────────────────────────────────
    def _delegate_to_worker(self, task: str,
                             context: Dict) -> Dict[str, Any]:
        """Send task to Worker PC for full AI processing."""
        try:
            from bridge import send_task
            from context_harvester import harvest_minimal
            snapshot = harvest_minimal()
            return send_task({
                "task": task,
                "context": {**context, **snapshot},
                "source": "local_brain",
            })
        except Exception as e:
            return {"error": str(e), "degraded": True}

    def _queue_for_worker(self, task: str, context: Dict) -> None:
        """Queue task for Worker when it comes back online."""
        self._pending_worker_tasks.append({
            "task": task,
            "context": context,
            "queued_at": time.time(),
        })
        # Also persist to task_queue.db
        try:
            from task_queue import enqueue
            enqueue("worker_delegated", {"task": task, "context": context},
                    scheduled_for=time.time())
        except Exception:
            pass

    def flush_pending_to_worker(self) -> int:
        """When Worker comes back, send all queued tasks."""
        if not self.check_worker():
            return 0
        sent = 0
        for item in list(self._pending_worker_tasks):
            result = self._delegate_to_worker(
                item["task"], item["context"])
            if not result.get("error"):
                self._pending_worker_tasks.remove(item)
                sent += 1
        if sent:
            logging.info(f"[LOCAL_BRAIN] Flushed {sent} queued tasks to Worker")
        return sent

    # ── Memory storage ────────────────────────────────────────────
    def _store_decision(self, task: str,
                        result: Dict, source: str) -> None:
        """Store every decision in memory for future learning."""
        try:
            from memory_layer import remember
            remember({
                "type": "decision",
                "task": task[:200],
                "result_summary": str(result.get("result", ""))[:200],
                "intent": result.get("intent", ""),
                "provider": source,
                "timestamp": time.time(),
            })
        except Exception:
            pass
        # Keep local list too
        self._local_decisions.append({
            "task": task[:100],
            "source": source,
            "timestamp": time.time(),
        })
        if len(self._local_decisions) > 100:
            self._local_decisions = self._local_decisions[-100:]

    # ── Status ────────────────────────────────────────────────────
    def get_status_text(self) -> str:
        worker = "ONLINE ✓" if self._worker_online else "OFFLINE ✗"
        pending = len(self._pending_worker_tasks)
        decisions = len(self._local_decisions)
        try:
            from memory_monitor import get_memory_stats
            mem = get_memory_stats()
            ram = f"{mem.get('used_mb', 0):.0f}MB"
        except Exception:
            ram = "unknown"

        return (
            f"AENIDA Local Brain Status\n"
            f"  Worker PC:   {worker}\n"
            f"  Laptop RAM:  {ram}\n"
            f"  Decisions:   {decisions} made locally\n"
            f"  Queued:      {pending} waiting for Worker\n"
            f"  Capabilities: indicators✓ memory✓ "
            f"news✓ deep_AI={'✓' if self._worker_online else '✗'}"
        )

    def get_status_dict(self) -> Dict[str, Any]:
        return {
            "worker_online": self._worker_online,
            "pending_worker_tasks": len(self._pending_worker_tasks),
            "local_decisions_today": len(self._local_decisions),
            "capabilities": CAPABILITIES,
        }


# ── Global instance ───────────────────────────────────────────────
_brain: Optional[LocalBrain] = None


def get_brain() -> LocalBrain:
    global _brain
    if _brain is None:
        _brain = LocalBrain()
    return _brain


def think(task: str, context: Optional[Dict] = None) -> Dict[str, Any]:
    """Main entry point — always returns something useful."""
    return get_brain().think(task, context)


def check_worker() -> bool:
    return get_brain().check_worker()


def get_status() -> Dict[str, Any]:
    return get_brain().get_status_dict()


def flush_pending() -> int:
    return get_brain().flush_pending_to_worker()
