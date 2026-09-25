"""
AENIDA Trading Assistant  v2.0
================================
Real-time deep signal engine for any coin.

Signals computed (all in pure Python, works when worker offline):
  1. TREND            — Short/medium/long-term trend direction
  2. RSI              — Overbought / oversold zones
  3. MACD             — Momentum crossovers, histogram direction
  4. BOLLINGER BANDS  — Squeeze (low volatility) → Breakout detection
  5. BREAKOUT         — Price breaking key S/R levels with volume confirmation
  6. VOLUME           — Spike detection, buying vs selling pressure
  7. SUPPORT/RESIST   — Pivot-based S/R zones, swing highs/lows
  8. LIQUIDITY ZONES  — High-volume price clusters where price reacts
  9. VWAP             — Price position vs Volume-Weighted Average
 10. ATR              — Volatility, suggested stop-loss distance
 11. EMA CROSS        — 9/21 short signal, 50/200 macro signal
 12. FUNDING RATE     — Sentiment for perpetuals
 13. TRADE PLAN       — Entry zone, stop-loss, take-profit targets

NEW IN v2.0:
  - Better error handling and recovery
  - Caching for faster repeated analysis
  - Multi-timeframe analysis
  - Enhanced chat with context memory
  - Signal confidence scoring
  - Alert system integration

USAGE (CLI):
  from trading_assistant import analyze, chat
  result = analyze("BTC-USDT", timeframe="1H")
  answer = chat("BTC-USDT", "Is there a breakout forming?")

USAGE (from main.py):
  python main.py trade BTC-USDT          # Full analysis
  python main.py trade SOL-USDT --tf 4H  # 4-hour timeframe
  python main.py ask-trade "Is ETH oversold?"
"""

import logging
import math
import time
import json
import os
import hashlib
from typing import Any, Dict, List, Optional, Tuple, Union
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from functools import lru_cache
from contextlib import contextmanager

# Setup logging
log = logging.getLogger("trading_assistant")

# Constants
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cache")
CACHE_TTL_SECONDS = 60  # Cache data for 1 minute
MAX_CACHE_SIZE = 100

os.makedirs(CACHE_DIR, exist_ok=True)


# ═════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class SignalResult:
    """Structured signal result for better type safety."""
    symbol: str
    timeframe: str
    price: float
    signal_strength: int
    recommendation: str
    status: str = "ok"
    error: Optional[str] = None
    
    # Indicators
    rsi: Optional[float] = None
    macd: Optional[Dict] = None
    bollinger: Optional[Dict] = None
    vwap: Optional[float] = None
    above_vwap: Optional[bool] = None
    atr: Optional[float] = None
    
    # EMAs
    ema9: Optional[float] = None
    ema21: Optional[float] = None
    ema50: Optional[float] = None
    ema200: Optional[float] = None
    
    # Trends
    trend_short: Optional[str] = None
    trend_long: Optional[str] = None
    cross_signal: Optional[str] = None
    
    # Advanced signals
    breakout: Optional[Dict] = None
    volume: Optional[Dict] = None
    support_resistance: Optional[Dict] = None
    liquidity_zones: Optional[Dict] = None
    
    # Funding
    funding_rate: Optional[float] = None
    funding_text: Optional[str] = None
    
    # Trade plan
    trade_plan: Optional[Dict] = None
    
    # Metadata
    analyzed_candles: int = 0
    elapsed_seconds: float = 0.0
    timestamp: str = ""
    
    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)


# ═════════════════════════════════════════════════════════════════════════════
# CACHE SYSTEM
# ═════════════════════════════════════════════════════════════════════════════

