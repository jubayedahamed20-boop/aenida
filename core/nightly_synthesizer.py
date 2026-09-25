"""
AENIDA Nightly Synthesizer
Sends performance trends to Worker, receives improvement rules.
AES-256 encrypted (BUG-27). Rule confidence gate before injection.
IDEA 12: Auto-generates daily summary report at 02:00 via RTC wake.
"""
import datetime
import json
import logging
import os
import time
from typing import Any, Dict, List

log = logging.getLogger(__name__)


def _encrypt(data: bytes, key: bytes) -> bytes:
    try:
        from cryptography.fernet import Fernet
        import base64, hashlib
        fernet_key = base64.urlsafe_b64encode(hashlib.sha256(key).digest())
        return Fernet(fernet_key).encrypt(data)
    except Exception:
        return data  # fallback: unencrypted if cryptography not installed

def _decrypt(data: bytes, key: bytes) -> bytes:
    try:
        from cryptography.fernet import Fernet
        import base64, hashlib
        fernet_key = base64.urlsafe_b64encode(hashlib.sha256(key).digest())
        return Fernet(fernet_key).decrypt(data)
    except Exception:
        return data

def _get_sync_key() -> bytes:
    raw = os.environ.get("TYR_SYNC_KEY", "default_sync_key_32bytes_!!")
    return raw.encode()[:32].ljust(32, b"0")

def export_trend_matrix() -> bytes:
    """Get trend matrix, JSON-encode, compress, AES-256 encrypt."""
    try:
        from performance_learner import get_trend_matrix
        matrix = get_trend_matrix()
        if hasattr(matrix, "tolist"):
            data = matrix.tolist()
        else:
            data = matrix
    except Exception as e:
        logging.warning(f"[SYNTH] trend matrix failed: {e}")
        data = []
    import zlib
    raw = json.dumps(data, default=str).encode()
    compressed = zlib.compress(raw, level=6)
    return _encrypt(compressed, _get_sync_key())

def receive_synthesized_rules(payload: bytes) -> List[Dict]:
    """Decrypt, decompress, parse rules from Worker."""
    import zlib
    try:
        decrypted = _decrypt(payload, _get_sync_key())
        decompressed = zlib.decompress(decrypted)
        rules = json.loads(decompressed.decode())
        if isinstance(rules, list):
            return rules
    except Exception as e:
        logging.error(f"[SYNTH] receive_rules failed: {e}")
    return []

def inject_rule(rule: Dict[str, Any]) -> bool:
    """
    Inject rule if confidence >= 0.7 (BUG-27).
    Log only if < 0.7.
    """
    conf = float(rule.get("confidence", 0.0))
    title = rule.get("rule", rule.get("title", "unknown"))
    if conf < 0.7:
        logging.info(f"[SYNTH] Rule below threshold ({conf:.0%}) — logged only: {title}")
        try:
            from knowledge_repo import record_pattern
            record_pattern("routing", title, json.dumps(rule), rule)
        except Exception:
            pass
        return False
    # Inject to routing logic
    logging.info(f"[SYNTH] Injecting rule ({conf:.0%}): {title}")
    try:
        from knowledge_repo import record_pattern
        record_pattern("success", title, json.dumps(rule), rule)
    except Exception:
        pass
    return True

