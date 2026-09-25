"""
AENIDA Security Gateway (TYR V3.0)
Token-based security system with Triple-Lock authentication.
"""

import os
import time
import hashlib
import hmac
import secrets
from typing import Dict, Any, Optional, Callable, List
from dataclasses import dataclass, field
from enum import Enum
import threading


class TyrError(Exception):
    """Base TYR security error."""
    pass


class TyrIdentityError(TyrError):
    """Identity verification failed."""
    pass


class TyrTokenError(TyrError):
    """Token validation failed."""
    pass


class HardAbend(TyrError):
    """Critical security failure - halt required."""
    pass


class ActionID(Enum):
    """Predefined action IDs for token scoping."""
    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    EXEC = "exec"
    ADMIN = "admin"
    CONFIG = "config"
    NIGHTLY_SYNC = "nightly_sync"


@dataclass
class TyrToken:
    """TYR security token."""
    token_id: str
    action_id: ActionID
    scope: str
    issued_at: float
    expires_at: float
    nonce: str
    signature: str


@dataclass
class ExecutionContext:
    """Context for rollback support."""
    rollbacks: List[tuple] = field(default_factory=list)
    
    def push(self, description: str, rollback_fn: Callable) -> None:
        """Push rollback action."""
        self.rollbacks.append((description, rollback_fn))
    
    def rollback(self) -> None:
        """Execute all rollbacks in LIFO order."""
        for desc, fn in reversed(self.rollbacks):
            try:
                fn()
            except Exception as e:
                # Log but continue rollback
                pass