class DataCache:
    """Simple file-based cache for OHLCV data."""
    
    def __init__(self, ttl_seconds: int = CACHE_TTL_SECONDS):
        self.ttl = ttl_seconds
        self.memory_cache: Dict[str, Tuple[Any, float]] = {}
    
    def _make_key(self, symbol: str, interval: str, limit: int) -> str:
        """Create cache key from parameters."""
        key_str = f"{symbol}:{interval}:{limit}"
        return hashlib.md5(key_str.encode()).hexdigest()
    
    def get(self, symbol: str, interval: str, limit: int) -> Optional[List[Dict]]:
        """Get cached data if not expired."""
        key = self._make_key(symbol, interval, limit)
        
        # Check memory cache first
        if key in self.memory_cache:
            data, timestamp = self.memory_cache[key]
            if time.time() - timestamp < self.ttl:
                log.debug(f"Memory cache hit for {symbol}")
                return data
            else:
                del self.memory_cache[key]
        
        # Check file cache
        cache_file = os.path.join(CACHE_DIR, f"{key}.json")
        if os.path.exists(cache_file):
            try:
                mtime = os.path.getmtime(cache_file)
                if time.time() - mtime < self.ttl:
                    with open(cache_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    # Update memory cache
                    self.memory_cache[key] = (data, time.time())
                    log.debug(f"File cache hit for {symbol}")
                    return data
            except Exception as e:
                log.debug(f"Cache read error: {e}")
        
        return None
    
    def set(self, symbol: str, interval: str, limit: int, data: List[Dict]) -> None:
        """Cache data."""
        key = self._make_key(symbol, interval, limit)
        
        # Update memory cache
        self.memory_cache[key] = (data, time.time())
        
        # Update file cache
        cache_file = os.path.join(CACHE_DIR, f"{key}.json")
        try:
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(data, f)
        except Exception as e:
            log.debug(f"Cache write error: {e}")
    
    def clear(self) -> None:
        """Clear all caches."""
        self.memory_cache.clear()
        try:
            for f in os.listdir(CACHE_DIR):
                if f.endswith('.json'):
                    os.remove(os.path.join(CACHE_DIR, f))
        except Exception as e:
            log.warning(f"Cache clear error: {e}")


# Global cache instance
_data_cache = DataCache()


# ═════════════════════════════════════════════════════════════════════════════
# ERROR HANDLING
# ═════════════════════════════════════════════════════════════════════════════

class TradingError(Exception):
    """Base exception for trading assistant errors."""
    pass

class DataFetchError(TradingError):
    """Raised when data fetching fails."""
    pass

class AnalysisError(TradingError):
    """Raised when analysis fails."""
    pass


@contextmanager
def error_handler(operation: str):
    """Context manager for consistent error handling."""
    try:
        yield
    except TradingError:
        raise
    except Exception as e:
        log.error(f"Error in {operation}: {e}")
        raise TradingError(f"{operation} failed: {e}")


# ═════════════════════════════════════════════════════════════════════════════
# DATA FETCH
# ═════════════════════════════════════════════════════════════════════════════

def _fetch_ohlcv(symbol: str, interval: str = "1H", limit: int = 200) -> List[Dict]:
    """Fetch OHLCV from OKX/Binance with full fallback and caching."""
    symbol = symbol.upper()
    
    # Check cache first
    cached = _data_cache.get(symbol, interval, limit)
    if cached is not None:
        return cached
    
    data = None
    errors = []
    
    # Try 1: market_data module
    try:
        from market_data import fetch_crypto_ohlcv
        data = fetch_crypto_ohlcv(symbol, interval=interval, limit=limit)
        if data and len(data) > 20:
            _data_cache.set(symbol, interval, limit, data)
            return data
    except Exception as e:
        errors.append(f"market_data: {e}")
        log.debug(f"market_data failed: {e}")
    
    # Try 2: Direct OKX
    try:
        import urllib.request
        sym = symbol.replace("-", "-")
        bar = {"1H": "1H", "4H": "4H", "1D": "1D", "15m": "15m"}.get(interval, "1H")
        url = f"https://www.okx.com/api/v5/market/candles?instId={sym}&bar={bar}&limit={limit}"
        
        req = urllib.request.Request(url, headers={'User-Agent': 'AENIDA-TradingBot/2.0'})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = json.loads(r.read())
        
        candles = raw.get("data", [])
        result = []
        for c in reversed(candles):
            result.append({
                "time": int(c[0]) / 1000,
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume": float(c[5]),
            })
        
        if len(result) > 20:
            _data_cache.set(symbol, interval, limit, result)
            return result
    except Exception as e:
        errors.append(f"OKX direct: {e}")
        log.debug(f"OKX direct failed: {e}")
    
    # Try 3: Binance as last resort
    try:
        import urllib.request
        sym = symbol.replace("-", "").replace("USDT", "USDT")
        interval_map = {"1H": "1h", "4H": "4h", "1D": "1d", "15m": "15m"}
        binance_interval = interval_map.get(interval, "1h")
        url = f"https://api.binance.com/api/v3/klines?symbol={sym}&interval={binance_interval}&limit={limit}"
        
        req = urllib.request.Request(url, headers={'User-Agent': 'AENIDA-TradingBot/2.0'})
        with urllib.request.urlopen(req, timeout=15) as r:
            candles = json.loads(r.read())
        
        result = []
        for c in candles:
            result.append({
                "time": int(c[0]) / 1000,
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume": float(c[5]),
            })
        
        if len(result) > 20:
            _data_cache.set(symbol, interval, limit, result)
            return result
    except Exception as e:
        errors.append(f"Binance: {e}")
        log.debug(f"Binance failed: {e}")
    
    # All sources failed
    log.error(f"All data sources failed for {symbol}: {'; '.join(errors)}")
    raise DataFetchError(f"Could not fetch data for {symbol}. Tried: {', '.join(errors)}")


def _fetch_price(symbol: str) -> Optional[float]:
    """Fetch current price with fallback."""
    try:
        from market_data import get_current_price
        return get_current_price(symbol)
    except Exception:
        pass
    
    try:
        import urllib.request
        url = f"https://www.okx.com/api/v5/market/ticker?instId={symbol}"
        req = urllib.request.Request(url, headers={'User-Agent': 'AENIDA-TradingBot/2.0'})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read())
        return float(d["data"][0]["last"])
    except Exception:
        return None


def _fetch_funding(symbol: str) -> Optional[float]:
    """Fetch funding rate with fallback."""
    try:
        from market_data import get_funding_rate
        return get_funding_rate(symbol)
    except Exception:
        pass
    
    try:
        import urllib.request
        url = f"https://www.okx.com/api/v5/public/funding-rate?instId={symbol}-SWAP"
        req = urllib.request.Request(url, headers={'User-Agent': 'AENIDA-TradingBot/2.0'})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read())
        return float(d["data"][0]["fundingRate"])
    except Exception:
        return None


