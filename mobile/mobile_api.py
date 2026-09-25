"""
AENIDA Mobile API
Browser-based monitoring dashboard for your phone.
No app install — just open http://laptop-ip:5000

Endpoints (REST + WebSocket):
  GET  /              — Beautiful HTML dashboard
  GET  /api/status    — Full system JSON
  GET  /api/worker    — Worker PC status
  GET  /api/signals   — Latest trade signals
  GET  /api/news      — Latest crypto news
  GET  /api/memory    — Memory stats
  GET  /api/logs      — Last 50 log lines
  POST /api/command   — Send command to AENIDA
  WS   /ws            — Live updates (auto-refreshes dashboard)

Usage:
  python main.py --mode api --port 5000
  Open on phone: http://192.168.1.X:5000
"""

import json
import logging
import os
import sys
import time
from typing import Any, Dict, Optional

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE)
sys.path.insert(0, PROJECT_ROOT)
import path_setup  # noqa: E402  registers all sub-dirs

log = logging.getLogger("mobile_api")

# ── Try FastAPI, fall back to http.server ────────────────────────
try:
    from fastapi import FastAPI, WebSocket
    from fastapi.responses import HTMLResponse, JSONResponse
    import uvicorn
    FASTAPI_OK = True
except ImportError:
    FASTAPI_OK = False
    log.warning("fastapi not installed — using stdlib HTTP server")

