"""
AENIDA USB Sentinel
Checks USB token presence every 30s (BUG-38 fix).
Token removed → immediate role downgrade to Worker.
Token re-inserted → requires fresh Triple-Lock auth.
"""
import logging, os, threading, time
from typing import Any, Dict, Optional

USB_LABEL = "AENIDA"
USB_PATHS = [
    f"/dev/disk/by-label/{USB_LABEL}",
    f"/media/{USB_LABEL}",
    f"/mnt/{USB_LABEL}",
]

_state: Dict[str, Any] = {
    "present": False, "role": "worker",
    "last_check": 0.0, "node_id": None,
    "last_inserted": None, "last_removed": None,
    "needs_reauth": False,
}
_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_running = False
MASTER_MODE = False


def _detect_usb() -> bool:
    """Check if AENIDA USB token is physically present."""
    for path in USB_PATHS:
        if os.path.exists(path):
            return True
    return False


def _on_removed() -> None:
    global MASTER_MODE
    logging.warning("[USB_SENTINEL] USB token REMOVED — downgrading to Worker")
    MASTER_MODE = False
    with _lock:
        _state["role"] = "worker"
        _state["node_id"] = None
        _state["last_removed"] = time.time()
        _state["needs_reauth"] = False
    # Revoke all TYR tokens immediately
    try:
        from security_gateway import get_notary
        count = get_notary().revoke_all_tokens()
        logging.warning(f"[USB_SENTINEL] Revoked {count} active TYR tokens")
    except Exception as e:
        logging.error(f"[USB_SENTINEL] Token revoke failed: {e}")
    # Alert
    try:
        from alert_manager import send, AlertLevel
        send(AlertLevel.CRITICAL, "USB Token Removed",
             "Master mode revoked. Re-insert USB and re-authenticate.")
    except Exception:
        pass


def _on_inserted() -> None:
    logging.info("[USB_SENTINEL] USB token INSERTED — re-auth required")
    with _lock:
        _state["last_inserted"] = time.time()
        _state["needs_reauth"] = True  # Cannot auto-elevate
    try:
        from alert_manager import send, AlertLevel
        send(AlertLevel.WARNING, "USB Token Inserted",
             "Re-authenticate to restore Master mode.")
    except Exception:
        pass


def _monitor_loop() -> None:
    global _running
    prev_present = _detect_usb()
    with _lock:
        _state["present"] = prev_present
        _state["last_check"] = time.time()
    while _running:
        time.sleep(30)
        now_present = _detect_usb()
        with _lock:
            _state["last_check"] = time.time()
        if now_present and not prev_present:
            _on_inserted()
        elif not now_present and prev_present:
            _on_removed()
        with _lock:
            _state["present"] = now_present
        prev_present = now_present


def start_monitoring() -> None:
    global _thread, _running
    _running = True
    _thread = threading.Thread(target=_monitor_loop,
                                daemon=True, name="usb_sentinel")
    _thread.start()
    logging.info("[USB_SENTINEL] Started (checking every 30s)")


def stop_monitoring() -> None:
    global _running
    _running = False


def is_present() -> bool:
    with _lock:
        return _state["present"]


def get_current_role() -> str:
    with _lock:
        return _state["role"]


def get_usb_state() -> Dict[str, Any]:
    with _lock:
        return _state.copy()


def force_downgrade() -> None:
    _on_removed()


def elevate_to_master(node_id: str) -> bool:
    """
    Called after successful Triple-Lock auth while USB is present.
    Only works if USB is actually present.
    """
    global MASTER_MODE
    if not is_present():
        logging.error("[USB_SENTINEL] Cannot elevate — USB not present")
        return False
    with _lock:
        _state["role"] = "mother"
        _state["node_id"] = node_id
        _state["needs_reauth"] = False
    MASTER_MODE = True
    logging.info(f"[USB_SENTINEL] Elevated to Master — node_id={node_id}")
    return True