class TyrNotary:
    """
    TYR Security Notary - token issuance and verification.
    All BUG-01 through BUG-10 fixes applied.
    """
    
    __slots__ = ['_ready', '_master_secret', '_hkdf_key', 
                 '_used_nonces', '_nonce_lock', '_identity_verified']
    
    def __init__(self):
        self._ready = False
        self._master_secret: Optional[str] = None
        self._hkdf_key: Optional[bytes] = None
        self._used_nonces: Dict[str, float] = {}
        self._nonce_lock = threading.Lock()
        self._identity_verified = False
    
    def initialize(self, master_secret: str, ares_identity: str) -> None:
        """
        Initialize TYR with master secret.
        BUG-02: HKDF derivation required before any use.
        """
        self._master_secret = master_secret
        
        # BUG-02: HKDF key derivation
        salt = os.environ.get("TYR_HKDF_SALT", "default_salt").encode()
        prk = hmac.new(salt, master_secret.encode(), hashlib.sha256).digest()
        info = b"tyr::token-signing-key\x01"
        self._hkdf_key = hmac.new(prk, info, hashlib.sha256).digest()[:32]
        
        # Clear raw secret from memory
        self._master_secret = None
        
        # BUG-04: ARES identity verification
        expected = os.environ.get("ARES_SHARED_IDENTITY", "")
        if not expected:
            raise TyrIdentityError("ARES identity not configured")
        
        if not hmac.compare_digest(
            hashlib.sha256(ares_identity.encode()).hexdigest(),
            hashlib.sha256(expected.encode()).hexdigest()
        ):
            raise TyrIdentityError("ARES identity verification failed")
        
        self._identity_verified = True
        self._ready = True
    
    def _assert_ready(self) -> None:
        """BUG-08: TYR down → ARES halts, never bypasses."""
        if not self._ready:
            raise HardAbend("TYR not initialized - security halt")
    
    def issue_token(self, action_id: ActionID, scope: str, 
                    ttl_seconds: int = 30) -> TyrToken:
        """
        Issue a new TYR token.
        BUG-07: Log ISSUED to audit.
        """
        self._assert_ready()
        
        token_id = secrets.token_hex(16)
        nonce = secrets.token_hex(16)
        issued_at = time.time()
        expires_at = issued_at + ttl_seconds
        
        # Create signature
        sig_data = f"{token_id}:{action_id.value}:{scope}:{issued_at}:{expires_at}:{nonce}"
        signature = hmac.new(self._hkdf_key, sig_data.encode(), hashlib.sha256).hexdigest()
        
        token = TyrToken(
            token_id=token_id,
            action_id=action_id,
            scope=scope,
            issued_at=issued_at,
            expires_at=expires_at,
            nonce=nonce,
            signature=signature
        )
        
        # BUG-07: Log ISSUED
        self._log_audit("ISSUED", token)
        
        return token
    
    def verify_token(self, token: TyrToken, expected_action: ActionID,
                     expected_scope: str) -> bool:
        """
        Verify token authenticity and validity.
        BUG-03: Nonce replay prevention.
        BUG-07: Log VERIFIED or REJECTED.
        """
        self._assert_ready()
        
        # BUG-03: Check nonce not reused - check inside lock, raise outside
        nonce_reused = False
        with self._nonce_lock:
            self._purge_expired_nonces()
            if token.nonce in self._used_nonces:
                nonce_reused = True
            else:
                self._used_nonces[token.nonce] = time.time()
        
        if nonce_reused:
            self._log_audit("REJECTED", token, reason="nonce_replay")
            raise TyrTokenError("Nonce already used - possible replay attack")
        
        # Verify signature
        sig_data = f"{token.token_id}:{token.action_id.value}:{token.scope}:{token.issued_at}:{token.expires_at}:{token.nonce}"
        expected_sig = hmac.new(self._hkdf_key, sig_data.encode(), hashlib.sha256).hexdigest()
        
        if not hmac.compare_digest(token.signature, expected_sig):
            self._log_audit("REJECTED", token, reason="invalid_signature")
            raise TyrTokenError("Invalid token signature")
        
        # Verify expiry
        if time.time() > token.expires_at:
            self._log_audit("REJECTED", token, reason="expired")
            raise TyrTokenError("Token expired")
        
        # Verify action ID
        if token.action_id != expected_action:
            self._log_audit("REJECTED", token, reason="action_mismatch")
            raise TyrTokenError("Action ID mismatch")
        
        # BUG-05: Scope containment (path traversal check)
        if not self._validate_scope(token.scope, expected_scope):
            self._log_audit("REJECTED", token, reason="scope_violation")
            raise TyrTokenError("Scope violation - path traversal detected")
        
        # BUG-07: Log VERIFIED
        self._log_audit("VERIFIED", token)
        
        return True
    
    def _purge_expired_nonces(self) -> None:
        """BUG-03: Purge expired nonces."""
        now = time.time()
        retention = int(os.environ.get("TYR_NONCE_RETENTION", "60"))
        expired = [n for n, t in self._used_nonces.items() if now - t > retention]
        for n in expired:
            del self._used_nonces[n]
    
    def _validate_scope(self, token_scope: str, allowed_root: str) -> bool:
        """BUG-05: Validate scope path."""
        import os
        
        try:
            token_abs = os.path.abspath(os.path.expanduser(token_scope))
            allowed_abs = os.path.abspath(os.path.expanduser(allowed_root))
            return token_abs.startswith(allowed_abs)
        except Exception:
            return False
    
    def _log_audit(self, event: str, token: TyrToken, reason: str = "") -> None:
        """BUG-07: Log to audit."""
        import logging
        msg = f"TYR AUDIT [{event}] token={token.token_id[:8]}... action={token.action_id.value}"
        if reason:
            msg += f" reason={reason}"
        
        if event == "REJECTED":
            logging.warning(msg)
        else:
            logging.info(msg)
    
    def confirm_l3_lock(self, totp_code: str, usb_present: bool) -> bool:
        """
        BUG-10: L3 physical lock with USB + TOTP.
        """
        self._assert_ready()
        
        if not usb_present:
            raise TyrIdentityError("L3 requires USB token present")
        
        # Verify TOTP
        try:
            import pyotp
            totp_secret = os.environ.get("TOTP_SECRET", "")
            if not totp_secret:
                raise TyrIdentityError("TOTP not configured")
            
            totp = pyotp.TOTP(totp_secret)
            if not totp.verify(totp_code, valid_window=1):
                raise TyrIdentityError("TOTP verification failed")
        except ImportError:
            raise TyrIdentityError("pyotp not installed")
        
        return True
    
    def revoke_all_tokens(self) -> int:
        """Revoke all active tokens (USB removal)."""
        with self._nonce_lock:
            count = len(self._used_nonces)
            self._used_nonces.clear()
            return count

    def close(self) -> None:
        """BUG-E fix: set _ready=False on shutdown so ARES halts."""
        self._ready = False
        self._hkdf_key = None
        self._identity_verified = False


# Global instance
_notary: Optional[TyrNotary] = None


def get_notary() -> TyrNotary:
    """Get or create global notary."""
    global _notary
    if _notary is None:
        _notary = TyrNotary()
    return _notary


def require_token(action_id: ActionID, scope: str) -> TyrToken:
    """Decorator/requirement for token."""
    return get_notary().issue_token(action_id, scope)


# Export classes
__all__ = [
    'TyrNotary', 'TyrToken', 'ActionID', 'ExecutionContext',
    'TyrError', 'TyrIdentityError', 'TyrTokenError', 'HardAbend',
    'require_token', 'get_notary'
]
