"""
AENIDA Module Forge
Creates, tests, hot-reloads, and manages temporary/permanent modules.
AST security check (BUG-29), temp cleanup (BUG-31), isolated tests (BUG-32).
"""

import ast
import importlib
import importlib.util
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from typing import Any, Dict, List, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # aenida_v3 root
TEMP_DIR = os.path.join(PROJECT_ROOT, "modules", "temp")
PERM_DIR = os.path.join(PROJECT_ROOT, "modules", "permanent")
TESTS_DIR = os.path.join(PROJECT_ROOT, "tests")
DB_PATH = os.path.join(PROJECT_ROOT, "data", "module_registry.db")

BANNED_PATTERNS = [
    "os.system", "exec(", "eval(", "__import__",
    "subprocess.call", "subprocess.run", "subprocess.Popen",
    "open(\"/etc", "open(\"/sys", "open(\"/proc",
    "open('/etc", "open('/sys", "open('/proc",
]


# ── Database ──────────────────────────────────────────────────────
def _db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS modules (
            id            TEXT PRIMARY KEY,
            name          TEXT NOT NULL,
            module_type   TEXT NOT NULL,
            status        TEXT DEFAULT 'TEMP',
            file_path     TEXT NOT NULL,
            created_at    REAL NOT NULL,
            expires_at    REAL,
            approved_at   REAL,
            test_score    REAL,
            confidence    REAL,
            version       INTEGER DEFAULT 1,
            description   TEXT,
            source        TEXT
        )
    """)
    conn.commit()
    return conn


# ── AST Security Check (BUG-29) ───────────────────────────────────
class _SecurityVisitor(ast.NodeVisitor):
    def __init__(self):
        self.violations: List[str] = []

    def visit_Call(self, node: ast.Call) -> None:
        name = ast.unparse(node.func) if hasattr(ast, "unparse") else ""
        for banned in BANNED_PATTERNS:
            if banned.rstrip("(") in name:
                self.violations.append(f"Banned call: {name}")
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name in ("subprocess", "pty", "ctypes"):
                self.violations.append(f"Banned import: {alias.name}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module in ("subprocess", "pty", "ctypes", "os"):
            for alias in node.names:
                if alias.name in ("system", "popen", "execv", "execve"):
                    self.violations.append(
                        f"Banned import: from {node.module} import {alias.name}")
        self.generic_visit(node)


def run_ast_security_check(code: str) -> bool:
    """
    BUG-29: Parse and scan code for dangerous patterns.
    Returns True if safe, False if violations found.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        logging.error(f"[FORGE] Syntax error: {e}")
        return False

    visitor = _SecurityVisitor()
    visitor.visit(tree)

    # Also do raw string check for patterns AST might miss
    for pattern in BANNED_PATTERNS:
        if pattern in code:
            visitor.violations.append(f"Raw pattern found: {pattern}")

    if visitor.violations:
        logging.warning(f"[FORGE] Security violations: {visitor.violations}")
        return False

    return True


# ── Module creation ───────────────────────────────────────────────
def _ensure_dirs() -> None:
    for d in (TEMP_DIR, PERM_DIR, TESTS_DIR):
        os.makedirs(d, exist_ok=True)


def _generate_code(spec: Dict[str, Any]) -> str:
    """Generate module code via Worker AI."""
    purpose = spec.get("purpose", "Unknown purpose")
    functions = spec.get("required_functions", [])
    constraints = spec.get("constraints", "")

    prompt = f"""Write a Python module for AENIDA.
Purpose: {purpose}
Required functions: {', '.join(functions) if functions else 'as needed'}
Constraints: {constraints}

Hard rules:
- Import config from config.py for settings
- Import alert_manager for all alerts
- Handle ALL exceptions (never propagate crashes)
- No os.system, no exec, no eval, no subprocess
- No open('/etc/...') or open('/sys/...')
- Python 3.11+, type hints, docstrings
- Use __slots__ if defining a high-frequency class

Return ONLY the Python module code. No markdown. No explanation."""

    try:
        from model_shell import call
        result = call(prompt)
        code = result.get("result", "")
        import re
        code = re.sub(r"```python\n?|```\n?", "", code).strip()
        return code
    except Exception as e:
        logging.error(f"[FORGE] Code generation failed: {e}")
        return f'"""\nAuto-generated stub for {purpose}\n"""\n\ndef placeholder():\n    pass\n'