# ═════════════════════════════════════════════════════════════════════════════
# MATH HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def _ema(data: List[float], period: int) -> List[float]:
    """Calculate Exponential Moving Average."""
    if len(data) < period:
        return []
    k = 2 / (period + 1)
    result = [sum(data[:period]) / period]
    for v in data[period:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def _sma(data: List[float], period: int) -> List[float]:
    """Calculate Simple Moving Average."""
    if len(data) < period:
        return []
    return [sum(data[i:i+period]) / period for i in range(len(data) - period + 1)]


def _rsi(closes: List[float], period: int = 14) -> Optional[float]:
    """Calculate Relative Strength Index."""
    if len(closes) < period + 1:
        return None
    
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    
    avg_g = sum(gains[-period:]) / period
    avg_l = sum(losses[-period:]) / period
    
    if avg_l == 0:
        return 100.0
    
    rs = avg_g / avg_l
    return round(100 - (100 / (1 + rs)), 2)


def _macd(closes: List[float]) -> Dict[str, Any]:
    """Calculate MACD indicator."""
    if len(closes) < 26:
        return {}
    
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    min_len = min(len(ema12), len(ema26))
    macd_line = [ema12[-(min_len-i)] - ema26[-(min_len-i)] for i in range(min_len)]
    signal = _ema(macd_line, 9) if len(macd_line) >= 9 else []
    hist = macd_line[-1] - signal[-1] if signal else 0
    
    return {
        "macd": round(macd_line[-1], 6),
        "signal": round(signal[-1], 6) if signal else 0,
        "histogram": round(hist, 6),
        "bullish": hist > 0,
    }


def _bollinger(closes: List[float], period: int = 20) -> Dict[str, Any]:
    """Calculate Bollinger Bands."""
    if len(closes) < period:
        return {}
    
    recent = closes[-period:]
    mid = sum(recent) / period
    variance = sum((x - mid) ** 2 for x in recent) / period
    std = math.sqrt(variance)
    upper = mid + 2 * std
    lower = mid - 2 * std
    price = closes[-1]
    width = (upper - lower) / mid * 100 if mid != 0 else 0
    pct_b = ((price - lower) / (upper - lower) * 100) if upper != lower else 50
    
    return {
        "upper": round(upper, 4),
        "mid": round(mid, 4),
        "lower": round(lower, 4),
        "width": round(width, 2),
        "pct_b": round(pct_b, 1),
        "squeeze": width < 3.5,
    }


# ═════════════════════════════════════════════════════════════════════════════
# SIGNAL ENGINES
# ═════════════════════════════════════════════════════════════════════════════

def _detect_support_resistance(ohlcv: List[Dict], lookback: int = 50) -> Dict[str, Any]:
    """Detect S/R zones using pivot points and swing highs/lows."""
    data = ohlcv[-lookback:] if len(ohlcv) >= lookback else ohlcv
    if len(data) < 5:
        return {"pivot": None, "resistances": [], "supports": []}
    
    closes = [c["close"] for c in data]
    highs = [c["high"] for c in data]
    lows = [c["low"] for c in data]
    
    # Classic daily pivot
    h, l, c = highs[-1], lows[-1], closes[-1]
    pivot = (h + l + c) / 3
    r1 = 2 * pivot - l
    r2 = pivot + (h - l)
    r3 = h + 2 * (pivot - l)
    s1 = 2 * pivot - h
    s2 = pivot - (h - l)
    s3 = l - 2 * (h - pivot)
    
    # Swing highs/lows
    swing_highs, swing_lows = [], []
    for i in range(2, len(highs) - 2):
        if highs[i] == max(highs[i-2:i+3]):
            swing_highs.append(highs[i])
        if lows[i] == min(lows[i-2:i+3]):
            swing_lows.append(lows[i])
    
    # Cluster nearby levels
    def cluster(levels: List[float], tol: float = 0.005) -> List[float]:
        if not levels:
            return []
        sorted_l = sorted(set(round(x, 4) for x in levels))
        result, group = [], [sorted_l[0]]
        for v in sorted_l[1:]:
            if abs(v - group[-1]) / group[-1] <= tol:
                group.append(v)
            else:
                result.append(round(sum(group) / len(group), 4))
                group = [v]
        result.append(round(sum(group) / len(group), 4))
        return result
    
    resistances = cluster([r1, r2, r3] + swing_highs)
    supports = cluster([s1, s2, s3] + swing_lows)
    price = closes[-1]
    
    # Filter by price position
    resistances = [r for r in resistances if r > price * 1.001][:3]
    supports = [s for s in supports if s < price * 0.999][:3]
    
    return {
        "pivot": round(pivot, 4),
        "resistances": sorted(resistances),
        "supports": sorted(supports, reverse=True),
        "r1": round(r1, 4), "r2": round(r2, 4),
        "s1": round(s1, 4), "s2": round(s2, 4),
    }


def _detect_liquidity_zones(ohlcv: List[Dict]) -> Dict[str, Any]:
    """Detect liquidity zones from volume profile."""
    if len(ohlcv) < 20:
        return {"zones": [], "high_vol_level": None}
    
    data = ohlcv[-100:] if len(ohlcv) >= 100 else ohlcv
    all_highs = [c["high"] for c in data]
    all_lows = [c["low"] for c in data]
    all_closes = [c["close"] for c in data]
    all_vols = [c["volume"] for c in data]
    price = all_closes[-1]
    
    price_min = min(all_lows)
    price_max = max(all_highs)
    if price_max == price_min:
        return {"zones": [], "high_vol_level": None}
    
    BUCKETS = 12
    bucket_size = (price_max - price_min) / BUCKETS
    bucket_vol = [0.0] * BUCKETS
    
    for i, candle in enumerate(data):
        mid = (candle["high"] + candle["low"]) / 2
        b = int((mid - price_min) / bucket_size)
        b = max(0, min(b, BUCKETS - 1))
        bucket_vol[b] += all_vols[i]
    
    avg_vol = sum(bucket_vol) / BUCKETS
    zones = []
    for i, vol in enumerate(bucket_vol):
        if vol >= avg_vol * 1.5:
            lo = round(price_min + i * bucket_size, 4)
            hi = round(lo + bucket_size, 4)
            zones.append({
                "low": lo,
                "high": hi,
                "mid": round((lo + hi) / 2, 4),
                "vol_strength": round(vol / avg_vol, 1),
                "type": "resistance" if (lo + hi) / 2 > price else "support",
            })
    
    peak_bucket = bucket_vol.index(max(bucket_vol))
    peak_mid = price_min + (peak_bucket + 0.5) * bucket_size
    
    return {
        "zones": zones,
        "high_vol_level": round(peak_mid, 4),
        "description": f"{len(zones)} high-volume price clusters found",
    }


def _detect_breakout(ohlcv: List[Dict], bb: Dict, sr: Dict) -> Dict[str, Any]:
    """Detect breakout signals."""
    if len(ohlcv) < 25:
        return {"type": "none", "strength": 0, "description": "Insufficient data"}
    
    closes = [c["close"] for c in ohlcv]
    volumes = [c["volume"] for c in ohlcv]
    price = closes[-1]
    vol_avg = sum(volumes[-20:]) / 20
    vol_now = volumes[-1]
    vol_spike = vol_now >= vol_avg * 1.6
    
    breakout_type = "none"
    strength = 0
    signals = []
    
    # BB breakout
    if bb:
        if price > bb.get("upper", price):
            signals.append("Price above upper Bollinger Band")
            breakout_type = "bullish_bb"
            strength += 35
        elif price < bb.get("lower", price):
            signals.append("Price below lower Bollinger Band")
            breakout_type = "bearish_bb"
            strength += 35
        elif bb.get("squeeze"):
            signals.append("Bollinger Band squeeze — breakout imminent")
            breakout_type = "squeeze"
            strength += 20
    
    # S/R breakout
    nearest_resist = sr.get("resistances", [None])[0] if sr.get("resistances") else None
    nearest_support = sr.get("supports", [None])[0] if sr.get("supports") else None
    
    if nearest_resist and price > nearest_resist * 0.998:
        signals.append(f"Breaking resistance at {nearest_resist}")
        breakout_type = "bullish_sr" if breakout_type == "none" else breakout_type
        strength += 30
    
    if nearest_support and price < nearest_support * 1.002:
        signals.append(f"Breaking support at {nearest_support}")
        breakout_type = "bearish_sr" if breakout_type == "none" else breakout_type
        strength += 30
    
    # Volume confirmation
    if vol_spike and strength > 0:
        signals.append(f"Volume spike {vol_now/vol_avg:.1f}x average — confirms move")
        strength += 20
    elif not vol_spike and strength > 0:
        signals.append("Low volume — breakout may be weak (no confirmation)")
        strength = max(0, strength - 15)
    
    return {
        "type": breakout_type,
        "strength": min(strength, 100),
        "volume_confirmed": vol_spike,
        "signals": signals,
        "description": "; ".join(signals) if signals else "No breakout detected",
    }


def _detect_volume_analysis(ohlcv: List[Dict]) -> Dict[str, Any]:
    """Analyze volume patterns."""
    if len(ohlcv) < 20:
        return {}
    
    closes = [c["close"] for c in ohlcv[-50:]]
    volumes = [c["volume"] for c in ohlcv[-50:]]
    avg_vol = sum(volumes[-20:]) / 20
    vol_now = volumes[-1]
    ratio = vol_now / avg_vol if avg_vol else 1
    
    # Buy/sell pressure
    up_vol = sum(volumes[-10:][i] for i in range(10) if closes[-10:][i] >= closes[-11:-1][i] if i < 9)
    down_vol = sum(volumes[-10:][i] for i in range(10) if closes[-10:][i] < closes[-11:-1][i] if i < 9)
    total = up_vol + down_vol
    buy_pct = round(up_vol / total * 100, 1) if total else 50
    
    vol_trend = "rising" if volumes[-1] > volumes[-5] else "falling"
    
    if ratio >= 3.0:
        label = "🔥 Extreme Volume Spike"
    elif ratio >= 2.0:
        label = "⚡ High Volume Spike"
    elif ratio >= 1.5:
        label = "📈 Above-Average Volume"
    elif ratio <= 0.4:
        label = "📉 Volume Dry-Up (low interest)"
    elif ratio <= 0.6:
        label = "😶 Below-Average Volume"
    else:
        label = "Normal"
    
    return {
        "current": round(vol_now, 2),
        "average_20": round(avg_vol, 2),
        "ratio": round(ratio, 2),
        "label": label,
        "buy_pressure_pct": buy_pct,
        "sell_pressure_pct": round(100 - buy_pct, 1),
        "volume_trend": vol_trend,
        "spike": ratio >= 1.6,
    }


def _suggest_trade_plan(
    price: float, atr: float, signal_strength: int,
    sr: Dict, trend: str, rsi: float
) -> Dict[str, Any]:
    """Generate entry zone, stop-loss, and take-profit targets."""
    if not price or not atr:
        return {}
    
    direction = "LONG" if signal_strength >= 55 else ("SHORT" if signal_strength <= 45 else "WAIT")
    supports = sr.get("supports", [])
    resistances = sr.get("resistances", [])
    
    if direction == "LONG":
        entry_low = round(price * 0.998, 4)
        entry_high = round(price * 1.002, 4)
        stop_loss = round(supports[0] - atr * 0.5, 4) if supports else round(price - atr * 2, 4)
        tp1 = round(resistances[0], 4) if resistances else round(price + atr * 2, 4)
        tp2 = round(resistances[1], 4) if len(resistances) > 1 else round(price + atr * 4, 4)
        risk = round(price - stop_loss, 4)
        reward_tp1 = round(tp1 - price, 4)
        rr = round(reward_tp1 / risk, 2) if risk > 0 else 0
    elif direction == "SHORT":
        entry_low = round(price * 0.998, 4)
        entry_high = round(price * 1.002, 4)
        stop_loss = round(resistances[0] + atr * 0.5, 4) if resistances else round(price + atr * 2, 4)
        tp1 = round(supports[0], 4) if supports else round(price - atr * 2, 4)
        tp2 = round(supports[1], 4) if len(supports) > 1 else round(price - atr * 4, 4)
        risk = round(stop_loss - price, 4)
        reward_tp1 = round(price - tp1, 4)
        rr = round(reward_tp1 / risk, 2) if risk > 0 else 0
    else:
        return {"direction": "WAIT", "reason": "No clear signal — stay on sidelines"}
    
    return {
        "direction": direction,
        "entry_zone": f"{entry_low} – {entry_high}",
        "stop_loss": stop_loss,
        "take_profit_1": tp1,
        "take_profit_2": tp2,
        "risk_reward": rr,
        "atr": round(atr, 4),
        "note": "Based on ATR + nearest S/R. Always use proper position sizing.",
    }


# ═════════════════════════════════════════════════════════════════════════════
# MAIN ANALYSIS ENGINE
# ═════════════════════════════════════════════════════════════════════════════

def analyze(symbol: str, timeframe: str = "1H") -> Dict[str, Any]:
    """
    Full deep analysis of any coin/pair.
    Returns structured dict of ALL signals.
    """
    symbol = symbol.upper()
    if "-" not in symbol:
        symbol = symbol + "-USDT"
    
    log.info(f"Analyzing {symbol} [{timeframe}]")
    t0 = time.time()
    
    try:
        # Fetch data
        ohlcv = _fetch_ohlcv(symbol, interval=timeframe, limit=200)
        if len(ohlcv) < 25:
            return {
                "symbol": symbol,
                "timeframe": timeframe,
                "status": "error",
                "error": f"Not enough data for {symbol}. Check the symbol name (e.g. BTC-USDT, SOL-USDT).",
            }
        
        closes = [c["close"] for c in ohlcv]
        highs = [c["high"] for c in ohlcv]
        lows = [c["low"] for c in ohlcv]
        volumes = [c["volume"] for c in ohlcv]
        price = closes[-1]
        
        # Indicators
        rsi_val = _rsi(closes)
        macd_val = _macd(closes)
        bb_val = _bollinger(closes)
        
        ema9 = _ema(closes, 9)
        ema21 = _ema(closes, 21)
        ema50 = _ema(closes, 50) if len(closes) >= 50 else []
        ema200 = _ema(closes, 200) if len(closes) >= 200 else []
        
        # ATR
        tr_list = []
        for i in range(1, len(closes)):
            tr = max(highs[i] - lows[i],
                     abs(highs[i] - closes[i-1]),
                     abs(lows[i] - closes[i-1]))
            tr_list.append(tr)
        atr = sum(tr_list[-14:]) / 14 if len(tr_list) >= 14 else 0
        
        # Trend detection
        if ema9 and ema21:
            trend_short = "uptrend" if ema9[-1] > ema21[-1] else "downtrend"
        else:
            trend_short = "unknown"
        
        if ema50 and ema200:
            trend_long = "uptrend" if ema50[-1] > ema200[-1] else "downtrend"
        elif ema50:
            trend_long = "uptrend" if closes[-1] > ema50[-1] else "downtrend"
        else:
            trend_long = "unknown"
        
        # Golden/death cross
        cross_signal = None
        if ema50 and ema200 and len(ema50) > 1 and len(ema200) > 1:
            if ema50[-2] < ema200[-2] and ema50[-1] > ema200[-1]:
                cross_signal = "🟡 Golden Cross (50 EMA crossed above 200 EMA — long-term bullish)"
            elif ema50[-2] > ema200[-2] and ema50[-1] < ema200[-1]:
                cross_signal = "💀 Death Cross (50 EMA crossed below 200 EMA — long-term bearish)"
        
        # VWAP
        typical = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(len(closes))]
        cumvp = sum(t * v for t, v in zip(typical[-20:], volumes[-20:]))
        cumvol = sum(volumes[-20:])
        vwap = round(cumvp / cumvol, 4) if cumvol else None
        above_vwap = price > vwap if vwap else None
        
        # Advanced signals
        sr = _detect_support_resistance(ohlcv)
        liq = _detect_liquidity_zones(ohlcv)
        breakout = _detect_breakout(ohlcv, bb_val, sr)
        volume = _detect_volume_analysis(ohlcv)
        
        # Signal score
        score_signals = []
        if rsi_val:
            if rsi_val < 30:
                score_signals.append(+1.5)
            elif rsi_val > 70:
                score_signals.append(-1.5)
            elif rsi_val < 45:
                score_signals.append(+0.5)
            elif rsi_val > 55:
                score_signals.append(-0.5)
        
        if macd_val.get("bullish"):
            score_signals.append(+1.0)
        else:
            score_signals.append(-1.0)
        
        if bb_val:
            pct_b = bb_val.get("pct_b", 50)
            if pct_b < 20:
                score_signals.append(+1.0)
            elif pct_b > 80:
                score_signals.append(-1.0)
        
        if trend_short == "uptrend":
            score_signals.append(+1.0)
        elif trend_short == "downtrend":
            score_signals.append(-1.0)
        
        if trend_long == "uptrend":
            score_signals.append(+0.5)
        elif trend_long == "downtrend":
            score_signals.append(-0.5)
        
        if above_vwap is True:
            score_signals.append(+0.5)
        elif above_vwap is False:
            score_signals.append(-0.5)
        
        if breakout["type"] in ("bullish_bb", "bullish_sr"):
            score_signals.append(+1.5)
        elif breakout["type"] in ("bearish_bb", "bearish_sr"):
            score_signals.append(-1.5)
        
        raw_score = sum(score_signals) / (len(score_signals) or 1)
        signal_strength = max(0, min(100, int((raw_score + 2) / 4 * 100)))
        
        if signal_strength >= 68:
            recommendation = "STRONG BUY  🟢"
        elif signal_strength >= 56:
            recommendation = "BUY  🟡"
        elif signal_strength <= 32:
            recommendation = "STRONG SELL 🔴"
        elif signal_strength <= 44:
            recommendation = "SELL  🟠"
        else:
            recommendation = "HOLD / WAIT ⚪"
        
        # Trade plan
        trade_plan = _suggest_trade_plan(
            price, atr, signal_strength, sr, trend_short,
            rsi_val or 50
        )
        
        # Funding rate
        funding = None
        try:
            funding = _fetch_funding(symbol)
        except Exception:
            pass
        
        funding_text = None
        if funding is not None:
            pct = funding * 100
            if pct > 0.05:
                funding_text = f"+{pct:.3f}% — Longs paying shorts. Crowded long."
            elif pct < -0.05:
                funding_text = f"{pct:.3f}% — Shorts paying longs. Crowded short."
            else:
                funding_text = f"{pct:.3f}% — Neutral."
        
        elapsed = round(time.time() - t0, 2)
        
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "status": "ok",
            "price": round(price, 6),
            "signal_strength": signal_strength,
            "recommendation": recommendation,
            "rsi": rsi_val,
            "macd": macd_val,
            "bollinger": bb_val,
            "vwap": vwap,
            "above_vwap": above_vwap,
            "atr": round(atr, 6),
            "ema9": round(ema9[-1], 4) if ema9 else None,
            "ema21": round(ema21[-1], 4) if ema21 else None,
            "ema50": round(ema50[-1], 4) if ema50 else None,
            "ema200": round(ema200[-1], 4) if ema200 else None,
            "trend_short": trend_short,
            "trend_long": trend_long,
            "cross_signal": cross_signal,
            "breakout": breakout,
            "volume": volume,
            "support_resistance": sr,
            "liquidity_zones": liq,
            "funding_rate": funding,
            "funding_text": funding_text,
            "trade_plan": trade_plan,
            "analyzed_candles": len(ohlcv),
            "elapsed_seconds": elapsed,
        }
        
    except DataFetchError as e:
        log.error(f"Data fetch failed for {symbol}: {e}")
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "status": "error",
            "error": str(e),
        }
    except Exception as e:
        log.error(f"Analysis failed for {symbol}: {e}")
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "status": "error",
            "error": f"Analysis error: {str(e)}",
        }


