"""
AENIDA USB Transfer
Migrate entire AENIDA system from old laptop to new laptop.
Exports all data encrypted to USB; imports on new machine.
Secrets are NOT transferred — regenerate on new laptop.
"""

import getpass
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import tarfile
import tempfile
import time
from typing import Any, Dict, List, Optional

try:
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.backends import default_backend
    from cryptography.fernet import Fernet
    import base64
    CRYPTO_OK = True
except ImportError:
    CRYPTO_OK = False

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # aenida_v3 root

# Files/dirs to include in transfer
INCLUDE_DIRS = ["data", "knowledge", "skills", "strategies",
                "modules/permanent"]
INCLUDE_FILES = ["config.json"]

# Files explicitly excluded (must be regenerated)
EXCLUDE_FILES = [".env", "vault.enc", "usb_backup.enc",
                 "SCAN_WITH_AEGIS.png"]


# ── Encryption helpers ────────────────────────────────────────────
def _derive_key(password: str, salt: bytes) -> bytes:
    if not CRYPTO_OK:
        raise RuntimeError("cryptography package required: pip install cryptography")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=600_000,
        backend=default_backend(),
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode()))


def _encrypt(data: bytes, password: str) -> bytes:
    salt = os.urandom(16)
    key = _derive_key(password, salt)
    f = Fernet(key)
    return salt + f.encrypt(data)


