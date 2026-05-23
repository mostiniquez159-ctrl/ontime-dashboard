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
from urllib.parse import parse_qs, unquote, urlparse

# --- Configuration ---
PORT = int(os.environ.get("DASHBOARD_PORT", "8080"))
KB_ROOT = Path("/mnt/ontime/Книга знаний Агентов")
CLIENTS_ROOT = Path("/mnt/ontime/Клиенты")
QUEUE_ROOT = Path("/data/queue")
AGENT_REGISTRY = KB_ROOT / "_SYSTEM/10_REGISTRY/agent_registry.json"
CLIENT_REGISTRY = KB_ROOT / "_SYSTEM/10_REGISTRY/client_registry.json"
CHAT_STORE = KB_ROOT / "_runtime/shared/dashboard_chats_cli.json"
INTEGRATIONS_REGISTRY = KB_ROOT / "_runtime/integrations/service_registry.json"

def _load_json(path: Path, default=None):
    try:
        if not path.exists(): return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception: return default

def _save_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

def _now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def _chat_store():
    data = _load_json(CHAT_STORE, {"threads": [], "ministers": []})
    if not isinstance(data, dict):
        data = {"threads": [], "ministers": []}
    data.setdefault("threads", [])
    data.setdefault("ministers", [])
    return data

def get_client_chat(client_id):
    store = _chat_store()
    thread = next((t for t in store["threads"] if t.get("client_id") == client_id), None)
    if thread:
        return thread
    thread = {
        "thread_id": f"thr_client_{client_id}",
        "client_id": client_id,
        "title": f"Клиент {client_id} ↔ Стратег",
        "status": "active",
        "owner": "min_strateg",
        "priority": "normal",
        "updated_at": _now_iso(),
        "messages": []
    }
    store["threads"].append(thread)
    _save_json(CHAT_STORE, store)
    return thread

