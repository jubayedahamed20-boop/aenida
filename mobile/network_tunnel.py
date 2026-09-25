"""
AENIDA Network Tunnel Manager  v2.0
====================================
Solves the cross-network problem: Office (Mirpur 1) ↔ Home (Mirpur 12).

YOUR SETUP:
  Home laptop   = MASTER  (runs: python main.py --mode display)
  Office laptop = WORKER  (runs: python main.py --mode worker)
  Phone         = MONITOR (opens admin_panel.html)

THE PROBLEM:
  They are on different LAN networks — they can't see each other directly.

THE FIX (pick ONE):
  Strategy A — Tailscale  (BEST, free, permanent, installs once)
  Strategy B — Cloudflare Tunnel  (no install, works instantly, free)
  Strategy C — ngrok  (easiest, free tier available)
  Strategy D — LocalTunnel  (simplest, no signup)
  Strategy E — Telebit  (persistent URL, free tier)

MOBILE ACCESS WITHOUT LAN:
  These methods let you access AENIDA from your phone using mobile data
  (no need to be on the same WiFi as your home laptop!)

USAGE:
  python network_tunnel.py --setup           # Step-by-step wizard
  python network_tunnel.py --status          # Check who can see who
  python network_tunnel.py --start-cf        # Start Cloudflare tunnel (run on WORKER)
  python network_tunnel.py --start-ngrok     # Start ngrok tunnel
  python network_tunnel.py --start-lt        # Start LocalTunnel
  python network_tunnel.py --tailscale       # Show Tailscale setup steps
  python network_tunnel.py --set-worker URL  # Manually set worker URL in config.json
  python network_tunnel.py --test-worker     # Ping the worker to see if it's alive
  python network_tunnel.py --mobile-access   # Setup for mobile data access
"""

import json
import os
import subprocess
import sys
import time
import socket
import threading
import argparse
import logging
import urllib.request
import urllib.error
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime

# Setup logging
log = logging.getLogger("network_tunnel")
logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")
TUNNEL_STATE = os.path.join(PROJECT_ROOT, "data", "tunnel_state.json")
TUNNEL_LOG = os.path.join(PROJECT_ROOT, "data", "tunnel_log.jsonl")

os.makedirs(os.path.dirname(TUNNEL_STATE), exist_ok=True)


