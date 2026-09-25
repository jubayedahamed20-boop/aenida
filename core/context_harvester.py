"""
AENIDA Context Harvester
System snapshot for cognition_router offloads.
"""

import time
from typing import Dict, Any, Optional

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False


class ContextHarvester:
    """
    Harvest system context for Worker offloads.
    Max 100ms collection time, max 4096 bytes output.
    """
    
    __slots__ = ['max_bytes', 'max_time_ms']
    
    def __init__(self, max_bytes: int = 4096, max_time_ms: int = 100):
        self.max_bytes = max_bytes
        self.max_time_ms = max_time_ms
    
    def harvest(self) -> Dict[str, Any]:
        """
        Full context harvest.
        
        Returns:
            Dict with system snapshot
        """
        if not PSUTIL_AVAILABLE:
            return self.harvest_minimal()
        
        try:
            # Get memory info
            mem = psutil.virtual_memory()
            ram_free_mb = mem.available / (1024 * 1024)
            
            # Get CPU
            cpu_percent = psutil.cpu_percent(interval=0.1)
            
            # Get battery if available
            battery_pct = None
            try:
                battery = psutil.sensors_battery()
                if battery:
                    battery_pct = battery.percent
            except Exception:
                pass
            
            # Get active window (placeholder - platform specific)
            active_window = self._get_active_window()
            
            # Get local paths
            local_paths = self._get_local_paths()
            
            context = {
                "active_window": active_window,
                "battery_pct": battery_pct,
                "ram_free_mb": round(ram_free_mb, 1),
                "cpu_percent": round(cpu_percent, 1),
                "local_paths": local_paths,
                "timestamp": time.time()
            }
            
            # Trim to max bytes
            return self._trim_context(context)
            
        except Exception as e:
            # Safe defaults on any failure
            return self.harvest_minimal()
    
    def harvest_minimal(self) -> Dict[str, Any]:
        """
        Minimal context harvest (fastest).
        
        Returns:
            Minimal dict with essential info
        """
        ram_free_mb = 0
        
        if PSUTIL_AVAILABLE:
            try:
                mem = psutil.virtual_memory()
                ram_free_mb = mem.available / (1024 * 1024)
            except Exception:
                pass
        
        return {
            "ram_free_mb": round(ram_free_mb, 1),
            "timestamp": time.time()
        }
    
    def _get_active_window(self) -> Optional[str]:
        """Get active window title (platform specific)."""
        try:
            # Try Linux/X11
            import subprocess
            result = subprocess.run(
                ['xdotool', 'getactivewindow', 'getwindowname'],
                capture_output=True, text=True, timeout=1
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        
        return None
    
    def _get_local_paths(self) -> list:
        """Get relevant local paths."""
        import os
        
        paths = []
        
        # Add current working directory
        try:
            paths.append(os.getcwd())
        except Exception:
            pass
        
        # Add home directory
        try:
            paths.append(os.path.expanduser("~"))
        except Exception:
            pass
        
        return paths[:3]  # Limit to 3 paths
    
    def _trim_context(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Trim context to max bytes."""
        import json
        
        # Convert to JSON to check size
        json_str = json.dumps(context)
        
        if len(json_str) <= self.max_bytes:
            return context
        
        # Trim progressively
        while len(json_str) > self.max_bytes:
            # Remove optional fields first
            if "local_paths" in context:
                del context["local_paths"]
            elif "active_window" in context:
                del context["active_window"]
            elif "battery_pct" in context:
                del context["battery_pct"]
            else:
                break
            
            json_str = json.dumps(context)
        
        return context
    
    def get_e_score_context(self) -> Dict[str, float]:
        """
        Get context for E-score calculation.
        
        Returns:
            Dict with E-score relevant metrics
        """
        if not PSUTIL_AVAILABLE:
            return {"ram_free_mb": 0, "cpu_percent": 0}
        
        try:
            mem = psutil.virtual_memory()
            ram_free_mb = mem.available / (1024 * 1024)
            cpu_percent = psutil.cpu_percent(interval=0.1)
            
            return {
                "ram_free_mb": round(ram_free_mb, 1),
                "cpu_percent": round(cpu_percent, 1)
            }
        except Exception:
            return {"ram_free_mb": 0, "cpu_percent": 0}


# Global instance
_harvester: Optional[ContextHarvester] = None


def get_harvester() -> ContextHarvester:
    """Get or create global harvester."""
    global _harvester
    if _harvester is None:
        _harvester = ContextHarvester()
    return _harvester


def harvest() -> Dict[str, Any]:
    """Harvest full context."""
    return get_harvester().harvest()


def harvest_minimal() -> Dict[str, Any]:
    """Harvest minimal context."""
    return get_harvester().harvest_minimal()


def get_e_score_context() -> Dict[str, float]:
    """Get E-score context."""
    return get_harvester().get_e_score_context()