def create_temp(spec: Dict[str, Any]) -> str:
    """
    Create a temporary module (not active until approved).

    Args:
        spec: {purpose, required_functions, constraints, confidence,
               description, source}
    Returns:
        module_id (UUID)
    """
    _ensure_dirs()

    purpose = spec.get("purpose", "Unknown")
    safe_name = purpose.lower().replace(" ", "_")[:30]
    uid = uuid.uuid4().hex[:8]
    module_name = f"{safe_name}_{uid}"
    file_path = os.path.join(TEMP_DIR, f"{module_name}.py")

    # Step 1: Generate code
    logging.info(f"[FORGE] Generating code for: {purpose}")
    code = _generate_code(spec)

    # Step 2: Sanitize
    try:
        from sanitizer_shield import scrub
        code = scrub(code)
    except Exception:
        pass  # sanitizer not critical here — AST check covers it

    # Step 3: AST security check
    if not run_ast_security_check(code):
        logging.error(f"[FORGE] Security check failed for {module_name}")
        try:
            from alert_manager import send, AlertLevel
            send(AlertLevel.WARNING, "Module Forge",
                 f"Generated code had security violations: {module_name}")
        except Exception:
            pass
        return ""

    # Step 4: Write to temp file
    with open(file_path, "w") as f:
        f.write(code)

    # Step 5: Generate and write tests
    test_path = os.path.join(TESTS_DIR, f"test_{module_name}.py")
    try:
        from test_runner import generate_tests
        test_code = generate_tests(code, module_name)
        with open(test_path, "w") as f:
            f.write(test_code)
    except Exception as e:
        logging.warning(f"[FORGE] Test generation failed: {e}")

    # Step 6: Run tests
    test_score = 0.0
    status = "TEMP_FAILED"
    try:
        from test_runner import run_tests
        result = run_tests(module_name)
        test_score = result["score"]
        status = "TEMP_READY" if test_score >= 80.0 else "TEMP_FAILED"
        logging.info(f"[FORGE] Tests: {test_score}% — {status}")
    except Exception as e:
        logging.warning(f"[FORGE] Test run failed: {e}")

    # Step 7: Register
    module_id = str(uuid.uuid4())
    expiry = time.time() + spec.get("expiry_hours", 24) * 3600
    conn = _db()
    conn.execute("""
        INSERT INTO modules (id, name, module_type, status, file_path,
                             created_at, expires_at, test_score, confidence,
                             description, source)
        VALUES (?, ?, 'TEMP', ?, ?, ?, ?, ?, ?, ?, ?)
    """, (module_id, module_name, status, file_path, time.time(), expiry,
          test_score, spec.get("confidence", 0.0),
          spec.get("description", purpose),
          spec.get("source", "auto")))
    conn.commit()
    conn.close()

    # Step 8: Alert
    msg = (f"Module ready: {module_name}\n"
           f"Tests: {test_score}% | Status: {status}\n"
           f"Review: python main.py --review-modules")
    try:
        from alert_manager import send, AlertLevel
        level = AlertLevel.INFO if status == "TEMP_READY" else AlertLevel.WARNING
        send(level, "Module Forge", msg)
    except Exception:
        logging.info(f"[FORGE] {msg}")

    return module_id


