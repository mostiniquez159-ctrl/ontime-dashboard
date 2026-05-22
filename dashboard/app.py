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
LOGS_DIR = Path("/root/agents/v3/logs")
QUEUE_ROOT = Path("/queue")
CLIENTS_ROOT = Path("/mnt/ontime/Клиенты")
THIN_CTRL_JSON = Path("/data/runtime/thin_control_dashboard.json")
METRICS_JSON = Path("/data/runtime/metrics_snapshot.json")

BOTS = ["pluslogobot", "logo_gift", "printontime", "min_consulting", "cloudepluslogo"]
BOT_SERVICES = {
    "pluslogobot": "bot-pluslogobot",
    "logo_gift": "bot-logo-gift",
    "printontime": "bot-printontime",
    "min_consulting": "bot-consulting",
    "cloudepluslogo": "bot-cloudepluslogo",
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
    if KB_ROOT.exists():
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
    q_paths = [QUEUE_ROOT, Path("/data/queue")]
    for qp in q_paths:
        if qp.exists():
            for d in qp.iterdir():
                if d.is_dir():
                    c = len(list(d.glob("*.json")))
                    counts[d.name] = counts.get(d.name, 0) + c
                    total += c
            break # Use first found
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
body { font-family: var(--font); background: var(--bg); color: var(--text); display: flex; min-height: 100vh; overflow: hidden; }

aside {
  width: 260px; background: var(--sidebar); border-right: 1px solid var(--border);
  padding: 24px 16px; display: flex; flex-direction: column; gap: 4px;
  overflow-y: auto; flex-shrink: 0;
}
aside h1 { font-size: 18px; margin-bottom: 24px; padding-left: 12px; }
aside h1 span { color: var(--accent); }
.nav-item {
  padding: 10px 12px; border-radius: 8px; cursor: pointer; color: var(--text2);
  transition: all 0.2s; font-size: 14px; text-decoration: none; display: block;
}
.nav-item:hover { background: var(--surface); color: var(--text); }
.nav-item.active { background: var(--accent); color: #fff; }
.nav-label { font-size: 11px; text-transform: uppercase; color: var(--text2); margin: 12px 0 4px 12px; font-weight: 600; letter-spacing: 0.5px; }

main { flex: 1; padding: 32px; overflow-y: auto; }
.section { display: none; }
.section.active { display: block; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; margin-top: 20px; }
.card { background: var(--surface); border: 1px solid var(--border); padding: 20px; border-radius: var(--radius); }
.card h3 { font-size: 14px; color: var(--text2); text-transform: uppercase; margin-bottom: 12px; display: flex; justify-content: space-between; align-items: center; }
.card .val { font-size: 28px; font-weight: 700; }
.card .sub { font-size: 12px; color: var(--text2); margin-top: 4px; }

table { width: 100%; border-collapse: collapse; margin-top: 20px; background: var(--surface); border-radius: var(--radius); overflow: hidden; }
th { text-align: left; color: var(--text2); font-size: 11px; text-transform: uppercase; padding: 12px; border-bottom: 1px solid var(--border); }
td { padding: 12px; border-bottom: 1px solid var(--border); font-size: 14px; }

.status { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
.status.RUNNING, .status.active { background: var(--green); }
.status.IDLE { background: var(--text2); }
.status.PROCESSING { background: var(--accent); }

.badge { font-size: 10px; padding: 2px 6px; border-radius: 4px; background: var(--sidebar); color: var(--text2); }
.btn-group { display: flex; gap: 8px; margin-top: 12px; }
button { padding: 6px 12px; border-radius: 6px; border: none; background: var(--accent); color: #fff; cursor: pointer; font-size: 12px; transition: opacity 0.2s; }
button:hover { opacity: 0.9; }
button.secondary { background: var(--sidebar); border: 1px solid var(--border); color: var(--text); }

pre { background: var(--sidebar); padding: 12px; border-radius: 6px; font-size: 11px; white-space: pre-wrap; margin-top: 10px; border: 1px solid var(--border); max-height: 400px; overflow-y: auto; color: #a0a0b0; }
.sop-item { padding: 8px 12px; border-bottom: 1px solid var(--border); cursor: pointer; font-size: 13px; color: var(--text2); }
.sop-item:hover { background: var(--sidebar); color: var(--text); }

@media (max-width: 768px) {
  body { flex-direction: column; }
  aside { width: 100%; height: auto; flex-direction: row; overflow-x: auto; padding: 12px; }
  aside h1, .nav-label { display: none; }
  .nav-item { white-space: nowrap; }
  main { padding: 16px; }
}
</style>
</head>
<body>
  <aside>
    <h1><span>onTime</span> v3</h1>
    <div class="nav-label">Министерства</div>
    <div class="nav-item active" data-tab="marketing" onclick="showTab('marketing')">Маркетинг</div>
    <div class="nav-item" data-tab="sales" onclick="showTab('sales')">Продажи</div>
    <div class="nav-item" data-tab="analytics" onclick="showTab('analytics')">Аналитика</div>
    <div class="nav-item" data-tab="pr" onclick="showTab('pr')">PR</div>
    <div class="nav-item" data-tab="finance" onclick="showTab('finance')">Финансы</div>
    <div class="nav-item" data-tab="production" onclick="showTab('production')">Производство</div>
    <div class="nav-item" data-tab="design" onclick="showTab('design')">Дизайн/Креатив</div>
    <div class="nav-item" data-tab="legal" onclick="showTab('legal')">Юридический</div>
    <div class="nav-item" data-tab="consulting" onclick="showTab('consulting')">Консалтинг</div>
    
    <div class="nav-label">Система</div>
    <div class="nav-item" data-tab="admin" onclick="showTab('admin')">Администрирование</div>
    
    <div style="margin-top:auto; font-size:10px; color:var(--text2); padding: 12px;" id="ts">...</div>
  </aside>

  <main>
    <div id="marketing" class="section active">
      <h2>Министерство Маркетинга</h2>
      <div class="grid">
        <div class="card"><h3>Активные кампании</h3><div class="val">0</div><div class="sub">Мониторинг охватов и креативов</div></div>
        <div class="card"><h3>Lead Gen Score</h3><div class="val">--</div><div class="sub">Интеграция в процессе</div></div>
      </div>
    </div>

    <div id="sales" class="section">
      <h2>Министерство Продаж</h2>
      <div class="grid">
        <div class="card"><h3>Воронка (leads)</h3><div class="val">0</div><div class="sub">Обработка входящих заявок</div></div>
        <div class="card"><h3>Conversion Rate</h3><div class="val">0%</div><div class="sub">KPI по закрытым сделкам</div></div>
      </div>
    </div>

    <div id="analytics" class="section">
      <h2>Министерство Аналитики</h2>
      <div class="grid">
        <div class="card"><h3>Reports Generated</h3><div class="val">0</div><div class="sub">Сводные данные по юнитам</div></div>
      </div>
    </div>

    <div id="pr" class="section">
      <h2>Министерство PR</h2>
      <div class="grid">
        <div class="card"><h3>Публикации</h3><div class="val">0</div><div class="sub">Упоминания в медиа</div></div>
      </div>
    </div>

    <div id="finance" class="section">
      <h2>Министерство Финансов</h2>
      <div class="grid">
        <div class="card"><h3>Бюджет (24h)</h3><div class="val">$0.00</div><div class="sub">Расход на токены и API</div></div>
      </div>
    </div>

    <div id="production" class="section">
      <h2>Министерство Производства</h2>
      <div class="grid">
        <div class="card"><h3>Выпуск</h3><div class="val">0 items</div><div class="sub">Статус заказов и продуктов</div></div>
      </div>
    </div>

    <div id="design" class="section">
      <h2>Дизайн и Креатив</h2>
      <div class="grid">
        <div class="card"><h3>Запросы на арт</h3><div class="val">0</div><div class="sub">Генерация визуала</div></div>
      </div>
    </div>

    <div id="legal" class="section">
      <h2>Юридический контур</h2>
      <div class="grid">
        <div class="card"><h3>Договоры</h3><div class="val">0</div><div class="sub">Комплаенс и контракты</div></div>
      </div>
    </div>

    <div id="consulting" class="section">
      <h2>Консалтинг</h2>
      <div class="grid">
        <div class="card"><h3>Сессии</h3><div class="val">0</div><div class="sub">Экспертные заключения</div></div>
      </div>
    </div>

    <div id="admin" class="section">
      <h2>Администрирование</h2>
      
      <div style="margin-top:24px">
        <h3>A. Базы данных</h3>
        <div class="grid" id="admin-db"></div>
        <div class="btn-group">
          <button onclick="adminAction('db_check')">Проверить</button>
          <button class="secondary" onclick="adminAction('reindex_status')">Reindex status</button>
        </div>
      </div>

      <div style="margin-top:32px">
        <h3>B. Workflow Studio</h3>
        <div class="grid" style="grid-template-columns: 1fr 2fr;">
          <div class="card" style="padding:0">
            <div style="padding:12px; border-bottom:1px solid var(--border); font-weight:600; font-size:12px">СПИСОК SOP</div>
            <div id="sop-list" style="max-height:400px; overflow-y:auto"></div>
          </div>
          <div class="card">
            <div id="sop-viewer-title" style="font-weight:600; font-size:12px; margin-bottom:8px">VIEWER</div>
            <pre id="sop-content">Выберите SOP для просмотра...</pre>
            <div class="btn-group">
              <button onclick="adminAction('validate_sop')">Validate schema</button>
            </div>
          </div>
        </div>
      </div>

      <div style="margin-top:32px">
        <h3>C. Очереди и runtime</h3>
        <div class="grid" id="admin-queues"></div>
        <div style="margin-top:16px">
          <h4>Последние задачи</h4>
          <table id="admin-recent-tasks">
            <thead><tr><th>Task ID</th><th>Статус</th><th>Time</th></tr></thead>
            <tbody></tbody>
          </table>
        </div>
      </div>

      <div style="margin-top:32px">
        <h3>D. Агенты и роли</h3>
        <table id="admin-agents-table">
          <thead><tr><th>Agent Key</th><th>Role</th><th>Status</th></tr></thead>
          <tbody></tbody>
        </table>
      </div>

      <div style="margin-top:32px">
        <h3>E. Интеграции</h3>
        <div class="grid" id="admin-integrations"></div>
      </div>

      <div style="margin-top:32px">
        <h3>F. Логи и инциденты</h3>
        <pre id="admin-incidents">Loading...</pre>
      </div>
    </div>
  </main>

<script>
let currentSOP = '';

async function api(path) {
  try {
    const r = await fetch(path);
    return await r.json();
  } catch (e) {
    return {status: 'error', message: e.toString()};
  }
}

function showTab(id) {
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  
  const target = document.getElementById(id);
  if (target) target.classList.add('active');
  
  const nav = document.querySelector(`.nav-item[data-tab="${id}"]`);
  if (nav) nav.classList.add('active');
  
  if (id === 'admin') refreshAdmin();
  refresh();
}

async function refresh() {
  const status = await api('/api/status');
  document.getElementById('ts').innerText = status.generated_at || new Date().toISOString();
}

async function refreshAdmin() {
  const dbData = await api('/api/admin/db');
  if (dbData.status === 'ok') {
    document.getElementById('admin-db').innerHTML = dbData.databases.map(db => `
      <div class="card">
        <h3>${db.name}</h3>
        <div class="val">${db.exists === false ? 'MISSING' : formatSize(db.size)}</div>
        <div class="sub">${db.mtime || db.path}</div>
      </div>
    `).join('');
  }

  const sopData = await api('/api/admin/sop/list');
  if (sopData.status === 'ok') {
    document.getElementById('sop-list').innerHTML = sopData.sops.map(s => `
      <div class="sop-item" onclick="viewSOP('${s}')">${s}</div>
    `).join('');
  }

  const qData = await api('/api/admin/queues');
  if (qData.status === 'ok') {
    document.getElementById('admin-queues').innerHTML = Object.entries(qData.counts).map(([q, c]) => `
      <div class="card"><h3>${q}</h3><div class="val">${c}</div></div>
    `).join('');
  }

  const aData = await api('/api/admin/agents');
  if (aData.status === 'ok') {
    document.getElementById('admin-agents-table').querySelector('tbody').innerHTML = aData.agents.map(a => `
      <tr><td><b>${a.key}</b></td><td>${a.role}</td><td><span class="status ${a.status}"></span> ${a.status}</td></tr>
    `).join('');
  }

  const iData = await api('/api/admin/integrations');
  if (iData.status === 'ok') {
    const it = iData.integrations;
    document.getElementById('admin-integrations').innerHTML = `
      <div class="card"><h3>Telegram</h3><div class="val">${it.telegram.status}</div></div>
      <div class="card"><h3>Google Sheets</h3><div class="val">${it.google_sheets.configured ? 'Active' : 'Not configured'}</div></div>
      <div class="card"><h3>GitHub</h3><div class="val"><a href="${it.github.repo}" target="_blank" style="color:var(--accent); font-size:12px">Repo Link</a></div><div class="sub">Last run: ${it.github.last_run}</div></div>
    `;
  }

  const incData = await api('/api/admin/incidents');
  if (incData.status === 'ok') {
    document.getElementById('admin-incidents').innerText = incData.incidents.join('\\n') || 'No recent incidents found.';
  }
}

async function viewSOP(name) {
  currentSOP = name;
  const data = await api('/api/admin/sop/view?name=' + name);
  document.getElementById('sop-viewer-title').innerText = 'VIEWER: ' + name;
  document.getElementById('sop-content').innerText = JSON.stringify(data.content, null, 2);
}

function adminAction(action) { alert('Action: ' + action + ' (STUB)'); }

function formatSize(bytes) {
  if (!bytes) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

setInterval(refresh, 30000);
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
        url = urlparse(self.path)
        path = url.path
        if path in {"/", "/marketing", "/sales", "/analytics", "/pr", "/finance", "/production", "/design", "/legal", "/consulting", "/admin", "/agents", "/tasks", "/metrics", "/health", "/clients", "/settings", "/login"}:
            self._html(get_html())
        elif path == "/api/status":
            self._json(_load_json(THIN_CTRL_JSON, {"runtime": "ok", "generated_at": now_ts(), "message": "no_data"}))
        elif path == "/api/agents":
            self._json(get_agents())
        elif path == "/api/tasks":
            self._json(get_queue_counts())
        elif path == "/api/admin/db":
            try:
                dbs = []
                paths = ["/data/runtime/control.db", "/data/runtime/agents_runtime.sqlite", "/data/vectordb/control.db"]
                for p in paths:
                    path_obj = Path(p)
                    if path_obj.exists():
                        stat = path_obj.stat()
                        dbs.append({
                            "name": path_obj.name, "path": p, "size": stat.st_size, 
                            "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                        })
                    else: dbs.append({"name": path_obj.name, "path": p, "exists": False})
                vdb = Path("/data/vectordb")
                vdb_size = sum(f.stat().st_size for f in vdb.rglob('*') if f.is_file()) if vdb.exists() else 0
                dbs.append({"name": "Vector Store", "path": "/data/vectordb", "exists": vdb.exists(), "size": vdb_size})
                self._json({"status": "ok", "databases": dbs})
            except Exception as e: self._json({"status": "degraded", "error": str(e)})
        elif path == "/api/admin/sop/list":
            try:
                sop_dir = Path("/data/kb_mirror/sop")
                sops = [f.name for f in sop_dir.glob("*.json")] if sop_dir.exists() else []
                self._json({"status": "ok", "sops": sorted(sops)})
            except Exception as e: self._json({"status": "degraded", "error": str(e)})
        elif path == "/api/admin/sop/view":
            try:
                name = parse_qs(url.query).get("name", [""])[0]
                sop_path = Path("/data/kb_mirror/sop") / name
                if sop_path.exists() and sop_path.is_file():
                    self._json({"status": "ok", "name": name, "content": json.loads(sop_path.read_text(encoding="utf-8"))})
                else: self._json({"status": "error", "message": "not found"}, 404)
            except Exception as e: self._json({"status": "degraded", "error": str(e)})
        elif path == "/api/admin/queues":
            try: self._json({"status": "ok", **get_queue_counts()})
            except Exception as e: self._json({"status": "degraded", "error": str(e)})
        elif path == "/api/admin/agents":
            try: self._json({"status": "ok", "agents": get_agents()})
            except Exception as e: self._json({"status": "degraded", "error": str(e)})
        elif path == "/api/admin/integrations":
            try:
                res = {
                    "telegram": {"status": bot_status("ontime-collector")},
                    "google_sheets": {"configured": False},
                    "github": {"repo": "https://github.com/mostiniquez159-ctrl/codex", "last_run": "unknown"}
                }
                self._json({"status": "ok", "integrations": res})
            except Exception as e: self._json({"status": "degraded", "error": str(e)})
        elif path == "/api/admin/incidents":
            try:
                incidents = []
                log_file = LOGS_DIR / "dispatcher.log"
                if log_file.exists():
                    r = subprocess.run(["tail", "-n", "20", str(log_file)], capture_output=True, text=True)
                    incidents = r.stdout.splitlines()
                self._json({"status": "ok", "incidents": incidents})
            except Exception as e: self._json({"status": "degraded", "error": str(e)})
        else: self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/chat":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            bot_key = body.get("bot", "")
            text = body.get("text", "").strip()
            prompt = f"Ответь как {bot_key}: {text}"
            try:
                result = subprocess.run(["claude", "-p", prompt, "--model", "claude-haiku-4-5-20251001", "--max-turns", "1"], capture_output=True, text=True, timeout=20)
                response = result.stdout.strip()
            except Exception as e: response = str(e)
            self._json({"response": response})
        else: self._json({"error": "not found"}, 404)

if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Admin v3 started on port {PORT}")
    server.serve_forever()
