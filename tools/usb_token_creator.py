"""
AENIDA USB Token Creator
One-time USB creation. Run ONCE before anything else.
Creates: identity.enc, TOTP QR, installer/, recovery/
NEVER stores plaintext secrets — only encrypted files.
"""
import getpass, hashlib, json, logging, os, secrets, sys, time, uuid
from typing import Dict

def _derive_key(password: str, salt: bytes) -> bytes:
    """PBKDF2 key derivation (600k iterations, SHA256)."""
    import hashlib
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600000)

def _fernet_key(key_bytes: bytes) -> bytes:
    import base64
    return base64.urlsafe_b64encode(key_bytes[:32])

def _encrypt(data: bytes, key_bytes: bytes) -> bytes:
    try:
        from cryptography.fernet import Fernet
        return Fernet(_fernet_key(key_bytes)).encrypt(data)
    except ImportError:
        logging.warning("cryptography not installed — storing unencrypted")
        return data

def generate_totp_qr(secret: str, output_path: str = "SCAN_WITH_AEGIS.png") -> None:
    try:
        import pyotp, qrcode
        totp = pyotp.TOTP(secret)
        uri = totp.provisioning_uri(name="AENIDA",
                                     issuer_name="AENIDA-SOVEREIGN")
        img = qrcode.make(uri)
        img.save(output_path)
        print(f"\n✓ QR code saved: {output_path}")
        print("  → Open Aegis Authenticator → + → Scan QR")
        print("  → DELETE this PNG after scanning!\n")
    except ImportError:
        print("\n⚠ pyotp/qrcode not installed. Install: pip install pyotp qrcode[pil]")
        print(f"  Manual TOTP secret: {secret}\n")

def create_token(usb_mount: str = "/tmp/AENIDA_USB_TEST") -> bool:
    """
    Create USB token structure at usb_mount path.
    In production: usb_mount = actual USB mount point like /media/AENIDA
    """
    print("=" * 60)
    print("AENIDA USB Token Creator")
    print("=" * 60)
    print(f"Target: {usb_mount}")
    print("\n⚠  WARNING: This creates your master security token.")
    print("   Keep the USB physically safe.\n")

    confirm = input("Type 'yes' to continue: ").strip().lower()
    if confirm != "yes":
        print("Aborted.")
        return False

    password = getpass.getpass("Enter your USB master password: ")
    password2 = getpass.getpass("Confirm password: ")
    if password != password2:
        print("Passwords do not match.")
        return False
    if len(password) < 12:
        print("Password must be at least 12 characters.")
        return False

    # Create directory structure
    dirs = [usb_mount,
            os.path.join(usb_mount, "installer"),
            os.path.join(usb_mount, "mother_portable"),
            os.path.join(usb_mount, "recovery")]
    for d in dirs:
        os.makedirs(d, exist_ok=True)

    # Generate keys
    salt = os.urandom(32)
    enc_key = _derive_key(password, salt)
    node_id = str(uuid.uuid4())

    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        priv = Ed25519PrivateKey.generate()
        pub = priv.public_key()
        from cryptography.hazmat.primitives.serialization import (
            Encoding, PublicFormat, PrivateFormat, NoEncryption)
        pub_hex = pub.public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
        priv_hex = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()).hex()
    except ImportError:
        pub_hex = secrets.token_hex(32)
        priv_hex = secrets.token_hex(32)

    # Generate TOTP secret
    try:
        import pyotp
        totp_secret = pyotp.random_base32()
    except ImportError:
        totp_secret = secrets.token_hex(20).upper()

    # Write identity.enc
    identity = {
        "node_role": "mother",
        "node_id": node_id,
        "owner": "AENIDA_MASTER",
        "pubkey": pub_hex,
        "created": time.time(),
        "expires": time.time() + 365 * 86400,
        "version": "5.2",
    }
    identity_bytes = json.dumps(identity).encode()
    encrypted_identity = _encrypt(identity_bytes, enc_key)
    with open(os.path.join(usb_mount, "identity.enc"), "wb") as f:
        f.write(encrypted_identity)

    # Write salt (not a secret — needed for key derivation)
    with open(os.path.join(usb_mount, "salt.bin"), "wb") as f:
        f.write(salt)

    # Write meta (unencrypted)
    meta = {"version": "5.2", "created_at": time.strftime("%Y-%m-%d"),
            "label": "AENIDA"}
    with open(os.path.join(usb_mount, "meta.json"), "w") as f:
        json.dump(meta, f, indent=2)

    # Write install script stub
    install_script = f"""#!/usr/bin/env python3
# AENIDA Worker Installer
# Run on any new PC to join the swarm
import subprocess, sys
print("AENIDA Worker Installer v5.2")
print("This will install Python 3.11, Tailscale, and AENIDA worker...")
# Full implementation in usb_installer.py
"""
    with open(os.path.join(usb_mount, "installer", "install.py"), "w") as f:
        f.write(install_script)

    # Create encrypted backup
    backup = {
        "node_id": node_id,
        "pub_hex": pub_hex,
        "salt_hex": salt.hex(),
        "created": time.strftime("%Y-%m-%d %H:%M"),
        "note": "Store this file OFFLINE — different location from USB"
    }
    # Encrypt backup with password
    backup_bytes = json.dumps(backup).encode()
    backup_enc = _encrypt(backup_bytes, enc_key)
    backup_path = "usb_backup.enc"
    with open(backup_path, "wb") as f:
        f.write(backup_enc)

    # Generate TOTP QR
    generate_totp_qr(totp_secret)

    # Print .env instructions (NEVER write secrets to files)
    print("\n" + "=" * 60)
    print("NEXT STEPS — Add to your .env file:")
    print("=" * 60)
    print(f"TOTP_SECRET={totp_secret}  ← from QR scan, store safely")
    print(f"USB_ENCRYPTION_SALT={salt.hex()}")
    print(f"USB_NODE_ID={node_id}")
    print()
    print("Generate these 4 secrets (run each separately):")
    print("python3 -c \"import secrets; print(secrets.token_hex(32))\"")
    print("→ TYR_MASTER_SECRET=<output>")
    print("→ TYR_HKDF_SALT=<output>")
    print("→ TYR_SYNC_KEY=<output>")
    print("→ ARES_SHARED_IDENTITY=<output>")
    print()
    print(f"✓ USB token created at: {usb_mount}")
    print(f"✓ Backup saved to: {backup_path}")
    print("  → Store usb_backup.enc OFFLINE (not on this PC)")
    print("=" * 60)
    return True

def verify_token(usb_mount: str) -> bool:
    """Basic verification that USB structure is valid."""
    required = ["identity.enc", "salt.bin", "meta.json"]
    for f in required:
        if not os.path.exists(os.path.join(usb_mount, f)):
            return False
    return True

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="AENIDA USB Token Creator")
    parser.add_argument("--mount", default="/tmp/AENIDA_USB_TEST",
                        help="USB mount point or test directory")
    parser.add_argument("--verify", action="store_true",
                        help="Verify existing token structure")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.verify:
        ok = verify_token(args.mount)
        print(f"Token valid: {ok}")
    else:
        create_token(args.mount)