def append_client_chat_message(client_id, text):
    store = _chat_store()
    thread = next((t for t in store["threads"] if t.get("client_id") == client_id), None)
    if not thread:
        thread = get_client_chat(client_id)
        store = _chat_store()
        thread = next((t for t in store["threads"] if t.get("thread_id") == thread["thread_id"]), thread)

    ts = _now_iso()
    user_msg = {
        "message_id": str(uuid.uuid4()),
        "author": "you",
        "role": "human",
        "text": text.strip(),
        "created_at": ts
    }
    thread.setdefault("messages", []).append(user_msg)

    thread["updated_at"] = _now_iso()
    _save_json(CHAT_STORE, store)
    return thread

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
        
        c_json = fpath / "client.json"
        c_name = fname
        if c_json.exists():
            try: c_name = json.loads(c_json.read_text(encoding="utf-8")).get("name", fname)
            except: pass
            
        client_list.append({
            "client_id": fname, "name": c_name, "status": "unregistered",
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
        "recent_actions": recent_actions
    }

def get_client_details(client_id):
    detailed = get_clients_detailed()
    client = next((c for c in detailed if c['client_id'] == client_id), None)
    if not client: return None
    
    # Add dummy/extracted business details to avoid "old stuff" feeling
    # Try to find recent brief or brand data
    client['business'] = {
        "pain": "Хаос в процессах, отсутствие прозрачности",
        "offer": "Внедрение onTime OS за 7 дней",
        "usp": "Автономные AI-агенты + глубокая интеграция",
        "segment": "Собственники малого и среднего бизнеса"
    }
    
    # Try to override from project files if exists
    proj_p = find_project_by_run_id(client_id, "MP-") # any MP project
    if proj_p:
        brief_p = proj_p / "brief.md"
        if brief_p.exists():
            txt = brief_p.read_text(encoding="utf-8")
            m_goal = re.search(r"Цель:\*\* (.*)", txt)
            if m_goal: client['business']['offer'] = m_goal.group(1)
            
    return client


def find_project_by_run_id(client_id, run_id):
    registry = _load_json(CLIENT_REGISTRY, {}).get("clients", {})
    c_info = registry.get(client_id, {})
    c_folder = Path(c_info.get("folder", ""))
    if not c_folder.exists():
        c_folder = CLIENTS_ROOT / client_id
        if client_id.startswith("INT-") or client_id in ["pluslogo", "ontime-ai", "ra-vovremya"]:
            c_folder = CLIENTS_ROOT / "_INTERNAL" / client_id
    
    if not c_folder.exists(): return None
    
    p_dir = c_folder / "projects"
    if not p_dir.exists(): return None
    
    for p in p_dir.iterdir():
        if p.is_dir():
            st_p = p / "STATUS.md"
            if st_p.exists():
                try:
                    txt = st_p.read_text(encoding="utf-8")
                    if f"run_id: {run_id}" in txt: return p
                except: pass
            if run_id in p.name: return p
    return None

def get_client_folder(client_id):
    registry = _load_json(CLIENT_REGISTRY, {}).get("clients", {})
    c_info = registry.get(client_id, {})
    c_folder_raw = c_info.get("folder")
    c_folder = Path(c_folder_raw) if c_folder_raw else Path("__missing__")
    if not c_folder.exists():
        c_folder = CLIENTS_ROOT / client_id
        if client_id.startswith("INT-") or client_id in ["pluslogo", "ontime-ai", "ra-vovremya"]:
            c_folder = CLIENTS_ROOT / "_INTERNAL" / client_id
    return c_folder if c_folder.exists() else None

_BRIEF_PROMPTS = [
    "Какую цель нужно достичь? (например: увеличить базу клиентов на 20%)",
    "Какой продукт или услугу нужно продвигать?",
    "Какой рекламный бюджет планируется?",
    "На какой период рассчитан план? (например: Июнь 2026)",
    "Какой целевой KPI? (например: 300 лидов)",
]

def get_chat_history(client_id):
    folder = get_client_folder(client_id)
    if not folder:
        return []
    chat_file = folder / "chat.json"
    if not chat_file.exists():
        return []
    try:
        return json.loads(chat_file.read_text(encoding="utf-8"))
    except Exception:
        return []

def save_chat(client_id, history):
    folder = get_client_folder(client_id)
    if not folder:
        return False
    (folder / "chat.json").write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return True

def generate_brief_response(prior_user_count):
    if prior_user_count == 0:
        return "Привет! Я помогу собрать бриф. " + _BRIEF_PROMPTS[0]
    if prior_user_count < len(_BRIEF_PROMPTS):
        return _BRIEF_PROMPTS[prior_user_count]
    return (
        "Отлично, все данные собраны!\n\n"
        "Переходите на вкладку **Медиаплан** — заполните форму брифа "
        "с этими данными и запустите автоматическое планирование."
    )

def now_ts():
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

MARKETING_EMPTY = {
    "audiences": [], "pains": [], "usps": [], "offers": [],
    "funnels": [], "traffic_sources": [], "campaigns": [], "creatives": [], "brand": {}
}
CONTENT_EMPTY = {
    "topics": [], "articles": [], "posts": [],
    "publications": [], "comments": [], "sheets": []
}
TECH_EMPTY = {
    "bots": []
}
MODULE_EMPTY = {
    "marketing": MARKETING_EMPTY,
    "content": CONTENT_EMPTY,
    "tech": TECH_EMPTY,
}

def _client_folder(cid):
    reg = _load_json(CLIENT_REGISTRY, {}).get("clients", {})
    folder = reg.get(cid, {}).get("folder")
    if folder: return Path(folder)
    for sp in [CLIENTS_ROOT, CLIENTS_ROOT / "_INTERNAL"]:
        for d in (sp.iterdir() if sp.exists() else []):
            if d.is_dir() and d.name.upper() == cid.upper(): return d
    return CLIENTS_ROOT / cid

def get_module_data(cid, module):
    f = _client_folder(cid) / f"{module}.json"
    if not f.exists():
        return json.loads(json.dumps(MODULE_EMPTY.get(module, {}), ensure_ascii=False))
    return json.loads(f.read_text(encoding="utf-8"))

def save_module_data(cid, module, data):
    folder = _client_folder(cid); folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{module}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(folder / "log.md", "a", encoding="utf-8") as lf:
        lf.write(f"\n- {now_ts()} | {module} updated by dashboard")

def module_add_item(cid, module, section, item):
    data = get_module_data(cid, module)
    item["id"] = str(uuid.uuid4())[:8]; item["created_at"] = now_ts()
    if section == "brand": data["brand"] = item
    else: data.setdefault(section, []).append(item)
    save_module_data(cid, module, data); return item

def module_update_item(cid, module, section, item_id, updates):
    data = get_module_data(cid, module)
    if section == "brand":
        data["brand"] = dict(data.get("brand") or {})
        data["brand"].update(updates)
        data["brand"].setdefault("id", item_id or "brand")
        save_module_data(cid, module, data)
        return True
    for item in data.get(section, []):
        if item.get("id") == item_id: item.update(updates); save_module_data(cid, module, data); return True
    return False

def module_delete_item(cid, module, section, item_id):
    data = get_module_data(cid, module)
    if section == "brand":
        data["brand"] = {}
        save_module_data(cid, module, data)
        return True
    before = len(data.get(section, []))
    data[section] = [i for i in data.get(section, []) if i.get("id") != item_id]
    if len(data.get(section, [])) < before: save_module_data(cid, module, data); return True
    return False

def module_export_csv(cid, module, section):
    import csv, io
    items = get_module_data(cid, module).get(section, [])
    if not items: return ""
    buf = io.StringIO(); writer = csv.DictWriter(buf, fieldnames=list(items[0].keys()))
    writer.writeheader(); writer.writerows(items); return buf.getvalue()

def get_bots_inventory():
    def systemd_services():
        services = {}
        try:
            proc = subprocess.run(
                ["systemctl", "list-units", "--type=service", "--all", "--no-legend", "--no-pager"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except Exception:
            return services
        for raw in proc.stdout.splitlines():
            line = raw.strip()
            if not line or ".service" not in line:
                continue
            parts = line.split(None, 4)
            if len(parts) < 4:
                continue
            unit, load, active, sub = parts[:4]
            services[unit] = {
                "unit": unit,
                "load": load,
                "active": active,
                "sub": sub,
                "description": parts[4] if len(parts) > 4 else "",
            }
        return services

    def add_registry_entry(items, entry, group):
        tg_bot = entry.get("tg_bot")
        if not tg_bot or tg_bot == "inbox-only":
            return
        agent_key = (entry.get("runtime_key") or entry.get("public_identity_key") or entry.get("agent_id") or "").lower()
        if agent_key.startswith("min_"):
            agent_key = agent_key[4:]
        items.append({
            "agent_id": entry.get("agent_id") or "",
            "agent_key": agent_key,
            "tg_bot": tg_bot,
            "role": entry.get("role") or "",
            "registry_group": group,
            "registry_status": entry.get("status") or "",
        })

    services = systemd_services()
    registry = _load_json(AGENT_REGISTRY, {})
    items = []
    if isinstance(registry, dict):
        if isinstance(registry.get("bb"), dict):
            add_registry_entry(items, registry["bb"], "bb")
        for group in ["high_council", "ministers", "technicians", "workers", "unassigned"]:
            for entry in registry.get(group, []) if isinstance(registry.get(group), list) else []:
                add_registry_entry(items, entry, group)

    seen_services = set()
    for item in items:
        candidates = []
        if item["agent_key"]:
            candidates.append(f"tg-collector@{item['agent_key']}.service")
        bot_name = (item["tg_bot"] or "").lstrip("@").lower()
        bot_slug = bot_name.replace("_", "-").replace("bot", "bot")
        candidates.extend([
            f"bot-{bot_slug}.service",
            f"bot-{item['agent_key']}.service" if item["agent_key"] else "",
        ])
        service = next((c for c in candidates if c and c in services), "")
        state = services.get(service, {})
        if service:
            seen_services.add(service)
        item.update({
            "service": service,
            "active": state.get("active", "missing"),
            "sub": state.get("sub", "missing"),
            "runtime_status": "running" if state.get("active") == "active" and state.get("sub") == "running" else ("no_service" if not service else "not_running"),
            "description": state.get("description", ""),
        })

    # Front bot services are runtime objects too; show them even if registry lacks a matching agent row.
    for unit, state in services.items():
        if not unit.startswith("bot-") or unit in seen_services:
            continue
        items.append({
            "agent_id": unit.removesuffix(".service"),
            "agent_key": unit.removeprefix("bot-").removesuffix(".service"),
            "tg_bot": "",
            "role": state.get("description", ""),
            "registry_group": "systemd",
            "registry_status": "runtime_only",
            "service": unit,
            "active": state.get("active", "unknown"),
            "sub": state.get("sub", "unknown"),
            "runtime_status": "running" if state.get("active") == "active" and state.get("sub") == "running" else "not_running",
            "description": state.get("description", ""),
        })

    items.sort(key=lambda x: (x.get("registry_group", ""), x.get("agent_id", ""), x.get("service", "")))
    return {"status": "ok", "data": items, "source": "agent_registry+systemd"}

def get_bots_inventory_systemd_only():
    try:
        proc = subprocess.run(
            ["systemctl", "list-units", "--type=service", "--all", "--no-legend", "--no-pager"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception as exc:
        return {"status": "error", "message": str(exc), "data": []}

    bots = []
    for raw in proc.stdout.splitlines():
        line = raw.strip()
        if not line or ".service" not in line:
            continue
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        unit, load, active, sub = parts[:4]
        description = parts[4] if len(parts) > 4 else ""
        if not (unit.startswith("bot-") or unit.startswith("tg-collector@") or unit == "telegram-bot-api.service"):
            continue
        if unit.startswith("bot-"):
            bot_type = "bot"
        elif unit.startswith("tg-collector@"):
            bot_type = "tg-collector"
        else:
            bot_type = "telegram-api"
        bots.append({
            "unit": unit,
            "type": bot_type,
            "load": load,
            "active": active,
            "sub": sub,
            "description": description,
        })
    bots.sort(key=lambda x: (x["type"], x["unit"]))
    return {"status": "ok", "data": bots, "source": "systemd"}

def default_integrations_registry():
    return {
        "version": "1.0",
        "updated_at": now_ts(),
        "source": "_runtime/integrations/service_registry.json",
        "integrations": [],
    }

def load_integrations_registry():
    if not INTEGRATIONS_REGISTRY.exists():
        data = default_integrations_registry()
        _save_json(INTEGRATIONS_REGISTRY, data)
        return data
    data = _load_json(INTEGRATIONS_REGISTRY, default_integrations_registry())
    if not isinstance(data, dict):
        data = default_integrations_registry()
    data.setdefault("version", "1.0")
    data.setdefault("updated_at", now_ts())
    data.setdefault("source", "_runtime/integrations/service_registry.json")
    data.setdefault("integrations", [])
    if not isinstance(data["integrations"], list):
        data["integrations"] = []
    if not data["integrations"] and isinstance(data.get("services"), list):
        for svc in data["services"]:
            if not isinstance(svc, dict):
                continue
            name = svc.get("name") or svc.get("platform") or ""
            data["integrations"].append({
                "integration_id": re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or str(uuid.uuid4())[:8],
                "platform": name,
                "scope": svc.get("scope") or "system",
                "owner": svc.get("owner") or "it",
                "status": svc.get("status") if svc.get("status") in {"draft", "active", "disabled", "error", "needs_secret"} else "draft",
                "no_secret_required": bool(svc.get("no_secret_required", False)),
                "secret_ref": svc.get("secret_ref") or "",
                "last_check_at": svc.get("last_check_at") or "",
                "last_check_result": svc.get("last_check_result") or "legacy_registry",
                "allowed_agents": svc.get("allowed_agents") if isinstance(svc.get("allowed_agents"), list) else [],
                "links": svc.get("links") if isinstance(svc.get("links"), list) else [],
            })
        save_integrations_registry(data)
    normalized = [normalize_integration(x) for x in data["integrations"] if isinstance(x, dict)]
    if normalized != data["integrations"]:
        data["integrations"] = normalized
        save_integrations_registry(data)
    return data

def save_integrations_registry(data):
    data["updated_at"] = now_ts()
    _save_json(INTEGRATIONS_REGISTRY, data)

def normalize_integration(item):
    allowed_status = {"draft", "active", "disabled", "error", "needs_secret"}
    status = item.get("status") if item.get("status") in allowed_status else "draft"
    secret_ref = item.get("secret_ref") or ""
    no_secret_required = bool(item.get("no_secret_required"))
    if not secret_ref and not no_secret_required:
        status = "needs_secret"
    return {
        "integration_id": item.get("integration_id") or item.get("id") or str(uuid.uuid4())[:8],
        "platform": item.get("platform") or "",
        "scope": item.get("scope") or "system",
        "owner": item.get("owner") or "",
        "status": status,
        "secret_ref": secret_ref,
        "no_secret_required": no_secret_required,
        "last_check_at": item.get("last_check_at") or "",
        "last_check_result": item.get("last_check_result") or "",
        "allowed_agents": item.get("allowed_agents") if isinstance(item.get("allowed_agents"), list) else [],
        "links": item.get("links") if isinstance(item.get("links"), list) else [],
        "account_name": item.get("account_name") or "",
        "account_url": item.get("account_url") or "",
        "publish_mode": item.get("publish_mode") or "",
        "content_owner": item.get("content_owner") or "",
        "comment_owner": item.get("comment_owner") or "",
    }

def get_integrations_payload():
    registry = load_integrations_registry()
    items = [normalize_integration(x) for x in registry.get("integrations", []) if isinstance(x, dict)]
    return {
        "status": "ok",
        "source": str(INTEGRATIONS_REGISTRY),
        "updated_at": registry.get("updated_at"),
        "data": items,
    }

def check_integration(integration_id):
    registry = load_integrations_registry()
    found = False
    for idx, item in enumerate(registry.get("integrations", [])):
        if not isinstance(item, dict):
            continue
        norm = normalize_integration(item)
        if norm["integration_id"] != integration_id:
            continue
        found = True
        norm["last_check_at"] = now_ts()
        if norm["status"] == "needs_secret":
            norm["last_check_result"] = "needs_secret"
        elif norm["status"] == "disabled":
            norm["last_check_result"] = "disabled"
        else:
            norm["last_check_result"] = "metadata_ok"
        registry["integrations"][idx] = norm
        break
    if found:
        save_integrations_registry(registry)
    return found

def create_mock_artifacts(client_id, period, run_id, goal, product, budget, kpi):
    registry = _load_json(CLIENT_REGISTRY, {}).get("clients", {})
    c_info = registry.get(client_id, {})
    c_folder = Path(c_info.get("folder", ""))
    if not c_folder.exists():
        c_folder = CLIENTS_ROOT / client_id
        if client_id.startswith("INT-") or client_id in ["pluslogo", "ontime-ai", "ra-vovremya"]:
            c_folder = CLIENTS_ROOT / "_INTERNAL" / client_id
            
    proj_dir = c_folder / "projects" / f"MP-{period}_{run_id}"
    proj_dir.mkdir(parents=True, exist_ok=True)
    
    # 1. brief.md
    brief_content = f"""# Бриф на медиапланирование {period}
* **Клиент:** {client_id}
* **Цель:** {goal}
* **Продукт/Услуга:** {product}
* **Бюджет:** {budget}
* **Целевой KPI:** {kpi}
* **Запущено:** {now_ts()}
"""
    (proj_dir / "brief.md").write_text(brief_content, encoding="utf-8")
    
    # 2. strategy.md
    strategy_content = f"""# Стратегия продвижения для {product}
## 1. Целевая аудитория
* Основной сегмент: лица, заинтересованные в {product}.
* Боли: высокая стоимость решения, сложность выбора.

## 2. Каналы коммуникации
* Telegram Ads / Спецпроекты в каналах.
* Контекстная реклама Яндекс.Директ.
* SEO и контент-маркетинг.
"""
    (proj_dir / "strategy.md").write_text(strategy_content, encoding="utf-8")
    
    # 3. media_plan.md
    media_plan_content = f"""# Медиаплан {period}
| Канал | Бюджет | Прогноз переходов | Прогноз CPL | Прогноз лидов |
|---|---|---|---|---|
| Яндекс.Директ | 70 000 руб | 1 400 | 450 руб | 155 |
| Telegram Ads | 50 000 руб | 1 000 | 500 руб | 100 |
| Контент-завод | 30 000 руб | — | — | 45 |
| **Итого** | **{budget}** | **2 400** | **470 руб** | **300** |
"""
    (proj_dir / "media_plan.md").write_text(media_plan_content, encoding="utf-8")
    
    # 4. content_calendar.csv
    content_calendar_content = """Дата,Тема,Формат,Канал,Статус
01.06.2026,Анонс запуска,Пост,Telegram,Planned
03.06.2026,Интервью с экспертом,Статья,VC.ru,Planned
05.06.2026,Кейс использования,Пост,Telegram,Planned
"""
    (proj_dir / "content_calendar.csv").write_text(content_calendar_content, encoding="utf-8")
    
    # 5. STATUS.md
    status_content = f"""run_id: {run_id}
stage: approval
verdict: approve
updated_at: {now_ts()}
error: None
sheet_url: https://docs.google.com/spreadsheets/d/1{run_id.lower()}-sheet-canonical/edit
"""
    (proj_dir / "STATUS.md").write_text(status_content, encoding="utf-8")

def get_module_section_html(module, section_id, title):
    return f"""
    <div id="{section_id}" class="section">
      <h2>{title}</h2>
      <div style="display:flex; gap:12px; align-items:center; margin-bottom:20px; flex-wrap:wrap">
        <select id="{section_id}-client" class="premium-input module-client-select" style="width:240px" onchange="loadModuleData('{section_id}')">
          <option value="">Выберите клиента</option>
        </select>
        <button class="nav-item active" style="padding:8px 14px; font-size:12px; border:none; cursor:pointer" onclick="showModuleAddModal('{section_id}')">Добавить</button>
        <button class="nav-item" style="padding:8px 14px; font-size:12px; border:none; cursor:pointer; background:var(--surface-hover)" onclick="exportModuleCsv('{section_id}')">Экспорт CSV</button>
        <span id="{section_id}-status" style="font-size:12px; color:var(--text-muted); margin-left:auto"></span>
      </div>
      <div class="card premium-glow">
        <table class="premium-table">
          <thead id="{section_id}-thead"></thead>
          <tbody id="{section_id}-tbody"></tbody>
        </table>
      </div>
    </div>
    """

def get_bots_section_html():
    return """
    <div id="tech_боты" class="section">
      <h2>Боты</h2>
      <div style="display:flex; gap:12px; align-items:center; margin-bottom:20px; flex-wrap:wrap">
        <button class="nav-item active" style="padding:8px 14px; font-size:12px; border:none; cursor:pointer" onclick="loadBotsSection()">Обновить</button>
        <span id="bots-status" style="font-size:12px; color:var(--text-muted); margin-left:auto">Загрузка</span>
      </div>
      <div class="stats-grid" id="bots-summary" style="margin-bottom:20px"></div>
      <div class="card premium-glow">
        <table class="premium-table">
          <thead><tr><th>Агент</th><th>Бот</th><th>Роль</th><th>Runtime</th><th>Статус</th></tr></thead>
          <tbody id="bots-table-body"><tr><td colspan="5" style="color:var(--text-muted); padding:22px 12px;">Загрузка...</td></tr></tbody>
        </table>
      </div>
    </div>
    """

def get_integrations_section_html():
    return """
    <div id="tech_интеграции" class="section">
      <h2>Интеграции</h2>
      <div style="display:flex; gap:12px; align-items:center; margin-bottom:20px; flex-wrap:wrap">
        <button class="nav-item active" style="padding:8px 14px; font-size:12px; border:none; cursor:pointer" onclick="loadIntegrationsSection()">Обновить</button>
        <span id="integrations-status" style="font-size:12px; color:var(--text-muted); margin-left:auto">Загрузка</span>
      </div>
      <div class="stats-grid" id="integrations-summary" style="margin-bottom:20px"></div>
      <div class="card premium-glow">
        <table class="premium-table">
          <thead><tr><th>Платформа</th><th>Scope</th><th>Аккаунт</th><th>Статус</th><th>Secret</th><th>Last check</th><th>Действия</th></tr></thead>
          <tbody id="integrations-table-body"><tr><td colspan="7" style="color:var(--text-muted); padding:22px 12px;">Загрузка...</td></tr></tbody>
        </table>
      </div>
    </div>
    """

def get_html(ministers, initial_tab="home"):
    hierarchy = {
        "group_ministers": {
            "name": "Министерства", "icon": "layers", 
            "subgroups": {
                "sales": {"name": "Продажи", "icon": "trending-up", "items": ["Лиды", "Клиенты", "Диалоги", "Сделки", "Заказы", "КП", "Повторные продажи", "Sales Core"]},
                "content": {"name": "Контент", "icon": "edit-3", "items": ["Контент-завод", "Темы", "Статьи", "Посты", "Публикации", "Комментарии", "Google-таблицы"]},
                "marketing": {"name": "Маркетинг", "icon": "megaphone", "items": ["ЦА", "Боли", "УТП", "Офферы", "Воронки", "Источники трафика", "Реклама", "Креатив", "Упаковка продукта"]},
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
                    if t_id == "tech_боты":
                        sections_html += get_bots_section_html()
                    elif t_id == "tech_интеграции":
                        sections_html += get_integrations_section_html()
                    elif s_id in ["marketing", "content"]:
                        sections_html += get_module_section_html(s_id, t_id, item)
                    else:
                        sections_html += f'<div id="{t_id}" class="section"><h2>{s_data["name"]} • {item}</h2><div class="card premium-glow"><h3>Модуль активен</h3><p>Ожидание потока данных из контура {s_id}.</p></div></div>'
                nav_html += '</div></div>'
        else:
            for t_id, t_name in g_data["items"].items():
                nav_html += f'<div class="nav-item" data-tab="{t_id}" onclick="showTab(\'{t_id}\')">{t_name}</div>'
                if t_id != "kb_clients":
                    if t_id == "wf_queues":
                        sections_html += '<div id="wf_queues" class="section"><h2>Очереди</h2><div class="stats-grid" id="wf-counts"></div></div>'
                    else:
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
.client-tab.active {{ color: #fff !important; border-bottom: 2px solid var(--accent); }}
.client-tab:hover {{ color: #fff !important; }}
.chat-thread {{
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 12px;
  max-height: 320px;
  overflow-y: auto;
}}
.chat-msg {{
  margin-bottom: 10px;
  padding: 10px 12px;
  border-radius: 10px;
  font-size: 13px;
  line-height: 1.5;
  border: 1px solid var(--border);
}}
.chat-msg-human {{ background: rgba(59, 130, 246, 0.12); margin-left: 24px; }}
.chat-msg-minister {{ background: var(--surface-hover); margin-right: 24px; }}
.chat-meta {{ color: var(--text-muted); font-size: 11px; margin-bottom: 6px; }}
#client-chat-messages {{ display:flex; flex-direction:column; gap:10px; padding:16px; height:320px; overflow-y:auto; background:var(--bg); }}
.chat-bubble {{ max-width:85%; padding:10px 14px; border-radius:16px; font-size:13px; line-height:1.5; word-break:break-word; }}
.chat-bubble.user {{ align-self:flex-end; background:var(--accent); color:#fff; border-bottom-right-radius:4px; }}
.chat-bubble.assistant {{ align-self:flex-start; background:var(--surface-hover); border:1px solid var(--border); border-bottom-left-radius:4px; }}
.chat-ts {{ font-size:10px; opacity:0.6; margin-top:4px; text-align:right; }}
.chat-empty {{ color:var(--text-muted); font-size:13px; text-align:center; margin:auto; }}
.mkt-toolbar {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:14px; }}
.mkt-select {{ min-width:180px; }}
.mkt-actions {{ margin-left:auto; display:flex; flex-wrap:wrap; gap:8px; }}
.mkt-btn {{ border:1px solid var(--border); background:var(--surface-hover); color:#fff; padding:9px 12px; border-radius:10px; font-size:12px; font-weight:700; cursor:pointer; }}
.mkt-btn-primary {{ background:var(--accent); border-color:var(--accent); }}
.mkt-table-wrap {{ overflow:auto; }}
#mkt-sidepanel {{ position:fixed; top:0; right:0; width:420px; height:100vh; background:var(--sidebar); border-left:1px solid var(--border); z-index:2200; transform:translateX(100%); transition:0.25s ease; padding:24px; overflow:auto; }}
#mkt-sidepanel.open {{ transform:translateX(0); }}
.mkt-field {{ margin-bottom:12px; }}
.mkt-field label {{ display:block; font-size:11px; color:var(--text-muted); margin-bottom:6px; text-transform:uppercase; font-weight:700; }}
.mkt-field input {{ width:100%; }}
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
    {sections_html}
  </main>
  
  <div id="client-panel">
    <div class="panel-close" onclick="closeClientPanel()"><i data-feather="x"></i></div>
    <div id="panel-content">
      <h2 id="p-name">Client</h2>
      <div style="color:var(--text-muted); font-size:13px; margin-bottom:20px" id="p-client-id"></div>
      
      <!-- Premium Tab System -->
      <div class="client-tabs" style="display:flex; border-bottom:1px solid var(--border); margin-bottom:20px; gap:16px;">
        <div class="client-tab active" data-tab="general" onclick="showClientTab('general')" style="padding:10px 4px; cursor:pointer; font-weight:700; font-size:14px; color:var(--text-muted); transition:0.2s;">Основное</div>
        <div class="client-tab" data-tab="media-plan" onclick="showClientTab('media-plan')" style="padding:10px 4px; cursor:pointer; font-weight:700; font-size:14px; color:var(--text-muted); transition:0.2s;">Медиаплан</div>
        <div class="client-tab" data-tab="chat" onclick="showClientTab('chat')" style="padding:10px 4px; cursor:pointer; font-weight:700; font-size:14px; color:var(--text-muted); transition:0.2s;">Чат</div>
      </div>
      
      <!-- Tab 1: General -->
      <div id="client-general-tab">
        <div class="stats-grid">
          <div class="stat-box"><div class="label">Подготовка %</div><div id="p-prep" class="val"></div></div>
          <div class="stat-box"><div class="label">Реализация %</div><div id="p-exec" class="val"></div></div>
        </div>

        <div class="card premium-glow" style="margin-top:20px">
          <h3>Маркетинговая рамка</h3>
          <div id="p-business-details" style="font-size:13px; line-height:1.6; margin-top:10px;"></div>
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
      <!-- Tab 2: Media Plan -->
      <div id="client-media-plan-tab" style="display:none;">
        <div id="mp-brief-form" class="card premium-glow" style="margin-top:0;">
          <h3 style="margin-bottom:15px; font-size:16px;">Бриф на медиапланирование</h3>
          <div style="margin-bottom:12px;">
            <label style="font-size:10px; font-weight:800; color:var(--text-muted); display:block; margin-bottom:4px; text-transform:uppercase;">Цель (Goal)</label>
            <input type="text" id="mp-goal" class="premium-input" style="width:100%" placeholder="Например: Увеличить базу на 20%">
          </div>
          <div style="margin-bottom:12px;">
            <label style="font-size:10px; font-weight:800; color:var(--text-muted); display:block; margin-bottom:4px; text-transform:uppercase;">Продукт / Услуга</label>
            <input type="text" id="mp-product" class="premium-input" style="width:100%" placeholder="Например: Карта Визит">
          </div>
          <div style="margin-bottom:12px;">
            <label style="font-size:10px; font-weight:800; color:var(--text-muted); display:block; margin-bottom:4px; text-transform:uppercase;">Бюджет</label>
            <input type="text" id="mp-budget" class="premium-input" style="width:100%" placeholder="Например: 100 000 руб">
          </div>
          <div style="margin-bottom:12px;">
            <label style="font-size:10px; font-weight:800; color:var(--text-muted); display:block; margin-bottom:4px; text-transform:uppercase;">Период</label>
            <input type="text" id="mp-period" class="premium-input" style="width:100%" placeholder="Например: Июнь 2026">
          </div>
          <div style="margin-bottom:16px;">
            <label style="font-size:10px; font-weight:800; color:var(--text-muted); display:block; margin-bottom:4px; text-transform:uppercase;">Целевой KPI</label>
            <input type="text" id="mp-kpi" class="premium-input" style="width:100%" placeholder="Например: 300 лидов">
          </div>
          <div style="display:flex; gap:10px;">
            <button class="nav-item" style="flex:1; border:none; padding:12px; font-weight:700; border-radius:var(--radius-sm); cursor:pointer; text-align:center; background:var(--surface-hover); color:var(--text-muted);" onclick="resetMediaPlanForm()">Отмена</button>
            <button class="nav-item active" style="flex:2; border:none; padding:12px; font-weight:700; border-radius:var(--radius-sm); cursor:pointer; text-align:center;" onclick="runMediaPlan()">Запустить медиаплан</button>
          </div>
          <div id="mp-form-error" style="margin-top:12px; color:var(--red); font-size:12px; font-weight:700; text-align:center; display:none;"></div>

        </div>
        
        <div id="mp-status-view" class="card premium-glow" style="margin-top:0; display:none;">
          <h3 style="margin-bottom:15px; font-size:16px; display:flex; justify-content:space-between;">
            <span>Статус разработки</span>
            <button onclick="resetMediaPlanForm()" style="background:none; border:none; color:var(--text-muted); font-size:11px; cursor:pointer; text-decoration:underline;">Новый запуск</button>
          </h3>
          
          <div style="background:var(--bg); border:1px solid var(--border); padding:12px; border-radius:var(--radius-sm); font-family:monospace; font-size:11px; margin-bottom:20px; line-height:1.6;">
            <div>run_id: <span id="mp-val-run-id" style="color:var(--accent);">—</span></div>
            <div>stage: <span id="mp-val-stage" style="color:var(--yellow);">—</span></div>
            <div>verdict: <span id="mp-val-verdict" style="font-weight:bold;">—</span></div>
            <div>updated_at: <span id="mp-val-updated">—</span></div>
            <div>error: <span id="mp-val-error" style="color:var(--red);">None</span></div>
          </div>

          <div style="margin-bottom:20px;">
            <h4 style="font-size:11px; margin-bottom:10px; text-transform:uppercase; color:var(--text-muted); font-weight:800;">Прогресс проверок (Gates)</h4>
            <div id="mp-gates-container" style="display:flex; flex-direction:column; gap:8px;"></div>
          </div>

          <div style="margin-bottom:20px; display:none;" id="mp-artifacts-section">
            <h4 style="font-size:11px; margin-bottom:10px; text-transform:uppercase; color:var(--text-muted); font-weight:800;">Полученные артефакты</h4>
            <div id="mp-artifacts-container" style="display:flex; flex-direction:column; gap:6px;"></div>
          </div>

          <div style="margin-bottom:10px; display:none;" id="mp-sheet-section">
            <h4 style="font-size:11px; margin-bottom:6px; text-transform:uppercase; color:var(--text-muted); font-weight:800;">Google-таблица</h4>
            <div id="mp-sheet-container"></div>
          </div>
        </div>
      </div>
      <div id="client-chat-tab" style="display:none;">
        <div class="card premium-glow" style="margin-top:0;">
          <h3 style="margin-bottom:12px;">Диалог по клиенту</h3>
          <div id="client-chat-thread" class="chat-thread"></div>
          <div style="display:flex; gap:10px; margin-top:12px;">
            <input id="client-chat-input" type="text" class="premium-input" style="flex:1;" placeholder="Напишите сообщение...">
            <button class="nav-item active" style="border:none; padding:10px 12px; font-weight:700; border-radius:var(--radius-sm); cursor:pointer;" onclick="sendClientChat()">Отправить</button>
          </div>
        </div>
      </div>
      
    </div>
  </div>
  <div id="mkt-sidepanel">
    <div class="panel-close" onclick="closeMarketingPanel()"><i data-feather="x"></i></div>
    <h3 id="mkt-panel-title" style="margin-bottom:16px;">Карточка</h3>
    <div id="mkt-panel-fields"></div>
  </div>

<script>
const INITIAL_TAB = {json.dumps(initial_tab, ensure_ascii=False)};
const STATE_KEY = 'ontime_v3_state';
let allClients = [];
let currentClientId = null;
let pollInterval = null;
let chatPollInterval = null;

const MODULE_CONFIG = {{
  "marketing_ца": {{ title: "Ца", module: "marketing", section: "audiences", columns: ["Сегмент", "Возраст", "Гео", "Боли", "Размер", "Статус"] }},
  "marketing_боли": {{ title: "Боли", module: "marketing", section: "pains", columns: ["Боль", "Сегмент ЦА", "Интенсивность", "Статус", "Приоритет"] }},
  "marketing_утп": {{ title: "Утп", module: "marketing", section: "usps", columns: ["Формулировка", "Сегмент", "Канал", "Статус", "Владелец"] }},
  "marketing_офферы": {{ title: "Офферы", module: "marketing", section: "offers", columns: ["Заголовок", "Текст", "Канал", "Конверсия", "Статус"] }},
  "marketing_воронки": {{ title: "Воронки", module: "marketing", section: "funnels", columns: ["Этап", "Вход", "Выход", "CR%", "Статус", "Комментарий"] }},
  "marketing_источники_трафика": {{ title: "ИсточникиТрафика", module: "marketing", section: "traffic_sources", columns: ["Канал", "Бюджет", "Лиды", "CPL", "Статус", "ROI"] }},
  "marketing_реклама": {{ title: "Реклама", module: "marketing", section: "campaigns", columns: ["Кампания", "Канал", "Бюджет", "Статус", "Ссылка"] }},
  "marketing_креатив": {{ title: "Креатив", module: "marketing", section: "creatives", columns: ["Название", "Тип", "Канал", "Оффер", "Статус", "Конверсия"] }},
  "marketing_упаковка_продукта": {{ title: "УпаковкаПродукта", module: "marketing", section: "brand", columns: ["Параметр", "Значение", "Статус", "Комментарий"] }},
  "content_контент-завод": {{ title: "КонтентЗавод", module: "content", section: "topics", columns: ["Задача", "Статус", "Исполнитель"] }},
  "content_темы": {{ title: "Темы", module: "content", section: "topics", columns: ["Тема", "Приоритет", "Статус", "Дедлайн"] }},
  "content_статьи": {{ title: "Статьи", module: "content", section: "articles", columns: ["Заголовок", "Автор", "Статус", "Дата"] }},
  "content_посты": {{ title: "Посты", module: "content", section: "posts", columns: ["Текст", "Сеть", "Статус", "Дата"] }},
  "content_публикации": {{ title: "Публикации", module: "content", section: "publications", columns: ["Ресурс", "Ссылка", "Статус", "Дата"] }},
  "content_комментарии": {{ title: "Комментарии", module: "content", section: "comments", columns: ["Текст", "Где", "Статус", "Дата"] }},
  "content_google-таблицы": {{ title: "GoogleТаблицы", module: "content", section: "sheets", columns: ["Название", "URL", "Статус", "Комментарий"] }}
}};

function saveState(t) {{ const groups = Array.from(document.querySelectorAll('.nav-group.open')).map(el => el.getAttribute('data-group')); const subs = Array.from(document.querySelectorAll('.sub-group.open')).map(el => el.getAttribute('data-subgroup')); localStorage.setItem(STATE_KEY, JSON.stringify({{ tab: t, groups, subs }})); }}
function loadState() {{ const s = JSON.parse(localStorage.getItem(STATE_KEY) || '{{}}'); if (s.groups) s.groups.forEach(g => toggleGroup(g, true)); if (s.subs) s.subs.forEach(sub => toggleSubGroup(sub, true)); showTab(INITIAL_TAB || s.tab || 'home'); }}
function goHome(e) {{ if (e) e.preventDefault(); showTab('home'); window.history.replaceState(null, '', '/agents'); }}
function toggleGroup(id, force=false) {{ const el = document.querySelector(`.nav-group[data-group="${{id}}"]`); if (!el) return; if (!force && el.classList.contains('open')) return; document.querySelectorAll('.nav-group').forEach(g => g.classList.remove('open')); el.classList.add('open'); saveState(document.querySelector('.nav-item.active')?.getAttribute('data-tab')); }}
function toggleSubGroup(id, force=false) {{ const el = document.querySelector(`.sub-group[data-subgroup="${{id}}"]`); if (!el) return; const wasOpen = el.classList.contains('open'); if (!force) {{ document.querySelectorAll('.sub-group').forEach(g => g.classList.remove('open')); if (!wasOpen) el.classList.add('open'); }} else el.classList.add('open'); saveState(document.querySelector('.nav-item.active')?.getAttribute('data-tab')); }}
function showTab(id) {{ const target = document.getElementById(id); if (!target) return showTab('home'); document.querySelectorAll('.section').forEach(s => s.classList.remove('active')); document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active')); target.classList.add('active'); const nav = document.querySelector(`.nav-item[data-tab="${{id}}"]`); if (nav) nav.classList.add('active'); saveState(id); refreshData(id); if (id === 'tech_боты') loadBotsSection(); if (id === 'tech_интеграции') loadIntegrationsSection(); if (MODULE_CONFIG[id]) initModuleTab(id); else closeMarketingPanel(); }}
async function refreshData(id) {{ if (id === 'wf_queues') {{ const r = await fetch('/api/tasks').then(r => r.json()); if (r.counts) document.getElementById('wf-counts').innerHTML = Object.entries(r.counts).map(([k,v]) => `<div class="stat-box"><div class="label">${{k}}</div><div class="val">${{v}}</div></div>`).join(''); }} if (id === 'kb_clients') refreshClients(); if (id === 'home') refreshHome(); const st = await fetch('/api/status').then(r => r.json()); document.getElementById('ts').innerText = st.generated_at || new Date().toISOString(); }}

async function loadBotsSection() {{
    const status = document.getElementById('bots-status');
    const summary = document.getElementById('bots-summary');
    const tbody = document.getElementById('bots-table-body');
    if (!tbody) return;
    if (status) status.innerText = 'Загрузка';
    try {{
        const r = await fetch('/api/system/bots').then(res => res.json());
        const bots = r.data || [];
        const running = bots.filter(b => b.runtime_status === 'running').length;
        const noService = bots.filter(b => b.runtime_status === 'no_service').length;
        const notRunning = bots.filter(b => b.runtime_status === 'not_running').length;
        if (summary) summary.innerHTML = `
          <div class="stat-box"><div class="label">Всего</div><div class="val">${{bots.length}}</div></div>
          <div class="stat-box"><div class="label">Работают</div><div class="val">${{running}}</div></div>
          <div class="stat-box"><div class="label">Нет service</div><div class="val">${{noService}}</div></div>
          <div class="stat-box"><div class="label">Не running</div><div class="val">${{notRunning}}</div></div>
        `;
        if (!bots.length) {{
            tbody.innerHTML = '<tr><td colspan="5" style="color:var(--text-muted); padding:22px 12px;">В agent_registry нет TG-ботов</td></tr>';
        }} else {{
            tbody.innerHTML = bots.map(b => `
              <tr>
                <td><b>${{b.agent_id || b.agent_key || ''}}</b><div style="font-size:11px;color:var(--text-muted)">${{b.agent_key || ''}}</div></td>
                <td>${{b.tg_bot || ''}}</td>
                <td style="color:var(--text-muted)">${{b.role || ''}}</td>
                <td>${{b.service || 'нет service'}}</td>
                <td><span class="badge ${{b.runtime_status === 'running' ? 'badge-active' : (b.runtime_status === 'no_service' ? '' : 'badge-error')}}">${{b.runtime_status === 'running' ? 'running' : (b.runtime_status === 'no_service' ? 'service не найден' : b.active + ' / ' + b.sub)}}</span></td>
              </tr>
            `).join('');
        }}
        if (status) status.innerText = 'Источник: agent_registry + systemd';
    }} catch (e) {{
        tbody.innerHTML = '<tr><td colspan="5" style="color:var(--red); padding:22px 12px;">Ошибка загрузки /api/system/bots</td></tr>';
        if (status) status.innerText = 'Ошибка';
    }}
}}

async function loadIntegrationsSection() {{
    const status = document.getElementById('integrations-status');
    const summary = document.getElementById('integrations-summary');
    const tbody = document.getElementById('integrations-table-body');
    if (!tbody) return;
    if (status) status.innerText = 'Загрузка';
    try {{
        const r = await fetch('/api/integrations').then(res => res.json());
        const items = r.data || [];
        const active = items.filter(x => x.status === 'active').length;
        const needsSecret = items.filter(x => x.status === 'needs_secret').length;
        const errors = items.filter(x => x.status === 'error').length;
        if (summary) summary.innerHTML = `
          <div class="stat-box"><div class="label">Всего</div><div class="val">${{items.length}}</div></div>
          <div class="stat-box"><div class="label">Active</div><div class="val">${{active}}</div></div>
          <div class="stat-box"><div class="label">Needs secret</div><div class="val">${{needsSecret}}</div></div>
          <div class="stat-box"><div class="label">Error</div><div class="val">${{errors}}</div></div>
        `;
        if (!items.length) {{
            tbody.innerHTML = '<tr><td colspan="7" style="color:var(--text-muted); padding:22px 12px;">Интеграции не заведены. Registry создан: _runtime/integrations/service_registry.json</td></tr>';
        }} else {{
            tbody.innerHTML = items.map(x => {{
                const link = (x.account_url || (x.links && x.links[0]) || '').toString();
                const statusClass = x.status === 'active' ? 'badge-active' : (x.status === 'error' ? 'badge-error' : '');
                const secret = x.no_secret_required ? 'no_secret_required' : (x.secret_ref || 'needs_secret');
                return `
                  <tr>
                    <td><b>${{x.platform || x.integration_id}}</b><div style="font-size:11px;color:var(--text-muted)">${{x.integration_id}}</div></td>
                    <td>${{x.scope || ''}}</td>
                    <td>${{link ? `<a href="${{link}}" target="_blank">${{x.account_name || link}}</a>` : (x.account_name || '')}}</td>
                    <td><span class="badge ${{statusClass}}">${{x.status}}</span></td>
                    <td style="color:var(--text-muted)">${{secret}}</td>
                    <td style="color:var(--text-muted)">${{x.last_check_at || ''}} ${{x.last_check_result || ''}}</td>
                    <td><button class="mkt-btn" onclick="checkIntegration('${{x.integration_id}}')">Проверить</button>${{link ? `<button class="mkt-btn" onclick="window.open('${{link}}','_blank')">Открыть</button>` : ''}}</td>
                  </tr>
                `;
            }}).join('');
        }}
        if (status) status.innerText = 'Источник: ' + (r.source || '_runtime/integrations/service_registry.json');
    }} catch (e) {{
        tbody.innerHTML = '<tr><td colspan="7" style="color:var(--red); padding:22px 12px;">Ошибка загрузки /api/integrations</td></tr>';
        if (status) status.innerText = 'Ошибка';
    }}
}}

async function checkIntegration(id) {{
    const status = document.getElementById('integrations-status');
    if (status) status.innerText = 'Проверка';
    const r = await fetch(`/api/integrations/${{id}}/check`, {{ method: 'POST' }}).then(res => res.json());
    if (r.status !== 'ok') alert(r.message || 'Ошибка проверки');
    loadIntegrationsSection();
}}

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
    const recent = d.recent_actions || [];
    const recentHtml = recent.length
      ? recent.map(x => `<li class="recent-item"><div><b>${{x.task_id}}</b><div class="recent-meta">${{x.client_id}} • ${{x.task_type}}</div></div><div class="recent-meta">${{x.ts}}</div></li>`).join('')
      : '<li class="recent-item"><div class="recent-meta">Пока нет завершенных задач</div></li>';
    document.getElementById('home-recent').innerHTML = recentHtml;
}}

function initModuleTab(tabId) {{
    const sel = document.getElementById(tabId + '-client');
    if (!sel) return;
    ensureClientsLoaded().then(() => {{
        const opts = ['<option value="">Выберите клиента</option>'].concat(allClients.map(c => `<option value="${{c.client_id}}">${{c.name}} (${{c.client_id}})</option>`)).join('');
        sel.innerHTML = opts;
        const savedClient = localStorage.getItem('ontime_selected_client') || currentClientId;
        if (savedClient) currentClientId = savedClient;
        if (currentClientId) {{
            sel.value = currentClientId;
            loadModuleData(tabId);
        }} else {{
            renderModuleEmpty(tabId, 'Выберите клиента сверху');
        }}
    }});
}}

async function loadModuleData(tabId) {{
    const cid = document.getElementById(tabId + '-client').value;
    if (!cid) return renderModuleEmpty(tabId, 'Выберите клиента сверху');
    currentClientId = cid;
    localStorage.setItem('ontime_selected_client', cid);
    const cfg = MODULE_CONFIG[tabId];
    setModuleStatus(tabId, 'Загрузка');
    try {{
        const r = await fetch(`/api/module/${{cid}}/${{cfg.module}}/${{cfg.section}}`).then(res => res.json());
        if (r.status === 'ok') {{
            renderModuleTable(tabId, r.data);
            setModuleStatus(tabId, 'Клиент: ' + cid);
        }} else {{
            renderModuleEmpty(tabId, r.message || 'Ошибка загрузки');
            setModuleStatus(tabId, 'Ошибка');
        }}
    }} catch (e) {{
        renderModuleEmpty(tabId, 'Ошибка сети');
        setModuleStatus(tabId, 'Ошибка');
    }}
}}

function setModuleStatus(tabId, text) {{
    const el = document.getElementById(tabId + '-status');
    if (el) el.innerText = text || '';
}}

function renderModuleEmpty(tabId, text) {{
    const cfg = MODULE_CONFIG[tabId];
    const thead = document.getElementById(tabId + '-thead');
    const tbody = document.getElementById(tabId + '-tbody');
    if (!thead || !tbody) return;
    thead.innerHTML = '<tr>' + cfg.columns.map(c => `<th>${{c}}</th>`).join('') + '<th>Действия</th></tr>';
    tbody.innerHTML = `<tr><td colspan="${{cfg.columns.length + 1}}" style="color:var(--text-muted); padding:22px 12px;">${{text || 'Нет записей'}} <button class="mkt-btn" style="margin-left:12px" onclick="showModuleAddModal('${{tabId}}')">Добавить запись</button></td></tr>`;
}}

function renderModuleTable(tabId, items) {{
    const cfg = MODULE_CONFIG[tabId];
    const thead = document.getElementById(tabId + '-thead');
    const tbody = document.getElementById(tabId + '-tbody');
    const cols = cfg.columns;
    
    thead.innerHTML = '<tr>' + cols.map(c => `<th>${{c}}</th>`).join('') + '<th>Действия</th></tr>';
    
    const rows = (Array.isArray(items) ? items : (items && Object.keys(items).length ? [items] : [])).filter(Boolean);
    if (!rows.length) return renderModuleEmpty(tabId, 'Нет записей');
    tbody.innerHTML = rows.map(item => `
        <tr>
            ${{cols.map(c => `<td>${{item[c] || ''}}</td>`).join('')}}
            <td>
                <button class="mkt-btn" onclick="showModuleEditModal('${{tabId}}', '${{item.id || 'brand'}}')">Редактировать</button>
                <button class="mkt-btn" onclick="deleteModuleItem('${{tabId}}', '${{item.id || 'brand'}}')">Удалить</button>
            </td>
        </tr>
    `).join('');
}}

function showModuleAddModal(tabId) {{
    const cfg = MODULE_CONFIG[tabId];
    const cid = document.getElementById(tabId + '-client').value;
    if (!cid) return alert('Сначала выберите клиента');
    
    document.getElementById('mkt-panel-title').innerText = 'Добавить: ' + cfg.title;
    const host = document.getElementById('mkt-panel-fields');
    host.innerHTML = cfg.columns.map(c => `<div class="mkt-field"><label>${{c}}</label><input class="premium-input" id="mkt-inp-${{c}}"></div>`).join('') + 
                     `<button class="nav-item active" style="width:100%; border:none; padding:12px; margin-top:12px" onclick="saveModuleItem('${{tabId}}')">Сохранить</button>`;
    document.getElementById('mkt-sidepanel').classList.add('open');
}}

function showModuleEditModal(tabId, itemId) {{
    const cfg = MODULE_CONFIG[tabId];
    const cid = document.getElementById(tabId + '-client').value;
    
    fetch(`/api/module/${{cid}}/${{cfg.module}}/${{cfg.section}}`)
        .then(r => r.json())
        .then(r => {{
            if (r.status !== 'ok') return;
            const item = (Array.isArray(r.data) ? r.data : [r.data]).find(i => i.id === itemId);
            if (!item) return;

            document.getElementById('mkt-panel-title').innerText = 'Редактировать: ' + cfg.title;
            const host = document.getElementById('mkt-panel-fields');
            host.innerHTML = cfg.columns.map(c => `<div class="mkt-field"><label>${{c}}</label><input class="premium-input" id="mkt-inp-${{c}}" value="${{item[c] || ''}}"></div>`).join('') + 
                             `<button class="nav-item active" style="width:100%; border:none; padding:12px; margin-top:12px" onclick="saveModuleItem('${{tabId}}', '${{itemId}}')">Сохранить</button>`;
            document.getElementById('mkt-sidepanel').classList.add('open');
        }});
}}

async function saveModuleItem(tabId, itemId = null) {{
    const cfg = MODULE_CONFIG[tabId];
    const cid = document.getElementById(tabId + '-client').value;
    if (!cid) return alert('Выберите клиента');
    const body = {{}};
    if (itemId) body.id = itemId;
    cfg.columns.forEach(c => {{
        body[c] = document.getElementById('mkt-inp-' + c).value;
    }});
    
    const action = itemId ? 'update' : 'add';
    setModuleStatus(tabId, 'Сохранение');
    const r = await fetch(`/api/module/${{cid}}/${{cfg.module}}/${{cfg.section}}/${{action}}`, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(body)
    }}).then(res => res.json());
    
    if (r.status === 'ok') {{
        closeMarketingPanel();
        loadModuleData(tabId);
    }} else {{
        setModuleStatus(tabId, 'Ошибка сохранения');
        alert('Ошибка сохранения');
    }}
}}

async function deleteModuleItem(tabId, itemId) {{
    if (!confirm('Удалить запись?')) return;
    const cfg = MODULE_CONFIG[tabId];
    const cid = document.getElementById(tabId + '-client').value;
    if (!cid) return alert('Выберите клиента');
    const r = await fetch(`/api/module/${{cid}}/${{cfg.module}}/${{cfg.section}}/delete`, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ id: itemId }})
    }}).then(res => res.json());
    if (r.status === 'ok') loadModuleData(tabId);
}}

function exportModuleCsv(tabId) {{
    const cid = document.getElementById(tabId + '-client').value;
    if (!cid) return alert('Выберите клиента');
    const cfg = MODULE_CONFIG[tabId];
    window.open(`/api/module-export/${{cid}}/${{cfg.module}}/${{cfg.section}}`, '_blank');
}}

async function ensureClientsLoaded() {{
  if (allClients.length) return;
  try {{
    const rL = await fetch('/api/clients/list').then(r => r.json());
    if (rL.status === 'ok') allClients = rL.data || [];
  }} catch (e) {{}}
}}

// Global functions for HTML event handlers
Object.keys(MODULE_CONFIG).forEach(tabId => {{
    const cfg = MODULE_CONFIG[tabId];
    const jsName = cfg.title;
    window['load' + jsName] = () => loadModuleData(tabId);
    window['show' + jsName + 'AddModal'] = () => showModuleAddModal(tabId);
    window['export' + jsName + 'Csv'] = () => exportModuleCsv(tabId);
}});
function renderClients(list) {{
    const tbody = document.getElementById('clients-table-body');
    tbody.innerHTML = list.map(c => `<tr onclick="openClient('${{c.client_id}}')" style="cursor:pointer">
        <td><b>${{c.name}}</b><div style="font-size:11px;color:var(--text-muted)">${{c.client_id}}</div></td>
        <td class="kpi-val ${{getColor(c.prep_percent)}}">${{c.prep_percent}}%</td>
        <td class="kpi-val ${{getColor(c.exec_percent)}}">${{c.exec_percent}}%</td>
        <td style="color:var(--text-muted)">${{c.done_count}} / ${{c.total_count}}</td>
        <td><span class="badge badge-active">${{c.status}}</span></td>
        <td style="display:flex; gap:6px; align-items:center">
          <button class="nav-item active" style="padding:4px 10px; font-size:10px; border:none" onclick="event.stopPropagation(); openClient('${{c.client_id}}')">ОТКРЫТЬ</button>
          <button class="nav-item" style="padding:4px 10px; font-size:10px; border:none; background:rgba(239,68,68,0.15); color:var(--red)" onclick="event.stopPropagation(); deleteClient('${{c.client_id}}')">УДАЛИТЬ</button>
        </td>
    </tr>`).join('');
    feather.replace();
}}
function filterClients() {{
    const q = document.getElementById('client-search').value.toLowerCase();
    const filtered = allClients.filter(c => c.client_id.toLowerCase().includes(q) || (c.name||'').toLowerCase().includes(q));
    renderClients(filtered);
}}

function showClientTab(tabName) {{
    document.querySelectorAll('.client-tab').forEach(el => el.classList.remove('active'));
    const clickedTab = document.querySelector(`.client-tab[data-tab="${{tabName}}"]`);
    if (clickedTab) clickedTab.classList.add('active');

    if (tabName === 'general') {{
        document.getElementById('client-general-tab').style.display = 'block';
        document.getElementById('client-media-plan-tab').style.display = 'none';
        document.getElementById('client-chat-tab').style.display = 'none';
        stopChatPolling();
    }} else if (tabName === 'media-plan') {{
        document.getElementById('client-general-tab').style.display = 'none';
        document.getElementById('client-media-plan-tab').style.display = 'block';
        document.getElementById('client-chat-tab').style.display = 'none';
        stopChatPolling();
        checkActiveMediaPlan();
    }} else {{
        document.getElementById('client-general-tab').style.display = 'none';
        document.getElementById('client-media-plan-tab').style.display = 'none';
        document.getElementById('client-chat-tab').style.display = 'block';
        refreshClientChat();
        startChatPolling();
    }}
}}

function stopChatPolling() {{
    if (chatPollInterval) {{
        clearInterval(chatPollInterval);
        chatPollInterval = null;
    }}
}}

function startChatPolling() {{
    stopChatPolling();
    chatPollInterval = setInterval(() => {{
        const panelOpen = document.getElementById('client-panel').classList.contains('open');
        const chatVisible = document.getElementById('client-chat-tab').style.display !== 'none';
        if (panelOpen && chatVisible) refreshClientChat();
    }}, 4000);
}}

async function refreshClientChat() {{
    if (!currentClientId) return;
    const r = await fetch(`/api/clients/${{currentClientId}}/chat`).then(res => res.json());
    if (r.status !== 'ok') return;
    const messages = (r.data && r.data.messages) || [];
    const threadEl = document.getElementById('client-chat-thread');
    threadEl.innerHTML = messages.length
      ? messages.map(m => {{
          const klass = m.role === 'human' ? 'chat-msg chat-msg-human' : 'chat-msg chat-msg-minister';
          const author = m.author || '—';
          const ts = m.created_at || '';
          return `<div class="${{klass}}"><div class="chat-meta">${{author}} • ${{ts}}</div><div>${{m.text || ''}}</div></div>`;
        }}).join('')
      : '<div class="chat-meta">Сообщений пока нет</div>';
    threadEl.scrollTop = threadEl.scrollHeight;
}}

async function sendClientChat() {{
    if (!currentClientId) return;
    const input = document.getElementById('client-chat-input');
    const text = (input.value || '').trim();
    if (!text) return;
    const res = await fetch(`/api/clients/${{currentClientId}}/chat`, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ text }})
    }}).then(r => r.json());
    if (res.status === 'ok') {{
        input.value = '';
        refreshClientChat();
    }} else {{
        alert('Ошибка отправки сообщения');
    }}
}}

function resetMediaPlanForm() {{
    if (pollInterval) {{ clearInterval(pollInterval); pollInterval = null; }}
    document.getElementById('mp-brief-form').style.display = 'block';
    document.getElementById('mp-status-view').style.display = 'none';
    document.getElementById('mp-form-error').style.display = 'none';
    
    // Clear inputs
    document.getElementById('mp-goal').value = '';
    document.getElementById('mp-product').value = '';
    document.getElementById('mp-budget').value = '';
    document.getElementById('mp-period').value = '';
    document.getElementById('mp-kpi').value = '';
    
    showClientTab('general');
}}

async function checkActiveMediaPlan() {{
    if (pollInterval) {{ clearInterval(pollInterval); pollInterval = null; }}
    
    const r = await fetch(`/api/clients/${{currentClientId}}/media-plan/latest`).then(res => res.json());
    if (r.status === 'ok' && r.data) {{
        showMediaPlanStatus(r.data);
        if (r.data.verdict === 'pending' || r.data.verdict === 'rework') {{
            startMediaPlanPolling(r.data.run_id);
        }}
    }} else {{
        resetMediaPlanForm();
    }}
}}

function showMediaPlanStatus(data) {{
    document.getElementById('mp-brief-form').style.display = 'none';
    document.getElementById('mp-status-view').style.display = 'block';
    
    document.getElementById('mp-val-run-id').innerText = data.run_id || '—';
    document.getElementById('mp-val-stage').innerText = data.stage || '—';
    
    const verdictEl = document.getElementById('mp-val-verdict');
    verdictEl.innerText = data.verdict || '—';
    if (data.verdict === 'approve') {{
        verdictEl.className = 'color-green';
    }} else if (data.verdict === 'blocker' || data.verdict === 'escalate') {{
        verdictEl.className = 'color-red';
    }} else if (data.verdict === 'rework') {{
        verdictEl.className = 'color-yellow';
        verdictEl.innerText = 'rework (1/2)';
    }} else {{
        verdictEl.className = 'color-blue';
    }}
    
    document.getElementById('mp-val-updated').innerText = data.updated_at || '—';
    document.getElementById('mp-val-error').innerText = data.error || 'None';
    
    // Render Gates
    const gatesContainer = document.getElementById('mp-gates-container');
    const gates = data.gates || [];
    gatesContainer.innerHTML = gates.map((g, idx) => {{
        let vClass = 'color-blue';
        let vText = g.verdict || 'pending';
        if (g.verdict === 'approve') vClass = 'color-green';
        else if (g.verdict === 'blocker' || g.verdict === 'escalate') vClass = 'color-red';
        else if (g.verdict === 'rework') {{
            vClass = 'color-yellow';
            vText = 'rework (1/2)';
        }}
        return `<div style="display:flex; justify-content:space-between; align-items:center; background:var(--surface-hover); padding:10px; border-radius:var(--radius-sm); border:1px solid var(--border);">
            <span style="font-size:13px; font-weight:600;">${{g.name}}</span>
            <span class="${{vClass}}" style="font-size:12px; font-weight:700; text-transform:uppercase;">${{vText}}</span>
        </div>`;
    }}).join('');
    
    // Render Artifacts
    const artSection = document.getElementById('mp-artifacts-section');
    const artsContainer = document.getElementById('mp-artifacts-container');
    const arts = data.artifacts || {{}};
    const hasArts = Object.keys(arts).length > 0;
    
    if (hasArts) {{
        artSection.style.display = 'block';
        artsContainer.innerHTML = Object.entries(arts).map(([name, path]) => {{
            return `<div style="font-size:12px; display:flex; justify-content:space-between; background:var(--surface-hover); padding:8px 12px; border-radius:var(--radius-sm); border:1px solid var(--border);">
                <span style="color:var(--text-muted); font-family:monospace;">${{name}}</span>
                <span style="color:var(--accent); cursor:pointer;" onclick="alert('Путь: ${{path}}')">Посмотреть</span>
            </div>`;
        }}).join('');
    }} else {{
        artSection.style.display = 'none';
    }}
    
    // Render Sheet URL
    const sheetSection = document.getElementById('mp-sheet-section');
    const sheetContainer = document.getElementById('mp-sheet-container');
    if (data.sheet_url) {{
        sheetSection.style.display = 'block';
        sheetContainer.innerHTML = `<a href="${{data.sheet_url}}" target="_blank" style="color:var(--accent); font-weight:700; text-decoration:underline; font-size:13px;">${{data.sheet_url}}</a>`;
    }} else {{
        sheetSection.style.display = 'none';
    }}
}}

async function runMediaPlan() {{
    const goal = document.getElementById('mp-goal').value;
    const product = document.getElementById('mp-product').value;
    const budget = document.getElementById('mp-budget').value;
    const period = document.getElementById('mp-period').value;
    const kpi = document.getElementById('mp-kpi').value;
    const errEl = document.getElementById('mp-form-error');
    
    if (!goal || !product || !budget || !period || !kpi) {{
        errEl.innerText = 'Заполните все поля брифа!';
        errEl.style.display = 'block';
        return;
    }}
    errEl.style.display = 'none';
    
    const res = await fetch(`/api/clients/${{currentClientId}}/media-plan/run`, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ goal, product_service: product, budget, period, kpi }})
    }}).then(r => r.json());
    
    if (res.status === 'ok') {{
        const runId = res.run_id;
        startMediaPlanPolling(runId);
    }} else {{
        alert('Ошибка при запуске: ' + res.message);
    }}
}}

