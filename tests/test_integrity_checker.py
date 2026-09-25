"""
Tests for integrity_checker.py
"""

import pytest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import path_setup  # registers all sub-packages

from integrity_checker import IntegrityChecker


class TestIntegrityChecker:
    """Test integrity checker."""
    
    def test_compute_payload_hash(self):
        """Test hash computation."""
        checker = IntegrityChecker()
        payload = b"test data"
        
        hash1 = checker.compute_payload_hash(payload)
        hash2 = checker.compute_payload_hash(payload)
        
        assert len(hash1) == 64  # SHA-256 hex is 64 chars
        assert hash1 == hash2  # Deterministic
    
    def test_verify_payload(self):
        """Test payload verification."""
        checker = IntegrityChecker()
        payload = b"test data"
        
        hash_val = checker.compute_payload_hash(payload)
        assert checker.verify_payload(payload, hash_val) == True
        assert checker.verify_payload(payload, "wrong_hash") == False
    
    def test_generate_step_uuid(self):
        """Test UUID generation."""
        checker = IntegrityChecker()
        
        uuid1 = checker.generate_step_uuid("task1", 0)
        uuid2 = checker.generate_step_uuid("task1", 0)
        uuid3 = checker.generate_step_uuid("task1", 1)
        
        assert uuid1 == uuid2  # Deterministic
        assert uuid1 != uuid3  # Different step
    
    def test_check_idempotency(self):
        """Test idempotency check."""
        checker = IntegrityChecker()
        
        uuid_val = checker.generate_step_uuid("task1", 0)
        
        assert checker.check_idempotency(uuid_val) == False  # First time
        assert checker.check_idempotency(uuid_val) == True   # Second time
    
    def test_compress_decompress(self):
        """Test compression roundtrip."""
        checker = IntegrityChecker()
        data = {"key": "value", "number": 123}
        
        compressed = checker.compress_payload(data)
        decompressed = checker.decompress_payload(compressed)
        
        assert decompressed == data
    
    def test_compute_dict_hash(self):
        """Test dictionary hash."""
        checker = IntegrityChecker()
        
        data1 = {"b": 2, "a": 1}
        data2 = {"a": 1, "b": 2}
        data3 = {"a": 1, "b": 3}
        
        hash1 = checker.compute_dict_hash(data1)
        hash2 = checker.compute_dict_hash(data2)
        hash3 = checker.compute_dict_hash(data3)
        
        assert hash1 == hash2  # Same data, different order
        assert hash1 != hash3  # Different data
    
    def test_signed_payload(self):
        """Test HMAC signing."""
        checker = IntegrityChecker()
        data = {"action": "test"}
        secret = "my_secret_key"
        
        signed = checker.create_signed_payload(data, secret)
        assert "signature" in signed
        assert signed["algorithm"] == "HMAC-SHA256"
        
        # Verify
        assert checker.verify_signed_payload(signed, secret) == True
        assert checker.verify_signed_payload(signed, "wrong_secret") == False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