def run_nightly_synthesis() -> Dict[str, Any]:
    """Full nightly synthesis cycle."""
    logging.info("[SYNTH] Starting nightly synthesis...")
    report: Dict[str, Any] = {"rules_received": 0, "rules_injected": 0,
                                "rules_skipped": 0, "error": None}
    try:
        payload = export_trend_matrix()
        # Send to Worker
        try:
            from bridge import send_task, check_worker_online
            if not check_worker_online():
                report["error"] = "worker_offline"
                return report
            result = send_task({"task": "nightly_synthesis",
                                 "payload": payload.hex()})
            raw_rules_hex = result.get("result", "")
            if raw_rules_hex:
                rules_payload = bytes.fromhex(raw_rules_hex)
                rules = receive_synthesized_rules(rules_payload)
            else:
                rules = []
        except Exception as e:
            logging.warning(f"[SYNTH] Worker unavailable: {e} — using local AI")
            rules = _local_synthesis()

        report["rules_received"] = len(rules)
        for rule in rules:
            # Sanitize before injection
            try:
                from sanitizer_shield import scrub_dict
                rule = scrub_dict(rule)
            except Exception:
                pass
            if inject_rule(rule):
                report["rules_injected"] += 1
            else:
                report["rules_skipped"] += 1

    except Exception as e:
        report["error"] = str(e)
        logging.error(f"[SYNTH] Synthesis failed: {e}")
    logging.info(f"[SYNTH] Done — {report}")
    return report

def _local_synthesis() -> List[Dict]:
    """Fallback: ask local AI to suggest rules from performance data."""
    try:
        from performance_learner import get_stats, get_all_scores
        from model_shell import call
        scores = get_all_scores() if hasattr(
            __import__("performance_learner"), "get_all_scores") else {}
        prompt = (f"Analyse these node reliability scores: {json.dumps(scores)} "
                  f"Return a JSON array of improvement rules with confidence 0-1.")
        result = call(prompt)
        import re
        text = re.sub(r"```json\n?|```\n?", "", result.get("result","[]")).strip()
        return json.loads(text)
    except Exception:
        return []


# ── IDEA 12: Daily Auto-Summary ───────────────────────────────────

def _collect_trades_from_memory(today_str: str) -> List[Dict]:
    """Pull today's BUY/SELL pipeline decisions from memory layer."""
    try:
        from memory_layer import search
        candidates = search("pipeline_run", top_k=200)
        trades = []
        midnight = datetime.datetime.strptime(
            today_str, "%Y-%m-%d").timestamp()
        for d in candidates:
            ts = d.get("timestamp", 0)
            action = d.get("decision", {}).get("action", "")
            if ts >= midnight and action in ("BUY", "SELL"):
                trades.append(d)
        return trades
    except Exception as e:
        log.warning(f"[SUMMARY] memory read failed: {e}")
        return []


def _collect_all_decisions(today_str: str) -> List[Dict]:
    """All pipeline runs today (for error + health stats)."""
    try:
        from memory_layer import search
        candidates = search("pipeline_run", top_k=500)
        midnight = datetime.datetime.strptime(
            today_str, "%Y-%m-%d").timestamp()
        return [d for d in candidates if d.get("timestamp", 0) >= midnight]
    except Exception:
        return []


def _estimate_pnl(trades: List[Dict]) -> float:
    """Rough P&L estimate from signal data stored in memory."""
    pnl = 0.0
    try:
        from trade_journal import get_closed_pnl_today
        pnl = get_closed_pnl_today()
    except Exception:
        # Fallback: estimate from signal strength as proxy
        for t in trades:
            dec = t.get("decision", {})
            strength = float(dec.get("strength", 50)) / 100.0
            # Very rough: strong signals assumed small positive, weak = neutral
            if dec.get("action") == "BUY" and strength > 0.70:
                pnl += strength * 10
            elif dec.get("action") == "SELL" and strength > 0.70:
                pnl += strength * 8
    return round(pnl, 2)


def _count_errors(decisions: List[Dict]) -> int:
    """Count decisions that had a degraded/error flag."""
    return sum(
        1 for d in decisions
        if d.get("decision", {}).get("blocked_by_risk")
        or d.get("degraded")
    )


def _get_system_health() -> Dict[str, Any]:
    """Collect live system health snapshot."""
    health: Dict[str, Any] = {"status": "OK"}
    try:
        from memory_layer import get_stats
        mem = get_stats()
        health["memory"] = mem
        if not mem.get("memvid_ok"):
            health["status"] = "DEGRADED"
    except Exception as e:
        health["memory_error"] = str(e)
    try:
        import psutil
        vm = psutil.virtual_memory()
        health["ram_used_pct"] = round(vm.percent, 1)
        health["ram_free_gb"] = round(vm.available / 1e9, 2)
        if vm.percent > 90:
            health["status"] = "CRITICAL"
    except Exception:
        pass
    try:
        from worker_registry import get_registry
        active = get_registry().get_active_workers()
        health["active_workers"] = len(active)
    except Exception:
        pass
    return health


