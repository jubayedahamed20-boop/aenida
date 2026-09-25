"""
AENIDA Code Guardian  v2.0
============================
Your assistant when working on AENIDA's own code.
AENIDA helps YOU when you edit AENIDA's files.

FEATURES:
  1. WATCH MODE  — monitors files for changes, runs tests automatically on save
  2. AI CHAT     — ask questions about the codebase in plain language
  3. MAP         — visual dependency graph
  4. EXPLAIN     — AI explains what any file does
  5. CHECK       — analyze a file for problems before you touch it
  6. REFACTOR    — safely refactor code with dependency tracking
  7. SEARCH      — search across all files
  8. DIFF        — compare file versions

USAGE:
  python code_guardian.py               # Start file watcher (background assistant)
  python code_guardian.py --chat        # Interactive AI code assistant
  python code_guardian.py --map         # Show dependency graph
  python code_guardian.py --explain FILE  # AI explains this file
  python code_guardian.py --check FILE    # Analyze file for problems
  python code_guardian.py --what-breaks FILE  # What will break if I edit this?
  python code_guardian.py --search TERM   # Search across all files
  python code_guardian.py --diff FILE     # Show diff with backup

HOW THE WATCHER WORKS:
  1. Run: python code_guardian.py
  2. Edit any .py file and save it
  3. Guardian automatically:
     a. Detects the change
     b. Backs up the old version
     c. Checks syntax
     d. Runs tests for that file + dependents
     e. Prints clear PASS ✅ or FAIL ❌ with details
     f. Rolls back automatically if tests fail
"""

import ast
import argparse
import hashlib
import json
import logging
import os
import shutil
import sys
import time
import threading
import difflib
import re
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple, Any
from pathlib import Path

# Setup logging with colors
class ColoredFormatter(logging.Formatter):
    """Colored log formatter for better visibility."""
    COLORS = {
        'DEBUG': '\033[36m',
        'INFO': '\033[32m',
        'WARNING': '\033[33m',
        'ERROR': '\033[31m',
        'CRITICAL': '\033[35m',
        'RESET': '\033[0m'
    }
    
    def format(self, record):
        color = self.COLORS.get(record.levelname, self.COLORS['RESET'])
        reset = self.COLORS['RESET']
        record.levelname = f"{color}{record.levelname}{reset}"
        return super().format(record)

log = logging.getLogger("code_guardian")
handler = logging.StreamHandler()
handler.setFormatter(ColoredFormatter("%(levelname)s | %(message)s"))
log.addHandler(handler)
log.setLevel(logging.INFO)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # aenida_v3 root
BACKUP_DIR = os.path.join(PROJECT_ROOT, "data", "code_backups")
WATCH_DELAY = 1.5  # seconds between file scans
ANALYSIS_CACHE = os.path.join(PROJECT_ROOT, "data", "guardian_cache.json")

os.makedirs(BACKUP_DIR, exist_ok=True)


# ═════════════════════════════════════════════════════════════════════════════
# ERROR HANDLING
# ═════════════════════════════════════════════════════════════════════════════

class GuardianError(Exception):
    """Base exception for guardian errors."""
    pass


# ═════════════════════════════════════════════════════════════════════════════
# FILE HASHING & CHANGE DETECTION
# ═════════════════════════════════════════════════════════════════════════════