# ═════════════════════════════════════════════════════════════════════════════
# CONFIG HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def _load_config() -> dict:
    """Load config with error handling."""
    try:
        with open(CONFIG_PATH, encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        log.warning("config.json not found, creating default")
        default_cfg = {"worker": {"url": "http://localhost:8000"}}
        _save_config(default_cfg)
        return default_cfg
    except json.JSONDecodeError as e:
        log.error(f"Invalid config.json: {e}")
        return {"worker": {"url": "http://localhost:8000"}}
    except Exception as e:
        log.error(f"Error loading config: {e}")
        return {"worker": {"url": "http://localhost:8000"}}


def _save_config(cfg: dict) -> None:
    """Save config with error handling."""
    try:
        with open(CONFIG_PATH, "w", encoding='utf-8') as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        log.error(f"Error saving config: {e}")


def _get_worker_url() -> str:
    """Get configured worker URL."""
    cfg = _load_config()
    return cfg.get("worker", {}).get("url", "http://localhost:8000")


def _set_worker_url(url: str) -> None:
    """Set worker URL in config."""
    cfg = _load_config()
    if "worker" not in cfg:
        cfg["worker"] = {}
    cfg["worker"]["url"] = url.rstrip("/")
    _save_config(cfg)
    print(f"\n✅  Worker URL saved to config.json: {url}")
    print(f"   Test connection: python network_tunnel.py --test-worker")


def _log_tunnel_event(event: Dict[str, Any]) -> None:
    """Log tunnel events for debugging."""
    event["timestamp"] = datetime.now().isoformat()
    try:
        with open(TUNNEL_LOG, "a", encoding='utf-8') as f:
            f.write(json.dumps(event) + "\n")
    except Exception:
        pass


# ═════════════════════════════════════════════════════════════════════════════
# NETWORK UTILS
# ═════════════════════════════════════════════════════════════════════════════

def get_local_ip() -> str:
    """Get this machine's LAN IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def get_public_ip() -> Optional[str]:
    """Get public IP address."""
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=10) as r:
            return r.read().decode().strip()
    except Exception:
        return None


def ping_url(url: str, timeout: int = 5) -> bool:
    """HTTP GET to /health — returns True if worker replies."""
    try:
        health_url = url.rstrip("/") + "/health"
        req = urllib.request.Request(health_url, headers={'User-Agent': 'AENIDA-NetworkCheck/2.0'})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def check_port_open(host: str, port: int, timeout: int = 3) -> bool:
    """Check if a port is open on a host."""
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False


def test_internet_connection() -> bool:
    """Test if we have internet access."""
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=3)
        return True
    except Exception:
        return False


# ═════════════════════════════════════════════════════════════════════════════
# TAILSCALE
# ═════════════════════════════════════════════════════════════════════════════

def check_tailscale_installed() -> bool:
    """Check if Tailscale is installed."""
    try:
        result = subprocess.run(["tailscale", "status"], capture_output=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


def get_tailscale_ip() -> Optional[str]:
    """Get Tailscale IP address."""
    try:
        result = subprocess.run(
            ["tailscale", "ip", "--4"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def get_tailscale_status() -> Dict[str, Any]:
    """Get detailed Tailscale status."""
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"], 
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except Exception:
        pass
    return {}


def show_tailscale_guide() -> None:
    """Show Tailscale setup guide."""
    my_ip = get_local_ip()
    ts_installed = check_tailscale_installed()
    ts_ip = get_tailscale_ip() if ts_installed else None
    
    print("""
╔══════════════════════════════════════════════════════════════════════╗
║  STRATEGY A — Tailscale (Recommended, Free, Permanent)              ║
╚══════════════════════════════════════════════════════════════════════╝

Tailscale creates a secure private network between your machines.
After setup, your home laptop and office laptop talk directly
no matter which internet connection you're on.

STEP 1: Install Tailscale on BOTH machines
──────────────────────────────────────────
  Linux / Kali:
    curl -fsSL https://tailscale.com/install.sh | sh

  Windows:
    Download from: https://tailscale.com/download/windows

  Mac:
    brew install tailscale
    OR download from: https://tailscale.com/download/mac

STEP 2: Login (same account on both machines)
──────────────────────────────────────────────
  sudo tailscale up
  → Opens a browser → login with Google/GitHub/email

STEP 3: Find your machine IPs
──────────────────────────────
  tailscale status
  → Shows: 100.x.x.x  your-machine-name  owner
""")
    
    if ts_installed and ts_ip:
        print(f"  ✅  Tailscale is running on THIS machine!")
        print(f"  Your Tailscale IP: {ts_ip}")
        print(f"\n  On the WORKER machine: run  tailscale ip --4")
        print(f"  Then run on THIS machine:")
        print(f"    python network_tunnel.py --set-worker http://WORKER_TAILSCALE_IP:8000")
    elif ts_installed:
        print(f"  ⚠  Tailscale installed but not running. Run: sudo tailscale up")
    else:
        print(f"  ⚠  Tailscale not installed on this machine (LAN IP: {my_ip})")
    
    print("""
STEP 4: Tell AENIDA where the worker is
─────────────────────────────────────────
  python network_tunnel.py --set-worker http://100.X.X.X:8000
  (replace 100.X.X.X with the worker's Tailscale IP)

STEP 5: Start AENIDA normally
──────────────────────────────
  Home (master):   python main.py --mode display
  Office (worker): python main.py --mode worker
  Phone:           Open the admin panel URL

That's it. Tailscale handles everything automatically.
""")


# ═════════════════════════════════════════════════════════════════════════════
# CLOUDFLARE TUNNEL
# ═════════════════════════════════════════════════════════════════════════════

def check_cloudflared_installed() -> bool:
    """Check if cloudflared is installed."""
    try:
        result = subprocess.run(["cloudflared", "--version"], capture_output=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


def install_cloudflared_guide() -> None:
    """Show installation instructions for cloudflared."""
    print("""
Installing cloudflared (Cloudflare Tunnel client):

  Linux / Kali (64-bit):
    wget -O cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
    sudo dpkg -i cloudflared.deb

  Linux / Kali (ARM64 for Raspberry Pi):
    wget -O cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb
    sudo dpkg -i cloudflared.deb

  Windows:
    Download from: https://github.com/cloudflare/cloudflared/releases/latest
    → cloudflared-windows-amd64.exe  (rename to cloudflared.exe, add to PATH)

  Mac:
    brew install cloudflare/cloudflare/cloudflared
""")


def start_cloudflare_tunnel(port: int = 8000, save_url: bool = True) -> None:
    """
    Run on the WORKER machine (office laptop).
    Creates a public HTTPS URL pointing to localhost:8000.
    """
    if not check_cloudflared_installed():
        print("\n❌  cloudflared not installed on this machine.")
        install_cloudflared_guide()
        return
    
    print(f"""
╔══════════════════════════════════════════════════════════════════════╗
║  STRATEGY B — Cloudflare Tunnel (No VPN, Works Instantly)           ║
╚══════════════════════════════════════════════════════════════════════╝

Run this on the WORKER machine (Office, Mirpur 1).
It will create a public URL that your home laptop can reach.

Starting tunnel on port {port}...
(Press Ctrl+C to stop)
""")
    
    state_dir = os.path.join(PROJECT_ROOT, "data")
    os.makedirs(state_dir, exist_ok=True)
    url_file = os.path.join(state_dir, "cf_tunnel_url.txt")
    
    print("⏳  Starting Cloudflare tunnel...")
    _log_tunnel_event({"action": "cf_tunnel_start", "port": port})
    
    proc = subprocess.Popen(
        ["cloudflared", "tunnel", "--url", f"http://localhost:{port}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    
    tunnel_url = None
    try:
        for line in proc.stdout:
            line = line.strip()
            if "trycloudflare.com" in line or ".cloudflare.com" in line:
                # Parse URL from log line
                for word in line.split():
                    if "https://" in word and ("cloudflare" in word or "trycloudflare" in word):
                        tunnel_url = word.strip("|")
                        break
            if tunnel_url:
                print(f"\n{'═'*62}")
                print(f"  ✅  TUNNEL ACTIVE!")
                print(f"  Worker URL: {tunnel_url}")
                print(f"{'═'*62}")
                print(f"\n  👉  Now on your HOME laptop, run:")
                print(f"      python network_tunnel.py --set-worker {tunnel_url}")
                print(f"\n  Keep this window open while you work.\n")
                
                if save_url:
                    with open(url_file, "w", encoding='utf-8') as f:
                        f.write(tunnel_url)
                
                _log_tunnel_event({"action": "cf_tunnel_active", "url": tunnel_url})
                break
            else:
                print(f"  {line}", flush=True)
        
        # Keep running
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        print("\n🛑  Tunnel stopped.")
        _log_tunnel_event({"action": "cf_tunnel_stop"})


# ═════════════════════════════════════════════════════════════════════════════
# NGROK
# ═════════════════════════════════════════════════════════════════════════════

def check_ngrok_installed() -> bool:
    """Check if ngrok is installed."""
    try:
        result = subprocess.run(["ngrok", "version"], capture_output=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


def install_ngrok_guide() -> None:
    """Show ngrok installation instructions."""
    print("""
Installing ngrok:

  1. Sign up at: https://ngrok.com
  2. Get your authtoken from the dashboard
  3. Install:

  Linux:
    curl -s https://ngrok-agent.s3.amazonaws.com/ngrok.asc | sudo tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null
    echo "deb https://ngrok-agent.s3.amazonaws.com buster main" | sudo tee /etc/apt/sources.list.d/ngrok.list
    sudo apt update && sudo apt install ngrok
    ngrok config add-authtoken YOUR_TOKEN

  Windows:
    choco install ngrok
    ngrok config add-authtoken YOUR_TOKEN

  Mac:
    brew install ngrok/ngrok/ngrok
    ngrok config add-authtoken YOUR_TOKEN
""")


def start_ngrok_tunnel(port: int = 8000, region: str = "us") -> None:
    """Start ngrok tunnel."""
    if not check_ngrok_installed():
        print("\n❌  ngrok not installed.")
        install_ngrok_guide()
        return
    
    print(f"""
╔══════════════════════════════════════════════════════════════════════╗
║  STRATEGY C — ngrok (Easy Setup, Free Tier)                         ║
╚══════════════════════════════════════════════════════════════════════╝

Starting ngrok tunnel on port {port}...
(Press Ctrl+C to stop)
""")
    
    _log_tunnel_event({"action": "ngrok_tunnel_start", "port": port})
    
    try:
        proc = subprocess.Popen(
            ["ngrok", "http", str(port), "--region", region],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        
        print("⏳  Starting ngrok...")
        print("  📱  Open https://dashboard.ngrok.com/cloud-edge/endpoints to see your URL")
        print("  (ngrok free tier doesn't show URL in terminal)\n")
        
        # Print output
        for line in proc.stdout:
            print(f"  {line.strip()}")
            
    except KeyboardInterrupt:
        proc.terminate()
        print("\n🛑  Tunnel stopped.")
        _log_tunnel_event({"action": "ngrok_tunnel_stop"})


# ═════════════════════════════════════════════════════════════════════════════
# LOCALTUNNEL
# ═════════════════════════════════════════════════════════════════════════════

def check_localtunnel_installed() -> bool:
    """Check if localtunnel is installed."""
    try:
        result = subprocess.run(["npx", "localtunnel", "--version"], capture_output=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


def start_localtunnel(port: int = 8000) -> None:
    """Start LocalTunnel (simplest option, no signup)."""
    print(f"""
╔══════════════════════════════════════════════════════════════════════╗
║  STRATEGY D — LocalTunnel (Simplest, No Signup)                     ║
╚══════════════════════════════════════════════════════════════════════╝

Starting LocalTunnel on port {port}...
(Press Ctrl+C to stop)

Note: Requires Node.js/npm to be installed.
""")
    
    _log_tunnel_event({"action": "lt_tunnel_start", "port": port})
    
    try:
        proc = subprocess.Popen(
            ["npx", "localtunnel", "--port", str(port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        
        url = None
        for line in proc.stdout:
            line = line.strip()
            print(f"  {line}")
            
            # Extract URL
            if "your url is:" in line.lower():
                parts = line.split(":")
                if len(parts) >= 2:
                    url = parts[-1].strip()
            elif "https://" in line and "loca.lt" in line:
                for word in line.split():
                    if "https://" in word and "loca.lt" in word:
                        url = word
                        break
            
            if url and "loca.lt" in url:
                print(f"\n{'═'*62}")
                print(f"  ✅  TUNNEL ACTIVE!")
                print(f"  URL: {url}")
                print(f"{'═'*62}")
                print(f"\n  👉  Use this URL on your phone or home laptop")
                _log_tunnel_event({"action": "lt_tunnel_active", "url": url})
        
        proc.wait()
        
    except KeyboardInterrupt:
        proc.terminate()
        print("\n🛑  Tunnel stopped.")
        _log_tunnel_event({"action": "lt_tunnel_stop"})
    except FileNotFoundError:
        print("\n❌  npx not found. Please install Node.js first:")
        print("    https://nodejs.org/")


# ═════════════════════════════════════════════════════════════════════════════
# MOBILE ACCESS SETUP
# ═════════════════════════════════════════════════════════════════════════════

def setup_mobile_access() -> None:
    """Guide for setting up mobile data access."""
    print("""
╔══════════════════════════════════════════════════════════════════════╗
║  📱 MOBILE ACCESS SETUP (No LAN Required)                           ║
╚══════════════════════════════════════════════════════════════════════╝

These methods let you access AENIDA from your phone using MOBILE DATA
(no need to be on the same WiFi as your home laptop!)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
METHOD 1: Cloudflare Tunnel (Recommended)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

On your HOME laptop (the one running main.py):

  1. Install cloudflared:
     curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o cloudflared.deb
     sudo dpkg -i cloudflared.deb

  2. Start the tunnel:
     python network_tunnel.py --start-cf --port 5000

  3. Copy the HTTPS URL shown

  4. Open that URL on your phone (works on mobile data!)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
METHOD 2: ngrok
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  1. Sign up: https://ngrok.com
  2. Install: python network_tunnel.py --install-ngrok
  3. Start:   python network_tunnel.py --start-ngrok --port 5000
  4. Check dashboard for your URL

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
METHOD 3: LocalTunnel (Easiest, No Signup)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  1. Install Node.js: https://nodejs.org/
  2. Start: python network_tunnel.py --start-lt --port 5000
  3. Copy the URL shown
  4. Open on your phone

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
METHOD 4: Telebit (Persistent URL)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  1. Install: curl https://get.telebit.io/ | bash
  2. Run: telebit http 5000
  3. Get a persistent URL that doesn't change!

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
QUICK START (Copy & Paste):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""")
    
    # Show current status
    print("\n📊 Current Status:")
    print(f"  Your LAN IP: {get_local_ip()}")
    
    public_ip = get_public_ip()
    if public_ip:
        print(f"  Your Public IP: {public_ip}")
    else:
        print(f"  Your Public IP: (could not determine)")
    
    print(f"\n  Internet: {'✅ Connected' if test_internet_connection() else '❌ No connection'}")
    
    # Check installed tools
    print("\n🔧 Installed Tools:")
    print(f"  Tailscale:   {'✅' if check_tailscale_installed() else '❌'}")
    print(f"  Cloudflared: {'✅' if check_cloudflared_installed() else '❌'}")
    print(f"  ngrok:       {'✅' if check_ngrok_installed() else '❌'}")
    print(f"  LocalTunnel: {'✅' if check_localtunnel_installed() else '❌'}")
    
    print("""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

💡 RECOMMENDED WORKFLOW:

  1. On HOME laptop:
     python main.py --mode display
     (in another terminal)
     python network_tunnel.py --start-cf --port 5000

  2. Copy the HTTPS URL

  3. On your PHONE (using mobile data):
     Open the URL in any browser

  4. Done! You can now monitor AENIDA from anywhere.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""")


# ═════════════════════════════════════════════════════════════════════════════
# STATUS CHECK
# ═════════════════════════════════════════════════════════════════════════════

def show_status() -> None:
    """Show comprehensive network status."""
    my_ip = get_local_ip()
    public_ip = get_public_ip()
    worker_url = _get_worker_url()
    worker_alive = ping_url(worker_url)
    ts_installed = check_tailscale_installed()
    ts_ip = get_tailscale_ip() if ts_installed else None
    
    # Check saved tunnel URLs
    cf_url = None
    url_file = os.path.join(PROJECT_ROOT, "data", "cf_tunnel_url.txt")
    if os.path.exists(url_file):
        with open(url_file, encoding='utf-8') as f:
            cf_url = f.read().strip()
    
    print(f"""
╔══════════════════════════════════════════════════════════════════════╗
║              AENIDA Network Status v2.0                             ║
╚══════════════════════════════════════════════════════════════════════╝

  🖥️  This Machine
  ─────────────────────────────────────────────────────────────────────
  LAN IP:              {my_ip}
  Public IP:           {public_ip or '(could not determine)'}
  Internet:            {'✅ Connected' if test_internet_connection() else '❌ No connection'}

  🔗 Worker Connection
  ─────────────────────────────────────────────────────────────────────
  Configured URL:      {worker_url}
  Worker Status:       {'✅ ONLINE' if worker_alive else '❌ OFFLINE (worker offline or wrong URL)'}

  🔧 Tunnel Tools
  ─────────────────────────────────────────────────────────────────────
  Tailscale:           {'✅ Running — IP: ' + ts_ip if ts_ip else ('⚠️  Installed but not running' if ts_installed else '❌ Not installed')}
  Cloudflare Tunnel:   {cf_url or '(none saved)'}
  ngrok:               {'✅ Installed' if check_ngrok_installed() else '❌ Not installed'}
  LocalTunnel:         {'✅ Available' if check_localtunnel_installed() else '❌ Not installed'}
""")
    
    if not worker_alive:
        print("""  🔧 FIXES:
  ─────────────────────────────────────────────────────────────────────
  1. Is the worker running? (python main.py --mode worker)
  2. Run: python network_tunnel.py --tailscale   (to set up Tailscale)
  3. Or run on worker: python network_tunnel.py --start-cf
  4. For mobile access: python network_tunnel.py --mobile-access
""")
    else:
        print("  ✅  Everything looks good! Master and worker are connected.\n")


# ═════════════════════════════════════════════════════════════════════════════
# SETUP WIZARD
# ═════════════════════════════════════════════════════════════════════════════

def setup_wizard() -> None:
    """Interactive setup wizard."""
    print("""
╔══════════════════════════════════════════════════════════════════════╗
║         AENIDA Network Setup Wizard v2.0                            ║
╚══════════════════════════════════════════════════════════════════════╝

YOUR SITUATION:
  Home (Mirpur 12)   = Master laptop   → runs the brain
  Office (Mirpur 1)  = Worker laptop   → does the heavy tasks
  Phone              = Monitor         → watches status

Which machine are you on right now?
  1. Home laptop (Master)
  2. Office laptop (Worker)  
  3. Just want to see status
  4. Setup mobile access (phone on mobile data)
""")
    
    choice = input("Enter 1, 2, 3, or 4: ").strip()
    
    if choice == "1":
        print("\nYou are on the HOME (Master) machine.")
        print("\nRecommended: Set up Tailscale for permanent connection.")
        show_tailscale_guide()
    elif choice == "2":
        print("\nYou are on the OFFICE (Worker) machine.")
        print("\nYou need to expose this machine so the home laptop can reach it.")
        print("\nQuick options:")
        print("  1. Cloudflare Tunnel (easiest, no account)")
        print("  2. ngrok (requires signup)")
        print("  3. LocalTunnel (simplest, no signup)")
        
        tunnel_choice = input("\nChoose (1-3): ").strip()
        if tunnel_choice == "1":
            start_cloudflare_tunnel()
        elif tunnel_choice == "2":
            start_ngrok_tunnel()
        elif tunnel_choice == "3":
            start_localtunnel()
    elif choice == "3":
        show_status()
    elif choice == "4":
        setup_mobile_access()
    else:
        print("Invalid choice.")


# ═════════════════════════════════════════════════════════════════════════════
# TEST WORKER
# ═════════════════════════════════════════════════════════════════════════════

def test_worker() -> None:
    """Test connection to worker."""
    worker_url = _get_worker_url()
    print(f"\nTesting worker at: {worker_url}")
    print("Pinging /health endpoint...", end=" ", flush=True)
    
    alive = ping_url(worker_url, timeout=10)
    
    if alive:
        print("✅  Worker is alive and responding!")
        _log_tunnel_event({"action": "worker_test", "url": worker_url, "success": True})
    else:
        print("❌  Worker not reachable.")
        print("\nTroubleshooting:")
        print(f"  1. Worker is running: python main.py --mode worker")
        print(f"  2. Worker URL is correct: {worker_url}")
        print(f"  3. Network path exists (Tailscale or Cloudflare tunnel)")
        print(f"\nRun: python network_tunnel.py --status  for full diagnostics")
        print(f"     python network_tunnel.py --mobile-access  for phone setup")
        _log_tunnel_event({"action": "worker_test", "url": worker_url, "success": False})


# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="AENIDA Network Tunnel Manager v2.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --setup                    # Interactive wizard
  %(prog)s --status                   # Show network status
  %(prog)s --tailscale                # Tailscale setup guide
  %(prog)s --start-cf --port 8000     # Start Cloudflare tunnel
  %(prog)s --start-ngrok              # Start ngrok tunnel
  %(prog)s --start-lt                 # Start LocalTunnel
  %(prog)s --mobile-access            # Mobile data access setup
  %(prog)s --set-worker URL           # Set worker URL
  %(prog)s --test-worker              # Test worker connection
        """
    )
    parser.add_argument("--setup", action="store_true", help="Step-by-step wizard")
    parser.add_argument("--status", action="store_true", help="Show network status")
    parser.add_argument("--tailscale", action="store_true", help="Tailscale setup guide")
    parser.add_argument("--start-cf", action="store_true", help="Start Cloudflare tunnel (run on worker)")
    parser.add_argument("--start-ngrok", action="store_true", help="Start ngrok tunnel")
    parser.add_argument("--start-lt", action="store_true", help="Start LocalTunnel")
    parser.add_argument("--set-worker", metavar="URL", help="Set worker URL in config")
    parser.add_argument("--test-worker", action="store_true", help="Ping worker to test connection")
    parser.add_argument("--mobile-access", action="store_true", help="Setup for mobile data access")
    parser.add_argument("--port", type=int, default=8000, help="Port to tunnel (default: 8000)")
    parser.add_argument("--region", default="us", help="ngrok region (default: us)")
    parser.add_argument("--version", action="version", version="%(prog)s 2.0")
    args = parser.parse_args()
    
    try:
        if args.set_worker:
            _set_worker_url(args.set_worker)
        elif args.setup:
            setup_wizard()
        elif args.status:
            show_status()
        elif args.tailscale:
            show_tailscale_guide()
        elif args.start_cf:
            start_cloudflare_tunnel(port=args.port)
        elif args.start_ngrok:
            start_ngrok_tunnel(port=args.port, region=args.region)
        elif args.start_lt:
            start_localtunnel(port=args.port)
        elif args.mobile_access:
            setup_mobile_access()
        elif args.test_worker:
            test_worker()
        else:
            show_status()
            print("\nRun with --setup for the full wizard.")
            print("Run with --mobile-access for phone setup help.")
    except KeyboardInterrupt:
        print("\n\n🛑  Operation cancelled.")
        sys.exit(130)


if __name__ == "__main__":
    main()
