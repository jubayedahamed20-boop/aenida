# AENIDA — Master Bug Fix Guide
**Total bugs fixed: 11 across 7 files (across 3 sessions)**

---

## SESSION 1 — Quick Setup + Shadow Worker + Mobile App

| File | Change |
|------|--------|
| `quick_setup.py` *(new)* | Replaces 20-min USB token process with 30-second SoftToken setup |
| `shadow_worker.py` *(new)* | True silent background daemon — SIGTERM-safe, file-only logging |
| `mobile_app.html` *(new)* | Installable PWA for phone monitoring and command control |
| `serve_mobile.py` *(new)* | One-command LAN server with QR code for phone access |

---

## SESSION 2 — Worker Mode + Bridge Bugs (5 bugs)

### Bug S2-1 · `main.py` → `run_worker_mode()`
**Root cause:** Worker mode copied from display mode — `print(BANNER)` and 4 other `print()` calls flooded the user's terminal on every start.
**Fix:** All `print()` replaced with `log.info()`. Worker terminal stays completely blank.

### Bug S2-2 · `orchestrator.py` → `setup_logging()`
**Root cause:** `logging.StreamHandler(sys.stdout)` was unconditionally attached in ALL modes. Even without `print()`, every log line still appeared on screen.
**Fix:** Added `shadow_mode: bool` parameter. `StreamHandler` only attached when `shadow_mode=False` (display/api). Worker passes `shadow_mode=True`.

### Bug S2-3 · `main.py` → `_run_task_loop()`
**Root cause:** Failing tasks retried with zero delay — tight loop. `_auto_recover()` in `orchestrator.py` was dead code, never called.
**Fix:** Per-task exponential backoff `5s→10s→20s→…→120s max`. Loop crash now calls `_auto_recover(e)` before 30s sleep.

### Bug S2-4 · `main.py` → `_run_task_loop()`
**Root cause:** systemd sends `SIGTERM` to stop services. No handler registered — worker killed mid-task leaving tasks stuck as `status='running'` forever.
**Fix:** `SIGTERM` + `SIGINT` handlers set a `threading.Event`. Loop finishes current task then exits cleanly.

### Bug S2-5 · `bridge.py` → `/task` endpoint
**Root cause:** Comment said `# placeholder`. Returned fake `"processed"` without calling any AENIDA module. 40 workers all faking results.
**Fix:** Added `_dispatch_task_real()` → `cognition_router` → `local_brain` → task-type handlers. `/health` reports real CPU/RAM. `POST /recover` lets master trigger self-healing remotely. Agent loop calls `/recover` automatically on degraded workers.

---

## SESSION 3 — GitHub → Module Pipeline Bugs (6 bugs)

### Bug S3-1 · `github_explorer.py` → `run_nightly_research()`
**Root cause:** Called private `_append_to_md()` directly — bypassing DB, dedup, and `route_suggestion()`. Same patterns re-appended every night.
**Fix:** Each pattern now routed through `route_suggestion()` individually. DB records it, dedup runs, confidence gate applies.

### Bug S3-2 · `github_explorer.py` → `run_nightly_research()`
**Root cause:** Only `read_readme()` was called. READMEs are prose documentation — not source code. Patterns extracted from English text, not real Python logic.
**Fix:** Added `fetch_python_files()` — calls GitHub tree API, fetches top `.py` files, passes to `extract_patterns(source_type="python_code")`. AI prompt adjusted per content type. `confidence` field added per pattern.

### Bug S3-3 · `test_runner.py` → `run_tests()`
**Root cause:** `PYTHONPATH = PROJECT_ROOT` only. Generated modules live in `modules/temp/`. Every test hit `ModuleNotFoundError` → score always 0.0 → every module always `TEMP_FAILED`. **Entire module creation pipeline silently broken.**
**Fix:** Added `_build_pythonpath()` → `PROJECT_ROOT + modules/temp/ + modules/permanent/ + existing PYTHONPATH`. Applied in both `run_tests()` and `trigger_ci()`.

### Bug S3-4 · `update_engine.py` → `route_suggestion()`
**Root cause:** Auto-creation gate was `stype == "fix"`. GitHub always produces `type="new_module"`. Even 100%-confidence GitHub patterns could never trigger module creation.
**Fix:** Gate changed to `stype in ("fix", "new_module")`.