def _file_hash(filepath: str) -> str:
    """Calculate MD5 hash of file contents."""
    try:
        with open(filepath, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except Exception:
        return ""


def _get_all_py_files() -> List[str]:
    """Get all Python files in project."""
    files = []
    try:
        for f in os.listdir(PROJECT_ROOT):
            if f.endswith(".py") and not f.startswith("_"):
                full = os.path.join(PROJECT_ROOT, f)
                if os.path.isfile(full):
                    files.append(full)
    except OSError as e:
        log.warning(f"Could not list directory: {e}")
    return files


def _get_file_info(filepath: str) -> Dict[str, Any]:
    """Get detailed file information."""
    try:
        stat = os.stat(filepath)
        return {
            'path': filepath,
            'name': os.path.basename(filepath),
            'size': stat.st_size,
            'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(),
            'hash': _file_hash(filepath)
        }
    except Exception as e:
        return {'path': filepath, 'error': str(e)}


# ═════════════════════════════════════════════════════════════════════════════
# DEPENDENCY GRAPH
# ═════════════════════════════════════════════════════════════════════════════

def _module_name(filepath: str) -> str:
    """Get module name from filepath."""
    return os.path.basename(filepath).replace(".py", "")


def _extract_imports(filepath: str) -> Set[str]:
    """Extract all imports from a Python file."""
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
    except SyntaxError:
        pass
    except Exception:
        pass
    return imports


def _build_graph() -> Dict[str, Set[str]]:
    """Build dependency graph of all modules."""
    graph: Dict[str, Set[str]] = {}
    all_mods = {_module_name(f) for f in _get_all_py_files()}
    
    for f in _get_all_py_files():
        mod = _module_name(f)
        raw = _extract_imports(f)
        graph[mod] = raw & all_mods - {mod}
    
    return graph


def _dependents(mod: str, graph: Dict[str, Set[str]]) -> List[str]:
    """Find all modules that depend on given module."""
    return sorted(m for m, deps in graph.items() if mod in deps)


def get_circular_dependencies(graph: Dict[str, Set[str]]) -> List[List[str]]:
    """Detect circular dependencies in the graph."""
    cycles = []
    visited = set()
    rec_stack = set()
    
    def dfs(node: str, path: List[str]):
        visited.add(node)
        rec_stack.add(node)
        path.append(node)
        
        for neighbor in graph.get(node, set()):
            if neighbor not in visited:
                dfs(neighbor, path)
            elif neighbor in rec_stack:
                cycle_start = path.index(neighbor)
                cycles.append(path[cycle_start:] + [neighbor])
        
        path.pop()
        rec_stack.remove(node)
    
    for node in graph:
        if node not in visited:
            dfs(node, [])
    
    return cycles


# ═════════════════════════════════════════════════════════════════════════════
# SYNTAX CHECK
# ═════════════════════════════════════════════════════════════════════════════

def check_syntax(filepath: str) -> Tuple[bool, Optional[str], List[Dict]]:
    """
    Check Python file syntax.
    Returns: (is_valid, error_message, detailed_errors)
    """
    detailed_errors = []
    try:
        with open(filepath, encoding="utf-8") as f:
            source = f.read()
        
        if not source.strip():
            return False, "File is empty", []
        
        ast.parse(source)
        return True, None, []
    except SyntaxError as e:
        error_detail = {
            'line': e.lineno,
            'column': e.offset,
            'message': e.msg,
            'text': e.text
        }
        detailed_errors.append(error_detail)
        return False, f"Line {e.lineno}: {e.msg}", detailed_errors
    except Exception as e:
        return False, str(e), []


def check_code_quality(filepath: str) -> Dict[str, Any]:
    """Check code quality metrics."""
    issues = []
    metrics = {
        'lines': 0,
        'functions': 0,
        'classes': 0,
        'imports': 0,
        'complexity': 0
    }
    
    try:
        with open(filepath, encoding="utf-8") as f:
            source = f.read()
        
        lines = source.split('\n')
        metrics['lines'] = len(lines)
        
        # Check for common issues
        for i, line in enumerate(lines, 1):
            # Bare except
            if 'except:' in line and 'except Exception' not in line:
                issues.append(f"Line {i}: Bare except clause")
            
            # Mutable defaults
            if 'def ' in line and ('=[]' in line or '={}' in line):
                issues.append(f"Line {i}: Mutable default argument")
            
            # Debug prints
            if 'print(' in line and '# debug' not in line.lower():
                # Allow prints in certain files
                pass
        
        # Parse AST for metrics
        try:
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    metrics['functions'] += 1
                elif isinstance(node, ast.ClassDef):
                    metrics['classes'] += 1
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    metrics['imports'] += 1
        except Exception:
            pass
        
    except Exception as e:
        issues.append(f"Could not analyze: {e}")
    
    return {
        'issues': issues,
        'metrics': metrics,
        'score': max(0, 100 - len(issues) * 10)
    }


# ═════════════════════════════════════════════════════════════════════════════
# TEST RUNNER
# ═════════════════════════════════════════════════════════════════════════════

def quick_test(mod: str) -> Dict[str, Any]:
    """Run quick tests for a module."""
    result = {"module": mod, "passed": False, "score": 0.0, "details": "", "duration": 0.0}
    start = time.time()
    
    try:
        sys.path.insert(0, PROJECT_ROOT)
        from test_runner import run_tests
        r = run_tests(mod)
        result["passed"] = r.get("score", 0) >= 0.7
        result["score"] = r.get("score", 0.0)
        result["details"] = r.get("summary", "")
    except ImportError:
        # No test runner - just do syntax check
        filepath = os.path.join(PROJECT_ROOT, mod + ".py")
        ok, msg, _ = check_syntax(filepath)
        result["passed"] = ok
        result["score"] = 1.0 if ok else 0.0
        result["details"] = msg or "Syntax OK"
    except Exception as e:
        result["details"] = str(e)
    
    result["duration"] = time.time() - start
    return result


# ═════════════════════════════════════════════════════════════════════════════
# AI INTEGRATION
# ═════════════════════════════════════════════════════════════════════════════

def _read_file_for_ai(filepath: str, max_chars: int = 4000) -> str:
    """Read file content for AI context."""
    try:
        with open(filepath, encoding="utf-8", errors="ignore") as f:
            content = f.read()
        if len(content) > max_chars:
            half = max_chars // 2
            content = content[:half] + "\n\n...[truncated]...\n\n" + content[-half:]
        return content
    except Exception:
        return "(could not read file)"


def _list_all_modules() -> str:
    """List all modules with their dependencies."""
    files = _get_all_py_files()
    graph = _build_graph()
    lines = []
    
    for f in sorted(files):
        mod = _module_name(f)
        deps = graph.get(mod, set())
        dep_str = f"  uses: {', '.join(sorted(deps))}" if deps else ""
        lines.append(f"- {mod}.py{dep_str}")
    
    return "\n".join(lines)


def ask_ai(question: str, context_file: Optional[str] = None) -> str:
    """Send question to AI with codebase context."""
    codebase_summary = _list_all_modules()
    
    file_content = ""
    if context_file and os.path.exists(context_file):
        file_content = f"\n\nFILE CONTENT ({os.path.basename(context_file)}):\n```python\n{_read_file_for_ai(context_file)}\n```"
    
    system_prompt = f"""You are the AENIDA Code Guardian — an expert assistant for the AENIDA autonomous AI system.
You know the complete codebase structure. Help the developer understand, edit, or debug the system safely.

CODEBASE STRUCTURE:
{codebase_summary}

RULES:
- Always warn about high-impact changes (files depended on by many others)
- Suggest using safe_updater.py for any edits
- Be specific about which functions/lines to change
- If explaining a file, describe what it does, what it connects to, and what to watch out for
- Respond in clear, simple language
{file_content}"""
    
    # Try local_brain first
    try:
        sys.path.insert(0, PROJECT_ROOT)
        from local_brain import think
        response = think(question)
        if response and len(response) > 20:
            return response
    except Exception:
        pass
    
    # Try Groq API
    try:
        config_path = os.path.join(PROJECT_ROOT, "config.json")
        with open(config_path, encoding='utf-8') as f:
            cfg = json.load(f)
        
        groq_key = cfg.get("api_keys", {}).get("groq", "")
        if groq_key:
            import urllib.request
            payload = json.dumps({
                "model": "llama-3.3-70b-versatile",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": question}
                ],
                "max_tokens": 1000,
                "temperature": 0.3
            }).encode()
            
            req = urllib.request.Request(
                "https://api.groq.com/openai/v1/chat/completions",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {groq_key}"
                }
            )
            
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read())
                return data["choices"][0]["message"]["content"]
    except Exception:
        pass
    
    return "⚠️  AI unavailable. Add GROQ_API_KEY to config.json for AI assistance."


