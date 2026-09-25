"""
AENIDA Technical Indicators
15 core trading indicators with data sufficiency checks.
BUG-34: Data sufficiency check before every calculation.
"""

import numpy as np
from typing import Dict, Any, List, Optional
from dataclasses import dataclass


# BUG-34: Minimum data requirements
MIN_DATA = {
    "rsi": 15,
    "macd": 35,
    "bollinger": 22,
    "ichimoku": 53,
    "stochastic": 15,
    "vwap": 2,
    "atr": 14,
    "obv": 2,
    "sma_20": 21,
    "sma_50": 51,
    "ema_12": 13,
    "ema_26": 27
}


@dataclass
class IndicatorResult:
    """Indicator calculation result."""
    value: Any
    status: str
    error: Optional[str] = None


class TechnicalIndicators:
    """
    Technical analysis indicators with data sufficiency checks.
    BUG-34: Returns None with status instead of crashing.
    """
    
    __slots__ = []
    
    def __init__(self):
        pass
    
    def _check_sufficiency(self, indicator: str, data_len: int) -> bool:
        """Check if enough data for indicator."""
        min_required = MIN_DATA.get(indicator, 10)
        return data_len >= min_required
    
    def calculate_rsi(self, closes: List[float], period: int = 14) -> IndicatorResult:
        """
        Calculate Relative Strength Index.
        
        Args:
            closes: List of closing prices
            period: RSI period (default 14)
            
        Returns:
            IndicatorResult with RSI value
        """
        if not self._check_sufficiency("rsi", len(closes)):
            return IndicatorResult(
                value=None, 
                status="insufficient_data",
                error=f"Need {MIN_DATA['rsi']} data points, got {len(closes)}"
            )
        
        try:
            deltas = np.diff(closes)
            gains = np.where(deltas > 0, deltas, 0)
            losses = np.where(deltas < 0, -deltas, 0)
            
            avg_gain = np.mean(gains[:period])
            avg_loss = np.mean(losses[:period])
            
            for i in range(period, len(gains)):
                avg_gain = (avg_gain * (period - 1) + gains[i]) / period
                avg_loss = (avg_loss * (period - 1) + losses[i]) / period
            
            if avg_loss == 0:
                rsi = 100
            else:
                rs = avg_gain / avg_loss
                rsi = 100 - (100 / (1 + rs))
            
            return IndicatorResult(value=round(rsi, 2), status="ok")
            
        except Exception as e:
            return IndicatorResult(value=None, status="error", error=str(e))
    
    def calculate_macd(self, closes: List[float], fast: int = 12,
                       slow: int = 26, signal: int = 9) -> IndicatorResult:
        """Calculate MACD."""
        if not self._check_sufficiency("macd", len(closes)):
            return IndicatorResult(
                value=None,
                status="insufficient_data",
                error=f"Need {MIN_DATA['macd']} data points, got {len(closes)}"
            )
        
        try:
            ema_fast = self._calculate_ema(closes, fast)
            ema_slow = self._calculate_ema(closes, slow)
            
            macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
            signal_line = self._calculate_ema(macd_line, signal)
            
            histogram = [m - s for m, s in zip(macd_line[-len(signal_line):], signal_line)]
            
            return IndicatorResult(value={
                "macd": round(macd_line[-1], 4),
                "signal": round(signal_line[-1], 4),
                "histogram": round(histogram[-1], 4)
            }, status="ok")
            
        except Exception as e:
            return IndicatorResult(value=None, status="error", error=str(e))
    
    def calculate_bollinger(self, closes: List[float], period: int = 20,
                           std_dev: float = 2.0) -> IndicatorResult:
        """Calculate Bollinger Bands."""
        if not self._check_sufficiency("bollinger", len(closes)):
            return IndicatorResult(
                value=None,
                status="insufficient_data",
                error=f"Need {MIN_DATA['bollinger']} data points, got {len(closes)}"
            )
        
        try:
            sma = np.mean(closes[-period:])
            std = np.std(closes[-period:])
            
            upper = sma + (std * std_dev)
            lower = sma - (std * std_dev)
            
            # Position within bands (0-100)
            current = closes[-1]
            position = (current - lower) / (upper - lower) * 100 if upper != lower else 50
            
            return IndicatorResult(value={
                "upper": round(upper, 2),
                "middle": round(sma, 2),
                "lower": round(lower, 2),
                "position": round(position, 2)
            }, status="ok")
            
        except Exception as e:
            return IndicatorResult(value=None, status="error", error=str(e))
    
    def calculate_sma(self, closes: List[float], period: int) -> IndicatorResult:
        """Calculate Simple Moving Average."""
        key = f"sma_{period}"
        if not self._check_sufficiency(key, len(closes)):
            return IndicatorResult(
                value=None,
                status="insufficient_data",
                error=f"Need {period + 1} data points, got {len(closes)}"
            )
        
        try:
            sma = np.mean(closes[-period:])
            return IndicatorResult(value=round(sma, 2), status="ok")
        except Exception as e:
            return IndicatorResult(value=None, status="error", error=str(e))
    
    def calculate_ema(self, closes: List[float], period: int) -> IndicatorResult:
        """Calculate Exponential Moving Average."""
        key = f"ema_{period}"
        if not self._check_sufficiency(key, len(closes)):
            return IndicatorResult(
                value=None,
                status="insufficient_data",
                error=f"Need {period + 1} data points, got {len(closes)}"
            )
        
        try:
            ema = self._calculate_ema(closes, period)[-1]
            return IndicatorResult(value=round(ema, 2), status="ok")
        except Exception as e:
            return IndicatorResult(value=None, status="error", error=str(e))
    
    def _calculate_ema(self, data: List[float], period: int) -> List[float]:
        """Internal EMA calculation."""
        multiplier = 2 / (period + 1)
        ema = [data[0]]
        
        for price in data[1:]:
            ema.append((price - ema[-1]) * multiplier + ema[-1])
        
        return ema
    
    def calculate_vwap(self, highs: List[float], lows: List[float],
                      closes: List[float], volumes: List[float]) -> IndicatorResult:
        """Calculate Volume Weighted Average Price."""
        if not self._check_sufficiency("vwap", len(closes)):
            return IndicatorResult(
                value=None,
                status="insufficient_data",
                error=f"Need {MIN_DATA['vwap']} data points"
            )
        
        try:
            typical_prices = [(h + l + c) / 3 for h, l, c in zip(highs, lows, closes)]
            
            cumulative_tp_vol = sum(tp * v for tp, v in zip(typical_prices, volumes))
            cumulative_vol = sum(volumes)
            
            vwap = cumulative_tp_vol / cumulative_vol if cumulative_vol > 0 else 0
            
            # Position relative to VWAP
            current = closes[-1]
            position = "above" if current > vwap else "below"
            
            return IndicatorResult(value={
                "vwap": round(vwap, 2),
                "position": position,
                "current": round(current, 2)
            }, status="ok")
            
        except Exception as e:
            return IndicatorResult(value=None, status="error", error=str(e))
    
    def calculate_atr(self, highs: List[float], lows: List[float],
                     closes: List[float], period: int = 14) -> IndicatorResult:
        """Calculate Average True Range."""
        if not self._check_sufficiency("atr", len(closes)):
            return IndicatorResult(
                value=None,
                status="insufficient_data",
                error=f"Need {MIN_DATA['atr']} data points, got {len(closes)}"
            )
        
        try:
            tr_list = []
            for i in range(1, len(closes)):
                tr1 = highs[i] - lows[i]
                tr2 = abs(highs[i] - closes[i-1])
                tr3 = abs(lows[i] - closes[i-1])
                tr_list.append(max(tr1, tr2, tr3))
            
            atr = np.mean(tr_list[-period:])
            
            return IndicatorResult(value=round(atr, 4), status="ok")
            
        except Exception as e:
            return IndicatorResult(value=None, status="error", error=str(e))
    
    def detect_trend(self, closes: List[float], lookback: int = 20) -> str:
        """Detect trend direction."""
        if len(closes) < lookback + 1:
            return "unknown"
        
        recent = closes[-lookback:]
        first_half = np.mean(recent[:lookback//2])
        second_half = np.mean(recent[lookback//2:])
        
        if second_half > first_half * 1.02:
            return "bullish"
        elif second_half < first_half * 0.98:
            return "bearish"
        else:
            return "sideways"
    
    def generate_signals(self, symbol: str, ohlcv_data: List[Dict]) -> Dict[str, Any]:
        """
        Generate unified trading signals from OHLCV data.
        
        Args:
            symbol: Trading pair symbol
            ohlcv_data: List of OHLCV dictionaries
            
        Returns:
            Unified signal dictionary
        """
        if len(ohlcv_data) < 20:
            return {
                "symbol": symbol,
                "status": "insufficient_data",
                "message": "Collecting data..."
            }
        
        # Extract data
        closes = [c["close"] for c in ohlcv_data]
        highs = [c["high"] for c in ohlcv_data]
        lows = [c["low"] for c in ohlcv_data]
        volumes = [c["volume"] for c in ohlcv_data]
        
        # Calculate indicators
        rsi = self.calculate_rsi(closes)
        macd = self.calculate_macd(closes)
        bollinger = self.calculate_bollinger(closes)
        sma20 = self.calculate_sma(closes, 20)
        sma50 = self.calculate_sma(closes, 50) if len(closes) >= 51 else IndicatorResult(None, "insufficient_data")
        vwap = self.calculate_vwap(highs, lows, closes, volumes)
        atr = self.calculate_atr(highs, lows, closes)
        
        # Detect trend
        trend = self.detect_trend(closes)
        
        # Calculate signal strength
        signals = []
        
        if rsi.value is not None:
            if rsi.value < 30:
                signals.append(1)  # Oversold = bullish
            elif rsi.value > 70:
                signals.append(-1)  # Overbought = bearish
        
        if macd.value is not None:
            if macd.value.get("histogram", 0) > 0:
                signals.append(1)
            else:
                signals.append(-1)
        
        if bollinger.value is not None:
            pos = bollinger.value.get("position", 50)
            if pos < 20:
                signals.append(1)
            elif pos > 80:
                signals.append(-1)
        
        # Calculate strength (0-100)
        if signals:
            avg_signal = sum(signals) / len(signals)
            signal_strength = int((avg_signal + 1) * 50)
        else:
            signal_strength = 50
        
        # Determine recommendation
        if signal_strength >= 70:
            recommendation = "BUY"
        elif signal_strength <= 30:
            recommendation = "SELL"
        elif signal_strength >= 60 or signal_strength <= 40:
            recommendation = "WATCH"
        else:
            recommendation = "HOLD"
        
        return {
            "symbol": symbol,
            "price": round(closes[-1], 2),
            "trend": trend,
            "rsi": rsi.value,
            "macd": macd.value,
            "bollinger": bollinger.value,
            "sma20": sma20.value,
            "sma50": sma50.value,
            "vwap": vwap.value,
            "atr": atr.value,
            "signal_strength": signal_strength,
            "recommended": recommendation,
            "status": "ok"
        }


# Global instance
_indicators: Optional[TechnicalIndicators] = None


def get_indicators() -> TechnicalIndicators:
    """Get or create global indicators."""
    global _indicators
    if _indicators is None:
        _indicators = TechnicalIndicators()
    return _indicators


def generate_signals(symbol: str, ohlcv_data: List[Dict]) -> Dict[str, Any]:
    """Generate trading signals."""
    return get_indicators().generate_signals(symbol, ohlcv_data)


def calculate_rsi(closes: List[float], period: int = 14) -> IndicatorResult:
    """Calculate RSI."""
    return get_indicators().calculate_rsi(closes, period)