# ═════════════════════════════════════════════════════════════════════════════
# NARRATIVE OUTPUT
# ═════════════════════════════════════════════════════════════════════════════

def format_analysis(result: Dict) -> str:
    """Convert analysis dict into a readable trading report."""
    if result.get("status") == "error":
        return f"❌ {result.get('error', 'Unknown error')}"
    
    sym = result["symbol"]
    price = result["price"]
    tf = result["timeframe"]
    strength = result["signal_strength"]
    rec = result["recommendation"]
    
    lines = []
    lines.append(f"{'═'*58}")
    lines.append(f"  {sym}  [{tf}]  @ {price}")
    lines.append(f"  Signal: {rec}  ({strength}/100)")
    lines.append(f"{'═'*58}")
    
    # Trend
    ts = result.get("trend_short", "?")
    tl = result.get("trend_long", "?")
    lines.append(f"\n  📈 TREND")
    lines.append(f"    Short-term : {ts.upper()}")
    lines.append(f"    Long-term  : {tl.upper()}")
    if result.get("cross_signal"):
        lines.append(f"    ⚡ {result['cross_signal']}")
    
    vwap = result.get("vwap")
    av = result.get("above_vwap")
    if vwap:
        pos = "ABOVE" if av else "BELOW"
        lines.append(f"    VWAP       : {vwap}  (price is {pos} VWAP)")
    
    # Indicators
    rsi = result.get("rsi")
    lines.append(f"\n  📊 INDICATORS")
    if rsi:
        rsi_label = "OVERSOLD 🟢" if rsi < 30 else ("OVERBOUGHT 🔴" if rsi > 70 else "NEUTRAL")
        lines.append(f"    RSI(14)    : {rsi}  [{rsi_label}]")
    
    macd = result.get("macd", {})
    if macd:
        m_dir = "BULLISH ▲" if macd.get("bullish") else "BEARISH ▼"
        lines.append(f"    MACD       : {macd.get('macd',0):+.4f}  hist {macd.get('histogram',0):+.4f}  [{m_dir}]")
    
    bb = result.get("bollinger", {})
    if bb:
        squeeze = "🔒 SQUEEZE — breakout incoming!" if bb.get("squeeze") else ""
        lines.append(f"    BB %B      : {bb.get('pct_b',0):.0f}%  width {bb.get('width',0):.1f}%  {squeeze}")
        lines.append(f"    BB upper   : {bb.get('upper',0)}  lower : {bb.get('lower',0)}")
    
    for ema_k, ema_label in [("ema9","EMA 9"), ("ema21","EMA 21"), ("ema50","EMA 50"), ("ema200","EMA 200")]:
        v = result.get(ema_k)
        if v:
            diff = round((price - v) / v * 100, 2)
            lines.append(f"    {ema_label:<10} : {v}  ({diff:+.2f}% from price)")
    
    # Breakout
    bo = result.get("breakout", {})
    lines.append(f"\n  ⚡ BREAKOUT DETECTION")
    if bo.get("type") == "none":
        lines.append(f"    No active breakout.")
    else:
        lines.append(f"    Type       : {bo.get('type','?').upper().replace('_',' ')}")
        lines.append(f"    Strength   : {bo.get('strength',0)}/100")
        lines.append(f"    Vol confirm: {'YES ✓' if bo.get('volume_confirmed') else 'NO (weak)'}")
        for sig in bo.get("signals", []):
            lines.append(f"    ↳ {sig}")
    
    # Volume
    vol = result.get("volume", {})
    lines.append(f"\n  📦 VOLUME")
    if vol:
        lines.append(f"    Status     : {vol.get('label','?')}")
        lines.append(f"    Ratio      : {vol.get('ratio',0)}x  vs 20-period avg")
        lines.append(f"    Buy press. : {vol.get('buy_pressure_pct',0)}%  Sell press. : {vol.get('sell_pressure_pct',0)}%")
        lines.append(f"    Vol trend  : {vol.get('volume_trend','?').upper()}")
    
    # Support & Resistance
    sr = result.get("support_resistance", {})
    lines.append(f"\n  🎯 SUPPORT & RESISTANCE")
    if sr:
        for r in sr.get("resistances", []):
            dist = round((r - price) / price * 100, 2)
            lines.append(f"    Resistance : {r}  (+{dist}%)")
        lines.append(f"    Pivot      : {sr.get('pivot','?')}")
        for s in sr.get("supports", []):
            dist = round((price - s) / price * 100, 2)
            lines.append(f"    Support    : {s}  (-{dist}%)")
    
    # Liquidity
    liq = result.get("liquidity_zones", {})
    lines.append(f"\n  💧 LIQUIDITY ZONES")
    if liq and liq.get("zones"):
        lines.append(f"    High-vol level: {liq.get('high_vol_level')}")
        for z in liq.get("zones", [])[:4]:
            lines.append(f"    {z['type'].upper():<10} {z['low']} – {z['high']}  ({z['vol_strength']}x avg vol)")
    else:
        lines.append(f"    Insufficient data for liquidity zones.")
    
    # Funding
    ft = result.get("funding_text")
    if ft:
        lines.append(f"\n  💸 FUNDING RATE")
        lines.append(f"    {ft}")
    
    # Trade plan
    tp = result.get("trade_plan", {})
    lines.append(f"\n  📋 TRADE PLAN")
    if tp.get("direction") == "WAIT":
        lines.append(f"    ⚪ No clear trade setup. Wait for confirmation.")
    elif tp:
        lines.append(f"    Direction  : {tp.get('direction')}")
        lines.append(f"    Entry zone : {tp.get('entry_zone')}")
        lines.append(f"    Stop-loss  : {tp.get('stop_loss')}")
        lines.append(f"    TP1        : {tp.get('take_profit_1')}")
        lines.append(f"    TP2        : {tp.get('take_profit_2')}")
        rr = tp.get("risk_reward", 0)
        rr_label = "✅ Good" if rr >= 2 else ("⚠ Low" if rr < 1.5 else "OK")
        lines.append(f"    Risk:Reward: 1:{rr}  [{rr_label}]")
        lines.append(f"    Note: {tp.get('note','')}")
    
    lines.append(f"\n  ⏱  Analyzed {result['analyzed_candles']} candles in {result['elapsed_seconds']}s")
    lines.append(f"{'═'*58}")
    
    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════════════════