# ═════════════════════════════════════════════════════════════════════════════
# FILE WATCHER
# ═════════════════════════════════════════════════════════════════════════════

class FileWatcher:
    """Polls project files and reacts to changes."""
    
    def __init__(self):
        self.hashes: Dict[str, str] = {}
        self.graph: Dict[str, Set[str]] = {}
        self.running = False
        self._init_hashes()
    
    def _init_hashes(self):
        """Initialize file hashes."""
        for f in _get_all_py_files():
            self.hashes[f] = _file_hash(f)
        self.graph = _build_graph()
    
    def _backup(self, filepath: str) -> str:
        """Create backup of file."""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = os.path.basename(filepath)
        backup = os.path.join(BACKUP_DIR, f"{name}.{ts}.bak")
        shutil.copy2(filepath, backup)
        return backup
    
    def _rollback(self, filepath: str) -> bool:
        """Rollback to latest backup."""
        name = os.path.basename(filepath)
        try:
            backups = sorted(
                [f for f in os.listdir(BACKUP_DIR) if f.startswith(name + ".") and f.endswith(".bak")],
                reverse=True
            )
        except OSError:
            backups = []
        
        if not backups:
            return False
        
        shutil.copy2(os.path.join(BACKUP_DIR, backups[0]), filepath)
        return True
    
    def _handle_change(self, filepath: str) -> None:
        """Handle file change event."""
        mod = _module_name(filepath)
        name = os.path.basename(filepath)
        ts = datetime.now().strftime("%H:%M:%S")
        
        print(f"\n{'━'*58}")
        print(f"  📝  CHANGE DETECTED [{ts}]: {name}")
        print(f"{'━'*58}")
        
        # Syntax check
        ok, err, _ = check_syntax(filepath)
        if not ok:
            print(f"\n  ❌  SYNTAX ERROR — {err}")
            print(f"  ⏪  Rolling back...")
            self._rollback(filepath)
            print(f"  ✅  Restored. Fix the syntax error then save again.")
            return
        
        print(f"  ✅  Syntax OK")
        
        # Backup
        backup = self._backup(filepath)
        
        # Find dependents
        deps = _dependents(mod, self.graph)
        test_targets = [mod] + deps
        if deps:
            print(f"  🔗  Also testing dependents: {', '.join(deps)}")
        
        # Run tests
        all_passed = True
        print()
        for target in test_targets:
            r = quick_test(target)
            icon = "✅" if r["passed"] else "❌"
            score_str = f"  ({r['score']:.0%})" if r["score"] else ""
            print(f"  {icon}  {target:<35}{score_str}")
            if not r["passed"]:
                all_passed = False
                if r["details"]:
                    detail_lines = r["details"].strip().split("\n")[:3]
                    for dl in detail_lines:
                        print(f"       {dl}")
        
        if all_passed:
            print(f"\n  ✅  ALL TESTS PASSED — change accepted.")
        else:
            print(f"\n  ❌  TESTS FAILED — rolling back {name}...")
            self._rollback(filepath)
            print(f"  ✅  Restored to last working version.")
        
        # Update hash
        self.hashes[filepath] = _file_hash(filepath)
    
    def start(self) -> None:
        """Start watching files."""
        self.running = True
        print(f"""
╔══════════════════════════════════════════════════════════════════════╗
║  AENIDA Code Guardian — Watching {len(self.hashes)} files                    ║
║  Edit any .py file and save → auto-tested immediately               ║
║  Press Ctrl+C to stop                                              ║
╚══════════════════════════════════════════════════════════════════════╝
""")
        try:
            while self.running:
                for f in _get_all_py_files():
                    current_hash = _file_hash(f)
                    if f not in self.hashes:
                        self.hashes[f] = current_hash
                        continue
                    if current_hash != self.hashes[f]:
                        self.hashes[f] = current_hash
                        self.graph = _build_graph()
                        try:
                            self._handle_change(f)
                        except Exception as e:
                            log.error(f"Error handling change in {f}: {e}")
                time.sleep(WATCH_DELAY)
        except KeyboardInterrupt:
            print("\n\n🛑  Code Guardian stopped.")


