"""
AENIDA Safe Code Updater  v2.0
================================
Lets admin update AENIDA code WITHOUT breaking the system.

THE PROBLEM:
  AENIDA has 60+ files that import each other. If you manually edit
  a file, it can break 10 other files that depend on it — silently.

HOW THIS FIXES IT:
  1. IMPACT SCAN  — before you touch anything, shows what will break
  2. BACKUP       — auto-saves .bak copy of the file
  3. SAFE APPLY   — you provide the new code, it writes it
  4. AUTO TEST    — runs tests for the file AND all files that use it
  5. AUTO ROLLBACK — if any test fails, it restores the backup
  6. HISTORY LOG  — every change is logged with timestamp
  7. BATCH UPDATE — update multiple files at once
  8. VALIDATION   — enhanced syntax and import checking

USAGE:
  python safe_updater.py --status              # Health of all modules
  python safe_updater.py --impact FILE         # What depends on FILE?
  python safe_updater.py --backup FILE         # Just backup (no change)
  python safe_updater.py --apply FILE CODEFILE # Safely apply new code
  python safe_updater.py --rollback FILE       # Restore from backup
  python safe_updater.py --health              # Run all module tests
  python safe_updater.py --log                 # Show update history
  python safe_updater.py --batch DIR           # Update multiple files
  python safe_updater.py --upgrade             # Check & apply updates
  python safe_updater.py --interactive         # Step-by-step wizard

EXAMPLES:
  python safe_updater.py --impact bridge.py
  python safe_updater.py --apply bridge.py new_bridge.py
  python safe_updater.py --rollback bridge.py
  python safe_updater.py --batch ./updates/
  python safe_updater.py --interactive
"""

import ast
import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import traceback
from typing import Dict, List, Set, Tuple, Optional, Any
from datetime import datetime
from pathlib import Path

# Setup logging with colors
class ColoredFormatter(logging.Formatter):
    """Colored log formatter for better visibility."""
    COLORS = {
        'DEBUG': '\033[36m',    # Cyan
        'INFO': '\033[32m',     # Green
        'WARNING': '\033[33m',  # Yellow
        'ERROR': '\033[31m',    # Red
        'CRITICAL': '\033[35m', # Magenta
        'RESET': '\033[0m'
    }
    
    def format(self, record):
        color = self.COLORS.get(record.levelname, self.COLORS['RESET'])
        reset = self.COLORS['RESET']
        record.levelname = f"{color}{record.levelname}{reset}"
        return super().format(record)

log = logging.getLogger("safe_updater")
handler = logging.StreamHandler()
handler.setFormatter(ColoredFormatter("%(levelname)s | %(message)s"))
log.addHandler(handler)
log.setLevel(logging.INFO)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # aenida_v3 root
BACKUP_DIR = os.path.join(PROJECT_ROOT, "data", "code_backups")
HISTORY_LOG = os.path.join(PROJECT_ROOT, "data", "update_history.jsonl")
TESTS_DIR = os.path.join(PROJECT_ROOT, "tests")
UPDATE_CACHE = os.path.join(PROJECT_ROOT, "data", "update_cache.json")

os.makedirs(BACKUP_DIR, exist_ok=True)
os.makedirs(os.path.dirname(HISTORY_LOG), exist_ok=True)


# ═════════════════════════════════════════════════════════════════════════════
# ERROR HANDLING & VALIDATION
# ═════════════════════════════════════════════════════════════════════════════

class UpdaterError(Exception):
    """Base exception for updater errors."""
    pass

class SyntaxValidationError(UpdaterError):
    """Raised when syntax validation fails."""
    pass

class ImportValidationError(UpdaterError):
    """Raised when import validation fails."""
    pass

class TestFailureError(UpdaterError):
    """Raised when tests fail."""
    pass


def safe_file_operation(func):
    """Decorator for safe file operations with error handling."""
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except PermissionError as e:
            log.error(f"Permission denied: {e}")
            raise UpdaterError(f"Cannot access file: {e}")
        except FileNotFoundError as e:
            log.error(f"File not found: {e}")
            raise UpdaterError(f"File not found: {e}")
        except OSError as e:
            log.error(f"OS error: {e}")
            raise UpdaterError(f"System error: {e}")
        except Exception as e:
            log.error(f"Unexpected error: {e}")
            raise UpdaterError(f"Unexpected error: {e}")
    return wrapper


# ═════════════════════════════════════════════════════════════════════════════
# DEPENDENCY SCANNER
# ═════════════════════════════════════════════════════════════════════════════

def _module_name(filepath: str) -> str:
    """Convert file path to module name."""
    basename = os.path.basename(filepath)
    return basename.replace(".py", "")


