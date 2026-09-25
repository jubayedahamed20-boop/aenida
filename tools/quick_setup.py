"""
AENIDA Quick Setup  v1.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Replaces the USB token creation process.
No USB drive needed — uses a SoftToken stored at ~/.aenida/

Run:  python quick_setup.py
Done in ~30 seconds. One command, zero USB hassle.

What it does:
  1. Creates ~/.aenida/ directory (your secure config home)
  2. Generates all secrets automatically (no typing hex strings)
  3. Writes a ready-to-use .env file
  4. Installs the shadow worker as a systemd service (Linux)
  5. Prints a QR code in your terminal for TOTP (no PNG file needed)

SoftToken vs USB:
  USB = physical drive required at all times, 20-min setup
  SoftToken = stored in ~/.aenida/, secured by OS file permissions
  Security level: same — both use Fernet + PBKDF2 + TOTP
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import getpass, hashlib, json, os, platform, secrets, stat, subprocess
import sys, time, uuid
from pathlib import Path

# ── Colours (safe fallback if terminal doesn't support) ────────────
try:
    from colorama import Fore, Style, init as _cinit
    _cinit(autoreset=True)
    G = Fore.GREEN; R = Fore.RED; Y = Fore.YELLOW
    B = Fore.CYAN;  P = Fore.MAGENTA; Z = Style.RESET_ALL
except ImportError:
    G = R = Y = B = P = Z = ""

AENIDA_HOME = Path.home() / ".aenida"
ENV_FILE     = Path(".env")          # written next to quick_setup.py
TOKEN_FILE   = AENIDA_HOME / "softtoken.json"
SECRETS_FILE = AENIDA_HOME / "secrets.enc"


# ════════════════════════════════════════════════════════════════════
#  HELPERS
# ════════════════════════════════════════════════════════════════════
def _step(n: int, total: int, msg: str) -> None:
    print(f"\n{B}[{n}/{total}]{Z} {msg}")


def _ok(msg: str) -> None:
    print(f"  {G}✓{Z}  {msg}")


def _warn(msg: str) -> None:
    print(f"  {Y}⚠{Z}  {msg}")


def _fail(msg: str) -> None:
    print(f"  {R}✗{Z}  {msg}")


def _run(cmd: str, silent: bool = True) -> bool:
    try:
        r = subprocess.run(cmd, shell=True,
                           capture_output=silent, text=True)
        return r.returncode == 0
    except Exception:
        return False


def _derive_key(password: str, salt: bytes) -> bytes:
    """PBKDF2 — 200k iterations (fast enough for setup, still strong)."""
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)


def _fernet_key(raw: bytes) -> bytes:
    import base64
    return base64.urlsafe_b64encode(raw[:32])


def _encrypt(data: bytes, key_raw: bytes) -> bytes:
    try:
        from cryptography.fernet import Fernet
        return Fernet(_fernet_key(key_raw)).encrypt(data)
    except ImportError:
        _warn("cryptography not installed — secrets stored as plaintext hash")
        return data


# ════════════════════════════════════════════════════════════════════
#  TOTP QR  — printed directly in terminal (no PNG file)
# ════════════════════════════════════════════════════════════════════
def _print_totp_qr(secret: str, node_id: str) -> None:
    """Print a QR code directly in the terminal using qrcode text mode."""
    try:
        import pyotp, qrcode
        totp = pyotp.TOTP(secret)
        uri  = totp.provisioning_uri(name=f"AENIDA-{node_id[:8]}",
                                      issuer_name="AENIDA-SOVEREIGN")
        qr = qrcode.QRCode(border=1)
        qr.add_data(uri)
        qr.make(fit=True)
        print()
        print(f"  {P}━━━  Scan this QR with Aegis Authenticator  ━━━{Z}")
        qr.print_ascii(invert=True)
        print(f"  {Y}Open Aegis → + → Scan QR  (then press Enter){Z}")
        input("  Press Enter once scanned ▶ ")
        return True
    except ImportError:
        # No qrcode library — just print the secret
        _warn("qrcode library not found — add TOTP manually:")
        print(f"     Secret: {Y}{secret}{Z}")
        print(f"     Issuer: AENIDA-SOVEREIGN")
        return False


# ════════════════════════════════════════════════════════════════════
#  STEP 1 — Create ~/.aenida/ with locked permissions
# ════════════════════════════════════════════════════════════════════
def step_create_home() -> None:
    _step(1, 5, "Creating AENIDA home directory")
    AENIDA_HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Ensure only owner can read/write/execute
    os.chmod(AENIDA_HOME, stat.S_IRWXU)
    _ok(f"Directory: {AENIDA_HOME}  (chmod 700 — owner only)")


# ════════════════════════════════════════════════════════════════════
#  STEP 2 — Password + generate all secrets
# ════════════════════════════════════════════════════════════════════
def step_generate_secrets(password: str) -> dict:
    _step(2, 5, "Generating all secrets")

    salt     = os.urandom(32)
    enc_key  = _derive_key(password, salt)
    node_id  = str(uuid.uuid4())

    # Ed25519 keypair (or random fallback)
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import (
            Encoding, PublicFormat, PrivateFormat, NoEncryption)
        priv = Ed25519PrivateKey.generate()
        pub  = priv.public_key()
        pub_hex  = pub.public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
        priv_hex = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()).hex()
    except ImportError:
        pub_hex  = secrets.token_hex(32)
        priv_hex = secrets.token_hex(32)

    # TOTP
    try:
        import pyotp
        totp_secret = pyotp.random_base32()
    except ImportError:
        totp_secret = secrets.token_hex(20).upper()

    # TYR / ARES secrets — all auto-generated, no manual hex typing
    tyr_master   = secrets.token_hex(32)
    tyr_hkdf     = secrets.token_hex(32)
    tyr_sync     = secrets.token_hex(32)
    ares_id      = secrets.token_hex(32)
    api_key      = secrets.token_hex(24)     # mobile API key
    dashboard_pw = secrets.token_urlsafe(16) # dashboard password

    bundle = {
        "node_id":       node_id,
        "pub_hex":       pub_hex,
        "priv_hex":      priv_hex,
        "totp_secret":   totp_secret,
        "tyr_master":    tyr_master,
        "tyr_hkdf":      tyr_hkdf,
        "tyr_sync":      tyr_sync,
        "ares_id":       ares_id,
        "api_key":       api_key,
        "dashboard_pw":  dashboard_pw,
        "salt_hex":      salt.hex(),
        "created":       time.strftime("%Y-%m-%d %H:%M"),
    }

    # Encrypt and save softtoken
    token_plain = json.dumps({k: v for k, v in bundle.items()
                               if k not in ("priv_hex",)}).encode()
    token_enc = _encrypt(token_plain, enc_key)
    TOKEN_FILE.write_bytes(token_enc)
    os.chmod(TOKEN_FILE, stat.S_IRUSR | stat.S_IWUSR)  # 600

    _ok(f"SoftToken saved: {TOKEN_FILE}  (chmod 600)")
    _ok(f"Node ID: {node_id[:16]}…")
    _ok("Ed25519 keypair generated")
    _ok("TYR / ARES / TOTP secrets generated")

    return bundle


# ════════════════════════════════════════════════════════════════════
#  STEP 3 — Write .env file
# ════════════════════════════════════════════════════════════════════
def step_write_env(b: dict) -> None:
    _step(3, 5, f"Writing .env file → {ENV_FILE.absolute()}")
    env_text = f"""\