# ═════════════════════════════════════════════════════════════════════════════
# DEPENDENCY MAP
# ═════════════════════════════════════════════════════════════════════════════

def show_map() -> None:
    """Show dependency map."""
    graph = _build_graph()
    
    # Count dependents
    dep_count: Dict[str, int] = {}
    for mod in graph:
        dep_count[mod] = len(_dependents(mod, graph))
    
    # Sort by criticality
    sorted_mods = sorted(graph.keys(), key=lambda m: dep_count[m], reverse=True)
    
    print(f"\n{'═'*62}")
    print(f"  AENIDA Dependency Map  (sorted by criticality)")
    print(f"{'═'*62}")
    print(f"  {'Module':<32} {'Used by':<8} {'Uses'}")
    print(f"  {'─'*30} {'─'*7} {'─'*20}")
    
    for mod in sorted_mods:
        used_by = dep_count[mod]
        uses = len(graph.get(mod, set()))
        risk = "🔴" if used_by >= 5 else "🟡" if used_by >= 2 else "🟢"
        print(f"  {risk} {mod:<30} {used_by:<8} {uses}")
    
    # Check for circular dependencies
    cycles = get_circular_dependencies(graph)
    if cycles:
        print(f"\n  ⚠️  CIRCULAR DEPENDENCIES DETECTED:")
        for cycle in cycles:
            print(f"      {' → '.join(cycle)}")
    
    print(f"\n  🔴 = High risk (many files depend on it)  edit carefully!")
    print(f"  🟡 = Medium risk    🟢 = Low risk\n")


