"""
Tests for technical_indicators.py
"""

import pytest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import path_setup  # registers all sub-packages

from technical_indicators import TechnicalIndicators, MIN_DATA


class TestTechnicalIndicators:
    """Test technical indicators."""
    
    def generate_sample_data(self, count: int) -> list:
        """Generate sample OHLCV data."""
        data = []
        base = 50000
        for i in range(count):
            close = base + i * 100 + (i % 5) * 50
            data.append({
                "open": close - 50,
                "high": close + 100,
                "low": close - 100,
                "close": close,
                "volume": 1000 + i * 10
            })
        return data
    
    def test_calculate_rsi_sufficient_data(self):
        """Test RSI with sufficient data."""
        indicators = TechnicalIndicators()
        closes = [self.generate_sample_data(50)[i]["close"] for i in range(50)]
        
        result = indicators.calculate_rsi(closes)
        
        assert result.status == "ok"
        assert result.value is not None
        assert 0 <= result.value <= 100
    
    def test_calculate_rsi_insufficient_data(self):
        """Test BUG-34: RSI with insufficient data."""
        indicators = TechnicalIndicators()
        closes = [50000, 50100, 50200]  # Only 3 points
        
        result = indicators.calculate_rsi(closes)
        
        assert result.status == "insufficient_data"
        assert result.value is None
    
    def test_calculate_macd_sufficient_data(self):
        """Test MACD with sufficient data."""
        indicators = TechnicalIndicators()
        closes = [self.generate_sample_data(50)[i]["close"] for i in range(50)]
        
        result = indicators.calculate_macd(closes)
        
        assert result.status == "ok"
        assert result.value is not None
        assert "macd" in result.value
        assert "signal" in result.value
        assert "histogram" in result.value
    
    def test_calculate_macd_insufficient_data(self):
        """Test BUG-34: MACD with insufficient data."""
        indicators = TechnicalIndicators()
        closes = [50000 + i * 100 for i in range(10)]
        
        result = indicators.calculate_macd(closes)
        
        assert result.status == "insufficient_data"
        assert result.value is None
    
    def test_calculate_bollinger(self):
        """Test Bollinger Bands."""
        indicators = TechnicalIndicators()
        closes = [self.generate_sample_data(50)[i]["close"] for i in range(50)]
        
        result = indicators.calculate_bollinger(closes)
        
        assert result.status == "ok"
        assert result.value is not None
        assert "upper" in result.value
        assert "middle" in result.value
        assert "lower" in result.value
        assert "position" in result.value
    
    def test_calculate_sma(self):
        """Test SMA calculation."""
        indicators = TechnicalIndicators()
        closes = [self.generate_sample_data(50)[i]["close"] for i in range(50)]
        
        result = indicators.calculate_sma(closes, 20)
        
        assert result.status == "ok"
        assert result.value is not None
    
    def test_detect_trend_bullish(self):
        """Test bullish trend detection."""
        indicators = TechnicalIndicators()
        # Rising prices
        closes = [100 + i * 10 for i in range(30)]
        
        trend = indicators.detect_trend(closes)
        
        assert trend == "bullish"
    
    def test_detect_trend_bearish(self):
        """Test bearish trend detection."""
        indicators = TechnicalIndicators()
        # Falling prices
        closes = [400 - i * 10 for i in range(30)]
        
        trend = indicators.detect_trend(closes)
        
        assert trend == "bearish"
    
    def test_generate_signals(self):
        """Test unified signal generation."""
        indicators = TechnicalIndicators()
        data = self.generate_sample_data(60)
        
        signals = indicators.generate_signals("BTC-USDT", data)
        
        assert "symbol" in signals
        assert "price" in signals
        assert "trend" in signals
        assert "signal_strength" in signals
        assert "recommended" in signals
        assert signals["status"] == "ok"
    
    def test_generate_signals_insufficient(self):
        """Test signals with insufficient data."""
        indicators = TechnicalIndicators()
        data = self.generate_sample_data(5)
        
        signals = indicators.generate_signals("BTC-USDT", data)
        
        assert signals["status"] == "insufficient_data"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
