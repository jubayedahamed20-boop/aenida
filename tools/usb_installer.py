"""
AENIDA USB Auto-Installer  v2.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Plug this USB into any blank PC → run one command →
that PC joins the AENIDA supercomputer, auto-enrolls
with the Mother, and appears on the dashboard.

Zero manual setup on the worker PC after the USB is created.

HOW TO CREATE THE USB (run once on Mother PC)
─────────────────────────────────────────────
    python tools/usb_installer.py create-usb /dev/sdX

    This will:
      1. Format the USB (FAT32, labelled AENIDA)
      2. Copy the entire aenida_v3 project onto it
      3. Bake the Mother's URL and API key into the USB config
      4. Write autorun helpers for both Linux and Windows
      5. Print the command the new worker should run

HOW TO DEPLOY ON A BLANK WORKER PC
───────────────────────────────────
  Linux (Debian/Ubuntu/Kali/Arch/Fedora):
      sudo python /media/$USER/AENIDA/install.py

  Windows (run as Administrator):
      python E:\\install.py

  What happens automatically:
      [1/6] Environment detected
      [2/6] Python packages installed
      [3/6] AENIDA copied to /opt/aenida (or C:\\aenida)
      [4/6] Config written (Mother URL, API key, node name)
      [5/6] Systemd / Task Scheduler service created + started
      [6/6] Auto-enrolled with Mother — appears on dashboard

STRUCTURE ON THE USB
─────────────────────
    /AENIDA/
      install.py            ← this file
      aenida_project/       ← full project copy
      usb_config.json       ← baked-in Mother URL + API key
      autorun.inf           ← Windows autorun hint
      README.txt            ← plain English instructions
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import getpass
import json
import logging
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("usb_installer")

# ── Paths ─────────────────────────────────────────────────────────
_BASE        = Path(__file__).parent.parent.resolve()   # aenida_v3/
INSTALL_DIR  = Path("/opt/aenida")        # Linux default
INSTALL_WIN  = Path("C:/aenida")          # Windows default
SERVICE_NAME = "aenida_worker"
USB_LABEL    = "AENIDA"
USB_CONFIG   = "usb_config.json"          # baked config on USB root

# ── Python packages required on worker ────────────────────────────
REQUIRED_PACKAGES = [
    "fastapi", "uvicorn", "httpx", "websockets",
    "psutil", "requests", "cryptography",
    "python-dotenv", "aiohttp", "numpy",
]
OPTIONAL_PACKAGES = [
    "pandas", "ccxt", "groq", "google-generativeai",
]


# ════════════════════════════════════════════════════════════════════
#  STEP PRINTER  (colourful progress on any terminal)
# ════════════════════════════════════════════════════════════════════

class Progress:
    BOLD  = "\033[1m"
    GREEN = "\033[92m"
    GOLD  = "\033[93m"
    RED   = "\033[91m"
    CYAN  = "\033[96m"
    RESET = "\033[0m"

    def __init__(self, total_steps: int):
        self.total = total_steps
        self.current = 0

    def step(self, title: str) -> None:
        self.current += 1
        bar = f"[{self.current}/{self.total}]"
        print(f"\n{self.BOLD}{self.CYAN}{bar}{self.RESET} {self.BOLD}{title}{self.RESET}")

    def ok(self, msg: str) -> None:
        print(f"  {self.GREEN}✓{self.RESET} {msg}")

    def warn(self, msg: str) -> None:
        print(f"  {self.GOLD}⚠{self.RESET} {msg}")

    def fail(self, msg: str) -> None:
        print(f"  {self.RED}✗{self.RESET} {msg}")

    def info(self, msg: str) -> None:
        print(f"    {msg}")


# ════════════════════════════════════════════════════════════════════
#  ENVIRONMENT DETECTION
# ════════════════════════════════════════════════════════════════════

def detect_environment() -> Dict:
    """Detect OS, distro, RAM, disk, Python version."""
    env = {
        "os":       platform.system(),   # "Linux" | "Windows" | "Darwin"
        "distro":   "",
        "python":   platform.python_version(),
        "hostname": socket.gethostname(),
        "ram_gb":   0.0,
        "disk_gb":  0.0,
        "is_root":  (os.geteuid() == 0) if hasattr(os, "geteuid") else False,
        "is_admin": False,
    }

    # Linux distro
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("ID="):
                    env["distro"] = line.split("=")[1].strip().strip('"').lower()
                    break
    except FileNotFoundError:
        pass

    # Windows admin check
    if env["os"] == "Windows":
        try:
            import ctypes
            env["is_admin"] = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            pass

    # RAM and disk via psutil
    try:
        import psutil
        env["ram_gb"]  = round(psutil.virtual_memory().total / 1e9, 1)
        env["disk_gb"] = round(psutil.disk_usage("/").free / 1e9, 1)
    except ImportError:
        pass

    return env


def check_prerequisites(env: Dict, p: Progress) -> bool:
    """Verify the target PC meets minimum requirements."""
    ok = True
    if env["ram_gb"] > 0 and env["ram_gb"] < 2.0:
        p.warn(f"Low RAM: {env['ram_gb']} GB (recommend ≥ 2 GB)")
    if env["disk_gb"] > 0 and env["disk_gb"] < 5.0:
        p.fail(f"Insufficient disk: {env['disk_gb']} GB free (need ≥ 5 GB)")
        ok = False
    if env["os"] == "Linux" and not env["is_root"]:
        p.warn("Not running as root — systemd service install may fail")
    if env["os"] == "Windows" and not env["is_admin"]:
        p.warn("Not running as Administrator — service install may fail")
    return ok


# ════════════════════════════════════════════════════════════════════
#  COMMAND RUNNER
# ════════════════════════════════════════════════════════════════════

def run(cmd: str, desc: str = "", capture: bool = True,
        timeout: int = 120) -> Tuple[bool, str]:
    """Run a shell command.  Returns (success, stdout+stderr)."""
    if desc:
        log.debug(f"  → {desc}")
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=capture,
            text=True, timeout=timeout)
        output = (r.stdout or "") + (r.stderr or "")
        return r.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, f"Timeout after {timeout}s"
    except Exception as e:
        return False, str(e)


def run_ok(cmd: str, desc: str = "") -> bool:
    ok, _ = run(cmd, desc)
    return ok


# ════════════════════════════════════════════════════════════════════
#  STEP 1 — INSTALL SYSTEM PACKAGES
# ════════════════════════════════════════════════════════════════════

def install_system_packages(env: Dict, p: Progress) -> bool:
    distro = env.get("distro", "")

    pkg_cmds = {
        ("ubuntu", "debian", "kali", "linuxmint", "pop"):
            "apt-get update -qq && apt-get install -y -qq python3 python3-pip curl git",
        ("arch", "manjaro", "endeavouros"):
            "pacman -Sy --noconfirm python python-pip curl git",
        ("fedora", "rhel", "centos", "rocky"):
            "dnf install -y python3 python3-pip curl git",
    }

    installed_sys = False
    for distros, cmd in pkg_cmds.items():
        if distro in distros:
            ok, out = run(cmd, f"system packages ({distro})")
            if ok:
                p.ok("System packages installed")
            else:
                p.warn(f"System package install issue: {out[:120]}")
            installed_sys = True
            break

    if not installed_sys and env["os"] == "Linux":
        p.warn(f"Unknown distro '{distro}' — skipping system packages")

    # Always try pip
    pip_ok = True
    for pkg in REQUIRED_PACKAGES:
        ok, _ = run(
            f"pip3 install {pkg} --quiet --break-system-packages",
            f"pip install {pkg}")
        if not ok:
            # try without flag
            ok, _ = run(f"pip3 install {pkg} --quiet")
        if ok:
            p.ok(f"  {pkg}")
        else:
            p.warn(f"  {pkg} — could not install (will try at runtime)")
            pip_ok = False

    return pip_ok


# ════════════════════════════════════════════════════════════════════
#  STEP 2 — COPY AENIDA FILES
# ════════════════════════════════════════════════════════════════════

def install_aenida_files(source_dir: Path, install_dir: Path,
                         p: Progress) -> bool:
    """Copy the project from USB to the install directory."""
    try:
        install_dir.mkdir(parents=True, exist_ok=True)

        # Copy everything from source
        for item in source_dir.iterdir():
            dest = install_dir / item.name
            if item.is_dir():
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)

        # Ensure data directories exist
        for d in ["data", "logs", "modules/temp",
                  "modules/permanent", "exports", "knowledge"]:
            (install_dir / d).mkdir(parents=True, exist_ok=True)

        p.ok(f"Files installed to {install_dir}")
        return True
    except Exception as e:
        p.fail(f"File copy failed: {e}")
        return False


# ════════════════════════════════════════════════════════════════════
#  STEP 3 — BAKE CONFIG  (Mother URL + API key + node identity)
# ════════════════════════════════════════════════════════════════════

def write_worker_config(install_dir: Path, usb_config: Dict,
                        node_name: str, p: Progress) -> bool:
    """
    Merge the USB config (Mother URL, API key) with the project's
    config.json to create a fully configured worker.
    """
    config_path = install_dir / "core" / "config.json"

    # Load existing config if present
    existing = {}
    if config_path.exists():
        try:
            with open(config_path) as f:
                existing = json.load(f)
        except Exception:
            pass

    # Inject worker identity and Mother connection
    mother_url = usb_config.get("mother_url", "http://localhost:8000")
    api_key    = usb_config.get("worker_api_key", "")

    existing.setdefault("worker", {})
    existing["worker"]["url"]          = mother_url
    existing["worker"]["node_id"]      = node_name
    existing["worker"]["role"]         = "worker"
    existing["worker"]["auto_enrolled"]= True

    existing.setdefault("api_keys", {})
    existing["api_keys"]["worker_api_key"] = api_key

    # Carry over any extra keys baked into the USB config
    for k, v in usb_config.items():
        if k not in ("mother_url", "worker_api_key"):
            existing["api_keys"][k] = v

    try:
        with open(config_path, "w") as f:
            json.dump(existing, f, indent=2)
        p.ok(f"Config written — Mother: {mother_url} / Node: {node_name}")
        return True
    except Exception as e:
        p.fail(f"Config write failed: {e}")
        return False


# ════════════════════════════════════════════════════════════════════
#  STEP 4 — SYSTEMD SERVICE (Linux)
# ════════════════════════════════════════════════════════════════════

def install_systemd_service(install_dir: Path, p: Progress) -> bool:
    svc_content = f"""[Unit]
