"""
Tests for 3 new AENIDA features
  TestTaskQueueV3    — 25 tests (task_queue.py in-place upgrade)
  TestEScoreAuction  — 25 tests (escore_auction.py)
  TestUSBInstaller   — 18 tests (usb_installer.py)

Total: 68 tests
Run: python -m pytest tests/test_features_v3.py -v
"""

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Loader ────────────────────────────────────────────────────────
import importlib.util

def _load(rel_path: str, name: str):
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(base, rel_path)
    spec = importlib.util.spec_from_file_location(name, path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

tq_mod  = _load("core/task_queue.py",     "task_queue")
es_mod  = _load("core/escore_auction.py", "escore_auction")
usb_mod = _load("tools/usb_installer.py", "usb_installer")


# ════════════════════════════════════════════════════════════════════
#  1. TASK QUEUE v3  (25 tests)
# ════════════════════════════════════════════════════════════════════

TaskQueue        = tq_mod.TaskQueue
TaskStatus       = tq_mod.TaskStatus
DEFAULT_PRIORITY = tq_mod.DEFAULT_PRIORITY
PRIORITY_FLOOR   = tq_mod.PRIORITY_FLOOR
PRIORITY_MIN     = tq_mod.PRIORITY_MIN
PRIORITY_MAX     = tq_mod.PRIORITY_MAX


def _make_tq(tmp_dir) -> TaskQueue:
    return TaskQueue(
        db_path      = os.path.join(tmp_dir, "tq.db"),
        audit_db_path= os.path.join(tmp_dir, "audit.db"),
        enable_background=False,  # controlled manually in tests
    )


class TestTaskQueueV3(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tq  = _make_tq(self.tmp)

    def tearDown(self):
        self.tq.shutdown()

    # ── Schema migration ─────────────────────────────────────────

    def test_new_columns_exist(self):
        cols = {r[1] for r in self.tq._conn.execute(
            "PRAGMA table_info(tasks)").fetchall()}
        self.assertIn("deadline_ts",   cols)
        self.assertIn("boosted_count", cols)

    def test_migrate_adds_columns_to_v1_db(self):
        """A v1.0 DB (without new columns) should be migrated safely."""
        v1_path = os.path.join(self.tmp, "v1.db")
        conn = sqlite3.connect(v1_path)
        conn.execute("""CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY, task_type TEXT, payload TEXT,
            scheduled_for REAL, priority INTEGER DEFAULT 5,
            status TEXT DEFAULT 'queued', created_at REAL,
            started_at REAL, completed_at REAL, retry_count INTEGER DEFAULT 0
        )""")
        conn.commit(); conn.close()
        # Now open with v3 — should migrate without error
        tq2 = TaskQueue(db_path=v1_path,
                        audit_db_path=os.path.join(self.tmp,"a2.db"),
                        enable_background=False)
        cols = {r[1] for r in tq2._conn.execute(
            "PRAGMA table_info(tasks)").fetchall()}
        self.assertIn("deadline_ts",   cols)
        self.assertIn("boosted_count", cols)
        tq2.shutdown()

    # ── Enqueue ──────────────────────────────────────────────────

    def test_enqueue_returns_uuid(self):
        tid = self.tq.enqueue("ping", {})
        self.assertEqual(len(tid), 36)       # UUID format

    def test_enqueue_default_priority(self):
        tid = self.tq.enqueue("ping", {})
        r = self.tq._conn.execute(
            "SELECT priority FROM tasks WHERE task_id=?", (tid,)).fetchone()
        self.assertEqual(r[0], DEFAULT_PRIORITY)

    def test_enqueue_clamps_priority_high(self):
        tid = self.tq.enqueue("em", {}, priority=99)
        r = self.tq._conn.execute(
            "SELECT priority FROM tasks WHERE task_id=?", (tid,)).fetchone()
        self.assertEqual(r[0], PRIORITY_MAX)

    def test_enqueue_clamps_priority_low(self):
        tid = self.tq.enqueue("em", {}, priority=-5)
        r = self.tq._conn.execute(
            "SELECT priority FROM tasks WHERE task_id=?", (tid,)).fetchone()
        self.assertEqual(r[0], PRIORITY_MIN)

    def test_enqueue_stores_deadline(self):
        dl = time.time() + 300
        tid = self.tq.enqueue("work", {}, deadline_ts=dl)
        r = self.tq._conn.execute(
            "SELECT deadline_ts FROM tasks WHERE task_id=?", (tid,)).fetchone()
        self.assertAlmostEqual(r[0], dl, places=0)

    def test_enqueue_no_deadline_is_null(self):
        tid = self.tq.enqueue("work", {})
        r = self.tq._conn.execute(
            "SELECT deadline_ts FROM tasks WHERE task_id=?", (tid,)).fetchone()
        self.assertIsNone(r[0])

    def test_v1_callers_work_unchanged(self):
        """v1.0 callers that don't pass deadline_ts must still work."""
        tid = tq_mod.enqueue.__wrapped__("ping", {}) if hasattr(
            tq_mod.enqueue, "__wrapped__") else self.tq.enqueue("ping", {})
        self.assertIsInstance(tid, str)

    # ── Dequeue ordering ─────────────────────────────────────────

    def test_dequeue_priority_order(self):
        self.tq.enqueue("lo",  {}, priority=8)
        self.tq.enqueue("hi",  {}, priority=1)
        self.tq.enqueue("mid", {}, priority=4)
        tasks = self.tq.dequeue_ready(limit=3)
        pris  = [t["priority"] for t in tasks]
        self.assertEqual(pris, sorted(pris))

    def test_dequeue_future_task_not_returned(self):
        self.tq.enqueue("future", {}, scheduled_for=time.time() + 9999)
        tasks = self.tq.dequeue_ready()
        self.assertEqual(len(tasks), 0)

    def test_dequeue_marks_running(self):
        self.tq.enqueue("ping", {})
        tasks = self.tq.dequeue_ready(limit=1)
        r = self.tq._conn.execute(
            "SELECT status FROM tasks WHERE task_id=?",
            (tasks[0]["task_id"],)).fetchone()
        self.assertEqual(r[0], "running")

    def test_dequeue_returns_deadline_ts(self):
        dl = time.time() + 300
        self.tq.enqueue("work", {}, deadline_ts=dl)
        tasks = self.tq.dequeue_ready()
        self.assertAlmostEqual(tasks[0]["deadline_ts"], dl, places=0)

    # ── State transitions ─────────────────────────────────────────

    def test_mark_complete(self):
        tid = self.tq.enqueue("ping", {})
        self.tq.dequeue_ready()
        self.assertTrue(self.tq.mark_complete(tid))
        r = self.tq._conn.execute(
            "SELECT status FROM tasks WHERE task_id=?", (tid,)).fetchone()
        self.assertEqual(r[0], "complete")

    def test_mark_failed_retry_requeues(self):
        tid = self.tq.enqueue("work", {})
        self.tq.dequeue_ready()
        self.assertTrue(self.tq.mark_failed(tid, retry=True, max_retries=3))
        r = self.tq._conn.execute(
            "SELECT status, retry_count FROM tasks WHERE task_id=?",
            (tid,)).fetchone()
        self.assertEqual(r[0], "queued")
        self.assertEqual(r[1], 1)

    def test_mark_failed_exhausted_fails(self):
        tid = self.tq.enqueue("work", {})
        self.tq.dequeue_ready()
        self.assertTrue(self.tq.mark_failed(tid, retry=True, max_retries=1))
        r = self.tq._conn.execute(
            "SELECT status FROM tasks WHERE task_id=?", (tid,)).fetchone()
        self.assertEqual(r[0], "failed")

    def test_mark_failed_exponential_backoff(self):
        """Each retry should be scheduled further in the future."""
        delays = []
        for attempt in range(3):
            tid = self.tq.enqueue("work", {})
            # Force retry_count to 'attempt' directly
            self.tq._conn.execute(
                "UPDATE tasks SET status='running', retry_count=? WHERE task_id=?",
                (attempt, tid))
            self.tq._conn.commit()
            self.tq.mark_failed(tid, retry=True, max_retries=10)
            r = self.tq._conn.execute(
                "SELECT scheduled_for FROM tasks WHERE task_id=?",
                (tid,)).fetchone()
            delays.append(r[0] - time.time())
        # Each successive delay should be longer (exponential)
        self.assertLess(delays[0], delays[1])
        self.assertLess(delays[1], delays[2])

    def test_reset_stale_running(self):
        tid = self.tq.enqueue("stale", {})
        self.tq._conn.execute(
            "UPDATE tasks SET status='running', started_at=? WHERE task_id=?",
            (time.time() - 9999, tid))
        self.tq._conn.commit()
        n = self.tq.reset_stale_running(stale_seconds=60)
        self.assertGreater(n, 0)
        r = self.tq._conn.execute(
            "SELECT status FROM tasks WHERE task_id=?", (tid,)).fetchone()
        self.assertEqual(r[0], "queued")

    # ── Starvation prevention ─────────────────────────────────────

    def test_starvation_boosts_old_task(self):
        tid = self.tq.enqueue("bg", {}, priority=8)
        self.tq._conn.execute(
            "UPDATE tasks SET created_at=?, scheduled_for=? WHERE task_id=?",
            (time.time()-9999, time.time()-9999, tid))
        self.tq._conn.commit()
        n = self.tq._run_starvation()
        r = self.tq._conn.execute(
            "SELECT priority FROM tasks WHERE task_id=?", (tid,)).fetchone()
        self.assertEqual(r[0], 7)    # 8 → 7

    def test_starvation_does_not_boost_emergency(self):
        tid = self.tq.enqueue("em", {}, priority=0)
        self.tq._conn.execute(
            "UPDATE tasks SET created_at=? WHERE task_id=?",
            (time.time()-9999, tid))
        self.tq._conn.commit()
        self.tq._run_starvation()
        r = self.tq._conn.execute(
            "SELECT priority FROM tasks WHERE task_id=?", (tid,)).fetchone()
        self.assertEqual(r[0], 0)    # unchanged

    def test_starvation_floor_is_respected(self):
        tid = self.tq.enqueue("hi", {}, priority=PRIORITY_FLOOR)
        self.tq._conn.execute(
            "UPDATE tasks SET created_at=? WHERE task_id=?",
            (time.time()-9999, tid))
        self.tq._conn.commit()
        self.tq._run_starvation()
        r = self.tq._conn.execute(
            "SELECT priority FROM tasks WHERE task_id=?", (tid,)).fetchone()
        self.assertGreaterEqual(r[0], PRIORITY_FLOOR)

    # ── Deadline enforcement ──────────────────────────────────────

    def test_expired_task_low_priority(self):
        tid = self.tq.enqueue("bg", {}, priority=8,
                               deadline_ts=time.time()-1)
        self.tq._run_deadline()
        r = self.tq._conn.execute(
            "SELECT status FROM tasks WHERE task_id=?", (tid,)).fetchone()
        self.assertEqual(r[0], "expired")

    def test_escalated_task_high_priority(self):
        tid = self.tq.enqueue("crit", {}, priority=2,
                               deadline_ts=time.time()-1)
        self.tq._run_deadline()
        r = self.tq._conn.execute(
            "SELECT priority, status FROM tasks WHERE task_id=?",
            (tid,)).fetchone()
        self.assertEqual(r[0], 0)         # escalated to P0
        self.assertEqual(r[1], "queued")  # re-queued, not dropped

    def test_future_deadline_untouched(self):
        tid = self.tq.enqueue("work", {}, deadline_ts=time.time()+9999)
        self.tq._run_deadline()
        r = self.tq._conn.execute(
            "SELECT status FROM tasks WHERE task_id=?", (tid,)).fetchone()
        self.assertEqual(r[0], "queued")

    # ── Stats + audit ─────────────────────────────────────────────

    def test_stats_v1_keys_present(self):
        s = self.tq.get_stats()
        for k in ("total","queued","running","complete","failed"):
            self.assertIn(k, s)

    def test_stats_v3_keys_present(self):
        s = self.tq.get_stats()
        for k in ("expired","with_deadline","boosted"):
            self.assertIn(k, s)

    def test_audit_log_records_enqueue(self):
        self.tq.enqueue("ping", {})
        log = self.tq.get_audit_log(limit=5)
        self.assertGreater(len(log), 0)
        events = [e["event"] for e in log]
        self.assertIn("enqueued", events)


# ════════════════════════════════════════════════════════════════════
#  2. E-SCORE AUCTION  (25 tests)
# ════════════════════════════════════════════════════════════════════

compute_escore     = es_mod.compute_escore
WorkerBid          = es_mod.WorkerBid
AuctionStore       = es_mod.AuctionStore
EScoreAuction      = es_mod.EScoreAuction
POWER_WEIGHTS      = es_mod.POWER_WEIGHTS
DECAY_PER_TASK     = es_mod.DECAY_PER_TASK
MIN_ESCORE         = es_mod.MIN_ESCORE
SCORE_TTL_S        = es_mod.SCORE_TTL_S
build_escore_heartbeat_payload = es_mod.build_escore_heartbeat_payload


def _make_bid(node_id="w1", power_class="MEDIUM",
              cpu=60.0, ram=2048.0, total=4096.0,
              active=0, offset=0.0) -> WorkerBid:
    return WorkerBid(
        node_id=node_id, hostname=node_id,
        power_class=power_class,
        escore=compute_escore(cpu, ram, total, power_class, active),
        cpu_budget=cpu, free_ram_mb=ram, active_tasks=active,
        submitted_at=time.time() + offset,
    )


class TestComputeEScore(unittest.TestCase):

    def test_idle_high_class_highest_score(self):
        score = compute_escore(80, 8192, 16384, "HIGH", active_tasks=0)
        self.assertGreater(score, 0.5)  # 0.8*0.5*2.0=0.8

    def test_busy_low_class_lowest_score(self):
        score = compute_escore(5, 256, 4096, "LOW", active_tasks=8)
        self.assertLess(score, 0.1)

    def test_zero_cpu_budget_gives_zero(self):
        score = compute_escore(0, 4096, 4096, "HIGH", 0)
        self.assertEqual(score, 0.0)

    def test_decay_reduces_score_with_tasks(self):
        s0 = compute_escore(80, 2048, 4096, "MEDIUM", 0)
        s4 = compute_escore(80, 2048, 4096, "MEDIUM", 4)
        self.assertGreater(s0, s4)

    def test_decay_matches_formula(self):
        cpu, ram, total, cls = 100, 4096, 4096, "MEDIUM"
        s0 = compute_escore(cpu, ram, total, cls, 0)
        s3 = compute_escore(cpu, ram, total, cls, 3)
        expected = s0 * (DECAY_PER_TASK ** 3)
        self.assertAlmostEqual(s3, expected, places=4)

    def test_high_beats_medium_same_resources(self):
        s_hi  = compute_escore(60, 2048, 4096, "HIGH",   0)
        s_mid = compute_escore(60, 2048, 4096, "MEDIUM", 0)
        self.assertGreater(s_hi, s_mid)

    def test_power_weights_used(self):
        ratio = compute_escore(100, 4096, 4096, "HIGH", 0) / \
                compute_escore(100, 4096, 4096, "MEDIUM", 0)
        self.assertAlmostEqual(ratio,
            POWER_WEIGHTS["HIGH"] / POWER_WEIGHTS["MEDIUM"], places=3)

    def test_score_non_negative(self):
        self.assertGreaterEqual(compute_escore(0, 0, 0, "LOW", 99), 0.0)

    def test_score_at_most_power_weight(self):
        # cpu=100%, ram_factor=1.0 → raw = power_weight; decay ≤ 1
        s = compute_escore(100, 4096, 4096, "HIGH", 0)
        self.assertLessEqual(s, POWER_WEIGHTS["HIGH"] + 0.001)


class TestAuctionStore(unittest.TestCase):

    def setUp(self):
        self.tmp   = tempfile.mkdtemp()
        self.store = AuctionStore(os.path.join(self.tmp, "auction.db"))

    def test_submit_bid_creates_record(self):
        b = _make_bid("w1")
        self.store.submit_bid(b)
        rows = self.store._conn.execute(
            "SELECT node_id FROM worker_bids WHERE node_id='w1'").fetchall()
        self.assertEqual(len(rows), 1)

    def test_submit_bid_upserts(self):
        b1 = _make_bid("w1", cpu=60.0)
        b2 = _make_bid("w1", cpu=30.0)
        self.store.submit_bid(b1)
        self.store.submit_bid(b2)
        rows = self.store._conn.execute(
            "SELECT COUNT(*) FROM worker_bids WHERE node_id='w1'").fetchone()
        self.assertEqual(rows[0], 1)   # only one row per worker

    def test_eligible_bids_excludes_stale(self):
        stale = _make_bid("stale", offset=-(SCORE_TTL_S + 1))
        fresh = _make_bid("fresh")
        self.store.submit_bid(stale)
        self.store.submit_bid(fresh)
        bids = self.store.get_eligible_bids()
        ids = [b.node_id for b in bids]
        self.assertNotIn("stale", ids)
        self.assertIn("fresh", ids)

    def test_eligible_bids_sorted_by_escore(self):
        self.store.submit_bid(_make_bid("lo", cpu=10.0))
        self.store.submit_bid(_make_bid("hi", cpu=90.0))
        bids = self.store.get_eligible_bids()
        ids  = [b.node_id for b in bids]
        self.assertEqual(ids[0], "hi")

    def test_record_assignment_increments_wins(self):
        b = _make_bid("w1")
        self.store.submit_bid(b)
        self.store.record_assignment("task-1", "ping", b, rival_count=2)
        r = self.store._conn.execute(
            "SELECT total_wins FROM worker_bids WHERE node_id='w1'"
        ).fetchone()
        self.assertEqual(r[0], 1)

    def test_leaderboard_returns_list(self):
        self.store.submit_bid(_make_bid("w1"))
        lb = self.store.get_leaderboard()
        self.assertIsInstance(lb, list)
        self.assertGreater(len(lb), 0)
        self.assertIn("node_id", lb[0])

    def test_stats_keys(self):
        s = self.store.get_stats()
        self.assertIn("total_workers", s)
        self.assertIn("eligible_workers", s)
        self.assertIn("total_assignments", s)


class TestEScoreAuction(unittest.TestCase):

    def setUp(self):
        self.tmp     = tempfile.mkdtemp()
        self.auction = EScoreAuction(os.path.join(self.tmp, "auc.db"))

    def test_receive_bid_from_full_heartbeat(self):
        hb = {"node_id":"w1","hostname":"w1","power_class":"HIGH",
              "escore":1.2,"cpu_budget_pct":80,"free_ram_mb":4096,
              "total_ram_mb":8192,"active_tasks":0}
        self.auction.receive_bid(hb)   # should not raise
        lb = self.auction.leaderboard()
        self.assertEqual(lb[0]["node_id"], "w1")

    def test_receive_bid_computes_escore_from_metrics(self):
        hb = {"node_id":"w2","hostname":"w2","power_class":"MEDIUM",
              "cpu_budget_pct":60,"free_ram_mb":2048,"total_ram_mb":4096,
              "active_tasks":1}
        self.auction.receive_bid(hb)
        lb = self.auction.leaderboard()
        self.assertGreater(lb[0]["escore"], 0)

    def test_select_worker_highest_escore_wins(self):
        for node, cpu in [("lo", 10.0), ("hi", 90.0), ("mid", 50.0)]:
            self.auction.receive_bid({
                "node_id": node, "hostname": node, "power_class": "MEDIUM",
                "cpu_budget_pct": cpu, "free_ram_mb": 2048,
                "total_ram_mb": 4096, "active_tasks": 0})
        winner = self.auction.select_worker("ping")
        self.assertEqual(winner.node_id, "hi")

    def test_select_worker_none_when_no_eligible(self):
        w = self.auction.select_worker("ping")
        self.assertIsNone(w)

    def test_select_worker_excludes_node(self):
        for node in ("w1", "w2"):
            self.auction.receive_bid({
                "node_id": node, "hostname": node, "power_class": "HIGH",
                "cpu_budget_pct": 80, "free_ram_mb": 4096,
                "total_ram_mb": 8192, "active_tasks": 0})
        winner = self.auction.select_worker("ping", exclude=["w1"])
        self.assertEqual(winner.node_id, "w2")

    def test_assign_task_records_in_db(self):
        self.auction.receive_bid({
            "node_id":"w1","hostname":"w1","power_class":"HIGH",
            "cpu_budget_pct":80,"free_ram_mb":4096,
            "total_ram_mb":8192,"active_tasks":0})
        self.auction.assign_task("w1", "task-001", "ping")
        stats = self.auction.stats()
        self.assertEqual(stats["total_assignments"], 1)

    def test_build_heartbeat_payload_keys(self):
        p = build_escore_heartbeat_payload(
            cpu_budget_pct=60, free_ram_mb=2048,
            total_ram_mb=4096, power_class="MEDIUM", active_tasks=2)
        for k in ("escore","cpu_budget_pct","free_ram_mb",
                  "total_ram_mb","power_class","active_tasks"):
            self.assertIn(k, p)

    def test_build_heartbeat_escore_is_float(self):
        p = build_escore_heartbeat_payload(60, 2048, 4096, "MEDIUM", 0)
        self.assertIsInstance(p["escore"], float)
        self.assertGreater(p["escore"], 0)


# ════════════════════════════════════════════════════════════════════
#  3. USB INSTALLER  (18 tests)
# ════════════════════════════════════════════════════════════════════

detect_environment       = usb_mod.detect_environment
check_prerequisites      = usb_mod.check_prerequisites
write_worker_config      = usb_mod.write_worker_config
install_aenida_files     = usb_mod.install_aenida_files
Progress                 = usb_mod.Progress
enroll_with_mother       = usb_mod.enroll_with_mother


class TestDetectEnvironment(unittest.TestCase):

    def test_returns_dict(self):
        env = detect_environment()
        self.assertIsInstance(env, dict)

    def test_has_required_keys(self):
        env = detect_environment()
        for k in ("os","python","hostname","ram_gb","disk_gb"):
            self.assertIn(k, env)

    def test_os_is_string(self):
        env = detect_environment()
        self.assertIn(env["os"], ("Linux","Windows","Darwin"))

    def test_python_is_version_string(self):
        env = detect_environment()
        parts = env["python"].split(".")
        self.assertGreaterEqual(len(parts), 2)


class TestCheckPrerequisites(unittest.TestCase):

    def setUp(self):
        self.p = Progress(total_steps=6)

    def test_good_env_passes(self):
        env = {"ram_gb": 8.0, "disk_gb": 50.0, "os": "Linux",
               "is_root": True, "is_admin": False}
        result = check_prerequisites(env, self.p)
        self.assertTrue(result)

    def test_low_disk_fails(self):
        env = {"ram_gb": 8.0, "disk_gb": 1.0, "os": "Linux",
               "is_root": True, "is_admin": False}
        result = check_prerequisites(env, self.p)
        self.assertFalse(result)

    def test_low_ram_still_passes(self):
        """Low RAM is a warning, not a blocker."""
        env = {"ram_gb": 1.5, "disk_gb": 20.0, "os": "Linux",
               "is_root": True, "is_admin": False}
        result = check_prerequisites(env, self.p)
        self.assertTrue(result)


class TestWriteWorkerConfig(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.install_dir = Path(self.tmp) / "aenida"
        (self.install_dir / "core").mkdir(parents=True)
        self.p = Progress(total_steps=6)

    def test_writes_config_file(self):
        usb_config = {"mother_url": "http://192.168.1.10:8000",
                      "worker_api_key": "secret123"}
        write_worker_config(self.install_dir, usb_config, "worker-test", self.p)
        cfg_path = self.install_dir / "core" / "config.json"
        self.assertTrue(cfg_path.exists())

    def test_config_has_mother_url(self):
        usb_config = {"mother_url": "http://192.168.1.10:8000",
                      "worker_api_key": "key"}
        write_worker_config(self.install_dir, usb_config, "w1", self.p)
        cfg = json.loads(
            (self.install_dir / "core" / "config.json").read_text())
        self.assertEqual(cfg["worker"]["url"], "http://192.168.1.10:8000")

    def test_config_has_node_id(self):
        write_worker_config(self.install_dir, {}, "my-node", self.p)
        cfg = json.loads(
            (self.install_dir / "core" / "config.json").read_text())
        self.assertEqual(cfg["worker"]["node_id"], "my-node")

    def test_config_has_api_key(self):
        write_worker_config(self.install_dir,
                            {"worker_api_key": "k3y"}, "n1", self.p)
        cfg = json.loads(
            (self.install_dir / "core" / "config.json").read_text())
        self.assertEqual(cfg["api_keys"]["worker_api_key"], "k3y")

    def test_existing_config_preserved(self):
        """Existing config.json keys should not be wiped."""
        existing = {"trading": {"symbol": "BTC-USDT"}, "api_keys": {}}
        cfg_path = self.install_dir / "core" / "config.json"
        cfg_path.write_text(json.dumps(existing))
        write_worker_config(self.install_dir,
                            {"worker_api_key": "k"}, "n1", self.p)
        result = json.loads(cfg_path.read_text())
        self.assertIn("trading", result)
        self.assertEqual(result["trading"]["symbol"], "BTC-USDT")

    def test_auto_enrolled_flag(self):
        write_worker_config(self.install_dir, {}, "n1", self.p)
        cfg = json.loads(
            (self.install_dir / "core" / "config.json").read_text())
        self.assertTrue(cfg["worker"]["auto_enrolled"])


class TestInstallAenidaFiles(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # Create a minimal fake source
        self.src = Path(self.tmp) / "src"
        (self.src / "core").mkdir(parents=True)
        (self.src / "core" / "main.py").write_text("# main")
        (self.src / "tools").mkdir()
        (self.src / "tools" / "smart_worker.py").write_text("# sw")
        self.dst = Path(self.tmp) / "dst"
        self.p   = Progress(total_steps=6)

    def test_copies_files(self):
        install_aenida_files(self.src, self.dst, self.p)
        self.assertTrue((self.dst / "core" / "main.py").exists())
        self.assertTrue((self.dst / "tools" / "smart_worker.py").exists())

    def test_creates_data_dirs(self):
        install_aenida_files(self.src, self.dst, self.p)
        self.assertTrue((self.dst / "data").is_dir())
        self.assertTrue((self.dst / "logs").is_dir())


class TestEnrollWithMother(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.p   = Progress(total_steps=6)

    def test_no_mother_url_returns_true(self):
        """Enroll should not crash if no Mother URL is configured."""
        result = enroll_with_mother(self.tmp, {}, "test-node", self.p)
        self.assertTrue(result)

    def test_unreachable_mother_returns_true(self):
        """Network failure should be non-fatal (heartbeat will retry)."""
        usb_cfg = {"mother_url": "http://127.0.0.1:19999",
                   "worker_api_key": "key"}
        result = enroll_with_mother(self.tmp, usb_cfg, "test-node", self.p)
        self.assertTrue(result)


# ════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)