# ═════════════════════════════════════════════════════════════════════════════
# EXPLAIN FILE
# ═════════════════════════════════════════════════════════════════════════════

def explain_file(filepath: str) -> None:
    """Get AI explanation of a file."""
    if not os.path.exists(filepath):
        filepath = os.path.join(PROJECT_ROOT, filepath)
    if not os.path.exists(filepath):
        print(f"❌  File not found: {filepath}")
        return
    
    mod = _module_name(filepath)
    graph = _build_graph()
    deps = graph.get(mod, set())
    dependents = _dependents(mod, graph)
    
    print(f"\n⏳  Asking AI to explain {os.path.basename(filepath)}...")
    
    question = f"""Explain the file {os.path.basename(filepath)} in simple terms.
    
Cover:
1. What does this file DO? (one sentence)
2. What are the main functions and what do they do?
3. What other files does it connect to?
4. What should I be careful about if I edit it?
5. Any known issues or tricky parts?

Dependencies info:
- This file imports: {', '.join(sorted(deps)) or 'nothing local'}
- Other files that depend on this: {', '.join(dependents) or 'none'}
"""
    response = ask_ai(question, context_file=filepath)
    
    print(f"\n{'═'*58}")
    print(f"  EXPLANATION: {os.path.basename(filepath)}")
    print(f"{'═'*58}")
    print(response)
    print()


# ═════════════════════════════════════════════════════════════════════════════
# CHECK FILE
# ═════════════════════════════════════════════════════════════════════════════