Description=AENIDA Worker Node — Virtual Supercomputer
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory={install_dir}
ExecStart=/usr/bin/python3 {install_dir}/tools/smart_worker.py
Restart=always
RestartSec=15
StandardOutput=append:{install_dir}/logs/service.log
StandardError=append:{install_dir}/logs/service.log
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
"""
    svc_path = Path(f"/etc/systemd/system/{SERVICE_NAME}.service")
    try:
        svc_path.write_text(svc_content)
        run_ok("systemctl daemon-reload",      "daemon-reload")
        run_ok(f"systemctl enable {SERVICE_NAME}", "enable service")
        run_ok(f"systemctl start  {SERVICE_NAME}", "start service")
        time.sleep(2)
        ok, _ = run(f"systemctl is-active {SERVICE_NAME}")
        if ok:
            p.ok(f"Service '{SERVICE_NAME}' active and running")
        else:
            p.warn(f"Service installed but not yet active — check: "
                   f"journalctl -u {SERVICE_NAME}")
        return True
    except PermissionError:
        p.warn("Root required for systemd — run: sudo python3 install.py")
        return False
    except Exception as e:
        p.fail(f"Service install failed: {e}")
        return False


# ════════════════════════════════════════════════════════════════════
#  STEP 4 (Windows) — TASK SCHEDULER
# ════════════════════════════════════════════════════════════════════

def install_windows_task(install_dir: Path, p: Progress) -> bool:
    py_exe  = sys.executable
    script  = install_dir / "tools" / "smart_worker.py"
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>AENIDA Worker Node — Virtual Supercomputer</Description>
  </RegistrationInfo>
  <Triggers>
    <BootTrigger><Enabled>true</Enabled></BootTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartInterval>PT1M</RestartInterval>
    <RestartCount>999</RestartCount>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
  </Settings>
  <Actions>
    <Exec>
      <Command>{py_exe}</Command>
      <Arguments>{script}</Arguments>
      <WorkingDirectory>{install_dir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>"""
    xml_path = install_dir / "aenida_task.xml"
    try:
        xml_path.write_text(xml, encoding="utf-16")
        ok, out = run(
            f'schtasks /Create /XML "{xml_path}" /TN AENIDA_Worker /F',
            "register Task Scheduler task")
        if ok:
            run_ok('schtasks /Run /TN AENIDA_Worker', "start task now")
            p.ok("Windows Task Scheduler task created and started")
        else:
            p.warn(f"Task Scheduler: {out[:120]}")
        return ok
    except Exception as e:
        p.fail(f"Windows task failed: {e}")
        return False