# AI-POWERED TRADING CHAT
# ═════════════════════════════════════════════════════════════════════════════

# Chat context memory
_chat_context: Dict[str, List[Dict]] = {}


def chat(symbol: str, question: str, timeframe: str = "1H", use_context: bool = True) -> str:
    """
    Answer a trading question about a coin using live data + AI.
    Falls back to rule-based answer if no AI available.
    """
    symbol = symbol.upper()
    if "-" not in symbol:
        symbol = symbol + "-USDT"
    
    # Get fresh analysis
    result = analyze(symbol, timeframe)
    
    if result.get("status") == "error":
        return result.get("error", "Cannot analyze this symbol.")
    
    # Build context for AI
    context = f"""
Current {symbol} market analysis:
- Price: {result['price']}
- Signal: {result['recommendation']} (score: {result['signal_strength']}/100)
- Trend (short): {result.get('trend_short')}
- Trend (long): {result.get('trend_long')}
- RSI(14): {result.get('rsi')}
- MACD bullish: {result.get('macd',{}).get('bullish')}
- Bollinger squeeze: {result.get('bollinger',{}).get('squeeze')}
- Breakout: {result.get('breakout',{}).get('type')} (strength: {result.get('breakout',{}).get('strength')})
- Volume: {result.get('volume',{}).get('label')}
- Buy pressure: {result.get('volume',{}).get('buy_pressure_pct')}%
- Key resistance: {result.get('support_resistance',{}).get('resistances',['?'])[0] if result.get('support_resistance',{}).get('resistances') else 'none'}
- Key support: {result.get('support_resistance',{}).get('supports',['?'])[0] if result.get('support_resistance',{}).get('supports') else 'none'}
- Funding rate: {result.get('funding_text','N/A')}
- Trade plan: {result.get('trade_plan',{})}
"""
    
    prompt = f"Crypto trader question: {question}\n\nLive data:\n{context}\n\nGive a direct, clear trading answer in 3-5 sentences. Be specific about price levels."
    
    # Try AI providers
    try:
        import sys
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        with open(cfg_path, encoding='utf-8') as f:
            cfg = json.load(f)
        
        for provider, key_name, url, model in [
            ("groq", "groq", "https://api.groq.com/openai/v1/chat/completions", "llama-3.3-70b-versatile"),
            ("gemini", "gemini", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "gemini-1.5-flash"),
        ]:
            api_key = cfg.get("api_keys", {}).get(key_name, "")
            if not api_key:
                continue
            try:
                import urllib.request as _req
                payload = json.dumps({
                    "model": model,
                    "messages": [
                        {"role": "system", "content": "You are a professional crypto trading analyst. Give precise, actionable advice based on technical data."},
                        {"role": "user", "content": prompt}
                    ],
                    "max_tokens": 400,
                    "temperature": 0.3,
                }).encode()
                headers = {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                }
                r = _req.Request(url, data=payload, headers=headers)
                with _req.urlopen(r, timeout=15) as resp:
                    d = json.loads(resp.read())
                    answer = d["choices"][0]["message"]["content"].strip()
                    
                    # Store in context
                    if use_context:
                        if symbol not in _chat_context:
                            _chat_context[symbol] = []
                        _chat_context[symbol].append({"q": question, "a": answer})
                        _chat_context[symbol] = _chat_context[symbol][-10:]  # Keep last 10
                    
                    return answer
            except Exception:
                continue
    except Exception:
        pass
    
    # Rule-based fallback
    return _rule_based_answer(question, result)