# ── Dashboard HTML (served to phone browser) ──────────────────────
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>AENIDA Monitor</title>
<style>
  :root {
    --bg:     #0a0e17;
    --card:   #111827;
    --border: #1f2937;
    --text:   #e2e8f0;
    --muted:  #6b7280;
    --green:  #10b981;
    --red:    #ef4444;
    --yellow: #f59e0b;
    --blue:   #3b82f6;
    --purple: #8b5cf6;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'SF Mono', 'Fira Code', monospace;
    font-size: 13px;
    min-height: 100vh;
  }
  .header {
    background: linear-gradient(135deg, #0f172a, #1e1b4b);
    padding: 16px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .logo {
    font-size: 18px;
    font-weight: 700;
    letter-spacing: 3px;
    color: var(--purple);
  }
  .dot {
    width: 8px; height: 8px; border-radius: 50%;
    display: inline-block; margin-right: 6px;
  }
  .dot.green { background: var(--green); box-shadow: 0 0 6px var(--green); }
  .dot.red   { background: var(--red);   box-shadow: 0 0 6px var(--red);   }
  .dot.yellow{ background: var(--yellow);box-shadow: 0 0 6px var(--yellow);}
  .grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
    padding: 12px;
  }
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 14px;
  }
  .card.full { grid-column: 1 / -1; }
  .card-title {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 1.5px;
    color: var(--muted);
    margin-bottom: 10px;
  }
  .value {
    font-size: 22px;
    font-weight: 700;
    color: var(--text);
  }
  .value.green  { color: var(--green);  }
  .value.red    { color: var(--red);    }
  .value.yellow { color: var(--yellow); }
  .value.blue   { color: var(--blue);   }
  .sub { font-size: 11px; color: var(--muted); margin-top: 4px; }
  .row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 6px 0;
    border-bottom: 1px solid var(--border);
  }
  .row:last-child { border-bottom: none; }
  .badge {
    font-size: 10px;
    padding: 2px 8px;
    border-radius: 20px;
    font-weight: 600;
  }
  .badge.ok     { background: #064e3b; color: var(--green); }
  .badge.fail   { background: #7f1d1d; color: var(--red);   }
  .badge.warn   { background: #78350f; color: var(--yellow); }
  .news-item {
    padding: 8px 0;
    border-bottom: 1px solid var(--border);
    font-size: 12px;
    line-height: 1.4;
  }
  .news-item:last-child { border-bottom: none; }
  .news-coin {
    display: inline-block;
    font-size: 10px;
    color: var(--purple);
    margin-right: 6px;
  }
  .cmd-bar {
    display: flex;
    gap: 8px;
    margin-top: 10px;
  }
  .cmd-input {
    flex: 1;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text);
    font-family: inherit;
    font-size: 12px;
    padding: 8px 12px;
    outline: none;
  }
  .cmd-input:focus { border-color: var(--purple); }
  .btn {
    background: var(--purple);
    border: none;
    border-radius: 6px;
    color: white;
    cursor: pointer;
    font-size: 12px;
    padding: 8px 16px;
    font-family: inherit;
  }
  .btn:active { opacity: 0.7; }
  .signal-box {
    background: var(--bg);
    border-radius: 6px;
    padding: 10px;
    margin-top: 8px;
  }
  .signal-row {
    display: flex;
    justify-content: space-between;
    padding: 3px 0;
    font-size: 12px;
  }
  .bar-wrap {
    background: var(--border);
    border-radius: 4px;
    height: 6px;
    margin-top: 8px;
    overflow: hidden;
  }
  .bar { height: 100%; border-radius: 4px; transition: width 0.5s; }
  .bar.buy  { background: var(--green); }
  .bar.sell { background: var(--red);   }
  .bar.hold { background: var(--muted); }
  .ts { font-size: 10px; color: var(--muted); text-align: right;
        padding: 8px 12px 4px; }
  .log-box {
    background: var(--bg);
    border-radius: 6px;
    padding: 10px;
    max-height: 200px;
    overflow-y: auto;
    font-size: 11px;
    color: var(--muted);
    line-height: 1.6;
  }
  .log-line { white-space: pre-wrap; word-break: break-all; }
  .log-line.ERROR { color: var(--red); }
  .log-line.WARNING { color: var(--yellow); }
  .log-line.INFO { color: var(--muted); }
  .refreshed { font-size: 10px; color: var(--muted); }
</style>
</head>
<body>

<div class="header">
  <span class="logo">AENIDA</span>
  <span id="live-dot" class="dot red" title="Connecting..."></span>
  <span class="refreshed" id="refresh-ts">Loading...</span>
</div>

<div class="grid">

  <!-- Worker Status -->
  <div class="card">
    <div class="card-title">Worker PC</div>
    <div class="value" id="worker-status">—</div>
    <div class="sub" id="worker-sub">checking...</div>
  </div>

  <!-- Laptop RAM -->
  <div class="card">
    <div class="card-title">Laptop RAM</div>
    <div class="value" id="ram-value">—</div>
    <div class="sub" id="ram-sub">checking...</div>
  </div>

  <!-- Last Signal -->
  <div class="card full">
    <div class="card-title">Last Trade Signal</div>
    <div class="signal-box" id="signal-box">
      <div style="color:var(--muted)">No signal yet</div>
    </div>
  </div>

  <!-- API Status -->
  <div class="card full">
    <div class="card-title">AI APIs</div>
    <div id="api-status">Loading...</div>
  </div>

  <!-- Pending Tasks -->
  <div class="card">
    <div class="card-title">Pending Tasks</div>
    <div class="value blue" id="task-count">—</div>
    <div class="sub">in queue</div>
  </div>

  <!-- Decisions -->
  <div class="card">
    <div class="card-title">Decisions Today</div>
    <div class="value green" id="decision-count">—</div>
    <div class="sub">pipeline runs</div>
  </div>

  <!-- News -->
  <div class="card full">
    <div class="card-title">Latest Crypto News</div>
    <div id="news-list"><div style="color:var(--muted)">Loading...</div></div>
  </div>

  <!-- Command Bar -->
  <div class="card full">
    <div class="card-title">Send Command</div>
    <div class="cmd-bar">
      <input class="cmd-input" id="cmd-input"
             placeholder="analyze BTC-USDT | status | think what is RSI"
             autocomplete="off">
      <button class="btn" onclick="sendCmd()">Send</button>
    </div>
    <div id="cmd-result" style="margin-top:10px;color:var(--muted);font-size:11px;"></div>
  </div>

  <!-- Logs -->
  <div class="card full">
    <div class="card-title">Recent Logs</div>
    <div class="log-box" id="log-box">Loading...</div>
  </div>

</div>

<script>
let ws;
let retries = 0;

function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(proto + '://' + location.host + '/ws');

  ws.onopen = () => {
    document.getElementById('live-dot').className = 'dot green';
    retries = 0;
    refresh();
  };

  ws.onmessage = (e) => {
    try { applyUpdate(JSON.parse(e.data)); } catch(ex) {}
  };

  ws.onclose = () => {
    document.getElementById('live-dot').className = 'dot red';
    // Reconnect with backoff
    setTimeout(connectWS, Math.min(30000, 1000 * Math.pow(2, retries++)));
  };

  ws.onerror = () => ws.close();
}

function refresh() {
  fetch('/api/status')
    .then(r => r.json())
    .then(data => applyUpdate(data))
    .catch(() => {});

  fetch('/api/news')
    .then(r => r.json())
    .then(data => renderNews(data.news || []))
    .catch(() => {});

  fetch('/api/logs')
    .then(r => r.json())
    .then(data => renderLogs(data.lines || []))
    .catch(() => {});
}

function applyUpdate(data) {
  const ts = new Date().toLocaleTimeString();
  document.getElementById('refresh-ts').textContent = 'Updated ' + ts;

  const st = data.state || data || {};

  // Worker
  const wOnline = st.worker_online;
  const wEl = document.getElementById('worker-status');
  wEl.textContent = wOnline ? 'ONLINE' : 'OFFLINE';
  wEl.className = 'value ' + (wOnline ? 'green' : 'red');
  document.getElementById('worker-sub').textContent =
    wOnline ? 'Worker PC connected' : 'Local Brain active';

  // RAM
  const ram = st.ram_mb || 0;
  const ramEl = document.getElementById('ram-value');
  ramEl.textContent = ram.toFixed(0) + ' MB';
  ramEl.className = 'value ' + (ram > 180 ? 'red' : ram > 150 ? 'yellow' : 'green');
  document.getElementById('ram-sub').textContent = 'of ~4000MB available';

  // Counters
  document.getElementById('task-count').textContent = st.pending_tasks || 0;
  document.getElementById('decision-count').textContent = st.decisions_today || 0;

  // APIs
  const apis = st.api_status || {};
  let apiHtml = '';
  for (const [name, status] of Object.entries(apis)) {
    const ok = String(status).includes('healthy');
    const cls = ok ? 'ok' : 'fail';
    const label = ok ? 'OK' : status;
    apiHtml += `<div class="row">
      <span>${name}</span>
      <span class="badge ${cls}">${label}</span>
    </div>`;
  }
  if (!apiHtml) apiHtml = '<div style="color:var(--muted)">No API data</div>';
  document.getElementById('api-status').innerHTML = apiHtml;

  // Signal
  const sig = st.last_signal || {};
  if (sig.symbol) {
    const action = sig.recommended || sig.action || 'HOLD';
    const strength = sig.signal_strength || sig.strength || 50;
    const price = sig.price || 0;
    const color = action === 'BUY' ? 'green' : action === 'SELL' ? 'red' : 'hold';
    const barClass = action.toLowerCase();

    document.getElementById('signal-box').innerHTML = `
      <div class="signal-row">
        <span style="color:var(--muted)">Symbol</span>
        <strong>${sig.symbol}</strong>
      </div>
      <div class="signal-row">
        <span style="color:var(--muted)">Action</span>
        <strong style="color:var(--${color === 'green' ? 'green' : color === 'red' ? 'red' : 'muted'})">${action}</strong>
      </div>
      <div class="signal-row">
        <span style="color:var(--muted)">Price</span>
        <span>$${Number(price).toLocaleString()}</span>
      </div>
      <div class="signal-row">
        <span style="color:var(--muted)">RSI</span>
        <span>${sig.rsi || 'N/A'}</span>
      </div>
      <div class="signal-row">
        <span style="color:var(--muted)">Trend</span>
        <span>${sig.trend || 'unknown'}</span>
      </div>
      <div class="bar-wrap">
        <div class="bar ${barClass}" style="width:${Math.min(100,strength)}%"></div>
      </div>
      <div style="font-size:10px;color:var(--muted);margin-top:4px">
        Confidence: ${strength.toFixed(0)}%
      </div>
    `;
  }
}

function renderNews(news) {
  if (!news.length) {
    document.getElementById('news-list').innerHTML =
      '<div style="color:var(--muted)">No recent news</div>';
    return;
  }
  const html = news.slice(0, 6).map(n => {
    const coins = n.coins ? `<span class="news-coin">${n.coins}</span>` : '';
    const title = (n.title || '').substring(0, 90);
    const ts = n.published_at
      ? new Date(n.published_at * 1000).toLocaleTimeString()
      : '';
    return `<div class="news-item">${coins}${title}
      <span style="color:var(--muted);font-size:10px;float:right">${ts}</span>
    </div>`;
  }).join('');
  document.getElementById('news-list').innerHTML = html;
}

function renderLogs(lines) {
  const html = lines.slice(-30).map(l => {
    let cls = 'log-line';
    if (l.includes(' ERROR ') || l.includes(' CRITICAL ')) cls += ' ERROR';
    else if (l.includes(' WARNING ')) cls += ' WARNING';
    else cls += ' INFO';
    return `<div class="${cls}">${escHtml(l)}</div>`;
  }).join('');
  const box = document.getElementById('log-box');
  box.innerHTML = html || '<div style="color:var(--muted)">No logs yet</div>';
  box.scrollTop = box.scrollHeight;
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function sendCmd() {
  const input = document.getElementById('cmd-input');
  const val = input.value.trim();
  if (!val) return;

  document.getElementById('cmd-result').textContent = 'Sending...';
  input.value = '';

  fetch('/api/command', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({command: val})
  })
  .then(r => r.json())
  .then(data => {
    const text = typeof data.result === 'string'
      ? data.result
      : JSON.stringify(data, null, 2);
    document.getElementById('cmd-result').textContent = text.substring(0, 500);
  })
  .catch(e => {
    document.getElementById('cmd-result').textContent = 'Error: ' + e;
  });
}

document.getElementById('cmd-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') sendCmd();
});

