# AENIDA v6 + TradingView MCP — Complete Setup Guide

---

## Part 1 — Bug Fix Summary (10 bugs resolved)

| ID | Severity | File | What was wrong | Fix |
|---|---|---|---|---|
| BUG-TV-2 | 🔴 CRITICAL | tradingview_mcp.py | HTTP POST to `/mcp` — **wrong protocol**. tradingview-mcp uses JSON-RPC over **stdin/stdout** (MCP stdio), not HTTP | Rewrote entire transport layer as `_MCPProcess` subprocess class with proper stdio piping |
| BUG-TV-3 | 🔴 HIGH | tradingview_mcp.py | `is_available()` called a `"ping"` JSON-RPC method that doesn't exist in MCP protocol | Replaced with `is_alive()` poll on the process handle after `initialize` handshake |
| BUG-TV-1 | 🟠 MED | tradingview_mcp.py | `sys.path.insert()` called inside every function — path pollution on every invocation | Moved to module top level, runs once on import |
| BUG-TV-4 | 🟠 MED | tradingview_mcp.py | `time.sleep(3)` hardcoded — Node cold start takes 5-8s, caused false `is_running()=False` | Replaced with `wait_ready(timeout_s=10)` poll loop |
| BUG-TV-7 | 🟠 MED | tradingview_mcp.py | `os.path.dirname(__file__)` without `abspath` — broke when called from different CWD | All paths now anchored to `os.path.abspath(__file__)` |
| BUG-BR-1 | 🟠 MED | bridge.py | Path dedup check used relative string — `sys.path` holds absolute version, guard always failed → repeated insertions | Changed to `os.path.abspath()` before the `in sys.path` check |
| BUG-ST-1 | 🟠 MED | setup_tradingview_mcp.py | `npm run build` with `check=True` crashed if the script didn't exist | Now reads `package.json` scripts first; gracefully handles pre-built repos |
| BUG-ST-2 | 🟡 LOW | setup_tradingview_mcp.py | Relative `__file__` for config path — failed if script not run from project root | Fixed with `os.path.abspath(__file__)` |
| BUG-TV-5 | 🟡 LOW | tradingview_mcp.py | `import json` was unused (requests handled it) | Now used by stdio serialisation in `_MCPProcess._write()` |
| BUG-TV-6 | 🟡 LOW | tradingview_mcp.py | `List` imported from typing but never used | Removed |
| BUG-TEST-1 | 🟡 LOW | test_tradingview_mcp.py | Relative `sys.path` inserts broke pytest from project root | Fixed with `os.path.abspath(_HERE)` pattern |
| BUG-TEST-2 | 🟡 LOW | test_tradingview_mcp.py | Imported private `_dispatch_task_real` (bypassed public API) | Tests now use public `handle_tradingview_task()` |

---

## Part 2 — Mother PC Setup

The **Mother PC** is your laptop that sends commands to the Worker PC.
It does **NOT** need TradingView Desktop, Node.js, or any MCP software.

### Step 1 — Prerequisites

```bash
# Python 3.10+
python3 --version

# Required pip packages
pip3 install fastapi uvicorn requests psutil cryptography \
             python-dotenv aiohttp numpy --break-system-packages
```

### Step 2 — Install AENIDA

```bash
# Copy the project to your home directory
cp -r aenida_v6_integrated ~/aenida

cd ~/aenida
```

### Step 3 — Configure Mother

Edit `core/config.json` — set your Worker PC's IP address:

```json
{
  "worker": {
    "url": "http://192.168.1.XX:8000",
    "grpc_port": 50051
  },
  "api_keys": {
    "worker_api_key": "CHANGE_ME_USE_SAME_KEY_AS_WORKER"
  }
}
```

> **Finding your Worker IP:** On the Worker PC run `ip addr` (Linux) or `ipconfig` (Windows)

### Step 4 — Start the Mother Node

```bash
cd ~/aenida/mobile
python3 mother_router.py
```

The Mother starts on port **8001** by default.

### Step 5 — Test Connection

```bash
cd ~/aenida
python3 -c "
from mobile.bridge import check_worker_online
print('Worker online:', check_worker_online())
"
```

### Step 6 — Mobile Dashboard (optional)

Open `mobile/admin_panel.html` in your phone browser at:
```
http://<mother-ip>:8001/dashboard
```

---

## Part 3 — Worker PC Setup with AENIDA USB Self-Deployment

This lets you plug a USB into any blank PC and have it join AENIDA automatically.

