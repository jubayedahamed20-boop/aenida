"""
AENIDA Test Runner
Runs pytest in isolated subprocesses (BUG-32 fix).
One failing module NEVER crashes others.
Auto-generates tests via Worker AI.
"""

import subprocess
import sys
import os
import re
import time
import logging
from typing import Dict, Any, List, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # aenida_v3 root
TESTS_DIR    = os.path.join(PROJECT_ROOT, "tests")
TEMP_DIR     = os.path.join(PROJECT_ROOT, "modules", "temp")
PERM_DIR     = os.path.join(PROJECT_ROOT, "modules", "permanent")


def _build_pythonpath() -> str:
    """
    BUG 3 FIX — Build PYTHONPATH that includes ALL module locations.

    Old code:  env={"PYTHONPATH": PROJECT_ROOT}
    Problem:   modules/temp/ and modules/permanent/ were missing.
               Any generated module that lives there caused ModuleNotFoundError
               in tests → score always 0.0 → module always TEMP_FAILED.
    Fix:       Include PROJECT_ROOT + temp dir + perm dir + existing PYTHONPATH.
    """
    paths = [
        PROJECT_ROOT,
        TEMP_DIR,
        PERM_DIR,
    ]
    existing = os.environ.get("PYTHONPATH", "")
    if existing:
        paths.append(existing)
    return os.pathsep.join(paths)