function startMediaPlanPolling(runId) {{
    if (pollInterval) clearInterval(pollInterval);
    
    pollMediaPlanStatus(runId);
    pollInterval = setInterval(() => {{
        pollMediaPlanStatus(runId);
    }}, 2000);
}}

async function pollMediaPlanStatus(runId) {{
    const r = await fetch(`/api/clients/${{currentClientId}}/media-plan/${{runId}}`).then(res => res.json());
    if (r.status === 'ok') {{
        showMediaPlanStatus(r.data);
        const term = ['approve', 'blocker', 'escalate', 'dead'];
        if (term.includes(r.data.verdict)) {{
            clearInterval(pollInterval);
            pollInterval = null;
            const rC = await fetch(`/api/clients/${{currentClientId}}`).then(r => r.json());
            if (rC.status === 'ok') {{
                document.getElementById('p-done').innerText = rC.data.done_count;
                document.getElementById('p-total').innerText = rC.data.total_count;
            }}
        }}
    }}
}}

async function openClient(cid) {{
    currentClientId = cid;
    showClientTab('general');
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
        
        const b = c.business || {{}};
        document.getElementById('p-business-details').innerHTML = `
            <div style="margin-bottom:8px"><b>Боль:</b> <span style="color:var(--text-muted)">${{b.pain || '—'}}</span></div>
            <div style="margin-bottom:8px"><b>Оффер:</b> <span style="color:var(--text-muted)">${{b.offer || '—'}}</span></div>
            <div style="margin-bottom:8px"><b>УТП:</b> <span style="color:var(--text-muted)">${{b.usp || '—'}}</span></div>
            <div style="margin-bottom:8px"><b>Сегмент:</b> <span style="color:var(--text-muted)">${{b.segment || '—'}}</span></div>
        `;

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

function closeClientPanel() {{
    if (pollInterval) {{ clearInterval(pollInterval); pollInterval = null; }}
    stopChatPolling();
    document.getElementById('client-panel').classList.remove('open');
}}

window.onload = () => {{
    feather.replace();
    loadState();
    setInterval(() => {{
        const active = document.querySelector('.section.active');
        if (active && active.id) refreshData(active.id);
    }}, 15000);
}};
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args): pass
    def _json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0"); self.send_header("Pragma", "no-cache"); self.send_header("Expires", "0"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def _html(self, html, code=200):
        body = html.encode(); self.send_response(code); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0"); self.send_header("Pragma", "no-cache"); self.send_header("Expires", "0"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        url = urlparse(self.path); path = unquote(url.path).strip("/")
        aliases = {
            "agents/marketing/ca": "marketing_ца",
            "agents/marketing/pains": "marketing_боли",
            "agents/marketing/usp": "marketing_утп",
            "agents/marketing/offers": "marketing_офферы",
            "agents/marketing/funnels": "marketing_воронки",
            "agents/marketing/traffic": "marketing_источники_трафика",
            "agents/marketing/ads": "marketing_реклама",
            "agents/marketing/creative": "marketing_креатив",
            "agents/marketing/packaging": "marketing_упаковка_продукта",
            "agents/tech/bots": "tech_боты",
            "agents/tech/integrations": "tech_интеграции",
        }
        ministers = get_ministers_from_registry()
        valid = ["", "home", "agents", "login", "kb_docs", "kb_sop", "kb_clients", "kb_vector", "wf_templates", "wf_runs", "wf_queues", "wf_logs", "sys_users", "sys_roles", "sys_integrations", "sys_admin"]
        valid += [f"{s}_{i.replace(' ', '_').lower()}" for s,d in {"sales":["Лиды","Клиенты","Диалоги","Сделки","Заказы","КП","Повторные продажи","Sales Core"],"content":["Контент-завод","Темы","Статьи","Посты","Публикации","Комментарии","Google-таблицы"],"marketing":["ЦА","Боли","УТП","Офферы","Воронки","Источники трафика","Реклама","Креатив","Упаковка продукта"],"analytics":["Дашборды","Метрики","План-факт","Отчёты","Ошибки данных","Выводы и рекомендации"],"production":["Заказы","План","Смены","Операции","Материалы","Остатки","Брак","Загрузка"],"tech":["Сервер","Скрипты","Боты","API","Интеграции","Воркфлоу","Очереди","Логи","Ошибки"],"mgmt":["Финансы","Юрконтур","Консалтинг","PR","Задачи собственника","Стратегия","Документы"]}.items() for i in d]
        if path in aliases:
            self._html(get_html(ministers, aliases[path]))
        elif path == "" or path in valid:
            init_tab = "home" if path in ("", "agents", "home") else path
            self._html(get_html(ministers, init_tab))
        elif path == "api/status": self._json({"runtime": "ok", "generated_at": now_ts()})
        elif path == "api/tasks": self._json(get_queue_counts())
        elif path == "api/home": self._json({"status": "ok", "data": get_home_snapshot()})
        elif path == "api/clients/summary": self._json({"status": "ok", "data": get_client_summary()})
        elif path == "api/clients/list": self._json({"status": "ok", "data": get_clients_detailed()})
        elif path == "api/system/bots": self._json(get_bots_inventory())
        elif path == "api/integrations": self._json(get_integrations_payload())
        elif path.startswith("api/module/"):
            parts = path.split("/")  # api/module/{cid}/{module}/{section}
            if len(parts) == 5:
                cid, module, section = parts[2], parts[3], parts[4]
                data = get_module_data(cid, module)
                self._json({"status": "ok", "data": data.get(section, data if section == "brand" else [])})
            elif len(parts) == 4:
                cid, module = parts[2], parts[3]
                self._json({"status": "ok", "data": get_module_data(cid, module)})
        elif path.startswith("api/module-export/"):
            parts = path.split("/")
            if len(parts) == 5:
                csv_data = module_export_csv(parts[2], parts[3], parts[4])
                body = csv_data.encode("utf-8-sig")
                self.send_response(200); self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", f"attachment; filename={parts[4]}.csv")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
        elif path.startswith("api/clients/"):
            parts = path.split("/")
            if len(parts) == 4 and parts[3] == "chat":
                history = get_chat_history(parts[2])
                self._json({"status": "ok", "data": {"messages": history}})
                return
            if len(parts) == 5 and parts[3] == "media-plan" and parts[4] == "latest":
                client_id_param = parts[2]
                latest_run = None
                latest_mtime = 0
                
                for state in ["done", "dead", "processing", "pending"]:
                    d = QUEUE_ROOT / state
                    if not d.exists(): continue
                    for f in d.glob("*.json"):
                        try:
                            payload = json.loads(f.read_text(encoding="utf-8"))
                            if payload.get("client_id") == client_id_param and payload.get("task_type") == "media_plan_v3":
                                mtime = f.stat().st_mtime
                                if mtime > latest_mtime:
                                    latest_mtime = mtime
                                    latest_run = payload.get("run_id")
                        except: continue
                
                if not latest_run:
                    registry = _load_json(CLIENT_REGISTRY, {}).get("clients", {})
                    c_info = registry.get(client_id_param, {})
                    c_folder = Path(c_info.get("folder", ""))
                    if not c_folder.exists():
                        c_folder = CLIENTS_ROOT / client_id_param
                        if client_id_param.startswith("INT-") or client_id_param in ["pluslogo", "ontime-ai", "ra-vovremya"]:
                            c_folder = CLIENTS_ROOT / "_INTERNAL" / client_id_param
                    if c_folder.exists():
                        p_dir = c_folder / "projects"
                        if p_dir.exists():
                            for proj in p_dir.iterdir():
                                if proj.is_dir() and "RUN-MP-" in proj.name:
                                    mtime = proj.stat().st_mtime
                                    if mtime > latest_mtime:
                                        latest_mtime = mtime
                                        latest_run = proj.name.split("_")[-1]
                
                if latest_run:
                    parts[4] = latest_run
                else:
                    self._json({"status": "ok", "data": None})
                    return

            if len(parts) == 3:
                details = get_client_details(parts[2])
                if details: self._json({"status": "ok", "data": details})
                else: self._json({"status": "error", "message": "not found"}, 404)
            
            elif len(parts) == 5 and parts[3] == "media-plan" and parts[4] != "run":
                client_id_param = parts[2]
                run_id_param = parts[4]

                res_data = {
                    "stage": "discovery", "verdict": "pending", 
                    "gates": [
                        {"name": "Gate-1: Анализ ЦА", "verdict": "pending", "updated_at": ""},
                        {"name": "Gate-2: Выбор каналов", "verdict": "pending", "updated_at": ""},
                        {"name": "Gate-3: Бюджет и ROI", "verdict": "pending", "updated_at": ""},
                        {"name": "Gate-4: Утверждение", "verdict": "pending", "updated_at": ""}
                    ], 
                    "artifacts": {}, "sheet_url": None, "error": None, "updated_at": now_ts(), "run_id": run_id_param
                }

                task_found = False; found_state = None; payload = {}
                for state in ["done", "dead", "processing", "pending"]:
                    d = QUEUE_ROOT / state
                    if not d.exists(): continue
                    for f in d.glob("*" + run_id_param + "*.json"):
                        try:
                            tmp = json.loads(f.read_text(encoding="utf-8"))
                            if tmp.get("client_id") == client_id_param:
                                payload = tmp; found_state = state; task_found = True; break
                        except: continue
                    if task_found: break

                if task_found:
                    res_data["updated_at"] = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).isoformat()
                    if found_state == "done":
                        res_data.update({"stage": "approval", "verdict": "approve"})
                        for g in res_data["gates"]: g.update({"verdict": "approve", "updated_at": res_data["updated_at"]})
                    elif found_state == "dead":
                        res_data.update({"stage": "approval", "verdict": "blocker", "error": "Task failed"})
                        for g in res_data["gates"]: g.update({"verdict": "blocker", "updated_at": res_data["updated_at"]})
                    else:
                        # Simulation
                        try:
                            c_dt = datetime.strptime(payload.get("created_at", "").replace(" UTC", ""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                            elap = (datetime.now(timezone.utc) - c_dt).total_seconds()
                            if elap > 5: res_data["gates"][0]["verdict"] = "approve"
                            if elap > 10: res_data["gates"][1]["verdict"] = "approve"
                        except: pass

                # Linkage
                proj_p = find_project_by_run_id(client_id_param, run_id_param)
                if proj_p:
                    for art in ["brief.md", "strategy.md", "media_plan.md", "content_calendar.csv"]:
                        if (proj_p / art).exists(): res_data["artifacts"][art] = str(proj_p / art)
                    st_p = proj_p / "STATUS.md"
                    if st_p.exists():
                        txt = st_p.read_text(encoding="utf-8")
                        import re
                        m = re.search(r"sheet_url:\s*(https://\S+)", txt)
                        if m: res_data["sheet_url"] = m.group(1)
                        if "verdict: approve" in txt: res_data["verdict"] = "approve"

                self._json({"status": "ok", "data": res_data})
                
            elif len(parts) == 6 and parts[3] == "media-plan" and parts[4] == "artifacts":
                            # GET /api/clients/{client_id}/media-plan/artifacts/{run_id}
                            cid = parts[2]; rid = parts[5]
                            arts = {}
                            proj_p = find_project_by_run_id(cid, rid)
                            if proj_p:
                                for art in ["brief.md", "strategy.md", "media_plan.md", "content_calendar.csv"]:
                                    if (proj_p / art).exists(): arts[art] = str(proj_p / art)
                            self._json({"status": "ok", "data": arts})
            else:
                self._json({"error": "not found"}, 404)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        url = urlparse(self.path); path = unquote(url.path).strip("/")
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

        if path.startswith("api/clients/") and path.endswith("/chat"):
            cid = path.split("/")[2]
            text = str(body.get("text", "")).strip()
            if not text:
                self._json({"status": "error", "message": "message text required"}, 400)
                return
            ts = _now_iso()
            history = get_chat_history(cid)
            prior_user_count = sum(1 for m in history if m.get("role") == "user")
            history.append({"role": "user", "content": text, "ts": ts})
            bot_reply = generate_brief_response(prior_user_count)
            history.append({"role": "assistant", "content": bot_reply, "ts": _now_iso()})
            save_chat(cid, history)
            self._json({"status": "ok", "data": {"messages": history}})
        elif path.startswith("api/clients/") and path.endswith("/media-plan/run"):
            cid = path.split("/")[2]
            goal = body.get("goal")
            product = body.get("product_service")
            budget = body.get("budget")
            period = body.get("period")
            kpi = body.get("kpi")
            
            if not all([goal, product, budget, period, kpi]):
                self._json({"status": "error", "message": "all brief fields required"}, 400)
                return
            
            run_id = f"RUN-MP-{uuid.uuid4().hex[:6].upper()}"
            task_id = f"TASK-{run_id}"
            
            payload = {
                "task_id": task_id,
                "run_id": run_id,
                "client_id": cid,
                "project_id": f"MP-{period}",
                "task_type": "media_plan_v3",
                "sop_id": "SOP-MEDIA-PLAN-V3-001",
                "goal_metric": kpi,
                "brief": body,
                "created_at": now_ts()
            }
            
            p_dir = QUEUE_ROOT / "pending"
            p_dir.mkdir(parents=True, exist_ok=True)
            (p_dir / f"{task_id}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            
            self._json({"status": "ok", "run_id": run_id})
        elif path.startswith("api/module/"):
            parts = path.split("/")  # api/module/{cid}/{module}/{section}/action
            if len(parts) == 6:
                cid, module, section, action = parts[2], parts[3], parts[4], parts[5]
                if action == "add":
                    item = module_add_item(cid, module, section, body)
                    self._json({"status": "ok", "data": item})
                elif action == "update":
                    ok = module_update_item(cid, module, section, body.get("id"), body)
                    self._json({"status": "ok"} if ok else {"status": "error", "message": "not found"})
                elif action == "delete":
                    ok = module_delete_item(cid, module, section, body.get("id"))
                    self._json({"status": "ok"} if ok else {"status": "error", "message": "not found"})
        elif path.startswith("api/integrations/") and path.endswith("/check"):
            parts = path.split("/")
            integration_id = parts[2] if len(parts) >= 4 else ""
            ok = check_integration(integration_id)
            self._json({"status": "ok"} if ok else {"status": "error", "message": "integration not found"}, 404 if not ok else 200)
        elif path == "api/marketing/export-sheet" or path.endswith("api/marketing/export-sheet"):
            client_id = str(body.get("client_id", "")).strip()
            tab_id = str(body.get("tab_id", "")).strip()
            project_id = str(body.get("project_id", "all")).strip() or "all"
            columns = body.get("columns") or []
            rows = body.get("rows") or []
            if not client_id or not tab_id:
                self._json({"status": "error", "message": "client_id and tab_id required"}, 400)
                return
            if not isinstance(columns, list) or not isinstance(rows, list):
                self._json({"status": "error", "message": "columns/rows must be arrays"}, 400)
                return

            c_folder = get_client_folder(client_id)
            if not c_folder:
                self._json({"status": "error", "message": f"client folder not found: {client_id}"}, 404)
                return

            export_dir = c_folder / "marketing" / "exports"
            export_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            base = f"{tab_id}_{project_id}_{ts}"
            json_path = export_dir / f"{base}.json"
            csv_path = export_dir / f"{base}.csv"

            json_path.write_text(json.dumps({
                "client_id": client_id,
                "project_id": project_id,
                "tab_id": tab_id,
                "columns": columns,
                "rows": rows,
                "exported_at": now_ts(),
            }, ensure_ascii=False, indent=2), encoding="utf-8")

            def _csv_escape(v):
                s = str(v if v is not None else "")
                return '"' + s.replace('"', '""') + '"'
            csv_lines = []
            if columns:
                csv_lines.append(",".join(_csv_escape(c) for c in columns))
                for row in rows:
                    if isinstance(row, dict):
                        csv_lines.append(",".join(_csv_escape(row.get(c, "")) for c in columns))
            csv_path.write_text("\n".join(csv_lines) + ("\n" if csv_lines else ""), encoding="utf-8")

            log_path = c_folder / "marketing" / "log.md"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"- {now_ts()} export_sheet tab={tab_id} project={project_id} json={json_path.name} csv={csv_path.name}\n")

            sheet_url = None
            p_dir = c_folder / "projects"
            if p_dir.exists():
                latest_status = None
                latest_mtime = 0
                for p in p_dir.iterdir():
                    st = p / "STATUS.md"
                    if st.exists():
                        m = st.stat().st_mtime
                        if m > latest_mtime:
                            latest_mtime = m
                            latest_status = st
                if latest_status:
                    txt = latest_status.read_text(encoding="utf-8")
                    m = re.search(r"sheet_url:\s*(https://\S+)", txt)
                    if m:
                        sheet_url = m.group(1)

            self._json({
                "status": "ok",
                "client_id": client_id,
                "tab_id": tab_id,
                "export_path": str(csv_path),
                "json_path": str(json_path),
                "sheet_url": sheet_url
            })
        else:
            self._json({"error": "not found"}, 404)

if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"onTime OS started on port {PORT}")
    server.serve_forever()
