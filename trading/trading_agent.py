"""
AENIDA Trading Agent
Hybrid analysis: Real data (OKX/Binance, 70%) + AI Vision (30%).
BUG-39 fix: real numbers from API, visual patterns from screenshot.
BUG-36 fix: non-blocking approval card (threading.Event).
BUG-28 fix: price_history.db schema enforced.
"""

import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional


def _get_local_power_class() -> str:
    """Determine this machine's power class based on total RAM."""
    try:
        import psutil
        total_gb = psutil.virtual_memory().total / 1e9
        if total_gb >= 16:
            return "HIGH"
        elif total_gb >= 8:
            return "MEDIUM"
        else:
            return "LOW"
    except Exception:
        return "MEDIUM"

# Optional imports — degrade gracefully
try:
    from PIL import ImageGrab, Image
    PIL_OK = True
except ImportError:
    PIL_OK = False

try:
    import tkinter as tk
    TK_OK = True
except ImportError:
    TK_OK = False

DB_DEFAULT = "data/price_history.db"
APPROVAL_TIMEOUT = 120  # seconds


# ── Price History DB (BUG-28 schema) ─────────────────────────────
def _init_price_db(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol    TEXT NOT NULL,
            timestamp REAL NOT NULL,
            price     REAL NOT NULL,
            volume    REAL,
            source    TEXT DEFAULT 'screen_capture'
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_symbol_time
        ON price_history(symbol, timestamp)
    """)
    conn.commit()
    return conn


def _append_price(conn: sqlite3.Connection, symbol: str,
                  price: float, volume: float = 0.0,
                  source: str = "api") -> None:
    conn.execute("""
        INSERT INTO price_history (symbol, timestamp, price, volume, source)
        VALUES (?, ?, ?, ?, ?)
    """, (symbol, time.time(), price, volume, source))
    # Keep last 10 000 rows per symbol
    conn.execute("""
        DELETE FROM price_history WHERE symbol=? AND id NOT IN (
            SELECT id FROM price_history WHERE symbol=?
            ORDER BY timestamp DESC LIMIT 10000
        )
    """, (symbol, symbol))
    conn.commit()


def _get_price_history(conn: sqlite3.Connection,
                       symbol: str, limit: int = 100) -> List[Dict]:
    cursor = conn.execute("""
        SELECT timestamp, price, volume FROM price_history
        WHERE symbol=? ORDER BY timestamp DESC LIMIT ?
    """, (symbol, limit))
    rows = cursor.fetchall()
    rows.reverse()  # oldest first for indicators
    return [{"timestamp": r[0], "close": r[1], "volume": r[2]} for r in rows]


# ── Signal approval (BUG-36 non-blocking) ────────────────────────
class _ApprovalGate:
    """Non-blocking approval — main loop keeps running behind it."""

    def __init__(self):
        self._event = threading.Event()
        self._result: Optional[bool] = None
        self._lock = threading.Lock()
        self._signal_id: Optional[str] = None

    def request(self, signal_id: str, timeout: int = APPROVAL_TIMEOUT,
                card_text: str = "") -> bool:
        """
        Show approval card and wait (non-blocking to main loop).
        Called from a daemon thread so main loop continues.
        """
        with self._lock:
            self._event.clear()
            self._result = None
            self._signal_id = signal_id

        # Print card to terminal (overlay integration point)
        print("\n" + "╔" + "═" * 43 + "╗")
        print(f"║  ⚠️  TRADE SIGNAL — APPROVAL REQUIRED       ║")
        print("╠" + "═" * 43 + "╣")
        for line in card_text.split("\n"):
            print(f"║  {line:<41}║")
        print(f"║  [Y] Approve   [N] Reject  [S] Snooze      ║")
        print(f"║  Auto-rejects in: {timeout}s                  ║")
        print("╚" + "═" * 43 + "╝")

        # Start keyboard listener in daemon thread
        t = threading.Thread(target=self._listen,
                             args=(signal_id, timeout), daemon=True)
        t.start()
        self._event.wait(timeout=timeout)

        with self._lock:
            result = self._result
            self._signal_id = None

        if result is None:
            logging.info(f"[TRADE] Approval timeout — auto-rejected: {signal_id}")
            return False
        return result

    def _listen(self, signal_id: str, timeout: int) -> None:
        """Read keyboard input in separate thread."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                ch = input("").strip().lower()
            except (EOFError, KeyboardInterrupt):
                break
            if ch == "y":
                self._set_result(True)
                return
            elif ch == "n":
                self._set_result(False)
                return
            elif ch == "s":
                logging.info("[TRADE] Snoozed — will remind in 5 min")
                time.sleep(300)
                # Re-show card (simplified)
                print(f"[Snoozed reminder] Signal: {signal_id} — Y/N?")
        self._set_result(None)

    def _set_result(self, value: Optional[bool]) -> None:
        with self._lock:
            if self._result is None:
                self._result = value
        self._event.set()

    def approve(self) -> None:
        self._set_result(True)

    def reject(self) -> None:
        self._set_result(False)


# ── Hybrid analysis ───────────────────────────────────────────────
def _real_data_signals(symbol: str, price_conn: sqlite3.Connection
                       ) -> Dict[str, Any]:
    """Stream A: real price + technical indicators (70% weight)."""
    signals: Dict[str, Any] = {"symbol": symbol, "price": None}

    # Get live price from market_data
    try:
        from market_data import get_current_price, fetch_crypto_ohlcv
        price = get_current_price(symbol)
        signals["price"] = price
        if price:
            _append_price(price_conn, symbol, price, source="api")

        ohlcv = fetch_crypto_ohlcv(symbol, "1H", 100)
        history = _get_price_history(price_conn, symbol, 100)

        if not history:
            # Fall back to fresh OHLCV
            history = [{"close": c.close, "volume": c.volume,
                        "high": c.high, "low": c.low,
                        "open": c.open, "timestamp": c.timestamp}
                       for c in ohlcv]
    except Exception as e:
        logging.warning(f"[TRADE] market_data failed: {e}")
        history = _get_price_history(price_conn, symbol, 100)

    # Calculate indicators
    try:
        from technical_indicators import generate_signals
        ind = generate_signals(symbol, history)
        signals.update(ind)
    except Exception as e:
        logging.warning(f"[TRADE] indicators failed: {e}")
        signals["status"] = "indicators_error"

    # ── TimesFM Forecast (MEDIUM/HIGH workers, 2GB+ RAM only) ────
    try:
        import psutil
        free_ram_gb = psutil.virtual_memory().available / 1e9
        power_class = _get_local_power_class()
        if free_ram_gb >= 2.0 and power_class in ("MEDIUM", "HIGH"):
            from price_forecaster import forecast as tfm_forecast
            from price_forecaster import format_for_overlay as tfm_overlay
            prices = [h["close"] for h in history if h.get("close") is not None]
            if len(prices) >= 10:
                fc = tfm_forecast(prices[-100:], horizon=12)
                signals["forecast"] = fc
                signals["forecast_overlay"] = tfm_overlay(fc)
                logging.debug(
                    f"[TRADE] TimesFM forecast for {symbol}: "
                    f"{fc.get('forecast_overlay', fc.get('confidence_summary', ''))}"
                )
        else:
            logging.debug(
                f"[TRADE] TimesFM skipped — RAM: {free_ram_gb:.1f}GB "
                f"class: {power_class}"
            )
    except Exception as e:
        logging.warning(f"[TRADE] TimesFM forecast failed: {e}")

    return signals


def _vision_signals(screenshot_path: Optional[str],
                    real_price: Optional[float],
                    real_rsi: Optional[float],
                    real_trend: Optional[str]) -> Dict[str, Any]:
    """Stream B: AI vision for chart patterns only (30% weight)."""
    if not screenshot_path or not os.path.exists(screenshot_path):
        return {"vision_confidence": 0, "patterns": [], "status": "no_screenshot"}

    prompt = f"""This is a trading chart.
I already have exact numerical data:
  Price: {real_price}
  RSI: {real_rsi}
  Trend: {real_trend}

Look ONLY for visual patterns (do NOT guess price numbers):
- Chart formations (triangles, H&S, wedges, flags, pennants)
- Visible drawn trend lines and channels
- Candlestick body shapes and significant wicks
- Volume histogram visual shape (not the number)
- Any unusual visual patterns

Return ONLY valid JSON:
{{
  "patterns": ["ascending triangle", ...],
  "trendlines": ["rising support", ...],
  "visual_signals": ["bullish", ...],
  "key_visual_zones": [66800, 68500],
  "vision_confidence": 0-100,
  "pattern_reliability": "high|medium|low"
}}"""

    try:
        from fallback_chain import call_ai
        with open(screenshot_path, "rb") as f:
            image_bytes = f.read()
        result = call_ai(prompt, image=image_bytes)
        text = result.get("result", "{}")
        import re
        text = re.sub(r"```json\n?|```\n?", "", text).strip()
        vision_data = json.loads(text)
        vision_data["status"] = "ok"
        return vision_data
    except Exception as e:
        logging.warning(f"[TRADE] Vision analysis failed: {e}")
        return {"vision_confidence": 0, "patterns": [], "status": str(e)}


def _combine_signals(real: Dict, vision: Dict,
                     real_weight: float = 0.70) -> Dict[str, Any]:
    """Combine real data and vision into unified signal."""
    vision_weight = 1.0 - real_weight

    real_strength = float(real.get("signal_strength", 50))
    vision_conf = float(vision.get("vision_confidence", 50))

    combined_score = (real_strength * real_weight +
                      vision_conf * vision_weight)
    combined_score = max(0, min(100, combined_score))

    # Determine recommendation
    if combined_score >= 70:
        recommended = "BUY"
    elif combined_score <= 30:
        recommended = "SELL"
    elif combined_score >= 60 or combined_score <= 40:
        recommended = "WATCH"
    else:
        recommended = "HOLD"

    return {
        "price": real.get("price"),
        "trend": real.get("trend"),
        "rsi": real.get("rsi"),
        "macd": real.get("macd"),
        "patterns": vision.get("patterns", []),
        "trendlines": vision.get("trendlines", []),
        "support": real.get("bollinger", {}).get("lower") if isinstance(
            real.get("bollinger"), dict) else None,
        "resistance": real.get("bollinger", {}).get("upper") if isinstance(
            real.get("bollinger"), dict) else None,
        "signal_strength": round(combined_score, 1),
        "recommended": recommended,
        "confidence": round(combined_score / 100, 3),
        "vision_patterns": vision.get("patterns", []),
        "real_weight": real_weight,
        "vision_weight": vision_weight,
        "forecast": real.get("forecast", {}),
        "forecast_overlay": real.get("forecast_overlay", ""),
    }


# ── Main agent class ──────────────────────────────────────────────
class TradingAgent:
    """
    Main trading analysis agent.
    Runs hybrid analysis loop every 60 seconds.
    """

    __slots__ = ["symbols", "capture_interval", "chart_crop",
                 "real_weight", "min_confidence", "_running",
                 "_thread", "_price_conn", "_approval_gate",
                 "_last_signals"]

    def __init__(self,
                 symbols: Optional[List[str]] = None,
                 capture_interval: int = 60,
                 chart_crop: Optional[Dict] = None,
                 real_weight: float = 0.70,
                 min_confidence: float = 0.65,
                 db_path: str = DB_DEFAULT):
        self.symbols = symbols or ["BTC-USDT"]
        self.capture_interval = capture_interval
        self.chart_crop = chart_crop or {"x": 0, "y": 0,
                                          "width": 1280, "height": 720}
        self.real_weight = real_weight
        self.min_confidence = min_confidence
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._price_conn = _init_price_db(db_path)
        self._approval_gate = _ApprovalGate()
        self._last_signals: Dict[str, Any] = {}

    def capture_chart(self, symbol: str) -> Optional[str]:
        """Screenshot chart area, save to temp file."""
        if not PIL_OK:
            return None
        try:
            crop = self.chart_crop
            img = ImageGrab.grab(bbox=(
                crop["x"], crop["y"],
                crop["x"] + crop["width"],
                crop["y"] + crop["height"]
            ))
            path = f"/tmp/aenida_chart_{symbol.replace('-', '_')}.png"
            img.save(path)
            return path
        except Exception as e:
            logging.warning(f"[TRADE] Screenshot failed: {e}")
            return None

    def analyze_symbol(self, symbol: str) -> Dict[str, Any]:
        """
        Run full hybrid analysis for one symbol.
        BUG-39: real data 70%, vision 30%.
        """
        # Stream A: real data (numbers always accurate)
        real = _real_data_signals(symbol, self._price_conn)

        # Stream B: vision (patterns only, never numbers)
        screenshot = self.capture_chart(symbol)
        vision = _vision_signals(
            screenshot,
            real_price=real.get("price"),
            real_rsi=real.get("rsi"),
            real_trend=real.get("trend"),
        )

        # Combine
        unified = _combine_signals(real, vision, self.real_weight)
        unified["symbol"] = symbol
        self._last_signals[symbol] = unified
        return unified

    def _check_signal_and_approve(self, symbol: str,
                                   unified: Dict[str, Any]) -> None:
        """If signal fires, show approval card (BUG-36 non-blocking)."""
        conf = unified.get("confidence", 0)
        if conf < self.min_confidence:
            return
        recommended = unified.get("recommended", "HOLD")
        if recommended not in ("BUY", "SELL"):
            return

        # Log to trade_journal
        signal_id = ""
        try:
            from trade_journal import log_signal
            signal_id = log_signal(
                symbol=symbol,
                signal_data=unified,
                reasoning=(
                    f"RSI={unified.get('rsi')} "
                    f"Trend={unified.get('trend')} "
                    f"Patterns={unified.get('patterns', [])}"
                )
            )
        except Exception as e:
            logging.warning(f"[TRADE] Journal log failed: {e}")

        # Build approval card text
        rsi_val = unified.get("rsi", "N/A")
        trend = unified.get("trend", "N/A")
        price = unified.get("price", "N/A")
        forecast_line = unified.get("forecast_overlay", "")
        price_str = f"${price:,.2f}" if isinstance(price, (int, float)) else str(price)
        card = (
            f"Symbol:   {symbol}\n"
            f"Action:   {recommended}\n"
            f"Price:    {price_str}\n"
            f"RSI:      {rsi_val}  Trend: {trend}\n"
            f"Patterns: {', '.join(unified.get('patterns', [])) or 'None'}\n"
            f"Signal:   {unified.get('signal_strength', 0):.0f}%  "
            f"Conf: {conf:.0%}"
        )
        if forecast_line:
            card += f"\nForecast: {forecast_line}"

        # Non-blocking approval in separate daemon thread
        def _request():
            approved = self._approval_gate.request(
                signal_id, APPROVAL_TIMEOUT, card)
            if approved:
                logging.info(f"[TRADE] Signal APPROVED: {symbol} {recommended}")
            else:
                logging.info(f"[TRADE] Signal REJECTED: {symbol} {recommended}")
            try:
                from trade_journal import log_approval
                log_approval(signal_id, approved)
            except Exception:
                pass

        t = threading.Thread(target=_request, daemon=True, name="approval")
        t.start()

    def _analysis_loop(self) -> None:
        """Main analysis loop — runs every capture_interval seconds."""
        while self._running:
            for symbol in self.symbols:
                if not self._running:
                    break
                try:
                    unified = self.analyze_symbol(symbol)
                    logging.info(
                        f"[TRADE] {symbol}: "
                        f"{unified.get('recommended','?')} "
                        f"{unified.get('signal_strength', 0):.0f}% "
                        f"price=${unified.get('price', 0)}"
                    )
                    self._check_signal_and_approve(symbol, unified)
                except Exception as e:
                    logging.error(f"[TRADE] Analysis failed for {symbol}: {e}")

            # Sleep in increments so stop() works promptly
            for _ in range(self.capture_interval):
                if not self._running:
                    break
                time.sleep(1)

    def start(self) -> None:
        """Start background analysis loop."""
        self._running = True
        self._thread = threading.Thread(
            target=self._analysis_loop, daemon=True, name="trading_agent")
        self._thread.start()
        logging.info(
            f"[TRADE] Agent started — symbols: {self.symbols} "
            f"interval: {self.capture_interval}s"
        )

    def stop(self) -> None:
        self._running = False

    def get_last_signals(self) -> Dict[str, Any]:
        return self._last_signals.copy()

    def force_refresh(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        """Force immediate analysis (hotkey trigger)."""
        targets = [symbol] if symbol else self.symbols
        results = {}
        for sym in targets:
            results[sym] = self.analyze_symbol(sym)
        return results


# ── Global instance ───────────────────────────────────────────────
_agent: Optional[TradingAgent] = None


def get_agent() -> TradingAgent:
    global _agent
    if _agent is None:
        try:
            import config
            symbols = config.get("trading.symbols", ["BTC-USDT"])
            interval = config.get("trading.capture_interval_seconds", 60)
            crop = config.get("trading.chart_crop",
                              {"x": 0, "y": 0, "width": 1280, "height": 720})
            real_w = config.get("hybrid_analysis.real_data_weight", 0.70)
            min_c = config.get("hybrid_analysis.min_confidence_for_signal", 0.65)
            db = config.get("paths.price_history_db", DB_DEFAULT)
        except Exception:
            symbols = ["BTC-USDT"]
            interval = 60
            crop = {"x": 0, "y": 0, "width": 1280, "height": 720}
            real_w = 0.70
            min_c = 0.65
            db = DB_DEFAULT
        _agent = TradingAgent(symbols=symbols, capture_interval=interval,
                              chart_crop=crop, real_weight=real_w,
                              min_confidence=min_c, db_path=db)
    return _agent


def start() -> None:
    get_agent().start()


def stop() -> None:
    get_agent().stop()


def force_refresh(symbol: Optional[str] = None) -> Dict[str, Any]:
    return get_agent().force_refresh(symbol)


def get_last_signals() -> Dict[str, Any]:
    return get_agent().get_last_signals()