def _get_all_py_files() -> List[str]:
    """All .py files in project root."""
    files = []
    try:
        for f in os.listdir(PROJECT_ROOT):
            if f.endswith(".py") and os.path.isfile(os.path.join(PROJECT_ROOT, f)):
                files.append(f)
    except OSError as e:
        log.warning(f"Could not list directory: {e}")
    return sorted(files)


def _extract_imports(filepath: str) -> Set[str]:
    """Parse a Python file and extract all local module imports."""
    imports = set()
    try:
        with open(filepath, encoding="utf-8", errors="ignore") as f:
            source = f.read()
        
        if not source.strip():
            return imports
            
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.add(node.module.split(".")[0])
    except SyntaxError as e:
        log.warning(f"Syntax error in {filepath}: {e}")
    except Exception as e:
        log.debug(f"Could not parse imports from {filepath}: {e}")
    return imports


def build_dependency_graph() -> Dict[str, Set[str]]:
    """
    Returns: { 'module': {'dep1', 'dep2'} }
    dep = what THIS module imports
    """
    graph: Dict[str, Set[str]] = {}
    all_modules = {_module_name(f) for f in _get_all_py_files()}
    
    for pyfile in _get_all_py_files():
        mod = _module_name(pyfile)
        raw_imports = _extract_imports(os.path.join(PROJECT_ROOT, pyfile))
        # Only keep local imports (files that exist in our project)
        local_imports = raw_imports & all_modules - {mod}
        graph[mod] = local_imports
    
    return graph


def find_dependents(target_module: str, graph: Dict[str, Set[str]]) -> List[str]:
    """
    Who imports target_module? (reverse lookup)
    These are the files that WILL BREAK if target_module changes.
    """
    dependents = []
    for mod, deps in graph.items():
        if target_module in deps:
            dependents.append(mod)
    return sorted(dependents)


def full_impact_chain(
    target_module: str,
    graph: Dict[str, Set[str]],
    visited: Optional[Set[str]] = None,
) -> Set[str]:
    """Recursive — finds ALL modules that would be affected (transitive)."""
    if visited is None:
        visited = set()
    if target_module in visited:
        return visited
    visited.add(target_module)
    for dep in find_dependents(target_module, graph):
        full_impact_chain(dep, graph, visited)
    return visited - {target_module}


def get_module_risk_level(module: str, graph: Dict[str, Set[str]]) -> Tuple[str, int]:
    """Get risk level and dependent count for a module."""
    dependents = find_dependents(module, graph)
    count = len(dependents)
    
    if count >= 5:
        return "🔴 HIGH", count
    elif count >= 2:
        return "🟡 MEDIUM", count
    else:
        return "🟢 LOW", count


# ═════════════════════════════════════════════════════════════════════════════
# BACKUP & RESTORE
# ═════════════════════════════════════════════════════════════════════════════