### Phase A — Create the USB (run on Mother PC)

```bash
# 1. Find your USB device name
lsblk

# 2. Create the deployment USB (WIPES the USB stick)
cd ~/aenida
sudo python3 tools/usb_installer.py create-usb /dev/sdX \
    http://192.168.1.XX:8000 \
    YOUR_WORKER_API_KEY

# Replace:
#   /dev/sdX          → your USB device (e.g. /dev/sdb)
#   192.168.1.XX      → your Mother PC's IP
#   YOUR_WORKER_API_KEY → same key as in Mother's config.json
```

The USB will contain:
```
/AENIDA/
  install.py          ← auto-installer
  aenida_project/     ← full AENIDA copy
  usb_config.json     ← Mother URL + API key baked in
  README.txt          ← instructions
```

### Phase B — Deploy on Worker PC

Plug the USB into the Worker PC, then:

**Linux:**
```bash
sudo python3 /media/$USER/AENIDA/install.py
```

**Windows (run PowerShell as Administrator):**
```
python E:\AENIDA\install.py
```

The installer does 6 steps automatically:
```
[1/6] Checking environment      (OS, RAM, disk, Python version)
[2/6] Installing Python packages (fastapi, uvicorn, requests, etc.)
[3/6] Copying AENIDA to /opt/aenida
[4/6] Writing worker config     (Mother URL + API key from USB)
[5/6] Installing startup service (systemd on Linux / Task Scheduler on Windows)
[6/6] Auto-enrolling with Mother (appears on dashboard)
```

After install the Worker PC will:
- Start AENIDA Worker automatically on every boot
- Appear on Mother's dashboard within 60 seconds
- Accept tasks from the Mother over the LAN

### Phase C — Add TradingView MCP to the Worker PC

After the USB install, run this **once** on the Worker PC:

```bash
cd /opt/aenida
python3 tools/setup_tradingview_mcp.py
```

This will:
1. Clone `https://github.com/mac-/tradingview-mcp`
2. Run `npm install`
3. Run `npm run build` (compiles TypeScript)
4. Update `core/config.json` with the MCP project path

### Phase D — Daily Startup on Worker PC

```bash
# Terminal 1: Start TradingView Desktop (GUI app — open manually)

# Terminal 2: TradingView MCP is auto-spawned by AENIDA
# (no manual npm start needed — AENIDA starts it on first task)

# Terminal 3: Start AENIDA Worker
cd /opt/aenida/mobile
python3 bridge.py --mode worker --port 8000
```

Or if using systemd (installed by USB installer):
```bash
# Worker starts automatically on boot
sudo systemctl status aenida_worker
sudo systemctl restart aenida_worker
```

---

## Part 4 — Sending TradingView Tasks from Mother

From the Mother PC (laptop), send these task types to the Worker:

```python
from mobile.bridge import send_task

# Analyze a chart
result = send_task({
    "type": "tradingview_analyze",
    "payload": {
        "symbol": "BTCUSDT",
        "interval": "1H",
        "question": "Is this a good entry point for a long?"
    }
})
print(result["analysis"])

# Get raw chart data
result = send_task({
    "type": "tradingview_chart_data",
    "payload": {"symbol": "ETHUSDT", "interval": "4H"}
})

# Run a Pine Script
result = send_task({
    "type": "tradingview_pine",
    "payload": {
        "action": "run",
        "script": '//@version=5\nindicator("My Script")\nplot(close)',
        "symbol": "BTCUSDT"
    }
})

# Create a price alert
result = send_task({
    "type": "tradingview_alert",
    "payload": {
        "action": "create",
        "symbol": "BTCUSDT",
        "condition": "crossing_up",
        "price": 70000.0,
        "message": "BTC hit 70k!"
    }
})

# Check MCP health
result = send_task({
    "type": "tradingview_status",
    "payload": {}
})
print(result)
```

---

## Part 5 — Troubleshooting

| Problem | Solution |
|---|---|
| `dist/index.js not found` | `cd ~/tradingview-mcp && npm run build` |
| `node not found` | Install Node.js 18+: `sudo apt install nodejs npm` |
| `MCP process not running` | Ensure TradingView Desktop is open before sending tasks |
| Worker not appearing on dashboard | Check `core/config.json` has correct Mother URL and API key |
| `checksum_mismatch` on bridge | API keys differ between Mother and Worker — must be identical |
| USB installer fails at Step 5 | Run with `sudo` on Linux, or as Administrator on Windows |