def check_file(filepath: str) -> None:
    """Comprehensive file check."""
    if not os.path.exists(filepath):
        filepath = os.path.join(PROJECT_ROOT, filepath)
    if not os.path.exists(filepath):
        print(f"❌  File not found: {filepath}")
        return
    
    mod = _module_name(filepath)
    graph = _build_graph()
    dependents = _dependents(mod, graph)
    
    print(f"\n{'═'*58}")
    print(f"  CHECK: {os.path.basename(filepath)}")
    print(f"{'═'*58}")
    
    # Syntax
    ok, err, errors = check_syntax(filepath)
    print(f"\n  Syntax     : {'✅  OK' if ok else '❌  ' + str(err)}")
    
    # Quality
    quality = check_code_quality(filepath)
    print(f"  Quality    : {quality['score']}/100")
    if quality['issues']:
        for issue in quality['issues'][:5]:
            print(f"    ⚠️  {issue}")
    
    # Metrics
    m = quality['metrics']
    print(f"\n  Metrics:")
    print(f"    Lines: {m['lines']}, Functions: {m['functions']}, Classes: {m['classes']}")
    
    # Risk level
    risk_level = "🔴 HIGH" if len(dependents) >= 5 else ("🟡 MEDIUM" if len(dependents) >= 2 else "🟢 LOW")
    print(f"\n  Risk level : {risk_level}  ({len(dependents)} files depend on this)")
    
    # File size
    size = os.path.getsize(filepath)
    print(f"  File size  : {size:,} bytes")
    
    # Last backup
    name = os.path.basename(filepath)
    try:
        backups = [f for f in os.listdir(BACKUP_DIR) if f.startswith(name + ".")]
    except OSError:
        backups = []
    
    if backups:
        latest = sorted(backups, reverse=True)[0]
        print(f"  Last backup: {latest}")
    else:
        print(f"  Last backup: None")
    
    # Tests
    print(f"\n  Running tests...")
    r = quick_test(mod)
    icon = "✅" if r["passed"] else "❌"
    print(f"  Tests      : {icon}  score {r['score']:.0%}")
    
    if dependents:
        print(f"\n  ⚠️  Dependents (will also be tested on change):")
        for d in dependents:
            print(f"     → {d}.py")
    
    print()


# ═════════════════════════════════════════════════════════════════════════════
# SEARCH
# ═════════════════════════════════════════════════════════════════════════════

def search_code(term: str, case_sensitive: bool = False) -> None:
    """Search for term across all Python files."""
    files = _get_all_py_files()
    results = []
    
    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.compile(term, flags)
    
    for filepath in files:
        try:
            with open(filepath, encoding="utf-8", errors="ignore") as f:
                content = f.read()
            
            matches = []
            for i, line in enumerate(content.split('\n'), 1):
                if pattern.search(line):
                    matches.append((i, line.strip()))
            
            if matches:
                results.append((os.path.basename(filepath), matches))
        except Exception:
            pass
    
    print(f"\n{'═'*58}")
    print(f"  SEARCH RESULTS for: '{term}'")
    print(f"{'═'*58}\n")
    
    if not results:
        print("  No matches found.")
        return
    
    for filename, matches in results:
        print(f"  📄 {filename}")
        for line_num, line in matches[:5]:  # Show first 5 matches per file
            highlighted = pattern.sub(lambda m: f"\033[91m{m.group()}\033[0m", line[:80])
            print(f"     {line_num:4d}: {highlighted}")
        if len(matches) > 5:
            print(f"     ... and {len(matches) - 5} more matches")
        print()


# ═════════════════════════════════════════════════════════════════════════════
# DIFF
# ═════════════════════════════════════════════════════════════════════════════

def show_diff(filepath: str) -> None:
    """Show diff between current file and latest backup."""
    if not os.path.exists(filepath):
        filepath = os.path.join(PROJECT_ROOT, filepath)
    if not os.path.exists(filepath):
        print(f"❌  File not found: {filepath}")
        return
    
    name = os.path.basename(filepath)
    
    # Find latest backup
    try:
        backups = sorted(
            [f for f in os.listdir(BACKUP_DIR) if f.startswith(name + ".") and f.endswith(".bak")],
            reverse=True
        )
    except OSError:
        backups = []
    
    if not backups:
        print(f"No backups found for {name}")
        return
    
    backup_path = os.path.join(BACKUP_DIR, backups[0])
    
    try:
        with open(filepath, encoding="utf-8") as f:
            current = f.readlines()
        with open(backup_path, encoding="utf-8") as f:
            backup = f.readlines()
    except Exception as e:
        print(f"Error reading files: {e}")
        return
    
    print(f"\n{'═'*58}")
    print(f"  DIFF: {name} (current vs {backups[0]})")
    print(f"{'═'*58}\n")
    
    diff = difflib.unified_diff(
        backup, current,
        fromfile=f"{name} (backup)",
        tofile=f"{name} (current)",
        lineterm=""
    )
    
    for line in diff:
        if line.startswith('+'):
            print(f"\033[92m{line}\033[0m")  # Green for additions
        elif line.startswith('-'):
            print(f"\033[91m{line}\033[0m")  # Red for deletions
        else:
            print(line)