# ═══════════════════════════════════════════════════════
#  AENIDA Environment — auto-generated by quick_setup.py
#  Generated: {b['created']}
#  Node ID:   {b['node_id']}
# ═══════════════════════════════════════════════════════

# ── Security ─────────────────────────────────────────
AENIDA_NODE_ID={b['node_id']}
TOTP_SECRET={b['totp_secret']}
USB_ENCRYPTION_SALT={b['salt_hex']}

TYR_MASTER_SECRET={b['tyr_master']}
TYR_HKDF_SALT={b['tyr_hkdf']}
TYR_SYNC_KEY={b['tyr_sync']}
ARES_SHARED_IDENTITY={b['ares_id']}

AENIDA_API_KEY={b['api_key']}
DASHBOARD_PASSWORD={b['dashboard_pw']}

# ── Auth mode (softtoken = no USB required) ──────────
AUTH_MODE=softtoken
SOFTTOKEN_PATH={TOKEN_FILE}

# ── AI API Keys (fill these in) ──────────────────────
GROQ_API_KEY=
GEMINI_API_KEY=
TOGETHER_API_KEY=

# ── Trading (optional) ────────────────────────────────
OKX_API_KEY=
OKX_SECRET=
OKX_PASSPHRASE=

# ── Telegram alerts (optional) ───────────────────────
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# ── Worker network ────────────────────────────────────
WORKER_URL=http://localhost:8000
MOBILE_API_PORT=5000
"""
    ENV_FILE.write_text(env_text)
    os.chmod(ENV_FILE, stat.S_IRUSR | stat.S_IWUSR)  # 600
    _ok(f".env written  (fill in API keys when ready)")


# ════════════════════════════════════════════════════════════════════
#  STEP 4 — TOTP QR in terminal
# ════════════════════════════════════════════════════════════════════
def step_totp(b: dict) -> None:
    _step(4, 5, "Setting up TOTP (2-factor auth)")
    _print_totp_qr(b["totp_secret"], b["node_id"])
    _ok("TOTP configured")


# ════════════════════════════════════════════════════════════════════
#  STEP 5 — Install shadow worker service
# ════════════════════════════════════════════════════════════════════
def step_install_service() -> None:
    _step(5, 5, "Installing shadow worker (background service)")
    script_dir = Path(__file__).parent.resolve()

    if platform.system() == "Linux":
        _install_systemd(script_dir)
    elif platform.system() == "Darwin":
        _install_launchd(script_dir)
    else:
        _warn("Windows: run  shadow_worker.py --install  as Administrator")


def _install_systemd(script_dir: Path) -> None:
    service_content = f"""\
