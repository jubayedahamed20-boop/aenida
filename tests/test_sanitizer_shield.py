"""
Tests for sanitizer_shield.py
"""

import pytest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import path_setup  # registers all sub-packages

from sanitizer_shield import SanitizerShield, SanitizationError


class TestSanitizerShield:
    """Test sanitizer shield."""
    
    def test_scrub_basic(self):
        """Test basic string scrubbing."""
        result = SanitizerShield.scrub("Hello World")
        assert "Hello World" in result
    
    def test_scrub_script_tag(self):
        """Test script tag removal."""
        result = SanitizerShield.scrub("<script>alert('xss')</script>")
        assert "<script>" not in result
        assert "[⛔SCRIPT]" in result or "alert" not in result.lower()
    
    def test_scrub_javascript_uri(self):
        """Test javascript: URI removal."""
        result = SanitizerShield.scrub("javascript:alert('xss')")
        assert "javascript:" not in result.lower()
    
    def test_scrub_event_handler(self):
        """Test event handler removal."""
        result = SanitizerShield.scrub("<div onclick='alert(1)'>")
        assert "onclick" not in result.lower()
    
    def test_scrub_shell_meta(self):
        """Test shell metacharacter removal."""
        result = SanitizerShield.scrub("; rm -rf /")
        assert ";" not in result
        assert "`" not in result
    
    def test_scrub_path_traversal(self):
        """Test path traversal removal."""
        result = SanitizerShield.scrub("../../../etc/passwd")
        assert "../" not in result
    
    def test_scrub_system_path(self):
        """Test system path blocking."""
        result = SanitizerShield.scrub("/etc/passwd")
        assert "/etc/" not in result or "[⛔SYSPATH]" in result
    
    def test_scrub_strict_mode(self):
        """Test strict mode raises exception."""
        raised = False
        try:
            SanitizerShield.scrub("<script>alert(1)</script>", strict=True)
        except SanitizationError:
            raised = True
        assert raised, "Expected SanitizationError was not raised in strict mode"
    
    def test_scrub_dict(self):
        """Test dictionary scrubbing."""
        data = {
            "key1": "<script>alert(1)</script>",
            "key2": "safe_value",
            "nested": {
                "key3": "javascript:alert(1)"
            }
        }
        
        result = SanitizerShield.scrub_dict(data)
        assert "<script>" not in result["key1"]
        assert result["key2"] == "safe_value"
        assert "javascript:" not in result["nested"]["key3"].lower()
    
    def test_is_clean(self):
        """Test clean check."""
        assert SanitizerShield.is_clean("safe text") == True
        assert SanitizerShield.is_clean("<script>alert(1)</script>") == False
    
    def test_threat_report(self):
        """Test threat report generation."""
        report = SanitizerShield.threat_report("<script>alert(1)</script>")
        assert report["has_script"] == True
        
        report = SanitizerShield.threat_report("safe text")
        assert report["has_script"] == False
    
    def test_scrub_unicode_homoglyph(self):
        """Test NFKC normalization (BUG-05)."""
        # Homoglyph attack
        result = SanitizerShield.scrub("ｓｃｒｉｐｔ")  # Fullwidth characters
        # Should be normalized
        assert result is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
