"""
AENIDA Alert Manager
Single alert handler for ALL system alerts with rate limiting.
"""

import time
import logging
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from enum import Enum
import threading


class AlertLevel(Enum):
    """Alert severity levels."""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class Alert:
    """Alert data structure."""
    level: AlertLevel
    title: str
    message: str
    timestamp: float = field(default_factory=time.time)
    sent: bool = False


class AlertManager:
    """Centralized alert management with rate limiting."""
    
    __slots__ = ['min_interval', 'max_per_hour', 'alert_queue', 
                 'last_alert_time', 'alerts_this_hour', 'hour_start',
                 '_lock', 'telegram_enabled', 'overlay_enabled']
    
    def __init__(self, min_interval_seconds: int = 60, max_per_hour: int = 10):
        self.min_interval = min_interval_seconds
        self.max_per_hour = max_per_hour
        self.alert_queue: List[Alert] = []
        self.last_alert_time: float = 0
        self.alerts_this_hour: int = 0
        self.hour_start: float = time.time()
        self._lock = threading.Lock()
        self.telegram_enabled: bool = False
        self.overlay_enabled: bool = False
        
        # Setup logging
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s'
        )
    
    def send(self, level: AlertLevel, title: str, message: str) -> bool:
        """
        Send an alert with rate limiting.
        
        Args:
            level: Alert severity level
            title: Alert title
            message: Alert message
            
        Returns:
            True if alert was sent/queued, False if rejected
        """
        with self._lock:
            alert = Alert(level=level, title=title, message=message)
            
            # Reset hourly counter if hour has passed
            if time.time() - self.hour_start >= 3600:
                self.hour_start = time.time()
                self.alerts_this_hour = 0
            
            # Check rate limits
            time_since_last = time.time() - self.last_alert_time
            
            if level == AlertLevel.INFO:
                # INFO: Log only, no rate limit
                self._log_alert(alert)
                return True
            
            if self.alerts_this_hour >= self.max_per_hour:
                # Queue for later if over hourly limit
                if len(self.alert_queue) < 50:
                    self.alert_queue.append(alert)
                return False
            
            if time_since_last < self.min_interval and level != AlertLevel.CRITICAL:
                # Queue if under interval (except CRITICAL)
                if len(self.alert_queue) < 50:
                    self.alert_queue.append(alert)
                return False
            
            # Send the alert
            self._send_alert(alert)
            self.last_alert_time = time.time()
            self.alerts_this_hour += 1
            alert.sent = True
            
            return True
    
    def _send_alert(self, alert: Alert) -> None:
        """Internal alert dispatch."""
        self._log_alert(alert)
        
        if alert.level in (AlertLevel.WARNING, AlertLevel.CRITICAL):
            self._send_telegram(alert)
        
        if alert.level == AlertLevel.CRITICAL:
            self._flash_overlay(alert)
            # CRITICAL: schedule 2 more alerts 5 min apart
            # (simplified - would need scheduler in production)
    
    def _log_alert(self, alert: Alert) -> None:
        """Log alert to file."""
        log_msg = f"[{alert.level.value.upper()}] {alert.title}: {alert.message}"
        
        if alert.level == AlertLevel.INFO:
            logging.info(log_msg)
        elif alert.level == AlertLevel.WARNING:
            logging.warning(log_msg)
        else:
            logging.critical(log_msg)
    
    def _send_telegram(self, alert: Alert) -> bool:
        """Send alert via Telegram bot."""
        try:
            import os
            import requests
            
            token = os.environ.get('TELEGRAM_BOT_TOKEN')
            chat_id = os.environ.get('TELEGRAM_CHAT_ID')
            
            if not token or not chat_id:
                logging.warning("Telegram not configured")
                return False
            
            emoji = "⚠️" if alert.level == AlertLevel.WARNING else "🚨"
            text = f"{emoji} *{alert.title}*\n{alert.message}"
            
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = {
                'chat_id': chat_id,
                'text': text,
                'parse_mode': 'Markdown'
            }
            
            response = requests.post(url, json=payload, timeout=10)
            return response.status_code == 200
            
        except Exception as e:
            logging.error(f"Telegram send failed: {e}")
            return False
    
    def _flash_overlay(self, alert: Alert) -> None:
        """Flash message on overlay display."""
        # Placeholder for overlay flash
        # Would integrate with trading_agent overlay
        logging.critical(f"OVERLAY FLASH: {alert.title}")
    
    def send_telegram(self, text: str) -> bool:
        """Send raw text via Telegram."""
        try:
            import os
            import requests
            
            token = os.environ.get('TELEGRAM_BOT_TOKEN')
            chat_id = os.environ.get('TELEGRAM_CHAT_ID')
            
            if not token or not chat_id:
                return False
            
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = {'chat_id': chat_id, 'text': text}
            
            response = requests.post(url, json=payload, timeout=10)
            return response.status_code == 200
            
        except Exception as e:
            logging.error(f"Telegram send failed: {e}")
            return False
    
    def flash_overlay(self, message: str, color: str = "red") -> None:
        """Flash message on overlay."""
        logging.info(f"OVERLAY [{color}]: {message}")
    
    def get_alert_stats(self) -> Dict:
        """Get alert statistics."""
        with self._lock:
            return {
                "queued": len(self.alert_queue),
                "this_hour": self.alerts_this_hour,
                "max_per_hour": self.max_per_hour,
                "last_alert": self.last_alert_time,
                "hour_start": self.hour_start
            }
    
    def process_queue(self) -> int:
        """Process queued alerts. Returns count processed."""
        with self._lock:
            processed = 0
            
            while (self.alert_queue and 
                   self.alerts_this_hour < self.max_per_hour and
                   time.time() - self.last_alert_time >= self.min_interval):
                
                alert = self.alert_queue.pop(0)
                self._send_alert(alert)
                processed += 1
            
            return processed


# Global alert manager instance
_alert_manager: Optional[AlertManager] = None


def get_manager() -> AlertManager:
    """Get or create global alert manager."""
    global _alert_manager
    if _alert_manager is None:
        _alert_manager = AlertManager()
    return _alert_manager


def send(level: AlertLevel, title: str, message: str) -> bool:
    """Send an alert."""
    return get_manager().send(level, title, message)


def send_telegram(text: str) -> bool:
    """Send Telegram message."""
    return get_manager().send_telegram(text)


def flash_overlay(message: str, color: str = "red") -> None:
    """Flash overlay message."""
    get_manager().flash_overlay(message, color)


def get_alert_stats() -> Dict:
    """Get alert statistics."""
    return get_manager().get_alert_stats()
