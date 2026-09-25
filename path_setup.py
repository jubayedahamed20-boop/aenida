"""
AENIDA v3 — Path Setup
======================
Call `import path_setup` (or `from path_setup import setup`) at the top
of any entry-point script to make every sub-package importable by its
flat module name (e.g. `from orchestrator import ...` still works even
though orchestrator.py now lives in core/).

All subdirectories are added to sys.path so existing flat-import style
across the whole codebase continues without modification.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))

_SUB_DIRS = [
    "core",
    "trading",
    "mobile",
    "tools",
    "data",
    "modules",
    "modules/temp",
    "modules/permanent",
]


def setup():
    """Insert all AENIDA sub-package directories into sys.path (idempotent)."""
    # Root first so top-level imports resolve
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)

    for sub in _SUB_DIRS:
        full = os.path.join(_ROOT, sub)
        if os.path.isdir(full) and full not in sys.path:
            sys.path.insert(0, full)


# Auto-run when the module is imported
setup()