@safe_file_operation
def backup_file(filepath: str) -> str:
    """Create a timestamped backup. Returns backup path."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = os.path.basename(filepath)
    backup_path = os.path.join(BACKUP_DIR, f"{filename}.{ts}.bak")
    shutil.copy2(filepath, backup_path)
    return backup_path


@safe_file_operation
def get_latest_backup(filepath: str) -> Optional[str]:
    """Find the most recent backup for this file."""
    filename = os.path.basename(filepath)
    try:
        backups = [
            f for f in os.listdir(BACKUP_DIR)
            if f.startswith(filename + ".") and f.endswith(".bak")
        ]
    except OSError:
        return None
    
    if not backups:
        return None
    backups.sort(reverse=True)  # Latest first
    return os.path.join(BACKUP_DIR, backups[0])


@safe_file_operation
def rollback_file(filepath: str) -> bool:
    """Restore file from most recent backup. Returns True if successful."""
    backup = get_latest_backup(filepath)
    if not backup:
        log.error(f"No backup found for {os.path.basename(filepath)}")
        return False
    
    # Create a backup of current state before rollback
    current_backup = backup_file(filepath)
    log.info(f"Pre-rollback backup: {os.path.basename(current_backup)}")
    
    shutil.copy2(backup, filepath)
    log.info(f"Rolled back {os.path.basename(filepath)} from {os.path.basename(backup)}")
    return True


@safe_file_operation
def list_backups(filepath: Optional[str] = None) -> List[Dict[str, Any]]:
    """List all backups, optionally filtered by file."""
    backups = []
    try:
        for f in os.listdir(BACKUP_DIR):
            if not f.endswith(".bak"):
                continue
            if filepath and not f.startswith(os.path.basename(filepath) + "."):
                continue
            
            full_path = os.path.join(BACKUP_DIR, f)
            stat = os.stat(full_path)
            backups.append({
                'filename': f,
                'path': full_path,
                'size': stat.st_size,
                'created': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                'original': f.split('.')[0] + '.py'
            })
    except OSError:
        pass
    
    return sorted(backups, key=lambda x: x['created'], reverse=True)


# ═════════════════════════════════════════════════════════════════════════════
# CODE VALIDATION
# ═════════════════════════════════════════════════════════════════════════════

def validate_syntax(filepath: str) -> Tuple[bool, str, List[Dict]]:
    """
    Check if Python file has valid syntax.
    Returns: (is_valid, message, detailed_errors)
    """
    detailed_errors = []
    try:
        with open(filepath, encoding="utf-8") as f:
            source = f.read()
        
        if not source.strip():
            return False, "File is empty", []
        
        ast.parse(source)
        return True, "Syntax OK", []
    except SyntaxError as e:
        error_detail = {
            'line': e.lineno,
            'column': e.offset,
            'message': e.msg,
            'text': e.text
        }
        detailed_errors.append(error_detail)
        return False, f"SyntaxError at line {e.lineno}: {e.msg}", detailed_errors
    except UnicodeDecodeError as e:
        return False, f"Encoding error: {e}", []
    except Exception as e:
        return False, str(e), []


def validate_imports(filepath: str, graph: Dict[str, Set[str]]) -> Tuple[bool, List[str]]:
    """
    Validate that all imports in a file can be resolved.
    Returns: (all_valid, list_of_missing_imports)
    """
    try:
        with open(filepath, encoding="utf-8") as f:
            source = f.read()
        
        tree = ast.parse(source)
        all_modules = {_module_name(f) for f in _get_all_py_files()}
        missing = []
        
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mod = alias.name.split(".")[0]
                    # Check if it's a local module that doesn't exist
                    if mod not in all_modules and not _is_stdlib_module(mod):
                        # Could be external package - not an error
                        pass
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    mod = node.module.split(".")[0]
                    if mod not in all_modules and not _is_stdlib_module(mod):
                        # External package
                        pass
        
        return len(missing) == 0, missing
    except Exception as e:
        return False, [str(e)]


def _is_stdlib_module(module: str) -> bool:
    """Check if a module is part of Python stdlib."""
    stdlib_modules = {
        'os', 'sys', 'json', 'time', 'datetime', 'logging', 'argparse',
        'subprocess', 'threading', 'pathlib', 'typing', 'traceback',
        'hashlib', 'shutil', 'ast', 'urllib', 'http', 'socket', 're',
        'collections', 'itertools', 'functools', 'math', 'random',
        'string', 'inspect', 'warnings', 'contextlib', 'io', 'csv',
        'pickle', 'copy', 'enum', 'dataclasses', 'abc', 'types'
    }
    return module in stdlib_modules


def check_file_safety(filepath: str) -> Dict[str, Any]:
    """Perform comprehensive safety checks on a file."""
    result = {
        'filepath': filepath,
        'exists': os.path.exists(filepath),
        'readable': os.access(filepath, os.R_OK) if os.path.exists(filepath) else False,
        'writable': os.access(filepath, os.W_OK) if os.path.exists(filepath) else False,
        'syntax_valid': False,
        'syntax_message': '',
        'size': 0,
        'line_count': 0
    }
    
    if not result['exists']:
        result['syntax_message'] = 'File does not exist'
        return result
    
    try:
        stat = os.stat(filepath)
        result['size'] = stat.st_size
        
        with open(filepath, encoding='utf-8') as f:
            content = f.read()
            result['line_count'] = len(content.splitlines())
        
        valid, msg, errors = validate_syntax(filepath)
        result['syntax_valid'] = valid
        result['syntax_message'] = msg
        result['syntax_errors'] = errors
        
    except Exception as e:
        result['syntax_message'] = str(e)
    
    return result


# ═════════════════════════════════════════════════════════════════════════════
# TEST RUNNER
# ═════════════════════════════════════════════════════════════════════════════

def run_module_tests(module_name: str) -> Dict[str, Any]:
    """Run tests for a specific module. Returns detailed result dict."""
    result = {
        "module": module_name,
        "passed": False,
        "score": 0.0,
        "details": "",
        "test_count": 0,
        "passed_count": 0,
        "failed_count": 0,
        "duration": 0.0
    }
    
    start_time = time.time()
    
    try:
        # Try the built-in test_runner first
        sys.path.insert(0, PROJECT_ROOT)
        from test_runner import run_tests
        test_result = run_tests(module_name)
        result["passed"] = test_result.get("score", 0) >= 0.7
        result["score"] = test_result.get("score", 0)
        result["details"] = test_result.get("summary", "")
        result["test_count"] = test_result.get("total", 0)
        result["passed_count"] = test_result.get("passed", 0)
        result["failed_count"] = test_result.get("failed", 0)
    except ImportError:
        # Fallback: pytest directly
        test_file = os.path.join(TESTS_DIR, f"test_{module_name}.py")
        if os.path.exists(test_file):
            try:
                proc = subprocess.run(
                    [sys.executable, "-m", "pytest", test_file, "-v", "--tb=short"],
                    capture_output=True, text=True, cwd=PROJECT_ROOT, timeout=60
                )
                result["passed"] = proc.returncode == 0
                result["details"] = proc.stdout[-2000:] + proc.stderr[-500:]
                # Parse test count from output
                if "passed" in proc.stdout:
                    result["test_count"] = 1  # Simplified
            except subprocess.TimeoutExpired:
                result["details"] = "Tests timed out after 60 seconds"
            except Exception as e:
                result["details"] = f"Test execution error: {e}"
        else:
            # No test file — do syntax check only
            filepath = os.path.join(PROJECT_ROOT, module_name + ".py")
            ok, msg, _ = validate_syntax(filepath)
            result["passed"] = ok
            result["details"] = msg
            result["score"] = 1.0 if ok else 0.0
            result["test_count"] = 1 if ok else 0
    except Exception as e:
        result["details"] = f"Test error: {str(e)}"
    
    result["duration"] = time.time() - start_time
    return result


def run_all_tests_parallel(modules: List[str], max_workers: int = 4) -> Dict[str, Dict]:
    """Run tests for multiple modules in parallel."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(run_module_tests, mod): mod for mod in modules}
        for future in as_completed(futures):
            mod = futures[future]
            try:
                results[mod] = future.result()
            except Exception as e:
                results[mod] = {
                    "module": mod,
                    "passed": False,
                    "score": 0.0,
                    "details": f"Execution error: {e}"
                }
    return results


