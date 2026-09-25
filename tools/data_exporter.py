"""
AENIDA Data Exporter
Exports full system knowledge as clean markdown for any AI to read.
"""
import json, logging, os, time
from typing import Any, Dict

EXPORTS_DIR = "exports"

def export_for_ai(max_tokens: int = 50000) -> str:
    lines = [
        "# AENIDA System Snapshot",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M')}",
        f"Version: 5.2 | Files: 45 | Bugs Fixed: 40",
        "",
        "## Architecture",
        "- Laptop (Home): Display + overlay terminal, 4GB RAM",
        "- Office PC: Worker + background services, RTC wake at night",
        "- Communication: gRPC over Tailscale",
        "- Security: TYR V3.0 (61/61 tests), Triple-Lock",
        "",
        "## Active Modules",
    ]
    # List existing .py files
    py_files = [f for f in os.listdir(".") if f.endswith(".py")]
    for f in sorted(py_files):
        lines.append(f"- {f}")

    # Performance stats
    lines += ["", "## Performance Stats"]
    try:
        from performance_learner import get_stats
        stats = get_stats()
        lines.append(f"- Nodes tracked: {stats.get('node_count',0)}")
        lines.append(f"- Fingerprints: {stats.get('fingerprint_count',0)}")
        bl = stats.get("escore_baselines",{})
        lines.append(f"- E_score baselines: local={bl.get('local_tps',50)} "
                     f"remote={bl.get('remote_tps',800)} tok/s")
    except Exception:
        lines.append("- Stats unavailable")

    # Worker status
    lines += ["", "## Workers"]
    try:
        from worker_registry import get_all_workers
        workers = get_all_workers()
        for w in workers:
            lines.append(f"- {w['id']}: {w['status']} score={w['health_score']:.0f}")
    except Exception:
        lines.append("- Worker registry unavailable")

    # Skills
    lines += ["", "## Skills"]
    try:
        from skill_library import get_skill_stats
        s = get_skill_stats()
        lines.append(f"- Active: {s.get('active',0)} / Total: {s.get('total',0)}")
        lines.append(f"- Avg confidence: {s.get('avg_confidence',0):.0%}")
        lines.append(f"- Total uses: {s.get('total_uses',0)}")
    except Exception:
        lines.append("- Skill library unavailable")

    # Pending suggestions
    lines += ["", "## Pending Improvements"]
    try:
        from update_engine import get_pending_suggestions
        suggestions = get_pending_suggestions()
        for s in suggestions[:5]:
            lines.append(f"- [{s.get('priority','?')}] {s.get('title','')} "
                         f"({int(s.get('confidence',0)*100)}%)")
    except Exception:
        lines.append("- Update engine unavailable")

    # Knowledge repo patterns
    lines += ["", "## Known Patterns"]
    try:
        from knowledge_repo import get_patterns
        for ptype in ("failure","success"):
            patterns = get_patterns(ptype)
            if patterns:
                lines.append(f"\n### {ptype.title()} ({len(patterns)})")
                for p in patterns[-3:]:
                    lines.append(f"- {p.get('name','?')}: {p.get('description','')[:80]}")
    except Exception:
        lines.append("- Knowledge repo unavailable")

    lines += ["", "## Bug History",
              "40 bugs fixed. See MASTER BUILD PROMPT V5.2 for full list.",
              "Key fixes: BUG-11 E_score, BUG-15 backpressure, BUG-16 timeouts,",
              "BUG-18 memory decay, BUG-19 Memvid API, BUG-29 AST check,",
              "BUG-30 confidence gate, BUG-36 non-blocking approval,",
              "BUG-38 USB sentinel, BUG-39 hybrid analysis."]

    text = "\n".join(lines)
    char_limit = max_tokens * 4
    if len(text) > char_limit:
        text = text[:char_limit] + "\n\n[... truncated ...]"
    return text

def save_snapshot() -> str:
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    fname = f"aenida_snapshot_{time.strftime('%Y-%m-%d')}.md"
    fpath = os.path.join(EXPORTS_DIR, fname)
    content = export_for_ai()
    with open(fpath, "w") as f:
        f.write(content)
    logging.info(f"[EXPORTER] Snapshot saved: {fpath}")
    return fpath

def get_snapshot_stats() -> Dict[str, Any]:
    snapshots = []
    if os.path.isdir(EXPORTS_DIR):
        snapshots = [f for f in os.listdir(EXPORTS_DIR) if f.endswith(".md")]
    return {"snapshot_count": len(snapshots),
            "exports_dir": EXPORTS_DIR,
            "latest": snapshots[-1] if snapshots else None}

if __name__ == "__main__":
    path = save_snapshot()
    print(f"Snapshot saved: {path}")