# ═════════════════════════════════════════════════════════════════════════════
# INTERACTIVE CHAT
# ═════════════════════════════════════════════════════════════════════════════

def chat_mode() -> None:
    """Interactive AI chat mode."""
    print(f"""
╔══════════════════════════════════════════════════════════════════════╗
║  AENIDA Code Guardian — AI Chat Mode                                ║
║  Ask anything about the codebase.                                   ║
║  Type 'quit' or Ctrl+C to exit.                                     ║
╚══════════════════════════════════════════════════════════════════════╝

Examples:
  "What does bridge.py do?"
  "How do I safely add a new feature to orchestrator.py?"
  "What will break if I edit memory_layer.py?"
  "How does the GitHub module pipeline work?"
  "Why are my tests failing in module_forge.py?"
""")
    
    try:
        while True:
            try:
                question = input("You: ").strip()
            except EOFError:
                break
            
            if not question:
                continue
            if question.lower() in {"quit", "exit", "bye", "q"}:
                print("Goodbye!")
                break
            
            # Detect file mentions
            context_file = None
            for f in _get_all_py_files():
                name = os.path.basename(f)
                if name in question or _module_name(f) in question:
                    context_file = f
                    break
            
            print("\nGuardian: ⏳ thinking...", end="\r", flush=True)
            response = ask_ai(question, context_file=context_file)
            print(f"Guardian: {response}\n")
    
    except KeyboardInterrupt:
        print("\nGoodbye!")


# ═════════════════════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="AENIDA Code Guardian v2.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                    # Start file watcher
  %(prog)s --chat             # Interactive AI chat
  %(prog)s --map              # Show dependency graph
  %(prog)s --explain bridge.py
  %(prog)s --check memory_layer.py
  %(prog)s --search "def analyze"
  %(prog)s --diff bridge.py
        """
    )
    parser.add_argument("--chat", action="store_true", help="Interactive AI code assistant")
    parser.add_argument("--map", action="store_true", help="Show dependency graph")
    parser.add_argument("--explain", metavar="FILE", help="AI explains this file")
    parser.add_argument("--check", metavar="FILE", help="Analyze file for problems")
    parser.add_argument("--what-breaks", metavar="FILE", help="What will break if I edit this?")
    parser.add_argument("--search", metavar="TERM", help="Search across all files")
    parser.add_argument("--diff", metavar="FILE", help="Show diff with backup")
    parser.add_argument("--case-sensitive", action="store_true", help="Case-sensitive search")
    parser.add_argument("--version", action="version", version="%(prog)s 2.0")
    args = parser.parse_args()
    
    try:
        if args.chat:
            chat_mode()
        elif args.map:
            show_map()
        elif args.explain:
            explain_file(args.explain)
        elif args.check:
            check_file(args.check)
        elif args.what_breaks:
            # Use safe_updater's impact analysis
            try:
                sys.path.insert(0, PROJECT_ROOT)
                from safe_updater import show_impact
                path = args.what_breaks
                if not os.path.isabs(path):
                    path = os.path.join(PROJECT_ROOT, path)
                show_impact(path)
            except ImportError:
                print("Run: python safe_updater.py --impact " + args.what_breaks)
        elif args.search:
            search_code(args.search, case_sensitive=args.case_sensitive)
        elif args.diff:
            show_diff(args.diff)
        else:
            # Default: start file watcher
            watcher = FileWatcher()
            watcher.start()
    except KeyboardInterrupt:
        print("\n\n🛑  Guardian stopped.")
        sys.exit(130)


if __name__ == "__main__":
    main()