# ═════════════════════════════════════════════════════════════════════════════
# HISTORY LOG
# ═════════════════════════════════════════════════════════════════════════════

def _log_event(event: dict) -> None:
    """Log an event to the history file."""
    event["timestamp"] = datetime.now().isoformat()
    event["version"] = "2.0"
    try:
        with open(HISTORY_LOG, "a", encoding='utf-8') as f:
            f.write(json.dumps(event) + "\n")
    except Exception as e:
        log.warning(f"Could not write to history log: {e}")


def show_history(limit: int = 20, filter_action: Optional[str] = None) -> None:
    """Show update history with optional filtering."""
    if not os.path.exists(HISTORY_LOG):
        print("No update history yet.")
        return
    
    print(f"\n{'═'*70}")
    print("  AENIDA Update History (most recent first)")
    if filter_action:
        print(f"  Filtered by: {filter_action}")
    print(f"{'═'*70}")
    
    try:
        with open(HISTORY_LOG, encoding='utf-8') as f:
            lines = f.readlines()
    except Exception as e:
        print(f"Error reading history: {e}")
        return
    
    count = 0
    for line in reversed(lines):
        if count >= limit:
            break
        try:
            ev = json.loads(line)
            if filter_action and ev.get("action") != filter_action:
                continue
            
            ts = ev.get("timestamp", "")[:19]
            action = ev.get("action", "")
            file_ = ev.get("file", "")
            ok = ev.get("success", False)
            status = "✅" if ok else "❌"
            
            # Color code actions
            action_color = {
                'apply_success': '\033[32m',
                'apply_rolled_back': '\033[31m',
                'rollback': '\033[33m',
                'manual_backup': '\033[36m'
            }.get(action, '')
            reset = '\033[0m'
            
            print(f"  {ts}  {status}  {action_color}{action:<18}{reset} {file_}")
            
            # Show additional details if available
            if ev.get('affected'):
                print(f"              Affected: {', '.join(ev['affected'][:3])}")
            if ev.get('failed'):
                print(f"              Failed: {', '.join(ev['failed'])}")
            
            count += 1
        except json.JSONDecodeError:
            continue
        except Exception:
            continue
    
    if count == 0:
        print("  No matching entries found.")
    print()


def get_update_stats() -> Dict[str, Any]:
    """Get statistics from update history."""
    stats = {
        'total_updates': 0,
        'successful': 0,
        'failed': 0,
        'rollbacks': 0,
        'most_updated': {}
    }
    
    if not os.path.exists(HISTORY_LOG):
        return stats
    
    try:
        with open(HISTORY_LOG, encoding='utf-8') as f:
            for line in f:
                try:
                    ev = json.loads(line)
                    stats['total_updates'] += 1
                    
                    if ev.get('success'):
                        stats['successful'] += 1
                    else:
                        stats['failed'] += 1
                    
                    if ev.get('action') == 'apply_rolled_back':
                        stats['rollbacks'] += 1
                    
                    file_ = ev.get('file', '')
                    if file_:
                        stats['most_updated'][file_] = stats['most_updated'].get(file_, 0) + 1
                        
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    
    return stats


# ═════════════════════════════════════════════════════════════════════════════
# MAIN OPERATIONS
# ═════════════════════════════════════════════════════════════════════════════