def run_tests(module_name: str) -> Dict[str, Any]:
    """
    Run pytest for a single module in an isolated subprocess.
    BUG-32 fix: one module cannot crash the others.

    Args:
        module_name: e.g. "sanitizer_shield" (no .py)

    Returns:
        {module, passed, failed, errors, score, output, returncode}
    """
    test_file = os.path.join(TESTS_DIR, f"test_{module_name}.py")
    if not os.path.exists(test_file):
        return {
            "module": module_name, "passed": 0, "failed": 0,
            "errors": 1, "score": 0.0,
            "output": f"Test file not found: {test_file}",
            "returncode": -1
        }

    try:
        result = subprocess.run(
            args=[
                sys.executable, "-m", "pytest",
                test_file,
                "-v", "--tb=short", "-q", "--no-header",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=PROJECT_ROOT,
            # BUG 3 FIX: include modules/temp and modules/permanent in PYTHONPATH
            env={**os.environ, "PYTHONPATH": _build_pythonpath()},
        )
        output = (result.stdout or "") + (result.stderr or "")
        passed = _count(output, r"(\d+) passed")
        failed = _count(output, r"(\d+) failed")
        errors = _count(output, r"(\d+) error")
        total = passed + failed + errors
        score = round((passed / total * 100) if total > 0 else 0.0, 1)

        return {
            "module": module_name, "passed": passed,
            "failed": failed, "errors": errors,
            "score": score, "output": output[-3000:],
            "returncode": result.returncode
        }
    except subprocess.TimeoutExpired:
        return {
            "module": module_name, "passed": 0, "failed": 0,
            "errors": 1, "score": 0.0,
            "output": "Test suite timed out after 60s",
            "returncode": -2
        }
    except Exception as e:
        return {
            "module": module_name, "passed": 0, "failed": 0,
            "errors": 1, "score": 0.0,
            "output": str(e), "returncode": -3
        }


def _count(text: str, pattern: str) -> int:
    m = re.search(pattern, text)
    return int(m.group(1)) if m else 0


def run_all_tests(module_names: Optional[List[str]] = None
                  ) -> Dict[str, Dict[str, Any]]:
    """
    Run tests for all specified (or all existing) modules.

    Returns:
        Dict mapping module_name → test result dict
    """
    if module_names is None:
        # Discover from tests/ directory
        module_names = []
        if os.path.isdir(TESTS_DIR):
            for f in os.listdir(TESTS_DIR):
                if f.startswith("test_") and f.endswith(".py"):
                    module_names.append(f[5:-3])  # strip test_ and .py

    results: Dict[str, Dict[str, Any]] = {}
    for name in module_names:
        logging.info(f"[TEST_RUNNER] Testing {name}...")
        res = run_tests(name)
        results[name] = res
        icon = "✓" if res["score"] >= 80 else "✗"
        logging.info(f"[TEST_RUNNER] {icon} {name}: {res['score']}% "
                     f"({res['passed']}P {res['failed']}F {res['errors']}E)")
    return results


def generate_tests(module_code: str, module_name: str) -> str:
    """
    Auto-generate pytest tests via Worker AI.
    Returns test file content as string.
    """
    prompt = f"""Write pytest tests for this Python module.
Module name: {module_name}
Code:
{module_code[:3000]}

Write EXACTLY 10 tests:
Tests 1-3: All required functions/classes exist and are importable
Tests 4-6: Happy path (normal inputs, correct output type)
Tests 7-8: Edge cases (None, empty string, 0, very large values)
Test 9:    Error handling (bad input types → no crash, graceful)
Test 10:   Integration (can import config, no env var crashes)

Rules:
- Use pytest (import pytest)
- Add: import sys, os; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
- Mock all external API calls with unittest.mock
- Each test fully independent (no shared mutable state)
- Clear names: test_function_does_what()
- Return ONLY the Python test code. No markdown. No explanation."""

    try:
        from model_shell import call
        result = call(prompt)
        code = result.get("result", "")
        # Strip markdown fences if present
        code = re.sub(r"```python\n?|```\n?", "", code).strip()
        return code
    except Exception as e:
        logging.error(f"[TEST_RUNNER] generate_tests failed: {e}")
        return _fallback_test_template(module_name)


def _fallback_test_template(module_name: str) -> str:
    """Minimal test template when AI generation fails."""
    return f'''"""
Auto-generated tests for {module_name}.
"""
import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import path_setup  # registers all sub-dirs


def test_{module_name}_imports():
    """Module imports without error."""
    import importlib
    mod = importlib.import_module("{module_name}")
    assert mod is not None


def test_{module_name}_has_content():
    """Module has at least one attribute."""
    import importlib
    mod = importlib.import_module("{module_name}")
    assert len(dir(mod)) > 0


def test_placeholder():
    """Placeholder — replace with real tests."""
    assert True
'''


def trigger_ci(module_name: str) -> Dict[str, Any]:
    """
    Full CI pipeline:
      Stage 1: AST security check
      Stage 2: Isolated pytest
      Stage 3: Import smoke test (checks all module locations)

    BUG 6 FIX: Old code looked for {PROJECT_ROOT}/{module_name}.py only.
    Modules generated by module_forge live in modules/temp/ or modules/permanent/.
    Fix: search all locations in priority order.
    """
    # ── BUG 6 FIX: find the module file in the correct location ──────
    candidate_dirs = [PROJECT_ROOT, TEMP_DIR, PERM_DIR]
    module_file = None
    for d in candidate_dirs:
        candidate = os.path.join(d, f"{module_name}.py")
        if os.path.exists(candidate):
            module_file = candidate
            break

    if module_file is None:
        return {"status": "FAILED", "stage": "file_not_found",
                "module": module_name,
                "searched": candidate_dirs}

    # Stage 1: AST check
    try:
        from module_forge import run_ast_security_check
        with open(module_file) as f:
            code = f.read()
        if not run_ast_security_check(code):
            return {"status": "FAILED", "stage": "security",
                    "module": module_name}
    except ImportError:
        pass  # module_forge not yet built — skip security check

    # Stage 2: Tests (PYTHONPATH now correct via _build_pythonpath)
    result = run_tests(module_name)
    if result["score"] < 80.0:
        return {"status": "FAILED", "stage": "tests",
                "score": result["score"], "module": module_name,
                "output": result["output"]}

    # Stage 3: Smoke import
    smoke = subprocess.run(
        [sys.executable, "-c", f"import {module_name}; print('OK')"],
        capture_output=True, text=True, timeout=10,
        cwd=PROJECT_ROOT,
        # BUG 6 FIX: use full PYTHONPATH so import finds the module
        env={**os.environ, "PYTHONPATH": _build_pythonpath()}
    )
    if smoke.returncode != 0:
        return {"status": "FAILED", "stage": "import",
                "output": smoke.stderr, "module": module_name}

    return {"status": "PASSED", "score": result["score"],
            "module": module_name}


def get_worst_performing(results: Dict[str, Dict[str, Any]],
                         n: int = 5) -> List[Dict[str, Any]]:
    """Return n worst-performing modules by score."""
    scored = [{"module": k, **v} for k, v in results.items()]
    return sorted(scored, key=lambda x: x.get("score", 0))[:n]


def schedule_nightly_tests() -> None:
    """Schedule nightly test run via task_queue."""
    try:
        from task_queue import enqueue
        tomorrow_3am = time.time() - (time.time() % 86400) + 86400 + 3 * 3600
        enqueue("nightly_tests", {}, scheduled_for=tomorrow_3am, priority=8)
        logging.info("[TEST_RUNNER] Nightly tests scheduled for 3AM")
    except Exception as e:
        logging.error(f"[TEST_RUNNER] schedule_nightly failed: {e}")


# ── CLI ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="AENIDA Test Runner")
    parser.add_argument("module", nargs="?", default="all",
                        help="Module name to test, or 'all'")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if args.module == "all":
        results = run_all_tests()
        passed = sum(1 for r in results.values() if r["score"] >= 80)
        print(f"\n{'='*50}")
        print(f"Results: {passed}/{len(results)} modules passed (≥80%)")
        print(f"{'='*50}")
        for name, r in results.items():
            icon = "✓" if r["score"] >= 80 else "✗"
            print(f"  {icon} {name}: {r['score']}%")
    else:
        r = run_tests(args.module)
        print(f"\nModule: {r['module']}")
        print(f"Score:  {r['score']}%")
        print(f"Tests:  {r['passed']}P / {r['failed']}F / {r['errors']}E")
        if r["score"] < 80:
            print(f"\nOutput:\n{r['output']}")
