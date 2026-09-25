"""
AENIDA Fallback Chain
AI API call routing with health monitoring and degraded mode.
"""

import time
import os
import logging
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field
from enum import Enum
import threading


class ProviderStatus(Enum):
    """Provider health status."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"
    BANNED = "banned"


@dataclass
class Provider:
    """AI provider configuration and state."""
    name: str
    status: ProviderStatus = ProviderStatus.HEALTHY
    last_checked: float = field(default_factory=time.time)
    ttl: int = 300  # Seconds until recheck
    fail_streak: int = 0
    banned_until: float = 0
    last_result: Optional[str] = None


class FallbackChain:
    """
    Manages AI API fallback chain with dynamic health monitoring.
    Order: Groq → Gemini → Together AI → Local → Offline
    """
    
    __slots__ = ['providers', 'chain_order', '_lock', 'last_cache', 
                 'degraded_since', 'offline_mode']
    
    def __init__(self, chain_order: Optional[List[str]] = None):
        self.chain_order = chain_order or ["groq", "gemini", "together_ai", "local", "offline"]
        self._lock = threading.Lock()
        self.last_cache: Optional[str] = None
        self.degraded_since: Optional[float] = None
        self.offline_mode: bool = False
        
        # Initialize providers
        self.providers: Dict[str, Provider] = {}
        for name in self.chain_order:
            self.providers[name] = Provider(name=name)
    
    def call_ai(self, prompt: str, image=None) -> dict:
        """Call AI with fallback chain. Network calls happen OUTSIDE the lock."""
        for provider_name in self.chain_order:
            # Step 1: Check provider status INSIDE lock (fast)
            with self._lock:
                provider = self.providers[provider_name]
                if provider.status == ProviderStatus.BANNED:
                    if time.time() < provider.banned_until:
                        continue
                    provider.status = ProviderStatus.HEALTHY
                    provider.fail_streak = 0
                if time.time() - provider.last_checked > provider.ttl:
                    provider.last_checked = time.time()
                    should_recheck = True
                else:
                    should_recheck = False
                skip = provider.status == ProviderStatus.DOWN
                current_name = provider.name

            if skip:
                continue

            # Step 2: Recheck OUTSIDE lock if needed
            if should_recheck:
                self._recheck_provider_safe(current_name)

            # Step 3: Make the API call OUTSIDE lock (slow network call)
            result = self._try_provider_safe(current_name, prompt, image)

            # Step 4: Update status INSIDE lock (fast)
            with self._lock:
                provider = self.providers[current_name]
                if result:
                    provider.fail_streak = 0
                    self.last_cache = result
                    if self.degraded_since:
                        self.degraded_since = None
                        logging.info("Exited degraded mode")
                    return {"result": result, "provider": current_name, "degraded": False}
                else:
                    provider.fail_streak += 1
                    if provider.fail_streak >= 3:
                        provider.status = ProviderStatus.BANNED
                        provider.banned_until = time.time() + 600
                        logging.warning(f"Banned {current_name} for 10 min")

        # All providers failed
        with self._lock:
            if not self.degraded_since:
                self.degraded_since = time.time()
                logging.critical("ENTERING FULL DEGRADED MODE")
            degraded_time = time.strftime("%H:%M", time.localtime())
            cache_result = self.last_cache or "[No cached result available]"

        return {
            "result": f"{cache_result} [FULL DEGRADED {degraded_time}]",
            "provider": "offline",
            "degraded": True
        }

    def _try_provider_safe(self, name: str, prompt: str, image) -> 'Optional[str]':
        """Call provider outside lock."""
        try:
            if name == "groq":       return self._call_groq(prompt, image)
            elif name == "gemini":   return self._call_gemini(prompt, image)
            elif name == "together_ai": return self._call_together(prompt, image)
            elif name == "local":    return self._call_local(prompt, image)
            elif name == "offline":  return self._call_offline(prompt, image)
        except Exception as e:
            logging.error(f"{name} failed: {e}")
        return None

    def _recheck_provider_safe(self, name: str) -> None:
        """Recheck provider health outside lock."""
        try:
            if name == "groq":
                import groq
                client = groq.Groq(api_key=os.environ.get("GROQ_API_KEY", ""))
                client.models.list()
                status = ProviderStatus.HEALTHY
            elif name == "gemini":
                import google.generativeai as genai
                genai.configure(api_key=os.environ.get("GEMINI_API_KEY", ""))
                status = ProviderStatus.HEALTHY
            else:
                status = ProviderStatus.HEALTHY
        except Exception:
            status = ProviderStatus.DEGRADED
        with self._lock:
            self.providers[name].status = status
    
    def _try_provider(self, provider: 'Provider', prompt: str, image) -> 'Optional[str]':
        """Legacy wrapper - use _try_provider_safe instead."""
        return self._try_provider_safe(provider.name, prompt, image)
    
    def _call_groq(self, prompt: str, image: Optional[Any]) -> Optional[str]:
        """Call Groq API."""
        try:
            import groq
            client = groq.Groq(api_key=os.environ.get("GROQ_API_KEY", ""))
            
            messages = [{"role": "user", "content": prompt}]
            
            response = client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=messages,
                max_tokens=1000,
                timeout=30
            )
            
            return response.choices[0].message.content
        except Exception as e:
            logging.error(f"Groq error: {e}")
            return None
    
    def _call_gemini(self, prompt: str, image: Optional[Any]) -> Optional[str]:
        """Call Gemini API."""
        try:
            import google.generativeai as genai
            genai.configure(api_key=os.environ.get("GEMINI_API_KEY", ""))
            
            model = genai.GenerativeModel('gemini-1.5-flash')
            response = model.generate_content(prompt)
            
            return response.text
        except Exception as e:
            logging.error(f"Gemini error: {e}")
            return None
    
    def _call_together(self, prompt: str, image: Optional[Any]) -> Optional[str]:
        """Call Together AI API."""
        try:
            import requests
            
            api_key = os.environ.get("TOGETHER_API_KEY", "")
            if not api_key:
                return None
            
            url = "https://api.together.xyz/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": "meta-llama/Llama-3-8b-chat-hf",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 1000
            }
            
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            
            if response.status_code == 200:
                return response.json()["choices"][0]["message"]["content"]
            return None
        except Exception as e:
            logging.error(f"Together AI error: {e}")
            return None
    
    def _call_local(self, prompt: str, image: Optional[Any]) -> Optional[str]:
        """Call local rule-based engine."""
        # Simple rule-based responses
        prompt_lower = prompt.lower()
        
        if "price" in prompt_lower or "chart" in prompt_lower:
            return "[Local] Price analysis requires market data connection."
        elif "signal" in prompt_lower or "trade" in prompt_lower:
            return "[Local] Trading signals unavailable in offline mode."
        else:
            return "[Local] Query processed with rule-based engine."
    
    def _call_offline(self, prompt: str, image: Optional[Any]) -> Optional[str]:
        """Return cached result or placeholder."""
        if self.last_cache:
            return self.last_cache
        return "[Offline] No cached data available."
    
    def _recheck_provider(self, provider: 'Provider') -> None:
        """Legacy wrapper - use _recheck_provider_safe instead."""
        self._recheck_provider_safe(provider.name)
    
    def get_provider_status(self) -> Dict[str, Any]:
        """Get status of all providers."""
        with self._lock:
            return {
                name: {
                    "status": p.status.value,
                    "fail_streak": p.fail_streak,
                    "last_checked": p.last_checked
                }
                for name, p in self.providers.items()
            }
    
    def force_recheck_all(self) -> Dict[str, Any]:
        """Force recheck of all providers.

        Rechecks are done OUTSIDE the lock to avoid deadlock:
        _recheck_provider_safe acquires self._lock at the end to write
        the updated status, so calling it while already holding the same
        threading.Lock (non-reentrant) would deadlock.
        """
        with self._lock:
            names = list(self.providers.keys())
        for name in names:
            self._recheck_provider_safe(name)
        return self.get_provider_status()


# Global instance
_chain: Optional[FallbackChain] = None


def get_chain() -> FallbackChain:
    """Get or create global fallback chain."""
    global _chain
    if _chain is None:
        _chain = FallbackChain()
    return _chain


def call_ai(prompt: str, image: Optional[Any] = None) -> Dict[str, Any]:
    """Call AI with fallback."""
    return get_chain().call_ai(prompt, image)


def get_provider_status() -> Dict[str, Any]:
    """Get provider status."""
    return get_chain().get_provider_status()


def force_recheck_all() -> Dict[str, Any]:
    """Force recheck all providers."""
    return get_chain().force_recheck_all()
