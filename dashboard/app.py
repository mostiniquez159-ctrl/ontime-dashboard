#!/usr/bin/env python3
import json
import html as html_lib
import os
import subprocess
import time
import re
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

# --- Shared Data & Constants ---
from data import *
from templates import get_html, get_module_section_html

# --- Configuration ---
PORT = int(os.environ.get("DASHBOARD_PORT", "8080"))

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


def _stage_thread_id(client_id, stage, run_id=""):
    key = (stage or "").strip().lower().replace(" ", "_")
    rid = (run_id or "na").strip()
    return f"thr_stage_{client_id}_{key}_{rid}"


def get_stage_chat(client_id, stage, run_id=""):
    store = _chat_store()
    tid = _stage_thread_id(client_id, stage, run_id)
    thread = next((t for t in store["threads"] if t.get("thread_id") == tid), None)
    if thread:
        return thread
    thread = {
        "thread_id": tid,
        "client_id": client_id,
        "stage": stage,
        "run_id": run_id or "",
        "title": f"{client_id} • {stage}",
        "status": "active",
        "updated_at": _now_iso(),
        "messages": [],
    }
    store["threads"].append(thread)
    _save_json(CHAT_STORE, store)
    return thread


def append_stage_chat_message(client_id, stage, run_id, author, text):
    store = _chat_store()
    tid = _stage_thread_id(client_id, stage, run_id)
    thread = next((t for t in store["threads"] if t.get("thread_id") == tid), None)
    if not thread:
        thread = get_stage_chat(client_id, stage, run_id)
        store = _chat_store()
        thread = next((t for t in store["threads"] if t.get("thread_id") == tid), thread)
    thread.setdefault("messages", []).append({
        "message_id": str(uuid.uuid4()),
        "author": author,
        "role": "assistant" if author != "you" else "human",
        "text": text,
        "created_at": _now_iso(),
    })
    thread["updated_at"] = _now_iso()
    _save_json(CHAT_STORE, store)

def get_queue_counts():
    counts = {"pending": 0, "high": 0, "processing": 0, "done": 0, "dead": 0}
    if QUEUE_ROOT.exists():
        for d in QUEUE_ROOT.iterdir():
            if d.is_dir() and d.name in counts:
                counts[d.name] = len(list(d.glob("*.json")))
    return {"counts": counts}


def get_workflow_templates():
    return {"data": [
        {"template": "content_plan_v1", "module": "content", "description": "Построение контент-плана"},
        {"template": "media_plan_v3", "module": "marketing", "description": "Медиаплан с gate-проверками"},
        {"template": "cjm_stage_action", "module": "sales", "description": "Запуск этапа воронки по клиенту"},
        {"template": "sla_escalation", "module": "sales", "description": "Эскалация просрочки SLA"},
        {"template": "chat_workflow_step", "module": "sales", "description": "Запуск шага из чата"},
    ]}


def get_workflow_runs(limit=300):
    rows = []
    for state in ["pending", "processing", "done", "dead", "blocked"]:
        d = QUEUE_ROOT / state
        if not d.exists():
            continue
        for f in d.glob("*.json"):
            try:
                p = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            rows.append({
                "task_id": p.get("task_id", f.stem),
                "task_type": p.get("task_type", ""),
                "client_id": p.get("client_id", ""),
                "state": state,
                "priority": p.get("priority", ""),
                "assigned_agent": p.get("assigned_agent", ""),
                "created_at": p.get("created_at", datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).isoformat()),
            })
    rows.sort(key=lambda x: str(x.get("created_at", "")), reverse=True)
    return {"data": rows[:limit]}


def get_workflow_logs(limit=200):
    rows = []
    for root in WF_LOG_CANDIDATES:
        if not root.exists():
            continue
        for f in root.glob("*.log"):
            try:
                st = f.stat()
            except Exception:
                continue
            rows.append({
                "file": f.name,
                "path": str(f),
                "size_kb": round(st.st_size / 1024, 1),
                "updated_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            })
    rows.sort(key=lambda x: x["updated_at"], reverse=True)
    return {"data": rows[:limit]}

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



def _normalize_funnel_stage(val):
    if val in FUNNEL_STAGES:
        return val
    return FUNNEL_STAGES[0]


def _promote_stage(current, candidate):
    cur = _normalize_funnel_stage(current)
    cand = _normalize_funnel_stage(candidate)
    return cand if FUNNEL_STAGE_RANK[cand] > FUNNEL_STAGE_RANK[cur] else cur


def _sync_sales_transitions(registry):
    leads = registry.get("leads", []) or []
    deals = registry.get("deals", []) or []
    clients = registry.get("clients", []) or []

    # Ensure normalized stages on all entities.
    for item in leads:
        item["Этап воронки"] = _normalize_funnel_stage(item.get("Этап воронки"))
    for item in deals:
        item["Этап воронки"] = _normalize_funnel_stage(item.get("Этап воронки"))
    for item in clients:
        item["Этап воронки"] = _normalize_funnel_stage(item.get("Этап воронки"))

    by_client_name = {str(c.get("Название", "")).strip().lower(): c for c in clients}

    # Lead-driven promotion by sales status.
    lead_status_map = {
        "qualified": "Есть потребность / нет решения",
        "квалифицирован": "Есть потребность / нет решения",
        "proposal": "Есть решение / нет покупки",
        "кп отправлено": "Есть решение / нет покупки",
        "negotiation": "Есть решение / нет покупки",
        "переговоры": "Есть решение / нет покупки",
        "won": "Есть покупка / нет лояльности",
        "выиграна": "Есть покупка / нет лояльности",
    }
    for lead in leads:
        client_key = str(lead.get("Клиент", "")).strip().lower()
        if not client_key:
            continue
        target = by_client_name.get(client_key)
        if not target:
            continue
        st = str(lead.get("Статус", "")).strip().lower()
        cand = lead_status_map.get(st)
        if cand:
            target["Этап воронки"] = _promote_stage(target.get("Этап воронки"), cand)

    # Deal-driven promotion.
    for deal in deals:
        client_key = str(deal.get("Клиент", "")).strip().lower()
        if not client_key:
            continue
        target = by_client_name.get(client_key)
        if not target:
            continue
        deal_stage = _normalize_funnel_stage(deal.get("Этап воронки"))
        target["Этап воронки"] = _promote_stage(target.get("Этап воронки"), deal_stage)

        deal_status = str(deal.get("Стадия", "")).strip().lower()
        if deal_status in ("won", "выиграна"):
            target["Этап воронки"] = _promote_stage(target.get("Этап воронки"), "Повторная сделка")

    return registry


def _apply_loyalty_transition_to_sales(client_id, section, item):
    reg = get_sales_registry()
    clients = reg.get("clients", []) or []
    target = None
    for c in clients:
        if str(c.get("_client_id", "")).strip() == str(client_id).strip():
            target = c
            break
    if not target:
        return

    if section == "repeat_sales":
        src_stage = item.get("Этап воронки") or "Повторная сделка"
        target["Этап воронки"] = _promote_stage(target.get("Этап воронки"), src_stage)
    elif section == "referrals":
        target["Этап воронки"] = _promote_stage(target.get("Этап воронки"), "Рекомендатель")

    reg["clients"] = clients
    reg = _sync_sales_transitions(reg)
    save_sales_registry(reg)


def get_sales_cjm_rows():
    reg = get_sales_registry()
    leads = reg.get("leads", []) or []
    clients = reg.get("clients", []) or []
    deals = reg.get("deals", []) or []
    rows = []
    runtime = reg.get("_stage_runtime", {}) if isinstance(reg.get("_stage_runtime", {}), dict) else {}
    for stage in FUNNEL_STAGES:
        l_cnt = sum(1 for x in leads if _normalize_funnel_stage(x.get("Этап воронки")) == stage)
        c_cnt = sum(1 for x in clients if _normalize_funnel_stage(x.get("Этап воронки")) == stage)
        d_cnt = sum(1 for x in deals if _normalize_funnel_stage(x.get("Этап воронки")) == stage)
        total = l_cnt + c_cnt + d_cnt
        risk = "Высокий риск" if total == 0 else ("Средний риск" if total < 3 else "Норма")
        action = {
            "Нет потребности / есть проблема": "Усилить охват и лид-магниты",
            "Есть потребность / нет решения": "Добавить оффер и кейсы",
            "Есть решение / нет покупки": "Закрыть возражения и ремаркетинг",
            "Есть покупка / нет лояльности": "Запустить post-sale сопровождение",
            "Лояльный клиент": "Запросить отзыв и кейс",
            "Рекомендатель": "Запустить реферальный цикл",
            "Повторная сделка": "Upsell/Crosssell пакет",
        }.get(stage, "Проверить данные")
        rt = runtime.get(stage, {}) if isinstance(runtime.get(stage, {}), dict) else {}
        rows.append({
            "CJM-состояние": stage,
            "Лиды": l_cnt,
            "Клиенты": c_cnt,
            "Сделки": d_cnt,
            "Риск": risk,
            "Рекомендованное действие": action,
            "Ответственный": rt.get("agent", "—"),
            "Дедлайн": rt.get("deadline", "—"),
            "Последний запуск": rt.get("updated_at", "—"),
        })
    return rows


def get_sales_sla_rows():
    reg = get_sales_registry()
    leads = reg.get("leads", []) or []
    now = datetime.now(timezone.utc)
    sla_days = {
        "Нет потребности / есть проблема": 14,
        "Есть потребность / нет решения": 10,
        "Есть решение / нет покупки": 7,
        "Есть покупка / нет лояльности": 21,
        "Лояльный клиент": 30,
        "Рекомендатель": 30,
        "Повторная сделка": 45,
    }
    rows = []
    by_stage = {s: [] for s in FUNNEL_STAGES}
    for l in leads:
        st = _normalize_funnel_stage(l.get("Этап воронки"))
        by_stage.setdefault(st, []).append(l)
    runtime = reg.get("_stage_runtime", {}) if isinstance(reg.get("_stage_runtime", {}), dict) else {}
    for st in FUNNEL_STAGES:
        items = by_stage.get(st, [])
        overdue = 0
        max_age = 0
        for i in items:
            raw = str(i.get("created_at") or i.get("Дата") or "").strip()
            age_days = 0
            if raw:
                try:
                    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    age_days = max(0, (now - dt).days)
                except Exception:
                    age_days = 0
            max_age = max(max_age, age_days)
            if age_days > sla_days.get(st, 14):
                overdue += 1
        rt = runtime.get(st, {}) if isinstance(runtime.get(st, {}), dict) else {}
        rows.append({
            "Этап": st,
            "SLA (дни)": sla_days.get(st, 14),
            "Лидов в этапе": len(items),
            "Просрочено": overdue,
            "Макс. возраст (дни)": max_age,
            "Статус": "Требует внимания" if overdue > 0 else "В норме",
            "Ответственный": rt.get("agent", "—"),
            "Дедлайн": rt.get("deadline", "—"),
            "Последний запуск": rt.get("updated_at", "—"),
        })
    return rows


def _auto_create_sla_escalations(registry, rows):
    runtime = registry.get("_stage_runtime", {})
    if not isinstance(runtime, dict):
        runtime = {}
    escalations = registry.get("_escalations", [])
    if not isinstance(escalations, list):
        escalations = []
    created = 0
    for r in rows:
        stage = r.get("Этап")
        if not stage or int(r.get("Просрочено", 0) or 0) <= 0:
            continue
        rt = runtime.get(stage, {}) if isinstance(runtime.get(stage, {}), dict) else {}
        if rt.get("status") == "escalated":
            continue
        if any(e.get("stage") == stage and e.get("status") == "open" for e in escalations):
            continue
        task_id = f"TASK-SLA-AUTO-{uuid.uuid4().hex[:8].upper()}"
        payload = {
            "task_id": task_id,
            "task_type": "sla_escalation",
            "stage": stage,
            "priority": "high",
            "created_at": now_ts(),
        }
        p_dir = QUEUE_ROOT / "pending"
        p_dir.mkdir(parents=True, exist_ok=True)
        (p_dir / f"{task_id}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        escalations.append({
            "id": str(uuid.uuid4())[:8],
            "task_id": task_id,
            "stage": stage,
            "status": "open",
            "priority": "high",
            "created_at": now_ts(),
            "reason": "auto_sla_breach",
        })
        rt["status"] = "escalated"
        rt["task_id"] = task_id
        rt["updated_at"] = now_ts()
        runtime[stage] = rt
        created += 1
    registry["_stage_runtime"] = runtime
    registry["_escalations"] = escalations
    return created

def get_sales_clients():
    reg = get_sales_registry()
    clients_reg = reg.get("clients", [])
    if isinstance(clients_reg, list) and clients_reg:
        for c in clients_reg:
            c.setdefault("Этап воронки", FUNNEL_STAGES[0])
        return clients_reg

    clients = get_clients_detailed()
    mapped = []
    for c in clients:
        mapped.append({
            "id": c.get("client_id"),
            "Название": c.get("name", c.get("client_id", "")),
            "Контакт": c.get("contact", "—"),
            "Этап воронки": FUNNEL_STAGES[0],
            "Статус": c.get("status", "active"),
            "Ответственный": c.get("responsible", "sales_manager"),
            "Активность": c.get("last_activity", c.get("updated_at") or "—"),
            "Сделки": c.get("open_deals", 0),
        })
    return mapped

def get_sales_dialogs():
    dialogs = []
    scan_paths = [CLIENTS_ROOT, CLIENTS_ROOT / "_INTERNAL"]
    for sp in scan_paths:
        if not sp.exists(): continue
        for d in sp.iterdir():
            if not d.is_dir() or d.name == "_INTERNAL": continue
            chat_f = d / "chat.json"
            if chat_f.exists():
                chat_data = _load_json(chat_f, [])
                if chat_data:
                    last_msg = chat_data[-1]
                    dialogs.append({
                        "id": d.name,
                        "client_id": d.name,
                        "client_name": d.name,
                        "channel": "telegram",
                        "last_text": last_msg.get("content", last_msg.get("text", "")),
                        "status": "active",
                        "responsible": "sales_manager",
                        "time": last_msg.get("ts", last_msg.get("created_at", ""))
                    })
    return dialogs

def calculate_prep_kpi(folder_path):
    folder = Path(folder_path)
    missing = []
    score = 0
    scaffold = {
        "client.md": "Описание клиента (client.md)",
        "index.md": "Карта навигации (index.md)",
        "log.md": "Журнал активности (log.md)",
        "_DASHBOARD.md": "Дашборд (_DASHBOARD.md)",
        "brand/brand.json": "Бренд-бук (brand/brand.json)",
        "brief.md": "Общий бриф (brief.md)",
        "brand/guidelines.md": "Гайдлайны бренда (guidelines.md)",
        "knowledge/geo.json": "География GEO (geo.json)",
        "knowledge/hooks.json": "Библиотека хуков (hooks.json)",
        "knowledge/products.md": "Описание продуктов (products.md)",
        "knowledge/competitors.md": "Анализ конкурентов (competitors.md)",
        "profile/brief.md": "Профиль: Бриф (profile/brief.md)",
        "roadmap/prep.json": "Дорожная карта подготовки (roadmap/prep.json)"
    }
    if not folder.exists():
        return 0, list(scaffold.values())

    found_count = 0
    for rel, label in scaffold.items():
        if (folder / rel).exists(): found_count += 1
        else: missing.append(label)
    score = int((found_count / len(scaffold)) * 100)
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
        
        is_placeholder = False
        brand_json_path = folder / "brand" / "brand.json"
        if brand_json_path.exists():
            try:
                brand_data = json.loads(brand_json_path.read_text(encoding="utf-8"))
                if brand_data.get("brand_name") == "Placeholder Brand" or "Placeholder" in str(brand_data.get("brand_name")):
                    is_placeholder = True
            except:
                pass
        client_md_path = folder / "client.md"
        if client_md_path.exists():
            try:
                txt = client_md_path.read_text(encoding="utf-8")
                if "Описание деятельности и ключевых функций" in txt:
                    is_placeholder = True
            except:
                pass

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
            "is_placeholder": is_placeholder,
            "updated_at": datetime.fromtimestamp(folder.stat().st_mtime, tz=timezone.utc).isoformat() if folder.exists() else None,
            "path": str(folder)
        })
        if cid in folders_found: del folders_found[cid]
        # Also remove by folder basename to avoid duplicates when cid != folder name
        folder_basename = folder.name
        if folder_basename in folders_found: del folders_found[folder_basename]

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
            "is_placeholder": False,
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
    # Notifications should reflect actionable runtime alerts, not registry hygiene noise.
    notifications = q.get("dead", 0) + q.get("high", 0)

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

    dr = get_dr_health_snapshot()
    return {
        "tasks": q.get("pending", 0) + q.get("processing", 0),
        "risks": risks,
        "notifications": notifications,
        "kpi_prep_avg": csum.get("prep_avg", 0),
        "kpi_exec_avg": csum.get("exec_avg", 0),
        "kpi_dead": q.get("dead", 0),
        "recent_actions": recent_actions,
        "server_recovery": dr.get("server_recovery"),
        "backup_storage": dr.get("backup_storage"),
    }


