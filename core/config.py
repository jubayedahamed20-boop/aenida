"""
AENIDA Configuration Manager
Handles loading, validation, and access to configuration settings.
"""

import json
import os
from typing import Any, Dict, List, Optional


class Config:
    """Configuration manager with dot-notation access."""
    
    _instance = None
    _data: Dict[str, Any] = {}
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def load(self, path: str = "config.json") -> Dict[str, Any]:
        """Load configuration from JSON file."""
        if not os.path.exists(path):
            raise FileNotFoundError(f"Config file not found: {path}")
        
        with open(path, 'r') as f:
            self._data = json.load(f)
        
        return self._data
    
    def get(self, key_path: str, default: Any = None) -> Any:
        """Get config value using dot notation (e.g., 'worker.timeout_seconds')."""
        keys = key_path.split('.')
        value = self._data
        
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default
        
        return value
    
    def set(self, key_path: str, value: Any) -> None:
        """Set config value using dot notation."""
        keys = key_path.split('.')
        target = self._data
        
        for key in keys[:-1]:
            if key not in target:
                target[key] = {}
            target = target[key]
        
        target[keys[-1]] = value
    
    def reload(self, path: str = "config.json") -> Dict[str, Any]:
        """Reload configuration from file."""
        return self.load(path)
    
    def save(self, path: str = "config.json") -> None:
        """Save current configuration to file."""
        with open(path, 'w') as f:
            json.dump(self._data, f, indent=2)
    
    def validate(self) -> List[str]:
        """Validate configuration and return list of issues."""
        issues = []
        
        required_paths = [
            "api_keys",
            "worker",
            "trading",
            "market_data",
            "memory",
            "security",
            "paths"
        ]
        
        for path in required_paths:
            if self.get(path) is None:
                issues.append(f"Missing required config section: {path}")
        
        return issues
    
    def get_all(self) -> Dict[str, Any]:
        """Get entire configuration dictionary."""
        return self._data.copy()


# Global config instance
_config = Config()


def load_config(path: str = "config.json") -> Dict[str, Any]:
    """Load configuration from file."""
    return _config.load(path)


def get(key_path: str, default: Any = None) -> Any:
    """Get configuration value."""
    return _config.get(key_path, default)


def set_value(key_path: str, value: Any) -> None:
    """Set configuration value."""
    _config.set(key_path, value)


def reload(path: str = "config.json") -> Dict[str, Any]:
    """Reload configuration."""
    return _config.reload(path)


def save(path: str = "config.json") -> None:
    """Save configuration."""
    _config.save(path)


def validate() -> List[str]:
    """Validate configuration."""
    return _config.validate()


# Auto-load on import — use absolute path so it works from any CWD
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
if os.path.exists(_CONFIG_PATH):
    _config.load(_CONFIG_PATH)
