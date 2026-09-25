"""
AENIDA Memory Cleaner
Ebbinghaus decay + two-stage DB size enforcement (BUG-18 fix).
"""
import logging, math, os, sqlite3, time
from typing import Any, Dict

def run_decay(db_path="data/performance.db", lambda_val=0.1,
              prune_threshold=0.1, max_mb=100.0) -> Dict[str, Any]:
    t0 = time.time()
    report = {"rows_decayed":0,"rows_pruned":0,"rows_archived":0,
              "db_size_mb":0.0,"duration_ms":0}
    if not os.path.exists(db_path):
        report["duration_ms"] = int((time.time()-t0)*1000)
        return report
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        now = time.time()
        rows = conn.execute("SELECT id,timestamp,weight FROM fingerprints").fetchall()
        pruned = 0
        for fid,ts,w in rows:
            w_new = w * math.exp(-lambda_val * (now-ts)/3600.0)
            report["rows_decayed"] += 1
            if w_new < prune_threshold:
                conn.execute("DELETE FROM fingerprints WHERE id=?", (fid,))
                pruned += 1
            else:
                conn.execute("UPDATE fingerprints SET weight=? WHERE id=?", (w_new,fid))
        conn.commit()
        report["rows_pruned"] = pruned
    except sqlite3.OperationalError:
        pass
    conn.close()
    size_mb = os.path.getsize(db_path)/1e6
    report["db_size_mb"] = round(size_mb, 2)
    if size_mb > max_mb:
        report["rows_archived"] = _archive_oldest(db_path, 0.10)
        report["db_size_mb"] = round(os.path.getsize(db_path)/1e6, 2)
    report["duration_ms"] = int((time.time()-t0)*1000)
    logging.info(f"[CLEANER] Done — DB={report['db_size_mb']}MB pruned={report['rows_pruned']}")
    return report

def _archive_oldest(db_path: str, fraction: float) -> int:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        total = conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0]
        limit = max(1, int(total * fraction))
        rows = conn.execute(
            "SELECT id,node_id,latency_ms,ram_free_mb,hour_of_day,timestamp,weight "
            "FROM fingerprints ORDER BY weight ASC,timestamp ASC LIMIT ?", (limit,)
        ).fetchall()
        entries = [{"type":"fingerprint_archive","node_id":r[1],"latency_ms":r[2],
                    "ram_free_mb":r[3],"hour_of_day":r[4],"timestamp":r[5],"weight":r[6]}
                   for r in rows]
        if entries:
            try:
                from memory_layer import archive_to_cold
                archive_to_cold(entries)
            except Exception as e:
                logging.warning(f"[CLEANER] Cold archive failed: {e}")
        ids = [r[0] for r in rows]
        if ids:
            conn.execute(f"DELETE FROM fingerprints WHERE id IN ({','.join('?'*len(ids))})", ids)
            conn.commit()
        conn.close()
        return len(ids)
    except Exception as e:
        logging.error(f"[CLEANER] Archive error: {e}")
        conn.close()
        return 0

def run_full_nightly_clean() -> Dict[str, Any]:
    logging.info("[CLEANER] Starting nightly cleanup...")
    try:
        import config
        db = config.get("paths.performance_db", "data/performance.db")
        lam = config.get("performance_learner.decay_lambda", 0.1)
        thresh = config.get("performance_learner.relevance_prune_threshold", 0.1)
        max_mb = config.get("performance_learner.db_max_mb", 100.0)
    except Exception:
        db, lam, thresh, max_mb = "data/performance.db", 0.1, 0.1, 100.0
    return {"performance_cleanup": run_decay(db, lam, thresh, max_mb),
            "completed_at": time.strftime("%Y-%m-%d %H:%M")}

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import json; print(json.dumps(run_full_nightly_clean(), indent=2))
