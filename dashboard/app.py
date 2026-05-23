#!/usr/bin/env python3
"""
onTime OS v3 — The Owner's Operating System.
Premium Single-File Business Orchestration UI.
Ref: STD_01A §1A.4b, STD_29A §29A.17.
"""
import json
import os
import subprocess
import time
import re
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# --- Configuration ---
PORT = int(os.environ.get("DASHBOARD_PORT", "8080"))
KB_ROOT = Path("/mnt/ontime/Книга знаний Агентов")
CLIENTS_ROOT = Path("/mnt/ontime/Клиенты")
QUEUE_ROOT = Path("/data/queue")
AGENT_REGISTRY = KB_ROOT / "_SYSTEM/10_REGISTRY/agent_registry.json"
CLIENT_REGISTRY = KB_ROOT / "_SYSTEM/10_REGISTRY/client_registry.json"

def _load_json(path: Path, default=None):
    try:
        if not path.exists(): return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception: return default

def get_queue_counts():
    counts = {"pending": 0, "processing": 0, "done": 0, "dead": 0}
    if QUEUE_ROOT.exists():
        for d in QUEUE_ROOT.iterdir():
            if d.is_dir() and d.name in counts:
                counts[d.name] = len(list(d.glob("*.json")))
    return {"counts": counts}

def get_all_client_task_stats():
    """Scans all queue directories to count tasks per client."""
    stats = {} # {client_id: {"done": 0, "total": 0}}
    if not QUEUE_ROOT.exists(): return stats
    
    for state in ["pending", "processing", "done", "dead"]:
        d = QUEUE_ROOT / state
        if not d.exists(): continue
        for f in d.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                cid = data.get("client_id")
                if cid:
                    if cid not in stats: stats[cid] = {"done": 0, "total": 0}
                    stats[cid]["total"] += 1
                    if state == "done": stats[cid]["done"] += 1
            except: continue
    return stats

def get_ministers_from_registry():
    data = _load_json(AGENT_REGISTRY, {})
    return data.get("ministers", [])

def calculate_prep_kpi(folder_path):
    folder = Path(folder_path)
    missing = []
    score = 0
    scaffold = {
        "client.md": "Описание клиента (client.md)",
        "index.md": "Карта навигации (index.md)",
        "log.md": "Журнал активности (log.md)",
        "_DASHBOARD.md": "Дашборд (_DASHBOARD.md)",
        "brand/brand.json": "Бренд-бук (brand/brand.json)"
    }
    if not folder.exists():
        return 0, list(scaffold.values())
    
    for rel, label in scaffold.items():
        if (folder / rel).exists(): score += 20
        else: missing.append(label)
    return score, missing

def get_clients_detailed():
    registry = _load_json(CLIENT_REGISTRY, {}).get("clients", {})
    task_stats = get_all_client_task_stats()
    client_list = []
    
    scan_paths = [CLIENTS_ROOT, CLIENTS_ROOT / "_INTERNAL"]
    folders_found = {}
    for sp in scan_paths:
        if sp.exists():
            for d in sp.iterdir():
                if d.is_dir() and d.name != "_INTERNAL": folders_found[d.name] = d

    for cid, cdata in registry.items():
        if cid == "_INTERNAL": continue
        folder = Path(cdata.get("folder", ""))
        prep_pct, m_prep = calculate_prep_kpi(folder)
        
        # Realization % (Strict: Done / Total)
        stats = task_stats.get(cid, {"done": 0, "total": 0})
        exec_pct = 0
        if stats["total"] > 0:
            exec_pct = round((stats["done"] / stats["total"]) * 100)
            
        client_list.append({
            "client_id": cid, "name": cdata.get("name", cid), "status": "active" if folder.exists() else "missing",
            "prep_percent": prep_pct, "exec_percent": exec_pct,
            "done_count": stats["done"], "total_count": stats["total"],
            "missing_prep": m_prep,
            "updated_at": datetime.fromtimestamp(folder.stat().st_mtime, tz=timezone.utc).isoformat() if folder.exists() else None,
            "path": str(folder)
        })
        if cid in folders_found: del folders_found[cid]

    for fname, fpath in folders_found.items():
        prep_pct, m_prep = calculate_prep_kpi(fpath)
        stats = task_stats.get(fname, {"done": 0, "total": 0})
        exec_pct = 0
        if stats["total"] > 0: exec_pct = round((stats["done"] / stats["total"]) * 100)
        
        client_list.append({
            "client_id": fname, "name": fname, "status": "unregistered",
            "prep_percent": prep_pct, "exec_percent": exec_pct,
            "done_count": stats["done"], "total_count": stats["total"],
            "missing_prep": m_prep,
            "updated_at": datetime.fromtimestamp(fpath.stat().st_mtime, tz=timezone.utc).isoformat(),
            "path": str(fpath)
        })
    return client_list