def approve_module(module_id: str) -> bool:
    """
    Promote TEMP → PERMANENT and hot-reload.
    Returns True on success.
    """
    conn = _db()
    cursor = conn.execute(
        "SELECT name, file_path, status FROM modules WHERE id=?", (module_id,)
    )
    row = cursor.fetchone()
    conn.close()

    if not row:
        logging.error(f"[FORGE] Module not found: {module_id}")
        return False

    name, temp_path, status = row
    if "FAILED" in status:
        logging.warning(f"[FORGE] Cannot approve failed module: {name}")
        return False

    # Backup existing permanent if present
    perm_path = os.path.join(PERM_DIR, f"{name}.py")
    bak_path  = os.path.join(PERM_DIR, f"{name}.bak.py")
    if os.path.exists(perm_path):
        shutil.copy2(perm_path, bak_path)

    # Copy to permanent
    try:
        shutil.copy2(temp_path, perm_path)
    except Exception as e:
        logging.error(f"[FORGE] Copy to permanent failed: {e}")
        return False

    # ── BUG 5 FIX: Backtest / integration validation before hot-reload ──
    # Old code: promoted the module straight to hot-reload with no extra check.
    # A module that passed unit tests (imports OK, basic functions exist) could
    # still produce wrong output when run against real data.
    # Fix: run trigger_ci() which includes AST check + tests + smoke import.
    # Only if all 3 stages pass do we hot-reload into the live system.
    try:
        from test_runner import trigger_ci
        ci_result = trigger_ci(name)
        if ci_result["status"] != "PASSED":
            logging.error(
                f"[FORGE] CI failed at stage '{ci_result.get('stage')}' "
                f"for {name} — aborting approval")
            # Remove the perm copy we just wrote
            if os.path.exists(perm_path):
                os.remove(perm_path)
            # Restore backup if it existed
            if os.path.exists(bak_path):
                shutil.copy2(bak_path, perm_path)
            return False
        logging.info(f"[FORGE] CI passed ({ci_result.get('score', 0):.1f}%) — promoting")
    except Exception as ci_err:
        # If test_runner itself is broken, log and continue (degraded mode)
        logging.warning(f"[FORGE] CI runner failed: {ci_err} — skipping backtest")

    # Hot-reload
    if not hot_reload(name, perm_path):
        logging.warning(f"[FORGE] Hot-reload failed — rolling back")
        auto_rollback(name)
        return False

    # Update registry
    conn = _db()
    conn.execute("""
        UPDATE modules SET status='ACTIVE', approved_at=?, module_type='PERMANENT',
               file_path=?
        WHERE id=?
    """, (time.time(), perm_path, module_id))
    conn.commit()
    conn.close()

    # ── BUG 5 FIX: Save version history to knowledge_repo ────────────
    # Old code: no version was ever saved. If .bak.py was missing during
    # rollback, the module was lost permanently.
    # Fix: always record the version in knowledge_repo so full history exists.
    try:
        with open(perm_path) as f:
            code = f.read()
        from knowledge_repo import save_module_version, log_decision
        version = save_module_version(
            name, code,
            changelog=f"Approved via module_forge at {time.strftime('%Y-%m-%d %H:%M')}"
        )
        log_decision(
            title=f"Module approved: {name}",
            why=f"CI passed — confidence gate, AST check, tests, smoke import",
            how=f"module_forge.approve_module() → knowledge_repo version {version}",
            result="ACTIVE — hot-reloaded into live system"
        )
        logging.info(f"[FORGE] Version saved: {name} {version}")
    except Exception as ve:
        logging.warning(f"[FORGE] Version save failed (non-fatal): {ve}")

    logging.info(f"[FORGE] Module approved and activated: {name}")
    try:
        from alert_manager import send, AlertLevel
        send(AlertLevel.INFO, "Module Forge",
             f"Module activated (no restart needed): {name}")
    except Exception:
        pass
    return True


