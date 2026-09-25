"""
TradingView MCP integration tests.
Run on the Worker PC AFTER:
  1. TradingView Desktop is open
  2. cd ~/tradingview-mcp && npm run build
  3. python tests/test_tradingview_mcp.py

BUGS FIXED vs v1:
  BUG-TEST-1  sys.path inserts used relative paths → broke when pytest runs
              from project root.  Fixed with os.path.abspath.
  BUG-TEST-2  Imported private _dispatch_task_real → replaced with public
              handle_tradingview_task which is the real contract boundary.
"""

import os
import sys

# BUG-TEST-1 fix: always anchor paths relative to THIS file's real location.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))

for _p in [
    os.path.join(_ROOT, "trading"),
    os.path.join(_ROOT, "mobile"),
    os.path.join(_ROOT, "core"),
]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tradingview_mcp import (        # noqa: E402
    TradingViewMCPBridge,
    handle_tradingview_task,
    _mcp_dir,
    _mcp_entry,
)

PASS = "✓"
FAIL = "✗"
_results: list = []


def check(name: str, ok: bool, hint: str = "") -> None:
    _results.append(ok)
    tag = PASS if ok else FAIL
    suffix = f"  [{hint}]" if hint else ""
    print(f"  {tag} {name}{suffix}")


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_paths() -> None:
    """Verify path helpers return absolute, non-empty strings."""
    print("\n[1] Path helpers")
    d = _mcp_dir()
    e = _mcp_entry()
    check("_mcp_dir() is absolute",  os.path.isabs(d),  d)
    check("_mcp_entry() is absolute", os.path.isabs(e), e)
    check("_mcp_entry() ends in index.js", e.endswith("index.js"))


def test_mcp_status() -> bool:
    print("\n[2] MCP process availability")
    bridge = TradingViewMCPBridge()
    available = bridge.is_available()
    check(
        "MCP process alive",
        available,
        "Run: cd ~/tradingview-mcp && npm run build, then open TradingView Desktop",
    )
    return available


def test_chart_data() -> None:
    print("\n[3] Chart data  (tradingview_chart_data)")
    result = handle_tradingview_task("tradingview_chart_data", {
        "symbol": "BTCUSDT", "interval": "1H",
    })
    check("status == ok",        result.get("status") == "ok",  str(result.get("status")))
    check("chart key present",   "chart" in result)
    check("symbol echoed back",  result.get("symbol") == "BTCUSDT")
    check("no error in result",  "error" not in result)


def test_analyze() -> None:
    print("\n[4] Chart analysis  (tradingview_analyze)")
    result = handle_tradingview_task("tradingview_analyze", {
        "symbol": "BTCUSDT", "interval": "1H",
        "question": "Is the trend bullish or bearish?",
    })
    check("status == ok",       result.get("status") == "ok")
    check("analysis key present", "analysis" in result)


def test_pine_list() -> None:
    print("\n[5] Pine Script list  (tradingview_pine action=list)")
    result = handle_tradingview_task("tradingview_pine", {"action": "list"})
    check("status == ok",  result.get("status") == "ok",  str(result.get("status")))
    check("action echoed", result.get("action") == "list")


def test_pine_bad_action() -> None:
    print("\n[6] Pine Script bad action (error path)")
    result = handle_tradingview_task("tradingview_pine", {"action": "explode"})
    # Must return status=ok with an error inside result, not a crash
    check("returns dict",    isinstance(result, dict))
    check("has status key",  "status" in result)
    # Either error status or an error key inside result
    check("reports error",
          result.get("status") == "error" or "error" in result.get("result", {}))


def test_alert_list() -> None:
    print("\n[7] Alert list  (tradingview_alert action=list)")
    result = handle_tradingview_task("tradingview_alert", {"action": "list"})
    check("status == ok", result.get("status") == "ok")


def test_status_task() -> None:
    print("\n[8] Status task  (tradingview_status)")
    result = handle_tradingview_task("tradingview_status", {})
    check("has mcp_available key", "mcp_available" in result)
    check("has mcp_dir key",       "mcp_dir" in result)
    check("mcp_dir is absolute",
          os.path.isabs(result.get("mcp_dir", "")))


def test_unknown_task() -> None:
    print("\n[9] Unknown task type (error path)")
    result = handle_tradingview_task("tradingview_nonexistent", {})
    check("status == error", result.get("status") == "error")
    check("error message present", bool(result.get("error")))


# BUG-TEST-2 fix: test public handle_tradingview_task directly instead of
# importing the private bridge._dispatch_task_real function.
def test_public_dispatch() -> None:
    print("\n[10] Public dispatch contract")
    result = handle_tradingview_task("tradingview_status", {})
    check("returns dict",       isinstance(result, dict))
    check("has 'status' key",   "status" in result)
    check("has 'source' key",   "source" in result)
    check("source == tradingview_mcp",
          result.get("source") == "tradingview_mcp")


# ── Runner ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 58)
    print("  AENIDA × TradingView MCP — Integration Tests (Fixed)")
    print("=" * 58)

    test_paths()
    test_public_dispatch()
    test_status_task()
    test_unknown_task()
    test_pine_bad_action()

    mcp_up = test_mcp_status()
    if mcp_up:
        test_chart_data()
        test_analyze()
        test_pine_list()
        test_alert_list()
    else:
        print("\n  ⚠  Skipping live tests — MCP process not running.")
        print("     Build: cd ~/tradingview-mcp && npm install && npm run build")
        print("     Then open TradingView Desktop and re-run this script.")

    passed = sum(_results)
    total  = len(_results)
    print(f"\n{'=' * 58}")
    print(f"  Results: {passed}/{total} passed")
    print("  All tests passed ✓" if passed == total
          else f"  {total - passed} test(s) need attention ✗")
    print("=" * 58)
    sys.exit(0 if passed == total else 1)