// Start
connectWS();
// Auto-refresh every 30s even without WS
setInterval(refresh, 30000);
</script>
</body>
</html>"""


# ── FastAPI app ───────────────────────────────────────────────────
def _build_fastapi_app():
    app = FastAPI(title="AENIDA Mobile API")

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        return HTMLResponse(DASHBOARD_HTML)

    @app.get("/api/status")
    async def api_status():
        try:
            from orchestrator import get_agent_status
            return JSONResponse(get_agent_status())
        except Exception as e:
            return JSONResponse({"error": str(e)})

    @app.get("/api/worker")
    async def api_worker():
        try:
            from bridge import check_worker_online, get_worker_status
            online = check_worker_online()
            details = get_worker_status() if online else {}
            return JSONResponse({"online": online, **details})
        except Exception as e:
            return JSONResponse({"online": False, "error": str(e)})

    @app.get("/api/signals")
    async def api_signals():
        try:
            from trading_agent import get_last_signals
            return JSONResponse({"signals": get_last_signals()})
        except Exception as e:
            return JSONResponse({"signals": {}, "error": str(e)})

    @app.get("/api/news")
    async def api_news():
        try:
            from news_watcher import get_recent
            return JSONResponse({"news": get_recent(10)})
        except Exception as e:
            return JSONResponse({"news": [], "error": str(e)})

    @app.get("/api/memory")
    async def api_memory():
        try:
            from memory_layer import get_stats
            return JSONResponse(get_stats())
        except Exception as e:
            return JSONResponse({"error": str(e)})

    @app.get("/api/logs")
    async def api_logs():
        """Return last 50 lines from today's log file."""
        try:
            log_dir = os.path.join(BASE, "logs")
            today = time.strftime("%Y%m%d")
            log_file = os.path.join(log_dir, f"aenida_{today}.log")
            if os.path.exists(log_file):
                with open(log_file, encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()[-50:]
                return JSONResponse({"lines": [l.rstrip() for l in lines]})
            return JSONResponse({"lines": ["No log file found for today"]})
        except Exception as e:
            return JSONResponse({"lines": [str(e)]})

    @app.post("/api/command")
    async def api_command(body: dict):
        cmd_raw = body.get("command", "").strip()
        if not cmd_raw:
            return JSONResponse({"error": "empty command"})
        parts = cmd_raw.split()
        cmd = parts[0].lower()
        args = parts[1:]
        try:
            from orchestrator import process_command
            result = process_command(cmd, args)
            return JSONResponse(result if isinstance(result, dict)
                                else {"result": str(result)})
        except Exception as e:
            return JSONResponse({"error": str(e)})

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        """Push live updates to phone dashboard every 10s."""
        import asyncio
        await websocket.accept()
        try:
            while True:
                try:
                    from orchestrator import get_agent_status
                    data = get_agent_status()
                except Exception:
                    data = {"error": "status unavailable"}
                await websocket.send_text(json.dumps(data, default=str))
                await asyncio.sleep(10)
        except Exception:
            pass

    return app


# ── Stdlib fallback server ────────────────────────────────────────
def _run_stdlib_server(port: int):
    """Simple fallback when FastAPI is not installed."""
    import http.server
    import urllib.parse

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path

            if path == "/":
                self._send(200, "text/html", DASHBOARD_HTML.encode())
            elif path == "/api/status":
                self._json(self._get_status())
            elif path == "/api/worker":
                self._json(self._get_worker())
            elif path == "/api/news":
                self._json(self._get_news())
            elif path == "/api/logs":
                self._json(self._get_logs())
            else:
                self._send(404, "text/plain", b"Not found")

        def do_POST(self):
            if self.path == "/api/command":
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                cmd_raw = body.get("command", "")
                parts = cmd_raw.split()
                try:
                    from orchestrator import process_command
                    result = process_command(parts[0], parts[1:])
                    self._json(result)
                except Exception as e:
                    self._json({"error": str(e)})

        def _send(self, code, ctype, body):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", len(body))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data):
            b = json.dumps(data, default=str).encode()
            self._send(200, "application/json", b)

        def _get_status(self):
            try:
                from orchestrator import get_agent_status
                return get_agent_status()
            except Exception as e:
                return {"error": str(e)}

        def _get_worker(self):
            try:
                from bridge import check_worker_online
                return {"online": check_worker_online()}
            except Exception as e:
                return {"online": False, "error": str(e)}

        def _get_news(self):
            try:
                from news_watcher import get_recent
                return {"news": get_recent(10)}
            except Exception as e:
                return {"news": [], "error": str(e)}

        def _get_logs(self):
            try:
                log_dir = os.path.join(BASE, "logs")
                today = time.strftime("%Y%m%d")
                lf = os.path.join(log_dir, f"aenida_{today}.log")
                if os.path.exists(lf):
                    with open(lf, encoding="utf-8", errors="replace") as f:
                        return {"lines": [l.rstrip() for l in f.readlines()[-50:]]}
                return {"lines": ["No logs yet"]}
            except Exception as e:
                return {"lines": [str(e)]}

        def log_message(self, *args):
            pass  # Suppress access logs

    server = http.server.HTTPServer(("0.0.0.0", port), Handler)
    log.info(f"Stdlib HTTP server on port {port}")
    server.serve_forever()


# ── Public run() ─────────────────────────────────────────────────
def run(port: int = 5000):
    """Start mobile API server. Called by main.py --mode api."""
    import socket
    # Get local IP for display
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "localhost"

    print()
    print(f"  ╔══════════════════════════════════════════════╗")
    print(f"  ║     AENIDA MOBILE MONITORING READY           ║")
    print(f"  ╠══════════════════════════════════════════════╣")
    print(f"  ║  Open on phone:                              ║")
    print(f"  ║  http://{local_ip}:{port:<34}║")
    print(f"  ║                                              ║")
    print(f"  ║  Features:                                   ║")
    print(f"  ║  • Live worker PC status                     ║")
    print(f"  ║  • Real-time trade signals                   ║")
    print(f"  ║  • Crypto news feed                          ║")
    print(f"  ║  • System logs                               ║")
    print(f"  ║  • Send commands from phone                  ║")
    print(f"  ║  • Auto-refreshes every 10s                  ║")
    print(f"  ╚══════════════════════════════════════════════╝")
    print()

    if FASTAPI_OK:
        app = _build_fastapi_app()
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
    else:
        log.warning("FastAPI not found — using stdlib server (no WebSocket)")
        print("  Note: Install fastapi+uvicorn for WebSocket live updates")
        _run_stdlib_server(port)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=5000)
    args = p.parse_args()
    run(port=args.port)