def get_dr_health_snapshot():
    cov_p = Path("/data/runtime/cloud_coverage_report.json")
    sto_p = Path("/data/runtime/backup_storage_report.json")
    cov = _load_json(cov_p, None) if cov_p.exists() else None
    sto = _load_json(sto_p, None) if sto_p.exists() else None
    if not isinstance(cov, dict):
        cov = {
            "status": "UNKNOWN",
            "docs_sync_pass": False,
            "dr_backup_pass": False,
            "off_host_copy_pass": False,
            "restore_smoke_pass": False,
            "server_recovery_pass": False,
            "missing_zones": ["report_missing"],
            "warnings": ["coverage_report_missing"],
        }
    if not isinstance(sto, dict):
        sto = {
            "status": "UNKNOWN",
            "local_backup_gb": None,
            "cloud_backup_gb": None,
            "stage_gb": None,
            "cleanup_candidates": [],
            "warnings": ["storage_report_missing"],
            "budget": {},
        }
    return {"server_recovery": cov, "backup_storage": sto}

def get_client_details(client_id):
    detailed = get_clients_detailed()
    client = next((c for c in detailed if c["client_id"] == client_id), None)
    if not client: return None
    
    # Unit Economics
    ue = {"target_cpl": "—", "current_cpl": "—"}
    c_folder = get_client_folder(client_id)
    if c_folder:
        # Target CPL from brief.md
        brief_p = c_folder / "profile" / "brief.md"
        if brief_p.exists():
            txt = brief_p.read_text(encoding="utf-8")
            m = re.search(r"\| target_cpl \| (.*) \|", txt)
            if m:
                ue["target_cpl"] = m.group(1).strip()
        
        # Current CPL from analytics.json
        an_p = c_folder / "analytics.json"
        if an_p.exists():
            try:
                adata = json.loads(an_p.read_text(encoding="utf-8"))
                ue["current_cpl"] = adata.get("current_cpl", "—")
            except: pass
    client["unit_economics"] = ue

    client["business"] = {
        "pain": "Хаос в процессах, отсутствие прозрачности",
        "offer": "Внедрение onTime OS за 7 дней",
        "usp": "Автономные AI-агенты + глубокая интеграция",
        "segment": "Собственники малого и среднего бизнеса"
    }
    
    if c_folder:
        prod_path = c_folder / "knowledge" / "products.json"
        if prod_path.exists():
            try:
                pdata = json.loads(prod_path.read_text(encoding="utf-8"))
                client["products"] = pdata.get("products", [])
                client["products_profile"] = pdata.get("profile", "")
            except:
                client["products"] = []
        else:
            client["products"] = []
            

        aud_path = c_folder / "knowledge" / "audience.json"
        if aud_path.exists():
            try:
                adata = json.loads(aud_path.read_text(encoding="utf-8"))
                client["avatars"] = adata.get("avatars", [])
            except:
                client["avatars"] = []
        else:
            client["avatars"] = []

        # New: Load Hooks
        hooks_path = c_folder / "knowledge" / "hooks.json"
        if hooks_path.exists():
            try:
                hdata = json.loads(hooks_path.read_text(encoding="utf-8"))
                client["hooks"] = hdata.get("hooks", [])
            except: client["hooks"] = []
        else: client["hooks"] = []

        # New: Load GEO
        geo_path = c_folder / "knowledge" / "geo.json"
        if geo_path.exists():
            try:
                gdata = json.loads(geo_path.read_text(encoding="utf-8"))
                client["geo"] = gdata
            except: client["geo"] = {}
        else: client["geo"] = {}

    else:
        client["products"] = []
        client["avatars"] = []

    proj_p = find_project_by_run_id(client_id, "MP-")
    if proj_p:
        brief_p = proj_p / "brief.md"
        if brief_p.exists():
            txt = brief_p.read_text(encoding="utf-8")
            m_goal = re.search(r"Цель:\*\* (.*)", txt)
            if m_goal: client["business"]["offer"] = m_goal.group(1)
            
    return client

def get_client_roadmap(client_id):
    c_folder = get_client_folder(client_id)
    if not c_folder:
        return {"stats": {"prep_pct": 0, "impl_pct": 0, "blocked": False}}
    
    roadmap_dir = c_folder / "roadmap"
    prep = _load_json(roadmap_dir / "prep.json", {})
    impl = _load_json(roadmap_dir / "impl.json", {})
    
    def calc_stats(data):
        total_items = 0
        done_items = 0
        phases = data.get("phases", [])
        for phase in phases:
            for item in phase.get("checklist", []):
                if item.get("status") == "na":
                    continue
                total_items += 1
                if item.get("status") == "done":
                    done_items += 1
        
        pct = round((done_items / total_items) * 100) if total_items > 0 else 0
        current_phase = data.get("current_phase", 0)
        total_phases = len(phases)
        status = data.get("status", "not_started")
        
        return pct, current_phase, total_phases, status

    prep_pct, prep_cur, prep_total, prep_status = calc_stats(prep)
    impl_pct, impl_cur, impl_total, impl_status = calc_stats(impl)
    
    blocked = impl_status == "blocked"
    
    return {
        "stats": {
            "prep_pct": prep_pct,
            "prep_phase_info": f"{prep_cur}/{prep_total}",
            "prep_status": prep_status,
            "impl_pct": impl_pct,
            "impl_phase_info": f"{impl_cur}/{impl_total}",
            "impl_status": impl_status,
            "blocked": blocked,
            "block_reason": impl.get("block_reason", ""),
            "impl_current_phase": impl_cur
        }
    }

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

def get_client_projects(cid):
    c_folder = get_client_runtime_folder(cid)
    res = set()
    folder = c_folder / "projects"
    if folder.exists():
        try:
            for d in folder.iterdir():
                if d.is_dir() and not d.name.startswith("."):
                    res.add(d.name)
        except Exception:
            pass
            
    c_folder_orig = get_client_folder(cid)
    if c_folder_orig:
        folder_orig = c_folder_orig / "projects"
        if folder_orig.exists():
            try:
                for d in folder_orig.iterdir():
                    if d.is_dir() and not d.name.startswith("."):
                        res.add(d.name)
            except Exception:
                pass
                
    return sorted(list(res))

_BRIEF_PROMPTS = [
    "Какую цель нужно достичь? (например: увеличить базу клиентов на 20%)",
    "Какой продукт или услугу нужно продвигать?",
    "Какой рекламный бюджет планируется?",
    "На какой период рассчитан план? (например: Июнь 2026)",
    "Какой целевой KPI? (например: 300 лидов)",
    "Какая география охвата (GEO)?",
    "Кто основные 3 конкурента?",
    "Какой целевой CPL (стоимость лида)?",
    "Какие основные боли закрывает продукт?",
]