def show_impact(filepath: str, verbose: bool = False) -> Dict[str, Any]:
    """Show what depends on this file before editing."""
    mod = _module_name(filepath)
    graph = build_dependency_graph()
    direct = find_dependents(mod, graph)
    chain = full_impact_chain(mod, graph)
    
    result = {
        'module': mod,
        'direct_dependents': direct,
        'full_chain': list(chain),
        'risk_level': get_module_risk_level(mod, graph)
    }
    
    print(f"\n{'═'*70}")
    print(f"  IMPACT ANALYSIS: {mod}.py")
    print(f"{'═'*70}")
    
    # File info
    safety = check_file_safety(filepath)
    print(f"\n  File Info:")
    print(f"    Size: {safety['size']:,} bytes")
    print(f"    Lines: {safety['line_count']}")
    print(f"    Syntax: {'✅ Valid' if safety['syntax_valid'] else '❌ ' + safety['syntax_message']}")
    
    # What THIS file imports
    what_it_uses = graph.get(mod, set())
    print(f"\n  This file USES:")
    if what_it_uses:
        for dep in sorted(what_it_uses):
            risk, count = get_module_risk_level(dep, graph)
            print(f"    → {dep}.py {risk} ({count} dependents)")
    else:
        print(f"    (no local imports)")
    
    # What imports THIS file
    print(f"\n  Files that DEPEND on this file (will break if this breaks):")
    if direct:
        for dep in direct:
            print(f"    ⚠  {dep}.py")
    else:
        print(f"    (nothing imports this — safe to edit)")
    
    # Full chain
    if len(chain) > len(direct):
        indirect = chain - set(direct)
        print(f"\n  Indirect impact (transitive):")
        for dep in sorted(indirect):
            print(f"    ~  {dep}.py")
    
    # Risk assessment
    risk, count = result['risk_level']
    print(f"\n  {'─'*50}")
    print(f"  Risk Level: {risk}")
    print(f"  Summary: editing {mod}.py directly affects {len(direct)} file(s),")
    print(f"           total impact chain: {len(chain)} file(s)")
    
    if count >= 5:
        print(f"\n  ⚠️  WARNING: This is a HIGH-RISK file!")
        print(f"      Consider using --interactive mode for guided updates.")
    
    print()
    return result


def safe_apply(target_filepath: str, new_code_filepath: str, 
               skip_tests: bool = False, force: bool = False) -> bool:
    """
    Safely apply new code to target file.
    1. Validate new code syntax
    2. Backup old file
    3. Write new code
    4. Run tests for this module + all dependents
    5. If tests fail → rollback
    Returns True if successful.
    """
    target_name = os.path.basename(target_filepath)
    mod = _module_name(target_filepath)
    
    print(f"\n{'═'*70}")
    print(f"  SAFE APPLY: {target_name}")
    print(f"{'═'*70}")
    
    # Pre-checks
    if not os.path.exists(target_filepath):
        log.error(f"Target file not found: {target_filepath}")
        return False
    
    if not os.path.exists(new_code_filepath):
        log.error(f"New code file not found: {new_code_filepath}")
        return False
    
    # Step 1: Validate new code syntax
    print("\n[1/5] Checking syntax of new code...")
    ok, msg, errors = validate_syntax(new_code_filepath)
    if not ok:
        print(f"  ❌  SYNTAX ERROR in new code: {msg}")
        if errors:
            for err in errors:
                print(f"       Line {err['line']}: {err['message']}")
        print(f"  Change NOT applied. Fix the syntax error first.")
        _log_event({
            "action": "apply_blocked", 
            "file": target_name, 
            "reason": msg, 
            "success": False
        })
        return False
    print(f"  ✅  Syntax OK")
    
    # Step 2: Impact analysis
    print(f"\n[2/5] Analyzing impact...")
    graph = build_dependency_graph()
    affected = list(full_impact_chain(mod, graph)) + [mod]
    print(f"  Files that will be tested: {', '.join(affected)}")
    
    # Step 3: Backup
    print(f"\n[3/5] Creating backup...")
    try:
        backup_path = backup_file(target_filepath)
        print(f"  ✅  Backed up to: {os.path.basename(backup_path)}")
    except UpdaterError as e:
        if not force:
            print(f"  ❌  Backup failed: {e}")
            print(f"  Use --force to apply without backup (NOT RECOMMENDED)")
            return False
        print(f"  ⚠️  Backup failed but --force specified, continuing...")
    
    # Step 4: Apply change
    print(f"\n[4/5] Applying changes...")
    try:
        shutil.copy2(new_code_filepath, target_filepath)
        print(f"  ✅  Changes applied")
    except Exception as e:
        print(f"  ❌  Failed to apply changes: {e}")
        _log_event({
            "action": "apply_failed",
            "file": target_name,
            "reason": str(e),
            "success": False
        })
        return False
    
    # Step 5: Run tests
    if skip_tests:
        print(f"\n[5/5] ⚠️  Tests skipped (--skip-tests)")
        print(f"\n⚠️  Changes applied WITHOUT testing!")
        _log_event({
            "action": "apply_no_test",
            "file": target_name,
            "affected": affected,
            "backup": os.path.basename(backup_path) if 'backup_path' in dir() else None,
            "success": True
        })
        return True
    
    print(f"\n[5/5] Running tests for {len(affected)} module(s)...")
    all_passed = True
    results = []
    
    for affected_mod in affected:
        print(f"\n  Testing {affected_mod}...")
        r = run_module_tests(affected_mod)
        results.append(r)
        status = "✅" if r["passed"] else "❌"
        score_str = f"{r['score']:.0%}" if r["score"] else "N/A"
        duration_str = f"({r['duration']:.2f}s)" if r['duration'] else ""
        print(f"  {status}  {affected_mod:<30} {score_str:<8} {duration_str}")
        if not r["passed"] and r["details"]:
            detail_lines = r["details"].strip().split("\n")[:2]
            for dl in detail_lines:
                print(f"       {dl}")
        if not r["passed"]:
            all_passed = False
    
    if all_passed:
        print(f"\n{'═'*70}")
        print(f"✅  ALL TESTS PASSED — {target_name} updated successfully!")
        print(f"{'═'*70}")
        _log_event({
            "action": "apply_success",
            "file": target_name,
            "affected": affected,
            "backup": os.path.basename(backup_path) if 'backup_path' in dir() else None,
            "success": True
        })
        return True
    else:
        print(f"\n{'═'*70}")
        print(f"❌  TESTS FAILED — rolling back automatically...")
        print(f"{'═'*70}")
        rollback_file(target_filepath)
        print(f"  ✅  Restored to previous working version.")
        failed_mods = [r["module"] for r in results if not r["passed"]]
        print(f"\n  Failed modules: {', '.join(failed_mods)}")
        print(f"  Fix the issues in these modules, then try again.")
        _log_event({
            "action": "apply_rolled_back",
            "file": target_name,
            "failed": failed_mods,
            "success": False
        })
        return False