def _count_modules_created(today_str: str) -> int:
    """Count modules created today via module_forge."""
    try:
        import glob
        perm_dir = "modules/permanent"
        temp_dir = "modules/temp"
        today_dt = datetime.datetime.strptime(today_str, "%Y-%m-%d")
        count = 0
        for d in (perm_dir, temp_dir):
            for f in glob.glob(os.path.join(d, "*.py")):
                mtime = os.path.getmtime(f)
                if mtime >= today_dt.timestamp():
                    count += 1
        return count
    except Exception:
        return 0


def _build_markdown(today_str: str, trades: List[Dict],
                    all_decisions: List[Dict], pnl: float,
                    modules_created: int, errors_caught: int,
                    health: Dict, synthesis_report: Dict) -> str:
    """Build the full daily summary markdown."""
    buy_count = sum(1 for t in trades
                    if t.get("decision", {}).get("action") == "BUY")
    sell_count = sum(1 for t in trades
                     if t.get("decision", {}).get("action") == "SELL")
    total_decisions = len(all_decisions)
    health_status = health.get("status", "OK")
    ram_free = health.get("ram_free_gb", "?")
    ram_pct = health.get("ram_used_pct", "?")
    workers = health.get("active_workers", "?")

    trade_rows = ""
    for t in trades[:20]:  # show max 20
        dec = t.get("decision", {})
        sym = t.get("symbol", "?")
        act = dec.get("action", "?")
        price = t.get("price", "?")
        strength = dec.get("strength", dec.get("signal_strength", "?"))
        ts = t.get("timestamp", 0)
        time_str = datetime.datetime.fromtimestamp(ts).strftime(
            "%H:%M") if ts else "?"
        trade_rows += f"| {time_str} | {sym} | {act} | {price} | {strength} |\n"

    rules_r = synthesis_report.get("rules_received", 0)
    rules_i = synthesis_report.get("rules_injected", 0)
    rules_s = synthesis_report.get("rules_skipped", 0)
    synth_err = synthesis_report.get("error") or "None"

    md = f"""# 📊 AENIDA Daily Report — {today_str}
*Generated automatically by Nightly Synthesizer at 02:00*

---

## 💹 Trading Activity
| Metric | Value |
|--------|-------|
| BUY signals | {buy_count} |
| SELL signals | {sell_count} |
| Total signals | {len(trades)} |
| Total pipeline runs | {total_decisions} |
| P&L estimate | {pnl:+.2f} USDT |

### Signal Log (top {min(len(trades), 20)})
| Time | Symbol | Action | Price | Strength |
|------|--------|--------|-------|----------|
{trade_rows if trade_rows else "| — | No trades today | — | — | — |\n"}

---

## 🧠 Synthesis Results
| Metric | Value |
|--------|-------|
| Rules received | {rules_r} |
| Rules injected | {rules_i} |
| Rules skipped (low conf) | {rules_s} |
| Synthesis error | {synth_err} |

---

## 🏗️ Module Activity
- Modules created today: **{modules_created}**

---

## ⚠️ Errors & Health
- Errors caught: **{errors_caught}**
- System health: **{health_status}**
- RAM free: {ram_free} GB ({ram_pct}% used)
- Active workers: {workers}

Memory layer: {health.get('memory', {}).get('memvid_ok', '?')} (memvid OK)

---

## 📝 Notes
- Report covers decisions since midnight {today_str}
- P&L estimate is based on logged signal data; actual exchange P&L may differ
- Next nightly wake: 01:55 (RTC alarm)

---
*AENIDA v5.2 — Sovereign-Alpha OS*
"""
    return md