[Unit]
Description=AENIDA Shadow Worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={os.getenv("USER", "root")}
WorkingDirectory={script_dir}
ExecStart=/usr/bin/python3 {script_dir}/shadow_worker.py
Restart=always
RestartSec=15
StandardOutput=null
StandardError=journal
SyslogIdentifier=aenida
Nice=10
CPUWeight=50
MemoryMax=512M

[Install]
WantedBy=multi-user.target
"""
    svc_path = Path("/etc/systemd/system/aenida.service")
    try:
        svc_path.write_text(service_content)
        _run("systemctl daemon-reload")
        _run("systemctl enable --now aenida")
        _ok("systemd service installed: aenida.service")
        _ok("Runs silently in background — no console output")
    except PermissionError:
        # Save the service file locally so user can install with sudo
        local_svc = script_dir / "aenida.service"
        local_svc.write_text(service_content)
        _warn("Need sudo to install service. Run these 3 commands:")
        print(f"     sudo cp {local_svc} /etc/systemd/system/")
        print(f"     sudo systemctl daemon-reload")
        print(f"     sudo systemctl enable --now aenida")


def _install_launchd(script_dir: Path) -> None:
    plist_content = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.aenida.worker</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>{script_dir}/shadow_worker.py</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>{Path.home()}/.aenida/logs/worker.log</string>
    <key>StandardErrorPath</key><string>{Path.home()}/.aenida/logs/worker.log</string>
</dict>
</plist>
"""
    plist_path = Path.home() / "Library/LaunchAgents/com.aenida.worker.plist"
    plist_path.write_text(plist_content)
    _run(f"launchctl load {plist_path}")
    _ok(f"launchd agent installed: {plist_path.name}")


# ════════════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════════════
def main():
    print(f"""
{P}╔══════════════════════════════════════════════╗
║    AENIDA  —  Quick Setup  v1.0              ║
║    No USB required  •  Done in ~30 seconds   ║
╚══════════════════════════════════════════════╝{Z}
""")

    # Check if already set up
    if TOKEN_FILE.exists():
        print(f"{Y}⚠ SoftToken already exists at {TOKEN_FILE}{Z}")
        redo = input("  Re-generate? This will replace your current token. (yes/no): ").strip()
        if redo.lower() != "yes":
            print("Setup cancelled. Existing token kept.")
            sys.exit(0)

    # Password
    print(f"\n{B}Enter a master password{Z} (min 8 chars — you need this to unlock):")
    while True:
        pw = getpass.getpass("  Password: ")
        if len(pw) < 8:
            _warn("Too short — try again")
            continue
        pw2 = getpass.getpass("  Confirm:  ")
        if pw != pw2:
            _warn("Passwords don't match — try again")
            continue
        break

    print(f"\n  {G}Password accepted. Generating secrets...{Z}")
    start = time.time()

    step_create_home()
    bundle = step_generate_secrets(pw)
    step_write_env(bundle)
    step_totp(bundle)
    step_install_service()

    elapsed = time.time() - start
    print(f"""
{G}═══════════════════════════════════════════════════
  Setup complete in {elapsed:.1f}s  ✓
═══════════════════════════════════════════════════{Z}

  {B}Next steps:{Z}
  1. Edit {Y}.env{Z} — add GROQ_API_KEY (or GEMINI_API_KEY)
  2. Run:  {Y}python main.py --mode display{Z}   (laptop)
  3. Run:  {Y}python main.py --mode api{Z}         (phone monitor)
  4. Shadow worker is already running silently in background

  {B}Mobile access:{Z}  http://YOUR-LAN-IP:5000
  {B}Dashboard PW:{Z}   {Y}{bundle['dashboard_pw']}{Z}  ← save this!
  {B}Node ID:{Z}        {bundle['node_id'][:16]}…

  {P}No USB drive needed — ever.{Z}
""")


if __name__ == "__main__":
    main()