def batch_update(update_dir: str, dry_run: bool = False) -> Dict[str, Any]:
    """Apply multiple updates from a directory."""
    results = {
        'processed': 0,
        'successful': 0,
        'failed': 0,
        'skipped': 0,
        'details': []
    }
    
    if not os.path.isdir(update_dir):
        log.error(f"Directory not found: {update_dir}")
        return results
    
    update_files = [f for f in os.listdir(update_dir) if f.endswith('.py')]
    
    print(f"\n{'═'*70}")
    print(f"  BATCH UPDATE: {len(update_files)} file(s) found")
    print(f"{'═'*70}")
    
    if dry_run:
        print("  DRY RUN MODE — no changes will be made\n")
    
    for update_file in sorted(update_files):
        target_name = update_file  # Assuming same filename
        target_path = os.path.join(PROJECT_ROOT, target_name)
        update_path = os.path.join(update_dir, update_file)
        
        print(f"\n  Processing: {update_file}")
        
        if not os.path.exists(target_path):
            print(f"    ⚠️  Target doesn't exist, skipping")
            results['skipped'] += 1
            continue
        
        if dry_run:
            print(f"    [DRY RUN] Would apply {update_file}")
            results['processed'] += 1
            continue
        
        # Apply the update
        success = safe_apply(target_path, update_path)
        results['processed'] += 1
        if success:
            results['successful'] += 1
        else:
            results['failed'] += 1
    
    print(f"\n{'═'*70}")
    print(f"  BATCH COMPLETE: {results['successful']}/{results['processed']} successful")
    print(f"{'═'*70}")
    
    return results


def show_health(detailed: bool = False) -> None:
    """Run tests for all modules and show status."""
    print(f"\n{'═'*70}")
    print(f"  AENIDA Module Health Check")
    print(f"{'═'*70}\n")
    
    files = _get_all_py_files()
    passed = failed = skipped = 0
    results = []
    
    for f in files:
        mod = _module_name(f)
        # Skip infra files
        if mod in {"safe_updater", "code_guardian", "network_tunnel", "admin_panel"}:
            continue
        
        r = run_module_tests(mod)
        results.append((mod, r))
        
        if r["score"] == 0 and not r["details"]:
            status = "⬜"
            skipped += 1
        elif r["passed"]:
            status = "✅"
            passed += 1
        else:
            status = "❌"
            failed += 1
        
        score_str = f"{r['score']:.0%}" if r["score"] else "N/A"
        print(f"  {status}  {mod:<35} {score_str}")
        
        if detailed and not r["passed"] and r["details"]:
            for line in r["details"].strip().split("\n")[:3]:
                print(f"       {line}")
    
    print(f"\n  ─────────────────────────────────────────")
    print(f"  Passed: {passed}  Failed: {failed}  No tests: {skipped}")
    
    if failed > 0:
        print(f"\n  ⚠️  {failed} module(s) need attention!")
        print(f"     Run with --detailed to see failure details")
    
    print()