# ════════════════════════════════════════════════════════════════════
#  STEP 5 — AUTO-ENROLL WITH MOTHER
# ════════════════════════════════════════════════════════════════════

def enroll_with_mother(install_dir: Path, usb_config: Dict,
                       node_name: str, p: Progress) -> bool:
    """
    POST a registration packet to the Mother's /api/worker/enroll endpoint.
    If this fails (Mother offline, network not ready), the smart_worker
    will register automatically on its first successful heartbeat.
    """
    mother_url = usb_config.get("mother_url", "")
    api_key    = usb_config.get("worker_api_key", "")

    if not mother_url:
        p.warn("No Mother URL in USB config — auto-enroll skipped")
        p.info("Worker will self-enroll on first heartbeat")
        return True

    payload = {
        "node_id":    node_name,
        "hostname":   socket.gethostname(),
        "platform":   platform.system(),
        "python":     platform.python_version(),
        "install_dir":str(install_dir),
        "enrolled_at":time.time(),
        "auto":       True,
    }

    try:
        import urllib.request
        data = json.dumps(payload).encode()
        req  = urllib.request.Request(
            f"{mother_url}/api/worker/enroll",
            data=data,
            headers={"Content-Type":  "application/json",
                     "X-AENIDA-Key":  api_key},
            method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                p.ok(f"Enrolled with Mother at {mother_url}")
                p.info("This PC now appears on the dashboard")
                return True
            else:
                p.warn(f"Mother responded {resp.status} — will retry on heartbeat")
    except Exception as e:
        p.warn(f"Mother unreachable ({e}) — worker will enroll on first heartbeat")

    return True   # not fatal — heartbeat will retry


# ════════════════════════════════════════════════════════════════════
#  MAIN INSTALL FLOW
# ════════════════════════════════════════════════════════════════════

def run_install(usb_root: Optional[Path] = None) -> bool:
    """
    Full install flow.  Called when the user runs install.py on a blank PC.

    Args:
        usb_root: Path to the USB mount point.
                  Auto-detected if not provided.
    """
    print()
    print("=" * 62)
    print("  AENIDA Virtual Supercomputer — Worker Auto-Installer v2.0")
    print("=" * 62)

    # ── Locate USB root ───────────────────────────────────────────
    if usb_root is None:
        usb_root = Path(__file__).parent.resolve()

    usb_config_path = usb_root / USB_CONFIG
    usb_config: Dict = {}
    if usb_config_path.exists():
        try:
            usb_config = json.loads(usb_config_path.read_text())
            print(f"\n  Mother URL : {usb_config.get('mother_url','(not set)')}")
        except Exception:
            print("\n  ⚠ Could not read usb_config.json — using defaults")
    else:
        print("\n  ⚠ usb_config.json not found — install will proceed but")
        print("    the worker won't know where the Mother is.")
        print("    Edit core/config.json after install to set worker.url")

    # ── Environment check ─────────────────────────────────────────
    env = detect_environment()
    print(f"\n  OS       : {env['os']} {env.get('distro','')}")
    print(f"  Python   : {env['python']}")
    print(f"  RAM      : {env['ram_gb']} GB")
    print(f"  Disk free: {env['disk_gb']} GB")
    print(f"  Hostname : {env['hostname']}")

    # Generate unique node name
    short_host = env["hostname"].split(".")[0][:12]
    node_name  = usb_config.get("node_prefix", "worker") + "-" + short_host

    # Confirm
    print(f"\n  Node name: {node_name}")
    print()
    ans = input("  Install AENIDA worker on this PC? (yes/no): ").strip().lower()
    if ans != "yes":
        print("  Aborted.")
        return False

    # ── Choose install dir ────────────────────────────────────────
    is_windows   = env["os"] == "Windows"
    install_dir  = INSTALL_WIN if is_windows else INSTALL_DIR
    custom       = usb_config.get("install_dir", "")
    if custom:
        install_dir = Path(custom)

    # ── Source dir (project files on USB) ────────────────────────
    source_dir = usb_root / "aenida_project"
    if not source_dir.exists():
        # Fallback: installer is inside the project
        source_dir = usb_root.parent
    if not source_dir.exists():
        print(f"\n  ✗ Project files not found at {source_dir}")
        return False

    p = Progress(total_steps=6)

    # ── Step 1: Environment ───────────────────────────────────────
    p.step("Checking environment")
    if not check_prerequisites(env, p):
        return False
    p.ok("Prerequisites satisfied")

    # ── Step 2: Packages ─────────────────────────────────────────
    p.step("Installing Python packages")
    install_system_packages(env, p)

    # ── Step 3: Copy files ────────────────────────────────────────
    p.step(f"Copying AENIDA to {install_dir}")
    if not install_aenida_files(source_dir, install_dir, p):
        return False

    # ── Step 4: Config ────────────────────────────────────────────
    p.step("Writing worker configuration")
    if not write_worker_config(install_dir, usb_config, node_name, p):
        return False

    # ── Step 5: Service ───────────────────────────────────────────
    p.step("Installing startup service")
    if is_windows:
        install_windows_task(install_dir, p)
    else:
        install_systemd_service(install_dir, p)

    # ── Step 6: Auto-enroll ───────────────────────────────────────
    p.step("Enrolling with Mother node")
    enroll_with_mother(install_dir, usb_config, node_name, p)

    # ── Done ──────────────────────────────────────────────────────
    print()
    print("=" * 62)
    print(f"  ✓  Installation complete!")
    print(f"     Node: {node_name}")
    print(f"     Dir : {install_dir}")
    print()
    print("  Remove the USB stick.  This PC is now part of the")
    print("  AENIDA virtual supercomputer and will appear on the")
    print("  dashboard within 60 seconds.")
    print("=" * 62)
    print()
    return True


# ════════════════════════════════════════════════════════════════════
#  USB CREATOR  (run on Mother PC to prepare the USB stick)
# ════════════════════════════════════════════════════════════════════

def create_usb(device: str, mother_url: str = "",
               api_key: str = "", node_prefix: str = "worker") -> bool:
    """
    Prepare a USB stick for auto-deployment.

    Args:
        device:       Block device path e.g. /dev/sdb (Linux)
        mother_url:   Mother's URL e.g. http://192.168.1.10:8000
        api_key:      Worker API key (must match Mother's config)
        node_prefix:  Prefix for auto-generated node names

    WARNING: This will WIPE the USB stick.
    """
    print()
    print("=" * 62)
    print("  AENIDA USB Creator v2.0")
    print("=" * 62)
    print(f"\n  Device     : {device}")
    print(f"  Mother URL : {mother_url or '(not set)'}")
    print(f"  Key set    : {'yes' if api_key else 'no'}")
    print()

    ans = input(f"  WARNING: This will WIPE {device}. Continue? (yes/no): ")
    if ans.strip().lower() != "yes":
        print("  Aborted.")
        return False

    # Format
    print("\n[1/4] Formatting USB stick...")
    if not run_ok(f"wipefs -a {device}", "wipe filesystem signatures"):
        print("  ⚠ wipefs failed — USB may not format cleanly")
    if not run_ok(f"mkfs.vfat -F 32 -n {USB_LABEL} {device}",
                  "format FAT32"):
        print("  ✗ Format failed — is the device path correct?")
        return False
    print("  ✓ Formatted")

    # Mount
    print("\n[2/4] Mounting USB...")
    mount_pt = Path(f"/tmp/aenida_usb_{int(time.time())}")
    mount_pt.mkdir(parents=True, exist_ok=True)
    if not run_ok(f"mount {device} {mount_pt}", "mount"):
        print("  ✗ Mount failed")
        return False

    try:
        usb_install_dir = mount_pt / "AENIDA"
        usb_install_dir.mkdir(exist_ok=True)

        # Copy project
        print("\n[3/4] Copying AENIDA project...")
        project_dest = usb_install_dir / "aenida_project"
        if project_dest.exists():
            shutil.rmtree(project_dest)
        shutil.copytree(_BASE, project_dest,
                        ignore=shutil.ignore_patterns(
                            "__pycache__", "*.pyc", ".pytest_cache",
                            "*.db", "*.log", ".git"))

        # Copy install.py to USB root (shortcut for users)
        shutil.copy2(__file__, usb_install_dir / "install.py")

        # Write usb_config.json
        config = {
            "mother_url":    mother_url,
            "worker_api_key":api_key,
            "node_prefix":   node_prefix,
            "created_at":    time.time(),
            "created_on":    socket.gethostname(),
        }
        (usb_install_dir / USB_CONFIG).write_text(
            json.dumps(config, indent=2))

        # Windows autorun hint
        (usb_install_dir / "autorun.inf").write_text(
            "[autorun]\n"
            "label=AENIDA Worker Installer\n"
            f"open=install.py\n"
            "icon=install.py\n")

        # README
        (usb_install_dir / "README.txt").write_text(
            "AENIDA Virtual Supercomputer — Worker USB Installer\n"
            "=" * 52 + "\n\n"
            "LINUX:\n"
            "  sudo python3 /media/$USER/AENIDA/install.py\n\n"
            "WINDOWS (run as Administrator):\n"
            "  python E:\\AENIDA\\install.py\n\n"
            f"Mother node: {mother_url or '(configure after install)'}\n"
            f"Created:     {time.strftime('%Y-%m-%d %H:%M')}\n"
        )

        print("  ✓ Files copied")
        print("\n[4/4] Finalising...")
        run_ok(f"sync", "sync filesystem")
        print("  ✓ Done")

    finally:
        run_ok(f"umount {mount_pt}", "unmount")
        mount_pt.rmdir()

    print()
    print("=" * 62)
    print("  ✓  USB stick is ready!")
    print()
    print("  Plug it into any Linux/Windows PC and run:")
    print("    sudo python3 /media/$USER/AENIDA/install.py")
    print()
    print(f"  The new worker will appear on the dashboard as")
    print(f"  '{node_prefix}-<hostname>'")
    print("=" * 62)
    return True


# ════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(message)s")

    args = sys.argv[1:]

    if args and args[0] == "create-usb":
        # python usb_installer.py create-usb /dev/sdX [mother_url] [api_key]
        if len(args) < 2:
            print("Usage: python usb_installer.py create-usb /dev/sdX "
                  "[http://mother:8000] [api_key]")
            sys.exit(1)
        ok = create_usb(
            device      = args[1],
            mother_url  = args[2] if len(args) > 2 else "",
            api_key     = args[3] if len(args) > 3 else "",
        )
        sys.exit(0 if ok else 1)
    else:
        # Default: run as installer on a blank worker PC
        ok = run_install()
        sys.exit(0 if ok else 1)
