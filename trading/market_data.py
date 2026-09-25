"""
AENIDA Market Data
Free real-time price data from OKX, Binance, CoinGecko.
BUG-33: Rate limiting implemented.
"""

import time
import logging
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
import threading


@dataclass
class OHLCV:
    """OHLCV candle data."""
    timestamp: float
    open: float
    high: float
    low: float
    close: float
    volume: float


class RateLimiter:
    """Token bucket rate limiter."""
    
    __slots__ = ['rate', 'tokens', 'last_update', '_lock']
    
    def __init__(self, rate_per_second: float):
        self.rate = rate_per_second
        self.tokens = rate_per_second
        self.last_update = time.time()
        self._lock = threading.Lock()
    
    def acquire(self) -> bool:
        """Acquire a token. Returns True if allowed."""
        with self._lock:
            now = time.time()
            elapsed = now - self.last_update
            self.tokens = min(self.rate, self.tokens + elapsed * self.rate)
            self.last_update = now
            
            if self.tokens >= 1:
                self.tokens -= 1
                return True
            return False
    
    def wait_time(self) -> float:
        """Get time to wait for next token."""
        with self._lock:
            if self.tokens >= 1:
                return 0
            return (1 - self.tokens) / self.rate


class MarketData:
    """
    Free market data provider with rate limiting.
    BUG-33: Token bucket rate limiter implemented.
    """
    
    __slots__ = ['okx_limiter', 'binance_limiter', 'coingecko_limiter',
                 'retry_wait', 'ban_wait', 'banned_until']
    
    def __init__(self):
        # Rate limiters (BUG-33)
        self.okx_limiter = RateLimiter(10)      # 10 req/s
        self.binance_limiter = RateLimiter(20)  # 20 req/s
        self.coingecko_limiter = RateLimiter(5) # 5 req/s
        
        self.retry_wait = 2
        self.ban_wait = 60
        self.banned_until: Dict[str, float] = {}
    
    def _is_banned(self, source: str) -> bool:
        """Check if source is temporarily banned."""
        if source in self.banned_until:
            if time.time() < self.banned_until[source]:
                return True
            del self.banned_until[source]
        return False
    
    def _ban(self, source: str) -> None:
        """Ban source temporarily."""
        self.banned_until[source] = time.time() + self.ban_wait
        logging.warning(f"Banned {source} for {self.ban_wait}s due to rate limit")
    
    def fetch_crypto_ohlcv(self, symbol: str, interval: str = "1H", 
                           limit: int = 100) -> List[OHLCV]:
        """
        Fetch OHLCV data from OKX (primary) or Binance (backup).
        
        Args:
            symbol: Trading pair (e.g., "BTC-USDT")
            interval: Candle interval (1H, 4H, 1D)
            limit: Number of candles
            
        Returns:
            List of OHLCV objects
        """
        # Try OKX first
        result = self._fetch_okx_ohlcv(symbol, interval, limit)
        if result:
            return result
        
        # Fallback to Binance
        result = self._fetch_binance_ohlcv(symbol, interval, limit)
        if result:
            return result
        
        return []
    
    def _fetch_okx_ohlcv(self, symbol: str, interval: str, 
                         limit: int) -> Optional[List[OHLCV]]:
        """Fetch from OKX public API."""
        if self._is_banned("okx"):
            return None
        
        if not self.okx_limiter.acquire():
            time.sleep(self.okx_limiter.wait_time())
        
        try:
            import requests
            
            # Convert symbol format
            okx_symbol = symbol.replace("-", "")
            
            # Map interval
            interval_map = {"1H": "1H", "4H": "4H", "1D": "1D", "1W": "1W"}
            okx_interval = interval_map.get(interval, "1H")
            
            url = f"https://www.okx.com/api/v5/market/candles"
            params = {
                "instId": symbol,
                "bar": okx_interval,
                "limit": limit
            }
            
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code == 429:
                self._ban("okx")
                return None
            
            if response.status_code != 200:
                return None
            
            data = response.json()
            
            if data.get("code") != "0":
                return None
            
            candles = []
            for item in data.get("data", []):
                # OKX format: [ts, o, h, l, c, vol, volCcy]
                candles.append(OHLCV(
                    timestamp=float(item[0]) / 1000,
                    open=float(item[1]),
                    high=float(item[2]),
                    low=float(item[3]),
                    close=float(item[4]),
                    volume=float(item[5])
                ))
            
            return candles
            
        except Exception as e:
            logging.error(f"OKX fetch error: {e}")
            return None
    
    def _fetch_binance_ohlcv(self, symbol: str, interval: str,
                             limit: int) -> Optional[List[OHLCV]]:
        """Fetch from Binance public API."""
        if self._is_banned("binance"):
            return None
        
        if not self.binance_limiter.acquire():
            time.sleep(self.binance_limiter.wait_time())
        
        try:
            import requests
            
            # Convert symbol format
            binance_symbol = symbol.replace("-", "")
            
            # Map interval
            interval_map = {"1H": "1h", "4H": "4h", "1D": "1d", "1W": "1w"}
            binance_interval = interval_map.get(interval, "1h")
            
            url = f"https://api.binance.com/api/v3/klines"
            params = {
                "symbol": binance_symbol.upper(),
                "interval": binance_interval,
                "limit": limit
            }
            
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code == 429:
                self._ban("binance")
                return None
            
            if response.status_code != 200:
                return None
            
            data = response.json()
            
            candles = []
            for item in data:
                # Binance format: [ts, o, h, l, c, v, ...]
                candles.append(OHLCV(
                    timestamp=float(item[0]) / 1000,
                    open=float(item[1]),
                    high=float(item[2]),
                    low=float(item[3]),
                    close=float(item[4]),
                    volume=float(item[5])
                ))
            
            return candles
            
        except Exception as e:
            logging.error(f"Binance fetch error: {e}")
            return None
    
    def get_current_price(self, symbol: str) -> Optional[float]:
        """Get current price from OKX or Binance."""
        # Try OKX
        price = self._get_okx_price(symbol)
        if price:
            return price
        
        # Fallback to Binance
        price = self._get_binance_price(symbol)
        if price:
            return price
        
        return None
    
    def _get_okx_price(self, symbol: str) -> Optional[float]:
        """Get price from OKX."""
        if self._is_banned("okx"):
            return None
        
        if not self.okx_limiter.acquire():
            time.sleep(self.okx_limiter.wait_time())
        
        try:
            import requests
            
            url = f"https://www.okx.com/api/v5/market/ticker"
            params = {"instId": symbol}
            
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code == 429:
                self._ban("okx")
                return None
            
            if response.status_code != 200:
                return None
            
            data = response.json()
            
            if data.get("code") == "0" and data.get("data"):
                return float(data["data"][0]["last"])
            
            return None
            
        except Exception as e:
            logging.error(f"OKX price error: {e}")
            return None
    
    def _get_binance_price(self, symbol: str) -> Optional[float]:
        """Get price from Binance."""
        if self._is_banned("binance"):
            return None
        
        if not self.binance_limiter.acquire():
            time.sleep(self.binance_limiter.wait_time())
        
        try:
            import requests
            
            binance_symbol = symbol.replace("-", "")
            
            url = f"https://api.binance.com/api/v3/ticker/price"
            params = {"symbol": binance_symbol.upper()}
            
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code == 429:
                self._ban("binance")
                return None
            
            if response.status_code != 200:
                return None
            
            data = response.json()
            return float(data.get("price", 0))
            
        except Exception as e:
            logging.error(f"Binance price error: {e}")
            return None
    
    def get_funding_rate(self, symbol: str) -> Optional[float]:
        """Get funding rate from OKX."""
        if self._is_banned("okx"):
            return None
        
        if not self.okx_limiter.acquire():
            time.sleep(self.okx_limiter.wait_time())
        
        try:
            import requests
            
            url = f"https://www.okx.com/api/v5/public/funding-rate"
            params = {"instId": symbol}
            
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code == 429:
                self._ban("okx")
                return None
            
            if response.status_code != 200:
                return None
            
            data = response.json()
            
            if data.get("code") == "0" and data.get("data"):
                return float(data["data"][0].get("fundingRate", 0))
            
            return None
            
        except Exception as e:
            logging.error(f"OKX funding rate error: {e}")
            return None
    
    def get_market_snapshot(self, symbols: List[str]) -> Dict[str, Any]:
        """Get snapshot for multiple symbols."""
        snapshot = {}
        
        for symbol in symbols:
            price = self.get_current_price(symbol)
            funding = self.get_funding_rate(symbol)
            
            snapshot[symbol] = {
                "price": price,
                "funding_rate": funding,
                "timestamp": time.time()
            }
        
        return snapshot


# Global instance
_md: Optional[MarketData] = None


def get_market_data() -> MarketData:
    """Get or create global market data."""
    global _md
    if _md is None:
        _md = MarketData()
    return _md


def fetch_crypto_ohlcv(symbol: str, interval: str = "1H", 
                       limit: int = 100) -> List[OHLCV]:
    """Fetch OHLCV data."""
    return get_market_data().fetch_crypto_ohlcv(symbol, interval, limit)


def get_current_price(symbol: str) -> Optional[float]:
    """Get current price."""
    return get_market_data().get_current_price(symbol)


def get_funding_rate(symbol: str) -> Optional[float]:
    """Get funding rate."""
    return get_market_data().get_funding_rate(symbol)


def get_market_snapshot(symbols: List[str]) -> Dict[str, Any]:
    """Get market snapshot."""
    return get_market_data().get_market_snapshot(symbols)