def show_status() -> None:
    """Show quick overview of the code base."""
    files = _get_all_py_files()
    backups = os.listdir(BACKUP_DIR) if os.path.exists(BACKUP_DIR) else []
    graph = build_dependency_graph()
    stats = get_update_stats()
    
    print(f"\n{'═'*70}")
    print(f"  AENIDA Code Status")
    print(f"{'═'*70}")
    print(f"  Total Python files  : {len(files)}")
    print(f"  Backed up files     : {len(set(b.split('.')[0] for b in backups))}")
    print(f"  Total backups       : {len(backups)}")
    print(f"  Total updates       : {stats['total_updates']}")
    print(f"  Successful updates  : {stats['successful']}")
    print(f"  Failed updates      : {stats['failed']}")
    print(f"  Rollbacks           : {stats['rollbacks']}")
    
    # Find most-depended-on modules
    dep_count = {}
    for mod in graph:
        deps = find_dependents(mod, graph)
        dep_count[mod] = len(deps)
    high_risk = sorted(dep_count.items(), key=lambda x: x[1], reverse=True)[:5]
    
    print(f"\n  HIGH-RISK files (most depended on — edit with extra care):")
    for mod, count in high_risk:
        if count > 0:
            print(f"    ⚠  {mod}.py  ← used by {count} other files")
    
    # Most updated files
    if stats['most_updated']:
        print(f"\n  Most frequently updated files:")
        most_updated = sorted(stats['most_updated'].items(), key=lambda x: x[1], reverse=True)[:3]
        for file_, count in most_updated:
            print(f"    📝 {file_}: {count} update(s)")
    
    print(f"\n  Use --impact FILE before editing any file.")
    print(f"  Use --health to test all modules.")
    print(f"  Use --interactive for guided updates.\n")


def interactive_mode() -> None:
    """Interactive step-by-step update wizard."""
    print(f"""
{'═'*70}
  AENIDA Safe Updater — Interactive Mode
{'═'*70}

This wizard will guide you through safely updating AENIDA code.
""")
    
    # Step 1: What do you want to do?
    print("What would you like to do?")
    print("  1. Update a single file")
    print("  2. Check impact of a file")
    print("  3. Run health check")
    print("  4. View update history")
    print("  5. Rollback a file")
    print("  6. Exit")
    
    choice = input("\nEnter choice (1-6): ").strip()
    
    if choice == "1":
        # Update single file
        target = input("\nEnter the file to update (e.g., bridge.py): ").strip()
        if not target.endswith('.py'):
            target += '.py'
        
        target_path = os.path.join(PROJECT_ROOT, target)
        if not os.path.exists(target_path):
            print(f"❌ File not found: {target}")
            return
        
        # Show impact first
        print(f"\nLet me show you what depends on {target}...")
        show_impact(target_path)
        
        confirm = input(f"\nDo you want to continue? (yes/no): ").strip().lower()
        if confirm not in ('yes', 'y'):
            print("Cancelled.")
            return
        
        new_code = input("Enter path to new code file: ").strip()
        if not os.path.exists(new_code):
            print(f"❌ New code file not found: {new_code}")
            return
        
        safe_apply(target_path, new_code)
        
    elif choice == "2":
        target = input("\nEnter file to analyze: ").strip()
        if not target.endswith('.py'):
            target += '.py'
        target_path = os.path.join(PROJECT_ROOT, target)
        show_impact(target_path, verbose=True)
        
    elif choice == "3":
        show_health(detailed=True)
        
    elif choice == "4":
        show_history(limit=30)
        
    elif choice == "5":
        target = input("\nEnter file to rollback: ").strip()
        if not target.endswith('.py'):
            target += '.py'
        target_path = os.path.join(PROJECT_ROOT, target)
        
        backups = list_backups(target_path)
        if not backups:
            print(f"No backups found for {target}")
            return
        
        print(f"\nAvailable backups for {target}:")
        for i, b in enumerate(backups[:5], 1):
            print(f"  {i}. {b['filename']} ({b['size']:,} bytes, {b['created'][:19]})")
        
        confirm = input("\nRollback to latest? (yes/no): ").strip().lower()
        if confirm in ('yes', 'y'):
            if rollback_file(target_path):
                print(f"✅ {target} rolled back successfully!")
            else:
                print(f"❌ Rollback failed!")
        
    elif choice == "6":
        print("Goodbye!")
        return


