# 🧠 AENIDA — Sovereign OS

**Autonomous, AI-assisted crypto trading system with a two-node Mother/Worker architecture.**

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Tests](https://img.shields.io/badge/tests-132%20passing-brightgreen)
![License](https://img.shields.io/badge/license-Private-lightgrey)
![Status](https://img.shields.io/badge/status-active%20development-orange)

---

## 📋 Overview

AENIDA is a self-hosted, autonomous trading assistant that splits work across two machines:

| Node | Role |
|---|---|
| **Mother PC** | Your laptop. Runs the dashboard, serves the mobile admin panel, issues commands, hosts the AI brain fallback. |
| **Worker PC** | A headless/office machine. Runs TradingView Desktop, executes technical analysis, holds live market API connections. |

The two nodes communicate over LAN, Tailscale, ngrok, or Cloudflare Tunnel. Every payload between them is **zlib-compressed and SHA-256 verified**. If the Worker goes offline, the Mother degrades gracefully and keeps running locally on its own AI brain and cached data.

**At a glance:**
- 73 Python modules, 2 HTML apps, 10 test files, 132 automated tests (all passing)
- 3-tier security model (L1 auto / L2 TOTP / L3 TOTP + USB hardware token)
- AI brain with provider fallback chain: Groq → Gemini → Together AI → local rule-based engine
- 13 built-in technical indicators, strategy backtesting, and a full trade-journal audit trail

---

## 📁 Project Structure

```
aenida/
├── path_setup.py           ← Run first — registers all sub-packages on the path
├── README.md
│
├── core/                   ← Runtime backbone (25 files)
│   ├── main.py              ← Entry point (display / api / worker / analyze / think)
│   ├── orchestrator.py      ← Wires every module together
│   ├── cognition_router.py  ← Routes tasks: reflex cache → AI brain → fallback
│   ├── local_brain.py       ← AI brain with provider fallback chain
│   ├── fallback_chain.py    ← Provider escalation ladder with ban/recovery
│   ├── memory_layer.py      ← Short-term + archive memory (memvid or in-memory)
│   ├── task_queue.py        ← Priority task queue, SQLite-backed
│   ├── worker_registry.py   ← Tracks Worker PCs (heartbeat, retire)
│   ├── escore_auction.py    ← Picks the best worker for a task
│   ├── security_gateway.py  ← L1/L2/L3 auth gate (TOTP + USB token)
│   ├── sanitizer_shield.py  ← Input sanitization / threat detection
│   ├── integrity_checker.py ← SHA-256 payload hashing + HMAC signing
│   ├── config.py / config.json
│   └── …
│
├── trading/                ← Market & strategy engine (12 files)
│   ├── trading_agent.py     ← Screenshot + API hybrid trading agent
│   ├── strategy_engine.py   ← Create / backtest / activate playbooks
│   ├── risk_engine.py       ← L1/L2/L3 risk scoring, stop-loss, sizing
│   ├── technical_indicators.py ← RSI, MACD, BB, VWAP, EMA, ATR, trend
│   ├── market_data.py       ← OKX + Binance live price/OHLCV
│   ├── trade_journal.py     ← Signal → approval → result audit trail
│   └── …
│
├── mobile/                 ← Remote access & PWA (5 files + 2 HTML)
│   ├── admin_panel.html     ← Password-locked PWA with AI chat + actions
│   ├── network_tunnel.py    ← ngrok / Cloudflare / Tailscale manager
│   ├── mobile_api.py        ← REST API — dashboard, analyze, status, health
│   └── bridge.py            ← Mother ↔ Worker transport
│
├── tools/                  ← Dev & admin utilities (19 files)
│   ├── smart_worker.py      ← systemd-safe Worker entry point
│   ├── safe_updater.py      ← Impact-scanned code updates + rollback
│   ├── quick_setup.py       ← 30-second setup wizard
│   └── …
│
├── tests/                  ← 10 files, 132 tests, all passing
├── modules/{temp,permanent}/ ← Auto-generated modules pending / approved
├── data/                   ← SQLite DBs, reflex cache (auto-created)
└── docs/
    ├── SETUP_GUIDE.md
    ├── MASTER_BUGFIX_GUIDE.md
    └── WORKER_BUGFIX.md
```

---

## 🚀 Quick Start

```bash
# 1. First-time setup
python tools/quick_setup.py

# 2. Install dependencies
pip install requests psutil cryptography numpy
pip install fastapi uvicorn python-dotenv aiohttp   # optional, recommended

# 3. Run on your laptop (dashboard)
python core/main.py --mode display

# 4. Run on the office/worker PC (silent)
python tools/smart_worker.py
# or: sudo systemctl start aenida_worker

# 5. Check status / analyze / ask the AI brain
python core/main.py status
python core/main.py analyze BTC-USDT
python core/main.py think "What does an RSI of 75 mean?"
```

### Mobile access
```bash
python core/main.py --mode api                       # same WiFi: http://<laptop-ip>:5000
python mobile/network_tunnel.py --start-cf --port 5000  # remote, free (Cloudflare Tunnel)
```
Default admin-panel password is **`123456`** — change it immediately (see [Security](#-security--known-risks)).

---

## ⚙️ Configuration

All settings live in `core/config.json`. The block below covers what you'll actually touch day-to-day; the full file has many more sections most users never need to edit.

### Core settings (edit these)

```json
{
  "api_keys": {
    "groq": "", "gemini": "", "together_ai": "", "cryptocompare": "",
    "github": "", "worker_api_key": "", "telegram_bot_token": "", "telegram_chat_id": ""
  },
  "worker": { "url": "http://localhost:8000", "grpc_port": 50051 },
  "trading": {
    "symbols": ["BTC-USDT", "ETH-USDT", "BNB-USDT"],
    "risk_per_trade_percent": 2.0,
    "signal_strength_threshold": 70
  }
}
```

> ⚠️ `worker_api_key` must be **identical** on the Mother and every Worker, or the bridge fails with `checksum_mismatch`.
> ⚠️ Never commit this file with real keys filled in — use `.env` / secret storage, and keep `core/config.json` out of version control once it holds live credentials.

**Graceful degradation** — the system runs with none of this filled in:
- No AI keys → falls back to a local rule-based brain
- No `fastapi` → falls back to Python's built-in HTTP server
- No `memvid` → falls back to in-memory-only (no persistence across restarts)

### Advanced sections (defaults are fine for most setups)

| Section | Controls |
|---|---|
| `market_data` | Per-exchange rate limits (OKX/Binance/CoinGecko) and retry/ban backoff timing |
| `news_watcher` | Poll interval, alert keywords, coin watchlist for the news-monitoring module |
| `memory` | Memory-usage warning/critical thresholds and check interval |
| `cognition` | Reflex-cache similarity thresholds and reflex map size/timeout limits |
| `performance_learner` | Decay rates, fingerprint matching, and worker-node retire/archive windows |
| `task_executor` | Task retry count, ghost-entry size, reconciliation timing |
| `security` | Vault path, TOTP token TTL, L3 challenge TTL, rate-limit thresholds for the L1/L2/L3 auth gate |
| `usb_token` | USB hardware-token device path, label, and expiry for L3 auth |
| `totp` | TOTP issuer/account name shown in authenticator apps |
| `knowledge` | Knowledge-base size cap and export token limits |
| `research` | Bounds on autonomous research runs (duration, API calls, experiment count) |
| `alerts` | Rate limiting for outgoing alerts |
| `worker_registry` | Heartbeat timeout and retire/archive windows for Worker PCs |
| `rtc_wake` | Scheduled nightly wake/sleep and morning wake times (RTC-based) |
| `timesfm` | Forecasting model name, minimum Worker RAM, forecast horizon |
| `module_forge` | Auto-generated module limits and banned code patterns (`os.system`, `eval(`, etc.) for safety |
| `update_engine` | Confidence thresholds that gate logging vs. suggesting vs. auto-applying code updates |
| `tradingview` | MCP host/port, default symbol/interval, screenshot directory |
| `paths` | File paths for every SQLite DB and data directory (auto-created; rarely needs editing) |

Full field-by-field docs don't exist yet for the advanced sections — if you need to tune one, the clearest reference is grep'ing the key name in `core/` or `trading/` to see how it's read.

---

## 🧪 Testing

Verified on Python 3.12 in a clean virtual environment:

```bash
pip install pytest
python -m pytest tests/ -v
```

**Result: 132 passed, 1 warning, 0 failed.**

| File | Tests | Result |
|---|---|---|
| test_config.py | 6 | ✅ |
| test_integrity_checker.py | 7 | ✅ |
| test_risk_engine.py | 7 | ✅ |
| test_sanitizer_shield.py | 11 | ✅ |
| test_task_queue.py | 6 | ✅ |
| test_technical_indicators.py | 10 | ✅ |
| test_trade_journal.py | 6 | ✅ |
| test_features_v3.py | 68 | ✅ |
| test_tradingview_mcp.py | 9 | ✅ |

The only warning is cosmetic: `test_mcp_status` returns a `bool` instead of using `assert` — pytest flags this as `PytestReturnNotNoneWarning`. It does not affect correctness; fix by changing `return True/False` to plain `assert` statements in `tests/test_tradingview_mcp.py`.

---

## 🐛 Known Issues Found in This Build — and Fixes

These were confirmed by running the code directly (syntax check, full test suite, live `status` / `analyze` / `think` / `--mode api` runs), not just read from the code.

| # | Issue | Where | Impact | Fix |
|---|---|---|---|---|
| 1 | **Version string mismatch.** The startup banner and internal metadata print `v5.2`, while `README.md` and `UPGRADE_SUMMARY.md` describe this as `v6.0`. | `core/main.py` (banner, lines ~2 & ~59), `tools/usb_token_creator.py`, `tools/usb_transfer.py`, `tools/data_exporter.py` | Cosmetic, but confusing for support/debugging and for anyone diffing releases. | Centralize the version in one place (e.g. `core/config.py` as `AENIDA_VERSION = "6.0"`) and import it everywhere instead of hardcoding the string in four separate files. |
| 2 | **Default admin-panel password is a hardcoded, well-known value (`123456`).** | `mobile/admin_panel.html` (`DEFAULT_PASSWORD` constant + on-screen hint) | Anyone who reaches the exposed port (e.g. via a leaked tunnel URL) before you change it has full control of the panel. | Force a password change on first run instead of just hinting at it; better yet, generate a random password during `quick_setup.py` and only show it once. |
| 3 | **`checksum_mismatch` on the Mother↔Worker bridge.** | `mobile/bridge.py` | Worker silently rejects tasks / shows offline. | Confirmed root cause: `worker_api_key` differs between the two `config.json` files. Set the identical value on both machines. |
| 4 | **AI brain falls back with `No module named 'groq'` / `No module named 'google'`.** | `core/local_brain.py` fallback chain | `think` / `analyze` return generic, rule-based answers instead of LLM output. | Expected when the optional packages aren't installed — not a bug. Run `pip install groq google-generativeai together` and add the keys to `config.json` to enable them. |
| 5 | **`[MEMORY] memvid not installed` warning on every run.** | `core/memory_layer.py` | Memory doesn't persist across restarts — silently falls back to in-memory. | Install with `pip install memvid` if you need durable memory; otherwise safe to ignore for short sessions. |
| 6 | **`price: 0` returned from `analyze`.** | `trading/market_data.py` | Signal analysis runs on placeholder data. | Happens when the Worker is offline and no exchange API keys/connection are configured — the Mother has no live price source to fall back to. Bring the Worker online or wire up direct OKX/Binance API keys. |
| 7 | **`dist/index.js not found` / `node not found` on the Worker.** | `trading/tradingview_mcp.py` | TradingView MCP bridge won't start. | Install Node.js 18+ (`sudo apt install nodejs npm`) and run `npm run build` inside the `tradingview-mcp` directory. |
| 8 | **USB installer fails at step 5.** | `tools/usb_installer.py` | Worker auto-deploy from USB aborts partway. | Needs elevated privileges: run with `sudo` on Linux or as Administrator on Windows. |

No import errors, syntax errors, or failing tests were found anywhere else in the codebase during this review — the issues above are the full list of reproducible problems.

---

## 🔒 Security & Known Risks

AENIDA uses a three-tier lock system for privileged actions:

| Level | Trigger | Requires |
|---|---|---|
| L1 | Read-only, low-risk ops | Auto-approved |
| L2 | Config changes, file writes | TOTP code |
| L3 | System exec, sensitive paths | TOTP + USB hardware token |

**Before exposing the admin panel or API to the internet:**
1. Change the default `123456` password immediately (see Known Issue #2 above).
2. Set a strong, unique `worker_api_key` and keep it out of version control.
3. Prefer Cloudflare Tunnel / Tailscale over raw port-forwarding.
4. Never commit `core/config.json` with real API keys — use `.env` / secret storage instead.

---

## 📚 Further Reading

| File | Contents |
|---|---|
| `docs/SETUP_GUIDE.md` | Full Mother + Worker setup, including TradingView MCP |
| `docs/MASTER_BUGFIX_GUIDE.md` | Complete history of bugs fixed across prior development sessions |
| `docs/WORKER_BUGFIX.md` | Worker-specific fixes (signal handling, logging, backoff) |
| `docs/UPGRADE_SUMMARY.md` | Feature changelog |

---

## ⚠️ Disclaimer

This is a personal, experimental trading system. It is **not financial advice**, has not been audited for production trading of real funds, and default credentials/config must be hardened before any internet-facing deployment. Use at your own risk.

---

*AENIDA — Built for reliability, organized for professionals.*
*73 Python files · 2 HTML apps · 132 passing tests · Two-node Mother/Worker architecture*