### Bug S3-5 · `module_forge.py` → `approve_module()`
**Root cause A — No backtest:** Approval went straight from "copy to permanent" → hot-reload with no additional validation. A module that passed basic unit tests could still produce wrong output on real data.
**Root cause B — No version history:** `knowledge_repo.save_module_version()` was never called. If `.bak.py` was missing, approved module history was permanently lost.
**Fix A:** `trigger_ci()` called between copy and hot-reload (AST check + pytest + smoke import). Fails → perm copy removed, backup restored.
**Fix B:** `save_module_version()` + `log_decision()` called after every successful approval.

### Bug S3-6 · `test_runner.py` → `trigger_ci()`
**Root cause:** `module_file = PROJECT_ROOT / {name}.py` — always looked in root. Generated modules live in `modules/temp/` or `modules/permanent/`. CI always returned `"stage": "file_not_found"`.
**Fix:** Searches `PROJECT_ROOT → modules/temp/ → modules/permanent/` in order.

---

## Complete GitHub → Module Pipeline (after all fixes)

```
GitHub REST API
  ├── search_repos(query)          find top repos by stars
  ├── read_readme(repo)            README prose patterns
  └── fetch_python_files(repo)     actual .py source code      ← BUG S3-2 fix

extract_patterns(content, source_type)    AI analysis per type   ← BUG S3-2 fix
  └── confidence field per pattern

route_suggestion() per pattern     DB save + dedup + routing     ← BUG S3-1 fix
  └── conf ≥ 0.95 AND type in ("fix","new_module")              ← BUG S3-4 fix

module_forge.create_temp()
  └── test_runner.run_tests()
        PYTHONPATH: root + modules/temp/ + modules/permanent/   ← BUG S3-3 fix
        score ≥ 80%  →  status = TEMP_READY

[human review]  python main.py --review-modules

module_forge.approve_module()
  ├── trigger_ci()  (AST + tests + smoke import)                ← BUG S3-5 fix
  │     searches: root → temp → permanent                       ← BUG S3-6 fix
  ├── hot_reload()  into live system
  └── knowledge_repo.save_module_version()                      ← BUG S3-5 fix
        + log_decision()
```

---

## Files Changed (all sessions)

| File | Session | Status |
|------|---------|--------|
| `quick_setup.py` | S1 | **NEW** |
| `shadow_worker.py` | S1 | **NEW** |
| `mobile_app.html` | S1 | **NEW** |
| `serve_mobile.py` | S1 | **NEW** |
| `main.py` | S2 | Fixed: S2-1, S2-3, S2-4 |
| `orchestrator.py` | S2 | Fixed: S2-2 |
| `bridge.py` | S2 | Fixed: S2-5 |
| `github_explorer.py` | S3 | Fixed: S3-1, S3-2 |
| `test_runner.py` | S3 | Fixed: S3-3, S3-6 |
| `update_engine.py` | S3 | Fixed: S3-4 |
| `module_forge.py` | S3 | Fixed: S3-5 |

**Unchanged files (49 total):** All other `.py` files, `config.json`, `data/`, `tests/` — untouched.

---

## Deployment Checklist

```bash
# 1. First-time setup (one time only)
python quick_setup.py
# → Fills .env, installs systemd service, prints TOTP QR in terminal

# 2. Add API keys to .env
nano .env
# → Set GROQ_API_KEY (or GEMINI_API_KEY)
# → Set GITHUB_TOKEN (free — get from github.com/settings/tokens)

# 3. Run on your main laptop
python main.py --mode display

# 4. Worker PCs — completely silent
python main.py --mode worker
# Nothing appears on screen. Check logs:
tail -f logs/aenida_$(date +%Y%m%d).log

# 5. Phone monitoring
python serve_mobile.py
# Scan QR on phone → open → tap "Add to Home Screen"

# 6. Verify GitHub → module pipeline works
python -c "
from github_explorer import run_nightly_research
import json; print(json.dumps(run_nightly_research(), indent=2))
"

# 7. Check generated modules pending review
python main.py --review-modules

# 8. Run full test suite
python main.py --run-tests all
```