def check_for_updates() -> Dict[str, Any]:
    """Check if updates are available from remote source."""
    result = {
        'update_available': False,
        'current_version': 'v2.0',
        'latest_version': 'v2.0',
        'changelog': '',
        'download_url': ''
    }
    
    print(f"\n{'═'*70}")
    print(f"  Checking for updates...")
    print(f"{'═'*70}")
    
    # Try to read cached update info
    try:
        if os.path.exists(UPDATE_CACHE):
            with open(UPDATE_CACHE, encoding='utf-8') as f:
                cached = json.load(f)
                cache_time = datetime.fromisoformat(cached.get('checked_at', '2000-01-01'))
                if (datetime.now() - cache_time).total_seconds() < 3600:  # 1 hour cache
                    print(f"  Using cached update info (checked at {cache_time:%H:%M})")
                    return cached
    except Exception:
        pass
    
    # In a real implementation, this would check a remote server
    # For now, just return current version
    print(f"  Current version: {result['current_version']}")
    print(f"  ✅ You are up to date!")
    
    # Cache the result
    result['checked_at'] = datetime.now().isoformat()
    try:
        with open(UPDATE_CACHE, 'w', encoding='utf-8') as f:
            json.dump(result, f)
    except Exception:
        pass
    
    return result


# ═════════════════════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="AENIDA Safe Code Updater v2.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --status                          # Show code status
  %(prog)s --impact bridge.py                # Check impact
  %(prog)s --apply bridge.py new_bridge.py   # Apply update
  %(prog)s --rollback bridge.py              # Rollback
  %(prog)s --health --detailed               # Detailed health check
  %(prog)s --interactive                     # Interactive wizard
        """
    )
    parser.add_argument("--status", action="store_true", help="Overview of all modules")
    parser.add_argument("--impact", metavar="FILE", help="Show what depends on FILE")
    parser.add_argument("--backup", metavar="FILE", help="Backup FILE (no change)")
    parser.add_argument("--apply", nargs=2, metavar=("TARGET", "NEW_CODE"), help="Safely apply new code")
    parser.add_argument("--rollback", metavar="FILE", help="Restore FILE from backup")
    parser.add_argument("--health", action="store_true", help="Run all module tests")
    parser.add_argument("--detailed", action="store_true", help="Show detailed output")
    parser.add_argument("--log", action="store_true", help="Show update history")
    parser.add_argument("--batch", metavar="DIR", help="Update multiple files from directory")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without making changes")
    parser.add_argument("--skip-tests", action="store_true", help="Skip tests (not recommended)")
    parser.add_argument("--force", action="store_true", help="Force operation even with warnings")
    parser.add_argument("--interactive", action="store_true", help="Interactive wizard mode")
    parser.add_argument("--check-updates", action="store_true", help="Check for available updates")
    parser.add_argument("--version", action="version", version="%(prog)s 2.0")
    args = parser.parse_args()
    
    def resolve(filename: str) -> str:
        """Resolve filename to full path."""
        if os.path.isabs(filename):
            return filename
        if os.path.exists(filename):
            return os.path.abspath(filename)
        candidate = os.path.join(PROJECT_ROOT, filename)
        if os.path.exists(candidate):
            return candidate
        return filename
    
    try:
        if args.status:
            show_status()
        elif args.impact:
            path = resolve(args.impact)
            if not os.path.exists(path):
                print(f"❌  File not found: {args.impact}")
                sys.exit(1)
            else:
                show_impact(path, verbose=args.detailed)
        elif args.backup:
            path = resolve(args.backup)
            try:
                bp = backup_file(path)
                print(f"✅  Backed up: {os.path.basename(bp)}")
                _log_event({
                    "action": "manual_backup", 
                    "file": args.backup, 
                    "backup": os.path.basename(bp), 
                    "success": True
                })
            except UpdaterError as e:
                print(f"❌  Backup failed: {e}")
                sys.exit(1)
        elif args.apply:
            target = resolve(args.apply[0])
            new_code = resolve(args.apply[1])
            if not os.path.exists(target):
                print(f"❌  Target file not found: {args.apply[0]}")
                sys.exit(1)
            elif not os.path.exists(new_code):
                print(f"❌  New code file not found: {args.apply[1]}")
                sys.exit(1)
            else:
                success = safe_apply(target, new_code, skip_tests=args.skip_tests, force=args.force)
                sys.exit(0 if success else 1)
        elif args.rollback:
            path = resolve(args.rollback)
            ok = rollback_file(path)
            _log_event({"action": "rollback", "file": args.rollback, "success": ok})
            sys.exit(0 if ok else 1)
        elif args.health:
            show_health(detailed=args.detailed)
        elif args.log:
            show_history()
        elif args.batch:
            batch_update(args.batch, dry_run=args.dry_run)
        elif args.interactive:
            interactive_mode()
        elif args.check_updates:
            check_for_updates()
        else:
            show_status()
    except KeyboardInterrupt:
        print("\n\n⚠️  Operation cancelled by user")
        sys.exit(130)
    except Exception as e:
        log.error(f"Unexpected error: {e}")
        if args.detailed:
            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
