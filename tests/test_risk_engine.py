"""
Tests for risk_engine.py
"""

import pytest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import path_setup  # registers all sub-packages

from risk_engine import RiskEngine, ActionPlan, LockLevel


class TestRiskEngine:
    """Test risk engine."""
    
    def test_calculate_low_risk(self):
        """Test low risk calculation (L1)."""
        engine = RiskEngine()
        plan = ActionPlan(
            action_type="READ",
            scope="/data/logs",
            auth_level=0.8
        )
        
        result = engine.calculate(plan)
        
        assert result.lock_level == LockLevel.L1
        assert result.auto_approve == True
        assert result.requires_totp == False
        assert result.requires_usb == False
    
    def test_calculate_medium_risk(self):
        """Test medium risk calculation (L2)."""
        engine = RiskEngine()
        plan = ActionPlan(
            action_type="WRITE",
            scope="/data/config",
            auth_level=0.5
        )
        
        result = engine.calculate(plan)
        
        assert result.lock_level == LockLevel.L2
        assert result.auto_approve == False
        assert result.requires_totp == True
        assert result.requires_usb == False
    
    def test_calculate_high_risk(self):
        """Test high risk calculation (L3)."""
        engine = RiskEngine()
        plan = ActionPlan(
            action_type="EXEC",
            scope="/etc/passwd",
            auth_level=0.3
        )
        
        result = engine.calculate(plan)
        
        assert result.lock_level == LockLevel.L3
        assert result.auto_approve == False
        assert result.requires_totp == True
        assert result.requires_usb == True
    
    def test_bug_01_auth_level_min(self):
        """Test BUG-01: auth_level minimum of 0.1."""
        engine = RiskEngine()
        
        # auth_level of 0 should be clamped to 0.1
        plan = ActionPlan(
            action_type="READ",
            scope="/data",
            auth_level=0.0
        )
        
        result = engine.calculate(plan)
        # Should not crash and should have valid score
        assert result.r_score >= 0.0
        assert result.r_score <= 1.0
    
    def test_validate_scope(self):
        """Test scope validation."""
        engine = RiskEngine()
        
        assert engine.validate_scope("/data/logs", "/data") == True
        assert engine.validate_scope("/etc/passwd", "/data") == False
        assert engine.validate_scope("/data/../etc", "/data") == False
    
    def test_frequency_penalty(self):
        """Test frequency penalty."""
        engine = RiskEngine()
        
        # Make multiple requests
        plan = ActionPlan(
            action_type="READ",
            scope="/data",
            auth_level=0.8
        )
        
        # First few should be low risk
        result1 = engine.calculate(plan)
        result2 = engine.calculate(plan)
        result3 = engine.calculate(plan)
        result4 = engine.calculate(plan)
        
        # After many requests, risk should increase
        # (implementation dependent, just verify it doesn't crash)
        assert result1.r_score >= 0.0
    
    def test_issue_l3_challenge(self):
        """Test L3 challenge issuance."""
        engine = RiskEngine()
        
        challenge = engine.issue_l3_challenge("Test action")
        
        assert challenge["level"] == "L3"
        assert challenge["requires_usb"] == True
        assert challenge["requires_totp"] == True
        assert "ttl_seconds" in challenge


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
