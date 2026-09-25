#!/usr/bin/env python3
"""
AENIDA — TradingView MCP Bridge Setup
======================================
Run this script ONCE on your Worker PC to install and configure
the TradingView MCP Bridge GitHub project.

Usage:
    python tools/setup_tradingview_mcp.py

Requirements on Worker PC:
    - Node.js 18+ (https://nodejs.org)
    - Git
    - TradingView Desktop app installed & running
    - A valid TradingView subscription
"""

import os
import sys
import subprocess
import json
import shutil
import time

REPO_URL   = "https://github.com/mac-/tradingview-mcp"
INSTALL_DIR = os.path.expanduser("~/tradingview-mcp")
# BUG-ST-2 fix: anchor config path to this file's real location, not CWD
CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "core", "config.json"
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _run(cmd: list, cwd: str = None, check: bool = True) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=cwd, check=check,
                          capture_output=False, text=True)


def _check_node() -> bool:
    try:
        result = subprocess.run(["node", "--version"],
                                capture_output=True, text=True)
        ver = result.stdout.strip()
        major = int(ver.lstrip("v").split(".")[0])
        if major < 18:
            print(f"  ✗ Node.js {ver} found — need v18+. Download: https://nodejs.org")
            return False
        print(f"  ✓ Node.js {ver}")
        return True
    except FileNotFoundError:
        print("  ✗ Node.js not found. Install from https://nodejs.org")
        return False


def _check_git() -> bool:
    if shutil.which("git"):
        print("  ✓ Git found")
        return True
    print("  ✗ Git not found. Install from https://git-scm.com")
    return False


# ── Steps ─────────────────────────────────────────────────────────────────────

def step_clone():
    print("\n[1/4] Cloning TradingView MCP Bridge repository...")
    if os.path.exists(INSTALL_DIR):
        print(f"  Already cloned at {INSTALL_DIR} — pulling latest")
        _run(["git", "pull"], cwd=INSTALL_DIR)
    else:
        _run(["git", "clone", REPO_URL, INSTALL_DIR])
    print(f"  ✓ Repository ready at {INSTALL_DIR}")


def step_install_deps():
    print("\n[2/4] Installing Node.js dependencies...")
    _run(["npm", "install"], cwd=INSTALL_DIR)
    print("  ✓ npm install complete")


def step_build():
    # BUG-ST-1 fix: check if a "build" script exists in package.json before
    # calling it. tradingview-mcp uses TypeScript so the build step compiles
    # TS → JS into dist/. If the repo ships pre-built dist/ we skip safely.
    pkg_json = os.path.join(INSTALL_DIR, "package.json")
    has_build = False
    try:
        with open(pkg_json) as f:
            pkg = json.load(f)
        has_build = "build" in pkg.get("scripts", {})
    except Exception:
        pass

    if has_build:
        print("\n[3/4] Building the MCP server (TypeScript → dist/)...")
        try:
            _run(["npm", "run", "build"], cwd=INSTALL_DIR, check=True)
            print("  ✓ Build complete")
        except subprocess.CalledProcessError as exc:
            print(f"  ⚠ Build failed (exit {exc.returncode}) — "
                  "if dist/ already exists this may be harmless.")
    else:
        # Check if dist/index.js already exists (pre-built or JS-only repo)
        dist_entry = os.path.join(INSTALL_DIR, "dist", "index.js")
        if os.path.exists(dist_entry):
            print("\n[3/4] dist/index.js already present — skipping build step ✓")
        else:
            print("\n[3/4] No 'build' script found in package.json and dist/ missing.")
            print("      Try manually: cd ~/tradingview-mcp && npx tsc")


def step_update_aenida_config(port: int = 3000):
    print(f"\n[4/4] Updating AENIDA config (tradingview.mcp_port = {port})...")
    try:
        cfg_path = os.path.abspath(CONFIG_PATH)
        with open(cfg_path) as f:
            cfg = json.load(f)
        cfg.setdefault("tradingview", {})
        cfg["tradingview"]["mcp_project_dir"] = INSTALL_DIR
        cfg["tradingview"]["mcp_port"]         = port
        with open(cfg_path, "w") as f:
            json.dump(cfg, f, indent=2)
        print(f"  ✓ Config updated: {cfg_path}")
    except Exception as e:
        print(f"  ! Could not update config automatically: {e}")
        print(f"    Manually set  tradingview.mcp_project_dir = \"{INSTALL_DIR}\"")
        print(f"    and           tradingview.mcp_port = {port}")


def step_print_instructions():
    print("""
╔══════════════════════════════════════════════════════════════════╗
║           TradingView MCP Bridge — Setup Complete!              ║
╠══════════════════════════════════════════════════════════════════╣
║                                                                  ║
║  EVERY TIME you want AENIDA to control TradingView:              ║
║                                                                  ║
║  1. Open TradingView Desktop on this Worker PC                   ║
║                                                                  ║
║  2. Start the MCP server (in a new terminal):                    ║
║       cd ~/tradingview-mcp && npm start                          ║
║                                                                  ║
║  3. Start AENIDA Worker as usual:                                ║
║       cd aenida_v3_working/mobile && python bridge.py --mode worker║
║                                                                  ║
║  From the Laptop (Mother), send these task types:                ║
║   • tradingview_analyze    — AI chart analysis + signal          ║
║   • tradingview_chart_data — raw OHLCV + indicator data          ║
║   • tradingview_pine       — run/save Pine Scripts               ║
║   • tradingview_alert      — create/list price alerts            ║
║   • tradingview_screenshot — capture chart image                 ║
║   • tradingview_set_symbol — switch symbol/timeframe             ║
║   • tradingview_status     — check MCP health                    ║
║                                                                  ║
║  All data processing stays on THIS Worker PC.                    ║
║  No TradingView data is sent to any external server.             ║
╚══════════════════════════════════════════════════════════════════╝
""")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  AENIDA × TradingView MCP Bridge — Worker PC Setup")
    print("=" * 60)

    print("\nChecking prerequisites...")
    if not _check_node() or not _check_git():
        sys.exit(1)

    # Ask for custom port
    port_input = input("\nMCP server port [default 3000]: ").strip()
    port = int(port_input) if port_input.isdigit() else 3000

    try:
        step_clone()
        step_install_deps()
        step_build()
        step_update_aenida_config(port)
        step_print_instructions()
    except subprocess.CalledProcessError as e:
        print(f"\n✗ Setup failed at command: {e.cmd}")
        print("  Check the error above and re-run this script.")
        sys.exit(1)


if __name__ == "__main__":
    main()