def get_client_summary():
    clients = get_clients_detailed()
    if not clients: return {"prep_avg": 0, "exec_avg": 0}
    return {
        "prep_avg": round(sum(c['prep_percent'] for c in clients) / len(clients)),
        "exec_avg": round(sum(c['exec_percent'] for c in clients) / len(clients))
    }

def get_home_snapshot():
    q = get_queue_counts().get("counts", {})
    clients = get_clients_detailed()
    csum = get_client_summary()
    missing_clients = sum(1 for c in clients if c.get("status") == "missing")
    unregistered_clients = sum(1 for c in clients if c.get("status") == "unregistered")
    risks = q.get("dead", 0) + missing_clients
    notifications = q.get("pending", 0) + unregistered_clients

    recent_actions = []
    done_dir = QUEUE_ROOT / "done"
    if done_dir.exists():
        done_files = sorted(done_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
        for f in done_files:
            try:
                payload = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                payload = {}
            recent_actions.append({
                "task_id": payload.get("task_id", f.stem),
                "client_id": payload.get("client_id", "—"),
                "task_type": payload.get("task_type", "—"),
                "ts": datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            })

    return {
        "tasks": q.get("pending", 0) + q.get("processing", 0),
        "risks": risks,
        "notifications": notifications,
        "kpi_prep_avg": csum.get("prep_avg", 0),
        "kpi_exec_avg": csum.get("exec_avg", 0),
        "kpi_dead": q.get("dead", 0),
        "recent_actions": recent_actions,
        "agents": get_agents_snapshot()
    }


def get_agents_snapshot():
    registry = _load_json(AGENT_REGISTRY, {})
    total = 0
    for cat in ["high_council", "ministers", "technicians", "workers", "unassigned"]:
        v = registry.get(cat, [])
        if isinstance(v, list):
            total += len(v)
    if isinstance(registry.get("bb"), dict) and "agent_id" in registry.get("bb", {}):
        total += 1
    elif isinstance(registry.get("bb"), list):
        total += len(registry.get("bb", []))

    # Считаем активные через systemd
    active = 0
    try:
        out = subprocess.check_output(
            ["systemctl", "list-units", "--state=active", "--no-legend",
             "--type=service", "agent-v2-*"],
            stderr=subprocess.DEVNULL, text=True
        )
        active = len([l for l in out.strip().splitlines() if l.strip()])
    except Exception:
        active = 0

    return {"total": total, "active": active}

def get_client_details(client_id):
    detailed = get_clients_detailed()
    return next((c for c in detailed if c['client_id'] == client_id), None)

def now_ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def get_html(ministers):
    hierarchy = {
        "group_ministers": {
            "name": "Министерства", "icon": "layers", 
            "subgroups": {
                "sales": {"name": "Продажи", "icon": "trending-up", "items": ["Лиды", "Клиенты", "Диалоги", "Сделки", "Заказы", "КП", "Повторные продажи", "Sales Core"]},
                "content": {"name": "Контент", "icon": "edit-3", "items": ["Контент-завод", "Темы", "Статьи", "Посты", "Публикации", "Комментарии", "Google-таблицы"]},
                "marketing": {"name": "Маркетинг", "icon": "megaphone", "items": ["ЦА", "Боли", "УТП", "Офферы", "Воронки", "Источники трафика", "Реклама", "Упаковка продукта"]},
                "analytics": {"name": "Аналитика", "icon": "bar-chart-2", "items": ["Дашборды", "Метрики", "План-факт", "Отчёты", "Ошибки данных", "Выводы и рекомендации"]},
                "production": {"name": "Производство", "icon": "factory", "items": ["Заказы", "План", "Смены", "Операции", "Материалы", "Остатки", "Брак", "Загрузка"]},
                "tech": {"name": "Техника", "icon": "cpu", "items": ["Сервер", "Скрипты", "Боты", "API", "Интеграции", "Воркфлоу", "Очереди", "Логи", "Ошибки"]},
                "mgmt": {"name": "Управление", "icon": "shield", "items": ["Финансы", "Юрконтур", "Консалтинг", "PR", "Задачи собственника", "Стратегия", "Документы"]}
            }
        },
        "group_kb": {"name": "База знаний", "icon": "book-open", "items": {"kb_docs": "Документы", "kb_sop": "Инструкции", "kb_clients": "Данные клиентов", "kb_vector": "Векторная база"}},
        "group_workflow": {"name": "Воркфлоу", "icon": "activity", "items": {"wf_templates": "Шаблоны", "wf_runs": "Запуски", "wf_queues": "Очереди", "wf_logs": "Логи"}},
        "group_system": {"name": "Система", "icon": "settings", "items": {"sys_users": "Пользователи", "sys_roles": "Роли", "sys_integrations": "Интеграции", "sys_admin": "Администрирование"}}
    }

    nav_html = ""
    sections_html = ""
    for g_id, g_data in hierarchy.items():
        is_ministers = (g_id == "group_ministers")
        nav_html += f'<div class="nav-group" data-group="{g_id}">'
        nav_html += f'<div class="group-header" onclick="toggleGroup(\'{g_id}\')"><i data-feather="{g_data["icon"]}"></i><span>{g_data["name"]}</span><i class="chevron" data-feather="chevron-right"></i></div>'
        nav_html += f'<div class="group-content">'
        if is_ministers:
            for s_id, s_data in g_data["subgroups"].items():
                nav_html += f'<div class="sub-group" data-subgroup="{s_id}">'
                nav_html += f'<div class="sub-header" onclick="toggleSubGroup(\'{s_id}\')"><i data-feather="{s_data["icon"]}" style="color:{s_data.get("color", "#fff")}"></i><span>{s_data["name"]}</span><i class="chevron" data-feather="chevron-down"></i></div>'
                nav_html += f'<div class="sub-content-items">'
                for item in s_data["items"]:
                    t_id = f"{s_id}_{item.replace(' ', '_').lower()}"
                    nav_html += f'<div class="nav-item" data-tab="{t_id}" onclick="showTab(\'{t_id}\')">{item}</div>'
                    sections_html += f'<div id="{t_id}" class="section"><h2>{s_data["name"]} • {item}</h2><div class="card premium-glow"><h3>Модуль активен</h3><p>Ожидание потока данных из контура {s_id}.</p></div></div>'
                nav_html += '</div></div>'
        else:
            for t_id, t_name in g_data["items"].items():
                nav_html += f'<div class="nav-item" data-tab="{t_id}" onclick="showTab(\'{t_id}\')">{t_name}</div>'
                if t_id != "kb_clients":
                    sections_html += f'<div id="{t_id}" class="section"><h2>{t_name}</h2><div class="card">Раздел "{t_name}" инициализирован.</div></div>'
        nav_html += '</div></div>'

    sections_html += """
    <div id="kb_clients" class="section">
      <h2>Данные клиентов</h2>
      <div class="stats-grid">
        <div class="stat-box"><div class="label">Подготовка % (AVG)</div><div class="val" id="kpi-prep">0%</div></div>
        <div class="stat-box"><div class="label">Реализация % (AVG)</div><div class="val" id="kpi-exec">0%</div></div>
      </div>
      <div class="card">
        <div style="display:flex; gap:16px; margin-bottom:20px;"><input type="text" id="client-search" placeholder="Поиск..." class="premium-input" oninput="filterClients()"></div>
        <table class="premium-table">
          <thead><tr><th>Клиент</th><th>Подготовка %</th><th>Реализация %</th><th>Done/Total</th><th>Статус</th><th>Действие</th></tr></thead>
          <tbody id="clients-table-body"></tbody>
        </table>
      </div>
    </div>
    """

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>onTime Business OS</title>
<script src="https://unpkg.com/feather-icons"></script>
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root {{
  --bg: #030305; --sidebar: #08080a; --surface: #0e0e12; --surface-hover: #15151c; --border: #1a1a24;
  --text: #f1f5f9; --text-muted: #64748b; --accent: #6366f1; --accent-glow: rgba(99, 102, 241, 0.15);
  --radius-lg: 16px; --radius-md: 12px; --radius-sm: 8px; --red: #ef4444; --green: #10b981; --yellow: #f59e0b; --blue: #3b82f6;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; outline: none; }}
body {{ font-family: 'Plus Jakarta Sans', sans-serif; background: var(--bg); color: var(--text); display: flex; height: 100vh; overflow: hidden; }}
aside {{ width: 280px; background: var(--sidebar); border-right: 1px solid var(--border); display: flex; flex-direction: column; flex-shrink: 0; padding: 24px 12px; overflow-y: auto; }}
.logo {{
  font-size: 20px; font-weight: 800; display: flex; align-items: center; gap: 10px;
  margin-bottom: 32px; padding: 12px 14px; border-radius: var(--radius-md); cursor: pointer;
  color: #fff; text-decoration: none; border: 1px solid transparent;
  background: linear-gradient(135deg, rgba(99,102,241,0.18), rgba(56,189,248,0.12));
  transition: 0.2s ease;
}}
.logo:hover {{ border-color: rgba(99,102,241,0.6); box-shadow: 0 0 0 3px rgba(99,102,241,0.15); }}
.logo svg {{ color: #c7d2fe; width: 20px; height: 20px; }}
.nav-group {{ margin-bottom: 6px; }}
.group-header {{ padding: 12px; cursor: pointer; display: flex; align-items: center; gap: 12px; border-radius: var(--radius-md); transition: 0.2s; color: var(--text-muted); font-weight: 600; font-size: 14px; }}
.group-header i {{ width: 14px; height: 14px; stroke: currentColor; }}
.group-header .chevron {{ margin-left: auto; width: 12px; height: 12px; transition: 0.3s; }}
.nav-group.open .group-header {{ color: #fff; background: var(--surface); }}
.nav-group.open .group-header .chevron {{ transform: rotate(90deg); }}
.group-content {{ display: none; padding: 4px 0 4px 12px; }}
.nav-group.open .group-content {{ display: block; }}
.sub-group {{ margin-bottom: 2px; }}
.sub-header {{ padding: 8px 12px; cursor: pointer; display: flex; align-items: center; gap: 10px; border-radius: var(--radius-sm); font-size: 13px; font-weight: 600; color: var(--text-muted); transition: 0.2s; }}
.sub-header i {{ width: 13px; height: 13px; stroke: currentColor; }}
.sub-header .chevron {{ margin-left: auto; width: 10px; height: 10px; transition: 0.3s; opacity: 0.5; }}
.sub-group.open .sub-header {{ color: #fff; }}
.sub-group.open .sub-header .chevron {{ transform: rotate(180deg); }}
.sub-content-items {{ display: none; border-left: 1px solid var(--border); margin-left: 18px; padding: 4px 0 4px 16px; }}
.sub-group.open .sub-content-items {{ display: block; }}
.nav-item {{ padding: 8px 12px; cursor: pointer; border-radius: var(--radius-sm); font-size: 13px; color: var(--text-muted); transition: 0.2s; }}
.nav-item:hover {{ color: #fff; background: var(--surface-hover); }}
.nav-item.active {{ color: #fff; background: var(--accent); font-weight: 600; box-shadow: 0 4px 12px var(--accent-glow); }}
main {{ flex: 1; overflow-y: auto; padding: 40px; }}
.section {{ display: none; animation: fadeIn 0.3s ease-out; }}
.section.active {{ display: block; }}
@keyframes fadeIn {{ from {{ opacity: 0; transform: translateY(10px); }} to {{ opacity: 1; transform: translateY(0); }} }}
h2 {{ font-size: 28px; font-weight: 800; margin-bottom: 32px; }}
.card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 20px; padding: 24px; margin-bottom: 24px; }}
.stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 20px; margin-bottom: 32px; }}
.stat-box {{ background: var(--surface); border: 1px solid var(--border); padding: 20px; border-radius: 20px; }}
.stat-box .label {{ font-size: 11px; font-weight: 700; color: var(--text-muted); text-transform: uppercase; margin-bottom: 8px; }}
.stat-box .val {{ font-size: 28px; font-weight: 800; }}
.premium-input {{ background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 10px 16px; color: #fff; font-size: 13px; }}
.premium-table {{ width: 100%; border-collapse: collapse; }}
.premium-table th {{ text-align: left; padding: 12px; color: var(--text-muted); font-size: 11px; text-transform: uppercase; border-bottom: 1px solid var(--border); }}
.premium-table td {{ padding: 14px 12px; border-bottom: 1px solid var(--border); font-size: 13px; }}
.kpi-val {{ font-weight: 800; }}
.color-red {{ color: var(--red); }} .color-yellow {{ color: var(--yellow); }} .color-blue {{ color: var(--blue); }} .color-green {{ color: var(--green); }}
#client-panel {{ position: fixed; top: 0; right: 0; width: 480px; height: 100vh; background: var(--sidebar); border-left: 1px solid var(--border); z-index: 2000; transform: translateX(100%); transition: 0.3s cubic-bezier(0.4, 0, 0.2, 1); padding: 32px; overflow-y: auto; }}
#client-panel.open {{ transform: translateX(0); }}
.panel-close {{ position: absolute; top: 20px; right: 20px; cursor: pointer; color: var(--text-muted); }}
.gap-list {{ list-style: none; margin-top: 12px; }}
.gap-item {{ font-size: 12px; color: var(--text-muted); display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }}
.gap-item i {{ width: 12px; height: 12px; color: var(--red); }}
.badge {{ padding: 2px 8px; border-radius: 100px; font-size: 10px; font-weight: 700; text-transform: uppercase; }}
.badge-active {{ background: rgba(16, 185, 129, 0.1); color: var(--green); }}
.home-grid {{ display:grid; gap:20px; grid-template-columns: repeat(12,minmax(0,1fr)); margin-bottom: 24px; }}
.home-card {{ grid-column: span 3; }}
.home-wide {{ grid-column: span 6; }}
.recent-list {{ list-style:none; margin:0; padding:0; }}
.recent-item {{ display:flex; justify-content:space-between; gap:10px; padding:10px 0; border-bottom:1px solid var(--border); font-size:13px; }}
.recent-meta {{ color: var(--text-muted); font-size:12px; }}
@media (max-width: 1100px) {{
  .home-card, .home-wide {{ grid-column: span 12; }}
}}
</style>
</head>
<body>
  <aside>
    <a class="logo" href="/agents" onclick="goHome(event)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"></path></svg> <span>onTime OS</span></a>
    {nav_html}
    <div style="margin-top:auto; padding:12px; font-size:10px; color:var(--text-muted);" id="ts">Loading...</div>
  </aside>
  <main>
    <div id="home" class="section active">
      <h2>Общий пульт</h2>
      <div class="home-grid">
        <div class="stat-box home-card"><div class="label">Задачи</div><div class="val" id="home-tasks">0</div></div>
        <div class="stat-box home-card"><div class="label">Риски</div><div class="val color-red" id="home-risks">0</div></div>
        <div class="stat-box home-card"><div class="label">Уведомления</div><div class="val color-yellow" id="home-notifications">0</div></div>
        <div class="stat-box home-card">
          <div class="label">Агентов всего</div>
          <div class="val" id="home-agents-total">0</div>
        </div>
        <div class="stat-box home-card">
          <div class="label">Агентов активны</div>
          <div class="val color-red" id="home-agents-active">0</div>
        </div>
        <div class="card home-wide">
          <h3 style="margin-bottom:12px">Ключевые показатели</h3>
          <div style="display:grid; grid-template-columns: repeat(3,minmax(0,1fr)); gap:12px;">
            <div class="stat-box"><div class="label">Подготовка AVG</div><div class="val" id="home-kpi-prep">0%</div></div>
            <div class="stat-box"><div class="label">Реализация AVG</div><div class="val" id="home-kpi-exec">0%</div></div>
            <div class="stat-box"><div class="label">Dead в очереди</div><div class="val color-red" id="home-kpi-dead">0</div></div>
          </div>
        </div>
        <div class="card home-wide">
          <h3 style="margin-bottom:12px">Последние действия</h3>
          <ul class="recent-list" id="home-recent"></ul>
        </div>
      </div>
    </div>
    <div id="wf_queues" class="section"><h2>Очереди</h2><div class="stats-grid" id="wf-counts"></div></div>
    {sections_html}
  </main>
  <div id="client-panel">
    <div class="panel-close" onclick="closeClientPanel()"><i data-feather="x"></i></div>
    <div id="panel-content">
      <h2 id="p-name">Client</h2>
      <div style="color:var(--text-muted); font-size:13px; margin-bottom:32px" id="p-client-id"></div>
      <div class="stats-grid">
        <div class="stat-box"><div class="label">Подготовка %</div><div id="p-prep" class="val"></div></div>
        <div class="stat-box"><div class="label">Реализация %</div><div id="p-exec" class="val"></div></div>
      </div>
      <div class="card premium-glow">
        <h3>Чего не хватает до 100%</h3>
        <div id="p-gaps"></div>
      </div>
      <div class="card"><h3>Статистика задач</h3>
        <p style="font-size:14px">Всего: <b id="p-total">0</b></p>
        <p style="font-size:14px">Завершено: <b id="p-done">0</b></p>
      </div>
    </div>
  </div>
<script>
const STATE_KEY = 'ontime_v3_state';
let allClients = [];
function saveState(t) {{ const groups = Array.from(document.querySelectorAll('.nav-group.open')).map(el => el.getAttribute('data-group')); const subs = Array.from(document.querySelectorAll('.sub-group.open')).map(el => el.getAttribute('data-subgroup')); localStorage.setItem(STATE_KEY, JSON.stringify({{ tab: t, groups, subs }})); }}
function loadState() {{ const s = JSON.parse(localStorage.getItem(STATE_KEY) || '{{}}'); if (s.groups) s.groups.forEach(g => toggleGroup(g, true)); if (s.subs) s.subs.forEach(sub => toggleSubGroup(sub, true)); showTab(s.tab || 'home'); }}
function goHome(e) {{ if (e) e.preventDefault(); showTab('home'); window.history.replaceState(null, '', '/agents'); }}
function toggleGroup(id, force=false) {{ const el = document.querySelector(`.nav-group[data-group="${{id}}"]`); if (!el) return; if (!force && el.classList.contains('open')) return; document.querySelectorAll('.nav-group').forEach(g => g.classList.remove('open')); el.classList.add('open'); saveState(document.querySelector('.nav-item.active')?.getAttribute('data-tab')); }}
function toggleSubGroup(id, force=false) {{ const el = document.querySelector(`.sub-group[data-subgroup="${{id}}"]`); if (!el) return; const wasOpen = el.classList.contains('open'); if (!force) {{ document.querySelectorAll('.sub-group').forEach(g => g.classList.remove('open')); if (!wasOpen) el.classList.add('open'); }} else el.classList.add('open'); saveState(document.querySelector('.nav-item.active')?.getAttribute('data-tab')); }}
function showTab(id) {{ const target = document.getElementById(id); if (!target) return showTab('home'); document.querySelectorAll('.section').forEach(s => s.classList.remove('active')); document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active')); target.classList.add('active'); const nav = document.querySelector(`.nav-item[data-tab="${{id}}"]`); if (nav) nav.classList.add('active'); saveState(id); refreshData(id); }}
async function refreshData(id) {{ if (id === 'wf_queues') {{ const r = await fetch('/api/tasks').then(r => r.json()); if (r.counts) document.getElementById('wf-counts').innerHTML = Object.entries(r.counts).map(([k,v]) => `<div class="stat-box"><div class="label">${{k}}</div><div class="val">${{v}}</div></div>`).join(''); }} if (id === 'kb_clients') refreshClients(); const st = await fetch('/api/status').then(r => r.json()); document.getElementById('ts').innerText = st.generated_at || new Date().toISOString(); }}
async function refreshData(id) {{ if (id === 'wf_queues') {{ const r = await fetch('/api/tasks').then(r => r.json()); if (r.counts) document.getElementById('wf-counts').innerHTML = Object.entries(r.counts).map(([k,v]) => `<div class="stat-box"><div class="label">${{k}}</div><div class="val">${{v}}</div></div>`).join(''); }} if (id === 'kb_clients') refreshClients(); if (id === 'home') refreshHome(); const st = await fetch('/api/status').then(r => r.json()); document.getElementById('ts').innerText = st.generated_at || new Date().toISOString(); }}

async function refreshHome() {{
    const r = await fetch('/api/home').then(r => r.json());
    if (r.status !== 'ok') return;
    const d = r.data;
    document.getElementById('home-tasks').innerText = d.tasks ?? 0;
    document.getElementById('home-risks').innerText = d.risks ?? 0;
    document.getElementById('home-notifications').innerText = d.notifications ?? 0;
    document.getElementById('home-kpi-prep').innerText = (d.kpi_prep_avg ?? 0) + '%';
    document.getElementById('home-kpi-exec').innerText = (d.kpi_exec_avg ?? 0) + '%';
    document.getElementById('home-kpi-dead').innerText = d.kpi_dead ?? 0;
    const agTotal = d.agents?.total ?? 0;
    const agActive = d.agents?.active ?? 0;
    document.getElementById('home-agents-total').innerText = agTotal;
    const agEl = document.getElementById('home-agents-active');
    agEl.innerText = agActive;
    agEl.className = 'val ' + (agActive > 0 ? 'color-green' : 'color-red');
    const recent = d.recent_actions || [];
    const recentHtml = recent.length
      ? recent.map(x => `<li class="recent-item"><div><b>${{x.task_id}}</b><div class="recent-meta">${{x.client_id}} • ${{x.task_type}}</div></div><div class="recent-meta">${{x.ts}}</div></li>`).join('')
      : '<li class="recent-item"><div class="recent-meta">Пока нет завершенных задач</div></li>';
    document.getElementById('home-recent').innerHTML = recentHtml;
}}

function getColor(p) {{
    if (p < 40) return 'color-red';
    if (p < 70) return 'color-yellow';
    if (p < 100) return 'color-blue';
    return 'color-green';
}}

async function refreshClients() {{
    const rS = await fetch('/api/clients/summary').then(r => r.json());
    if (rS.status === 'ok') {{
        document.getElementById('kpi-prep').innerText = rS.data.prep_avg + '%';
        document.getElementById('kpi-exec').innerText = rS.data.exec_avg + '%';
    }}
    const rL = await fetch('/api/clients/list').then(r => r.json());
    if (rL.status === 'ok') {{ allClients = rL.data; renderClients(allClients); }}
}}
function renderClients(list) {{
    const tbody = document.getElementById('clients-table-body');
    tbody.innerHTML = list.map(c => `<tr onclick="openClient('${{c.client_id}}')" style="cursor:pointer">
        <td><b>${{c.name}}</b><div style="font-size:11px;color:var(--text-muted)">${{c.client_id}}</div></td>
        <td class="kpi-val ${{getColor(c.prep_percent)}}">${{c.prep_percent}}%</td>
        <td class="kpi-val ${{getColor(c.exec_percent)}}">${{c.exec_percent}}%</td>
        <td style="color:var(--text-muted)">${{c.done_count}} / ${{c.total_count}}</td>
        <td><span class="badge badge-active">${{c.status}}</span></td>
        <td><button class="nav-item active" style="padding:4px 10px; font-size:10px; border:none" onclick="event.stopPropagation(); openClient('${{c.client_id}}')">ОТКРЫТЬ</button></td>
    </tr>`).join('');
    feather.replace();
}}
function filterClients() {{
    const q = document.getElementById('client-search').value.toLowerCase();
    const filtered = allClients.filter(c => c.client_id.toLowerCase().includes(q) || (c.name||'').toLowerCase().includes(q));
    renderClients(filtered);
}}
async function openClient(cid) {{
    const r = await fetch(`/api/clients/${{cid}}`).then(r => r.json());
    if (r.status === 'ok') {{
        const c = r.data;
        document.getElementById('p-name').innerText = c.name;
        document.getElementById('p-client-id').innerText = 'ID: ' + c.client_id;
        document.getElementById('p-prep').innerText = c.prep_percent + '%';
        document.getElementById('p-prep').className = 'val ' + getColor(c.prep_percent);
        document.getElementById('p-exec').innerText = c.exec_percent + '%';
        document.getElementById('p-exec').className = 'val ' + getColor(c.exec_percent);
        document.getElementById('p-done').innerText = c.done_count;
        document.getElementById('p-total').innerText = c.total_count;
        
        let gaps = '';
        if (c.missing_prep.length > 0) {{
            gaps += '<h4>Подготовка:</h4><ul class="gap-list">' + c.missing_prep.map(g => `<li class="gap-item"><i data-feather="x-circle"></i> ${{g}}</li>`).join('') + '</ul>';
        }}
        if (c.total_count === 0) {{
            gaps += '<h4 style="margin-top:16px">Реализация:</h4><ul class="gap-list"><li class="gap-item"><i data-feather="x-circle"></i> Нет ни одной задачи в очереди</li></ul>';
        }} else if (c.done_count < c.total_count) {{
            gaps += '<h4 style="margin-top:16px">Реализация:</h4><ul class="gap-list"><li class="gap-item"><i data-feather="x-circle"></i> ${{c.total_count - c.done_count}} задач не завершены</li></ul>';
        }}
        if (gaps === '') gaps = '<p style="color:var(--green)">✅ 100% Готовность</p>';
        document.getElementById('p-gaps').innerHTML = gaps;
        
        document.getElementById('client-panel').classList.add('open');
        feather.replace();
    }}
}}
function closeClientPanel() {{ document.getElementById('client-panel').classList.remove('open'); }}
window.onload = () => {{ feather.replace(); loadState(); }};
</script>
</body></html>"""

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args): pass
    def _json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def _html(self, html, code=200):
        body = html.encode(); self.send_response(code); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        url = urlparse(self.path); path = url.path.strip("/")
        ministers = get_ministers_from_registry()
        valid = ["", "home", "agents", "login", "kb_docs", "kb_sop", "kb_clients", "kb_vector", "wf_templates", "wf_runs", "wf_queues", "wf_logs", "sys_users", "sys_roles", "sys_integrations", "sys_admin"]
        valid += [f"{s}_{i.replace(' ', '_').lower()}" for s,d in {"sales":["Лиды","Клиенты","Диалоги","Сделки","Заказы","КП","Повторные продажи","Sales Core"],"content":["Контент-завод","Темы","Статьи","Посты","Публикации","Комментарии","Google-таблицы"],"marketing":["ЦА","Боли","УТП","Офферы","Воронки","Источники трафика","Реклама","Упаковка продукта"],"analytics":["Дашборды","Метрики","План-факт","Отчёты","Ошибки данных","Выводы и рекомендации"],"production":["Заказы","План","Смены","Операции","Материалы","Остатки","Брак","Загрузка"],"tech":["Сервер","Скрипты","Боты","API","Интеграции","Воркфлоу","Очереди","Логи","Ошибки"],"mgmt":["Финансы","Юрконтур","Консалтинг","PR","Задачи собственника","Стратегия","Документы"]}.items() for i in d]
        if path == "" or path in valid: self._html(get_html(ministers))
        elif path == "api/status": self._json({"runtime": "ok", "generated_at": now_ts()})
        elif path == "api/tasks": self._json(get_queue_counts())
        elif path == "api/home": self._json({"status": "ok", "data": get_home_snapshot()})
        elif path == "api/admin/db":
            try:
                dbs = []
                for p in ["/data/runtime/control.db", "/data/runtime/agents_runtime.sqlite"]:
                    path_obj = Path(p); dbs.append({"name": path_obj.name, "size": path_obj.stat().st_size if path_obj.exists() else 0, "exists": path_obj.exists()})
                self._json({"status": "ok", "databases": dbs})
            except Exception as e: self._json({"status": "error", "error": str(e)})
        elif path == "api/clients/summary": self._json({"status": "ok", "data": get_client_summary()})
        elif path == "api/clients/list": self._json({"status": "ok", "data": get_clients_detailed()})
        elif path.startswith("api/clients/"):
            parts = path.split("/")
            if len(parts) == 3:
                details = get_client_details(parts[2])
                if details: self._json({"status": "ok", "data": details})
                else: self._json({"status": "error", "message": "not found"}, 404)
        else: self._json({"error": "not found"}, 404)

if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"onTime OS started on port {PORT}")
    server.serve_forever()
