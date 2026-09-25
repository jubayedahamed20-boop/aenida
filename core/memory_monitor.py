"""
AENIDA Memory Monitor
RAM monitoring with graduated backpressure (BUG-15 fix).
"""

import time
import logging
import threading
from typing import Dict, Callable, Optional
from dataclasses import dataclass

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    logging.warning("psutil not available - memory monitoring disabled")


@dataclass
class MemoryThreshold:
    """Memory threshold configuration."""
    percent: float
    action: str
    callback: Optional[Callable] = None


class MemoryMonitor:
    """
    Monitor system memory and apply graduated backpressure.
    85%→github | 88%→synthesizer | 90%→news | 93%→capture | 95%→emergency
    """
    
    __slots__ = ['warning_mb', 'critical_mb', 'check_interval', 
                 'pause_flags', 'running', '_thread', '_lock',
                 'callbacks', 'backpressure_percent']
    
    def __init__(self, 
                 warning_mb: int = 150, 
                 critical_mb: int = 200,
                 check_interval_seconds: int = 60,
                 backpressure_percent: float = 85.0):
        self.warning_mb = warning_mb
        self.critical_mb = critical_mb
        self.check_interval = check_interval_seconds
        self.backpressure_percent = backpressure_percent
        
        # Pause flags for each service
        self.pause_flags: Dict[str, bool] = {
            "github_explorer": False,
            "nightly_synthesizer": False,
            "news_watcher": False,
            "trading_capture": False,
            "emergency": False
        }
        
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.callbacks: Dict[str, Optional[Callable]] = {
            "github_explorer": None,
            "nightly_synthesizer": None,
            "news_watcher": None,
            "trading_capture": None,
            "emergency": None
        }
    
    def start(self) -> None:
        """Start memory monitoring in background thread."""
        if not PSUTIL_AVAILABLE:
            logging.warning("Cannot start memory monitor - psutil unavailable")
            return
        
        self.running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        logging.info("Memory monitor started")
    
    def stop(self) -> None:
        """Stop memory monitoring."""
        self.running = False
        if self._thread:
            self._thread.join(timeout=5)
        logging.info("Memory monitor stopped")
    
    def _monitor_loop(self) -> None:
        """Main monitoring loop."""
        while self.running:
            try:
                self._check_memory()
            except Exception as e:
                logging.error(f"Memory check error: {e}")
            
            time.sleep(self.check_interval)
    
    def _check_memory(self) -> None:
        """Check memory and apply backpressure."""
        mem = psutil.virtual_memory()
        used_mb = mem.used / (1024 * 1024)
        percent = mem.percent
        
        logging.debug(f"Memory: {used_mb:.1f}MB ({percent:.1f}%)")
        
        with self._lock:
            # Graduated backpressure (BUG-15)
            
            # 85% → pause github_explorer (lowest priority)
            if percent >= 85:
                if not self.pause_flags["github_explorer"]:
                    self.pause_flags["github_explorer"] = True
                    logging.warning("Memory at 85%+ - pausing github_explorer")
                    if self.callbacks["github_explorer"]:
                        self.callbacks["github_explorer"](True)
            else:
                if self.pause_flags["github_explorer"]:
                    self.pause_flags["github_explorer"] = False
                    logging.info("Memory below 85% - resuming github_explorer")
                    if self.callbacks["github_explorer"]:
                        self.callbacks["github_explorer"](False)
            
            # 88% → pause nightly_synthesizer
            if percent >= 88:
                if not self.pause_flags["nightly_synthesizer"]:
                    self.pause_flags["nightly_synthesizer"] = True
                    logging.warning("Memory at 88%+ - pausing nightly_synthesizer")
                    if self.callbacks["nightly_synthesizer"]:
                        self.callbacks["nightly_synthesizer"](True)
            else:
                if self.pause_flags["nightly_synthesizer"]:
                    self.pause_flags["nightly_synthesizer"] = False
                    logging.info("Memory below 88% - resuming nightly_synthesizer")
                    if self.callbacks["nightly_synthesizer"]:
                        self.callbacks["nightly_synthesizer"](False)
            
            # 90% → pause news_watcher
            if percent >= 90:
                if not self.pause_flags["news_watcher"]:
                    self.pause_flags["news_watcher"] = True
                    logging.warning("Memory at 90%+ - pausing news_watcher")
                    if self.callbacks["news_watcher"]:
                        self.callbacks["news_watcher"](True)
            else:
                if self.pause_flags["news_watcher"]:
                    self.pause_flags["news_watcher"] = False
                    logging.info("Memory below 90% - resuming news_watcher")
                    if self.callbacks["news_watcher"]:
                        self.callbacks["news_watcher"](False)
            
            # 93% → pause trading screen capture
            if percent >= 93:
                if not self.pause_flags["trading_capture"]:
                    self.pause_flags["trading_capture"] = True
                    logging.warning("Memory at 93%+ - pausing trading capture")
                    if self.callbacks["trading_capture"]:
                        self.callbacks["trading_capture"](True)
            else:
                if self.pause_flags["trading_capture"]:
                    self.pause_flags["trading_capture"] = False
                    logging.info("Memory below 93% - resuming trading capture")
                    if self.callbacks["trading_capture"]:
                        self.callbacks["trading_capture"](False)
            
            # 95% → emergency flush all + CRITICAL alert
            if percent >= 95:
                if not self.pause_flags["emergency"]:
                    self.pause_flags["emergency"] = True
                    logging.critical("EMERGENCY: Memory at 95%+ - flushing all!")
                    self._emergency_flush()
                    if self.callbacks["emergency"]:
                        self.callbacks["emergency"](True)
            else:
                if self.pause_flags["emergency"]:
                    self.pause_flags["emergency"] = False
                    logging.info("Memory below 95% - emergency cleared")
                    if self.callbacks["emergency"]:
                        self.callbacks["emergency"](False)
    
    def _emergency_flush(self) -> None:
        """Emergency memory flush."""
        # Import here to avoid circular dependency
        try:
            import gc
            gc.collect()
            logging.info("Emergency garbage collection completed")
        except Exception as e:
            logging.error(f"Emergency flush failed: {e}")
    
    def get_memory_stats(self) -> Dict:
        """Get current memory statistics."""
        if not PSUTIL_AVAILABLE:
            return {"error": "psutil not available"}
        
        mem = psutil.virtual_memory()
        return {
            "total_mb": mem.total / (1024 * 1024),
            "available_mb": mem.available / (1024 * 1024),
            "used_mb": mem.used / (1024 * 1024),
            "percent": mem.percent,
            "warning_mb": self.warning_mb,
            "critical_mb": self.critical_mb
        }
    
    def is_paused(self, service: str) -> bool:
        """Check if a service is paused."""
        with self._lock:
            return self.pause_flags.get(service, False)
    
    def get_pause_status(self) -> Dict[str, bool]:
        """Get pause status of all services."""
        with self._lock:
            return self.pause_flags.copy()
    
    def register_callback(self, service: str, callback: Callable[[bool], None]) -> None:
        """Register a callback for pause/resume events."""
        self.callbacks[service] = callback


# Global instance
_monitor: Optional[MemoryMonitor] = None


def get_monitor() -> MemoryMonitor:
    """Get or create global memory monitor."""
    global _monitor
    if _monitor is None:
        _monitor = MemoryMonitor()
    return _monitor


def start_monitoring() -> None:
    """Start memory monitoring."""
    get_monitor().start()


def stop_monitoring() -> None:
    """Stop memory monitoring."""
    get_monitor().stop()


def get_memory_stats() -> Dict:
    """Get memory statistics."""
    return get_monitor().get_memory_stats()


def is_paused(service: str) -> bool:
    """Check if service is paused."""
    return get_monitor().is_paused(service)