def _rule_based_answer(question: str, result: Dict) -> str:
    """Answer trading questions without AI using pure analysis data."""
    q = question.lower()
    sym = result["symbol"]
    price = result["price"]
    rsi = result.get("rsi", 50)
    rec = result["recommendation"]
    breakout = result.get("breakout", {})
    sr = result.get("support_resistance", {})
    vol = result.get("volume", {})
    bb = result.get("bollinger", {})
    tp = result.get("trade_plan", {})
    
    if any(w in q for w in ["buy", "long", "enter", "entry"]):
        direction = tp.get("direction", "WAIT")
        if direction == "LONG":
            return (f"Based on current signals, {sym} shows a LONG setup at {price}. "
                    f"Entry zone: {tp.get('entry_zone')}. "
                    f"Stop-loss: {tp.get('stop_loss')}. TP1: {tp.get('take_profit_1')}. "
                    f"RSI is {rsi} — {'oversold, good entry' if rsi < 40 else 'neutral'}. "
                    f"Risk:Reward = 1:{tp.get('risk_reward', '?')}.")
        else:
            return (f"No clear BUY signal for {sym} right now. "
                    f"Current recommendation is {rec}. RSI: {rsi}. "
                    f"Wait for price to test support at {sr.get('supports', ['?'])[0] if sr.get('supports') else '?'} "
                    f"before entering long.")
    
    if any(w in q for w in ["sell", "short", "exit"]):
        direction = tp.get("direction", "WAIT")
        if direction == "SHORT":
            return (f"{sym} shows SHORT setup at {price}. "
                    f"Entry: {tp.get('entry_zone')}. Stop: {tp.get('stop_loss')}. "
                    f"TP1: {tp.get('take_profit_1')}. RSI: {rsi} — "
                    f"{'overbought' if rsi > 60 else 'neutral'}.")
        else:
            return (f"No strong SELL signal for {sym}. Overall signal: {rec}. "
                    f"Key resistance to watch: {sr.get('resistances', ['?'])[0] if sr.get('resistances') else '?' }.")
    
    if any(w in q for w in ["breakout", "break", "pump"]):
        bo_type = breakout.get("type", "none")
        if bo_type != "none":
            return (f"BREAKOUT DETECTED for {sym}! Type: {bo_type.replace('_',' ').upper()}. "
                    f"Strength: {breakout.get('strength')}/100. "
                    f"Volume confirmed: {'Yes' if breakout.get('volume_confirmed') else 'No — weak signal'}. "
                    f"{breakout.get('description','')}")
        elif bb.get("squeeze"):
            return (f"Bollinger Band SQUEEZE on {sym} — price is coiling. "
                    f"A breakout is building up. Watch for a candle close outside the bands "
                    f"with high volume. Current BB width: {bb.get('width')}%.")
        else:
            return f"No active breakout on {sym} right now. Price at {price}."
    
    if any(w in q for w in ["support", "level", "floor"]):
        sups = sr.get("supports", [])
        return (f"{sym} key supports: {', '.join(str(s) for s in sups[:3]) if sups else 'calculating...'}. "
                f"Pivot point: {sr.get('pivot','?')}. "
                f"Nearest support is {sups[0] if sups else '?'} "
                f"({round((price - sups[0]) / price * 100, 2) if sups else '?'}% below current price).")
    
    if any(w in q for w in ["resist", "ceiling", "target"]):
        ress = sr.get("resistances", [])
        return (f"{sym} key resistances: {', '.join(str(r) for r in ress[:3]) if ress else 'none'}. "
                f"First target: {ress[0] if ress else '?' }.")
    
    if any(w in q for w in ["volume", "vol"]):
        return (f"{sym} volume: {vol.get('label','?')}. "
                f"Current is {vol.get('ratio','?')}x the 20-period average. "
                f"Buy pressure: {vol.get('buy_pressure_pct','?')}% vs sell pressure {vol.get('sell_pressure_pct','?')}%.")
    
    if any(w in q for w in ["rsi", "oversold", "overbought"]):
        label = "OVERSOLD" if rsi < 30 else ("OVERBOUGHT" if rsi > 70 else "NEUTRAL")
        return f"{sym} RSI(14) = {rsi} — {label}. {'Potential bounce zone.' if rsi < 35 else ('Watch for reversal.' if rsi > 65 else 'No extreme reading.') }"
    
    if any(w in q for w in ["liquidity", "liq", "whale"]):
        liq = result.get("liquidity_zones", {})
        zones = liq.get("zones", [])
        hvl = liq.get("high_vol_level")
        return (f"{sym} has {len(zones)} high-liquidity zones. "
                f"Highest volume cluster: {hvl}. "
                f"These zones attract price — expect bounces or rejections there.")
    
    # Default: full summary
    return (f"{sym} @ {price}. Signal: {rec}. "
            f"RSI: {rsi}, Trend: {result.get('trend_short','?')} (short) / {result.get('trend_long','?')} (long). "
            f"Breakout: {breakout.get('type','none')}. Volume: {vol.get('label','normal')}.")