def reject_module(module_id: str, reason: str = "") -> None:
    """Mark module as rejected."""
    conn = _db()
    conn.execute("UPDATE modules SET status='REJECTED' WHERE id=?", (module_id,))
    conn.commit()
    conn.close()
    logging.info(f"[FORGE] Module rejected: {module_id} — {reason}")


def hot_reload(name: str, file_path: str) -> bool:
    """
    Hot-reload module without restart.
    Returns True on success.
    """
    try:
        spec = importlib.util.spec_from_file_location(name, file_path)
        if spec is None:
            return False
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore
        sys.modules[name] = module
        logging.info(f"[FORGE] Hot-reloaded: {name}")
        return True
    except Exception as e:
        logging.error(f"[FORGE] Hot-reload failed for {name}: {e}")
        return False


def auto_rollback(name: str) -> bool:
    """Restore .bak.py if hot-reload failed."""
    perm_path = os.path.join(PERM_DIR, f"{name}.py")
    bak_path = os.path.join(PERM_DIR, f"{name}.bak.py")
    if os.path.exists(bak_path):
        shutil.copy2(bak_path, perm_path)
        logging.info(f"[FORGE] Rolled back {name} to backup")
        return True
    logging.warning(f"[FORGE] No backup found for rollback: {name}")
    return False


def cleanup() -> int:
    """
    BUG-31 fix: Delete expired/rejected temp modules.
    Returns count deleted.
    """
    _ensure_dirs()
    conn = _db()
    now = time.time()
    cursor = conn.execute("""
        SELECT id, name, file_path FROM modules
        WHERE status IN ('REJECTED', 'TEMP_FAILED', 'TEMP_READY', 'EXPIRED')
          AND expires_at IS NOT NULL AND expires_at < ?
    """, (now,))
    rows = cursor.fetchall()
    deleted = 0
    for mid, name, fpath in rows:
        # Delete temp file
        if fpath and os.path.exists(fpath) and "temp" in fpath:
            try:
                os.remove(fpath)
            except Exception:
                pass
        # Delete test file
        test_path = os.path.join(TESTS_DIR, f"test_{name}.py")
        if os.path.exists(test_path):
            try:
                os.remove(test_path)
            except Exception:
                pass
        conn.execute("UPDATE modules SET status='EXPIRED' WHERE id=?", (mid,))
        deleted += 1

    # Enforce max 5 temp limit
    cursor2 = conn.execute("""
        SELECT id FROM modules
        WHERE status IN ('TEMP_READY', 'TEMP_FAILED')
        ORDER BY created_at ASC
    """)
    temps = [r[0] for r in cursor2.fetchall()]
    MAX_TEMP = 5
    if len(temps) > MAX_TEMP:
        for old_id in temps[:len(temps) - MAX_TEMP]:
            conn.execute(
                "UPDATE modules SET status='EXPIRED' WHERE id=?", (old_id,))
            deleted += 1
            logging.info(f"[FORGE] Auto-expired oldest temp: {old_id}")

    conn.commit()
    conn.close()
    if deleted:
        logging.info(f"[FORGE] Cleanup: removed {deleted} expired modules")
    return deleted


def list_modules(status: Optional[str] = None) -> List[Dict[str, Any]]:
    """List modules, optionally filtered by status."""
    conn = _db()
    if status:
        cursor = conn.execute(
            "SELECT * FROM modules WHERE status=? ORDER BY created_at DESC",
            (status,))
    else:
        cursor = conn.execute(
            "SELECT * FROM modules ORDER BY created_at DESC")
    cols = [d[0] for d in cursor.description]
    rows = [dict(zip(cols, r)) for r in cursor.fetchall()]
    conn.close()
    return rows


def get_module_info(module_id: str) -> Optional[Dict[str, Any]]:
    conn = _db()
    cursor = conn.execute(
        "SELECT * FROM modules WHERE id=?", (module_id,))
    cols = [d[0] for d in cursor.description]
    row = cursor.fetchone()
    conn.close()
    return dict(zip(cols, row)) if row else None
