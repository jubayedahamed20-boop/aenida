"""
Tests for config.py
"""

import pytest
import os
import json
import tempfile

# Ensure we can import from parent directory
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import path_setup  # registers all sub-packages

import config


class TestConfig:
    """Test configuration manager."""
    
    def test_load_config(self):
        """Test loading configuration."""
        cfg = config.Config()
        
        # Create temp config file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump({"test": {"key": "value"}}, f)
            temp_path = f.name
        
        try:
            result = cfg.load(temp_path)
            assert result["test"]["key"] == "value"
        finally:
            os.unlink(temp_path)
    
    def test_get_nested(self):
        """Test getting nested values."""
        cfg = config.Config()
        cfg._data = {"a": {"b": {"c": "deep_value"}}}
        
        assert cfg.get("a.b.c") == "deep_value"
        assert cfg.get("a.b.missing") is None
        assert cfg.get("a.b.missing", "default") == "default"
    
    def test_set_nested(self):
        """Test setting nested values."""
        cfg = config.Config()
        cfg._data = {}
        
        cfg.set("x.y.z", "new_value")
        assert cfg.get("x.y.z") == "new_value"
    
    def test_validate(self):
        """Test configuration validation."""
        cfg = config.Config()
        cfg._data = {
            "api_keys": {},
            "worker": {},
            "trading": {},
            "market_data": {},
            "memory": {},
            "security": {},
            "paths": {}
        }
        
        issues = cfg.validate()
        assert len(issues) == 0
    
    def test_validate_missing_sections(self):
        """Test validation with missing sections."""
        cfg = config.Config()
        cfg._data = {}
        
        issues = cfg.validate()
        assert len(issues) > 0
    
    def test_get_all(self):
        """Test getting all config."""
        cfg = config.Config()
        cfg._data = {"key": "value"}
        
        all_cfg = cfg.get_all()
        assert all_cfg["key"] == "value"
        # Should be a copy
        all_cfg["key"] = "modified"
        assert cfg._data["key"] == "value"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
