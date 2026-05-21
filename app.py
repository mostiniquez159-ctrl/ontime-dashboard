#!/usr/bin/env python3
"""
onTime Admin v3 — UX/AI dashboard.
Single file. No frameworks. No build step.
Ref: STD_01A §1A.4b.
"""
import json
import os
import subprocess
import time
import re
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("DASHBOARD_PORT", "8080"))
KB_ROOT = Path("/mnt/ontime/Книга знаний Агентов")
WIKI_ROOT = Path("/mnt/ontime/wiki")
LOGS_DIR = Path("/root/agents/v2/logs")
QUEUE_ROOT = Path("/queue")
CLIENTS_ROOT = Path("/mnt/ontime/Клиенты")
THIN_CTRL_JSON = Path("/data/runtime/thin_control_dashboard.json")
METRICS_JSON = Path("/data/runtime/metrics_snapshot.json")

BOTS = ["pluslogobot", "logo_gift", "printontime", "min_consulting"]
BOT_SERVICES = {
    "pluslogobot": "bot-pluslogobot",
    "logo_gift": "bot-logo-gift",
    "printontime": "bot-printontime",
    "min_consulting": "bot-consulting",
}

def now_ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def _load_json(path: Path, default=None):
    try:
        if not path.exists(): return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception: return default

def bot_status(service_name):
    try:
        r = subprocess.run(["systemctl", "is-active", service_name], capture_output=True, text=True, timeout=2)
        return r.stdout.strip()
    except Exception: return "unknown"

def parse_frontmatter(text):
    m = re.search(r'^---\s*(.*?)\s*---', text, re.DOTALL)
    if not m: return {}
    data = {}
    for line in m.group(1).splitlines():
        if ':' in line:
            k, v = line.split(':', 1)
            data[k.strip()] = v.strip().strip('"')
    return data

def get_agents():
    thin = _load_json(THIN_CTRL_JSON, {})
    states = thin.get('agent_states', {})
    agents = []
    for p in KB_ROOT.glob("*/agent.md"):
        key = p.parent.name
        try:
            content = p.read_text(encoding="utf-8")
            fm = parse_frontmatter(content)
        except: fm = {}
        agents.append({
            "key": key,
            "name": fm.get("display_name", key),
            "role": fm.get("role", "worker"),
            "tg": fm.get("tg_username", ""),
            "status": states.get(key, "IDLE"),
            "system_status": bot_status(BOT_SERVICES.get(key, f"bot-{key}")) if key in BOTS else "N/A"
        })
    return agents

def get_queue_counts():
    counts = {}
    total = 0
    if QUEUE_ROOT.exists():
        for d in QUEUE_ROOT.iterdir():
            if d.is_dir():
                c = len(list(d.glob("*.json")))
                counts[d.name] = c
                total += c
    return {"counts": counts, "total": total}

def get_clients():
    clients = []
    index_path = CLIENTS_ROOT / "index.md"
    if index_path.exists():
        text = index_path.read_text(encoding="utf-8")
        matches = re.findall(r'\[\[Клиенты/([^/\]]+)/client\]\]', text)
        for m in matches:
            if m != '_INTERNAL': clients.append(m)
        int_matches = re.findall(r'\[\[Клиенты/_INTERNAL/([^/\]]+)/client\]\]', text)
        for m in int_matches: clients.append(f"_INTERNAL/{m}")
    return clients