def get_chat_history(client_id):
    c_folder = get_client_runtime_folder(client_id)
    chat_file = c_folder / "chat.json"
    if chat_file.exists():
        try: return json.loads(chat_file.read_text(encoding="utf-8"))
        except: pass
    c_folder_orig = get_client_folder(client_id)
    if c_folder_orig:
        chat_file_orig = c_folder_orig / "chat.json"
        if chat_file_orig.exists():
            try: return json.loads(chat_file_orig.read_text(encoding="utf-8"))
            except: pass
    return []

def save_chat(client_id, history):
    folder = get_client_runtime_folder(client_id)
    if not folder:
        return False
    try:
        (folder / "chat.json").write_text(
            json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return True
    except Exception:
        return False

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



def get_default_web_data(cid):
    domain = f"{cid.lower()}.ru"
    if cid == "pluslogo":
        domain = "pluslogo.ru"
    elif cid == "ontime-ai":
        domain = "ontime.ai"
    elif cid == "andrey-brand":
        domain = "samsonov.ru"
    
    return {
        "library": [
            {"id": "lib_1", "name": "Логотип основной горизонтальный", "category": "logos", "size": "450 KB", "status": "active", "url": "/assets/logo_horiz.png", "notes": "Для светлого фона", "updated_at": "2026-05-28 14:20:00 UTC"},
            {"id": "lib_2", "name": "Баннер главной страницы весенний", "category": "banners", "size": "2.4 MB", "status": "active", "url": "/assets/hero_banner.jpg", "notes": "Кампания весна 2026", "updated_at": "2026-05-25 10:15:00 UTC"},
            {"id": "lib_3", "name": "Фото фасада офиса", "category": "photo", "size": "4.1 MB", "status": "active", "url": "/assets/office_front.jpg", "notes": "Съемка с квадрокоптера", "updated_at": "2026-05-10 16:30:00 UTC"},
            {"id": "lib_4", "name": "Текст о компании (официальный)", "category": "texts", "size": "15 KB", "status": "active", "url": "", "notes": "Согласовано с генеральным", "updated_at": "2026-05-29 11:00:00 UTC"},
            {"id": "lib_5", "name": "Отзыв клиента ООО Вектор", "category": "reviews", "size": "1.2 MB", "status": "active", "url": "/assets/review_vector.pdf", "notes": "С печатью", "updated_at": "2026-05-22 09:45:00 UTC"},
        ],
        "keywords": [
            {"id": "kw_1", "keyword": "разработка сайтов под ключ", "priority": "high", "volume": "4200", "status": "active"},
            {"id": "kw_2", "keyword": "заказать лендинг недорого", "priority": "medium", "volume": "1800", "status": "active"},
            {"id": "kw_3", "keyword": "оптимизация конверсии лендинга", "priority": "high", "volume": "850", "status": "active"},
        ],
        "competitors": [
            {"id": "comp_1", "domain": "competitor1.ru", "threat": "high", "strength": "Сильный SEO трафик", "details": "Активно ведут блог и закупают ссылки"},
            {"id": "comp_2", "domain": "competitor2.com", "threat": "medium", "strength": "Низкие цены и квизы", "details": "Используют агрессивную рекламу"},
        ],
        "brand_rules": {
            "id": "brand_rules",
            "voice": "Профессиональный, экспертный, уверенный, но дружелюбный. Без сложного сленга.",
            "tone": "Серьезный, деловой, технологичный",
            "colors": "#6366f1, #3b82f6, #0e0e12, #f1f5f9",
            "guidelines": "Всегда указывать гарантии и social proof в первом экране. CTA кнопки должны быть контрастными."
        },
        "default_owners": {
            "id": "default_owners",
            "content_owner": "OTDEL_CREATIVE",
            "tech_owner": "OTDEL_TECH",
            "design_owner": "OTDEL_CREATIVE",
            "qa_owner": "OTDEL_ZAVOD"
        },
        "sites": [
            {
                "id": "site_1",
                "domain": domain,
                "status": "active",
                "site_type": "Corporate",
                "pages": [
                    {"id": "p_1", "title": "Главная страница", "path": "/", "page_type": "Main", "status": "active", "last_check": "2026-05-29 09:30", "errors": 0},
                    {"id": "p_2", "title": "Каталог услуг", "path": "/services", "page_type": "Catalog", "status": "active", "last_check": "2026-05-29 09:32", "errors": 1},
                    {"id": "p_3", "title": "Контакты", "path": "/contacts", "page_type": "Info", "status": "active", "last_check": "2026-05-29 09:33", "errors": 0},
                ],
                "forms": [
                    {"id": "f_1", "name": "Заявка на консультацию", "form_id": "consult_main", "type": "Form", "conversions": 12, "status": "active"},
                    {"id": "f_2", "name": "Квиз подбора стоимости", "form_id": "quiz_price", "type": "Quiz", "conversions": 34, "status": "active"},
                ],
                "goals": [
                    {"id": "g_1", "name": "Клик по кнопке WhatsApp", "goal_id": "click_wa", "type": "Click", "conversions": 45, "status": "active"},
                    {"id": "g_2", "name": "Отправка формы лида", "goal_id": "lead_submit", "type": "Submit", "conversions": 46, "status": "active"},
                ],
                "errors": [
                    {"id": "err_1", "page": "/services", "type": "SEO", "message": "Отсутствует тег H1", "risk": "medium", "detected_at": "2026-05-29 09:32"},
                ],
                "checks": [
                    {"timestamp": "2026-05-29 09:33", "score": 92, "speed": 88, "seo": 85, "accessibility": 95, "checks_count": 14},
                    {"timestamp": "2026-05-15 11:20", "score": 89, "speed": 82, "seo": 85, "accessibility": 90, "checks_count": 14},
                ],
                "metrics": {
                    "visits": 1500,
                    "leads": 46,
                    "calls": 12,
                    "clicks": 320,
                    "conversion": 3.07
                },
                "last_check": "2026-05-29 09:33",
                "accesses": {
                    "ftp_host": "ftp.hostname.com",
                    "ftp_user": "ftpuser",
                    "ftp_pass": "ftppassword_masked_via_system",
                    "cms_admin": f"https://{domain}/admin",
                    "cms_user": "admin",
                    "cms_pass": "admin_password_masked_via_system"
                },
                "counters": {
                    "yandex_metrika_id": "89347123",
                    "yandex_metrika_token": "ym_token_masked_via_system",
                    "google_analytics_id": "UA-182374-1"
                },
                "integrations": {
                    "crm_type": "amocrm",
                    "crm_token": "amo_token_masked_via_system",
                    "webhook_url": "https://webhook.site/amo-integration"
                }
            }
        ],
        "production": [
            {"id": "prod_1", "site": domain, "title": "Лендинг спецпредложения", "page_type": "Landing", "status": "Дизайн", "owner": "OTDEL_CREATIVE", "priority": "high", "stage": "Дизайн", "created_at": "2026-05-28 10:00:00 UTC"},
            {"id": "prod_2", "site": domain, "title": "Страница отзывов", "page_type": "Info", "status": "Сборка", "owner": "OTDEL_TECH", "priority": "medium", "stage": "Сборка", "created_at": "2026-05-26 12:15:00 UTC"},
            {"id": "prod_3", "site": domain, "title": "Статья про SEO", "page_type": "Article", "status": "Контент", "owner": "OTDEL_CREATIVE", "priority": "low", "stage": "Контент", "created_at": "2026-05-29 15:30:00 UTC"},
        ]
    }

def mask_secret(val):
    if not val: return ""
    val_str = str(val)
    if "masked" in val_str or "***" in val_str:
        return val_str
    if len(val_str) <= 6:
        return "***"
    return val_str[:3] + "***" + val_str[-3:]

def get_web_analytics(cid, site_filter="all"):
    data = get_module_data(cid, "web")
    sites = data.get("sites", [])
    if not sites:
        return {
            "metrics": {"visits": 0, "leads": 0, "calls": 0, "clicks": 0, "conversion": 0},
            "best_pages": [],
            "poor_pages": [],
            "traffic_sources": [],
            "trend": []
        }
    if site_filter != "all":
        target_site = next((s for s in sites if s.get("domain") == site_filter or s.get("id") == site_filter), None)
        if target_site:
            sites = [target_site]
            
    visits = sum(s.get("metrics", {}).get("visits", 0) for s in sites)
    leads = sum(s.get("metrics", {}).get("leads", 0) for s in sites)
    calls = sum(s.get("metrics", {}).get("calls", 0) for s in sites)
    clicks = sum(s.get("metrics", {}).get("clicks", 0) for s in sites)
    conversion = round((leads / visits) * 100, 2) if visits > 0 else 0.0
    
    best_pages = []
    poor_pages = []
    
    for s in sites:
        domain = s.get("domain", "")
        for p in s.get("pages", []):
            best_pages.append({
                "page": f"{domain}{p.get('path', '/')}",
                "visits": 500,
                "leads": 20,
                "cr": 4.0
            })
        for err in s.get("errors", []):
            poor_pages.append({
                "page": f"{domain}{err.get('page', '/')}",
                "risk": err.get("risk", "medium").upper(),
                "error": err.get("message", "SEO issue"),
                "solution": "Добавить теги и заголовки"
            })
            
    if not best_pages:
        best_pages = [
            {"page": "mysite.ru/", "visits": 1200, "leads": 42, "cr": 3.5},
            {"page": "mysite.ru/services", "visits": 850, "leads": 31, "cr": 3.65},
            {"page": "mysite.ru/promo", "visits": 500, "leads": 24, "cr": 4.8}
        ]
    if not poor_pages:
        poor_pages = [
            {"page": "mysite.ru/contacts", "risk": "MEDIUM", "error": "Долгая загрузка (>3 сек)", "solution": "Оптимизировать изображения"},
            {"page": "mysite.ru/catalog", "risk": "HIGH", "error": "404 ошибка на 2 ссылки", "solution": "Настроить 301 редиректы"}
        ]
        
    traffic_sources = [
        {"source": "Яндекс.Директ", "visits": int(visits * 0.45), "leads": int(leads * 0.5), "percent": 45},
        {"source": "SEO (Яндекс/Google)", "visits": int(visits * 0.35), "leads": int(leads * 0.3), "percent": 35},
        {"source": "Прямые переходы", "visits": int(visits * 0.15), "leads": int(leads * 0.15), "percent": 15},
        {"source": "Социальные сети", "visits": int(visits * 0.05), "leads": int(leads * 0.05), "percent": 5}
    ]
    
    trend = [
        {"date": "25.05", "visits": int(visits * 0.12), "leads": int(leads * 0.12)},
        {"date": "26.05", "visits": int(visits * 0.15), "leads": int(leads * 0.14)},
        {"date": "27.05", "visits": int(visits * 0.14), "leads": int(leads * 0.16)},
        {"date": "28.05", "visits": int(visits * 0.18), "leads": int(leads * 0.19)},
        {"date": "29.05", "visits": int(visits * 0.22), "leads": int(leads * 0.23)}
    ]
    
    return {
        "metrics": {
            "visits": visits,
            "leads": leads,
            "calls": calls,
            "clicks": clicks,
            "conversion": conversion
        },
        "best_pages": best_pages[:5],
        "poor_pages": poor_pages[:5],
        "traffic_sources": traffic_sources,
        "trend": trend
    }

def get_web_settings(cid):
    data = get_module_data(cid, "web")
    import json
    settings = json.loads(json.dumps(data, ensure_ascii=False))
    for s in settings.get("sites", []):
        if "accesses" in s:
            for k in ["ftp_pass", "cms_pass"]:
                if k in s["accesses"]:
                    s["accesses"][k] = mask_secret(s["accesses"][k])
        if "counters" in s:
            for k in ["yandex_metrika_token"]:
                if k in s["counters"]:
                    s["counters"][k] = mask_secret(s["counters"][k])
        if "integrations" in s:
            for k in ["crm_token"]:
                if k in s["integrations"]:
                    s["integrations"][k] = mask_secret(s["integrations"][k])
    return settings



def get_project_status(cid, project_id):
    if not project_id: return {}
    c_folder = get_client_runtime_folder(cid)
    p = c_folder / "projects" / project_id / "STATUS.md"
    if not p.exists():
        c_folder_orig = get_client_folder(cid)
        if c_folder_orig:
            p = c_folder_orig / "projects" / project_id / "STATUS.md"
            
    res = {}
    if p.exists():
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    res[k.strip()] = v.strip()
        except Exception:
            pass
    return res

def log_marketing_action(cid, project_id, run_id, action, entity):
    c_folder = get_client_runtime_folder(cid)
    export_dir = c_folder / "marketing" / "exports"
    try:
        export_dir.mkdir(parents=True, exist_ok=True)
        audit_file = export_dir / "audit_log.jsonl"
        if not run_id and project_id and project_id != "all":
            status_data = get_project_status(cid, project_id)
            run_id = status_data.get("run_id")
        if not run_id:
            run_id = "TESTRUN"
        entry = {
            "run_id": run_id,
            "actor": "dashboard",
            "timestamp": now_ts(),
            "action": action,
            "entity": entity,
            "client_id": cid,
            "project_id": project_id or "all"
        }
        with audit_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"Error logging marketing action: {e}")

