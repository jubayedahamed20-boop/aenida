"""
AENIDA Integrity Checker
SHA-256 checksums + UUID5 deterministic idempotency.
"""

import hashlib
import json
import uuid
import zlib
from typing import Dict, Any, Optional


class IntegrityChecker:
    """Payload integrity and idempotency verification."""
    
    __slots__ = ['_seen_uuids']
    
    def __init__(self):
        self._seen_uuids: set = set()
    
    def compute_payload_hash(self, payload: bytes) -> str:
        """
        Compute SHA-256 hash of payload.
        
        Args:
            payload: Raw bytes to hash
            
        Returns:
            Hex digest of SHA-256 hash
        """
        return hashlib.sha256(payload).hexdigest()
    
    def verify_payload(self, payload: bytes, expected_hash: str) -> bool:
        """
        Verify payload against expected hash.
        
        Args:
            payload: Raw bytes to verify
            expected_hash: Expected SHA-256 hex digest
            
        Returns:
            True if hash matches
        """
        computed = self.compute_payload_hash(payload)
        return computed == expected_hash
    
    def generate_step_uuid(self, task_id: str, step_index: int) -> str:
        """
        Generate deterministic UUID for task step.
        Uses UUID5 with DNS namespace for consistency.
        
        Args:
            task_id: Parent task identifier
            step_index: Step number within task
            
        Returns:
            UUID string
        """
        namespace = uuid.NAMESPACE_DNS
        name = f"{task_id}::step::{step_index}"
        return str(uuid.uuid5(namespace, name))
    
    def check_idempotency(self, step_uuid: str) -> bool:
        """
        Check if step UUID has been seen before.
        
        Args:
            step_uuid: UUID to check
            
        Returns:
            True if already processed (skip), False if new
        """
        if step_uuid in self._seen_uuids:
            return True
        self._seen_uuids.add(step_uuid)
        return False
    
    def compress_payload(self, data: Dict[str, Any]) -> bytes:
        """
        Compress payload using zlib.
        
        Args:
            data: Dictionary to compress
            
        Returns:
            Compressed bytes
        """
        json_bytes = json.dumps(data, separators=(',', ':')).encode('utf-8')
        return zlib.compress(json_bytes, level=6)
    
    def decompress_payload(self, data: bytes) -> Dict[str, Any]:
        """
        Decompress payload using zlib.
        
        Args:
            data: Compressed bytes
            
        Returns:
            Decompressed dictionary
        """
        json_bytes = zlib.decompress(data)
        return json.loads(json_bytes.decode('utf-8'))
    
    def compute_dict_hash(self, data: Dict[str, Any]) -> str:
        """
        Compute hash of dictionary (canonical JSON).
        
        Args:
            data: Dictionary to hash
            
        Returns:
            SHA-256 hex digest
        """
        # Sort keys for canonical representation
        canonical = json.dumps(data, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(canonical.encode('utf-8')).hexdigest()
    
    def create_signed_payload(self, data: Dict[str, Any], secret: str) -> Dict[str, Any]:
        """
        Create HMAC-signed payload.
        
        Args:
            data: Data to sign
            secret: HMAC secret key
            
        Returns:
            Payload with signature
        """
        import hmac
        
        payload_str = json.dumps(data, sort_keys=True, separators=(',', ':'))
        signature = hmac.new(
            secret.encode('utf-8'),
            payload_str.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        
        return {
            "data": data,
            "signature": signature,
            "algorithm": "HMAC-SHA256"
        }
    
    def verify_signed_payload(self, payload: Dict[str, Any], secret: str) -> bool:
        """
        Verify HMAC-signed payload.
        
        Args:
            payload: Signed payload
            secret: HMAC secret key
            
        Returns:
            True if signature valid
        """
        import hmac
        
        data = payload.get("data", {})
        signature = payload.get("signature", "")
        
        payload_str = json.dumps(data, sort_keys=True, separators=(',', ':'))
        expected = hmac.new(
            secret.encode('utf-8'),
            payload_str.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        
        return hmac.compare_digest(signature, expected)


# Global instance
_checker: Optional[IntegrityChecker] = None


def get_checker() -> IntegrityChecker:
    """Get or create global integrity checker."""
    global _checker
    if _checker is None:
        _checker = IntegrityChecker()
    return _checker


def compute_payload_hash(payload: bytes) -> str:
    """Compute SHA-256 hash."""
    return get_checker().compute_payload_hash(payload)


def verify_payload(payload: bytes, expected: str) -> bool:
    """Verify payload hash."""
    return get_checker().verify_payload(payload, expected)


def generate_step_uuid(task_id: str, step_index: int) -> str:
    """Generate deterministic step UUID."""
    return get_checker().generate_step_uuid(task_id, step_index)


def check_idempotency(step_uuid: str) -> bool:
    """Check if step already processed."""
    return get_checker().check_idempotency(step_uuid)


def compress_payload(data: Dict[str, Any]) -> bytes:
    """Compress payload."""
    return get_checker().compress_payload(data)


def decompress_payload(data: bytes) -> Dict[str, Any]:
    """Decompress payload."""
    return get_checker().decompress_payload(data)