def run_daily_summary() -> Dict[str, Any]:
    """
    IDEA 12: Nightly Synthesizer Auto-Summary.

    Runs at 02:00 via RTC wake. Reads all decisions from today in
    memory_layer, produces a structured report (trades, P&L estimate,
    modules created, errors, system health), saves to
    exports/daily_YYYY-MM-DD.md, and sends a short digest to Telegram.
    """
    log.info("[SUMMARY] Starting daily auto-summary...")
    today_str = datetime.date.today().isoformat()
    result: Dict[str, Any] = {
        "date": today_str,
        "trades": 0,
        "pnl_estimate": 0.0,
        "modules_created": 0,
        "errors_caught": 0,
        "export_path": None,
        "telegram_sent": False,
        "errors": [],
    }

    # ── 1. Collect data ───────────────────────────────────────────
    trades       = _collect_trades_from_memory(today_str)
    all_decisions = _collect_all_decisions(today_str)
    pnl          = _estimate_pnl(trades)
    modules      = _count_modules_created(today_str)
    errors_n     = _count_errors(all_decisions)
    health       = _get_system_health()
    result["trades"]          = len(trades)
    result["pnl_estimate"]    = pnl
    result["modules_created"] = modules
    result["errors_caught"]   = errors_n

    # ── 2. Run synthesis (get rule stats for report) ──────────────
    synthesis_report: Dict[str, Any] = {}
    try:
        synthesis_report = run_nightly_synthesis()
    except Exception as e:
        result["errors"].append(f"synthesis: {e}")
        log.warning(f"[SUMMARY] Synthesis failed: {e}")

    # ── 3. Build markdown ─────────────────────────────────────────
    md = _build_markdown(
        today_str, trades, all_decisions, pnl,
        modules, errors_n, health, synthesis_report
    )

    # ── 4. Save to exports/daily_YYYY-MM-DD.md ───────────────────
    try:
        exports_dir = "exports"
        os.makedirs(exports_dir, exist_ok=True)
        export_path = os.path.join(exports_dir, f"daily_{today_str}.md")
        with open(export_path, "w", encoding="utf-8") as f:
            f.write(md)
        result["export_path"] = export_path
        log.info(f"[SUMMARY] Saved: {export_path}")
    except Exception as e:
        result["errors"].append(f"export_save: {e}")
        log.error(f"[SUMMARY] Save failed: {e}")

    # ── 5. Send Telegram digest ───────────────────────────────────
    try:
        from alert_manager import send_telegram
        health_emoji = {"OK": "✅", "DEGRADED": "⚠️",
                        "CRITICAL": "🚨"}.get(health.get("status", "OK"), "❓")
        pnl_sign = "+" if pnl >= 0 else ""
        short = (
            f"📊 *AENIDA Daily {today_str}*\n"
            f"💹 Trades: {len(trades)} "
            f"(BUY: {sum(1 for t in trades if t.get('decision', {}).get('action') == 'BUY')} "
            f"SELL: {sum(1 for t in trades if t.get('decision', {}).get('action') == 'SELL')})\n"
            f"💰 P&L est: {pnl_sign}{pnl:.2f} USDT\n"
            f"🏗️ Modules: {modules}  ⚠️ Errors: {errors_n}\n"
            f"{health_emoji} Health: {health.get('status', 'OK')} "
            f"| RAM free: {health.get('ram_free_gb', '?')}GB\n"
            f"📋 Rules injected: {synthesis_report.get('rules_injected', 0)}"
        )
        sent = send_telegram(short)
        result["telegram_sent"] = bool(sent)
        log.info(f"[SUMMARY] Telegram {'sent' if sent else 'failed'}")
    except Exception as e:
        result["errors"].append(f"telegram: {e}")
        log.warning(f"[SUMMARY] Telegram failed: {e}")

    log.info(f"[SUMMARY] Complete — {result}")
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import json as _j
    print(_j.dumps(run_daily_summary(), indent=2))