# ═════════════════════════════════════════════════════════════════════════════
# UTILITY FUNCTIONS
# ═════════════════════════════════════════════════════════════════════════════

def clear_cache() -> None:
    """Clear the data cache."""
    _data_cache.clear()
    log.info("Cache cleared")


def get_cache_stats() -> Dict[str, Any]:
    """Get cache statistics."""
    return {
        "memory_entries": len(_data_cache.memory_cache),
        "cache_dir": CACHE_DIR,
        "ttl_seconds": _data_cache.ttl
    }


def multi_timeframe_analysis(symbol: str, timeframes: List[str] = None) -> Dict[str, Any]:
    """Analyze a symbol across multiple timeframes."""
    if timeframes is None:
        timeframes = ["15m", "1H", "4H", "1D"]
    
    results = {}
    for tf in timeframes:
        try:
            results[tf] = analyze(symbol, tf)
        except Exception as e:
            results[tf] = {"status": "error", "error": str(e)}
    
    # Calculate overall signal
    strengths = [r.get("signal_strength", 50) for r in results.values() if r.get("status") == "ok"]
    avg_strength = sum(strengths) / len(strengths) if strengths else 50
    
    return {
        "symbol": symbol,
        "timeframes": results,
        "average_strength": round(avg_strength, 1),
        "overall_bias": "BULLISH" if avg_strength > 55 else ("BEARISH" if avg_strength < 45 else "NEUTRAL")
    }


# Export main functions
__all__ = [
    'analyze',
    'chat',
    'format_analysis',
    'multi_timeframe_analysis',
    'clear_cache',
    'get_cache_stats',
    'SignalResult',
    'DataCache'
]
