# AENIDA Worker Bug Fixes — 5 Bugs Fixed

## Files Changed
| File | Bugs Fixed |
|------|-----------|
| `main.py` | Bug #1, #3, #4 |
| `orchestrator.py` | Bug #2 |
| `bridge.py` | Bug #5 |

---

## Bug #1 — `main.py` → `run_worker_mode()`
**Symptom:** Giant ASCII banner + recovery report + status lines printed to worker PC terminal on every start.  
**Root cause:** Worker mode copy-pasted from display mode, `print()` calls never removed.  
**Fix:** All `print()` calls replaced with `log.info()` / `log.warning()`. Worker terminal stays completely blank.

---

## Bug #2 — `orchestrator.py` → `setup_logging()`
**Symptom:** Every INFO/WARNING/ERROR log message appears in the worker PC's terminal continuously.  
**Root cause:** `logging.StreamHandler(sys.stdout)` was unconditionally added to all modes including worker.  
**Fix:** Added `shadow_mode: bool` parameter. When `True` (worker), the `StreamHandler` is NOT attached — file handler only. `main.py` passes `shadow_mode=True` when `--mode worker`.

---

## Bug #3 — `main.py` → `_run_task_loop()`
**Symptom:** A failing task retries instantly in a tight loop, hammering the database and never recovering.  
**Root cause:** No backoff between retries. Also, when the loop itself crashed, `_auto_recover()` (which already existed in `orchestrator.py`) was never called — dead code.  
**Fix:**  
- Added per-task `fail_count` tracker with **exponential backoff**: `5s → 10s → 20s → 40s … max 120s`  
- On loop crash, now imports and calls `orchestrator._auto_recover(e)` before sleeping 30s  

---

## Bug #4 — `main.py` → `_run_task_loop()`
**Symptom:** systemd `stop` / `shadow_worker.py stop` kills the worker mid-task, leaving tasks stuck in `status='running'` forever. Every restart triggers a stale-task recovery pass unnecessarily.  
**Root cause:** Loop only caught `KeyboardInterrupt`. systemd sends `SIGTERM`, which caused ungraceful kill.  
**Fix:** Registered `SIGTERM` + `SIGINT` handlers that set a `threading.Event()`. The loop checks the event between tasks so it always finishes its current task before exiting cleanly.

---

## Bug #5 — `bridge.py` → `/task` endpoint
**Symptom:** Worker PCs receive tasks but do zero actual work. Master thinks everything succeeded (fake `"processed"` status returned). With 40 workers all faking results, the swarm appears healthy while computing nothing.  
**Root cause:** Comment in original code literally said `# placeholder`. The endpoint returned a hardcoded dict without calling any AENIDA module.  
**Fix:**  
- Added `_dispatch_task_real()` — routes through `cognition_router` → `local_brain` → task-type handlers (`trading_agent`, `update_engine`, `test_runner`, `knowledge_repo`)  
- `/health` now returns real CPU/RAM/disk + flags `"degraded"` when 5+ of last 20 tasks failed  
- Added `POST /recover` endpoint — master can remotely trigger `run_recovery()` on any worker  
- `orchestrator.py` agent loop now calls `/recover` automatically when worker reports `degraded`

---

## How to verify the fixes

```bash
# On worker PC — start silently, nothing should appear in terminal
python main.py --mode worker

# Check logs go to file only
tail -f logs/aenida_$(date +%Y%m%d).log

# Stop cleanly (no stale tasks)
kill -TERM $(pgrep -f "main.py --mode worker")

# Confirm no tasks stuck in 'running'
python -c "import task_queue; print(task_queue.get_queue().get_pending_count())"
```
