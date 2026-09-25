"""
AENIDA RTC Wake Manager
Schedules Office PC to wake/sleep via rtcwake (free, built-in Linux).
Handles both nightly tasks and morning wake.
"""
import logging, os, subprocess, time
from typing import Any, Dict

def verify_rtc_support() -> bool:
    try:
        result = subprocess.run(
            ["cat", "/sys/class/rtc/rtc0/wakealarm"],
            capture_output=True, text=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False

def set_wake_alarm(target_timestamp: float) -> bool:
    try:
        # Clear existing alarm
        subprocess.run(
            ["sh","-c","echo 0 > /sys/class/rtc/rtc0/wakealarm"],
            capture_output=True, timeout=5, check=False)
        # Set new alarm
        result = subprocess.run(
            ["sudo","rtcwake","-m","no","-l","-t",str(int(target_timestamp))],
            capture_output=True, text=True, timeout=10)
        return result.returncode == 0
    except Exception as e:
        logging.warning(f"[RTC] set_wake_alarm failed: {e}")
        return False

def schedule_nightly_wake(wake_hour: int = 1, wake_min: int = 55) -> bool:
    """Schedule wake at 01:55 AM tomorrow."""
    now = time.time()
    local = time.localtime(now)
    # Compute seconds until tomorrow HH:MM
    midnight = now - (local.tm_hour*3600 + local.tm_min*60 + local.tm_sec)
    target = midnight + 86400 + wake_hour*3600 + wake_min*60
    if target - now < 3600:  # already past today's time? use tomorrow
        target += 86400
    ok = set_wake_alarm(target)
    if ok:
        logging.info(f"[RTC] Nightly wake set for {time.strftime('%Y-%m-%d %H:%M',time.localtime(target))}")
    else:
        logging.warning("[RTC] Could not set nightly wake — tasks will queue in task_queue.db")
    return ok

def schedule_morning_wake(wake_hour: int = 8, wake_min: int = 0) -> bool:
    """Schedule wake at 08:00 AM today (for workday)."""
    now = time.time()
    local = time.localtime(now)
    midnight = now - (local.tm_hour*3600 + local.tm_min*60 + local.tm_sec)
    target = midnight + wake_hour*3600 + wake_min*60
    if target < now:
        target += 86400  # already past — use tomorrow
    ok = set_wake_alarm(target)
    if ok:
        logging.info(f"[RTC] Morning wake set for {time.strftime('%H:%M',time.localtime(target))}")
    return ok

def suspend_to_ram() -> bool:
    """Suspend PC to RAM (S3, ~2W)."""
    try:
        result = subprocess.run(
            ["sudo","rtcwake","-m","mem","-s","1"],
            capture_output=True, text=True, timeout=10)
        return result.returncode == 0
    except Exception as e:
        logging.error(f"[RTC] suspend failed: {e}")
        return False

def get_next_wake_time() -> str:
    try:
        with open("/sys/class/rtc/rtc0/wakealarm") as f:
            ts_str = f.read().strip()
        if ts_str and ts_str != "0":
            return time.strftime("%Y-%m-%d %H:%M",
                                  time.localtime(int(ts_str)))
    except Exception:
        pass
    return "Not set"

def get_status() -> Dict[str, Any]:
    return {
        "rtc_supported": verify_rtc_support(),
        "next_wake": get_next_wake_time(),
    }

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import json
    print(json.dumps(get_status(), indent=2))
    supported = verify_rtc_support()
    print(f"RTC supported: {supported}")
    if supported:
        schedule_nightly_wake()
