"""
AENIDA Sanitizer Shield
12-layer input sanitization pipeline for security.
"""

import re
import html
import unicodedata
from typing import Dict, List, Union, Any


class SanitizationError(Exception):
    """Raised when sanitization fails in strict mode."""
    pass


class SanitizerShield:
    """
    12-layer sanitization pipeline.
    Stateless - all regex pre-compiled at class definition.
    """
    
    __slots__ = []
    
    # Pre-compiled regex patterns for performance
    _ANSI_ESCAPE = re.compile(r'\x1b\[[0-9;]*m')
    _SCRIPT_TAG = re.compile(r'<script[^>]*>.*?</script>', re.IGNORECASE | re.DOTALL)
    _HTML_TAG = re.compile(r'<[^>]{0,1024}>', re.IGNORECASE)
    _SHELL_META = re.compile(r'[;&|`$(){}\[\]\\]')
    _PATH_TRAVERSAL = re.compile(r'\.\./|\.\.\\\\|\\\\\.\\')
    _DANGEROUS_URI = re.compile(r'(javascript|vbscript|data|file):', re.IGNORECASE)
    _EVENT_HANDLER = re.compile(r'\son\w+\s*=', re.IGNORECASE)
    _SYSTEM_PATH = re.compile(r'/(etc|sys|proc)/|C:\\Windows|\.ssh|id_rsa|\.gnupg', re.IGNORECASE)
    _NULL_BYTE = re.compile(r'\x00')
    _CONTROL_CHAR = re.compile(r'[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f-\x9f]')
    
    # Max content size (1MB)
    MAX_SIZE = 1024 * 1024
    
    @classmethod
    def scrub(cls, data: str, strict: bool = False) -> str:
        """
        Run 12-layer sanitization pipeline.
        
        Args:
            data: Input string to sanitize
            strict: If True, raise SanitizationError on threats
            
        Returns:
            Sanitized string
        """
        if not isinstance(data, str):
            data = str(data)
        
        # Layer 1: Length guard
        if len(data) > cls.MAX_SIZE:
            if strict:
                raise SanitizationError("Content exceeds maximum size")
            data = data[:cls.MAX_SIZE]
        
        # Layer 2: NFKC normalization (BUG-05: homoglyph fix)
        data = unicodedata.normalize('NFKC', data)
        
        # Layer 3: Null bytes
        data = cls._NULL_BYTE.sub('', data)
        
        # Layer 4: ANSI escapes
        data = cls._ANSI_ESCAPE.sub('', data)
        
        # Layer 5: Control chars (keep \t\n\r)
        data = cls._CONTROL_CHAR.sub('', data)
        
        # Layer 6: Script tags
        has_script = cls._SCRIPT_TAG.search(data)
        if has_script and strict:
            raise SanitizationError("Script tags detected")
        data = cls._SCRIPT_TAG.sub('[⛔SCRIPT]', data)
        
        # Layer 7: Dangerous URIs (BUG-06)
        has_dangerous_uri = cls._DANGEROUS_URI.search(data)
        if has_dangerous_uri and strict:
            raise SanitizationError("Dangerous URI scheme detected")
        data = cls._DANGEROUS_URI.sub('[⛔URI]', data)
        
        # Layer 8: Event handlers (BUG-06)
        has_event = cls._EVENT_HANDLER.search(data)
        if has_event and strict:
            raise SanitizationError("Event handler detected")
        data = cls._EVENT_HANDLER.sub('[⛔EVENT]', data)
        
        # Layer 9: HTML tags
        data = cls._HTML_TAG.sub('', data)
        
        # Layer 10: Shell metacharacters
        data = cls._SHELL_META.sub('', data)
        
        # Layer 11: Path traversal
        data = cls._PATH_TRAVERSAL.sub('', data)
        
        # Layer 12: System paths
        data = cls._SYSTEM_PATH.sub('[⛔SYSPATH]', data)
        
        # Output: HTML escape
        data = html.escape(data, quote=True)
        
        return data
    
    @classmethod
    def scrub_dict(cls, d: Dict[str, Any], strict: bool = False) -> Dict[str, Any]:
        """Sanitize all string values in a dictionary."""
        result = {}
        for key, value in d.items():
            # Sanitize key too
            clean_key = cls.scrub(key, strict)
            
            if isinstance(value, str):
                result[clean_key] = cls.scrub(value, strict)
            elif isinstance(value, dict):
                result[clean_key] = cls.scrub_dict(value, strict)
            elif isinstance(value, list):
                result[clean_key] = cls.scrub_list(value, strict)
            else:
                result[clean_key] = value
        
        return result
    
    @classmethod
    def scrub_list(cls, lst: List[Any], strict: bool = False) -> List[Any]:
        """Sanitize all string items in a list."""
        result = []
        for item in lst:
            if isinstance(item, str):
                result.append(cls.scrub(item, strict))
            elif isinstance(item, dict):
                result.append(cls.scrub_dict(item, strict))
            elif isinstance(item, list):
                result.append(cls.scrub_list(item, strict))
            else:
                result.append(item)
        
        return result
    
    @classmethod
    def is_clean(cls, data: str) -> bool:
        """Check if data is clean (would pass strict mode)."""
        try:
            cls.scrub(data, strict=True)
            return True
        except SanitizationError:
            return False
    
    @classmethod
    def threat_report(cls, data: str) -> Dict[str, bool]:
        """Get detailed threat report for data."""
        if not isinstance(data, str):
            data = str(data)
        
        # Normalize first
        normalized = unicodedata.normalize('NFKC', data)
        
        return {
            "oversized": len(data) > cls.MAX_SIZE,
            "has_script": cls._SCRIPT_TAG.search(normalized) is not None,
            "has_dangerous_uri": cls._DANGEROUS_URI.search(normalized) is not None,
            "has_event_handler": cls._EVENT_HANDLER.search(normalized) is not None,
            "has_shell_meta": cls._SHELL_META.search(normalized) is not None,
            "has_path_traversal": cls._PATH_TRAVERSAL.search(normalized) is not None,
            "has_system_path": cls._SYSTEM_PATH.search(normalized) is not None,
            "has_null_byte": cls._NULL_BYTE.search(data) is not None,
            "has_control_chars": cls._CONTROL_CHAR.search(data) is not None
        }


# Convenience functions
def scrub(data: str, strict: bool = False) -> str:
    """Sanitize string data."""
    return SanitizerShield.scrub(data, strict)


def scrub_dict(d: Dict[str, Any], strict: bool = False) -> Dict[str, Any]:
    """Sanitize dictionary."""
    return SanitizerShield.scrub_dict(d, strict)


def scrub_list(lst: List[Any], strict: bool = False) -> List[Any]:
    """Sanitize list."""
    return SanitizerShield.scrub_list(lst, strict)


def is_clean(data: str) -> bool:
    """Check if data is clean."""
    return SanitizerShield.is_clean(data)


def threat_report(data: str) -> Dict[str, bool]:
    """Get threat report."""
    return SanitizerShield.threat_report(data)
