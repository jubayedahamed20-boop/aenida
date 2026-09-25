"""
AENIDA Knowledge Repository
Internal 'GitHub' — version-controlled decisions, patterns, module history.
5 GB size cap (BUG-23). export_for_ai() for any AI to read.
"""

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

BASE = "knowledge"


def _ensure(path: str) -> None:
    os.makedirs(path, exist_ok=True)


class KnowledgeRepo:
    """Manages structured knowledge in the filesystem."""

    __slots__ = ["base_dir", "max_gb"]

    def __init__(self, base_dir: str = BASE, max_gb: float = 5.0):
        self.base_dir = base_dir
        self.max_gb = max_gb
        for sub in ("modules", "patterns", "decisions", "improvements"):
            _ensure(os.path.join(base_dir, sub))
        # Initialise improvements files
        for fname in ("suggested.md", "in_progress.md", "completed.md"):
            fpath = os.path.join(base_dir, "improvements", fname)
            if not os.path.exists(fpath):
                with open(fpath, "w") as f:
                    f.write(f"# {fname.replace('.md','').title()}\n\n")

    # ── Decision logging ──────────────────────────────────────────
    def log_decision(self, title: str, why: str,
                     how: str, result: str = "") -> None:
        """Write a dated decision log entry."""
        ts = time.strftime("%Y-%m-%d_%H-%M")
        safe = title.lower().replace(" ", "_")[:40]
        fname = f"{ts}_{safe}.md"
        path = os.path.join(self.base_dir, "decisions", fname)
        content = (
            f"# {title}\n\n"
            f"**Date:** {time.strftime('%Y-%m-%d %H:%M')}\n\n"
            f"## Why\n{why}\n\n"
            f"## How\n{how}\n\n"
            f"## Result\n{result or 'Pending'}\n"
        )
        with open(path, "w") as f:
            f.write(content)
        logging.info(f"[KNOWLEDGE] Decision logged: {title}")
        self._enforce_size_cap()

    # ── Module versioning ─────────────────────────────────────────
    def save_module_version(self, name: str, code: str,
                             changelog: str = "") -> str:
        """Save versioned module snapshot. Returns version string."""
        mod_dir = os.path.join(self.base_dir, "modules")
        # Find current version
        existing = [f for f in os.listdir(mod_dir)
                    if f.startswith(f"{name}.v")]
        version = len(existing) + 1
        vname = f"{name}.v{version}.py"
        with open(os.path.join(mod_dir, vname), "w") as f:
            f.write(f'"""\nVersion {version} | {time.strftime("%Y-%m-%d")}\n')
            f.write(f'Changelog: {changelog}\n"""\n\n')
            f.write(code)
        # Also write/overwrite current
        with open(os.path.join(mod_dir, f"{name}.current.py"), "w") as f:
            f.write(code)
        logging.info(f"[KNOWLEDGE] Module version saved: {name} v{version}")
        return f"v{version}"

    # ── Pattern recording ─────────────────────────────────────────
    def record_pattern(self, ptype: str, name: str,
                        description: str, data: Any = None) -> None:
        """
        ptype: 'failure' | 'success' | 'routing'
        Appends to patterns/{ptype}_patterns.json
        """
        valid = ("failure", "success", "routing")
        if ptype not in valid:
            ptype = "success"
        fpath = os.path.join(self.base_dir, "patterns",
                              f"{ptype}_patterns.json")
        patterns: List[Dict] = []
        if os.path.exists(fpath):
            try:
                with open(fpath) as f:
                    patterns = json.load(f)
            except Exception:
                patterns = []
        patterns.append({
            "name": name,
            "description": description,
            "data": data,
            "recorded_at": time.strftime("%Y-%m-%d %H:%M"),
        })
        with open(fpath, "w") as f:
            json.dump(patterns, f, indent=2, default=str)

    def get_patterns(self, ptype: str) -> List[Dict]:
        fpath = os.path.join(self.base_dir, "patterns",
                              f"{ptype}_patterns.json")
        if not os.path.exists(fpath):
            return []
        try:
            with open(fpath) as f:
                return json.load(f)
        except Exception:
            return []

    # ── System state snapshot ─────────────────────────────────────
    def snapshot_system_state(self) -> str:
        """Save a full system state snapshot. Returns version tag."""
        ts = time.strftime("%Y-%m-%d_%H-%M")
        snap: Dict[str, Any] = {
            "timestamp": ts,
            "files": self._list_modules(),
        }
        try:
            from performance_learner import get_stats
            snap["performance"] = get_stats()
        except Exception:
            pass
        try:
            from worker_registry import get_stats as wstats
            snap["workers"] = wstats()
        except Exception:
            pass

        fpath = os.path.join(self.base_dir, f"snapshot_{ts}.json")
        with open(fpath, "w") as f:
            json.dump(snap, f, indent=2, default=str)
        return ts

    # ── Search ────────────────────────────────────────────────────
    def search(self, query: str) -> List[Dict[str, Any]]:
        """Simple keyword search across all text files."""
        query_lower = query.lower()
        results: List[Dict] = []
        for root, _, files in os.walk(self.base_dir):
            for fname in files:
                if not (fname.endswith(".md") or fname.endswith(".json")):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, errors="ignore") as f:
                        content = f.read()
                    if query_lower in content.lower():
                        results.append({
                            "file": fname,
                            "path": fpath,
                            "snippet": content[:200],
                        })
                except Exception:
                    pass
        return results

    def get_module_history(self, name: str) -> List[Dict]:
        mod_dir = os.path.join(self.base_dir, "modules")
        versions = sorted(
            f for f in os.listdir(mod_dir)
            if f.startswith(f"{name}.v")
        )
        result = []
        for v in versions:
            fpath = os.path.join(mod_dir, v)
            result.append({
                "version": v,
                "path": fpath,
                "size_bytes": os.path.getsize(fpath),
                "modified": time.ctime(os.path.getmtime(fpath)),
            })
        return result

    # ── Export for AI (data_exporter helper) ─────────────────────
    def export_for_ai(self, format: str = "markdown",
                       max_tokens: int = 50000) -> str:
        """
        Export full knowledge as clean markdown.
        Ready to paste into any AI for analysis.
        """
        lines = [
            "# AENIDA System Knowledge Export",
            f"Generated: {time.strftime('%Y-%m-%d %H:%M')}",
            "",
            "## Module Versions",
        ]
        for fname in self._list_modules():
            lines.append(f"- {fname}")

        lines += ["", "## Patterns"]
        for ptype in ("failure", "success", "routing"):
            patterns = self.get_patterns(ptype)
            if patterns:
                lines.append(f"\n### {ptype.title()} Patterns ({len(patterns)})")
                for p in patterns[-5:]:  # Last 5
                    lines.append(f"- **{p['name']}**: {p['description']}")

        lines += ["", "## Recent Decisions"]
        dec_dir = os.path.join(self.base_dir, "decisions")
        decisions = sorted(os.listdir(dec_dir))[-10:] if os.path.isdir(dec_dir) else []
        for d in decisions:
            fpath = os.path.join(dec_dir, d)
            try:
                with open(fpath, errors="ignore") as f:
                    lines.append(f"\n### {d}\n{f.read()[:500]}")
            except Exception:
                pass

        lines += ["", "## Pending Improvements"]
        sug_path = os.path.join(self.base_dir, "improvements", "suggested.md")
        if os.path.exists(sug_path):
            with open(sug_path, errors="ignore") as f:
                lines.append(f.read()[-2000:])

        text = "\n".join(lines)
        # Truncate to token budget (~4 chars/token)
        char_limit = max_tokens * 4
        if len(text) > char_limit:
            text = text[:char_limit] + "\n\n[... truncated ...]"
        return text

    # ── Size cap (BUG-23) ─────────────────────────────────────────
    def _enforce_size_cap(self) -> None:
        size_bytes = self._dir_size(self.base_dir)
        max_bytes = self.max_gb * 1e9
        if size_bytes <= max_bytes:
            return
        logging.warning(
            f"[KNOWLEDGE] Size cap hit "
            f"({size_bytes/1e9:.1f}GB > {self.max_gb}GB) — archiving old decisions"
        )
        dec_dir = os.path.join(self.base_dir, "decisions")
        files = sorted(
            (os.path.join(dec_dir, f) for f in os.listdir(dec_dir)),
            key=os.path.getmtime
        )
        # Remove oldest 10% of decision files
        remove_count = max(1, len(files) // 10)
        for fpath in files[:remove_count]:
            try:
                os.remove(fpath)
            except Exception:
                pass

    def _dir_size(self, path: str) -> int:
        total = 0
        for root, _, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except Exception:
                    pass
        return total

    def _list_modules(self) -> List[str]:
        mod_dir = os.path.join(self.base_dir, "modules")
        if not os.path.isdir(mod_dir):
            return []
        return sorted(os.listdir(mod_dir))


# ── Global instance ───────────────────────────────────────────────
_repo: Optional[KnowledgeRepo] = None


def get_repo() -> KnowledgeRepo:
    global _repo
    if _repo is None:
        try:
            import config
            base = config.get("paths.knowledge_dir", BASE)
            max_gb = config.get("knowledge.max_gb", 5.0)
        except Exception:
            base, max_gb = BASE, 5.0
        _repo = KnowledgeRepo(base, max_gb)
    return _repo


def log_decision(title: str, why: str, how: str, result: str = "") -> None:
    get_repo().log_decision(title, why, how, result)


def save_module_version(name: str, code: str, changelog: str = "") -> str:
    return get_repo().save_module_version(name, code, changelog)


def record_pattern(ptype: str, name: str,
                    description: str, data: Any = None) -> None:
    get_repo().record_pattern(ptype, name, description, data)


def search(query: str) -> List[Dict[str, Any]]:
    return get_repo().search(query)


def export_for_ai(format: str = "markdown",
                   max_tokens: int = 50000) -> str:
    return get_repo().export_for_ai(format, max_tokens)


def snapshot_system_state() -> str:
    return get_repo().snapshot_system_state()


def get_patterns(ptype: str) -> List[Dict]:
    return get_repo().get_patterns(ptype)