def get_html():
    return """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>onTime Admin v3</title>
<style>
:root {
  --bg: #0a0a0f;
  --sidebar: #12121a;
  --surface: #161622;
  --border: #2a2a3a;
  --text: #e0e0ec;
  --text2: #8888a0;
  --accent: #6366f1;
  --green: #22c55e;
  --red: #ef4444;
  --radius: 10px;
  --font: 'Inter', system-ui, sans-serif;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: var(--font); background: var(--bg); color: var(--text); display: flex; min-height: 100vh; }

aside {
  width: 240px; background: var(--sidebar); border-right: 1px solid var(--border);
  padding: 24px 16px; display: flex; flex-direction: column; gap: 8px;
}
aside h1 { font-size: 18px; margin-bottom: 24px; padding-left: 12px; }
aside h1 span { color: var(--accent); }
.nav-item {
  padding: 10px 12px; border-radius: 8px; cursor: pointer; color: var(--text2);
  transition: all 0.2s; font-size: 14px; text-decoration: none; display: block;
}
.nav-item:hover { background: var(--surface); color: var(--text); }
.nav-item.active { background: var(--accent); color: #fff; }

main { flex: 1; padding: 32px; overflow-y: auto; }
.section { display: none; }
.section.active { display: block; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; margin-top: 20px; }
.card { background: var(--surface); border: 1px solid var(--border); padding: 20px; border-radius: var(--radius); }
.card h3 { font-size: 14px; color: var(--text2); text-transform: uppercase; margin-bottom: 8px; }
.card .val { font-size: 32px; font-weight: 700; }

table { width: 100%; border-collapse: collapse; margin-top: 20px; }
th { text-align: left; color: var(--text2); font-size: 12px; text-transform: uppercase; padding: 12px; border-bottom: 1px solid var(--border); }
td { padding: 12px; border-bottom: 1px solid var(--border); font-size: 14px; }

.status { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
.status.RUNNING, .status.active { background: var(--green); }
.status.IDLE { background: var(--text2); }
.status.PROCESSING { background: var(--accent); }

.chat-input-row { display: flex; gap: 8px; margin-top: 20px; }
input, select { background: var(--surface); border: 1px solid var(--border); color: var(--text); padding: 8px 12px; border-radius: 6px; }
button { padding: 8px 16px; border-radius: 6px; border: none; background: var(--accent); color: #fff; cursor: pointer; }
pre { background: var(--sidebar); padding: 12px; border-radius: 6px; font-size: 12px; white-space: pre-wrap; margin-top: 10px; }
</style>
</head>
<body>
  <aside>
    <h1><span>onTime</span> v3</h1>
    <div class="nav-item active" onclick="showTab('overview')">Обзор</div>
    <div class="nav-item" onclick="showTab('agents')">Агенты</div>
    <div class="nav-item" onclick="showTab('tasks')">Задачи</div>
    <div class="nav-item" onclick="showTab('metrics')">Метрики</div>
    <div class="nav-item" onclick="showTab('health')">Здоровье</div>
    <div class="nav-item" onclick="showTab('clients')">Клиенты</div>
    <div class="nav-item" onclick="showTab('settings')">Настройки</div>
    <div style="margin-top:auto; font-size:10px; color:var(--text2)" id="ts">...</div>
  </aside>

  <main>
    <div id="overview" class="section active">
      <h2>Главный пульт</h2>
      <div class="grid" id="stats-summary"></div>
      <div style="margin-top:30px">
        <h3>Быстрый чат</h3>
        <div class="chat-input-row">
          <select id="chat-bot"><option value="bb">BB</option><option value="strateg">Strateg</option></select>
          <input type="text" id="chat-msg" style="flex:1" placeholder="Запрос...">
          <button onclick="sendChat()">Run</button>
        </div>
        <pre id="chat-res" style="display:none"></pre>
      </div>
    </div>

    <div id="agents" class="section">
      <h2>Реестр Агентов</h2>
      <table id="agents-table">
        <thead><tr><th>Агент</th><th>Роль</th><th>Live Статус</th><th>Systemd</th></tr></thead>
        <tbody></tbody>
      </table>
    </div>

    <div id="tasks" class="section">
      <h2>Очереди исполнения</h2>
      <div class="grid" id="tasks-grid"></div>
    </div>

    <div id="metrics" class="section">
      <h2>KPI & Эффективность</h2>
      <div class="grid" id="metrics-grid"></div>
    </div>

    <div id="health" class="section">
      <h2>Состояние системы</h2>
      <div class="grid" id="health-grid"></div>
    </div>

    <div id="clients" class="section">
      <h2>Клиенты</h2>
      <ul id="clients-list" style="margin-top:20px; list-style:none"></ul>
    </div>

    <div id="settings" class="section">
      <h2>Настройки</h2>
      <p style="color:var(--text2); margin-top:20px">Конфигурация портала v3.</p>
    </div>
  </main>

<script>
async function api(path) {
  const r = await fetch('/api' + path);
  return r.json();
}

function showTab(id) {
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  const items = document.querySelectorAll('.nav-item');
  for (let it of items) if(it.innerText.toLowerCase().includes(id.substring(0,4))) it.classList.add('active');
  refresh();
}

async function refresh() {
  const status = await api('/status');
  document.getElementById('ts').innerText = status.generated_at || '';
  
  if (document.getElementById('overview').classList.contains('active')) {
    document.getElementById('stats-summary').innerHTML = `
      <div class="card"><h3>Runtime</h3><div class="val" style="color:${status.runtime==='ok'?'var(--green)':'var(--red)'}">${status.runtime}</div></div>
      <div class="card"><h3>Агенты</h3><div class="val">${status.active_agents}</div></div>
      <div class="card"><h3>Задачи сегодня</h3><div class="val">${status.tasks_today}</div></div>
      <div class="card"><h3>Эффективность</h3><div class="val">${status.efficiency_pct}%</div></div>
    `;
  }

  if (document.getElementById('agents').classList.contains('active')) {
    const agents = await api('/agents');
    const tbody = document.querySelector('#agents-table tbody');
    tbody.innerHTML = agents.map(a => `
      <tr>
        <td><b>${a.name}</b><br><small style="color:var(--text2)">${a.key}</small></td>
        <td>${a.role}</td>
        <td><span class="status ${a.status}"></span> ${a.status}</td>
        <td><span class="badge" style="font-size:11px; padding:2px 6px; background:var(--sidebar)">${a.system_status}</span></td>
      </tr>
    `).join('');
  }

  if (document.getElementById('tasks').classList.contains('active')) {
    const tasks = await api('/tasks');
    document.getElementById('tasks-grid').innerHTML = Object.entries(tasks.counts).map(([q, c]) => `
      <div class="card"><h3>${q}</h3><div class="val">${c}</div></div>
    `).join('');
  }

  if (document.getElementById('metrics').classList.contains('active')) {
    const m = await api('/metrics');
    document.getElementById('metrics-grid').innerHTML = `
      <div class="card"><h3>Выполнено (24h)</h3><div class="val">${m.tasks_done || 0}</div></div>
      <div class="card"><h3>Cost (24h)</h3><div class="val">$${m.cost || 0}</div></div>
      <div class="card"><h3>Efficiency</h3><div class="val">${m.efficiency_pct}%</div></div>
      <div class="card"><h3>Dead Rate</h3><div class="val">${m.dead_rate || 0}%</div></div>
    `;
  }

  if (document.getElementById('health').classList.contains('active')) {
    const h = await api('/health');
    document.getElementById('health-grid').innerHTML = `
      <div class="card"><h3>VectorDB</h3><div class="val">${h.vectordb}</div></div>
      <div class="card"><h3>Wiki Sync</h3><div class="val">${h.wiki_sync}</div></div>
      <div class="card"><h3>Incidents</h3><div class="val">${h.incidents_today || 0}</div></div>
      <div class="card"><h3>Reindex</h3><div class="val" style="font-size:14px">${h.last_reindex_at}</div></div>
    `;
  }

  if (document.getElementById('clients').classList.contains('active')) {
    const clients = await api('/clients');
    document.getElementById('clients-list').innerHTML = clients.map(c => `<li style="padding:8px; border-bottom:1px solid var(--border)">${c}</li>`).join('');
  }
}

async function sendChat() {
  const bot = document.getElementById('chat-bot').value;
  const msg = document.getElementById('chat-msg').value;
  const res = document.getElementById('chat-res');
  res.style.display = 'block';
  res.innerText = 'Sending...';
  const r = await fetch('/api/chat', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({bot, text: msg})
  });
  const data = await r.json();
  res.innerText = data.response || data.error;
}

refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>"""

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args): pass

    def _json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html, code=200):
        body = html.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in {"/", "/agents", "/tasks", "/metrics", "/health", "/clients", "/settings"}:
            self._html(get_html())
        elif path == "/api/status":
            self._json(_load_json(THIN_CTRL_JSON, {"runtime": "error", "message": "no_data"}))
        elif path == "/api/agents":
            self._json(get_agents())
        elif path == "/api/tasks":
            self._json(get_queue_counts())
        elif path == "/api/metrics":
            thin = _load_json(THIN_CTRL_JSON, {})
            mtr = _load_json(METRICS_JSON, {})
            self._json({
                "tasks_today": thin.get("tasks_today", 0),
                "tasks_done": thin.get("tasks_done", 0),
                "efficiency_pct": thin.get("efficiency_pct", 0),
                "active_agents": thin.get("active_agents", 0),
                "cost": mtr.get("cost_total", 0),
                "dead_rate": mtr.get("dead_rate", 0)
            })
        elif path == "/api/health":
            thin = _load_json(THIN_CTRL_JSON, {})
            self._json({
                "runtime": thin.get("runtime", "unknown"),
                "vectordb": thin.get("vectordb", "unknown"),
                "wiki_sync": thin.get("wiki_sync", "unknown"),
                "last_reindex_at": thin.get("last_reindex_at", ""),
                "incidents_today": thin.get("open_breakers", 0)
            })
        elif path == "/api/clients":
            self._json(get_clients())
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/chat":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            bot_key = body.get("bot", "")
            text = body.get("text", "").strip()
            prompt = f"Ответь как {bot_key}: {text}"
            try:
                result = subprocess.run(
                    ["claude", "-p", prompt, "--model", "claude-haiku-4-5-20251001", "--max-turns", "1"],
                    capture_output=True, text=True, timeout=20
                )
                response = result.stdout.strip()
            except Exception as e: response = str(e)
            self._json({"response": response})
        else: self._json({"error": "not found"}, 404)

if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()