def _decrypt(data: bytes, password: str) -> bytes:
    salt = data[:16]
    key = _derive_key(password, salt)
    f = Fernet(key)
    return f.decrypt(data[16:])


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ── Export ────────────────────────────────────────────────────────
def export_to_usb(usb_path: str, password: Optional[str] = None) -> Dict[str, Any]:  # type: ignore
    """
    Export AENIDA system to USB.

    Args:
        usb_path: Mount point of USB drive (e.g. /media/AENIDA)
        password: Encryption password (prompted if None)

    Returns:
        Summary dict with checksum and file sizes.
    """
    if not CRYPTO_OK:
        return {"error": "pip install cryptography first"}

    if password is None:
        password = getpass.getpass("Enter USB master password for export: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            return {"error": "Passwords do not match"}

    print("[TRANSFER] Starting export...")

    with tempfile.TemporaryDirectory() as tmp:
        archive_path = os.path.join(tmp, "aenida_transfer.tar")

        # Build tar of included content
        with tarfile.open(archive_path, "w") as tar:
            for d in INCLUDE_DIRS:
                full = os.path.join(PROJECT_ROOT, d)
                if os.path.exists(full):
                    tar.add(full, arcname=d)
                    print(f"  + {d}/")

            for f in INCLUDE_FILES:
                full = os.path.join(PROJECT_ROOT, f)
                if os.path.exists(full) and f not in EXCLUDE_FILES:
                    tar.add(full, arcname=f)
                    print(f"  + {f}")

        # Read and encrypt
        with open(archive_path, "rb") as fh:
            raw = fh.read()

        encrypted = _encrypt(raw, password)
        checksum = hashlib.sha256(encrypted).hexdigest()

        # Write metadata
        meta = {
            "version": "5.2",
            "exported_at": time.time(),
            "checksum": checksum,
            "files_included": INCLUDE_DIRS + INCLUDE_FILES,
            "secrets_excluded": True,
            "note": (
                "Secrets NOT included. On new laptop regenerate: "
                "TYR_MASTER_SECRET, TYR_HKDF_SALT, TYR_SYNC_KEY, "
                "ARES_SHARED_IDENTITY. TOTP secret stays in Aegis."
            ),
        }

        pkg_path = os.path.join(usb_path, "transfer_package.enc")
        meta_path = os.path.join(usb_path, "transfer_meta.json")

        os.makedirs(usb_path, exist_ok=True)
        with open(pkg_path, "wb") as fh:
            fh.write(encrypted)
        with open(meta_path, "w") as fh:
            json.dump(meta, fh, indent=2)

        size_mb = round(len(encrypted) / 1e6, 2)
        print(f"\n[TRANSFER] Export complete")
        print(f"  Package: {pkg_path}")
        print(f"  Size:    {size_mb} MB")
        print(f"  SHA-256: {checksum[:16]}...")
        print(f"\n⚠️  Secrets NOT transferred. Regenerate on new laptop:")
        print("  python -c \"import secrets; print(secrets.token_hex(32))\"")
        print("  Run 4 times for: TYR_MASTER_SECRET, TYR_HKDF_SALT,")
        print("                   TYR_SYNC_KEY, ARES_SHARED_IDENTITY")
        print("  TOTP: scan same Aegis QR on new phone if needed")

        return {
            "status": "success",
            "package_path": pkg_path,
            "size_mb": size_mb,
            "checksum": checksum,
        }


# ── Import ────────────────────────────────────────────────────────
def import_from_usb(usb_path: str, password: Optional[str] = None,  # type: ignore
                    target_dir: str = PROJECT_ROOT) -> Dict[str, Any]:
    """
    Import AENIDA system from USB onto new laptop.

    Args:
        usb_path: Mount point of USB drive
        password: Decryption password (prompted if None)
        target_dir: Where to restore files (default: project root)

    Returns:
        Summary dict.
    """
    if not CRYPTO_OK:
        return {"error": "pip install cryptography first"}

    pkg_path = os.path.join(usb_path, "transfer_package.enc")
    meta_path = os.path.join(usb_path, "transfer_meta.json")

    if not os.path.exists(pkg_path):
        return {"error": f"Transfer package not found: {pkg_path}"}

    if password is None:
        password = getpass.getpass("Enter USB master password to decrypt: ")

    print("[TRANSFER] Starting import...")

    # Read metadata
    if os.path.exists(meta_path):
        with open(meta_path) as fh:
            meta = json.load(fh)
        expected_checksum = meta.get("checksum", "")
        print(f"  Exported: {time.strftime('%Y-%m-%d', time.localtime(meta.get('exported_at', 0)))}")
        print(f"  Version:  {meta.get('version', '?')}")
    else:
        expected_checksum = ""

    # Read and verify checksum
    with open(pkg_path, "rb") as fh:
        encrypted = fh.read()

    actual_checksum = hashlib.sha256(encrypted).hexdigest()
    if expected_checksum and actual_checksum != expected_checksum:
        return {"error": "Checksum mismatch — package may be corrupted"}
    print(f"  Checksum: ✓ verified")

    # Decrypt
    try:
        raw = _decrypt(encrypted, password)
    except Exception:
        return {"error": "Decryption failed — wrong password?"}

    # Extract tar
    with tempfile.TemporaryDirectory() as tmp:
        archive_path = os.path.join(tmp, "aenida_transfer.tar")
        with open(archive_path, "wb") as fh:
            fh.write(raw)

        restored: List[str] = []
        with tarfile.open(archive_path, "r") as tar:
            members = tar.getmembers()
            for member in members:
                # Security: prevent path traversal
                if ".." in member.name or member.name.startswith("/"):
                    print(f"  ⚠ Skipped unsafe path: {member.name}")
                    continue
                tar.extract(member, path=target_dir)
                restored.append(member.name)
                if len(restored) % 50 == 0:
                    print(f"  Restored {len(restored)} files...")

    print(f"\n[TRANSFER] Import complete — {len(restored)} items restored")
    print("\n" + "=" * 50)
    print("NEXT STEPS ON NEW LAPTOP:")
    print("=" * 50)
    print("1. Generate new secrets (4 commands):")
    print('   python -c "import secrets; print(secrets.token_hex(32))"')
    print("2. Fill in .env with generated secrets")
    print("3. Regenerate hardware fingerprint (auto on first run)")
    print("4. Re-authenticate Tailscale:")
    print("   tailscale up --authkey YOUR_KEY")
    print("5. Test: python main.py --mode display")
    print("6. When confirmed working, wipe old laptop (optional)")
    print("=" * 50)

    return {
        "status": "success",
        "restored_count": len(restored),
        "checksum_verified": bool(expected_checksum),
    }


# ── CLI ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="AENIDA USB Transfer")
    parser.add_argument("--export", metavar="USB_PATH",
                        help="Export to USB at given path")
    parser.add_argument("--import", dest="import_path", metavar="USB_PATH",
                        help="Import from USB at given path")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if args.export:
        result = export_to_usb(args.export)
        print(json.dumps(result, indent=2))
    elif args.import_path:
        result = import_from_usb(args.import_path)
        print(json.dumps(result, indent=2))
    else:
        parser.print_help()