def save_module_data(cid, module, data):
    folder = _client_folder(cid)
    if module == "marketing":
        funnels = data.get("funnels", [])
        if isinstance(funnels, list):
            by_funnel = {}
            for item in funnels:
                fid = item.get("funnel_id")
                if fid: by_funnel.setdefault(fid, []).append(item)
            for fid, stages in by_funnel.items():
                stages.sort(key=lambda x: int(str(x.get("Порядок этапа", 0)) or 0))
                if stages:
                    try:
                        first_in = float(str(stages[0].get("Вход", 0)).replace(" ", "").replace(",", "."))
                        last_out = float(str(stages[-1].get("Выход", 0)).replace(" ", "").replace(",", "."))
                        overall_cr = round((last_out / first_in) * 100, 2) if first_in > 0 else 0
                        for item in stages:
                            try:
                                vin = float(str(item.get("Вход", 0)).replace(" ", "").replace(",", "."))
                                vout = float(str(item.get("Выход", 0)).replace(" ", "").replace(",", "."))
                                item["CR%"] = round((vout / vin) * 100, 2) if vin > 0 else 0
                            except: pass
                            item["Итоговая конверсия"] = overall_cr
                    except: pass
    folder.mkdir(parents=True, exist_ok=True)
    try:
        (folder / f"{module}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        with open(folder / "log.md", "a", encoding="utf-8") as lf:
            lf.write(f"\n- {now_ts()} | {module} updated by dashboard")
    except OSError as e:
        if e.errno == 30: print(f"ERROR: RO FS for {cid}")
        else: raise

def _audit_log(cid, module, action, payload):
    folder = _client_folder(cid)
    if not folder: return
    log_dir = folder / f"{module}/exports"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "audit_log.jsonl"
        entry = {"ts": now_ts(), "client_id": cid, "module": module, "action": action, "payload": payload}
        with open(log_file, "a", encoding="utf-8") as f: f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError: pass

def _lineage_log(cid, module, section, item_id, sheet_url):
    folder = _client_folder(cid)
    if not folder: return
    log_dir = folder / f"{module}/exports"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "lineage.jsonl"
        entry = {"ts": now_ts(), "client_id": cid, "module": module, "section": section, "item_id": item_id, "sheet_url": sheet_url}
        with open(log_file, "a", encoding="utf-8") as f: f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError: pass

def _process_module_hooks(cid, module, section, item, data=None):
    if module == "marketing" and section == "traffic_sources" and isinstance(item, dict):
        # Normalize legacy fields to the standard source contract.
        name = str(item.get("Название") or item.get("Канал") or "").strip()
        url = str(item.get("Ссылка") or item.get("URL") or "").strip()
        channel_type = str(item.get("Тип") or item.get("channel_type") or "other").strip().lower()
        status = str(item.get("Статус") or item.get("status") or "draft").strip().lower()
        clients = item.get("Клиенты") or item.get("assigned_clients") or ""
        if isinstance(clients, list):
            clients = ",".join([str(x).strip() for x in clients if str(x).strip()])
        clients = str(clients).strip()

        item["Название"] = name
        item["Ссылка"] = url
        item["Тип"] = channel_type
        item["Статус"] = status
        item["Клиенты"] = clients
        desc = str(item.get("Описание канала") or "").strip()
        if not desc or not re.match(r"^https?://", desc, re.I):
            item["Описание канала"] = url

        # Hidden publish limits (stored runtime, not rendered in manager UI).
        item.setdefault("max_title_length", 90)
        item.setdefault("max_post_chars", 2200)
        item.setdefault("max_hashtags", 10)
    return item

def module_add_item(cid, module, section, item):
    data = get_module_data(cid, module)
    item["id"] = str(uuid.uuid4())[:8]; item["created_at"] = now_ts()
    item = _process_module_hooks(cid, module, section, item, data)
    if section == "brand": data["brand"] = item
    else: data.setdefault(section, []).append(item)
    save_module_data(cid, module, data)
    _audit_log(cid, module, "add", {"section": section, "id": item["id"]})
    return item

def module_update_item(cid, module, section, item_id, updates):
    data = get_module_data(cid, module)
    if section == "brand":
        data["brand"] = dict(data.get("brand") or {}); data["brand"].update(updates); data["brand"].setdefault("id", item_id or "brand")
        save_module_data(cid, module, data); _audit_log(cid, module, "update", {"section": section, "id": item_id}); return True
    for item in data.get(section, []):
        if item.get("id") == item_id:
            old_status = item.get("status")
            new_status = updates.get("status")
            item.update(updates)
            
            if module == "content" and section == "items":
                if new_status == "Опубликовано" and old_status != "Опубликовано":
                    from datetime import datetime
                    kb = data.setdefault("strategy", {}).setdefault("kb_entries", [])
                    kb.append({
                        "source": "own_post",
                        "title": item.get("Текст", "")[:100] or item.get("title", "")[:100],
                        "date": datetime.now().strftime("%Y-%m-%d"),
                        "post_id": item.get("id", ""),
                    })
                    data["strategy"]["kb_entries"] = kb[-500:]
            
            save_module_data(cid, module, data)
            _audit_log(cid, module, "update", {"section": section, "id": item_id}); return True
    return False

def module_delete_item(cid, module, section, item_id):
    data = get_module_data(cid, module)
    if section == "brand":
        data["brand"] = {}
        save_module_data(cid, module, data)
        return True
    
    before = len(data.get(section, []))
    item_to_delete = None
    for item in data.get(section, []):
        if item.get("id") == item_id:
            item_to_delete = item
            break
            
    if item_to_delete:
        if module == "marketing":
            log_marketing_action(cid, item_to_delete.get("project_id"), item_to_delete.get("run_id"), "delete", f"Deleted item in {section}")
        data[section] = [i for i in data.get(section, []) if i.get("id") != item_id]
        save_module_data(cid, module, data)
        return True
    return False

def module_export_csv(cid, module, section):
    import csv, io
    items = get_module_data(cid, module).get(section, [])
    if not items: return ""
    buf = io.StringIO(); writer = csv.DictWriter(buf, fieldnames=list(items[0].keys()))
    writer.writeheader(); writer.writerows(items); return buf.getvalue()


def _find_item(items, item_id):
    for item in items:
        if item.get("id") == item_id:
            return item
    return None


def _append_gate_log(item, stage, verdict):
    logs = item.get("_gate_log")
    if not isinstance(logs, list):
        logs = []
    logs.append({"ts": now_ts(), "stage": stage, "verdict": verdict})
    item["_gate_log"] = logs


def _ensure_creative_for_post(content_data, marketing_data, post):
    creatives = marketing_data.setdefault("creatives", [])
    existing = next((c for c in creatives if c.get("Post ID") == post.get("id")), None)
    if existing:
        return existing
    creative = {
        "id": str(uuid.uuid4())[:8],
        "Название": (post.get("Текст") or "Креатив").strip()[:64],
        "Тип": post.get("Формат") or "Карусель",
        "Канал": post.get("Сеть") or "TG",
        "Оффер": "",
        "Статус": "in_design",
        "Конверсия": "",
        "Post ID": post.get("id"),
        "Ссылка": "",
        "created_at": now_ts(),
    }
    creatives.append(creative)
    return creative


def _ensure_publication_for_post(content_data, post):
    publications = content_data.setdefault("publications", [])
    existing = next((p for p in publications if p.get("Post ID") == post.get("id")), None)
    if existing:
        existing["Статус"] = "Опубликовано"
        existing["Дата"] = post.get("Дата") or now_ts().split(" ")[0]
        existing["Ссылка"] = post.get("Креатив URL") or existing.get("Ссылка", "")
        return existing
    publication = {
        "id": str(uuid.uuid4())[:8],
        "Ресурс": post.get("Сеть") or "—",
        "Ссылка": post.get("Креатив URL") or "",
        "Статус": "Опубликовано",
        "Дата": post.get("Дата") or now_ts().split(" ")[0],
        "Post ID": post.get("id"),
        "created_at": now_ts(),
    }
    publications.append(publication)
    return publication


def workflow_action(cid, tab_id, item_id, action):
    content_data = get_module_data(cid, "content")
    marketing_data = get_module_data(cid, "marketing")

    if tab_id == "content_посты":
        posts = content_data.setdefault("posts", [])
        post = _find_item(posts, item_id)
        if not post:
            return False, "post not found"
        post.setdefault("Формат", "Карусель")

        if action == "submit_review":
            post["Статус"] = "review"
            _append_gate_log(post, "Сценарий", "submitted")
        elif action == "approve":
            post["Статус"] = "approved"
            _append_gate_log(post, "Сценарий", "approved")
        elif action == "rework":
            post["Статус"] = "rework"
            _append_gate_log(post, "Сценарий", "rework")
        elif action == "send_to_design":
            post["Статус"] = "in_design"
            _append_gate_log(post, "Передача в дизайн", "approved")
            _ensure_creative_for_post(content_data, marketing_data, post)
        elif action == "ready_to_publish":
            post["Статус"] = "ready_to_publish"
            _append_gate_log(post, "Креатив", "approved")
        elif action == "publish":
            post["Статус"] = "published"
            _append_gate_log(post, "Публикация", "approved")
            _ensure_publication_for_post(content_data, post)
        else:
            return False, "unknown action"

        save_module_data(cid, "content", content_data)
        save_module_data(cid, "marketing", marketing_data)
        return True, "ok"

    if tab_id == "marketing_креатив":
        creatives = marketing_data.setdefault("creatives", [])
        creative = _find_item(creatives, item_id)
        if not creative:
            return False, "creative not found"

        post_id = creative.get("Post ID")
        post = _find_item(content_data.setdefault("posts", []), post_id) if post_id else None

        if action == "submit_review":
            creative["Статус"] = "review"
            _append_gate_log(creative, "Дизайн", "submitted")
        elif action == "approve":
            creative["Статус"] = "approved"
            _append_gate_log(creative, "Дизайн", "approved")
            if post:
                post["Статус"] = "ready_to_publish"
                post["Креатив URL"] = creative.get("Ссылка") or post.get("Креатив URL", "")
                _append_gate_log(post, "Креатив", "approved")
        elif action == "rework":
            creative["Статус"] = "rework"
            _append_gate_log(creative, "Дизайн", "rework")
            if post:
                post["Статус"] = "rework"
                _append_gate_log(post, "Креатив", "rework")
        else:
            return False, "unknown action"

        save_module_data(cid, "marketing", marketing_data)
        save_module_data(cid, "content", content_data)
        return True, "ok"

    return False, "tab is not workflow-enabled"

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

def save_integrations_registry(data):
    data["updated_at"] = now_ts()
    _save_json(INTEGRATIONS_REGISTRY, data)

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

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args): pass
    def _json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0"); self.send_header("Pragma", "no-cache"); self.send_header("Expires", "0"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def _html(self, html, code=200):
        body = html.encode(); self.send_response(code); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0"); self.send_header("Pragma", "no-cache"); self.send_header("Expires", "0"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        url = urlparse(self.path); path = unquote(url.path).strip("/")
        params = parse_qs(url.query)
        aliases = {
            "agents/sales/leads": "sales_лиды",
            "agents/sales/clients": "sales_клиенты",
            "agents/sales/dialogs": "sales_диалоги",
            "agents/sales/deals": "sales_сделки",
            "agents/sales/proposals": "sales_кп",
            "agents/marketing/ca": "marketing_ца",
            "agents/marketing/pains": "marketing_боли",
            "agents/marketing/usp": "marketing_утп",
            "agents/marketing/offers": "marketing_офферы",
            "agents/marketing/funnels": "marketing_воронки",
            "agents/marketing/traffic": "marketing_источники_трафика",
            "agents/marketing/ads": "marketing_реклама",
            "agents/marketing/creative": "marketing_креатив",
            "agents/marketing/packaging": "marketing_упаковка_продукта",
            "agents/content/content_factory": "content_контент-завод",
            "agents/content/web": "content_web-завод",
            "agents/content/sheets": "content_google-таблицы",
            "agents/content/media": "content_медиафайлы",
            "agents/smm/web": "smm_web-завод",
            "agents/smm/web-factory": "smm_web-завод",
            "agents/tech/bots": "tech_боты",
            "agents/tech/integrations": "tech_интеграции",
        }
        ministers = get_ministers_from_registry()
        valid = ["", "home", "agents", "login", "kb_docs", "kb_sop", "kb_clients", "kb_vector", "wf_templates", "wf_runs", "wf_queues", "wf_logs", "sys_users", "sys_roles", "sys_integrations", "sys_admin"]
        valid += [f"{s}_{i.replace(' ', '_').lower()}" for s,d in {"sales":["Лиды","Клиенты","Диалоги","Сделки","Заказы","КП","CJM","Этапы SLA","Эскалации","Повторные продажи","Рекомендатели","Sales Core"],"content":["Контент-завод","WEB-завод","Темы","Статьи","Посты","Публикации","Комментарии","Google-таблицы","Медиафайлы"],"marketing":["ЦА","Боли","УТП","Офферы","Воронки","Источники трафика","Реклама","Креатив","Упаковка продукта"],"analytics":["Обзор","Продажи","Контент","Маркетинг","Дашборды","Метрики","План-факт","Отчёты","Ошибки данных","Выводы и рекомендации"],"production":["Заказы","План","Смены","Операции","Материалы","Остатки","Брак","Загрузка"],"tech":["Сервер","Скрипты","Боты","API","Интеграции","Воркфлоу","Очереди","Логи","Ошибки"],"mgmt":["Финансы","Юрконтур","Консалтинг","PR","Задачи собственника","Стратегия","Документы"],"smm":["SMM","WEB-завод"]}.items() for i in d]
        if path == "favicon.ico":
            self.send_response(204); self.send_header("Content-Length", "0"); self.end_headers()
        elif path in aliases:
            self._html(get_html(ministers, aliases[path]))
        elif path == "" or path in valid:
            init_tab = "home" if path in ("", "agents", "home") else path
            self._html(get_html(ministers, init_tab))
        elif path == "api/status": self._json({"runtime": "ok", "generated_at": now_ts()})
        elif path == "api/tasks": self._json(get_queue_counts())
        elif path == "api/workflow/templates": self._json(get_workflow_templates())
        elif path == "api/workflow/runs": self._json(get_workflow_runs())
        elif path == "api/workflow/logs": self._json(get_workflow_logs())
        elif path == "api/home": self._json({"status": "ok", "data": get_home_snapshot()})
        elif path == "api/health": self._json(get_dr_health_snapshot())
        elif path == "api/clients": self._json({"status": "ok", "data": get_clients_detailed()})
        elif path == "api/clients/summary": self._json({"status": "ok", "data": get_client_summary()})
        elif path == "api/clients/list": self._json({"status": "ok", "data": get_clients_detailed()})
        elif path == "api/system/bots": self._json(get_bots_inventory())
        elif path == "api/integrations": self._json(get_integrations_payload())
        elif path == "api/tech/server": self._json({"status": "ok", "data": {"metrics": get_server_status()}})
        elif path == "api/tech/queues": self._json({"status": "ok", "data": get_tech_queue_stats()})
        elif path == "api/tech/errors": self._json({"status": "ok", "data": get_tech_errors()})
        elif path == "api/tech/workflow": self._json({"status": "ok", "data": get_workflow_status()})
        elif path == "api/tech/api-registry": self._json({"status": "ok", "data": get_api_registry()})
        elif path == "api/tech/scripts": self._json({"status": "ok", "data": get_tech_scripts()})
        elif path == "api/tech/bots": self._json({"status": "ok", "data": get_bots_list()})
        elif path == "api/tech/log-list":
            log_dir = Path("/root/agents/v3/logs")
            logs = sorted([f.stem for f in log_dir.glob("*.log")]) if log_dir.exists() else []
            self._json({"status": "ok", "data": logs})
        elif path.startswith("api/analytics/"):
            parts = path.split("/")
            # GET /api/analytics/{endpoint}?client_id=...
            # Compatibility: GET /api/analytics/{cid}/{endpoint}
            cid = params.get("client_id", [None])[0]
            etype = parts[2] if len(parts) >= 3 else None
            
            if len(parts) == 4: # Old cid-based: api/analytics/{cid}/{atype}
                cid, etype = parts[2], parts[3]
            
            if etype in ("dashboards", "overview"):
                self._json({"status": "ok", "data": get_analytics_dashboards_api(cid)})
            elif etype == "metrics":
                self._json({"status": "ok", "data": get_analytics_metrics_api(cid)})
            elif etype == "planfact":
                self._json({"status": "ok", "data": get_analytics_planfact_api(cid)})
            elif etype == "reports":
                self._json({"status": "ok", "data": get_analytics_reports_api()})
            elif etype == "data-errors":
                self._json({"status": "ok", "data": get_analytics_data_errors_api(cid)})
            elif etype == "insights":
                self._json({"status": "ok", "data": get_analytics_insights_api(cid)})
            elif etype == "sales":
                self._json({"status": "ok", "data": get_analytics_sales(cid)})
            elif etype == "content":
                self._json({"status": "ok", "data": get_analytics_content(cid)})
            elif etype == "marketing":
                self._json({"status": "ok", "data": get_analytics_marketing(cid)})
            else:
                self._json({"error": "unknown analytics type", "type": etype}, 404)
        elif path.startswith("api/content/factory/"):
            parts = path.split("/")  # api/content/factory/{cid}
            if len(parts) == 4:
                cid = parts[3]
                self._json({"status": "ok", "data": get_content_factory_data(cid)})
            else:
                self._json({"error": "bad path"}, 400)
        elif path.startswith("api/smm/"):
            parts = path.split("/")  # api/smm/{cid}/{type}
            if len(parts) == 4:
                cid, stype = parts[2], parts[3]
                if stype == "overview":
                    reg = get_sales_registry()
                    content_data = get_module_data(cid, "content")
                    posts = content_data.get("posts", [])
                    published = [p for p in posts if p.get("\u0421\u0442\u0430\u0442\u0443\u0441") == "\u041e\u043f\u0443\u0431\u043b\u0438\u043a\u043e\u0432\u0430\u043d\u043e"]
                    brand_data = get_module_data(cid, "marketing")
                    brand_sections = sum(1 for k in ["target_audience", "pain_points", "usp"] if brand_data.get(k))
                    self._json({"status": "ok", "data": {
                        "brand_filled":    f"{brand_sections}/3 \u0440\u0430\u0437\u0434\u0435\u043b\u043e\u0432",
                        "brand_sub":       "\u0417\u0430\u043f\u043e\u043b\u043d\u0435\u043d\u043e",
                        "autoposting":     "\u2014", "autoposting_sub": "\u041d\u0435 \u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0435\u043d\u043e",
                        "mediaplan":       str(len(content_data.get("topics", []))), "mediaplan_sub": "\u0442\u0435\u043c \u0432 \u043f\u043b\u0430\u043d\u0435",
                        "analytics":       str(len(reg.get("leads", []))), "analytics_sub": "\u043b\u0438\u0434\u043e\u0432",
                        "photos":          "\u2014", "photos_sub": "\u041d\u0435 \u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0435\u043d\u043e",
                        "history":         str(len(published)), "history_sub": "\u043e\u043f\u0443\u0431\u043b\u0438\u043a\u043e\u0432\u0430\u043d\u043e",
                    }})
                elif stype == "publications":
                    smm_data = get_module_data(cid, "smm")
                    pubs = smm_data.get("autoposting", {})
                    self._json({"status": "ok", "data": {
                        "enabled":    pubs.get("enabled", False),
                        "next_pub":   pubs.get("next_pub", "\u2014"),
                        "platforms":  pubs.get("platforms", []),
                        "slots":      pubs.get("slots", []),
                        "errors":     pubs.get("errors", []),
                    }})
                elif stype == "settings":
                    reg = _load_json(CLIENT_REGISTRY, {})
                    info = reg.get("clients", {}).get(cid, {})
                    smm_data = get_module_data(cid, "smm")
                    mkt_data = get_module_data(cid, "marketing")
                    bv = mkt_data.get("brand_voice", [])
                    brand_voice_text = bv[0].get("\u041e\u043f\u0438\u0441\u0430\u043d\u0438\u0435", "") if bv else ""
                    socials_masked = [dict(s, token=("\u2022\u2022\u2022\u2022" + s.get("token","")[-4:]) if len(s.get("token","")) > 4 else "\u2022\u2022\u2022\u2022") for s in smm_data.get("socials", [])]
                    traffic_sources = mkt_data.get("traffic_sources", []) if isinstance(mkt_data.get("traffic_sources", []), list) else []
                    publication_sources = []
                    for src in traffic_sources:
                        name = str(src.get("Название") or src.get("Канал") or "").strip()
                        url = str(src.get("Ссылка") or src.get("URL") or "").strip()
                        if not name and not url:
                            continue
                        publication_sources.append({
                            "name": name,
                            "url": url,
                            "type": str(src.get("Тип") or src.get("channel_type") or "other"),
                            "status": str(src.get("Статус") or src.get("status") or "draft")
                        })
                    self._json({"status": "ok", "data": {
                        "profile":         {"name": info.get("name", cid), "brand_name": smm_data.get("brand_name", info.get("name", cid)), "description": smm_data.get("description", "")},
                        "socials":         socials_masked,
                        "publication_sources": publication_sources,
                        "contacts":        smm_data.get("contacts", {"phone": "", "email": "", "telegram": ""}),
                        "schedule":        smm_data.get("schedule", {"frequency": "", "days": []}),
                        "approval_rules":  smm_data.get("approval_rules", {"require_approval": False, "approvers": []}),
                        "knowledge_bases": smm_data.get("knowledge_bases", []),
                        "brand_voice":     brand_voice_text,
                        "notifications":   smm_data.get("notifications", {"email": False, "telegram": False, "slack": False}),
                        "access_rights":   smm_data.get("access_rights", []),
                        "integrations":    smm_data.get("integrations", []),
                    }})
                else:
                    self._json({"error": "unknown smm type"}, 404)
            else:
                self._json({"error": "invalid path"}, 400)
        elif path == "api/agents/list":
            mins = get_ministers_from_registry() or []
            out = []
            for m in mins:
                if isinstance(m, dict):
                    aid = str(m.get("id") or m.get("key") or m.get("agent_id") or m.get("name") or "")
                    name = str(m.get("name") or m.get("title") or aid)
                else:
                    aid = str(m)
                    name = aid
                if aid:
                    out.append({"id": aid, "name": name})
            self._json({"status": "ok", "data": out})
        elif path.startswith("api/sales/"):
            parts = path.split("/")
            if len(parts) < 3: self._json({"error": "invalid path"}, 400); return
            section = parts[2]
            if section == "leads":
                leads = get_sales_registry().get("leads", [])
                for x in leads:
                    x["Этап воронки"] = _normalize_funnel_stage(x.get("Этап воронки"))
                self._json({"status": "ok", "data": leads})
            elif section == "clients": self._json({"status": "ok", "data": get_sales_clients()})
            elif section == "dialogs":
                if len(parts) == 4: # api/sales/dialogs/{cid}
                    self._json({"status": "ok", "data": {"messages": get_chat_history(parts[3])}})
                else:
                    self._json({"status": "ok", "data": get_sales_dialogs()})
            elif section == "deals": self._json({"status": "ok", "data": get_sales_registry().get("deals", [])})
            elif section == "proposals": self._json({"status": "ok", "data": get_sales_registry().get("proposals", [])})
            elif section == "sales_core": self._json({"status": "ok", "data": get_sales_core_rows()})
            elif section == "cjm": self._json({"status": "ok", "data": get_sales_cjm_rows()})
            elif section == "этапы_sla":
                reg = get_sales_registry()
                rows = get_sales_sla_rows()
                _auto_create_sla_escalations(reg, rows)
                save_sales_registry(reg)
                self._json({"status": "ok", "data": rows})
            elif section == "escalations":
                reg = get_sales_registry()
                esc = reg.get("_escalations", [])
                if not isinstance(esc, list):
                    esc = []
                self._json({"status": "ok", "data": esc})
            elif section == "export.csv":
                self._json({"status": "error", "message": "Not implemented"}, 501)
            else: self._json({"error": "not found"}, 404)
        elif path.startswith("api/module/"):
            parts = path.split("/")  # api/module/{cid}/{module}/{section}
            if len(parts) == 5:
                cid, module, section = parts[2], parts[3], parts[4]
                if module not in MODULE_EMPTY:
                    self._json({"status": "error", "message": "unknown module"}, 404)
                    return
                if module == "content" and section == "strategy":
                    strat = get_module_data(cid, "content").get("strategy", {})
                    self._json({"status": "ok", "data": strat})
                    return
                if cid == "all":
                    registry = _load_json(CLIENT_REGISTRY, {}).get("clients", {})
                    aggregated = []
                    for c_id in registry.keys():
                        if c_id == "_INTERNAL": continue
                        had_items = False
                        # traffic_sources: read from channels_index.json (STD_29A §29A.27)
                        if module == "marketing" and section == "traffic_sources":
                            c_folder = get_client_folder(c_id)
                            if c_folder:
                                cidx_path = c_folder / "knowledge" / "channels_index.json"
                                if cidx_path.exists():
                                    cidx = _load_json(cidx_path, {})
                                    for ch in cidx.get("channels", []):
                                        if not isinstance(ch, dict):
                                            continue
                                        aggregated.append({
                                            "Клиент": c_id,
                                            "Название": ch.get("name", ""),
                                            "Тип": ch.get("channel_type", "other"),
                                            "Ссылка": ch.get("url", ""),
                                            "Статус": ch.get("status", "draft"),
                                            "Описание канала": ch.get("platform", ""),
                                            "Бюджет": "", "Лиды": "", "CPL": ""
                                        })
                                        had_items = True
                        else:
                            c_data = get_module_data(c_id, module)
                            c_items = c_data.get(section, c_data if section == "brand" else [])
                            if isinstance(c_items, list):
                                for item in c_items:
                                    item_copy = dict(item)
                                    item_copy["Клиент"] = c_id
                                    aggregated.append(item_copy)
                                    had_items = True
                        if module == "marketing" and section == "traffic_sources" and not had_items:
                            aggregated.append({"Клиент": c_id, "Название": "—", "Тип": "—",
                                               "Статус": "Нет каналов", "_placeholder": True})
                    self._json({"status": "ok", "data": aggregated})
                    return
                data = get_module_data(cid, module)
                result = data.get(section, data if section == "brand" else [])
                # channels_index.json is source of truth for traffic_sources (STD_29A §29A.27)
                if module == "marketing" and section == "traffic_sources":
                    c_folder = get_client_folder(cid)
                    if c_folder:
                        cidx_path = c_folder / "knowledge" / "channels_index.json"
                        if cidx_path.exists():
                            cidx = _load_json(cidx_path, {})
                            channels = [ch for ch in cidx.get("channels", []) if isinstance(ch, dict)]
                            if channels:
                                result = [
                                    {
                                        "Название": ch.get("name", ""),
                                        "Тип": ch.get("channel_type", "other"),
                                        "Ссылка": ch.get("url", ""),
                                        "Статус": ch.get("status", "draft"),
                                        "Описание канала": ch.get("platform", ""),
                                        "Бюджет": "", "Лиды": "", "CPL": ""
                                    }
                                    for ch in channels
                                ]
                self._json({"status": "ok", "data": result})
            elif len(parts) == 4:
                cid, module = parts[2], parts[3]
                if module not in MODULE_EMPTY:
                    self._json({"status": "error", "message": "unknown module"}, 404)
                    return
                self._json({"status": "ok", "data": get_module_data(cid, module)})
        elif path.startswith("api/module-export/"):
            parts = path.split("/")
            if len(parts) == 5:
                cid, module, section = parts[2], parts[3], parts[4]
                query = parse_qs(url.query)
                project_id = query.get("project_id", [None])[0]
                
                # Fetch all items
                all_items = get_module_data(cid, module).get(section, [])
                
                # Filter by project_id if provided
                if project_id and project_id != "all":
                    items = [item for item in all_items if item.get("project_id") == project_id]
                else:
                    items = all_items
                    
                # Build CSV content
                columns = MODULE_COLUMNS.get(f"{module}_{section}", [])
                if not columns and items:
                    columns = list(items[0].keys())
                    
                import csv, io
                buf = io.StringIO()
                writer = csv.DictWriter(buf, fieldnames=columns, extrasaction='ignore')
                writer.writeheader()
                for item in items:
                    row = {col: item.get(col, "") for col in columns}
                    writer.writerow(row)
                csv_data = buf.getvalue()
                
                # Write physical copy to exports folder if project_id is provided
                if project_id and project_id != "all":
                    status_data = get_project_status(cid, project_id)
                    run_id = status_data.get("run_id", "NO_RUN_ID")
                    c_folder = get_client_runtime_folder(cid)
                    if c_folder:
                        export_dir = c_folder / "marketing" / "exports"
                        try:
                            export_dir.mkdir(parents=True, exist_ok=True)
                            csv_file_path = export_dir / f"{run_id}_funnels.csv"
                            csv_file_path.write_text(csv_data, encoding="utf-8")
                        except Exception as e:
                            print(f"Error saving physical CSV: {e}")
                        
                        # Log audit event
                        log_marketing_action(cid, project_id, run_id, "export_csv", f"Exported {section} to CSV")
                
                body = csv_data.encode("utf-8-sig")
                self.send_response(200); self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", f"attachment; filename={section}.csv")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
        elif path == "api/web/clients":
            self._json({"status": "ok", "data": get_clients_detailed()})
        elif path.startswith("api/web/"):
            parts = path.split("/")
            if len(parts) >= 4:
                cid = parts[2]
                endpoint = parts[3]
                
                # Isolation check
                registry = _load_json(CLIENT_REGISTRY, {}).get("clients", {})
                if cid not in registry:
                    self._json({"status": "error", "message": "Client not found or access denied"}, 403)
                    return
                
                if endpoint == "data":
                    self._json({"status": "ok", "data": get_web_settings(cid)})
                elif endpoint == "analytics":
                    site = params.get("site", ["all"])[0]
                    self._json({"status": "ok", "data": get_web_analytics(cid, site)})
                elif endpoint == "settings":
                    self._json({"status": "ok", "data": get_web_settings(cid)})
                else:
                    self._json({"status": "error", "message": "unknown web endpoint"}, 404)
            else:
                self._json({"status": "error", "message": "invalid path"}, 400)
        elif path.startswith("api/clients/"):
            parts = path.split("/")
            # Security check for client existence in registry
            cid = parts[2]
            registry = _load_json(CLIENT_REGISTRY, {}).get("clients", {})
            if cid != "all" and cid not in registry:
                self._json({"error": "Forbidden - Client not in registry"}, 403)
                return

            if len(parts) == 5 and parts[3] == "media-plan" and parts[4] == "package":
                cid = parts[2]
                c_folder = get_client_folder(cid)
                if not c_folder:
                    self._json({"status": "ok", "data": None})
                    return
                mp_dir = c_folder / "media_plan"
                if not mp_dir.exists():
                    self._json({"status": "ok", "data": None})
                    return
                res_data = {}
                for fn in ["mediaplan.json", "analytics.json", "control.json", "qa_checks.json", "export_summary.json"]:
                    f_path = mp_dir / fn
                    key = fn.replace(".json", "")
                    if f_path.exists():
                        try:
                            res_data[key] = json.loads(f_path.read_text(encoding="utf-8"))
                        except:
                            res_data[key] = None
                    else:
                        res_data[key] = None
                if res_data.get("mediaplan") is None:
                    self._json({"status": "ok", "data": None})
                else:
                    self._json({"status": "ok", "data": res_data})
                return

            if len(parts) == 4 and parts[3] == "roadmap":
                stats = get_client_roadmap(parts[2])
                self._json({"status": "ok", "data": stats})
                return
            if len(parts) == 4 and parts[3] == "projects":
                projects = get_client_projects(parts[2])
                self._json({"status": "ok", "data": projects})
                return
            if len(parts) == 4 and parts[3] == "stage-chat":
                cid = parts[2]
                q = parse_qs(url.query)
                stage = (q.get("stage") or [""])[0]
                run_id = (q.get("run_id") or [""])[0]
                th = get_stage_chat(cid, stage, run_id)
                self._json({"status": "ok", "data": {"messages": th.get("messages", [])}})
                return
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
        if path.startswith("api/clients/") and path.endswith("/gate5/approve"):
            parts = path.split("/")
            cid = parts[2]
            c_folder = get_client_folder(cid)
            if c_folder:
                log_p = c_folder / "log.md"
                with open(log_p, "a", encoding="utf-8") as f:
                    f.write(f"\n[{_now_iso()}] GATE 5 APPROVED via Dashboard\n")
                self._json({"status": "ok"})
            else:
                self._json({"error": "client not found"}, 404)
            return


        if path.startswith("api/web/"):
            parts = path.split("/")
            # POST /api/web/{cid}/settings/save
            # POST /api/web/{cid}/{production|sites|library}/{add|update|delete}
            if len(parts) >= 5:
                cid = parts[2]
                section = parts[3]
                action = parts[4]
                
                # Isolation check
                registry = _load_json(CLIENT_REGISTRY, {}).get("clients", {})
                if cid not in registry:
                    self._json({"status": "error", "message": "Client not found or access denied"}, 403)
                    return
                
                # Load existing web data
                data = get_module_data(cid, "web")
                
                if section == "settings" and action == "save":
                    # Update brand rules
                    if "brand_rules" not in data:
                        data["brand_rules"] = {}
                    data["brand_rules"]["voice"] = body.get("brand_rules", {}).get("voice", "")
                    data["brand_rules"]["colors"] = body.get("brand_rules", {}).get("colors", "")
                    data["brand_rules"]["guidelines"] = body.get("brand_rules", {}).get("guidelines", "")
                    
                    # Update default owners
                    if "default_owners" not in data:
                        data["default_owners"] = {}
                    data["default_owners"]["content_owner"] = body.get("default_owners", {}).get("content_owner", "")
                    data["default_owners"]["design_owner"] = body.get("default_owners", {}).get("design_owner", "")
                    data["default_owners"]["tech_owner"] = body.get("default_owners", {}).get("tech_owner", "")
                    data["default_owners"]["qa_owner"] = body.get("default_owners", {}).get("qa_owner", "")
                    
                    # Update keywords and competitors
                    data["keywords"] = body.get("keywords", [])
                    data["competitors"] = body.get("competitors", [])
                    
                    # Preserving masked tokens/secrets for sites
                    new_sites = body.get("sites", [])
                    old_sites = data.get("sites", [])
                    for ns in new_sites:
                        domain = ns.get("domain")
                        osite = next((s for s in old_sites if s.get("domain") == domain), None)
                        if osite:
                            if "accesses" in ns and "accesses" in osite:
                                for k in ["ftp_pass", "cms_pass"]:
                                    if k in ns["accesses"] and ("masked" in str(ns["accesses"][k]) or "***" in str(ns["accesses"][k])):
                                        ns["accesses"][k] = osite["accesses"].get(k, "")
                            if "counters" in ns and "counters" in osite:
                                for k in ["yandex_metrika_token"]:
                                    if k in ns["counters"] and ("masked" in str(ns["counters"][k]) or "***" in str(ns["counters"][k])):
                                        ns["counters"][k] = osite["counters"].get(k, "")
                            if "integrations" in ns and "integrations" in osite:
                                for k in ["crm_token"]:
                                    if k in ns["integrations"] and ("masked" in str(ns["integrations"][k]) or "***" in str(ns["integrations"][k])):
                                        ns["integrations"][k] = osite["integrations"].get(k, "")
                    data["sites"] = new_sites
                    
                elif section in ("production", "sites", "library"):
                    items = data.get(section, [])
                    if action == "add":
                        new_item = body
                        if "id" not in new_item:
                            new_item["id"] = f"{section[:4]}_{int(time.time())}"
                        items.append(new_item)
                    elif action == "update":
                        item_id = body.get("id")
                        for i, item in enumerate(items):
                            if item.get("id") == item_id:
                                items[i] = body
                                break
                    elif action == "delete":
                        item_id = body.get("id")
                        items = [item for item in items if item.get("id") != item_id]
                    data[section] = items
                else:
                    self._json({"status": "error", "message": "unknown POST section"}, 404)
                    return
                
                # Save modified web module data
                rf = get_client_runtime_folder(cid) / "web.json"
                try:
                    rf.parent.mkdir(parents=True, exist_ok=True)
                    rf.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                    self._json({"status": "ok"})
                except Exception as e:
                    self._json({"status": "error", "message": f"failed to write web.json: {str(e)}"}, 500)
            else:
                self._json({"status": "error", "message": "invalid path"}, 400)
        elif path == "api/sales/escalations/decision":
            eid = str(body.get("id", "")).strip()
            decision = str(body.get("decision", "")).strip().lower()
            note = str(body.get("note", "")).strip()
            if not eid or decision not in ("approve", "rework", "close"):
                self._json({"status": "error", "message": "invalid payload"}, 400)
                return
            reg = get_sales_registry()
            esc = reg.get("_escalations", [])
            if not isinstance(esc, list):
                esc = []
            found = False
            for e in esc:
                if str(e.get("id", "")) == eid:
                    e["status"] = {"approve": "approved", "rework": "rework", "close": "closed"}[decision]
                    e["decided_at"] = now_ts()
                    e["decision_note"] = note
                    found = True
                    break
            if not found:
                self._json({"status": "error", "message": "not found"}, 404)
                return
            reg["_escalations"] = esc
            save_sales_registry(reg)
            self._json({"status": "ok"})
        elif path == "api/sales/stage/decision":
            cid = str(body.get("client_id", "")).strip()
            stage = str(body.get("stage", "")).strip()
            decision = str(body.get("decision", "")).strip().lower()
            note = str(body.get("note", "")).strip()
            run_id = str(body.get("run_id", "")).strip()
            if not cid or not stage or decision not in ("approve", "rework", "close"):
                self._json({"status": "error", "message": "invalid payload"}, 400)
                return
            reg = get_sales_registry()
            rt = reg.get("_stage_runtime", {})
            if not isinstance(rt, dict):
                rt = {}
            prev = rt.get(stage, {}) if isinstance(rt.get(stage, {}), dict) else {}
            prev["status"] = {"approve": "approved", "rework": "rework", "close": "closed"}[decision]
            prev["updated_at"] = now_ts()
            if run_id:
                prev["task_id"] = run_id
            rt[stage] = prev
            reg["_stage_runtime"] = rt
            save_sales_registry(reg)
            append_stage_chat_message(cid, stage, run_id, "system", f"[DECISION] {decision} {('| ' + note) if note else ''}")
            self._json({"status": "ok"})
        elif path == "api/sales/cjm/run":
            cid = str(body.get("client_id", "")).strip()
            stage = str(body.get("stage", "")).strip()
            agent = str(body.get("agent", "")).strip() or "min_sales"
            goal = str(body.get("goal", "")).strip()
            kpi = str(body.get("kpi", "")).strip()
            deadline = str(body.get("deadline", "")).strip()
            priority = str(body.get("priority", "high")).strip() or "high"
            if not cid or not stage:
                self._json({"status": "error", "message": "client_id and stage required"}, 400)
                return
            agent_map = {
                "Нет потребности / есть проблема": "min_marketing",
                "Есть потребность / нет решения": "min_sales",
                "Есть решение / нет покупки": "min_sales",
                "Есть покупка / нет лояльности": "min_support",
                "Лояльный клиент": "min_account",
                "Рекомендатель": "min_marketing",
                "Повторная сделка": "min_sales",
            }
            task_id = f"TASK-CJM-{uuid.uuid4().hex[:8].upper()}"
            payload = {
                "task_id": task_id,
                "client_id": cid,
                "task_type": "cjm_stage_action",
                "stage": stage,
                "assigned_agent": agent or agent_map.get(stage, "min_sales"),
                "goal": goal,
                "kpi": kpi,
                "deadline": deadline,
                "priority": priority,
                "created_at": now_ts(),
            }
            p_dir = QUEUE_ROOT / "pending"
            p_dir.mkdir(parents=True, exist_ok=True)
            (p_dir / f"{task_id}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            reg = get_sales_registry()
            rt = reg.get("_stage_runtime", {})
            if not isinstance(rt, dict):
                rt = {}
            rt[stage] = {"task_id": task_id, "agent": payload["assigned_agent"], "deadline": deadline, "updated_at": now_ts(), "priority": priority}
            reg["_stage_runtime"] = rt
            save_sales_registry(reg)
            append_client_chat_message(cid, f"[CJM] Запущен этап: {stage} | task_id={task_id} | agent={payload['assigned_agent']} | deadline={deadline or '—'}")
            self._json({"status": "ok", "task_id": task_id})
        elif path == "api/sales/sla/escalate":
            cid = str(body.get("client_id", "")).strip()
            stage = str(body.get("stage", "")).strip()
            reason = str(body.get("reason", "")).strip()
            agent = str(body.get("agent", "min_ops")).strip() or "min_ops"
            deadline = str(body.get("deadline", "")).strip()
            if not cid or not stage:
                self._json({"status": "error", "message": "client_id and stage required"}, 400)
                return
            if not reason:
                self._json({"status": "error", "message": "reason required"}, 400)
                return
            task_id = f"TASK-SLA-{uuid.uuid4().hex[:8].upper()}"
            payload = {
                "task_id": task_id,
                "client_id": cid,
                "task_type": "sla_escalation",
                "stage": stage,
                "assigned_agent": agent,
                "priority": "high",
                "reason": reason,
                "deadline": deadline,
                "created_at": now_ts(),
            }
            p_dir = QUEUE_ROOT / "pending"
            p_dir.mkdir(parents=True, exist_ok=True)
            (p_dir / f"{task_id}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            reg = get_sales_registry()
            rt = reg.get("_stage_runtime", {})
            if not isinstance(rt, dict):
                rt = {}
            rt[stage] = {"task_id": task_id, "agent": payload["assigned_agent"], "deadline": deadline, "updated_at": now_ts(), "priority": "high", "status": "escalated"}
            reg["_stage_runtime"] = rt
            save_sales_registry(reg)
            append_client_chat_message(cid, f"[SLA] Эскалация этапа: {stage} | task_id={task_id} | reason={reason}")
            self._json({"status": "ok", "task_id": task_id})
        elif path.startswith("api/content/factory/"):
            parts = path.split("/")  # api/content/factory/{cid}/{action}
            if len(parts) == 5:
                cid, action = parts[3], parts[4]
                if action in ("add", "update", "delete", "move"):
                    self._json(save_content_factory(cid, action, body))
                else:
                    self._json({"error": "unknown action"}, 400)
            else:
                self._json({"error": "bad path"}, 400)
        elif path.startswith("api/sales/"):
            parts = path.split("/")
            if len(parts) < 3: self._json({"error": "invalid path"}, 400); return
            section = parts[2]
            action = parts[3] if len(parts) > 3 else "update"
            if section in ["leads", "clients", "deals", "proposals"]:
                reg = get_sales_registry()
                if action == "add":
                    body["id"] = str(uuid.uuid4())[:8]; body["created_at"] = _now_iso()
                    if section in ["leads", "clients", "deals"]:
                        body["Этап воронки"] = _normalize_funnel_stage(body.get("Этап воронки"))
                    reg.setdefault(section, []).append(body)
                    reg = _sync_sales_transitions(reg)
                    save_sales_registry(reg); self._json({"status": "ok", "data": body})
                elif action == "update":
                    item_id = body.get("id")
                    for i in reg.get(section, []):
                        if i.get("id") == item_id:
                            i.update(body)
                            if section in ["leads", "clients", "deals"]:
                                i["Этап воронки"] = _normalize_funnel_stage(i.get("Этап воронки"))
                            reg = _sync_sales_transitions(reg)
                            save_sales_registry(reg); self._json({"status": "ok"}); return
                    self._json({"error": "not found"}, 404)
                elif action == "delete":
                    item_id = body.get("id")
                    reg[section] = [i for i in reg.get(section, []) if i.get("id") != item_id]
                    reg = _sync_sales_transitions(reg)
                    save_sales_registry(reg); self._json({"status": "ok"})
                else: self._json({"error": "unknown action"}, 400)
            elif section == "dialogs" and action == "send":
                cid = body.get("client_id")
                text = body.get("text")
                if not cid or not text: self._json({"error": "missing data"}, 400); return
                history = get_chat_history(cid)
                history.append({"role": "assistant", "author": "sales_manager", "content": text, "ts": _now_iso()})
                save_chat(cid, history)
                self._json({"status": "ok"})
            else: self._json({"error": "not found"}, 404)
        elif path.startswith("api/clients/") and path.endswith("/chat/run"):
            cid = path.split("/")[2]
            text = str(body.get("text", "")).strip()
            mode = str(body.get("mode", "coordinator")).strip() or "coordinator"
            agent = str(body.get("agent", "")).strip()
            stage = str(body.get("stage", "")).strip()
            task_id = f"TASK-CHAT-{uuid.uuid4().hex[:8].upper()}"
            payload = {
                "task_id": task_id,
                "client_id": cid,
                "task_type": "chat_workflow_step",
                "mode": mode,
                "agent": agent,
                "stage": stage,
                "text": text,
                "created_at": now_ts(),
            }
            p_dir = QUEUE_ROOT / "pending"
            p_dir.mkdir(parents=True, exist_ok=True)
            (p_dir / f"{task_id}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            self._json({"status": "ok", "task_id": task_id})
        elif path.startswith("api/clients/") and path.endswith("/stage-chat"):
            cid = path.split("/")[2]
            text = str(body.get("text", "")).strip()
            st = body.get("stage_thread") or {}
            stage = str((st or {}).get("stage", "")).strip() or str(body.get("stage", "")).strip()
            run_id = str((st or {}).get("run_id", "")).strip()
            if not text or not stage:
                self._json({"status": "error", "message": "text and stage required"}, 400)
                return
            append_stage_chat_message(cid, stage, run_id, "you", text)
            append_stage_chat_message(cid, stage, run_id, "assistant", "Принято в этапный тред.")
            th = get_stage_chat(cid, stage, run_id)
            self._json({"status": "ok", "data": {"messages": th.get("messages", [])}})
        elif path.startswith("api/clients/") and path.endswith("/chat"):
            cid = path.split("/")[2]
            text = str(body.get("text", "")).strip()
            if not text:
                self._json({"status": "error", "message": "message text required"}, 400)
                return
            mode = str(body.get("mode", "coordinator")).strip() or "coordinator"
            agent = str(body.get("agent", "")).strip()
            stage = str(body.get("stage", "")).strip()
            ts = _now_iso()
            history = get_chat_history(cid)
            history.append({"role": "user", "content": text, "ts": ts, "mode": mode, "agent": agent, "stage": stage})
            if mode == "direct" and agent:
                bot_reply = f"Передал агенту {agent}. При необходимости нажмите 'Запустить'."
            else:
                bot_reply = "Принял. Могу помочь уточнить шаг и затем запустить задачу."
            history.append({"role": "assistant", "content": bot_reply, "ts": _now_iso(), "mode": mode, "agent": agent, "stage": stage})
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
        elif path == "api/workflow/run":
            client_id = str(body.get("client_id", "")).strip()
            workflow_type = str(body.get("workflow_type", "")).strip() or "content_plan_v1"
            if not client_id:
                self._json({"status": "error", "message": "client_id required"}, 400)
                return
            task_id = f"TASK-WF-{uuid.uuid4().hex[:8].upper()}"
            payload = {
                "task_id": task_id,
                "client_id": client_id,
                "task_type": workflow_type,
                "source": "dashboard",
                "created_at": now_ts(),
            }
            p_dir = QUEUE_ROOT / "pending"
            p_dir.mkdir(parents=True, exist_ok=True)
            (p_dir / f"{task_id}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            self._json({"status": "ok", "task_id": task_id})
        elif path == "api/workflow/action":
            cid = str(body.get("cid", "")).strip()
            tab_id = str(body.get("tab_id", "")).strip()
            item_id = str(body.get("item_id", "")).strip()
            action = str(body.get("action", "")).strip()
            if not cid or not tab_id or not item_id or not action:
                self._json({"status": "error", "message": "cid, tab_id, item_id, action required"}, 400)
                return
            ok, msg = workflow_action(cid, tab_id, item_id, action)
            self._json({"status": "ok"} if ok else {"status": "error", "message": msg}, 200 if ok else 400)
        elif path.startswith("api/module/"):
            parts = path.split("/")  # api/module/{cid}/{module}/{section}/action
            if len(parts) == 5 and parts[3] == "content" and parts[4] == "strategy":
                cid = parts[2]
                content_data = get_module_data(cid, "content")
                strat = content_data.setdefault("strategy", {})
                competitors = strat.setdefault("competitors", [])

                if "add_competitor" in body:
                    url = body["add_competitor"].strip()
                    if url and not any(c.get("url") == url for c in competitors):
                        competitors.append({"url": url, "last_parsed": None})

                elif "remove_competitor" in body:
                    try:
                        idx = int(body["remove_competitor"])
                        if 0 <= idx < len(competitors):
                            competitors.pop(idx)
                    except (ValueError, TypeError, IndexError):
                        pass

                elif body.get("action") == "parse_competitors":
                    from datetime import datetime
                    strat["last_parsed"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                    for c in competitors:
                        c["last_parsed"] = strat["last_parsed"]

                save_module_data(cid, "content", content_data)
                self._json({"status": "ok"})
                return

            if len(parts) == 6:
                cid, module, section, action = parts[2], parts[3], parts[4], parts[5]
                if module not in MODULE_EMPTY:
                    self._json({"status": "error", "message": "unknown module"}, 404)
                    return
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
        elif path.startswith("api/tech/scripts/"):
            action = path.split("/")[-1]
            if action in ("add", "update", "delete"):
                self._json(tech_scripts_action(action, body))
            else:
                self._json({"error": "not found"}, 404)
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

            c_folder = get_client_runtime_folder(client_id)
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
            run_id = "TESTRUN"
            
            if project_id and project_id != "all":
                st = c_folder / "projects" / project_id / "STATUS.md"
                if st.exists():
                    txt = st.read_text(encoding="utf-8")
                    m = re.search(r"sheet_url:\s*(https://\S+)", txt)
                    if m:
                        sheet_url = m.group(1)
                    m_run = re.search(r"run_id:\s*(\S+)", txt)
                    if m_run:
                        run_id = m_run.group(1)
            
            if not sheet_url:
                p_dir = c_folder / "projects"
                if p_dir.exists():
                    latest_status = None
                    latest_mtime = 0
                    for p in p_dir.iterdir():
                        st = p / "STATUS.md"
                        if st.exists():
                            m_time = st.stat().st_mtime
                            if m_time > latest_mtime:
                                latest_mtime = m_time
                                latest_status = st
                    if latest_status:
                        txt = latest_status.read_text(encoding="utf-8")
                        m = re.search(r"sheet_url:\s*(https://\S+)", txt)
                        if m:
                            sheet_url = m.group(1)
                        m_run = re.search(r"run_id:\s*(\S+)", txt)
                        if m_run:
                            run_id = m_run.group(1)

            if not sheet_url:
                self._json({
                    "status": "not_configured",
                    "message": "Google Sheet URL not configured in project STATUS.md"
                })
                return

            # Append lineage entry to lineage.jsonl
            lineage_data = {
                "timestamp": now_ts(),
                "artifact_type": "sheet_url",
                "client_id": client_id,
                "project_id": project_id,
                "run_id": run_id,
                "sheet_url": sheet_url
            }
            lineage_file = export_dir / "lineage.jsonl"
            try:
                with lineage_file.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(lineage_data, ensure_ascii=False) + "\n")
            except Exception as e:
                print(f"Error saving lineage: {e}")

            # Log audit event
            log_marketing_action(client_id, project_id, run_id, "export_google_sheet", f"Exported {tab_id} to Google Sheet {sheet_url}")

            self._json({
                "status": "ok",
                "client_id": client_id,
                "tab_id": tab_id,
                "export_path": str(csv_path),
                "json_path": str(json_path),
                "sheet_url": sheet_url
            })
        elif path.startswith("api/smm/"):
            parts = path.split("/")  # api/smm/{cid}/{type}
            if len(parts) == 4:
                cid, stype = parts[2], parts[3]
                smm_data = get_module_data(cid, "smm")
                if stype == "settings":
                    # Save settings fields (masked tokens preserved)
                    allowed = ["profile", "contacts", "schedule", "approval_rules",
                               "knowledge_bases", "brand_voice", "notifications",
                               "access_rights", "integrations"]
                    for k in allowed:
                        if k in body:
                            smm_data[k] = body[k]
                    if "socials_raw" in body:
                        # socials_raw: list of {platform, handle, token}
                        socials_raw = body["socials_raw"]
                        existing = {s.get("platform"): s for s in smm_data.get("socials", [])}
                        merged = []
                        for s in socials_raw:
                            plat = s.get("platform", "")
                            token = s.get("token", "")
                            # Keep existing real token if masked value sent
                            if token.startswith("••••") and plat in existing:
                                token = existing[plat].get("token", token)
                            merged.append({"platform": plat, "handle": s.get("handle", ""), "token": token})
                        smm_data["socials"] = merged
                    if "publication_sources" in body and isinstance(body["publication_sources"], list):
                        pub_sources = []
                        for s in body["publication_sources"]:
                            if not isinstance(s, dict):
                                continue
                            name = str(s.get("name", "")).strip()
                            url = str(s.get("url", "")).strip()
                            if not name and not url:
                                continue
                            pub_sources.append({
                                "name": name,
                                "url": url,
                                "type": str(s.get("type", "other")).strip().lower(),
                                "status": str(s.get("status", "draft")).strip().lower()
                            })
                        smm_data["publication_sources"] = pub_sources

                        # Cross-sync to marketing traffic_sources for the same client.
                        mkt_data = get_module_data(cid, "marketing")
                        traffic = mkt_data.get("traffic_sources", [])
                        if not isinstance(traffic, list):
                            traffic = []
                        for src in pub_sources:
                            key_name = src.get("name", "")
                            key_url = src.get("url", "")
                            found = None
                            for row in traffic:
                                rn = str(row.get("Название") or row.get("Канал") or "").strip()
                                ru = str(row.get("Ссылка") or row.get("URL") or "").strip()
                                if (key_name and rn == key_name) or (key_url and ru == key_url):
                                    found = row
                                    break
                            if not found:
                                found = {
                                    "id": str(uuid.uuid4())[:8],
                                    "created_at": now_ts(),
                                    "Название": key_name,
                                    "Ссылка": key_url,
                                    "Тип": src.get("type", "other"),
                                    "Статус": src.get("status", "draft"),
                                    "Клиенты": cid,
                                    "max_title_length": 90,
                                    "max_post_chars": 2200,
                                    "max_hashtags": 10
                                }
                                traffic.append(found)
                            else:
                                found["Название"] = key_name or found.get("Название") or found.get("Канал") or ""
                                found["Ссылка"] = key_url or found.get("Ссылка") or found.get("URL") or ""
                                found["Тип"] = src.get("type", found.get("Тип", "other"))
                                found["Статус"] = src.get("status", found.get("Статус", "draft"))
                                clients_s = str(found.get("Клиенты") or "").strip()
                                clients = [x.strip() for x in clients_s.split(",") if x.strip()]
                                if cid not in clients:
                                    clients.append(cid)
                                found["Клиенты"] = ", ".join(clients)
                                found.setdefault("max_title_length", 90)
                                found.setdefault("max_post_chars", 2200)
                                found.setdefault("max_hashtags", 10)
                        mkt_data["traffic_sources"] = traffic
                        save_module_data(cid, "marketing", mkt_data)
                    rf = get_client_runtime_folder(cid) / "smm.json"
                    rf.parent.mkdir(parents=True, exist_ok=True)
                    rf.write_text(json.dumps(smm_data, ensure_ascii=False, indent=2), encoding="utf-8")
                    self._json({"status": "ok"})
                elif stype == "publications":
                    ap = smm_data.setdefault("autoposting", {})
                    if "enabled" in body:
                        ap["enabled"] = bool(body["enabled"])
                    if "platforms" in body:
                        ap["platforms"] = body["platforms"]
                    if "add_slot" in body:
                        slots = ap.setdefault("slots", [])
                        slot_id = f"slot_{int(time.time())}"
                        slots.append({"id": slot_id, "time": body["add_slot"]})
                    if "remove_slot" in body:
                        ap["slots"] = [s for s in ap.get("slots", []) if s.get("id") != body["remove_slot"]]
                    smm_data["autoposting"] = ap
                    rf = get_client_runtime_folder(cid) / "smm.json"
                    rf.parent.mkdir(parents=True, exist_ok=True)
                    rf.write_text(json.dumps(smm_data, ensure_ascii=False, indent=2), encoding="utf-8")
                    self._json({"status": "ok"})
                else:
                    self._json({"error": "unknown smm POST type"}, 404)
            else:
                self._json({"error": "invalid smm path"}, 400)
        else:
            self._json({"error": "not found"}, 404)

if __name__ == "__main__":
if __name__ == "__main__":
    print(f"Starting dashboard on port {PORT}")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()
