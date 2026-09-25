"""
AENIDA Risk Engine
Risk scoring and authorization levels for security.
"""

import time
from typing import Dict, Any, Optional, Callable
from dataclasses import dataclass
from enum import Enum


class LockLevel(Enum):
    """Authorization lock levels."""
    L1 = "l1"  # Auto-approve
    L2 = "l2"  # TOTP required
    L3 = "l3"  # USB + TOTP required


@dataclass
class RiskResult:
    """Risk assessment result."""
    r_score: float
    lock_level: LockLevel
    auto_approve: bool
    requires_totp: bool
    requires_usb: bool
    reason: str


@dataclass
class ActionPlan:
    """Action to be evaluated."""
    action_type: str
    scope: str
    auth_level: float
    frequency_count: int = 0


class RiskEngine:
    """
    Risk scoring engine for security decisions.
    BUG-01 fix: auth_level = max(0.1, raw_auth_level)
    """
    
    __slots__ = ['action_weights', 'frequency_window', 'frequency_penalty',
                 '_action_history', '_l3_challenge_callback']
    
    def __init__(self):
        # Action type weights
        self.action_weights = {
            "READ": 0.1,
            "WRITE": 0.3,
            "DELETE": 0.5,
            "EXEC": 1.0,
            "ADMIN": 0.8,
            "CONFIG": 0.6
        }
        
        # Frequency tracking
        self.frequency_window = 300  # 5 minutes
        self.frequency_penalty = 0.05
        self._action_history: Dict[str, list] = {}
    
    def calculate(self, plan: ActionPlan) -> RiskResult:
        """
        Calculate risk score for an action plan.
        
        Args:
            plan: Action plan to evaluate
            
        Returns:
            RiskResult with score and requirements
        """
        # BUG-01: Ensure minimum auth level
        auth_level = max(0.1, plan.auth_level)
        
        # Get action weight
        action_weight = self.action_weights.get(plan.action_type, 0.5)
        
        # Scope sensitivity (system paths = higher)
        scope_sensitivity = self._calculate_scope_sensitivity(plan.scope)
        
        # Frequency penalty
        freq_penalty = self._calculate_frequency_penalty(plan.action_type)
        
        # Calculate R_SCORE
        r_score = (action_weight * 0.4 + 
                   scope_sensitivity * 0.3 + 
                   (1.0 - auth_level) * 0.2 +
                   freq_penalty * 0.1)
        
        # Clamp to 0-1
        r_score = max(0.0, min(1.0, r_score))
        
        # Determine lock level
        if r_score < 0.4:
            lock_level = LockLevel.L1
            auto_approve = True
            requires_totp = False
            requires_usb = False
            reason = "Low risk action - auto-approved"
        elif r_score < 0.7:
            lock_level = LockLevel.L2
            auto_approve = False
            requires_totp = True
            requires_usb = False
            reason = "Medium risk - TOTP required"
        else:
            lock_level = LockLevel.L3
            auto_approve = False
            requires_totp = True
            requires_usb = True
            reason = "High risk - USB + TOTP required"
        
        # Record action
        self._record_action(plan.action_type)
        
        return RiskResult(
            r_score=r_score,
            lock_level=lock_level,
            auto_approve=auto_approve,
            requires_totp=requires_totp,
            requires_usb=requires_usb,
            reason=reason
        )
    
    def _calculate_scope_sensitivity(self, scope: str) -> float:
        """Calculate sensitivity of scope path."""
        scope_lower = scope.lower()
        
        # System paths = highest sensitivity
        if any(p in scope_lower for p in ['/etc/', '/sys/', '/proc/', '/boot/']):
            return 1.0
        
        # Config paths = high sensitivity
        if any(p in scope_lower for p in ['config', 'settings', 'vault']):
            return 0.8
        
        # Data paths = medium sensitivity
        if any(p in scope_lower for p in ['data/', 'db/', 'memory']):
            return 0.6
        
        # Log paths = low sensitivity
        if 'log' in scope_lower:
            return 0.3
        
        # Default
        return 0.5
    
    def _calculate_frequency_penalty(self, action_type: str) -> float:
        """Calculate frequency penalty for repeated actions."""
        now = time.time()
        
        if action_type not in self._action_history:
            return 0.0
        
        # Count recent actions
        recent = [t for t in self._action_history[action_type] 
                  if now - t < self.frequency_window]
        
        self._action_history[action_type] = recent
        
        # Penalty after 3rd action
        if len(recent) > 3:
            penalty = min(0.30, (len(recent) - 3) * self.frequency_penalty)
            return penalty
        
        return 0.0
    
    def _record_action(self, action_type: str) -> None:
        """Record action for frequency tracking."""
        if action_type not in self._action_history:
            self._action_history[action_type] = []
        
        self._action_history[action_type].append(time.time())
    
    def validate_scope(self, requested: str, allowed_root: str) -> bool:
        """
        Validate that requested path is within allowed root.
        
        Args:
            requested: Requested path
            allowed_root: Allowed root directory
            
        Returns:
            True if path is valid
        """
        import os
        
        try:
            # Resolve both paths
            requested_abs = os.path.abspath(os.path.expanduser(requested))
            allowed_abs = os.path.abspath(os.path.expanduser(allowed_root))
            
            # Check if requested is within allowed
            return requested_abs.startswith(allowed_abs)
        except Exception:
            return False
    
    def issue_l3_challenge(self, description: str) -> Dict[str, Any]:
        """
        Issue L3 physical lock challenge.
        
        Args:
            description: Description of action requiring L3
            
        Returns:
            Challenge details
        """
        return {
            "level": "L3",
            "description": description,
            "requires_usb": True,
            "requires_totp": True,
            "ttl_seconds": 120,
            "issued_at": time.time()
        }
    
    def get_stats(self) -> Dict[str, Any]:
        """Get risk engine statistics."""
        return {
            "action_history_count": sum(len(v) for v in self._action_history.values()),
            "tracked_actions": list(self._action_history.keys()),
            "action_weights": self.action_weights
        }


# Global instance
_engine: Optional[RiskEngine] = None


def get_engine() -> RiskEngine:
    """Get or create global risk engine."""
    global _engine
    if _engine is None:
        _engine = RiskEngine()
    return _engine


def calculate(action_type: str, scope: str, auth_level: float, 
              frequency_count: int = 0) -> RiskResult:
    """Calculate risk for action."""
    plan = ActionPlan(
        action_type=action_type,
        scope=scope,
        auth_level=auth_level,
        frequency_count=frequency_count
    )
    return get_engine().calculate(plan)


def validate_scope(requested: str, allowed_root: str) -> bool:
    """Validate scope path."""
    return get_engine().validate_scope(requested, allowed_root)


def issue_l3_challenge(description: str) -> Dict[str, Any]:
    """Issue L3 challenge."""
    return get_engine().issue_l3_challenge(description)
