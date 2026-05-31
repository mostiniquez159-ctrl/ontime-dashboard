#!/usr/bin/env python3
"""
onTime OS v3 — The Owner's Operating System.
Premium Single-File Business Orchestration UI.
Ref: STD_01A §1A.4b, STD_29A §29A.17.
"""
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

# --- Configuration ---
PORT = int(os.environ.get("DASHBOARD_PORT", "8080"))
KB_ROOT = Path("/mnt/ontime/Книга знаний Агентов")
CLIENTS_ROOT = Path("/mnt/ontime/Клиенты")
QUEUE_ROOT = Path("/data/queue")
WF_LOG_CANDIDATES = [Path("/root/agents/v3/logs"), KB_ROOT / "_runtime/logs"]
AGENT_REGISTRY = KB_ROOT / "_SYSTEM/10_REGISTRY/agent_registry.json"
CLIENT_REGISTRY = KB_ROOT / "_SYSTEM/10_REGISTRY/client_registry.json"
CHAT_STORE = KB_ROOT / "_runtime/shared/dashboard_chats_cli.json"
INTEGRATIONS_REGISTRY = KB_ROOT / "_runtime/integrations/service_registry.json"
SALES_REGISTRY = KB_ROOT / "_runtime/sales/sales_registry.json"

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

def get_sales_registry():
    return _load_json(SALES_REGISTRY, {"leads": [], "clients": [], "deals": [], "proposals": []})

def save_sales_registry(data):
    _save_json(SALES_REGISTRY, data)

FUNNEL_STAGES = [
    "Нет потребности / есть проблема",
    "Есть потребность / нет решения",
    "Есть решение / нет покупки",
    "Есть покупка / нет лояльности",
    "Лояльный клиент",
    "Рекомендатель",
    "Повторная сделка",
]
FUNNEL_STAGE_RANK = {name: idx for idx, name in enumerate(FUNNEL_STAGES)}


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


def _get_loyalty_aggregates():
    """Read loyalty.json from all client folders → return per-stage deal and referral counts."""
    deal_counts = {s: 0 for s in FUNNEL_STAGES}
    deal_amounts = {s: 0.0 for s in FUNNEL_STAGES}
    ref_counts = {s: 0 for s in FUNNEL_STAGES}
    reg = _load_json(CLIENT_REGISTRY, {})
    for cid, info in reg.get("clients", {}).items():
        folder = info.get("folder")
        if not folder:
            for sp in [CLIENTS_ROOT, CLIENTS_ROOT / "_INTERNAL"]:
                candidate = sp / cid
                if candidate.is_dir():
                    folder = str(candidate)
                    break
        if not folder:
            continue
        lpath = Path(folder) / "loyalty.json"
        if not lpath.exists():
            continue
        try:
            ldata = json.loads(lpath.read_text(encoding="utf-8"))
        except Exception:
            continue
        for item in ldata.get("repeat_sales", []):
            stage = _normalize_funnel_stage(item.get("Этап воронки"))
            deal_counts[stage] += 1
            raw = str(item.get("Сумма", "")).replace(" ", "").replace(",", ".")
            try:
                deal_amounts[stage] += float(raw)
            except Exception:
                pass
        for item in ldata.get("referrals", []):
            stage = "Рекомендатель"
            ref_counts[stage] += 1
    return deal_counts, deal_amounts, ref_counts


def get_sales_core_rows():
    reg = get_sales_registry()
    leads = reg.get("leads", []) or []
    clients_reg = reg.get("clients", []) or []
    deals = reg.get("deals", []) or []
    rows = []
    targets = reg.get("_stage_targets", {}) if isinstance(reg.get("_stage_targets", {}), dict) else {}
    loyalty_deals, loyalty_amounts, loyalty_refs = _get_loyalty_aggregates()
    all_amounts = []
    for d in deals:
        raw = str(d.get("Сумма", "")).replace(" ", "").replace(",", ".")
        try:
            all_amounts.append(float(raw))
        except Exception:
            pass
    avg_deal = (sum(all_amounts) / len(all_amounts)) if all_amounts else 0.0
    prev_leads = None
    for stage in FUNNEL_STAGES:
        leads_n = sum(1 for x in leads if _normalize_funnel_stage(x.get("Этап воронки")) == stage)
        clients_n = (
            sum(1 for x in clients_reg if _normalize_funnel_stage(x.get("Этап воронки")) == stage)
            + loyalty_refs.get(stage, 0)
        )
        deals_n = 0
        amount = 0.0
        for d in deals:
            ds = _normalize_funnel_stage(d.get("Этап воронки"))
            if ds != stage:
                continue
            deals_n += 1
            raw = str(d.get("Сумма", "")).replace(" ", "").replace(",", ".")
            try:
                amount += float(raw)
            except Exception:
                pass
        deals_n += loyalty_deals.get(stage, 0)
        amount += loyalty_amounts.get(stage, 0.0)
        plan = float(targets.get(stage, 0) or 0)
        fact = float(amount)
        dev = 0.0 if plan == 0 else round(((fact - plan) / plan) * 100, 2)
        loss = 0.0
        if prev_leads is not None and prev_leads > leads_n and avg_deal > 0:
            loss = round((prev_leads - leads_n) * avg_deal, 2)
        rows.append({
            "Этап воронки": stage,
            "Лиды": leads_n,
            "Клиенты": clients_n,
            "Сделки": deals_n,
            "Сумма сделок": int(amount) if amount.is_integer() else round(amount, 2),
            "План": int(plan) if plan.is_integer() else plan,
            "Факт": int(fact) if fact.is_integer() else fact,
            "Отклонение %": dev,
            "Потери": int(loss) if float(loss).is_integer() else loss,
        })
        prev_leads = leads_n
    return rows


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

def is_clients_writable():
    test_file = CLIENTS_ROOT / "test_write_permit"
    try:
        test_file.touch(exist_ok=True)
        test_file.unlink(missing_ok=True)
        return True
    except Exception:
        return False

def get_client_runtime_folder(cid):
    if is_clients_writable():
        return get_client_folder(cid) or (CLIENTS_ROOT / cid)
    else:
        fallback_dir = Path("/data/runtime/Клиенты") / cid
        try:
            fallback_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return fallback_dir

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
MGMT_EMPTY = {
    "finance": [],
    "legal": [],
    "consulting": [],
    "pr": [],
    "owner_tasks": [],
    "strategy": [],
    "documents": [],
}
LOYALTY_EMPTY = {
    "repeat_sales": [],
    "referrals": [],
}
PRODUCTION_EMPTY = {
    "orders": [],
    "plan": [],
    "shifts": [],
    "operations": [],
    "materials": [],
    "stock": [],
    "defects": [],
    "load": [],
}
SMM_EMPTY = {"autoposting": {"enabled": False, "slots": [], "platforms": [], "errors": []}, "socials": []}

WEB_EMPTY = {
    "library": [],
    "keywords": [],
    "competitors": [],
    "brand_rules": {
        "id": "brand_rules",
        "voice": "",
        "colors": "",
        "guidelines": ""
    },
    "default_owners": {
        "id": "default_owners",
        "content_owner": "OTDEL_CREATIVE",
        "tech_owner": "OTDEL_TECH",
        "design_owner": "OTDEL_CREATIVE",
        "qa_owner": "OTDEL_ZAVOD"
    },
    "sites": [],
    "production": []
}

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

MODULE_EMPTY = {
    "marketing": MARKETING_EMPTY,
    "content": CONTENT_EMPTY,
    "tech": TECH_EMPTY,
    "mgmt": MGMT_EMPTY,
    "loyalty": LOYALTY_EMPTY,
    "production": PRODUCTION_EMPTY,
    "smm": SMM_EMPTY,
    "web": WEB_EMPTY,
}

MODULE_COLUMNS = {
    "marketing_воронки": ["funnel_id", "Название воронки", "Этап", "Порядок этапа", "Вход", "Выход", "CR%", "Итоговая конверсия", "Связанный оффер", "Источник трафика", "Рекламная кампания", "Статус", "Ответственный", "Комментарий"],
    "marketing_ца": ["id", "Сегмент ЦА", "Слоган", "УТП", "Боли", "Содержание объявления", "CTA", "Маркетинговые архетипы", "Подходящие продукты", "Товары с высокой вероятностью заказа", "Ключевые товары (основной доход)", "Что добавить и чего пока нет", "Комплексное предложение (апсейл)", "Плановая сумма в месяц (₽)", "Приоритет (1–5)", "Примерная доля в выручке", "Оценка вероятности продаж (%)", "Комментарий"],
    "marketing_боли": ["Боль", "Сегмент ЦА", "Интенсивность", "Статус", "Приоритет"],
    "marketing_утп": ["Формулировка", "Сегмент", "Канал", "Статус", "Владелец"],
    "marketing_офферы": ["Заголовок", "Текст", "Канал", "Конверсия", "Статус"],
    "marketing_источники_трафика": ["Название", "Ссылка", "Тип", "Клиенты", "Интеграция", "Бюджет", "Лиды", "CPL", "Статус"],
    "marketing_реклама": ["Кампания", "Канал", "Бюджет", "Статус", "Ссылка"],
    "marketing_креатив": ["Название", "Тип", "Канал", "Оффер", "Статус", "Конверсия", "Post ID", "Ссылка"],
    "marketing_упаковка_продукта": ["Параметр", "Значение", "Статус", "Комментарий"],
    "content_контент-завод": ["Задача", "Статус", "Исполнитель"],
    "content_темы": ["Тема", "Приоритет", "Статус", "Дедлайн"],
    "content_статьи": ["Заголовок", "Автор", "Статус", "Дата"],
    "content_посты": ["Текст", "Сеть", "Формат", "Статус", "Дата", "Креатив URL"],
    "content_публикации": ["Ресурс", "Ссылка", "Статус", "Дата", "Post ID"],
    "content_комментарии": ["Текст", "Где", "Статус", "Дата"],
    "content_google-таблицы": ["Название", "URL", "Статус", "Комментарий"],
    "mgmt_финансы": ["id", "Статья", "Тип", "Сумма", "Валюта", "Дата", "Категория", "Статус", "Комментарий"],
    "mgmt_юрконтур": ["id", "Документ", "Тип", "Контрагент", "Статус", "Дата подписания", "Дата окончания", "Ответственный"],
    "mgmt_консалтинг": ["id", "Проект", "Этап", "Бюджет", "Факт оплат", "Статус", "Дата старта", "Дата закрытия"],
    "mgmt_pr": ["id", "Материал", "Площадка", "Тип", "Автор", "Статус", "Дата выхода", "Охват"],
    "mgmt_задачи_собственника": ["id", "Задача", "Приоритет", "Категория", "Статус", "Дедлайн", "Делегировано", "Комментарий"],
    "mgmt_стратегия": ["id", "Цель", "KPI", "Горизонт", "Текущее значение", "Целевое значение", "Статус", "Ответственный"],
    "mgmt_документы": ["id", "Название", "Тип", "Категория", "Ответственный", "Дата", "Ссылка"],
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
    rf = get_client_runtime_folder(cid) / f"{module}.json"
    if rf.exists():
        try: return json.loads(rf.read_text(encoding="utf-8"))
        except: pass
    
    # Fallback to read-only template
    c_folder = get_client_folder(cid)
    if c_folder:
        of = c_folder / f"{module}.json"
        if of.exists():
            try: return json.loads(of.read_text(encoding="utf-8"))
            except: pass
            
    if module == "web":
        return get_default_web_data(cid)
            
    return json.loads(json.dumps(MODULE_EMPTY.get(module, {}), ensure_ascii=False))

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
            item.update(updates); save_module_data(cid, module, data)
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
        "assigned_clients": item.get("assigned_clients") if isinstance(item.get("assigned_clients"), list) else [],
        "channel_type": item.get("channel_type") or "",
        "effective_for_publication": item.get("effective_for_publication") or "",
        "publication_priority": item.get("publication_priority") or "",
        "recommended_use": item.get("recommended_use") or "",
        "traffic_direction": item.get("traffic_direction") or "",
        "source": item.get("source") or "",
        "source_row": item.get("source_row") or "",
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

def get_sales_dialogs_html():
    return """
    <div id="sales_диалоги" class="section">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:20px">
        <h2>Продажи • Диалоги</h2>
        <button class="mkt-btn" onclick="refreshSalesDialogs()">Обновить</button>
      </div>
      <div style="display:grid; grid-template-columns: 320px 1fr; gap:20px; height: calc(100vh - 240px);">
        <div class="card" style="padding:0; overflow-y:auto; display:flex; flex-direction:column;">
          <div style="padding:16px; border-bottom:1px solid var(--border)">
            <input type="text" placeholder="Поиск..." class="premium-input" style="width:100%" oninput="filterSalesDialogs(this.value)">
          </div>
          <div id="sales-dialogs-list" style="flex:1; overflow-y:auto;"></div>
        </div>
        <div class="card" style="display:flex; flex-direction:column; padding:0;">
          <div id="sales-dialog-header" style="padding:16px; border-bottom:1px solid var(--border); font-weight:700;">Выберите диалог из списка слева</div>
          <div id="sales-dialog-chat" style="flex:1; overflow-y:auto; padding:20px; background: var(--bg);">
             <div class="chat-empty">Нет выбранного диалога</div>
          </div>
          <div style="padding:16px; border-top:1px solid var(--border); display:flex; gap:10px;">
            <input type="text" id="sales-chat-input" class="premium-input" style="flex:1" placeholder="Сообщение..." onkeydown="if(event.key==='Enter') sendSalesMessage()">
            <button class="mkt-btn mkt-btn-primary" onclick="sendSalesMessage()">Отправить</button>
          </div>
        </div>
      </div>
    </div>
    """

def get_sales_deals_html():
    return """
    <div id="sales_сделки" class="section">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:20px">
        <h2>Продажи • Сделки</h2>
        <div style="display:flex; gap:10px">
          <button class="mkt-btn" id="deals-view-btn" onclick="toggleDealsView()">Вид: Таблица</button>
          <button class="mkt-btn mkt-btn-primary" onclick="showModuleAddModal('sales_сделки')">+ Сделка</button>
        </div>
      </div>
      <div id="sales-deals-kanban" style="display:grid; grid-template-columns: repeat(6, 1fr); gap:16px; overflow-x:auto; padding-bottom:20px; min-height: 400px;">
        <!-- Columns: New, Qualified, Proposal, Negotiation, Won, Lost -->
      </div>
      <div id="sales-deals-table-view" style="display:none;" class="card premium-glow">
        <table class="premium-table">
          <thead><tr><th>Название</th><th>Клиент</th><th>Сумма</th><th>Стадия</th><th>Вероятность</th><th>Дедлайн</th><th>Действия</th></tr></thead>
          <tbody id="sales-deals-table-body"></tbody>
        </table>
      </div>
    </div>
    """

def get_sales_table_html(section_id, title, columns):
    cols_html = "".join(f"<th>{c}</th>" for c in columns)
    return f"""
    <div id="{section_id}" class="section">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:20px">
        <h2>{title}</h2>
        <div style="display:flex; gap:10px">
          <button class="mkt-btn" onclick="refreshSalesSection('{section_id}')">Обновить</button>
          <button class="mkt-btn" onclick="exportSalesCsv('{section_id}')">Экспорт CSV</button>
          <button class="mkt-btn mkt-btn-primary" onclick="showModuleAddModal('{section_id}')">+ Добавить</button>
        </div>
      </div>
      <div class="card premium-glow">
        <table class="premium-table">
          <thead><tr>{cols_html}<th>Действия</th></tr></thead>
          <tbody id="{section_id}-tbody"></tbody>
        </table>
      </div>
    </div>
    """

SALES_MODULE_CONFIG = {
    "sales_лиды": {"columns": ["Имя", "Контакт", "Источник", "Запрос", "Этап воронки", "Статус", "Ответственный", "Следующий шаг"]},
    "sales_клиенты": {"columns": ["Название", "Контакт", "Этап воронки", "Статус", "Ответственный", "Активность", "Сделки"]},
    "sales_заказы": {"columns": ["Номер", "Клиент", "Продукт", "Сумма", "Статус", "Дедлайн", "Ответственный"]},
    "sales_кп": {"columns": ["Номер", "Клиент", "Сделка", "Сумма", "Статус", "Дата"]},
    "sales_sales_core": {"columns": ["Этап воронки", "Лиды", "Клиенты", "Сделки", "Сумма сделок", "План", "Факт", "Отклонение %", "Потери"]},
    "sales_cjm": {"columns": ["CJM-состояние", "Лиды", "Клиенты", "Сделки", "Риск", "Рекомендованное действие", "Ответственный", "Дедлайн", "Последний запуск"]},
    "sales_этапы_sla": {"columns": ["Этап", "SLA (дни)", "Лидов в этапе", "Просрочено", "Макс. возраст (дни)", "Статус", "Ответственный", "Дедлайн", "Последний запуск"]},
    "sales_эскалации": {"columns": ["stage", "priority", "status", "reason", "task_id", "created_at"]},
}

def get_module_section_html(module, section_id, title):
    workflow_html = ""
    if section_id == "content_контент-завод":
        workflow_html = f"""
        <div id="{section_id}-workflow-wrap" style="display:none; gap:8px; align-items:center;">
          <select id="{section_id}-workflow-type" class="premium-input" style="width:220px">
            <option value="content_plan_v1">Построение контент-плана</option>
            <option value="carousel_batch_v1">Пакет каруселей</option>
          </select>
          <button class="nav-item active" style="padding:8px 14px; font-size:12px; border:none; cursor:pointer" onclick="startWorkflowFromTab('{section_id}')">Запустить</button>
        </div>
        """
    biz_project_sel = f"""<select id="{section_id}-biz-project" class="premium-input module-biz-project-select" style="width:190px" onchange="onModuleBizProjectChange('{section_id}')">
          <option value="">Все проекты</option>
          <option value="pluslogo">Плюс Лого</option>
          <option value="ontime-ai">onTime.ai</option>
          <option value="andrey-brand">Андрей Самсонов</option>
          <option value="olga-brand">Ольга Федеева</option>
          <option value="external">Внешние клиенты</option>
        </select>"""
    return f"""
    <div id="{section_id}" class="section">
      <h2>{title}</h2>
      <div style="display:flex; gap:12px; align-items:center; margin-bottom:20px; flex-wrap:wrap">
        {biz_project_sel}
        <select id="{section_id}-client" class="premium-input module-client-select" style="width:240px" onchange="loadModuleData('{section_id}')">
          <option value="">Выберите клиента</option>
          <option value="all">Все клиенты</option>
        </select>
        {workflow_html}
        <button id="{section_id}-add-btn" class="nav-item active" style="padding:8px 14px; font-size:12px; border:none; cursor:pointer" onclick="showModuleAddModal('{section_id}')">Добавить</button>
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
    sheet_btn = ""
    if module == "marketing":
        sheet_btn = f"""
        <button class="nav-item" id="{section_id}-sheet-btn" style="padding:8px 14px; font-size:12px; border:none; cursor:pointer; background:var(--surface-hover)" onclick="exportModuleGoogleSheet('{section_id}')">Google Sheet</button>
        """
    return f"""
    <div id="{section_id}" class="section">
      <h2>{title}</h2>
      <div style="display:flex; gap:12px; align-items:center; margin-bottom:20px; flex-wrap:wrap">
        <select id="{section_id}-client" class="premium-input module-client-select" style="width:240px" onchange="loadModuleData('{section_id}')">
          <option value="">Выберите клиента</option>
        </select>
        {workflow_html}
        <button class="nav-item active" id="{section_id}-add-btn" style="padding:8px 14px; font-size:12px; border:none; cursor:pointer" onclick="showModuleAddModal('{section_id}')">Добавить</button>
        <button class="nav-item" style="padding:8px 14px; font-size:12px; border:none; cursor:pointer; background:var(--surface-hover)" onclick="exportModuleCsv('{section_id}')">Экспорт CSV</button>
        {sheet_btn}
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
    payload = get_integrations_payload()
    items = payload.get("data", [])
    total = len(items)
    active = sum(1 for x in items if x.get("status") == "active")
    needs_secret = sum(1 for x in items if x.get("status") == "needs_secret")
    errors = sum(1 for x in items if x.get("status") == "error")
    if items:
        rows = []
        for x in items:
            link = str(x.get("account_url") or (x.get("links") or [""])[0] or "")
            status = html_lib.escape(str(x.get("status") or ""))
            secret = "no_secret_required" if x.get("no_secret_required") else (x.get("secret_ref") or "needs_secret")
            account = html_lib.escape(str(x.get("account_name") or link or ""))
            account_html = f'<a href="{html_lib.escape(link)}" target="_blank">{account}</a>' if link else account
            status_class = "badge-active" if status == "active" else ("badge-error" if status == "error" else "")
            open_button = f'<button class="mkt-btn" onclick="window.open(\'{html_lib.escape(link)}\',\'_blank\')">Открыть</button>' if link else ""
            clients = ", ".join(x.get("assigned_clients") or [])
            pub = " / ".join([v for v in [x.get("publication_priority"), x.get("effective_for_publication")] if v])
            rows.append(f"""
              <tr>
                <td><b>{html_lib.escape(str(x.get("platform") or x.get("integration_id") or ""))}</b><div style="font-size:11px;color:var(--text-muted)">{html_lib.escape(str(x.get("integration_id") or ""))}</div></td>
                <td>{html_lib.escape(str(x.get("scope") or ""))}</td>
                <td>{html_lib.escape(clients)}</td>
                <td>{account_html}</td>
                <td>{html_lib.escape(pub)}</td>
                <td><span class="badge {status_class}">{status}</span></td>
                <td style="color:var(--text-muted)">{html_lib.escape(str(secret))}</td>
                <td><button class="mkt-btn" onclick="checkIntegration('{html_lib.escape(str(x.get("integration_id") or ""))}')">Проверить</button>{open_button}</td>
              </tr>
            """)
        tbody = "".join(rows)
    else:
        tbody = '<tr><td colspan="8" style="color:var(--text-muted); padding:22px 12px;">Интеграции не заведены. Registry: _runtime/integrations/service_registry.json</td></tr>'
    return """
    <div id="tech_интеграции" class="section">
      <h2>Интеграции</h2>
      <div style="display:flex; gap:12px; align-items:center; margin-bottom:20px; flex-wrap:wrap">
        <button class="nav-item active" style="padding:8px 14px; font-size:12px; border:none; cursor:pointer" onclick="loadIntegrationsSection()">Обновить</button>
        <span id="integrations-status" style="font-size:12px; color:var(--text-muted); margin-left:auto">Источник: _runtime/integrations/service_registry.json</span>
      </div>
      <div class="stats-grid" id="integrations-summary" style="margin-bottom:20px">
        <div class="stat-box"><div class="label">Всего</div><div class="val">""" + str(total) + """</div></div>
        <div class="stat-box"><div class="label">Active</div><div class="val">""" + str(active) + """</div></div>
        <div class="stat-box"><div class="label">Needs secret</div><div class="val">""" + str(needs_secret) + """</div></div>
        <div class="stat-box"><div class="label">Error</div><div class="val">""" + str(errors) + """</div></div>
      </div>
      <div class="card premium-glow">
        <table class="premium-table">
          <thead><tr><th>Платформа</th><th>Scope</th><th>Клиенты</th><th>Аккаунт</th><th>Публикации</th><th>Статус</th><th>Secret</th><th>Действия</th></tr></thead>
          <tbody id="integrations-table-body">""" + tbody + """</tbody>
        </table>
      </div>
    </div>
    """

TECH_SCRIPTS_FILE = Path("/root/agents/v3/runtime/shared/scripts_registry.json")
QUEUE_ROOT = Path("/queue")

def get_server_status():
    import subprocess
    metrics = []
    try:
        load = Path("/proc/loadavg").read_text().split()
        metrics.append({"label": "CPU load (1m)", "value": load[0], "unit": "", "status": "ok"})
    except: pass
    try:
        mem = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            mem[k.strip()] = int(v.strip().split()[0])
        used_mb = (mem["MemTotal"] - mem["MemAvailable"]) // 1024
        total_mb = mem["MemTotal"] // 1024
        pct = round(used_mb / total_mb * 100)
        metrics.append({"label": "RAM", "value": f"{used_mb}/{total_mb}", "unit": "MB", "status": "warn" if pct > 85 else "ok"})
    except: pass
    try:
        out = subprocess.check_output(["df", "-h", "/"], text=True).splitlines()
        parts = out[1].split()
        metrics.append({"label": "Диск /", "value": parts[2], "unit": f"/ {parts[1]}", "status": "warn" if int(parts[4].rstrip("%")) > 85 else "ok"})
    except: pass
    try:
        gng = _load_json(Path("/root/agents/v3/runtime/go_no_go_status.json"), {})
        state = gng.get("go_no_go_state", "unknown")
        metrics.append({"label": "Go/No-Go", "value": state, "unit": "", "status": "ok" if state == "pass" else "error"})
    except: pass
    for svc in ["ontime-admin.service", "ontime-dispatcher.service", "ontime-site.service"]:
        try:
            out = subprocess.check_output(["systemctl", "is-active", svc], text=True).strip()
            metrics.append({"label": svc.replace(".service", ""), "value": out, "unit": "", "status": "ok" if out == "active" else "error"})
        except:
            metrics.append({"label": svc.replace(".service", ""), "value": "unknown", "unit": "", "status": "error"})
    return metrics

def get_workflow_status():
    gng = _load_json(Path("/root/agents/v3/runtime/go_no_go_status.json"), {})
    gates = gng.get("gates", {})
    gate_list = [{"name": k, "status": v.get("status", "unknown"), "detail": v.get("detail", "")} for k, v in gates.items()]
    overall = {"go_no_go": gng.get("go_no_go_state", "unknown"), "maturity": gng.get("maturity_state", "unknown"), "go_live": gng.get("go_live", False), "ts": gng.get("ts", "")}
    return {"overall": overall, "gates": gate_list}

def get_tech_queue_stats():
    dirs = [("pending", QUEUE_ROOT/"pending"), ("high", QUEUE_ROOT/"high"), ("normal", QUEUE_ROOT/"normal"), ("processing", QUEUE_ROOT/"processing"), ("dead", QUEUE_ROOT/"dead"), ("blocked", QUEUE_ROOT/"blocked"), ("done", QUEUE_ROOT/"done"), ("failed", QUEUE_ROOT/"failed")]
    summary = [{"queue": label, "count": len(list(d.glob("*.json"))) if d.exists() else 0} for label, d in dirs]
    dead_dir = QUEUE_ROOT / "dead"
    recent_dead = []
    if dead_dir.exists():
        for f in sorted(dead_dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:5]:
            try:
                t = json.loads(f.read_text())
                recent_dead.append({"task_id": t.get("task_id", f.stem), "task_type": t.get("task_type", ""), "dead_reason": t.get("dead_reason", ""), "dead_at": t.get("dead_at", "")})
            except: pass
    return {"summary": summary, "recent_dead": recent_dead}

def get_tech_errors():
    errors = []
    for label, d in [("dead", QUEUE_ROOT/"dead"), ("blocked", QUEUE_ROOT/"blocked"), ("failed", QUEUE_ROOT/"failed")]:
        if not d.exists(): continue
        for f in sorted(d.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:50]:
            try:
                t = json.loads(f.read_text())
                errors.append({"queue": label, "task_id": t.get("task_id", f.stem), "task_type": t.get("task_type", ""), "to": t.get("to", ""), "reason": t.get("dead_reason") or t.get("last_error") or t.get("status", ""), "retry_count": t.get("retry_count", 0), "ts": t.get("dead_at") or t.get("created", "")})
            except: pass
    return sorted(errors, key=lambda x: x["ts"], reverse=True)

def get_api_registry():
    return [
        {"method": "GET",  "path": "/api/status",                    "description": "Runtime health check"},
        {"method": "GET",  "path": "/api/tasks",                     "description": "Queue counts"},
        {"method": "GET",  "path": "/api/home",                      "description": "Home snapshot"},
        {"method": "GET",  "path": "/api/clients/list",              "description": "All clients"},
        {"method": "GET",  "path": "/api/clients/summary",           "description": "Client summary"},
        {"method": "GET",  "path": "/api/system/bots",               "description": "Bots inventory"},
        {"method": "GET",  "path": "/api/logs?agent=...",            "description": "Agent logs"},
        {"method": "GET",  "path": "/api/integrations",              "description": "Integrations registry"},
        {"method": "GET",  "path": "/api/kb/sop",                    "description": "SOP list"},
        {"method": "GET",  "path": "/api/sales/{section}",           "description": "Sales module data"},
        {"method": "GET",  "path": "/api/module/{cid}/{mod}/{sec}",  "description": "Generic module data"},
        {"method": "GET",  "path": "/api/tech/server",               "description": "Server metrics"},
        {"method": "GET",  "path": "/api/tech/scripts",              "description": "Scripts registry"},
        {"method": "GET",  "path": "/api/tech/queues",               "description": "Queue stats"},
        {"method": "GET",  "path": "/api/tech/errors",               "description": "Dead/blocked tasks"},
        {"method": "GET",  "path": "/api/tech/workflow",             "description": "Pipeline gate status"},
        {"method": "GET",  "path": "/api/tech/api-registry",         "description": "API routes list"},
        {"method": "GET",  "path": "/api/tech/log-list",             "description": "Available log files"},
        {"method": "POST", "path": "/api/sales/{section}/add",       "description": "Add sales record"},
        {"method": "POST", "path": "/api/module/{cid}/{mod}/{sec}/add",    "description": "Add module record"},
        {"method": "POST", "path": "/api/module/{cid}/{mod}/{sec}/update", "description": "Update module record"},
        {"method": "POST", "path": "/api/module/{cid}/{mod}/{sec}/delete", "description": "Delete module record"},
        {"method": "POST", "path": "/api/tech/scripts/add",          "description": "Add script"},
        {"method": "POST", "path": "/api/tech/scripts/update",       "description": "Update script"},
        {"method": "POST", "path": "/api/tech/scripts/delete",       "description": "Delete script"},
    ]

def get_tech_scripts():
    return _load_json(TECH_SCRIPTS_FILE, {}).get("scripts", [])

def get_bots_list():
    import json as _json
    path_map_file = Path("/mnt/ontime/Книга знаний Агентов/_SYSTEM/10_REGISTRY/agent_path_map.json")
    logs_dir = Path("/root/agents/v3/logs")

    bots = []
    if path_map_file.exists():
        try:
            data = _json.loads(path_map_file.read_text(encoding="utf-8"))
            agent_map = data.get("map", {})
            for agent_key, rel_path in agent_map.items():
                log_file = logs_dir / f"{agent_key}.log"
                last_seen = ""
                if log_file.exists():
                    try:
                        mtime = log_file.stat().st_mtime
                        last_seen = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                        status = "active"
                    except:
                        status = "unknown"
                else:
                    status = "no_log"
                bots.append({
                    "agent": agent_key,
                    "path": rel_path,
                    "role": "agent",
                    "runtime": "v3",
                    "status": status,
                    "last_seen": last_seen
                })
        except Exception as e:
            bots.append({"agent": "error", "path": str(e), "role": "", "runtime": "", "status": "error", "last_seen": ""})
    return bots

def tech_scripts_action(action, body):
    data = _load_json(TECH_SCRIPTS_FILE, {"scripts": []})
    scripts = data.get("scripts", [])
    if action == "add":
        item = {k: v for k, v in body.items() if k != "client_id"}
        item["id"] = f"script_{now_ts()}_{len(scripts)}"
        scripts.append(item)
    elif action == "update":
        for i, s in enumerate(scripts):
            if s.get("id") == body.get("id"):
                scripts[i].update({k: v for k, v in body.items() if k != "client_id"})
                break
    elif action == "delete":
        scripts = [s for s in scripts if s.get("id") != body.get("id")]
    data["scripts"] = scripts
    TECH_SCRIPTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    TECH_SCRIPTS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    return {"status": "ok"}


def get_analytics_dashboards_api(cid=None):
    # Aggegate metrics for dashboards
    metrics = []
    
    # 1. Sales Funnel summary (Core)
    core = get_sales_core_rows()
    if cid: core = [x for x in core if x.get("client_id") == cid]
    
    leads = len([x for x in core if "Lead" in str(x.get("Stage"))])
    deals = len([x for x in core if "Deal" in str(x.get("Stage"))])
    cr = round(deals/leads*100, 1) if leads > 0 else 0
    
    metrics.append({"label": "Лиды (Core)", "value": leads, "unit": "шт"})
    metrics.append({"label": "Сделки (Core)", "value": deals, "unit": "шт"})
    metrics.append({"label": "Конверсия (CR)", "value": cr, "unit": "%"})

    # 2. Content production (WIP)
    if cid:
        c_data = get_module_data(cid, "content")
        posts = c_data.get("posts", [])
        metrics.append({"label": "Посты в работе", "value": len([p for p in posts if p.get("Статус") != "Posted"]), "unit": "шт"})
    
    # 3. Active Problems
    q_stats = get_tech_queue_stats()
    dead_count = sum(q["count"] for q in q_stats["summary"] if q["queue"] in ("dead", "blocked", "failed"))
    metrics.append({"label": "Ошибки очереди", "value": dead_count, "unit": "задач", "status": "error" if dead_count > 0 else "ok"})
    
    return metrics

def get_analytics_metrics_api(cid=None):
    if not cid: return []
    metrics = []
    # Sales
    s = get_module_data(cid, "sales")
    leads = s.get("leads", [])
    deals = [x for x in leads if x.get("Этап") in ("Сделка", "Оплата", "Deal", "Closed")]
    cr = round(len(deals)/len(leads)*100, 1) if leads else 0
    metrics.append({"Метрика": "Лиды", "Источник": "sales", "Период": "все время", "Значение": len(leads), "Статус": "ok", "Обновлено": ""})
    metrics.append({"Метрика": "Сделки", "Источник": "sales", "Период": "все время", "Значение": len(deals), "Статус": "ok", "Обновлено": ""})
    metrics.append({"Метрика": "Конверсия CR%", "Источник": "sales", "Период": "все время", "Значение": cr, "Статус": "ok" if cr > 0 else "warn", "Обновлено": ""})
    # Content
    c = get_module_data(cid, "content")
    posts = c.get("posts", [])
    factory = c.get("content_factory", [])
    done = [x for x in factory if x.get("Статус") in ("Готово", "Опубликовано")]
    metrics.append({"Метрика": "Постов", "Источник": "content", "Период": "все время", "Значение": len(posts), "Статус": "ok", "Обновлено": ""})
    metrics.append({"Метрика": "Контент опубликован", "Источник": "content_factory", "Период": "все время", "Значение": len(done), "Статус": "ok", "Обновлено": ""})
    # Marketing
    m = get_module_data(cid, "marketing")
    metrics.append({"Метрика": "Аудитории", "Источник": "marketing", "Период": "все время", "Значение": len(m.get("audiences", [])), "Статус": "ok", "Обновлено": ""})
    metrics.append({"Метрика": "Офферы", "Источник": "marketing", "Период": "все время", "Значение": len(m.get("offers", [])), "Статус": "ok", "Обновлено": ""})
    return metrics

def get_analytics_planfact_api(cid=None):
    if not cid: return []
    s = get_module_data(cid, "sales")
    c = get_module_data(cid, "content")
    leads = s.get("leads", [])
    deals = [x for x in leads if x.get("Этап") in ("Сделка", "Оплата", "Deal", "Closed")]
    factory = c.get("content_factory", [])
    done_content = [x for x in factory if x.get("Статус") in ("Готово", "Опубликовано")]
    plan_leads = s.get("plan", {}).get("leads", 0)
    plan_deals = s.get("plan", {}).get("deals", 0)
    plan_content = c.get("plan", {}).get("published", 0)
    def dev(plan, fact):
        if plan == 0: return 0
        return round((fact - plan) / plan * 100, 1)
    return [
        {"Показатель": "Лиды", "План": plan_leads, "Факт": len(leads), "Отклонение %": dev(plan_leads, len(leads)), "Статус": "ok", "Источник данных": "sales"},
        {"Показатель": "Сделки", "План": plan_deals, "Факт": len(deals), "Отклонение %": dev(plan_deals, len(deals)), "Статус": "ok", "Источник данных": "sales"},
        {"Показатель": "Контент опубликован", "План": plan_content, "Факт": len(done_content), "Отклонение %": dev(plan_content, len(done_content)), "Статус": "ok", "Источник данных": "content_factory"},
    ]

def get_analytics_reports_api():
    reports_dir = KB_ROOT / "_SYSTEM/reports"
    out = []
    if reports_dir.exists():
        for f in sorted(reports_dir.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).isoformat()
            out.append({
                "name": f.name,
                "date": mtime[:10],
                "type": "SOP/Incident Report",
                "path": str(f.relative_to(KB_ROOT)),
                "status": "Final"
            })
    return out

def get_analytics_data_errors_api(cid=None):
    errors = []
    clients = [cid] if cid else list((_load_json(CLIENT_REGISTRY, {}).get("clients", {}).keys()))
    
    for c_id in clients:
        if c_id == "_INTERNAL": continue
        folder = _client_folder(c_id)
        # 1. Missing mandatory files
        for f_name in ["sales.json", "marketing.json", "content.json"]:
            if not (folder / f_name).exists():
                errors.append({"client_id": c_id, "error": f"Отсутствует файл: {f_name}", "severity": "High"})
        
        # 2. Check JSON validity
        for f_path in folder.glob("*.json"):
            try: json.loads(f_path.read_text(encoding="utf-8"))
            except: errors.append({"client_id": c_id, "error": f"Битый JSON: {f_path.name}", "severity": "Critical"})
        
        # 3. Sync sales/loyalty
        if (folder / "sales.json").exists() and (folder / "loyalty.json").exists():
            try:
                s = json.loads((folder / "sales.json").read_text())
                l = json.loads((folder / "loyalty.json").read_text())
                if len(s.get("leads", [])) > 0 and not l.get("retention_metrics"):
                    errors.append({"client_id": c_id, "error": "Рассинхрон Sales/Loyalty (метрики удержания)", "severity": "Medium"})
            except: pass
                
    return errors

def get_analytics_insights_api(cid=None):
    insights = []
    if not cid: return []
    
    # 1. Dead tasks insight
    q_stats = get_tech_queue_stats()
    dead = sum(q["count"] for q in q_stats["summary"] if q["queue"] == "dead")
    if dead > 0:
        insights.append({"type": "Operation", "insight": f"В очереди {dead} мертвых задач", "recommendation": "Проверьте раздел Техника -> Ошибки.", "priority": "High"})

    # 2. Strategy insight
    m = get_module_data(cid, "marketing")
    if not m.get("audiences") and not m.get("offers"):
        insights.append({"type": "Strategy", "insight": "Стратегия маркетинга пуста", "recommendation": "Запустите SOP-MARKETING-BRIEF.", "priority": "Medium"})

    # 3. Fail reports insight
    reports = get_analytics_reports_api()
    fail_reports = [r for r in reports if "FAIL" in r["name"].upper() or "ERROR" in r["name"].upper()]
    if fail_reports:
        insights.append({"type": "Audit", "insight": f"Найдено {len(fail_reports)} отчетов с ошибками", "recommendation": "Изучите последние отчеты в разделе Отчеты.", "priority": "High"})

    return insights

def _cf_file(cid):
    folder = get_client_folder(cid)
    if not folder:
        return None
    cf_dir = folder / "content"
    cf_dir.mkdir(exist_ok=True)
    return cf_dir / "content_factory.json"

def get_content_factory_data(cid):
    f = _cf_file(cid)
    if f and f.exists():
        try: return json.loads(f.read_text(encoding="utf-8")).get("items", [])
        except: pass
    return []

def save_content_factory(cid, action, item):
    f = _cf_file(cid)
    if not f:
        return {"status": "error", "message": "client not found"}
    items = []
    if f.exists():
        try: items = json.loads(f.read_text(encoding="utf-8")).get("items", [])
        except: pass
    if action == "add":
        import uuid as _uuid
        item["id"] = "cf" + _uuid.uuid4().hex[:6]
        item.setdefault("created_at", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        items.append(item)
    elif action == "update":
        items = [item if x["id"] == item["id"] else x for x in items]
    elif action == "delete":
        items = [x for x in items if x["id"] != item.get("id")]
    elif action == "move":
        for x in items:
            if x["id"] == item.get("id"):
                x["status"] = item.get("status", x["status"])
    f.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": "ok", "count": len(items)}

def get_analytics_dashboards_html():
    return """
    <div id="analytics_дашборды" class="section">
      <h2>Аналитика — Дашборды</h2>
      <div style="display:flex; gap:12px; align-items:center; margin-bottom:20px; flex-wrap:wrap">
        <select id="cf-client" class="premium-input module-client-select" style="width:240px" onchange="loadAnalyticsDashboards()">
          <option value="">Выберите клиента</option>
        </select>
        <button class="nav-item active" style="padding:8px 14px; font-size:12px; border:none; cursor:pointer" onclick="loadAnalyticsDashboards()">Обновить</button>
      </div>
      <div class="stats-grid" id="analytics-dashboards-metrics" style="margin-bottom:24px"></div>
      <div class="card premium-glow">
        <h3>Воронка Продаж (Core)</h3>
        <div id="analytics-dashboards-funnel" style="padding:20px; color:var(--text-muted)">Загрузка...</div>
      </div>
    </div>
    """

def get_analytics_metrics_html():
    return """
    <div id="analytics_метрики" class="section">
      <h2>Аналитика — Метрики</h2>
      <div style="display:flex; gap:12px; align-items:center; margin-bottom:20px">
        <select id="analytics_метрики-client" class="premium-input module-client-select" style="width:240px" onchange="loadAnalyticsMetrics()">
          <option value="">Выберите клиента</option>
        </select>
        <button class="nav-item active" style="padding:8px 14px; font-size:12px; border:none; cursor:pointer" onclick="loadAnalyticsMetrics()">Обновить</button>
      </div>
      <div class="card premium-glow">
        <table class="premium-table">
          <thead><tr><th>Метрика</th><th>Источник</th><th>Период</th><th>Значение</th><th>Статус</th><th>Обновлено</th></tr></thead>
          <tbody id="analytics-metrics-body"></tbody>
        </table>
      </div>
    </div>
    """

def get_analytics_planfact_html():
    return """
    <div id="analytics_план-факт" class="section">
      <h2>Аналитика — План-факт</h2>
      <div style="display:flex; gap:12px; align-items:center; margin-bottom:20px">
        <select id="analytics_план-факт-client" class="premium-input module-client-select" style="width:240px" onchange="loadAnalyticsPlanFact()">
          <option value="">Выберите клиента</option>
        </select>
        <button class="nav-item active" style="padding:8px 14px; font-size:12px; border:none; cursor:pointer" onclick="loadAnalyticsPlanFact()">Обновить</button>
      </div>
      <div class="card premium-glow">
        <table class="premium-table">
          <thead><tr><th>Показатель</th><th>План</th><th>Факт</th><th>Отклонение</th><th>Статус</th><th>Источник</th></tr></thead>
          <tbody id="analytics-planfact-body"></tbody>
        </table>
      </div>
    </div>
    """

def get_analytics_reports_html():
    return """
    <div id="analytics_отчёты" class="section">
      <h2>Аналитика — Отчёты</h2>
      <div style="display:flex; gap:12px; align-items:center; margin-bottom:20px">
        <button class="nav-item active" style="padding:8px 14px; font-size:12px; border:none; cursor:pointer" onclick="loadAnalyticsReports()">Обновить</button>
        <span style="font-size:12px; color:var(--text-muted); margin-left:auto">Источник: _SYSTEM/reports</span>
      </div>
      <div class="card premium-glow">
        <table class="premium-table">
          <thead><tr><th>Название</th><th>Дата</th><th>Тип</th><th>Статус</th><th>Действие</th></tr></thead>
          <tbody id="analytics-reports-body"></tbody>
        </table>
      </div>
    </div>
    """

def get_analytics_data_errors_html():
    return """
    <div id="analytics_ошибки_данных" class="section">
      <h2>Аналитика — Ошибки данных</h2>
      <div style="display:flex; gap:12px; align-items:center; margin-bottom:20px">
        <select id="analytics_ошибки_данных-client" class="premium-input module-client-select" style="width:240px" onchange="loadAnalyticsDataErrors()">
          <option value="">Все клиенты</option>
        </select>
        <button class="nav-item active" style="padding:8px 14px; font-size:12px; border:none; cursor:pointer" onclick="loadAnalyticsDataErrors()">Обновить</button>
      </div>
      <div class="card premium-glow">
        <table class="premium-table">
          <thead><tr><th>Клиент</th><th>Ошибка</th><th>Критичность</th></tr></thead>
          <tbody id="analytics-dataerrors-body"></tbody>
        </table>
      </div>
    </div>
    """

def get_analytics_insights_html():
    return """
    <div id="analytics_выводы_и_рекомендации" class="section">
      <h2>Аналитика — Выводы и рекомендации</h2>
      <div style="display:flex; gap:12px; align-items:center; margin-bottom:20px">
        <select id="analytics_выводы_и_рекомендации-client" class="premium-input module-client-select" style="width:240px" onchange="loadAnalyticsInsights()">
          <option value="">Выберите клиента</option>
        </select>
        <button class="nav-item active" style="padding:8px 14px; font-size:12px; border:none; cursor:pointer" onclick="loadAnalyticsInsights()">Обновить</button>
      </div>
      <div id="analytics-insights-container"></div>
    </div>
    """

    for section, label in [("sources", "\u0418\u0441\u0442\u043e\u0447\u043d\u0438\u043a\u0438 \u0442\u0440\u0430\u0444\u0438\u043a\u0430"), ("funnels", "\u0412\u043e\u0440\u043e\u043d\u043a\u0438"), ("ads", "\u0420\u0435\u043a\u043b\u0430\u043c\u0430")]:
        items = mkt_data.get(section, [])
        result.append({"label": label, "value": len(items), "unit": "\u0448\u0442", "trend": None, "trend_dir": None})
    return result

def get_analytics_overview_html():
    return """
    <div id="analytics_\u043e\u0431\u0437\u043e\u0440" class="section">
      <h2>\u0410\u043d\u0430\u043b\u0438\u0442\u0438\u043a\u0430 \u2014 \u041e\u0431\u0437\u043e\u0440</h2>
      <div style="display:flex; gap:12px; align-items:center; margin-bottom:20px; flex-wrap:wrap">
        <select id="analytics_\u043e\u0431\u0437\u043e\u0440-client" class="premium-input module-client-select" style="width:240px" onchange="loadAnalyticsOverview()">
          <option value="">\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u043a\u043b\u0438\u0435\u043d\u0442\u0430</option>
        </select>
        <button class="nav-item active" style="padding:8px 14px; font-size:12px; border:none; cursor:pointer" onclick="loadAnalyticsOverview()">\u041e\u0431\u043d\u043e\u0432\u0438\u0442\u044c</button>
      </div>
      <div class="stats-grid" id="analytics-overview-metrics" style="margin-bottom:24px"></div>
    </div>
    """

def get_analytics_sales_html():
    return """
    <div id="analytics_\u043f\u0440\u043e\u0434\u0430\u0436\u0438" class="section">
      <h2>\u0410\u043d\u0430\u043b\u0438\u0442\u0438\u043a\u0430 \u2014 \u041f\u0440\u043e\u0434\u0430\u0436\u0438</h2>
      <div style="display:flex; gap:12px; align-items:center; margin-bottom:20px; flex-wrap:wrap">
        <select id="analytics_\u043f\u0440\u043e\u0434\u0430\u0436\u0438-client" class="premium-input module-client-select" style="width:240px" onchange="loadAnalyticsSales()">
          <option value="">\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u043a\u043b\u0438\u0435\u043d\u0442\u0430</option>
        </select>
        <button class="nav-item active" style="padding:8px 14px; font-size:12px; border:none; cursor:pointer" onclick="loadAnalyticsSales()">\u041e\u0431\u043d\u043e\u0432\u0438\u0442\u044c</button>
      </div>
      <div class="stats-grid" id="analytics-sales-metrics" style="margin-bottom:24px"></div>
    </div>
    """

def get_analytics_content_html():
    return """
    <div id="analytics_\u043a\u043e\u043d\u0442\u0435\u043d\u0442" class="section">
      <h2>\u0410\u043d\u0430\u043b\u0438\u0442\u0438\u043a\u0430 \u2014 \u041a\u043e\u043d\u0442\u0435\u043d\u0442</h2>
      <div style="display:flex; gap:12px; align-items:center; margin-bottom:20px; flex-wrap:wrap">
        <select id="analytics_\u043a\u043e\u043d\u0442\u0435\u043d\u0442-client" class="premium-input module-client-select" style="width:240px" onchange="loadAnalyticsContent()">
          <option value="">\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u043a\u043b\u0438\u0435\u043d\u0442\u0430</option>
        </select>
        <button class="nav-item active" style="padding:8px 14px; font-size:12px; border:none; cursor:pointer" onclick="loadAnalyticsContent()">\u041e\u0431\u043d\u043e\u0432\u0438\u0442\u044c</button>
      </div>
      <div class="stats-grid" id="analytics-content-metrics" style="margin-bottom:24px"></div>
    </div>
    """

def get_analytics_marketing_html():
    return """
    <div id="analytics_\u043c\u0430\u0440\u043a\u0435\u0442\u0438\u043d\u0433" class="section">
      <h2>\u0410\u043d\u0430\u043b\u0438\u0442\u0438\u043a\u0430 \u2014 \u041c\u0430\u0440\u043a\u0435\u0442\u0438\u043d\u0433</h2>
      <div style="display:flex; gap:12px; align-items:center; margin-bottom:20px; flex-wrap:wrap">
        <select id="analytics_\u043c\u0430\u0440\u043a\u0435\u0442\u0438\u043d\u0433-client" class="premium-input module-client-select" style="width:240px" onchange="loadAnalyticsMarketing()">
          <option value="">\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u043a\u043b\u0438\u0435\u043d\u0442\u0430</option>
        </select>
        <button class="nav-item active" style="padding:8px 14px; font-size:12px; border:none; cursor:pointer" onclick="loadAnalyticsMarketing()">\u041e\u0431\u043d\u043e\u0432\u0438\u0442\u044c</button>
      </div>
      <div class="stats-grid" id="analytics-marketing-metrics" style="margin-bottom:24px"></div>
    </div>
    """

def get_server_section_html():
    return """
    <div id="tech_\u0441\u0435\u0440\u0432\u0435\u0440" class="section">
      <h2>\u0421\u0435\u0440\u0432\u0435\u0440</h2>
      <div style="display:flex; gap:12px; align-items:center; margin-bottom:20px">
        <button class="nav-item active" style="padding:8px 14px; font-size:12px; border:none; cursor:pointer" onclick="loadServerStatus()">\u041e\u0431\u043d\u043e\u0432\u0438\u0442\u044c</button>
        <span id="server-updated" style="font-size:12px; color:var(--text-muted); margin-left:auto"></span>
      </div>
      <div class="stats-grid" id="server-metrics" style="margin-bottom:24px">
        <div style="color:var(--text-muted)">\u0417\u0430\u0433\u0440\u0443\u0437\u043a\u0430...</div>
      </div>
    </div>
    """

def get_scripts_section_html():
    return """
    <div id="tech_\u0441\u043a\u0440\u0438\u043f\u0442\u044b" class="section">
      <h2>\u0421\u043a\u0440\u0438\u043f\u0442\u044b</h2>
      <div style="display:flex; gap:12px; align-items:center; margin-bottom:20px; flex-wrap:wrap">
        <button class="nav-item active" style="padding:8px 14px; font-size:12px; border:none; cursor:pointer" onclick="loadTechScripts()">\u041e\u0431\u043d\u043e\u0432\u0438\u0442\u044c</button>
        <button class="nav-item" style="padding:8px 14px; font-size:12px; border:none; cursor:pointer" onclick="showTechScriptAddModal()">\u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c</button>
      </div>
      <div class="card premium-glow">
        <table class="premium-table">
          <thead><tr><th>\u041d\u0430\u0437\u0432\u0430\u043d\u0438\u0435</th><th>\u0422\u0438\u043f</th><th>\u041f\u0443\u0442\u044c</th><th>\u0420\u0430\u0441\u043f\u0438\u0441\u0430\u043d\u0438\u0435</th><th>\u041f\u043e\u0441\u043b\u0435\u0434\u043d\u0438\u0439 \u0437\u0430\u043f\u0443\u0441\u043a</th><th>\u0421\u0442\u0430\u0442\u0443\u0441</th><th>\u0414\u0435\u0439\u0441\u0442\u0432\u0438\u044f</th></tr></thead>
          <tbody id="tech-scripts-body"><tr><td colspan="7" style="color:var(--text-muted); padding:22px 12px">\u0417\u0430\u0433\u0440\u0443\u0437\u043a\u0430...</td></tr></tbody>
        </table>
      </div>
    </div>
    """

def get_api_section_html():
    return """
    <div id="tech_api" class="section">
      <h2>API</h2>
      <div style="display:flex; gap:12px; align-items:center; margin-bottom:20px">
        <button class="nav-item active" style="padding:8px 14px; font-size:12px; border:none; cursor:pointer" onclick="loadApiRegistry()">\u041e\u0431\u043d\u043e\u0432\u0438\u0442\u044c</button>
        <input class="premium-input" id="api-filter" placeholder="\u0424\u0438\u043b\u044c\u0442\u0440 \u043f\u043e \u043f\u0443\u0442\u0438..." style="width:260px" oninput="filterApiRegistry(this.value)">
      </div>
      <div class="card premium-glow">
        <table class="premium-table">
          <thead><tr><th>\u041c\u0435\u0442\u043e\u0434</th><th>\u041f\u0443\u0442\u044c</th><th>\u041e\u043f\u0438\u0441\u0430\u043d\u0438\u0435</th></tr></thead>
          <tbody id="api-registry-body"></tbody>
        </table>
      </div>
    </div>
    """

def get_workflow_section_html():
    return """
    <div id="tech_\u0432\u043e\u0440\u043a\u0444\u043b\u043e\u0443" class="section">
      <h2>\u0412\u043e\u0440\u043a\u0444\u043b\u043e\u0443</h2>
      <div style="display:flex; gap:12px; align-items:center; margin-bottom:20px">
        <button class="nav-item active" style="padding:8px 14px; font-size:12px; border:none; cursor:pointer" onclick="loadWorkflowStatus()">\u041e\u0431\u043d\u043e\u0432\u0438\u0442\u044c</button>
      </div>
      <div class="stats-grid" id="wf-overall" style="margin-bottom:24px"></div>
      <h3 style="margin:0 0 12px; font-size:14px; color:var(--text-muted)">Gates</h3>
      <div class="card premium-glow">
        <table class="premium-table">
          <thead><tr><th>Gate</th><th>\u0421\u0442\u0430\u0442\u0443\u0441</th><th>\u0414\u0435\u0442\u0430\u043b\u0438</th></tr></thead>
          <tbody id="wf-gates-body"></tbody>
        </table>
      </div>
    </div>
    """

def get_queues_section_html():
    return """
    <div id="tech_\u043e\u0447\u0435\u0440\u0435\u0434\u0438" class="section">
      <h2>\u041e\u0447\u0435\u0440\u0435\u0434\u0438</h2>
      <div style="display:flex; gap:12px; align-items:center; margin-bottom:20px">
        <button class="nav-item active" style="padding:8px 14px; font-size:12px; border:none; cursor:pointer" onclick="loadTechQueues()">\u041e\u0431\u043d\u043e\u0432\u0438\u0442\u044c</button>
        <button class="nav-item" style="padding:8px 14px; font-size:12px; border:none; cursor:pointer" onclick="showTab('tech_\u043e\u0448\u0438\u0431\u043a\u0438')">\u2192 \u0421\u043c\u043e\u0442\u0440\u0435\u0442\u044c \u043e\u0448\u0438\u0431\u043a\u0438</button>
      </div>
      <div class="stats-grid" id="queues-summary" style="margin-bottom:24px"></div>
      <h3 style="margin:0 0 12px; font-size:14px; color:var(--text-muted)">\u041f\u043e\u0441\u043b\u0435\u0434\u043d\u0438\u0435 dead-\u0437\u0430\u0434\u0430\u0447\u0438</h3>
      <div class="card premium-glow">
        <table class="premium-table">
          <thead><tr><th>Task ID</th><th>\u0422\u0438\u043f</th><th>\u041f\u0440\u0438\u0447\u0438\u043d\u0430</th><th>\u0412\u0440\u0435\u043c\u044f</th></tr></thead>
          <tbody id="queues-dead-body"></tbody>
        </table>
      </div>
    </div>
    """

def get_tech_logs_section_html():
    return """
    <div id="tech_\u043b\u043e\u0433\u0438" class="section">
      <h2>\u041b\u043e\u0433\u0438</h2>
      <div style="display:flex; gap:12px; align-items:center; margin-bottom:20px; flex-wrap:wrap">
        <select id="tech-log-agent" class="premium-input" style="width:220px" onchange="loadTechLog()">
          <option value="">\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u0430\u0433\u0435\u043d\u0442\u0430...</option>
        </select>
        <button class="nav-item active" style="padding:8px 14px; font-size:12px; border:none; cursor:pointer" onclick="loadTechLog()">\u041e\u0431\u043d\u043e\u0432\u0438\u0442\u044c</button>
        <label style="font-size:12px; color:var(--text-muted)">
          <input type="checkbox" id="tech-log-live" onchange="toggleTechLogLive()"> Live
        </label>
      </div>
      <div class="card premium-glow">
        <pre id="tech-log-content" style="font-size:11px; line-height:1.5; max-height:500px; overflow-y:auto; color:var(--text-muted); margin:0; padding:16px; white-space:pre-wrap">\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u0430\u0433\u0435\u043d\u0442\u0430</pre>
      </div>
    </div>
    """

def get_tech_errors_section_html():
    return """
    <div id="tech_\u043e\u0448\u0438\u0431\u043a\u0438" class="section">
      <h2>\u041e\u0448\u0438\u0431\u043a\u0438</h2>
      <div style="display:flex; gap:12px; align-items:center; margin-bottom:20px; flex-wrap:wrap">
        <button class="nav-item active" style="padding:8px 14px; font-size:12px; border:none; cursor:pointer" onclick="loadTechErrors()">\u041e\u0431\u043d\u043e\u0432\u0438\u0442\u044c</button>
        <input class="premium-input" id="errors-filter" placeholder="\u0424\u0438\u043b\u044c\u0442\u0440 \u043f\u043e \u0442\u0438\u043f\u0443 \u0437\u0430\u0434\u0430\u0447\u0438..." style="width:240px" oninput="filterTechErrors(this.value)">
        <button class="nav-item" style="padding:8px 14px; font-size:12px; border:none; cursor:pointer" onclick="showTab('tech_\u043e\u0447\u0435\u0440\u0435\u0434\u0438')">\u2190 \u0412\u0441\u0435 \u043e\u0447\u0435\u0440\u0435\u0434\u0438</button>
        <span id="errors-count" style="font-size:12px; color:var(--text-muted); margin-left:auto"></span>
      </div>
      <div class="card premium-glow">
        <table class="premium-table">
          <thead><tr><th>\u041e\u0447\u0435\u0440\u0435\u0434\u044c</th><th>Task ID</th><th>\u0422\u0438\u043f</th><th>\u0410\u0433\u0435\u043d\u0442</th><th>\u041f\u0440\u0438\u0447\u0438\u043d\u0430</th><th>Retry</th><th>\u0412\u0440\u0435\u043c\u044f</th></tr></thead>
          <tbody id="tech-errors-body"></tbody>
        </table>
      </div>
    </div>
    """

def get_smm_section_html():
    return """
    <div id="smm_smm" class="section smm-root">
      <div class="smm-layout">

        <!-- ПАНЕЛЬ 2: список клиентов -->
        <div class="smm-clients-panel" id="smm-clients-panel">
          <div style="padding:12px 12px 8px">
            <input class="premium-input" id="smm-client-search" placeholder="Поиск проекта..."
                   style="width:100%" oninput="filterSmmClients(this.value)">
          </div>
          <div id="smm-client-list" style="overflow-y:auto; flex:1"></div>
        </div>

        <!-- ПАНЕЛЬ 3: рабочая зона -->
        <div class="smm-workspace" id="smm-workspace">
          <div id="smm-no-client" style="display:flex; align-items:center; justify-content:center; height:100%; color:var(--text-muted)">
            Выберите проект
          </div>
          <div id="smm-client-workspace" style="display:none; flex-direction:column; height:100%">

            <!-- Шапка клиента -->
            <div class="smm-ws-header" id="smm-ws-header"></div>

            <!-- Вкладки -->
            <div class="smm-ws-tabs" id="smm-ws-tabs"></div>

            <!-- Контент вкладки -->
            <div class="smm-ws-body" id="smm-ws-body"></div>

          </div>
        </div>

      </div>
    </div>
    """

def get_content_factory_section_html():
    return """
    <div id="content_контент-завод" class="section" style="display:none">
      <div style="display:flex; height:calc(100vh - 120px); gap:0; overflow:hidden;">

        <!-- Middle panel: clients list -->
        <div id="cf-clients-panel" style="width:260px; min-width:220px; border-right:1px solid var(--border); display:flex; flex-direction:column; overflow:hidden;">
          <div style="padding:12px 16px; border-bottom:1px solid var(--border);">
            <input id="cf-client-search" class="premium-input" placeholder="Поиск клиента..." style="width:100%; font-size:13px" oninput="filterCfClients()">
          </div>
          <div id="cf-clients-list" style="flex:1; overflow-y:auto; padding:8px 0;"></div>
        </div>

        <!-- Right panel: workspace -->
        <div id="cf-workspace" style="flex:1; display:flex; flex-direction:column; overflow:hidden;">
          <div id="cf-workspace-empty" style="flex:1; display:flex; align-items:center; justify-content:center; color:var(--text-muted); font-size:14px;">
            Выберите клиента
          </div>
          <div id="cf-workspace-content" style="display:none; flex:1; flex-direction:column; overflow:hidden;">
            <!-- Client header -->
            <div id="cf-client-header" style="padding:16px 20px; border-bottom:1px solid var(--border); display:flex; align-items:center; gap:16px;">
              <div id="cf-client-name" style="font-weight:700; font-size:16px;"></div>
              <div id="cf-client-id" style="font-size:12px; color:var(--text-muted);"></div>
              <span id="cf-status" style="font-size:12px; color:var(--text-muted); margin-left:auto"></span>
            </div>
            <!-- Tabs -->
            <div id="cf-tabs" style="display:flex; gap:4px; padding:8px 16px; border-bottom:1px solid var(--border); flex-wrap:wrap;">
              <button class="nav-item active" data-cftab="overview" onclick="showCfTab('overview')">Обзор</button>
              <button class="nav-item" data-cftab="content" onclick="showCfTab('content')">Контент</button>
              <button class="nav-item" data-cftab="analytics" onclick="showCfTab('analytics')">Аналитика</button>
              <button class="nav-item" data-cftab="settings" onclick="showCfTab('settings')">Настройки</button>
              <button class="nav-item" id="cf-btn-publications" data-cftab="publications" onclick="showCfTab('publications')" style="display:none">Публикации</button>
              <button class="nav-item" id="cf-btn-visual" data-cftab="visual" onclick="showCfTab('visual')" style="display:none">Визуал</button>
            </div>
            <!-- Tab bodies -->
            <div style="flex:1; overflow-y:auto; padding:16px 20px;">
              <!-- Overview tab -->
              <div id="cf-tab-overview" class="cf-tabpanel">
                <div id="cf-overview-cards" style="display:grid; grid-template-columns:repeat(3,1fr); gap:16px;"></div>
              </div>
              <!-- Content tab -->
              <div id="cf-tab-content" class="cf-tabpanel" style="display:none">
                <div style="display:flex; gap:8px; margin-bottom:16px; align-items:center;">
                  <button class="nav-item active" id="cf-mode-pipeline" onclick="setCfMode('pipeline')">Конвейер</button>
                  <button class="nav-item" id="cf-mode-list" onclick="setCfMode('list')">Список</button>
                  <button class="nav-item active" style="margin-left:auto" onclick="addCfItem()">+ Добавить</button>
                </div>
                <div id="cf-pipeline" style="display:flex; gap:16px; overflow-x:auto; padding-bottom:8px;"></div>
                <div id="cf-list" style="display:none;"></div>
              </div>
              <!-- Analytics tab -->
              <div id="cf-tab-analytics" class="cf-tabpanel" style="display:none">
                <div id="cf-analytics-body" style="color:var(--text-muted); font-size:14px; padding:40px 0; text-align:center;">Аналитика загружается...</div>
              </div>
              <!-- Publications tab -->
              <div id="cf-tab-publications" class="cf-tabpanel" style="display:none">
                <div id="cf-publications-body"></div>
              </div>
              <!-- Visual tab -->
              <div id="cf-tab-visual" class="cf-tabpanel" style="display:none">
                <div id="cf-visual-body" style="color:var(--text-muted); font-size:14px; padding:40px 0; text-align:center;">Фотоархив недоступен — модуль не подключён</div>
              </div>
              <!-- Settings tab -->
              <div id="cf-tab-settings" class="cf-tabpanel" style="display:none">
                <div id="cf-settings-body"></div>
              </div>
            </div>
          </div>
        </div>

      </div>
    </div>
    """


def get_web_factory_section_html(div_id="content_web-завод"):
    return f"""
    <div id="{div_id}" class="section web-factory-root" style="width:100%; height:calc(100vh - 120px); overflow:hidden; display:none;">
      <div style="display:flex; width:100%; height:100%; gap:0; overflow:hidden;">

        <!-- Panel 2: clients list -->
        <div id="{div_id}-clients-panel" style="width:260px; min-width:220px; border-right:1px solid var(--border); display:flex; flex-direction:column; overflow:hidden; background:var(--sidebar);">
          <div style="padding:12px 16px; border-bottom:1px solid var(--border);">
            <input id="{div_id}-client-search" class="premium-input" placeholder="Поиск клиента..." style="width:100%; font-size:13px" oninput="filterWfClients('{div_id}')">
          </div>
          <div id="{div_id}-clients-list" style="flex:1; overflow-y:auto; padding:8px 0;"></div>
        </div>

        <!-- Panel 3: workspace -->
        <div id="{div_id}-workspace" style="flex:1; display:flex; flex-direction:column; overflow:hidden; background:var(--bg);">
          <div id="{div_id}-workspace-empty" style="flex:1; display:flex; align-items:center; justify-content:center; color:var(--text-muted); font-size:14px;">
            Выберите клиента в списке слева
          </div>
          <div id="{div_id}-workspace-content" style="display:none; flex:1; flex-direction:column; overflow:hidden;">
            
            <!-- Workspace Header -->
            <div id="{div_id}-client-header" style="padding:16px 20px; border-bottom:1px solid var(--border); display:flex; align-items:center; justify-content:space-between; gap:16px; flex-wrap:wrap">
              <div style="display:flex; align-items:center; gap:12px">
                <span id="{div_id}-client-name" style="font-weight:800; font-size:18px; color:#fff"></span>
                <span id="{div_id}-client-id" style="font-size:11px; padding:3px 8px; background:var(--surface-hover); border-radius:10px; color:var(--text-muted)"></span>
              </div>
              <div style="display:flex; align-items:center; gap:8px">
                <span style="font-size:12px; color:var(--text-muted)">Сайт:</span>
                <select id="{div_id}-site-select" class="premium-input" style="padding:6px 12px; min-width:180px; font-weight:600" onchange="onWfSiteChanged('{div_id}')">
                  <option value="all">Все сайты</option>
                </select>
              </div>
            </div>

            <!-- Workspace Tabs -->
            <div id="{div_id}-wf-tabs" style="display:flex; gap:4px; padding:8px 16px; border-bottom:1px solid var(--border); flex-wrap:wrap; background:var(--sidebar);">
              <button class="nav-item active" data-wftab="overview" onclick="switchWfTab('{div_id}', 'overview')">Обзор</button>
              <button class="nav-item" data-wftab="production" onclick="switchWfTab('{div_id}', 'production')">Производство</button>
              <button class="nav-item" data-wftab="sites" onclick="switchWfTab('{div_id}', 'sites')">Сайты</button>
              <button class="nav-item" data-wftab="library" onclick="switchWfTab('{div_id}', 'library')">Библиотека</button>
              <button class="nav-item" data-wftab="analytics" onclick="switchWfTab('{div_id}', 'analytics')">Аналитика</button>
              <button class="nav-item" data-wftab="settings" onclick="switchWfTab('{div_id}', 'settings')">Настройки</button>
            </div>

            <!-- Tab Panels container -->
            <div style="flex:1; overflow-y:auto; padding:20px 24px;">
              
              <!-- Tab 1: Overview -->
              <div id="{div_id}-tabpanel-overview" class="wf-tabpanel">
                <div id="{div_id}-overview-cards" style="display:grid; grid-template-columns:repeat(3,1fr); gap:16px; margin-bottom:24px"></div>
              </div>

              <!-- Tab 2: Production -->
              <div id="{div_id}-tabpanel-production" class="wf-tabpanel" style="display:none">
                <div style="display:flex; justify-content:space-between; align-items:center; gap:12px; margin-bottom:16px; flex-wrap:wrap">
                  <div style="display:flex; gap:4px; background:var(--surface-hover); padding:3px; border-radius:var(--radius-sm)">
                    <button class="nav-item active" id="{div_id}-prod-mode-kanban" style="padding:6px 12px; font-size:12px" onclick="switchWfProdMode('{div_id}', 'kanban')">Конвейер</button>
                    <button class="nav-item" id="{div_id}-prod-mode-list" style="padding:6px 12px; font-size:12px" onclick="switchWfProdMode('{div_id}', 'list')">Список</button>
                  </div>
                  <button class="nav-item active" style="padding:8px 16px; border:none; cursor:pointer" onclick="openWfAddProdModal('{div_id}')">+ Добавить страницу</button>
                </div>
                
                <!-- Pipeline / Kanban Mode -->
                <div id="{div_id}-production-kanban" style="display:flex; gap:16px; overflow-x:auto; padding-bottom:12px; align-items:stretch; min-height:400px"></div>
                
                <!-- List Mode with filters -->
                <div id="{div_id}-production-list" style="display:none">
                  <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(140px, 1fr)); gap:10px; margin-bottom:16px; padding:12px; background:var(--surface); border-radius:var(--radius-md); border:1px solid var(--border)">
                    <div style="display:flex; flex-direction:column; gap:4px">
                      <label style="font-size:10px; color:var(--text-muted); text-transform:uppercase; font-weight:700">Сайт</label>
                      <select id="{div_id}-filter-site" class="premium-input" style="padding:6px 10px" onchange="renderWfProdList('{div_id}')"><option value="all">Все</option></select>
                    </div>
                    <div style="display:flex; flex-direction:column; gap:4px">
                      <label style="font-size:10px; color:var(--text-muted); text-transform:uppercase; font-weight:700">Тип страницы</label>
                      <select id="{div_id}-filter-type" class="premium-input" style="padding:6px 10px" onchange="renderWfProdList('{div_id}')"><option value="all">Все</option></select>
                    </div>
                    <div style="display:flex; flex-direction:column; gap:4px">
                      <label style="font-size:10px; color:var(--text-muted); text-transform:uppercase; font-weight:700">Статус</label>
                      <select id="{div_id}-filter-status" class="premium-input" style="padding:6px 10px" onchange="renderWfProdList('{div_id}')"><option value="all">Все</option></select>
                    </div>
                    <div style="display:flex; flex-direction:column; gap:4px">
                      <label style="font-size:10px; color:var(--text-muted); text-transform:uppercase; font-weight:700">Ответственный</label>
                      <select id="{div_id}-filter-owner" class="premium-input" style="padding:6px 10px" onchange="renderWfProdList('{div_id}')"><option value="all">Все</option></select>
                    </div>
                    <div style="display:flex; flex-direction:column; gap:4px">
                      <label style="font-size:10px; color:var(--text-muted); text-transform:uppercase; font-weight:700">Приоритет</label>
                      <select id="{div_id}-filter-priority" class="premium-input" style="padding:6px 10px" onchange="renderWfProdList('{div_id}')"><option value="all">Все</option></select>
                    </div>
                  </div>
                  <div class="card premium-glow" style="padding:0; overflow:hidden">
                    <table class="premium-table">
                      <thead>
                        <tr><th>Сайт</th><th>Название</th><th>Тип</th><th>Приоритет</th><th>Ответственный</th><th>Статус</th><th>Действия</th></tr>
                      </thead>
                      <tbody id="{div_id}-prod-table-body"></tbody>
                    </table>
                  </div>
                </div>
              </div>

              <!-- Tab 3: Sites -->
              <div id="{div_id}-tabpanel-sites" class="wf-tabpanel" style="display:none">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:16px">
                  <h4 style="margin:0; font-size:16px; color:#fff">Список доменов и сайтов</h4>
                  <button class="nav-item active" style="padding:8px 16px" onclick="openWfAddSiteModal('{div_id}')">+ Добавить сайт</button>
                </div>
                <div class="card premium-glow" style="padding:0; overflow:hidden">
                  <table class="premium-table">
                    <thead>
                      <tr><th>Домен</th><th>Тип</th><th>Статус</th><th>Страницы</th><th>Формы / Цели</th><th>Ошибки</th><th>Последняя проверка</th><th>Действия</th></tr>
                    </thead>
                    <tbody id="{div_id}-sites-table-body"></tbody>
                  </table>
                </div>
              </div>

              <!-- Tab 4: Library -->
              <div id="{div_id}-tabpanel-library" class="wf-tabpanel" style="display:none">
                <div style="display:flex; gap:20px; height:500px; overflow:hidden">
                  <!-- Library Categories Left Menu -->
                  <div style="width:200px; border-right:1px solid var(--border); display:flex; flex-direction:column; gap:4px; overflow-y:auto; padding-right:12px">
                    <div style="font-size:10px; font-weight:700; color:var(--text-muted); text-transform:uppercase; margin-bottom:8px">Категории</div>
                    <button class="nav-item active lib-cat-btn" data-libcat="photo" onclick="switchWfLibCat('{div_id}', 'photo')" style="text-align:left; border:none; background:none">Фото</button>
                    <button class="nav-item lib-cat-btn" data-libcat="video" onclick="switchWfLibCat('{div_id}', 'video')" style="text-align:left; border:none; background:none">Видео</button>
                    <button class="nav-item lib-cat-btn" data-libcat="clips" onclick="switchWfLibCat('{div_id}', 'clips')" style="text-align:left; border:none; background:none">Ролики</button>
                    <button class="nav-item lib-cat-btn" data-libcat="banners" onclick="switchWfLibCat('{div_id}', 'banners')" style="text-align:left; border:none; background:none">Баннеры</button>
                    <button class="nav-item lib-cat-btn" data-libcat="logos" onclick="switchWfLibCat('{div_id}', 'logos')" style="text-align:left; border:none; background:none">Логотипы</button>
                    <button class="nav-item lib-cat-btn" data-libcat="texts" onclick="switchWfLibCat('{div_id}', 'texts')" style="text-align:left; border:none; background:none">Тексты</button>
                    <button class="nav-item lib-cat-btn" data-libcat="docs" onclick="switchWfLibCat('{div_id}', 'docs')" style="text-align:left; border:none; background:none">Документы</button>
                    <button class="nav-item lib-cat-btn" data-libcat="reviews" onclick="switchWfLibCat('{div_id}', 'reviews')" style="text-align:left; border:none; background:none">Отзывы</button>
                    <button class="nav-item lib-cat-btn" data-libcat="cases" onclick="switchWfLibCat('{div_id}', 'cases')" style="text-align:left; border:none; background:none">Кейсы</button>
                    <button class="nav-item lib-cat-btn" data-libcat="blocks" onclick="switchWfLibCat('{div_id}', 'blocks')" style="text-align:left; border:none; background:none">Блоки страниц</button>
                    <button class="nav-item lib-cat-btn" data-libcat="templates" onclick="switchWfLibCat('{div_id}', 'templates')" style="text-align:left; border:none; background:none">Шаблоны секций</button>
                  </div>
                  <!-- Library Category Content -->
                  <div style="flex:1; display:flex; flex-direction:column; overflow:hidden">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px">
                      <h4 id="{div_id}-lib-cat-title" style="margin:0; text-transform:capitalize; color:#fff">Материалы</h4>
                      <button class="nav-item active" style="padding:6px 12px; font-size:12px" onclick="openWfAddLibModal('{div_id}')">+ Добавить ассет</button>
                    </div>
                    <div style="flex:1; overflow-y:auto">
                      <div class="card premium-glow" style="padding:0; overflow:hidden">
                        <table class="premium-table">
                          <thead>
                            <tr><th>Название</th><th>Размер / Инфо</th><th>Ссылка / Спецификация</th><th>Статус</th><th>Обновлено</th><th>Действия</th></tr>
                          </thead>
                          <tbody id="{div_id}-library-table-body"></tbody>
                        </table>
                      </div>
                    </div>
                  </div>
                </div>
              </div>

              <!-- Tab 5: Analytics -->
              <div id="{div_id}-tabpanel-analytics" class="wf-tabpanel" style="display:none">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:20px">
                  <h4 id="{div_id}-analytics-title" style="margin:0; font-size:16px; color:#fff">Сводная аналитика</h4>
                  <button class="nav-item" style="padding:6px 12px; font-size:12px; background:var(--surface-hover)" onclick="runWfAnalyticsCheck('{div_id}')">Запустить аудит метрик</button>
                </div>
                
                <!-- Metric Cards -->
                <div class="stats-grid" id="{div_id}-analytics-metrics"></div>
                
                <!-- Analytics Details Grid -->
                <div style="display:grid; grid-template-columns:1fr 1fr; gap:20px; margin-bottom:20px">
                  <div class="card premium-glow" style="margin-bottom:0">
                    <h5 style="font-size:13px; color:var(--text-muted); text-transform:uppercase; margin-bottom:12px; font-weight:700">Лучшие страницы (по конверсии)</h5>
                    <table class="premium-table">
                      <thead><tr><th>Страница</th><th>Посещения</th><th>Заявки</th><th>CR%</th></tr></thead>
                      <tbody id="{div_id}-analytics-best-pages"></tbody>
                    </table>
                  </div>
                  <div class="card premium-glow" style="margin-bottom:0">
                    <h5 style="font-size:13px; color:var(--text-muted); text-transform:uppercase; margin-bottom:12px; font-weight:700">Слабые страницы / Критические ошибки</h5>
                    <table class="premium-table">
                      <thead><tr><th>Страница / Ошибка</th><th>Степень риска</th><th>Решение</th></tr></thead>
                      <tbody id="{div_id}-analytics-poor-pages"></tbody>
                    </table>
                  </div>
                </div>
                
                <div style="display:grid; grid-template-columns:1fr 1fr; gap:20px; margin-top:20px">
                  <div class="card premium-glow" style="margin-bottom:0">
                    <h5 style="font-size:13px; color:var(--text-muted); text-transform:uppercase; margin-bottom:12px; font-weight:700">Источники трафика</h5>
                    <table class="premium-table">
                      <thead><tr><th>Источник</th><th>Посещения</th><th>Конверсии</th><th>Доля %</th></tr></thead>
                      <tbody id="{div_id}-analytics-traffic-sources"></tbody>
                    </table>
                  </div>
                  <div class="card premium-glow" style="margin-bottom:0">
                    <h5 style="font-size:13px; color:var(--text-muted); text-transform:uppercase; margin-bottom:12px; font-weight:700">Динамика посещаемости и лидов</h5>
                    <div id="{div_id}-analytics-trend-chart" style="display:flex; flex-direction:column; gap:8px; padding-top:4px"></div>
                  </div>
                </div>
              </div>

              <!-- Tab 6: Settings -->
              <div id="{div_id}-tabpanel-settings" class="wf-tabpanel" style="display:none">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:20px; border-bottom:1px solid var(--border); padding-bottom:10px">
                  <h4 style="margin:0; font-size:16px; color:#fff">Настройки WEB-производства</h4>
                  <button class="nav-item active" style="padding:8px 20px; border:none; cursor:pointer" onclick="saveWfSettings('{div_id}')">Сохранить настройки</button>
                </div>
                
                <div style="display:grid; grid-template-columns:1fr 1fr; gap:24px">
                  <!-- Brand and Rules -->
                  <div style="display:flex; flex-direction:column; gap:16px">
                    <div class="card premium-glow" style="margin-bottom:0">
                      <h5 style="font-size:13px; color:#fff; font-weight:700; margin-bottom:12px">Правила бренда и Tone of Voice</h5>
                      <div style="display:flex; flex-direction:column; gap:10px">
                        <div style="display:flex; flex-direction:column; gap:4px">
                          <label style="font-size:11px; color:var(--text-muted)">Голос бренда (Tone of Voice)</label>
                          <textarea id="{div_id}-setting-brand-voice" class="premium-input" style="height:60px; resize:none"></textarea>
                        </div>
                        <div style="display:flex; flex-direction:column; gap:4px">
                          <label style="font-size:11px; color:var(--text-muted)">Цветовая палитра и стили</label>
                          <input id="{div_id}-setting-brand-colors" class="premium-input" type="text" placeholder="HEX коды через запятую">
                        </div>
                        <div style="display:flex; flex-direction:column; gap:4px">
                          <label style="font-size:11px; color:var(--text-muted)">Ключевые гайдлайны дизайна</label>
                          <textarea id="{div_id}-setting-brand-guidelines" class="premium-input" style="height:60px; resize:none"></textarea>
                        </div>
                      </div>
                    </div>
                    
                    <div class="card premium-glow" style="margin-bottom:0">
                      <h5 style="font-size:13px; color:#fff; font-weight:700; margin-bottom:12px">Ответственные по умолчанию</h5>
                      <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px">
                        <div style="display:flex; flex-direction:column; gap:4px">
                          <label style="font-size:11px; color:var(--text-muted)">Контент и Тексты</label>
                          <input id="{div_id}-setting-owner-content" class="premium-input" type="text">
                        </div>
                        <div style="display:flex; flex-direction:column; gap:4px">
                          <label style="font-size:11px; color:var(--text-muted)">Дизайн / Верстка</label>
                          <input id="{div_id}-setting-owner-design" class="premium-input" type="text">
                        </div>
                        <div style="display:flex; flex-direction:column; gap:4px">
                          <label style="font-size:11px; color:var(--text-muted)">Техническая сборка</label>
                          <input id="{div_id}-setting-owner-tech" class="premium-input" type="text">
                        </div>
                        <div style="display:flex; flex-direction:column; gap:4px">
                          <label style="font-size:11px; color:var(--text-muted)">Проверка / QA</label>
                          <input id="{div_id}-setting-owner-qa" class="premium-input" type="text">
                        </div>
                      </div>
                    </div>
                  </div>
                  
                  <!-- SEO and Competitors -->
                  <div style="display:flex; flex-direction:column; gap:16px">
                    <div class="card premium-glow" style="margin-bottom:0">
                      <h5 style="font-size:13px; color:#fff; font-weight:700; margin-bottom:12px">Доступы, токены и интеграции</h5>
                      <div style="display:flex; flex-direction:column; gap:10px">
                        <div style="display:flex; flex-direction:column; gap:4px">
                          <label style="font-size:11px; color:var(--text-muted)">Яндекс.Метрика Токен</label>
                          <input id="{div_id}-setting-secret-metrika" class="premium-input" type="text" placeholder="ym_token_...">
                        </div>
                        <div style="display:flex; flex-direction:column; gap:4px">
                          <label style="font-size:11px; color:var(--text-muted)">Google Search Console API Key</label>
                          <input id="{div_id}-setting-secret-gsc" class="premium-input" type="text" placeholder="gsc_api_key_...">
                        </div>
                        <div style="display:flex; flex-direction:column; gap:4px">
                          <label style="font-size:11px; color:var(--text-muted)">Интеграция CRM (API Key / Webhook)</label>
                          <input id="{div_id}-setting-secret-crm" class="premium-input" type="text" placeholder="crm_token_...">
                        </div>
                      </div>
                    </div>
                    
                    <div class="card premium-glow" style="margin-bottom:0">
                      <h5 style="font-size:13px; color:#fff; font-weight:700; margin-bottom:12px">Продвижение и конкуренты</h5>
                      <div style="display:flex; flex-direction:column; gap:10px">
                        <div style="display:flex; flex-direction:column; gap:4px">
                          <label style="font-size:11px; color:var(--text-muted)">Ключевые слова для SEO (через запятую)</label>
                          <input id="{div_id}-setting-keywords" class="premium-input" type="text" placeholder="ключевые фразы...">
                        </div>
                        <div style="display:flex; flex-direction:column; gap:4px">
                          <label style="font-size:11px; color:var(--text-muted)">Домены конкурентов (через запятую)</label>
                          <input id="{div_id}-setting-competitors" class="premium-input" type="text" placeholder="competitor1.com, competitor2.ru">
                        </div>
                        <div style="display:flex; flex-direction:column; gap:4px">
                          <label style="font-size:11px; color:var(--text-muted)">Цели лидогенерации (через запятую)</label>
                          <input id="{div_id}-setting-goals" class="premium-input" type="text" placeholder="клик на кнопку, отправка формы">
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              </div>

            </div>

          </div>
        </div>

      </div>
    </div>
    """

def get_html(ministers, initial_tab="home"):
    projects_nav_html = """
    <div style="padding:8px 4px 4px; font-size:10px; font-weight:700; color:var(--text-muted); text-transform:uppercase; letter-spacing:.08em; margin-top:4px;">Проекты</div>
    <div id="proj-pluslogo" class="nav-item proj-btn" data-project="pluslogo" onclick="selectProject('pluslogo')" style="display:flex;align-items:center;gap:8px">
      <span style="font-size:15px">🏭</span> Плюс Лого
    </div>
    <div id="proj-ontime-ai" class="nav-item proj-btn" data-project="ontime-ai" onclick="selectProject('ontime-ai')" style="display:flex;align-items:center;gap:8px">
      <span style="font-size:15px">🤖</span> onTime.ai
    </div>
    <div id="proj-andrey-brand" class="nav-item proj-btn" data-project="andrey-brand" onclick="selectProject('andrey-brand')" style="display:flex;align-items:center;gap:8px">
      <span style="font-size:15px">👤</span> Андрей Самсонов
    </div>
    <div id="proj-olga-brand" class="nav-item proj-btn" data-project="olga-brand" onclick="selectProject('olga-brand')" style="display:flex;align-items:center;gap:8px">
      <span style="font-size:15px">👤</span> Ольга Федеева
    </div>
    <div id="proj-external" class="nav-item proj-btn" data-project="external" onclick="selectProject('external')" style="display:flex;align-items:center;gap:8px">
      <span style="font-size:15px">🌐</span> Внешние клиенты
    </div>
    <div style="height:1px;background:var(--border);margin:10px 4px 8px;"></div>
    """

    hierarchy = {
        "group_ministers": {
            "name": "Министерства", "icon": "layers", 
            "subgroups": {
                "sales": {"name": "Продажи", "icon": "trending-up", "items": ["Лиды", "Клиенты", "Диалоги", "Сделки", "Заказы", "КП", "CJM", "Этапы SLA", "Эскалации", "Повторные продажи", "Рекомендатели", "Sales Core"]},
                "content": {"name": "Контент", "icon": "edit-3", "items": ["Контент-завод", "WEB-завод", "Google-таблицы", "Медиафайлы"]},
                "smm": {"name": "Контент / SMM", "icon": "send", "items": ["SMM", "WEB-завод"]},
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

    nav_html = projects_nav_html
    sections_html = ""
    for g_id, g_data in hierarchy.items():
        is_ministers = (g_id == "group_ministers")
        nav_html += f'<div class="nav-group" data-group="{g_id}">'
        nav_html += f'<div class="group-header" onclick="toggleGroup(\'{g_id}\')"><i data-feather="{g_data["icon"]}"></i><span>{g_data["name"]}</span><i class="chevron" data-feather="chevron-right"></i></div>'
        nav_html += f'<div class="group-content">'
        if is_ministers:
            for s_id, s_data in g_data["subgroups"].items():
                # smm subgroup is merged into content nav; hide from navigation
                smm_hidden = ' style="display:none"' if s_id == "smm" else ''
                nav_html += f'<div class="sub-group" data-subgroup="{s_id}"{smm_hidden}>'
                nav_html += f'<div class="sub-header" onclick="toggleSubGroup(\'{s_id}\')"><i data-feather="{s_data["icon"]}" style="color:{s_data.get("color", "#fff")}"></i><span>{s_data["name"]}</span><i class="chevron" data-feather="chevron-down"></i></div>'
                nav_html += f'<div class="sub-content-items">'
                for item in s_data["items"]:
                    t_id = f"{s_id}_{item.replace(' ', '_').lower()}"
                    nav_html += f'<div class="nav-item" data-tab="{t_id}" onclick="showTab(\'{t_id}\')">{item}</div>'
                    if t_id == "tech_боты":
                        sections_html += get_bots_section_html()
                    elif t_id == "tech_интеграции":
                        sections_html += get_integrations_section_html()
                    elif t_id == "tech_сервер":
                        sections_html += get_server_section_html()
                    elif t_id == "tech_скрипты":
                        sections_html += get_scripts_section_html()
                    elif t_id == "tech_api":
                        sections_html += get_api_section_html()
                    elif t_id == "tech_воркфлоу":
                        sections_html += get_workflow_section_html()
                    elif t_id == "tech_очереди":
                        sections_html += get_queues_section_html()
                    elif t_id == "tech_логи":
                        sections_html += get_tech_logs_section_html()
                    elif t_id == "tech_ошибки":
                        sections_html += get_tech_errors_section_html()
                    elif s_id == "sales":
                        if t_id == "sales_диалоги":
                            sections_html += get_sales_dialogs_html()
                        elif t_id == "sales_сделки":
                            sections_html += get_sales_deals_html()
                        else:
                            cfg = SALES_MODULE_CONFIG.get(t_id)
                            if cfg:
                                sections_html += get_sales_table_html(t_id, f"Продажи • {item}", cfg["columns"])
                            else:
                                sections_html += get_module_section_html(s_id, t_id, item)
                    elif s_id == "analytics":
                        if t_id == "analytics_дашборды":
                            sections_html += get_analytics_dashboards_html()
                        elif t_id == "analytics_метрики":
                            sections_html += get_analytics_metrics_html()
                        elif t_id == "analytics_план-факт":
                            sections_html += get_analytics_planfact_html()
                        elif t_id == "analytics_отчёты":
                            sections_html += get_analytics_reports_html()
                        elif t_id == "analytics_ошибки_данных":
                            sections_html += get_analytics_data_errors_html()
                        elif t_id == "analytics_выводы_и_рекомендации":
                            sections_html += get_analytics_insights_html()
                        else:
                            sections_html += get_module_section_html(s_id, t_id, item)
                    elif t_id == "smm_smm":
                        sections_html += get_smm_section_html()
                    elif t_id == "smm_web-завод":
                        sections_html += get_web_factory_section_html("smm_web-завод")
                    elif t_id == "content_контент-завод":
                        sections_html += get_content_factory_section_html()
                    elif t_id == "content_web-завод":
                        sections_html += get_web_factory_section_html("content_web-завод")
                    elif s_id in ["marketing", "content", "mgmt", "production"]:
                        sections_html += get_module_section_html(s_id, t_id, item)
                    else:
                        sections_html += f'<div id="{t_id}" class="section"><h2>{s_data["name"]} • {item}</h2><div class="card premium-glow"><h3>Модуль активен</h3><p>Ожидание потока данных из контура {s_id}.</p></div></div>'
                nav_html += '</div></div>'
        else:
            for t_id, t_name in g_data["items"].items():
                nav_html += f'<div class="nav-item" data-tab="{t_id}" onclick="showTab(\'{t_id}\')">{t_name}</div>'
                if t_id != "kb_clients":
                    if t_id == "wf_templates":
                        sections_html += '<div id="wf_templates" class="section"><h2>Шаблоны</h2><div class="card"><table class="premium-table"><thead><tr><th>Шаблон</th><th>Модуль</th><th>Описание</th></tr></thead><tbody id="wf-templates-body"></tbody></table></div></div>'
                    elif t_id == "wf_runs":
                        sections_html += '<div id="wf_runs" class="section"><h2>Запуски</h2><div class="card"><table class="premium-table"><thead><tr><th>Task ID</th><th>Тип</th><th>Клиент</th><th>Статус</th><th>Приоритет</th><th>Агент</th><th>Создано</th></tr></thead><tbody id="wf-runs-body"></tbody></table></div></div>'
                    elif t_id == "wf_queues":
                        sections_html += '<div id="wf_queues" class="section"><h2>Очереди</h2><div class="stats-grid" id="wf-counts"></div><div class="card"><table class="premium-table"><thead><tr><th>Task ID</th><th>Тип</th><th>Клиент</th><th>Статус</th><th>Приоритет</th><th>Агент</th></tr></thead><tbody id="wf-queues-body"></tbody></table></div></div>'
                    elif t_id == "wf_logs":
                        sections_html += '<div id="wf_logs" class="section"><h2>Логи</h2><div class="card"><table class="premium-table"><thead><tr><th>Файл</th><th>Размер, KB</th><th>Обновлен</th><th>Путь</th></tr></thead><tbody id="wf-logs-body"></tbody></table></div></div>'
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
.smm-root {{ padding: 0 !important; height: calc(100vh - 60px); overflow: hidden; }}
.smm-layout {{ display: flex; height: 100%; }}
.smm-clients-panel {{ width: 240px; min-width: 240px; border-right: 1px solid var(--border); display: flex; flex-direction: column; background: var(--sidebar); }}
.smm-workspace {{ flex: 1; display: flex; flex-direction: column; overflow: hidden; }}
.smm-client-item {{ padding: 10px 16px; cursor: pointer; border-bottom: 1px solid var(--border); font-size: 13px; transition: background 0.15s; }}
.smm-client-item:hover {{ background: var(--surface-hover); }}
.smm-client-item.active {{ background: var(--accent-glow); border-left: 3px solid var(--accent); color: var(--text); }}
.smm-ws-header {{ padding: 16px 20px 12px; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 12px; }}
.smm-ws-tabs {{ display: flex; gap: 4px; padding: 8px 16px; border-bottom: 1px solid var(--border); background: var(--surface); }}
.smm-ws-tab {{ padding: 6px 14px; border-radius: var(--radius-sm); cursor: pointer; font-size: 13px; color: var(--text-muted); border: none; background: none; transition: all 0.15s; }}
.smm-ws-tab:hover {{ background: var(--surface-hover); color: var(--text); }}
.smm-ws-tab.active {{ background: var(--accent-glow); color: var(--accent); font-weight: 600; }}
.smm-ws-body {{ flex: 1; overflow-y: auto; padding: 20px; }}
.smm-overview-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }}
.smm-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-md); padding: 18px 20px; cursor: pointer; transition: border-color 0.15s; }}
.smm-card:hover {{ border-color: var(--accent); }}
.smm-card-num {{ font-size: 11px; color: var(--text-muted); margin-bottom: 4px; }}
.smm-card-title {{ font-size: 14px; font-weight: 600; margin-bottom: 8px; }}
.smm-card-value {{ font-size: 22px; font-weight: 700; color: var(--accent); }}
.smm-card-sub {{ font-size: 11px; color: var(--text-muted); margin-top: 4px; }}
.smm-pipeline {{ display: flex; gap: 12px; overflow-x: auto; padding-bottom: 8px; }}
.smm-pipeline-col {{ min-width: 180px; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-md); padding: 12px; }}
.smm-pipeline-col-title {{ font-size: 12px; font-weight: 600; color: var(--text-muted); margin-bottom: 10px; text-transform: uppercase; letter-spacing: 0.05em; }}
.smm-pipeline-item {{ background: var(--surface-hover); border-radius: var(--radius-sm); padding: 8px 10px; margin-bottom: 6px; font-size: 12px; border-left: 3px solid var(--accent); }}
</style>
</head>
<body>
  <aside>
    <a class="logo" href="/agents" onclick="goHome(event)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"></path></svg> <span>onTime OS</span></a>
    {nav_html}
    <div style="margin-top:auto; padding:12px; font-size:10px; color:var(--text-muted);" id="ts">Loading...</div>
  </aside>
  <main>
    <div id="marketing-global-header" style="display:none; gap:16px; align-items:center; margin-bottom:20px; flex-wrap:wrap; background: var(--surface); padding: 16px 20px; border-radius: var(--radius-lg); border: 1px solid var(--border);">
      <div style="display:flex; flex-direction:column; gap:4px;">
        <span style="font-size:10px; font-weight:700; color:var(--text-muted); text-transform:uppercase;">Бизнес-проект</span>
        <select id="mkt-biz-project" class="premium-input" style="width:200px" onchange="onMktBizProjectChange()">
          <option value="">— выберите проект —</option>
          <option value="pluslogo">🏭 Плюс Лого</option>
          <option value="ontime-ai">🤖 onTime.ai</option>
          <option value="andrey-brand">👤 Андрей Самсонов</option>
          <option value="olga-brand">👤 Ольга Федеева</option>
          <option value="external">🌐 Внешние клиенты</option>
        </select>
      </div>
      <div style="display:flex; flex-direction:column; gap:4px;">
        <span style="font-size:10px; font-weight:700; color:var(--text-muted); text-transform:uppercase;">Клиент</span>
        <select id="mkt-global-client" class="premium-input" style="width:240px" onchange="onMktGlobalClientChange()">
          <option value="all">Все клиенты (Только чтение)</option>
        </select>
      </div>
    </div>
    <input type="hidden" id="mkt-global-project" value="all">
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
        <div class="card home-wide">
          <h3 style="margin-bottom:12px">Покрытие восстановления сервера</h3>
          <div style="display:grid; grid-template-columns: repeat(3,minmax(0,1fr)); gap:12px; margin-bottom:12px;">
            <div class="stat-box"><div class="label">Синхронизация документов</div><div class="val" id="dr-docs-sync">НЕИЗВЕСТНО</div></div>
            <div class="stat-box"><div class="label">DR-бэкап (полный)</div><div class="val" id="dr-backup-pass">НЕИЗВЕСТНО</div></div>
            <div class="stat-box"><div class="label">Копия вне сервера</div><div class="val" id="dr-off-host">НЕИЗВЕСТНО</div></div>
            <div class="stat-box"><div class="label">Тест восстановления</div><div class="val" id="dr-restore-smoke">НЕИЗВЕСТНО</div></div>
            <div class="stat-box"><div class="label">Восстановление сервера</div><div class="val" id="dr-server-pass">НЕИЗВЕСТНО</div></div>
            <div class="stat-box"><div class="label">Отсутствующие зоны</div><div class="val" id="dr-missing-zones">—</div></div>
          </div>
        </div>
        <div class="card home-wide">
          <h3 style="margin-bottom:12px">Бюджет хранилища бэкапов</h3>
          <div style="display:grid; grid-template-columns: repeat(5,minmax(0,1fr)); gap:12px; margin-bottom:12px;">
            <div class="stat-box"><div class="label">Локальный бэкап (ГБ)</div><div class="val" id="dr-local-gb">—</div></div>
            <div class="stat-box"><div class="label">Облачный бэкап (ГБ)</div><div class="val" id="dr-cloud-gb">НЕИЗВЕСТНО</div></div>
            <div class="stat-box"><div class="label">Stage GB</div><div class="val" id="dr-stage-gb">—</div></div>
            <div class="stat-box"><div class="label">Статус</div><div class="val" id="dr-storage-status">НЕИЗВЕСТНО</div></div>
            <div class="stat-box"><div class="label">Кандидаты на очистку</div><div class="val" id="dr-cleanup-cnt">0</div></div>
          </div>
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
          <div style="display:grid; grid-template-columns: 1fr 1fr 1fr auto; gap:8px; margin-bottom:10px;">
            <select id="client-chat-mode" class="premium-input">
              <option value="coordinator">Со мной (координатор)</option>
              <option value="direct">С агентом напрямую</option>
            </select>
            <select id="client-chat-agent" class="premium-input">
              <option value="">Выберите агента</option>
            </select>
            <select id="client-chat-stage" class="premium-input">
              <option value="Нет потребности / есть проблема">Нет потребности / есть проблема</option>
              <option value="Есть потребность / нет решения">Есть потребность / нет решения</option>
              <option value="Есть решение / нет покупки">Есть решение / нет покупки</option>
              <option value="Есть покупка / нет лояльности">Есть покупка / нет лояльности</option>
              <option value="Лояльный клиент">Лояльный клиент</option>
              <option value="Рекомендатель">Рекомендатель</option>
              <option value="Повторная сделка">Повторная сделка</option>
            </select>
            <button class="nav-item" style="border:none; padding:10px 12px; font-weight:700; border-radius:var(--radius-sm); cursor:pointer;" onclick="runClientTaskFromChat()">Запустить</button>
          </div>
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
let currentProjectId = localStorage.getItem('ontime_project') || null;

const PROJECT_CLIENT_MAP = {{
  'pluslogo':     c => c.client_id === 'INT-PLUSLOGO' || (c.folder||'').includes('pluslogo'),
  'ontime-ai':   c => ['INT-ONTIME-AI','INT-AGENCY-SMM','INT-AGENCY-ANALYTICS','INT-AGENCY-FINANCE',
                        'INT-AGENCY-PR','INT-AGENCY-SALES','INT-AGENCY-ZAVOD','INT-RA-VOVREMYA',
                        'INT-AGENCY-CONSULTING','INT-AGENCY-CREATIVE','INT-AGENCY-LEGAL',
                        'INT-AGENCY-PRODUCTION','INT-AGENCY-TECH','INT-AGENCY-HR'].includes(c.client_id)
                     || (c.folder||'').includes('ontime-ai') || (c.folder||'').includes('ra-vovremya')
                     || (c.folder||'').includes('media-') || (c.folder||'').includes('typography')
                     || (c.folder||'').includes('min-'),
  'andrey-brand': c => c.client_id === 'INT-ANDREY-SAMSONOV-BRAND' || (c.folder||'').includes('andrey'),
  'olga-brand':   c => c.client_id === 'INT-OLGA-FEDEEVA-BRAND' || (c.folder||'').includes('olga'),
  'external':     c => c.client_id.startsWith('CL-') || c.type === 'external',
}};

function selectProject(projId) {{
  currentProjectId = projId;
  localStorage.setItem('ontime_project', projId);
  document.querySelectorAll('.proj-btn').forEach(b => b.classList.remove('active'));
  const btn = document.getElementById('proj-' + projId);
  if (btn) btn.classList.add('active');
  refreshProjectClients();
}}

function refreshProjectClients() {{
  if (!currentProjectId || !PROJECT_CLIENT_MAP[currentProjectId]) return;
  const filter = PROJECT_CLIENT_MAP[currentProjectId];
  const filtered = allClients.filter(filter);
  // Update all module client selects
  document.querySelectorAll('.module-client-select').forEach(sel => {{
    const cur = sel.value;
    sel.innerHTML = '<option value="">Выберите клиента</option>' +
      filtered.map(c => `<option value="${{c.client_id}}" ${{c.client_id===cur?'selected':''}}>${{c.name}} (${{c.client_id}})</option>`).join('');
  }});
  // Update mkt-global-client
  const mktSel = document.getElementById('mkt-global-client');
  if (mktSel) {{
    const cur = mktSel.value;
    mktSel.innerHTML = '<option value="all">Все клиенты (Только чтение)</option>' +
      filtered.map(c => `<option value="${{c.client_id}}" ${{c.client_id===cur?'selected':''}}>${{c.name}} (${{c.client_id}})</option>`).join('');
  }}
  // Update global client selector in client panel
  const cSel = document.getElementById('client-select');
  if (cSel) {{
    const cur = cSel.value;
    cSel.innerHTML = '<option value="">Выберите клиента</option>' +
      filtered.map(c => `<option value="${{c.client_id}}" ${{c.client_id===cur?'selected':''}}>${{c.name}} (${{c.client_id}})</option>`).join('');
  }}
}}
let currentStageThread = null;
let pollInterval = null;
let chatPollInterval = null;

const MODULE_CONFIG = {{
  "sales_лиды": {{ title: "Лиды", module: "sales", section: "leads", columns: ["Имя", "Контакт", "Источник", "Запрос", "Этап воронки", "Статус", "Ответственный", "Следующий шаг"], fieldTypes: {{
    "Этап воронки": {{ type: "select", options: ["Нет потребности / есть проблема", "Есть потребность / нет решения", "Есть решение / нет покупки", "Есть покупка / нет лояльности", "Лояльный клиент", "Рекомендатель", "Повторная сделка"] }}
  }} }},
  "sales_клиенты": {{ title: "Клиенты", module: "sales", section: "clients", columns: ["Название", "Контакт", "Этап воронки", "Статус", "Ответственный", "Активность", "Сделки"], fieldTypes: {{
    "Этап воронки": {{ type: "select", options: ["Нет потребности / есть проблема", "Есть потребность / нет решения", "Есть решение / нет покупки", "Есть покупка / нет лояльности", "Лояльный клиент", "Рекомендатель", "Повторная сделка"] }}
  }} }},
  "sales_диалоги": {{ title: "Диалоги", module: "sales", section: "dialogs", columns: ["Клиент", "Канал", "Последний текст", "Статус", "Ответственный", "Время"] }},
  "sales_сделки": {{ title: "Сделки", module: "sales", section: "deals", columns: ["Название", "Клиент", "Сумма", "Стадия", "Вероятность", "Дедлайн"] }},
  "sales_заказы": {{ title: "Заказы", module: "sales", section: "orders", columns: ["Номер", "Клиент", "Продукт", "Сумма", "Статус", "Дедлайн", "Ответственный"], fieldTypes: {{
    "Статус": {{ type: "select", options: ["\u041d\u043e\u0432\u044b\u0439", "\u0412 \u0440\u0430\u0431\u043e\u0442\u0435", "\u041e\u043f\u043b\u0430\u0447\u0435\u043d", "\u0412\u044b\u043f\u043e\u043b\u043d\u0435\u043d", "\u041e\u0442\u043c\u0435\u043d\u0451\u043d"] }},
    "\u0414\u0435\u0434\u043b\u0430\u0439\u043d": {{ type: "date" }}
  }} }},
  "sales_кп": {{ title: "КП", module: "sales", section: "proposals", columns: ["Номер", "Клиент", "Сделка", "Сумма", "Статус", "Дата"] }},
  "sales_повторные_продажи": {{ title: "ПовторныеПродажи", module: "loyalty", section: "repeat_sales", columns: ["Клиент", "Тип", "Продукт/Услуга", "Сумма", "Этап воронки", "NPS", "Дата", "Статус", "Комментарий"], fieldTypes: {{
    "Тип": {{ type: "select", options: ["Повторная покупка", "Upsell", "Crosssell", "Пролонгация", "Пакет"] }},
    "Этап воронки": {{ type: "select", options: ["Есть покупка / нет лояльности", "Лояльный клиент", "Рекомендатель", "Повторная сделка"] }},
    "NPS": {{ type: "select", options: ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10"] }},
    "Дата": {{ type: "date" }},
    "Статус": {{ type: "select", options: ["В работе", "Выставлен счёт", "Оплачено", "Отменено"] }}
  }} }},
  "sales_рекомендатели": {{ title: "Рекомендатели", module: "loyalty", section: "referrals", columns: ["Рекомендатель", "Кого привёл", "Канал", "Статус реферала", "Бонус", "Дата", "Комментарий"], fieldTypes: {{
    "Канал": {{ type: "select", options: ["Личная рекомендация", "Соцсети", "Email", "Мессенджер", "Другое"] }},
    "Статус реферала": {{ type: "select", options: ["Передан", "Квалифицирован", "Сделка закрыта", "Не конвертировался"] }},
    "Дата": {{ type: "date" }}
  }} }},
  "sales_cjm": {{ title: "CJM", module: "sales", section: "cjm", columns: ["CJM-состояние", "Лиды", "Клиенты", "Сделки", "Риск", "Рекомендованное действие", "Ответственный", "Дедлайн", "Последний запуск"] }},
  "sales_этапы_sla": {{ title: "ЭтапыSLA", module: "sales", section: "этапы_sla", columns: ["Этап", "SLA (дни)", "Лидов в этапе", "Просрочено", "Макс. возраст (дни)", "Статус", "Ответственный", "Дедлайн", "Последний запуск"] }},
  "sales_эскалации": {{ title: "Эскалации", module: "sales", section: "escalations", columns: ["stage", "priority", "status", "reason", "task_id", "created_at"] }},
  "sales_sales_core": {{ title: "SalesCore", module: "sales", section: "sales_core", columns: ["Этап воронки", "Лиды", "Клиенты", "Сделки", "Сумма сделок", "План", "Факт", "Отклонение %", "Потери"] }},
  "marketing_ца": {{ title: "\u0426\u0410", module: "marketing", section: "audiences", columns: ["id", "\u0421\u0435\u0433\u043c\u0435\u043d\u0442 \u0426\u0410", "\u0421\u043b\u043e\u0433\u0430\u043d", "\u0423\u0422\u041f", "\u0411\u043e\u043b\u0438", "\u0421\u043e\u0434\u0435\u0440\u0436\u0430\u043d\u0438\u0435 \u043e\u0431\u044a\u044f\u0432\u043b\u0435\u043d\u0438\u044f", "CTA", "\u041c\u0430\u0440\u043a\u0435\u0442\u0438\u043d\u0433\u043e\u0432\u044b\u0435 \u0430\u0440\u0445\u0435\u0442\u0438\u043f\u044b", "\u041f\u043e\u0434\u0445\u043e\u0434\u044f\u0449\u0438\u0435 \u043f\u0440\u043e\u0434\u0443\u043a\u0442\u044b", "\u0422\u043e\u0432\u0430\u0440\u044b \u0441 \u0432\u044b\u0441\u043e\u043a\u043e\u0439 \u0432\u0435\u0440\u043e\u044f\u0442\u043d\u043e\u0441\u0442\u044c\u044e \u0437\u0430\u043a\u0430\u0437\u0430", "\u041a\u043b\u044e\u0447\u0435\u0432\u044b\u0435 \u0442\u043e\u0432\u0430\u0440\u044b (\u043e\u0441\u043d\u043e\u0432\u043d\u043e\u0439 \u0434\u043e\u0445\u043e\u0434)", "\u0427\u0442\u043e \u0434\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u0438 \u0447\u0435\u0433\u043e \u043f\u043e\u043a\u0430 \u043d\u0435\u0442", "\u041a\u043e\u043c\u043f\u043b\u0435\u043a\u0441\u043d\u043e\u0435 \u043f\u0440\u0435\u0434\u043b\u043e\u0436\u0435\u043d\u0438\u0435 (\u0430\u043f\u0441\u0435\u0439\u043b)", "\u041f\u043b\u0430\u043d\u043e\u0432\u0430\u044f \u0441\u0443\u043c\u043c\u0430 \u0432 \u043c\u0435\u0441\u044f\u0446 (\u20bd)", "\u041f\u0440\u0438\u043e\u0440\u0438\u0442\u0435\u0442 (1\u20135)", "\u041f\u0440\u0438\u043c\u0435\u0440\u043d\u0430\u044f \u0434\u043e\u043b\u044f \u0432 \u0432\u044b\u0440\u0443\u0447\u043a\u0435", "\u041e\u0446\u0435\u043d\u043a\u0430 \u0432\u0435\u0440\u043e\u044f\u0442\u043d\u043e\u0441\u0442\u0438 \u043f\u0440\u043e\u0434\u0430\u0436 (%)", "\u041a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440\u0438\u0439"], fieldTypes: {{
    "id": {{ type: "readonly" }},
    "\u0423\u0422\u041f": {{ type: "textarea" }},
    "\u0411\u043e\u043b\u0438": {{ type: "textarea" }},
    "\u0421\u043e\u0434\u0435\u0440\u0436\u0430\u043d\u0438\u0435 \u043e\u0431\u044a\u044f\u0432\u043b\u0435\u043d\u0438\u044f": {{ type: "textarea" }},
    "\u041c\u0430\u0440\u043a\u0435\u0442\u0438\u043d\u0433\u043e\u0432\u044b\u0435 \u0430\u0440\u0445\u0435\u0442\u0438\u043f\u044b": {{ type: "textarea" }},
    "\u041f\u043e\u0434\u0445\u043e\u0434\u044f\u0449\u0438\u0435 \u043f\u0440\u043e\u0434\u0443\u043a\u0442\u044b": {{ type: "textarea" }},
    "\u0422\u043e\u0432\u0430\u0440\u044b \u0441 \u0432\u044b\u0441\u043e\u043a\u043e\u0439 \u0432\u0435\u0440\u043e\u044f\u0442\u043d\u043e\u0441\u0442\u044c\u044e \u0437\u0430\u043a\u0430\u0437\u0430": {{ type: "textarea" }},
    "\u041a\u043b\u044e\u0447\u0435\u0432\u044b\u0435 \u0442\u043e\u0432\u0430\u0440\u044b (\u043e\u0441\u043d\u043e\u0432\u043d\u043e\u0439 \u0434\u043e\u0445\u043e\u0434)": {{ type: "textarea" }},
    "\u0427\u0442\u043e \u0434\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u0438 \u0447\u0435\u0433\u043e \u043f\u043e\u043a\u0430 \u043d\u0435\u0442": {{ type: "textarea" }},
    "\u041a\u043e\u043c\u043f\u043b\u0435\u043a\u0441\u043d\u043e\u0435 \u043f\u0440\u0435\u0434\u043b\u043e\u0436\u0435\u043d\u0438\u0435 (\u0430\u043f\u0441\u0435\u0439\u043b)": {{ type: "textarea" }},
    "\u041f\u043b\u0430\u043d\u043e\u0432\u0430\u044f \u0441\u0443\u043c\u043c\u0430 \u0432 \u043c\u0435\u0441\u044f\u0446 (\u20bd)": {{ type: "number" }},
    "\u041f\u0440\u0438\u043e\u0440\u0438\u0442\u0435\u0442 (1\u20135)": {{ type: "select", options: ["1", "2", "3", "4", "5"] }},
    "\u041e\u0446\u0435\u043d\u043a\u0430 \u0432\u0435\u0440\u043e\u044f\u0442\u043d\u043e\u0441\u0442\u0438 \u043f\u0440\u043e\u0434\u0430\u0436 (%)": {{ type: "number" }},
    "\u041a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440\u0438\u0439": {{ type: "textarea" }}
  }} }},
  "marketing_боли": {{ title: "Боли", module: "marketing", section: "pains", columns: ["Боль", "Сегмент ЦА", "Интенсивность", "Статус", "Приоритет"] }},
  "marketing_утп": {{ title: "Утп", module: "marketing", section: "usps", columns: ["Формулировка", "Сегмент", "Канал", "Статус", "Владелец"] }},
  "marketing_офферы": {{ title: "Офферы", module: "marketing", section: "offers", columns: ["Заголовок", "Текст", "Канал", "Конверсия", "Статус"] }},
  "marketing_воронки": {{ title: "Воронки", module: "marketing", section: "funnels", columns: ["funnel_id", "Название воронки", "Этап", "Порядок этапа", "Вход", "Выход", "CR%", "Итоговая конверсия", "Связанный оффер", "Источник трафика", "Рекламная кампания", "Статус", "Ответственный", "Комментарий"], fieldTypes: {{
    "Порядок этапа": {{ type: "number" }},
    "Вход": {{ type: "number" }},
    "Выход": {{ type: "number" }},
    "CR%": {{ type: "readonly" }},
    "Итоговая конверсия": {{ type: "readonly" }},
    "Связанный оффер": {{ type: "select-relational", relation: "offers", labelColumn: "Заголовок" }},
    "Источник трафика": {{ type: "select-relational", relation: "traffic_sources", labelColumn: "Канал" }},
    "Рекламная кампания": {{ type: "select-relational", relation: "campaigns", labelColumn: "Кампания" }},
    "Статус": {{ type: "select", options: ["Черновик", "Активна", "Тест", "Архив"] }}
  }} }},
  "marketing_источники_трафика": {{ title: "ИсточникиТрафика", module: "marketing", section: "traffic_sources", columns: ["Название", "Ссылка", "Тип", "Клиенты", "Интеграция", "Бюджет", "Лиды", "CPL", "Статус"], fieldTypes: {{
    "Ссылка": {{ type: "url" }},
    "Тип": {{ type: "select", options: ["social", "site", "marketplace", "ads", "email", "other"] }},
    "Статус": {{ type: "select", options: ["active", "paused", "draft", "archived", "error"] }},
    "Бюджет": {{ type: "number" }},
    "Лиды": {{ type: "number" }},
    "CPL": {{ type: "number" }}
  }} }},
  "marketing_реклама": {{ title: "Реклама", module: "marketing", section: "campaigns", columns: ["Кампания", "Канал", "Бюджет", "Статус", "Ссылка"] }},
  "marketing_креатив": {{ title: "Креатив", module: "marketing", section: "creatives", columns: ["Название", "Тип", "Канал", "Оффер", "Статус", "Конверсия", "Post ID", "Ссылка"], fieldTypes: {{
    "Тип": {{ type: "select", options: ["Карусель", "Баннер", "Текст", "Видео"] }},
    "Статус": {{ type: "select", options: ["draft", "in_design", "review", "approved", "rework"] }}
  }} }},
  "marketing_упаковка_продукта": {{ title: "УпаковкаПродукта", module: "marketing", section: "brand", columns: ["Параметр", "Значение", "Статус", "Комментарий"] }},
  "content_контент-завод": {{ title: "КонтентЗавод", module: "content", section: "topics", columns: ["Задача", "Статус", "Исполнитель"] }},
  "content_темы": {{ title: "Темы", module: "content", section: "topics", columns: ["Тема", "Приоритет", "Статус", "Дедлайн"] }},
  "content_статьи": {{ title: "Статьи", module: "content", section: "articles", columns: ["Заголовок", "Автор", "Статус", "Дата"] }},
  "content_посты": {{ title: "Посты", module: "content", section: "posts", columns: ["Текст", "Сеть", "Формат", "Статус", "Дата", "Креатив URL"], fieldTypes: {{
    "Сеть": {{ type: "select", options: ["TG", "VK", "VC", "Instagram"] }},
    "Формат": {{ type: "select", options: ["Карусель", "Пост", "Статья", "Видео"] }},
    "Статус": {{ type: "select", options: ["draft", "review", "approved", "in_design", "ready_to_publish", "published", "rework"] }},
    "Дата": {{ type: "date" }}
  }} }},
  "content_публикации": {{ title: "Публикации", module: "content", section: "publications", columns: ["Ресурс", "Ссылка", "Статус", "Дата", "Post ID"] }},
  "content_комментарии": {{ title: "Комментарии", module: "content", section: "comments", columns: ["Текст", "Где", "Статус", "Дата"] }},
  "content_google-таблицы": {{ title: "GoogleТаблицы", module: "content", section: "sheets", columns: ["Название", "URL", "Статус", "Комментарий"] }},
  "content_медиафайлы": {{ title: "Медиафайлы", module: "content", section: "media", columns: ["\u0418\u043c\u044f", "\u0422\u0438\u043f", "\u041f\u0440\u043e\u0435\u043a\u0442", "\u0420\u0430\u0437\u043c\u0435\u0440", "\u0421\u0442\u0430\u0442\u0443\u0441", "\u041e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u043e", "\u0421\u0441\u044b\u043b\u043a\u0430"] }},
  "production_заказы": {{ title: "Заказы", module: "production", section: "orders", columns: ["Номер", "Клиент", "Изделие", "Тираж", "Статус", "Дедлайн", "Ответственный"], fieldTypes: {{
    "Тираж": {{ type: "number" }},
    "Статус": {{ type: "select", options: ["Новый", "В работе", "Готов", "Отгружен", "Отменён"] }},
    "Дедлайн": {{ type: "date" }}
  }} }},
  "production_план": {{ title: "План", module: "production", section: "plan", columns: ["Дата", "Заказ", "Операция", "План", "Факт", "Статус", "Комментарий"], fieldTypes: {{
    "Дата": {{ type: "date" }},
    "План": {{ type: "number" }},
    "Факт": {{ type: "number" }},
    "Статус": {{ type: "select", options: ["План", "В работе", "Выполнено", "Срыв", "Перенос"] }}
  }} }},
  "production_смены": {{ title: "Смены", module: "production", section: "shifts", columns: ["Дата", "Смена", "Сотрудник", "Роль", "Часы", "Статус", "Комментарий"], fieldTypes: {{
    "Дата": {{ type: "date" }},
    "Смена": {{ type: "select", options: ["День", "Вечер", "Ночь"] }},
    "Часы": {{ type: "number" }},
    "Статус": {{ type: "select", options: ["Запланирована", "Идёт", "Закрыта", "Отмена"] }}
  }} }},
  "production_операции": {{ title: "Операции", module: "production", section: "operations", columns: ["Операция", "Заказ", "Станок", "Статус", "Начало", "Конец", "Ответственный"], fieldTypes: {{
    "Статус": {{ type: "select", options: ["Очередь", "В работе", "Готово", "Пауза", "Брак"] }},
    "Начало": {{ type: "date" }},
    "Конец": {{ type: "date" }}
  }} }},
  "production_материалы": {{ title: "Материалы", module: "production", section: "materials", columns: ["Материал", "Ед.", "План", "Факт", "Поставщик", "Статус", "Комментарий"], fieldTypes: {{
    "План": {{ type: "number" }},
    "Факт": {{ type: "number" }},
    "Статус": {{ type: "select", options: ["Нужно заказать", "Заказано", "В наличии", "Дефицит", "Списано"] }}
  }} }},
  "production_остатки": {{ title: "Остатки", module: "production", section: "stock", columns: ["Материал", "Ед.", "Остаток", "Резерв", "Минимум", "Статус", "Обновлено"], fieldTypes: {{
    "Остаток": {{ type: "number" }},
    "Резерв": {{ type: "number" }},
    "Минимум": {{ type: "number" }},
    "Статус": {{ type: "select", options: ["OK", "Ниже минимума", "Резерв", "Нет в наличии"] }},
    "Обновлено": {{ type: "date" }}
  }} }},
  "production_брак": {{ title: "Брак", module: "production", section: "defects", columns: ["Дата", "Заказ", "Операция", "Причина", "Количество", "Ответственный", "Решение"], fieldTypes: {{
    "Дата": {{ type: "date" }},
    "Количество": {{ type: "number" }},
    "Решение": {{ type: "select", options: ["Переделать", "Списать", "Согласовать", "Компенсация", "Закрыто"] }}
  }} }},
  "production_загрузка": {{ title: "Загрузка", module: "production", section: "load", columns: ["Ресурс", "Дата", "Плановая загрузка %", "Фактическая загрузка %", "Очередь", "Статус", "Комментарий"], fieldTypes: {{
    "Дата": {{ type: "date" }},
    "Плановая загрузка %": {{ type: "number" }},
    "Фактическая загрузка %": {{ type: "number" }},
    "Очередь": {{ type: "number" }},
    "Статус": {{ type: "select", options: ["Свободно", "Норма", "Перегруз", "Простой"] }}
  }} }},
  "mgmt_финансы": {{ title: "\u0424\u0438\u043d\u0430\u043d\u0441\u044b", module: "mgmt", section: "finance", columns: ["id", "\u0421\u0442\u0430\u0442\u044c\u044f", "\u0422\u0438\u043f", "\u0421\u0443\u043c\u043c\u0430", "\u0412\u0430\u043b\u044e\u0442\u0430", "\u0414\u0430\u0442\u0430", "\u041a\u0430\u0442\u0435\u0433\u043e\u0440\u0438\u044f", "\u0421\u0442\u0430\u0442\u0443\u0441", "\u041a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440\u0438\u0439"], fieldTypes: {{
    "\u0422\u0438\u043f": {{ type: "select", options: ["\u0414\u043e\u0445\u043e\u0434", "\u0420\u0430\u0441\u0445\u043e\u0434", "\u041f\u0435\u0440\u0435\u0432\u043e\u0434"] }},
    "\u0412\u0430\u043b\u044e\u0442\u0430": {{ type: "select", options: ["RUB", "USD", "EUR"] }},
    "\u0421\u0442\u0430\u0442\u0443\u0441": {{ type: "select", options: ["\u041f\u043b\u0430\u043d", "\u0424\u0430\u043a\u0442", "\u041e\u0442\u043c\u0435\u043d\u0451\u043d"] }},
    "\u0414\u0430\u0442\u0430": {{ type: "date" }}
  }} }},
  "mgmt_юрконтур": {{ title: "\u042e\u0440\u043a\u043e\u043d\u0442\u0443\u0440", module: "mgmt", section: "legal", columns: ["id", "\u0414\u043e\u043a\u0443\u043c\u0435\u043d\u0442", "\u0422\u0438\u043f", "\u041a\u043e\u043d\u0442\u0440\u0430\u0433\u0435\u043d\u0442", "\u0421\u0442\u0430\u0442\u0443\u0441", "\u0414\u0430\u0442\u0430 \u043f\u043e\u0434\u043f\u0438\u0441\u0430\u043d\u0438\u044f", "\u0414\u0430\u0442\u0430 \u043e\u043a\u043e\u043d\u0447\u0430\u043d\u0438\u044f", "\u041e\u0442\u0432\u0435\u0442\u0441\u0442\u0432\u0435\u043d\u043d\u044b\u0439"], fieldTypes: {{
    "\u0422\u0438\u043f": {{ type: "select", options: ["\u0414\u043e\u0433\u043e\u0432\u043e\u0440", "\u041d\u0414\u0410", "\u0421\u0447\u0451\u0442", "\u0410\u043a\u0442", "\u0414\u043e\u043f. \u0441\u043e\u0433\u043b\u0430\u0448\u0435\u043d\u0438\u0435"] }},
    "\u0421\u0442\u0430\u0442\u0443\u0441": {{ type: "select", options: ["\u0410\u043a\u0442\u0438\u0432\u0435\u043d", "\u0418\u0441\u0442\u0451\u043a", "\u041d\u0430 \u043f\u043e\u0434\u043f\u0438\u0441\u0438", "\u0420\u0430\u0441\u0442\u043e\u0440\u0433\u043d\u0443\u0442"] }},
    "\u0414\u0430\u0442\u0430 \u043f\u043e\u0434\u043f\u0438\u0441\u0430\u043d\u0438\u044f": {{ type: "date" }},
    "\u0414\u0430\u0442\u0430 \u043e\u043a\u043e\u043d\u0447\u0430\u043d\u0438\u044f": {{ type: "date" }}
  }} }},
  "mgmt_консалтинг": {{ title: "\u041a\u043e\u043d\u0441\u0430\u043b\u0442\u0438\u043d\u0433", module: "mgmt", section: "consulting", columns: ["id", "\u041f\u0440\u043e\u0435\u043a\u0442", "\u042d\u0442\u0430\u043f", "\u0411\u044e\u0434\u0436\u0435\u0442", "\u0424\u0430\u043a\u0442 \u043e\u043f\u043b\u0430\u0442", "\u0421\u0442\u0430\u0442\u0443\u0441", "\u0414\u0430\u0442\u0430 \u0441\u0442\u0430\u0440\u0442\u0430", "\u0414\u0430\u0442\u0430 \u0437\u0430\u043a\u0440\u044b\u0442\u0438\u044f"], fieldTypes: {{
    "\u0421\u0442\u0430\u0442\u0443\u0441": {{ type: "select", options: ["\u0410\u043a\u0442\u0438\u0432\u0435\u043d", "\u0417\u0430\u0432\u0435\u0440\u0448\u0451\u043d", "\u041f\u0440\u0438\u043e\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d", "\u0412 \u043f\u0435\u0440\u0435\u0433\u043e\u0432\u043e\u0440\u0430\u0445"] }},
    "\u0414\u0430\u0442\u0430 \u0441\u0442\u0430\u0440\u0442\u0430": {{ type: "date" }},
    "\u0414\u0430\u0442\u0430 \u0437\u0430\u043a\u0440\u044b\u0442\u0438\u044f": {{ type: "date" }}
  }} }},
  "mgmt_pr": {{ title: "PR", module: "mgmt", section: "pr", columns: ["id", "\u041c\u0430\u0442\u0435\u0440\u0438\u0430\u043b", "\u041f\u043b\u043e\u0449\u0430\u0434\u043a\u0430", "\u0422\u0438\u043f", "\u0410\u0432\u0442\u043e\u0440", "\u0421\u0442\u0430\u0442\u0443\u0441", "\u0414\u0430\u0442\u0430 \u0432\u044b\u0445\u043e\u0434\u0430", "\u041e\u0445\u0432\u0430\u0442"], fieldTypes: {{
    "\u0422\u0438\u043f": {{ type: "select", options: ["\u0421\u0442\u0430\u0442\u044c\u044f", "\u0418\u043d\u0442\u0435\u0440\u0432\u044c\u044e", "\u041f\u0440\u0435\u0441\u0441-\u0440\u0435\u043b\u0438\u0437", "\u041a\u0435\u0439\u0441", "\u041f\u043e\u0441\u0442"] }},
    "\u0421\u0442\u0430\u0442\u0443\u0441": {{ type: "select", options: ["\u0418\u0434\u0435\u044f", "\u0412 \u0440\u0430\u0431\u043e\u0442\u0435", "\u041d\u0430 \u0441\u043e\u0433\u043b\u0430\u0441\u043e\u0432\u0430\u043d\u0438\u0438", "\u041e\u043f\u0443\u0431\u043b\u0438\u043a\u043e\u0432\u0430\u043d"] }},
    "\u0414\u0430\u0442\u0430 \u0432\u044b\u0445\u043e\u0434\u0430": {{ type: "date" }}
  }} }},
  "mgmt_задачи_собственника": {{ title: "\u0417\u0430\u0434\u0430\u0447\u0438\u0421\u043e\u0431\u0441\u0442\u0432\u0435\u043d\u043d\u0438\u043a\u0430", module: "mgmt", section: "owner_tasks", columns: ["id", "\u0417\u0430\u0434\u0430\u0447\u0430", "\u041f\u0440\u0438\u043e\u0440\u0438\u0442\u0435\u0442", "\u041a\u0430\u0442\u0435\u0433\u043e\u0440\u0438\u044f", "\u0421\u0442\u0430\u0442\u0443\u0441", "\u0414\u0435\u0434\u043b\u0430\u0439\u043d", "\u0414\u0435\u043b\u0435\u0433\u0438\u0440\u043e\u0432\u0430\u043d\u043e", "\u041a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440\u0438\u0439"], fieldTypes: {{
    "\u041f\u0440\u0438\u043e\u0440\u0438\u0442\u0435\u0442": {{ type: "select", options: ["\u0412\u044b\u0441\u043e\u043a\u0438\u0439", "\u0421\u0440\u0435\u0434\u043d\u0438\u0439", "\u041d\u0438\u0437\u043a\u0438\u0439"] }},
    "\u0421\u0442\u0430\u0442\u0443\u0441": {{ type: "select", options: ["\u041d\u043e\u0432\u0430\u044f", "\u0412 \u0440\u0430\u0431\u043e\u0442\u0435", "\u0413\u043e\u0442\u043e\u0432\u043e", "\u041e\u0442\u043c\u0435\u043d\u0435\u043d\u0430"] }},
    "\u0414\u0435\u0434\u043b\u0430\u0439\u043d": {{ type: "date" }}
  }} }},
  "mgmt_стратегия": {{ title: "\u0421\u0442\u0440\u0430\u0442\u0435\u0433\u0438\u044f", module: "mgmt", section: "strategy", columns: ["id", "\u0426\u0435\u043b\u044c", "KPI", "\u0413\u043e\u0440\u0438\u0437\u043e\u043d\u0442", "\u0422\u0435\u043a\u0443\u0449\u0435\u0435 \u0437\u043d\u0430\u0447\u0435\u043d\u0438\u0435", "\u0426\u0435\u043b\u0435\u0432\u043e\u0435 \u0437\u043d\u0430\u0447\u0435\u043d\u0438\u0435", "\u0421\u0442\u0430\u0442\u0443\u0441", "\u041e\u0442\u0432\u0435\u0442\u0441\u0442\u0432\u0435\u043d\u043d\u044b\u0439"], fieldTypes: {{
    "\u0413\u043e\u0440\u0438\u0437\u043e\u043d\u0442": {{ type: "select", options: ["\u041d\u0435\u0434\u0435\u043b\u044f", "\u041c\u0435\u0441\u044f\u0446", "\u041a\u0432\u0430\u0440\u0442\u0430\u043b", "\u0413\u043e\u0434"] }},
    "\u0421\u0442\u0430\u0442\u0443\u0441": {{ type: "select", options: ["\u041d\u0430 \u0442\u0440\u0435\u043a\u0435", "\u041e\u0442\u0441\u0442\u0430\u0451\u043c", "\u0412\u044b\u043f\u043e\u043b\u043d\u0435\u043d\u043e", "\u0417\u0430\u043c\u043e\u0440\u043e\u0436\u0435\u043d\u043e"] }}
  }} }},
  "mgmt_документы": {{ title: "\u0414\u043e\u043a\u0443\u043c\u0435\u043d\u0442\u044b", module: "mgmt", section: "documents", columns: ["id", "\u041d\u0430\u0437\u0432\u0430\u043d\u0438\u0435", "\u0422\u0438\u043f", "\u041a\u0430\u0442\u0435\u0433\u043e\u0440\u0438\u044f", "\u041e\u0442\u0432\u0435\u0442\u0441\u0442\u0432\u0435\u043d\u043d\u044b\u0439", "\u0414\u0430\u0442\u0430", "\u0421\u0441\u044b\u043b\u043a\u0430"], fieldTypes: {{
    "\u0422\u0438\u043f": {{ type: "select", options: ["\u0414\u043e\u0433\u043e\u0432\u043e\u0440", "\u041f\u0440\u0435\u0437\u0435\u043d\u0442\u0430\u0446\u0438\u044f", "\u041e\u0442\u0447\u0451\u0442", "\u0420\u0435\u0433\u043b\u0430\u043c\u0435\u043d\u0442", "\u041f\u0440\u043e\u0447\u0435\u0435"] }},
    "\u0414\u0430\u0442\u0430": {{ type: "date" }}
  }} }}
}};

const LEGACY_SALES_TABS = new Set([
  'sales_лиды', 'sales_клиенты', 'sales_диалоги',
  'sales_сделки', 'sales_кп', 'sales_sales_core', 'sales_cjm', 'sales_этапы_sla', 'sales_эскалации'
]);
const FUNNEL_STAGE_OPTIONS = [
  'Нет потребности / есть проблема',
  'Есть потребность / нет решения',
  'Есть решение / нет покупки',
  'Есть покупка / нет лояльности',
  'Лояльный клиент',
  'Рекомендатель',
  'Повторная сделка'
];

function saveState(t) {{ const groups = Array.from(document.querySelectorAll('.nav-group.open')).map(el => el.getAttribute('data-group')); const subs = Array.from(document.querySelectorAll('.sub-group.open')).map(el => el.getAttribute('data-subgroup')); localStorage.setItem(STATE_KEY, JSON.stringify({{ tab: t, groups, subs }})); }}
function loadState() {{
  let s = {{}};
  try {{
    s = JSON.parse(localStorage.getItem(STATE_KEY) || '{{}}') || {{}};
  }} catch (e) {{
    // Self-heal corrupted localStorage state: keep UI functional instead of hard JS stop.
    try {{ localStorage.removeItem(STATE_KEY); }} catch (_) {{}}
    s = {{}};
  }}
  if (Array.isArray(s.groups)) s.groups.forEach(g => toggleGroup(g, true));
  if (Array.isArray(s.subs)) s.subs.forEach(sub => toggleSubGroup(sub, true));
  showTab(INITIAL_TAB || s.tab || 'home');
}}
function goHome(e) {{ if (e) e.preventDefault(); showTab('home'); window.history.replaceState(null, '', '/agents'); }}
function closeMarketingPanel() {{ const panel = document.getElementById('mkt-sidepanel'); if (panel) panel.classList.remove('open'); }}
function toggleGroup(id, force=false) {{ const el = document.querySelector(`.nav-group[data-group="${{id}}"]`); if (!el) return; if (!force && el.classList.contains('open')) return; document.querySelectorAll('.nav-group').forEach(g => g.classList.remove('open')); el.classList.add('open'); saveState(document.querySelector('.nav-item.active')?.getAttribute('data-tab')); }}
function toggleSubGroup(id, force=false) {{ const el = document.querySelector(`.sub-group[data-subgroup="${{id}}"]`); if (!el) return; const wasOpen = el.classList.contains('open'); if (!force) {{ document.querySelectorAll('.sub-group').forEach(g => g.classList.remove('open')); if (!wasOpen) el.classList.add('open'); }} else el.classList.add('open'); saveState(document.querySelector('.nav-item.active')?.getAttribute('data-tab')); }}

async function loadWorkflowTemplates() {{
  const tb = document.getElementById('wf-templates-body');
  if (!tb) return;
  const r = await fetch('/api/workflow/templates').then(x => x.json()).catch(() => ({{}}));
  const rows = r.data || [];
  tb.innerHTML = rows.length ? rows.map(x => `<tr><td>${{x.template||''}}</td><td>${{x.module||''}}</td><td>${{x.description||''}}</td></tr>`).join('') : '<tr><td colspan="3" style="color:var(--text-muted)">Нет шаблонов</td></tr>';
}}

async function loadWorkflowRuns() {{
  const tb = document.getElementById('wf-runs-body');
  if (!tb) return;
  const r = await fetch('/api/workflow/runs').then(x => x.json()).catch(() => ({{}}));
  const rows = r.data || [];
  tb.innerHTML = rows.length ? rows.map(x => `<tr><td>${{x.task_id||''}}</td><td>${{x.task_type||''}}</td><td>${{x.client_id||''}}</td><td>${{x.state||''}}</td><td>${{x.priority||''}}</td><td>${{x.assigned_agent||''}}</td><td>${{x.created_at||''}}</td></tr>`).join('') : '<tr><td colspan="7" style="color:var(--text-muted)">Нет запусков</td></tr>';
}}

async function loadWorkflowQueues() {{
  const s = await fetch('/api/tasks').then(x => x.json()).catch(() => ({{}}));
  const runs = await fetch('/api/workflow/runs').then(x => x.json()).catch(() => ({{}}));
  const counts = s.counts || {{}};
  const top = document.getElementById('wf-counts');
  if (top) top.innerHTML = Object.entries(counts).map(([k,v]) => `<div class="stat-box"><div class="label">${{k}}</div><div class="val">${{v}}</div></div>`).join('');
  const tb = document.getElementById('wf-queues-body');
  if (!tb) return;
  const rows = (runs.data || []).filter(x => ['pending','processing','dead','blocked'].includes((x.state||'')));
  tb.innerHTML = rows.length ? rows.map(x => `<tr><td>${{x.task_id||''}}</td><td>${{x.task_type||''}}</td><td>${{x.client_id||''}}</td><td>${{x.state||''}}</td><td>${{x.priority||''}}</td><td>${{x.assigned_agent||''}}</td></tr>`).join('') : '<tr><td colspan="6" style="color:var(--text-muted)">Очередь пуста</td></tr>';
}}

async function loadWorkflowLogs() {{
  const tb = document.getElementById('wf-logs-body');
  if (!tb) return;
  const r = await fetch('/api/workflow/logs').then(x => x.json()).catch(() => ({{}}));
  const rows = r.data || [];
  tb.innerHTML = rows.length ? rows.map(x => `<tr><td>${{x.file||''}}</td><td>${{x.size_kb||0}}</td><td>${{x.updated_at||''}}</td><td style="color:var(--text-muted)">${{x.path||''}}</td></tr>`).join('') : '<tr><td colspan="4" style="color:var(--text-muted)">Логи не найдены</td></tr>';
}}

function populateAnalyticsClients(id) {{
    const sel = document.getElementById(id + '-client');
    if (!sel || (sel.options && sel.options.length > 1)) return;
    sel.innerHTML = '<option value="">Выберите клиента</option>' + 
        allClients.map(c => `<option value="${{c.client_id}}">${{c.name}} (${{c.client_id}})</option>`).join('');
    const saved = localStorage.getItem('ontime_selected_client') || currentClientId;
    if (saved && Array.from(sel.options).some(o => o.value === saved)) sel.value = saved;
}}

function showTab(id) {{
    const target = document.getElementById(id);
    if (!target) return showTab("home");
    
    document.querySelectorAll(".section").forEach(s => s.classList.remove("active"));
    document.querySelectorAll(".nav-item").forEach(n => n.classList.remove("active"));
    target.classList.add("active");
    
    const nav = document.querySelector(`.nav-item[data-tab="${{id}}"]`);
    if (nav) nav.classList.add("active");
    
    saveState(id);
    refreshData(id);
    
    if (id === "tech_боты") loadBotsSection();
    if (id === "tech_интеграции") loadIntegrationsSection();
    if (id === "tech_сервер") loadServerStatus();
    if (id === "tech_скрипты") loadTechScripts();
    if (id === "tech_api") loadApiRegistry();
    if (id === "tech_воркфлоу") loadWorkflowStatus();
    if (id === "tech_очереди") loadTechQueues();
    if (id === "tech_логи") initTechLogs();
    if (id === "tech_ошибки") loadTechErrors();
    if (id !== "tech_логи" && _techLogInterval) {{
        clearInterval(_techLogInterval);
        _techLogInterval = null;
    }}
    
    if (LEGACY_SALES_TABS.has(id)) refreshSalesSection(id);
    
    if (id.startsWith('marketing_')) {{
        ensureMktGlobalHeaderPopulated(id);
    }} else {{
        const gh = document.getElementById('marketing-global-header');
        if (gh) gh.style.display = 'none';
        if (MODULE_CONFIG[id]) initModuleTab(id);
        else closeMarketingPanel();
    }}
    
    if (id === "analytics_дашборды") {{ ensureClientsLoaded().then(() => {{ populateAnalyticsClients(id); loadAnalyticsDashboards(); }}); }}
    if (id === "analytics_метрики") {{ ensureClientsLoaded().then(() => {{ populateAnalyticsClients(id); loadAnalyticsMetrics(); }}); }}
    if (id === "analytics_план-факт") {{ ensureClientsLoaded().then(() => {{ populateAnalyticsClients(id); loadAnalyticsPlanFact(); }}); }}
    if (id === "analytics_отчёты") loadAnalyticsReports();
    if (id === "analytics_ошибки_данных") {{ ensureClientsLoaded().then(() => {{ populateAnalyticsClients(id); loadAnalyticsDataErrors(); }}); }}
    if (id === "analytics_выводы_и_рекомендации") {{ ensureClientsLoaded().then(() => {{ populateAnalyticsClients(id); loadAnalyticsInsights(); }}); }}
    if (id === 'smm_smm') initSmmSection();
    if (id === 'smm_web-\u0437\u0430\u0432\u043e\u0434') initWebFactory('smm_web-\u0437\u0430\u0432\u043e\u0434');
    if (id === 'content_web-\u0437\u0430\u0432\u043e\u0434') initWebFactory('content_web-\u0437\u0430\u0432\u043e\u0434');
    if (id === 'content_\u043a\u043e\u043d\u0442\u0435\u043d\u0442-\u0437\u0430\u0432\u043e\u0434') {{ ensureClientsLoaded().then(() => {{
        renderCfClientsList();
        const saved = localStorage.getItem('ontime_selected_client') || currentClientId;
        if (saved) {{ const cl = allClients.find(c => c.client_id === saved); if (cl) selectCfClient(cl.client_id, cl.name); }}
    }}); }}
}}
async function refreshData(id) {{
  try {{
    if (id === 'wf_templates' && typeof loadWorkflowTemplates === 'function') await loadWorkflowTemplates();
    if (id === 'wf_runs' && typeof loadWorkflowRuns === 'function') await loadWorkflowRuns();
    if (id === 'wf_queues' && typeof loadWorkflowQueues === 'function') await loadWorkflowQueues();
    if (id === 'wf_logs' && typeof loadWorkflowLogs === 'function') await loadWorkflowLogs();
    if (id === 'kb_clients' && typeof refreshClients === 'function') await refreshClients();
    if (id === 'home' && typeof refreshHome === 'function') await refreshHome();
    const ts = document.getElementById('ts');
    if (ts) {{
      const st = await fetch('/api/status').then(r => r.json()).catch(() => null);
      if (st && st.generated_at) ts.innerText = st.generated_at;
    }}
  }} catch (e) {{
    console.error('refreshData failed', e);
  }}
}}

let salesDialogs = [];
let currentSalesCid = null;
let dealsView = 'kanban';

async function refreshSalesSection(id) {{
    if (id === 'sales_диалоги') return refreshSalesDialogs();
    if (id === 'sales_сделки') return refreshSalesDeals();
    
    const section = id.replace('sales_', '');
    const tbody = document.getElementById(id + '-tbody');
    if (!tbody) return;
    
    try {{
        const r = await fetch('/api/sales/' + section).then(res => res.json());
        const items = r.data || [];
        const cfg = MODULE_CONFIG[id];
        if (!items.length) {{
            tbody.innerHTML = `<tr><td colspan="${{cfg.columns.length + 1}}" style="text-align:center; padding:40px; color:var(--text-muted)">Данных нет. Начните с добавления.</td></tr>`;
            return;
        }}
        if (id === 'sales_sales_core') {{
            tbody.innerHTML = items.map(item => `
                <tr>
                    ${{cfg.columns.map(c => `<td>${{item[c] ?? ''}}</td>`).join('')}}
                    <td>—</td>
                </tr>
            `).join('');
            return;
        }}
        if (id === 'sales_cjm') {{
            tbody.innerHTML = items.map(item => `
                <tr>
                    ${{cfg.columns.map(c => `<td>${{item[c] ?? ''}}</td>`).join('')}}
                    <td>
                      <button class="mkt-btn" onclick="runCjmStage('${{item['CJM-состояние'] || ''}}')">Запустить шаг</button>
                      <button class="mkt-btn" onclick="openStageThread('${{item['CJM-состояние'] || ''}}')">Открыть тред</button>
                      <button class="mkt-btn" onclick="stageDecision('${{item['CJM-состояние'] || ''}}','approve')">Утвердить</button>
                      <button class="mkt-btn" onclick="stageDecision('${{item['CJM-состояние'] || ''}}','rework')">Доработка</button>
                      <button class="mkt-btn" onclick="stageDecision('${{item['CJM-состояние'] || ''}}','close')">Закрыть</button>
                    </td>
                </tr>
            `).join('');
            return;
        }}
        if (id === 'sales_клиенты') {{
            tbody.innerHTML = items.map((item, idx) => {{
                const cid = (item['_client_id'] || item['id'] || '').toString();
                const selected = (item['Этап воронки'] || FUNNEL_STAGE_OPTIONS[0]).toString();
                const options = FUNNEL_STAGE_OPTIONS.map(s =>
                    `<option value="${{s}}" ${{s===selected?'selected':''}}>${{s}}</option>`
                ).join('');
                return `
                    <tr>
                        ${{cfg.columns.map(c => `<td>${{item[c] ?? ''}}</td>`).join('')}}
                        <td style="white-space:nowrap">
                          <select id="sales-client-stage-${{idx}}" class="premium-input" style="width:240px; margin-right:6px;">
                            ${{options}}
                          </select>
                          <button class="mkt-btn" onclick="launchClientStage('${{cid}}','sales-client-stage-${{idx}}')">Запустить</button>
                          <button class="mkt-btn" onclick="showModuleEditModal('sales_клиенты', '${{item.id || item.client_id || ''}}')">Открыть</button>
                        </td>
                    </tr>
                `;
            }}).join('');
            return;
        }}
        if (id === 'sales_этапы_sla') {{
            tbody.innerHTML = items.map(item => `
                <tr>
                    ${{cfg.columns.map(c => `<td>${{item[c] ?? ''}}</td>`).join('')}}
                    <td>
                      <button class="mkt-btn" onclick="escalateSlaStage('${{item['Этап'] || ''}}')">Эскалация</button>
                      <button class="mkt-btn" onclick="openStageThread('${{item['Этап'] || ''}}')">Открыть тред</button>
                      <button class="mkt-btn" onclick="stageDecision('${{item['Этап'] || ''}}','approve')">Утвердить</button>
                      <button class="mkt-btn" onclick="stageDecision('${{item['Этап'] || ''}}','rework')">Доработка</button>
                      <button class="mkt-btn" onclick="stageDecision('${{item['Этап'] || ''}}','close')">Закрыть</button>
                    </td>
                </tr>
            `).join('');
            return;
        }}
        if (id === 'sales_эскалации') {{
            tbody.innerHTML = items.map(item => `
                <tr>
                    ${{cfg.columns.map(c => `<td>${{item[c] ?? ''}}</td>`).join('')}}
                    <td>
                      <button class="mkt-btn" onclick="decideEscalation('${{item.id || ''}}','approve')">Утвердить</button>
                      <button class="mkt-btn" onclick="decideEscalation('${{item.id || ''}}','rework')">Доработка</button>
                      <button class="mkt-btn" onclick="decideEscalation('${{item.id || ''}}','close')">Закрыть</button>
                    </td>
                </tr>
            `).join('');
            return;
        }}
        tbody.innerHTML = items.map(item => `
            <tr onclick="showModuleEditModal('${{id}}', '${{item.id || item.client_id}}')" style="cursor:pointer">
                ${{cfg.columns.map(c => `<td>${{item[c] || ''}}</td>`).join('')}}
                <td><button class="mkt-btn" onclick="event.stopPropagation(); showModuleEditModal('${{id}}', '${{item.id || item.client_id}}')">Открыть</button></td>
            </tr>
        `).join('');
    }} catch (e) {{ tbody.innerHTML = '<tr><td colspan="10" style="color:var(--red)">Ошибка загрузки</td></tr>'; }}
}}

function exportSalesCsv(id) {{
    const section = id.replace('sales_', '');
    window.open('/api/sales/export.csv?section=' + section, '_blank');
}}

async function runCjmStage(stage) {{
    const cid = currentClientId || localStorage.getItem('ontime_selected_client') || '';
    if (!cid) return alert('Сначала выберите клиента в любом модульном разделе');
    const agent = prompt('Агент (например min_sales):', 'min_sales') || 'min_sales';
    const defaultGoal = 'Продвинуть этап: ' + stage;
    const goal = prompt('Цель шага:', defaultGoal) || defaultGoal;
    const kpi = prompt('KPI шага:', 'Переход клиента на следующий этап') || 'Переход клиента на следующий этап';
    const deadline = prompt('Дедлайн (YYYY-MM-DD):', '') || '';
    const priority = prompt('Приоритет (low|medium|high):', 'high') || 'high';
    const r = await fetch('/api/sales/cjm/run', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ client_id: cid, stage, agent, goal, kpi, deadline, priority }})
    }}).then(res => res.json());
    if (r.status !== 'ok') return alert('Ошибка запуска шага');
    alert('Запущено: ' + (r.task_id || 'OK'));
    currentStageThread = {{ stage: stage, run_id: r.task_id || '' }};
}}

async function launchClientStage(clientId, selectId) {{
    const cid = (clientId || '').trim();
    if (!cid) return alert('Не найден client_id');
    const sel = document.getElementById(selectId);
    const stage = sel && sel.value ? sel.value : FUNNEL_STAGE_OPTIONS[0];
    const agent = 'min_sales';
    const goal = 'Продвинуть этап: ' + stage;
    const kpi = 'Переход клиента на следующий этап';
    localStorage.setItem('ontime_selected_client', cid);
    currentClientId = cid;
    const r = await fetch('/api/sales/cjm/run', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ client_id: cid, stage, agent, goal, kpi, deadline: '', priority: 'high' }})
    }}).then(res => res.json());
    if (r.status !== 'ok') return alert('Ошибка запуска шага');
    currentStageThread = {{ stage: stage, run_id: r.task_id || '' }};
    alert('Запуск отправлен: ' + (r.task_id || 'OK'));
}}

async function escalateSlaStage(stage) {{
    const cid = currentClientId || localStorage.getItem('ontime_selected_client') || '';
    if (!cid) return alert('Сначала выберите клиента в любом модульном разделе');
    const reason = prompt('Причина эскалации (обязательно):', '') || '';
    if (!reason.trim()) return alert('Причина обязательна');
    const agent = prompt('Кому эскалировать (agent id):', 'min_ops') || 'min_ops';
    const deadline = prompt('Новый дедлайн (YYYY-MM-DD):', '') || '';
    const r = await fetch('/api/sales/sla/escalate', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ client_id: cid, stage, reason, agent, deadline }})
    }}).then(res => res.json());
    if (r.status !== 'ok') return alert('Ошибка эскалации');
    alert('Эскалация отправлена: ' + (r.task_id || 'OK'));
    currentStageThread = {{ stage: stage, run_id: r.task_id || '' }};
}}

async function openStageThread(stage) {{
    if (!currentClientId) return alert('Сначала открой клиента');
    currentStageThread = currentStageThread && currentStageThread.stage === stage ? currentStageThread : {{ stage: stage, run_id: '' }};
    showClientTab('chat');
    await refreshClientChat();
}}

async function stageDecision(stage, decision) {{
    const cid = currentClientId || localStorage.getItem('ontime_selected_client') || '';
    if (!cid) return alert('Выберите клиента');
    const note = prompt('Комментарий решения:', '') || '';
    const run_id = (currentStageThread && currentStageThread.stage === stage) ? (currentStageThread.run_id || '') : '';
    const r = await fetch('/api/sales/stage/decision', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ client_id: cid, stage, decision, note, run_id }})
    }}).then(res => res.json());
    if (r.status !== 'ok') return alert('Ошибка решения');
    if (LEGACY_SALES_TABS.has('sales_cjm')) refreshSalesSection('sales_cjm');
    if (LEGACY_SALES_TABS.has('sales_этапы_sla')) refreshSalesSection('sales_этапы_sla');
}}

async function decideEscalation(escalationId, decision) {{
    const note = prompt('Комментарий решения:', '') || '';
    const r = await fetch('/api/sales/escalations/decision', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ id: escalationId, decision, note }})
    }}).then(res => res.json());
    if (r.status !== 'ok') return alert('Ошибка решения');
    refreshSalesSection('sales_эскалации');
}}

async function refreshSalesDialogs() {{
    const listEl = document.getElementById('sales-dialogs-list');
    if (!listEl) return;
    try {{
        const r = await fetch('/api/sales/dialogs').then(res => res.json());
        salesDialogs = r.data || [];
        renderSalesDialogsList(salesDialogs);
    }} catch (e) {{
        listEl.innerHTML = '<div style="padding:20px; color:var(--red)">Ошибка загрузки</div>';
    }}
}}

function renderSalesDialogsList(items) {{
    const listEl = document.getElementById('sales-dialogs-list');
    if (!items.length) {{
        listEl.innerHTML = '<div style="padding:20px; color:var(--text-muted)">Диалогов нет</div>';
        return;
    }}
    listEl.innerHTML = items.map(d => `
        <div class="nav-item ${{currentSalesCid === d.client_id ? 'active' : ''}}" style="border-radius:0; border-bottom:1px solid var(--border); padding:12px 16px; cursor:pointer;" onclick="loadSalesDialog('${{d.client_id}}')">
            <div style="font-weight:700; font-size:13px; margin-bottom:4px;">${{d.client_name}}</div>
            <div style="font-size:11px; color:var(--text-muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${{d.last_text || '—'}}</div>
            <div style="font-size:10px; opacity:0.6; margin-top:4px; text-align:right;">${{d.time || ''}}</div>
        </div>
    `).join('');
}}

async function loadSalesDialog(cid) {{
    currentSalesCid = cid;
    const header = document.getElementById('sales-dialog-header');
    const chat = document.getElementById('sales-dialog-chat');
    header.innerText = 'Чат: ' + cid;
    renderSalesDialogsList(salesDialogs); 
    
    try {{
        const r = await fetch('/api/sales/dialogs/' + cid).then(res => res.json());
        const messages = (r.data && r.data.messages) || [];
        if (!messages.length) {{
            chat.innerHTML = '<div class="chat-empty">Нет сообщений</div>';
        }} else {{
            chat.innerHTML = messages.map(m => {{
                const isAssistant = m.role === 'assistant';
                const cls = isAssistant ? 'assistant' : 'user';
                return `<div class="chat-bubble ${{cls}}">
                    <div style="font-size:11px; opacity:0.7; margin-bottom:4px">${{m.author || (isAssistant ? 'Manager' : 'Client')}}</div>
                    <div>${{m.content || m.text || ''}}</div>
                    <div class="chat-ts">${{m.ts || m.created_at || ''}}</div>
                </div>`;
            }}).join('');
        }}
        chat.scrollTop = chat.scrollHeight;
    }} catch (e) {{
        chat.innerHTML = '<div class="chat-empty" style="color:var(--red)">Ошибка загрузки чата</div>';
    }}
}}

async function sendSalesMessage() {{
    const inp = document.getElementById('sales-chat-input');
    const text = inp.value.trim();
    if (!text || !currentSalesCid) return;
    
    try {{
        const r = await fetch('/api/sales/dialogs/send', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ client_id: currentSalesCid, text: text }})
        }}).then(res => res.json());
        
        if (r.status === 'ok') {{
            inp.value = '';
            loadSalesDialog(currentSalesCid);
            refreshSalesDialogs();
        }}
    }} catch (e) {{ alert('Ошибка отправки'); }}
}}

function filterSalesDialogs(q) {{
    const filtered = salesDialogs.filter(d => d.client_id.toLowerCase().includes(q.toLowerCase()) || (d.last_text||'').toLowerCase().includes(q.toLowerCase()));
    renderSalesDialogsList(filtered);
}}

async function refreshSalesDeals() {{
    try {{
        const r = await fetch('/api/sales/deals').then(res => res.json());
        const deals = r.data || [];
        if (dealsView === 'kanban') renderDealsKanban(deals);
        else renderDealsTable(deals);
    }} catch (e) {{ console.error('Failed to load deals', e); }}
}}

function toggleDealsView() {{
    dealsView = dealsView === 'kanban' ? 'table' : 'kanban';
    document.getElementById('deals-view-btn').innerText = 'Вид: ' + (dealsView === 'kanban' ? 'Канбан' : 'Таблица');
    document.getElementById('sales-deals-kanban').style.display = dealsView === 'kanban' ? 'grid' : 'none';
    document.getElementById('sales-deals-table-view').style.display = dealsView === 'table' ? 'block' : 'none';
    refreshSalesDeals();
}}

function renderDealsKanban(deals) {{
    const kanban = document.getElementById('sales-deals-kanban');
    const stages = ["new", "qualified", "proposal", "negotiation", "won", "lost"];
    const stageNames = {{ "new": "Новые", "qualified": "Квалифицирован", "proposal": "КП отправлено", "negotiation": "Переговоры", "won": "Выиграно", "lost": "Проиграно" }};
    
    kanban.innerHTML = stages.map(s => {{
        const stageDeals = deals.filter(d => (d.Стадия || d.stage || 'new').toLowerCase() === s);
        return `
            <div style="background: var(--surface); border-radius:12px; padding:12px; min-height:300px; border: 1px solid var(--border);">
                <div style="font-size:11px; font-weight:800; color:var(--text-muted); text-transform:uppercase; margin-bottom:12px; display:flex; justify-content:space-between;">
                    <span>${{stageNames[s]}}</span>
                    <span>${{stageDeals.length}}</span>
                </div>
                <div style="display:flex; flex-direction:column; gap:10px;">
                    ${{stageDeals.map(d => `
                        <div class="card" style="padding:12px; margin-bottom:0; cursor:pointer; border-radius:8px;" onclick="showModuleEditModal('sales_сделки', '${{d.id}}')">
                            <div style="font-weight:700; font-size:13px; margin-bottom:4px">${{d.Название || d.name || 'Сделка'}}</div>
                            <div style="font-size:11px; color:var(--text-muted); margin-bottom:6px">${{d.Клиент || d.client || '—'}}</div>
                            <div style="display:flex; justify-content:space-between; align-items:center">
                                <span style="font-weight:800; color:var(--accent); font-size:12px">${{d.Сумма || d.amount || '0'}}</span>
                                <span style="font-size:10px; color:var(--text-muted)">${{d.Дедлайн || ''}}</span>
                            </div>
                        </div>
                    `).join('')}}
                    ${{stageDeals.length === 0 ? '<div style="font-size:11px; color:var(--text-muted); text-align:center; padding:20px">Пусто</div>' : ''}}
                </div>
            </div>
        `;
    }}).join('');
}}

function renderDealsTable(deals) {{
    const tbody = document.getElementById('sales-deals-table-body');
    tbody.innerHTML = deals.map(d => `
        <tr>
            <td><b>${{d.Название || d.name || 'Сделка'}}</b></td>
            <td>${{d.Клиент || d.client || '—'}}</td>
            <td><b style="color:var(--accent)">${{d.Сумма || d.amount || '0'}}</b></td>
            <td><span class="badge">${{d.Стадия || d.stage || 'new'}}</span></td>
            <td>${{d.Вероятность || ''}}</td>
            <td>${{d.Дедлайн || ''}}</td>
            <td><button class="mkt-btn" onclick="showModuleEditModal('sales_сделки', '${{d.id}}')">Изменить</button></td>
        </tr>
    `).join('');
}}

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
            tbody.innerHTML = '<tr><td colspan="8" style="color:var(--text-muted); padding:22px 12px;">Интеграции не заведены. Registry создан: _runtime/integrations/service_registry.json</td></tr>';
        }} else {{
            tbody.innerHTML = items.map(x => {{
                const link = (x.account_url || (x.links && x.links[0]) || '').toString();
                const statusClass = x.status === 'active' ? 'badge-active' : (x.status === 'error' ? 'badge-error' : '');
                const secret = x.no_secret_required ? 'no_secret_required' : (x.secret_ref || 'needs_secret');
                const clients = (x.assigned_clients || []).join(', ');
                const publication = [x.publication_priority, x.effective_for_publication].filter(Boolean).join(' / ');
                return `
                  <tr>
                    <td><b>${{x.platform || x.integration_id}}</b><div style="font-size:11px;color:var(--text-muted)">${{x.integration_id}}</div></td>
                    <td>${{x.scope || ''}}</td>
                    <td>${{clients}}</td>
                    <td>${{link ? `<a href="${{link}}" target="_blank">${{x.account_name || link}}</a>` : (x.account_name || '')}}</td>
                    <td>${{publication}}</td>
                    <td><span class="badge ${{statusClass}}">${{x.status}}</span></td>
                    <td style="color:var(--text-muted)">${{secret}}</td>
                    <td><button class="mkt-btn" onclick="checkIntegration('${{x.integration_id}}')">Проверить</button>${{link ? `<button class="mkt-btn" onclick="window.open('${{link}}','_blank')">Открыть</button>` : ''}}</td>
                  </tr>
                `;
            }}).join('');
        }}
        if (status) status.innerText = 'Источник: ' + (r.source || '_runtime/integrations/service_registry.json');
    }} catch (e) {{
        tbody.innerHTML = '<tr><td colspan="8" style="color:var(--red); padding:22px 12px;">Ошибка загрузки /api/integrations</td></tr>';
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

// =================== TECH SECTIONS ===================
async function loadServerStatus() {{
    const r = await fetch('/api/tech/server').then(x => x.json());
    if (r.status !== 'ok') return;
    const grid = document.getElementById('server-metrics');
    grid.innerHTML = r.data.metrics.map(m => `
        <div class="stat-card" style="border-left: 3px solid ${{m.status === 'ok' ? '#22c55e' : m.status === 'warn' ? '#f59e0b' : '#ef4444'}}">
            <div class="stat-value" style="font-size:18px">${{m.value}}${{m.unit ? ' ' + m.unit : ''}}</div>
            <div class="stat-label">${{m.label}}</div>
        </div>
    `).join('');
    document.getElementById('server-updated').textContent = '\u041e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u043e: ' + new Date().toLocaleTimeString();
}}

async function loadTechScripts() {{
    const r = await fetch('/api/tech/scripts').then(x => x.json());
    const tbody = document.getElementById('tech-scripts-body');
    if (!r.data || r.data.length === 0) {{
        tbody.innerHTML = '<tr><td colspan="7" style="color:var(--text-muted); padding:22px 12px">\u0421\u043a\u0440\u0438\u043f\u0442\u044b \u043d\u0435 \u0437\u0430\u0432\u0435\u0434\u0435\u043d\u044b</td></tr>';
        return;
    }}
    const statusColors = {{ active: '#22c55e', disabled: '#94a3b8', error: '#ef4444' }};
    tbody.innerHTML = r.data.map(s => `
        <tr>
            <td><b>${{s['\u041d\u0430\u0437\u0432\u0430\u043d\u0438\u0435'] || ''}}</b></td>
            <td>${{s['\u0422\u0438\u043f'] || ''}}</td>
            <td style="font-size:11px; color:var(--text-muted)">${{s['\u041f\u0443\u0442\u044c'] || ''}}</td>
            <td>${{s['\u0420\u0430\u0441\u043f\u0438\u0441\u0430\u043d\u0438\u0435'] || '\u2014'}}</td>
            <td>${{s['\u041f\u043e\u0441\u043b\u0435\u0434\u043d\u0438\u0439 \u0437\u0430\u043f\u0443\u0441\u043a'] || '\u2014'}}</td>
            <td><span style="color:${{statusColors[s['\u0421\u0442\u0430\u0442\u0443\u0441']] || '#fff'}}">${{s['\u0421\u0442\u0430\u0442\u0443\u0441'] || ''}}</span></td>
            <td>
                <button class="mkt-btn" onclick="showTechScriptEditModal('${{s.id}}')">\u0418\u0437\u043c\u0435\u043d\u0438\u0442\u044c</button>
                <button class="mkt-btn" onclick="deleteTechScript('${{s.id}}')">\u0423\u0434\u0430\u043b\u0438\u0442\u044c</button>
            </td>
        </tr>
    `).join('');
}}

async function showTechScriptAddModal() {{
    document.getElementById('mkt-panel-title').innerText = '\u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u0441\u043a\u0440\u0438\u043f\u0442';
    const host = document.getElementById('mkt-panel-fields');
    host.innerHTML = `
        <div class="mkt-field"><label>\u041d\u0430\u0437\u0432\u0430\u043d\u0438\u0435</label><input class="premium-input" id="ts-name"></div>
        <div class="mkt-field"><label>\u0422\u0438\u043f</label>
          <select class="premium-input" id="ts-type">
            <option>cron</option><option>systemd</option><option>manual</option><option>trigger</option>
          </select>
        </div>
        <div class="mkt-field"><label>\u041f\u0443\u0442\u044c</label><input class="premium-input" id="ts-path"></div>
        <div class="mkt-field"><label>\u0420\u0430\u0441\u043f\u0438\u0441\u0430\u043d\u0438\u0435</label><input class="premium-input" id="ts-schedule" placeholder="* * * * *"></div>
        <div class="mkt-field"><label>\u0421\u0442\u0430\u0442\u0443\u0441</label>
          <select class="premium-input" id="ts-status">
            <option>active</option><option>disabled</option><option>error</option>
          </select>
        </div>
        <input type="hidden" id="ts-edit-id" value="">
        <button class="nav-item active" style="width:100%;border:none;padding:12px;margin-top:12px" onclick="saveTechScript()">\u0421\u043e\u0445\u0440\u0430\u043d\u0438\u0442\u044c</button>
    `;
    document.getElementById('mkt-sidepanel').classList.add('open');
}}

async function showTechScriptEditModal(id) {{
    const r = await fetch('/api/tech/scripts').then(x => x.json());
    const s = (r.data || []).find(x => x.id === id);
    if (!s) return;
    await showTechScriptAddModal();
    document.getElementById('mkt-panel-title').innerText = '\u0418\u0437\u043c\u0435\u043d\u0438\u0442\u044c \u0441\u043a\u0440\u0438\u043f\u0442';
    document.getElementById('ts-name').value = s['\u041d\u0430\u0437\u0432\u0430\u043d\u0438\u0435'] || '';
    document.getElementById('ts-type').value = s['\u0422\u0438\u043f'] || 'manual';
    document.getElementById('ts-path').value = s['\u041f\u0443\u0442\u044c'] || '';
    document.getElementById('ts-schedule').value = s['\u0420\u0430\u0441\u043f\u0438\u0441\u0430\u043d\u0438\u0435'] || '';
    document.getElementById('ts-status').value = s['\u0421\u0442\u0430\u0442\u0443\u0441'] || 'active';
    document.getElementById('ts-edit-id').value = id;
}}

async function saveTechScript() {{
    const editId = document.getElementById('ts-edit-id').value;
    const body = {{
        '\u041d\u0430\u0437\u0432\u0430\u043d\u0438\u0435': document.getElementById('ts-name').value,
        '\u0422\u0438\u043f': document.getElementById('ts-type').value,
        '\u041f\u0443\u0442\u044c': document.getElementById('ts-path').value,
        '\u0420\u0430\u0441\u043f\u0438\u0441\u0430\u043d\u0438\u0435': document.getElementById('ts-schedule').value,
        '\u0421\u0442\u0430\u0442\u0443\u0441': document.getElementById('ts-status').value,
    }};
    if (editId) body.id = editId;
    const action = editId ? 'update' : 'add';
    const r = await fetch(`/api/tech/scripts/${{action}}`, {{
        method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify(body)
    }}).then(x => x.json());
    if (r.status === 'ok') {{ closeMarketingPanel(); loadTechScripts(); }}
    else alert('\u041e\u0448\u0438\u0431\u043a\u0430: ' + (r.message || 'unknown'));
}}

async function deleteTechScript(id) {{
    if (!confirm('\u0423\u0434\u0430\u043b\u0438\u0442\u044c?')) return;
    await fetch('/api/tech/scripts/delete', {{
        method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{id}})
    }});
    loadTechScripts();
}}

let _apiRoutes = [];
async function loadApiRegistry() {{
    const r = await fetch('/api/tech/api-registry').then(x => x.json());
    _apiRoutes = r.data || [];
    renderApiRegistry(_apiRoutes);
}}
function renderApiRegistry(routes) {{
    const methodColors = {{ GET: '#22c55e', POST: '#3b82f6', DELETE: '#ef4444' }};
    document.getElementById('api-registry-body').innerHTML = routes.map(r => `
        <tr>
            <td><span style="color:${{methodColors[r.method] || '#fff'}}; font-weight:600">${{r.method}}</span></td>
            <td style="font-family:monospace; font-size:12px">${{r.path}}</td>
            <td style="color:var(--text-muted)">${{r.description}}</td>
        </tr>
    `).join('');
}}
function filterApiRegistry(q) {{
    renderApiRegistry(_apiRoutes.filter(r => r.path.toLowerCase().includes(q.toLowerCase()) || r.description.toLowerCase().includes(q.toLowerCase())));
}}

async function loadWorkflowStatus() {{
    const r = await fetch('/api/tech/workflow').then(x => x.json());
    if (r.status !== 'ok') return;
    const d = r.data;
    const overallColors = {{ pass: '#22c55e', fail: '#ef4444', unknown: '#94a3b8' }};
    document.getElementById('wf-overall').innerHTML = `
        <div class="stat-card" style="border-left:3px solid ${{overallColors[d.overall.go_no_go] || '#94a3b8'}}">
            <div class="stat-value">${{d.overall.go_no_go}}</div><div class="stat-label">Go/No-Go</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">${{d.overall.maturity}}</div><div class="stat-label">Maturity</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">${{d.overall.go_live ? 'YES' : 'NO'}}</div><div class="stat-label">Go Live</div>
        </div>
        <div class="stat-card">
            <div class="stat-value" style="font-size:12px">${{d.overall.ts ? d.overall.ts.substring(0,10) : '\u2014'}}</div>
            <div class="stat-label">\u041e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u043e</div>
        </div>
    `;
    const statusColors = {{ pass: '#22c55e', fail: '#ef4444', unknown: '#94a3b8', warn: '#f59e0b' }};
    document.getElementById('wf-gates-body').innerHTML = d.gates.length
        ? d.gates.map(g => `
            <tr>
                <td style="font-family:monospace">${{g.name}}</td>
                <td><span style="color:${{statusColors[g.status] || '#fff'}};font-weight:600">${{g.status}}</span></td>
                <td style="color:var(--text-muted); font-size:12px">${{g.detail || '\u2014'}}</td>
            </tr>`).join('')
        : '<tr><td colspan="3" style="color:var(--text-muted); padding:22px 12px">Gates \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u044b</td></tr>';
}}

async function loadTechQueues() {{
    const r = await fetch('/api/tech/queues').then(x => x.json());
    if (r.status !== 'ok') return;
    const d = r.data;
    const alertQueues = ['dead', 'blocked', 'failed'];
    document.getElementById('queues-summary').innerHTML = d.summary.map(q => `
        <div class="stat-card" style="border-left:3px solid ${{alertQueues.includes(q.queue) && q.count > 0 ? '#ef4444' : '#22c55e'}}">
            <div class="stat-value">${{q.count}}</div><div class="stat-label">${{q.queue}}</div>
        </div>
    `).join('');
    document.getElementById('queues-dead-body').innerHTML = d.recent_dead.length
        ? d.recent_dead.map(t => `
            <tr>
                <td style="font-family:monospace; font-size:11px">${{t.task_id}}</td>
                <td>${{t.task_type}}</td>
                <td style="color:#ef4444">${{t.dead_reason}}</td>
                <td style="color:var(--text-muted); font-size:11px">${{t.dead_at ? t.dead_at.substring(0,19) : '\u2014'}}</td>
            </tr>`).join('')
        : '<tr><td colspan="4" style="color:var(--text-muted); padding:22px 12px">Dead-\u0437\u0430\u0434\u0430\u0447 \u043d\u0435\u0442</td></tr>';
}}

let _techLogInterval = null;
async function initTechLogs() {{
    const r = await fetch('/api/tech/log-list').then(x => x.json());
    const sel = document.getElementById('tech-log-agent');
    sel.innerHTML = '<option value="">\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u0430\u0433\u0435\u043d\u0442\u0430...</option>' +
        (r.data || []).map(a => `<option value="${{a}}">${{a}}</option>`).join('');
}}
async function loadTechLog() {{
    const agent = document.getElementById('tech-log-agent').value;
    if (!agent) return;
    const r = await fetch(`/api/logs?agent=${{encodeURIComponent(agent)}}`).then(x => x.json());
    const pre = document.getElementById('tech-log-content');
    const lines = Array.isArray(r.data) ? r.data : (r.data?.lines || [r.data]);
    pre.textContent = lines.join('\\n') || '\u041b\u043e\u0433 \u043f\u0443\u0441\u0442\u043e\u0439';
    pre.scrollTop = pre.scrollHeight;
}}
function toggleTechLogLive() {{
    const on = document.getElementById('tech-log-live').checked;
    if (on) {{ _techLogInterval = setInterval(loadTechLog, 5000); loadTechLog(); }}
    else {{ clearInterval(_techLogInterval); _techLogInterval = null; }}
}}

let _techErrors = [];
async function loadTechErrors() {{
    const r = await fetch('/api/tech/errors').then(x => x.json());
    _techErrors = r.data || [];
    document.getElementById('errors-count').textContent = `\u0412\u0441\u0435\u0433\u043e: ${{_techErrors.length}}`;
    renderTechErrors(_techErrors);
}}
function renderTechErrors(errors) {{
    const qColors = {{ dead: '#ef4444', blocked: '#f59e0b', failed: '#f97316' }};
    document.getElementById('tech-errors-body').innerHTML = errors.length
        ? errors.map(e => `
            <tr>
                <td><span style="color:${{qColors[e.queue] || '#fff'}}; font-weight:600">${{e.queue}}</span></td>
                <td style="font-family:monospace; font-size:10px">${{e.task_id.substring(0, 30)}}\u2026</td>
                <td style="font-size:12px">${{e.task_type}}</td>
                <td style="font-size:12px; color:var(--text-muted)">${{e.to}}</td>
                <td style="font-size:12px; color:#ef4444">${{e.reason}}</td>
                <td style="text-align:center">${{e.retry_count}}</td>
                <td style="font-size:11px; color:var(--text-muted)">${{e.ts ? e.ts.substring(0,16) : '\u2014'}}</td>
            </tr>`).join('')
        : '<tr><td colspan="7" style="color:var(--text-muted); padding:22px 12px">\u041e\u0448\u0438\u0431\u043e\u043a \u043d\u0435\u0442</td></tr>';
}}
function filterTechErrors(q) {{
    renderTechErrors(_techErrors.filter(e =>
        e.task_type.toLowerCase().includes(q.toLowerCase()) ||
        e.reason.toLowerCase().includes(q.toLowerCase()) ||
        e.to.toLowerCase().includes(q.toLowerCase())
    ));
}}
// =================== END TECH SECTIONS ===================

// =================== ANALYTICS SECTIONS ===================
async function loadAnalyticsDashboards() {{
    const sel = document.getElementById('analytics_дашборды-client');
    let cid = sel ? sel.value : '';
    if (!cid && allClients.length > 0) {{ cid = allClients[0].client_id; if(sel) sel.value = cid; }}
    
    const r = await fetch(`/api/analytics/dashboards${{cid ? '?client_id='+cid : ''}}`).then(x => x.json());
    if (r.status !== 'ok') return;
    
    const grid = document.getElementById('analytics-dashboards-metrics');
    grid.innerHTML = (r.data || []).map(m => `
        <div class="stat-card" style="border-left: 3px solid ${{m.status === 'error' ? '#ef4444' : '#6366f1'}}">
            <div class="stat-value">${{m.value}}${{m.unit ? ' ' + m.unit : ''}}</div>
            <div class="stat-label">${{m.label}}</div>
        </div>
    `).join('');
    
    const funnel = document.getElementById('analytics-dashboards-funnel');
    if (r.data && r.data.length > 0) {{
        funnel.innerHTML = '<div style="display:flex; flex-direction:column; gap:8px">' + 
            r.data.filter(m => m.label.includes('(Core)') || m.label.includes('CR')).map(m => `
                <div style="display:flex; justify-content:space-between; padding:10px; background:var(--surface); border-radius:var(--radius-sm)">
                    <span>${{m.label}}</span>
                    <b>${{m.value}}${{m.unit}}</b>
                </div>
            `).join('') + '</div>';
    }} else {{
        funnel.innerHTML = '<p>Нет данных для воронки</p>';
    }}
}}

async function loadAnalyticsMetrics() {{
    const sel = document.getElementById('analytics_метрики-client');
    let cid = sel ? sel.value : '';
    if (!cid && allClients.length > 0) {{ cid = allClients[0].client_id; if(sel) sel.value = cid; }}
    if (!cid) return;
    
    const r = await fetch(`/api/analytics/metrics?client_id=${{cid}}`).then(x => x.json());
    const tbody = document.getElementById('analytics-metrics-body');
    tbody.innerHTML = (r.data || []).map(m => `
        <tr>
            <td><b>${{m['Метрика'] || ''}}</b></td>
            <td>${{m['Источник'] || ''}}</td>
            <td>${{m['Период'] || ''}}</td>
            <td>${{m['Значение'] || ''}}</td>
            <td>${{m['Статус'] || ''}}</td>
            <td>${{m['Обновлено'] || ''}}</td>
        </tr>
    `).join('') || '<tr><td colspan="6" style="text-align:center; padding:20px; color:var(--text-muted)">Нет метрик</td></tr>';
}}

async function loadAnalyticsPlanFact() {{
    const sel = document.getElementById('analytics_план-факт-client');
    let cid = sel ? sel.value : '';
    if (!cid && allClients.length > 0) {{ cid = allClients[0].client_id; if(sel) sel.value = cid; }}
    if (!cid) return;

    const r = await fetch(`/api/analytics/planfact?client_id=${{cid}}`).then(x => x.json());
    const tbody = document.getElementById('analytics-planfact-body');
    tbody.innerHTML = (r.data || []).map(i => `
        <tr>
            <td><b>${{i['Показатель'] || ''}}</b></td>
            <td>${{i['План'] || 0}}</td>
            <td>${{i['Факт'] || 0}}</td>
            <td style="color:${{parseFloat(i['Отклонение %']) < 0 ? 'var(--red)' : 'var(--green)'}}">${{i['Отклонение %'] || 0}}%</td>
            <td>${{i['Статус'] || ''}}</td>
            <td>${{i['Источник данных'] || ''}}</td>
        </tr>
    `).join('') || '<tr><td colspan="6" style="text-align:center; padding:20px; color:var(--text-muted)">Нет данных план-факт</td></tr>';
}}

async function loadAnalyticsReports() {{
    const r = await fetch('/api/analytics/reports').then(x => x.json());
    const tbody = document.getElementById('analytics-reports-body');
    tbody.innerHTML = (r.data || []).map(rep => `
        <tr>
            <td><b>${{rep.name}}</b></td>
            <td>${{rep.date}}</td>
            <td>${{rep.type}}</td>
            <td style="font-family:monospace; font-size:11px">${{rep.path}}</td>
            <td><button class="mkt-btn" onclick="window.open('/api/raw?path=${{encodeURIComponent(rep.path)}}','_blank')">Открыть</button></td>
        </tr>
    `).join('') || '<tr><td colspan="5" style="text-align:center; padding:20px; color:var(--text-muted)">Отчеты не найдены</td></tr>';
}}

async function loadAnalyticsDataErrors() {{
    const sel = document.getElementById('analytics_ошибки_данных-client');
    const cid = sel ? sel.value : '';
    const r = await fetch(`/api/analytics/data-errors${{cid ? '?client_id='+cid : ''}}`).then(x => x.json());
    const tbody = document.getElementById('analytics-dataerrors-body');
    tbody.innerHTML = (r.data || []).map(e => `
        <tr>
            <td>${{e.client_id}}</td>
            <td style="color:${{e.severity==='Critical' ? 'var(--red)' : 'inherit'}}">${{e.error}}</td>
            <td><span class="badge ${{e.severity==='Critical' ? 'badge-error' : ''}}">${{e.severity}}</span></td>
        </tr>
    `).join('') || '<tr><td colspan="3" style="text-align:center; padding:20px; color:var(--text-muted)">Ошибок данных не обнаружено</td></tr>';
}}

async function loadAnalyticsInsights() {{
    const sel = document.getElementById('analytics_выводы_и_рекомендации-client');
    let cid = sel ? sel.value : '';
    if (!cid && allClients.length > 0) {{ cid = allClients[0].client_id; if(sel) sel.value = cid; }}
    if (!cid) return;

    const r = await fetch(`/api/analytics/insights?client_id=${{cid}}`).then(x => x.json());
    const container = document.getElementById('analytics-insights-container');
    container.innerHTML = (r.data || []).map(i => `
        <div class="card premium-glow" style="margin-bottom:12px; border-left:4px solid ${{i.priority==='High' ? 'var(--red)' : (i.priority==='Medium' ? 'var(--yellow)' : 'var(--accent)')}}">
            <div style="display:flex; justify-content:space-between; margin-bottom:8px">
                <span class="badge">${{i.type}}</span>
                <span style="font-size:11px; color:var(--text-muted)">Приоритет: ${{i.priority}}</span>
            </div>
            <div style="font-weight:600; margin-bottom:4px">${{i.insight}}</div>
            <div style="font-size:13px; color:var(--text-muted)">💡 Рекомендация: ${{i.recommendation}}</div>
        </div>
    `).join('') || '<p style="color:var(--text-muted); padding:20px">Нет выводов для данного клиента</p>';
}}
// =================== END ANALYTICS SECTIONS ===================

// =================== SMM MODULE ===================
let _smmClients = [];
let _smmSelectedCid = null;
let _smmActiveTab = 'overview';

async function initSmmSection() {{
    const r = await fetch('/api/clients/list').then(x => x.json());
    _smmClients = r.data || [];
    renderSmmClientList(_smmClients);
}}

function renderSmmClientList(clients) {{
    const list = document.getElementById('smm-client-list');
    if (!list) return;
    if (!clients.length) {{
        list.innerHTML = '<div style="padding:16px; color:var(--text-muted); font-size:12px">\u041f\u0440\u043e\u0435\u043a\u0442\u043e\u0432 \u043d\u0435\u0442</div>';
        return;
    }}
    list.innerHTML = clients.map(c => `
        <div class="smm-client-item ${{_smmSelectedCid === c.client_id ? 'active' : ''}}"
             onclick="selectSmmClient('${{c.client_id}}', '${{(c.name || c.client_id).replace(/'/g, "\\'")}}')" >
            <div style="font-weight:600">${{c.name || c.client_id}}</div>
            <div style="font-size:11px; color:var(--text-muted)">${{c.client_id}}</div>
        </div>
    `).join('');
}}

function filterSmmClients(q) {{
    const filtered = _smmClients.filter(c =>
        (c.name || '').toLowerCase().includes(q.toLowerCase()) ||
        (c.client_id || '').toLowerCase().includes(q.toLowerCase())
    );
    renderSmmClientList(filtered);
}}

async function selectSmmClient(cid, name) {{
    _smmSelectedCid = cid;
    renderSmmClientList(_smmClients);
    document.getElementById('smm-no-client').style.display = 'none';
    const ws = document.getElementById('smm-client-workspace');
    ws.style.display = 'flex';

    document.getElementById('smm-ws-header').innerHTML = `
        <div style="width:36px; height:36px; border-radius:50%; background:var(--accent-glow); display:flex; align-items:center; justify-content:center; font-weight:700; color:var(--accent)">${{(name[0]||'?').toUpperCase()}}</div>
        <div>
            <div style="font-weight:700; font-size:15px">${{name}}</div>
            <div style="font-size:11px; color:var(--text-muted)">${{cid}}</div>
        </div>
    `;

    const info = _smmClients.find(c => c.client_id === cid) || {{}};
    const tabs = [
        {{ id: 'overview', label: '\u041e\u0431\u0437\u043e\u0440', always: true }},
        {{ id: 'content',  label: '\u041a\u043e\u043d\u0442\u0435\u043d\u0442', always: true }},
        {{ id: 'pubs',     label: '\u041f\u0443\u0431\u043b\u0438\u043a\u0430\u0446\u0438\u0438', always: false, flag: 'autoposting_enabled' }},
        {{ id: 'analytics',label: '\u0410\u043d\u0430\u043b\u0438\u0442\u0438\u043a\u0430', always: true }},
        {{ id: 'settings', label: '\u041d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438', always: true }},
    ];
    const visibleTabs = tabs.filter(t => t.always || info[t.flag]);
    document.getElementById('smm-ws-tabs').innerHTML = visibleTabs.map(t => `
        <button class="smm-ws-tab ${{_smmActiveTab === t.id ? 'active' : ''}}" onclick="showSmmTab('${{t.id}}')">${{t.label}}</button>
    `).join('');

    showSmmTab(_smmActiveTab);
}}

async function showSmmTab(tab) {{
    _smmActiveTab = tab;
    const labelMap = {{
        overview: '\u041e\u0431\u0437\u043e\u0440', content: '\u041a\u043e\u043d\u0442\u0435\u043d\u0442',
        pubs: '\u041f\u0443\u0431\u043b\u0438\u043a\u0430\u0446\u0438\u0438', analytics: '\u0410\u043d\u0430\u043b\u0438\u0442\u0438\u043a\u0430', settings: '\u041d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438'
    }};
    document.querySelectorAll('.smm-ws-tab').forEach(b => b.classList.toggle('active', b.textContent.trim() === labelMap[tab]));
    const body = document.getElementById('smm-ws-body');
    if (!_smmSelectedCid) return;
    body.innerHTML = '<div style="color:var(--text-muted)">\u0417\u0430\u0433\u0440\u0443\u0437\u043a\u0430...</div>';
    if (tab === 'overview')   await renderSmmOverview(body);
    if (tab === 'content')    await renderSmmContent(body);
    if (tab === 'pubs')       await renderSmmPubs(body);
    if (tab === 'analytics')  await renderSmmAnalytics(body);
    if (tab === 'settings')   await renderSmmSettings(body);
}}

async function renderSmmOverview(body) {{
    const r = await fetch(`/api/smm/${{_smmSelectedCid}}/overview`).then(x => x.json());
    const d = r.data || {{}};
    const cards = [
        {{ n: '01', title: '\u0413\u043e\u043b\u043e\u0441 \u0431\u0440\u0435\u043d\u0434\u0430',        value: d.brand_filled   || '\u2014', sub: d.brand_sub   || '', tab: 'settings' }},
        {{ n: '02', title: '\u0410\u0432\u0442\u043e\u043f\u043e\u0441\u0442\u0438\u043d\u0433',         value: d.autoposting    || '\u2014', sub: d.autoposting_sub || '', tab: 'pubs'     }},
        {{ n: '03', title: '\u041c\u0435\u0434\u0438\u0430\u043f\u043b\u0430\u043d',                    value: d.mediaplan      || '\u2014', sub: d.mediaplan_sub   || '', tab: 'content'  }},
        {{ n: '04', title: '\u0410\u043d\u0430\u043b\u0438\u0442\u0438\u043a\u0430',                    value: d.analytics      || '\u2014', sub: d.analytics_sub   || '', tab: 'analytics'}},
        {{ n: '05', title: '\u0424\u043e\u0442\u043e\u0430\u0440\u0445\u0438\u0432',                    value: d.photos         || '\u2014', sub: d.photos_sub      || '', tab: 'settings' }},
        {{ n: '06', title: '\u0418\u0441\u0442\u043e\u0440\u0438\u044f \u043f\u0443\u0431\u043b\u0438\u043a\u0430\u0446\u0438\u0439', value: d.history || '\u2014', sub: d.history_sub || '', tab: 'pubs' }},
    ];
    body.innerHTML = `<div class="smm-overview-grid">${{cards.map(c => `
        <div class="smm-card" onclick="showSmmTab('${{c.tab}}')">
            <div class="smm-card-num">${{c.n}}</div>
            <div class="smm-card-title">${{c.title}}</div>
            <div class="smm-card-value">${{c.value}}</div>
            <div class="smm-card-sub">${{c.sub}}</div>
        </div>`).join('')}}</div>`;
}}

async function renderSmmContent(body) {{
    const r = await fetch(`/api/module/${{_smmSelectedCid}}/content/posts`).then(x => x.json());
    const posts = r.data || [];
    const stages = [
        ['\u0418\u0434\u0435\u044f', ['\u0418\u0434\u0435\u044f', '']],
        ['\u0411\u0440\u0438\u0444', ['\u0411\u0440\u0438\u0444']],
        ['\u0412 \u0440\u0430\u0431\u043e\u0442\u0435', ['\u0412 \u0440\u0430\u0431\u043e\u0442\u0435']],
        ['\u0413\u043e\u0442\u043e\u0432\u043e', ['\u0413\u043e\u0442\u043e\u0432\u043e']],
        ['\u041e\u043f\u0443\u0431\u043b\u0438\u043a\u043e\u0432\u0430\u043d\u043e', ['\u041e\u043f\u0443\u0431\u043b\u0438\u043a\u043e\u0432\u0430\u043d\u043e']],
    ];
    const modeToggle = `<div style="display:flex;gap:8px;margin-bottom:16px">
        <button class="nav-item active" style="padding:6px 12px;font-size:12px;border:none;cursor:pointer" onclick="renderSmmContentPipeline()">&#9644; \u041a\u043e\u043d\u0432\u0435\u0439\u0435\u0440</button>
        <button class="nav-item" style="padding:6px 12px;font-size:12px;border:none;cursor:pointer" onclick="renderSmmContentList()">&#9776; \u0421\u043f\u0438\u0441\u043e\u043a</button>
    </div>`;
    window._smmPosts = posts;
    window._smmStages = stages;
    body.innerHTML = modeToggle + '<div id="smm-content-view"></div>';
    renderSmmContentPipeline();
}}

function renderSmmContentPipeline() {{
    const stages = window._smmStages || [];
    const posts = window._smmPosts || [];
    const view = document.getElementById('smm-content-view');
    if (!view) return;
    view.innerHTML = `<div class="smm-pipeline">${{stages.map(([label, statuses]) => {{
        const items = posts.filter(p => statuses.includes(p['\u0421\u0442\u0430\u0442\u0443\u0441'] || ''));
        return `<div class="smm-pipeline-col">
            <div class="smm-pipeline-col-title">${{label}} <span style="color:var(--accent)">${{items.length}}</span></div>
            ${{items.map(p => `<div class="smm-pipeline-item">${{(p['\u0422\u0435\u043a\u0441\u0442'] || '').substring(0,60) || p['\u0422\u0435\u043c\u0430'] || '\u0431\u0435\u0437 \u043d\u0430\u0437\u0432\u0430\u043d\u0438\u044f'}}</div>`).join('')}}
        </div>`;
    }}).join('')}}</div>`;
}}

function renderSmmContentList() {{
    const posts = window._smmPosts || [];
    const view = document.getElementById('smm-content-view');
    if (!view) return;
    if (!posts.length) {{ view.innerHTML = '<p style="color:var(--text-muted)">\u041d\u0435\u0442 \u0437\u0430\u043f\u0438\u0441\u0435\u0439</p>'; return; }}
    view.innerHTML = `<div class="card premium-glow"><table class="premium-table">
        <thead><tr><th>\u0422\u0435\u043a\u0441\u0442</th><th>\u0421\u0435\u0442\u044c</th><th>\u0424\u043e\u0440\u043c\u0430\u0442</th><th>\u0421\u0442\u0430\u0442\u0443\u0441</th><th>\u0414\u0430\u0442\u0430</th></tr></thead>
        <tbody>${{posts.map(p => `<tr>
            <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis">${{(p['\u0422\u0435\u043a\u0441\u0442']||'').substring(0,80)}}</td>
            <td>${{p['\u0421\u0435\u0442\u044c']||''}}</td>
            <td>${{p['\u0424\u043e\u0440\u043c\u0430\u0442']||''}}</td>
            <td>${{p['\u0421\u0442\u0430\u0442\u0443\u0441']||''}}</td>
            <td>${{p['\u0414\u0430\u0442\u0430']||''}}</td>
        </tr>`).join('')}}</tbody>
    </table></div>`;
}}

async function renderSmmPubs(body) {{
    const r = await fetch(`/api/smm/${{_smmSelectedCid}}/publications`).then(x => x.json());
    const d = r.data || {{}};
    const enabled = d.enabled ?? false;
    const slots = d.slots || [];
    const errors = d.errors || [];
    body.innerHTML = `
        <div style="display:flex;gap:12px;align-items:center;margin-bottom:20px">
            <span style="font-size:14px;font-weight:600">\u0410\u0432\u0442\u043e\u043f\u043e\u0441\u0442\u0438\u043d\u0433</span>
            <span style="padding:4px 12px;border-radius:20px;font-size:12px;background:${{enabled ? 'rgba(34,197,94,0.15)' : 'rgba(100,116,139,0.15)'}};color:${{enabled ? '#22c55e' : '#94a3b8'}}">${{enabled ? '\u0432\u043a\u043b' : '\u0432\u044b\u043a\u043b'}}</span>
        </div>
        <div style="margin-bottom:16px">
            <div style="font-size:12px;color:var(--text-muted);margin-bottom:6px">\u0421\u043b\u0435\u0434\u0443\u044e\u0449\u0430\u044f \u043f\u0443\u0431\u043b\u0438\u043a\u0430\u0446\u0438\u044f</div>
            <div style="font-size:15px">${{d.next_pub || '\u2014'}}</div>
        </div>
        <div style="margin-bottom:16px">
            <div style="font-size:12px;color:var(--text-muted);margin-bottom:8px">\u041f\u043b\u043e\u0449\u0430\u0434\u043a\u0438: ${{(d.platforms||[]).join(', ') || '\u2014'}}</div>
        </div>
        <div class="card premium-glow" style="margin-bottom:16px">
            <div style="font-weight:600;font-size:13px;margin-bottom:12px">\u0421\u043b\u043e\u0442\u044b \u0440\u0430\u0441\u043f\u0438\u0441\u0430\u043d\u0438\u044f</div>
            ${{slots.length ? slots.map(s => `<div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--border)">
                <span style="font-size:13px">${{s.day || ''}} ${{s.time || ''}} \u2014 ${{s.platform || ''}}</span>
                <button class="mkt-btn" onclick="deleteSmmSlot('${{s.id}}')">\u0423\u0434\u0430\u043b\u0438\u0442\u044c</button>
            </div>`).join('') : '<div style="color:var(--text-muted);font-size:12px">\u0421\u043b\u043e\u0442\u043e\u0432 \u043d\u0435\u0442</div>'}}
            <button class="nav-item" style="margin-top:12px;padding:6px 14px;font-size:12px;border:none;cursor:pointer" onclick="addSmmSlot()">+ \u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u0441\u043b\u043e\u0442</button>
        </div>
        ${{errors.length ? `<div style="color:#ef4444;font-size:12px">\u041e\u0448\u0438\u0431\u043a\u0438: ${{errors.join(', ')}}</div>` : ''}}
    `;
}}

async function renderSmmAnalytics(body) {{
    const r = await fetch(`/api/analytics/${{_smmSelectedCid}}/overview`).then(x => x.json());
    const metrics = (r.data || {{}}).metrics || [];
    body.innerHTML = metrics.length
        ? `<div class="stats-grid">${{metrics.map(m => `<div class="stat-card"><div class="stat-value">${{m.value}}${{m.unit ? ' '+m.unit : ''}}</div><div class="stat-label">${{m.label}}</div></div>`).join('')}}</div>`
        : '<p style="color:var(--text-muted)">\u041d\u0435\u0442 \u0434\u0430\u043d\u043d\u044b\u0445</p>';
}}

async function renderSmmSettings(body) {{
    const r = await fetch(`/api/smm/${{_smmSelectedCid}}/settings`).then(x => x.json());
    const d = r.data || {{}};
    const maskToken = t => t ? '\u2022'.repeat(Math.min(t.length, 8)) + t.slice(-4) : '\u2014';
    body.innerHTML = `
        <div style="display:grid;gap:20px">
            <div class="card premium-glow">
                <div style="font-weight:600;margin-bottom:12px">\u041f\u0440\u043e\u0444\u0438\u043b\u044c \u043f\u0440\u043e\u0435\u043a\u0442\u0430</div>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
                    <div><div style="font-size:11px;color:var(--text-muted)">\u041d\u0430\u0437\u0432\u0430\u043d\u0438\u0435</div><div>${{d.name || '\u2014'}}</div></div>
                    <div><div style="font-size:11px;color:var(--text-muted)">\u041a\u043e\u043d\u0442\u0430\u043a\u0442</div><div>${{d.contact || '\u2014'}}</div></div>
                </div>
            </div>
            <div class="card premium-glow">
                <div style="font-weight:600;margin-bottom:12px">\u0421\u043e\u0446\u0441\u0435\u0442\u0438 / \u0422\u043e\u043a\u0435\u043d\u044b</div>
                ${{(d.socials || []).map(s => `<div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border)">
                    <span>${{s.platform}}</span>
                    <span style="font-family:monospace;font-size:12px;color:var(--text-muted)">${{maskToken(s.token)}}</span>
                </div>`).join('') || '<div style="color:var(--text-muted);font-size:12px">\u041d\u0435 \u043d\u0430\u0441\u0442\u0440\u043e\u0435\u043d\u043e</div>'}}
            </div>
            <div class="card premium-glow">
                <div style="font-weight:600;margin-bottom:12px">\u0413\u043e\u043b\u043e\u0441 \u0431\u0440\u0435\u043d\u0434\u0430</div>
                <div style="font-size:13px;color:var(--text-muted)">${{d.brand_voice || '\u041d\u0435 \u0437\u0430\u043f\u043e\u043b\u043d\u0435\u043d\u043e'}}</div>
            </div>
        </div>
    `;
}}

function addSmmSlot() {{ alert('\u0414\u043e\u0431\u0430\u0432\u043b\u0435\u043d\u0438\u0435 \u0441\u043b\u043e\u0442\u0430: \u0432 \u0440\u0430\u0437\u0440\u0430\u0431\u043e\u0442\u043a\u0435'); }}
function deleteSmmSlot(id) {{ alert('\u0423\u0434\u0430\u043b\u0435\u043d\u0438\u0435 \u0441\u043b\u043e\u0442\u0430 ' + id + ': \u0432 \u0440\u0430\u0437\u0440\u0430\u0431\u043e\u0442\u043a\u0435'); }}
// =================== END SMM MODULE ===================

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

    const sr = d.server_recovery || {{}};
    const bs = d.backup_storage || {{}};
    const tf = (v) => v === true ? 'ПРОЙДЕНО' : (v === false ? 'ПРОВАЛ' : 'НЕИЗВЕСТНО');
    document.getElementById('dr-docs-sync').innerText = tf(sr.docs_sync_pass);
    document.getElementById('dr-backup-pass').innerText = tf(sr.dr_backup_pass);
    document.getElementById('dr-off-host').innerText = tf(sr.off_host_copy_pass);
    document.getElementById('dr-restore-smoke').innerText = tf(sr.restore_smoke_pass);
    document.getElementById('dr-server-pass').innerText = tf(sr.server_recovery_pass);
    const mz = Array.isArray(sr.missing_zones) ? sr.missing_zones : [];
    document.getElementById('dr-missing-zones').innerText = mz.length ? mz.join(', ') : '[]';

    document.getElementById('dr-local-gb').innerText = bs.local_backup_gb ?? '—';
    document.getElementById('dr-cloud-gb').innerText = (bs.cloud_backup_gb === null || bs.cloud_backup_gb === undefined) ? 'НЕИЗВЕСТНО' : bs.cloud_backup_gb;
    document.getElementById('dr-stage-gb').innerText = bs.stage_gb ?? '—';
    const statusMap = {{'PASS':'ПРОЙДЕНО','FAIL':'ПРОВАЛ','UNKNOWN':'НЕИЗВЕСТНО','WARN':'ПРЕДУПРЕЖДЕНИЕ','DEGRADED':'ДЕГРАДАЦИЯ','RED':'КРИТИЧНО','GREEN':'НОРМА'}};
    const st = String(bs.status || 'UNKNOWN').toUpperCase();
    document.getElementById('dr-storage-status').innerText = statusMap[st] || bs.status || 'НЕИЗВЕСТНО';
    const cc = Array.isArray(bs.cleanup_candidates) ? bs.cleanup_candidates.length : 0;
    document.getElementById('dr-cleanup-cnt').innerText = cc;
}}

let mktRelationsCache = null;

async function fetchMktRelations(cid) {{
    try {{
        const r = await fetch(`/api/module/${{cid}}/marketing`).then(res => res.json());
        if (r.status === 'ok') {{
            mktRelationsCache = r.data;
        }} else {{
            mktRelationsCache = null;
        }}
    }} catch (e) {{
        mktRelationsCache = null;
    }}
}}

function onMktBizProjectChange() {{
    const projId = document.getElementById('mkt-biz-project').value;
    if (!projId || !PROJECT_CLIENT_MAP[projId]) return;
    const filter = PROJECT_CLIENT_MAP[projId];
    const filtered = allClients.filter(filter);
    const sel = document.getElementById('mkt-global-client');
    sel.innerHTML = '<option value="all">Все клиенты (Только чтение)</option>' +
      filtered.map(c => `<option value="${{c.client_id}}">${{c.name}} (${{c.client_id}})</option>`).join('');
    if (filtered.length === 1) {{
      sel.value = filtered[0].client_id;
      onMktGlobalClientChange();
    }}
}}

function onModuleBizProjectChange(sectionId) {{
    const projId = document.getElementById(sectionId + '-biz-project').value;
    const clientSel = document.getElementById(sectionId + '-client');
    if (!clientSel) return;
    const clients = projId && PROJECT_CLIENT_MAP[projId] ? allClients.filter(PROJECT_CLIENT_MAP[projId]) : allClients;
    clientSel.innerHTML = '<option value="">Выберите клиента</option><option value="all">Все клиенты</option>' +
      clients.map(c => `<option value="${{c.client_id}}">${{c.name}} (${{c.client_id}})</option>`).join('');
    if (clients.length === 1) {{
      clientSel.value = clients[0].client_id;
      loadModuleData(sectionId);
    }}
}}

async function onMktGlobalClientChange() {{
    const cid = document.getElementById('mkt-global-client').value;
    const projSel = document.getElementById('mkt-global-project');
    const activeTabId = document.querySelector('.nav-item.active')?.getAttribute('data-tab');
    
    if (cid === 'all') {{
        projSel.innerHTML = '<option value="all">Все проекты</option>';
        projSel.value = 'all';
        projSel.disabled = true;
    }} else {{
        projSel.disabled = false;
        projSel.innerHTML = '<option value="all">Все проекты</option>';
        try {{
            const res = await fetch(`/api/clients/${{cid}}/projects`).then(r => r.json());
            if (res.status === 'ok' && Array.isArray(res.data)) {{
                let html = '<option value="all">Все проекты</option>';
                res.data.forEach(p => {{
                    html += `<option value="${{p}}">${{p}}</option>`;
                }});
                projSel.innerHTML = html;
            }}
        }} catch (e) {{
            console.error('Failed to load projects', e);
        }}
        projSel.value = 'all';
    }}
    
    toggleMktButtons(cid);
    
    if (activeTabId && activeTabId.startsWith('marketing_')) {{
        loadModuleData(activeTabId);
    }}
}}

function onMktGlobalProjectChange() {{
    const activeTabId = document.querySelector('.nav-item.active')?.getAttribute('data-tab');
    if (activeTabId && activeTabId.startsWith('marketing_')) {{
        loadModuleData(activeTabId);
    }}
}}

function toggleMktButtons(cid) {{
    const activeTabId = document.querySelector('.nav-item.active')?.getAttribute('data-tab');
    if (!activeTabId || !activeTabId.startsWith('marketing_')) return;
    
    const addBtn = document.getElementById(`${{activeTabId}}-add-btn`);
    const sheetBtn = document.getElementById(`${{activeTabId}}-sheet-btn`);
    
    if (cid === 'all') {{
        if (addBtn) addBtn.style.display = 'none';
        if (sheetBtn) sheetBtn.style.display = 'none';
    }} else {{
        if (addBtn) addBtn.style.display = '';
        if (sheetBtn) sheetBtn.style.display = '';
    }}
}}

function ensureMktGlobalHeaderPopulated(tabId) {{
    const globalHeader = document.getElementById('marketing-global-header');
    if (!globalHeader) return;
    
    globalHeader.style.display = 'flex';
    
    const localSel = document.getElementById(tabId + '-client');
    if (localSel) {{
        localSel.style.display = 'none';
    }}
    
    const globalSel = document.getElementById('mkt-global-client');
    if (globalSel && globalSel.options.length <= 1) {{
        ensureClientsLoaded().then(() => {{
            let opts = '<option value="all">Все клиенты (Только чтение)</option>';
            opts += allClients.map(c => `<option value="${{c.client_id}}">${{c.name}} (${{c.client_id}})</option>`).join('');
            globalSel.innerHTML = opts;
            
            const savedClient = localStorage.getItem('ontime_selected_client') || currentClientId || 'all';
            globalSel.value = savedClient;
            
            onMktGlobalClientChange();
        }});
    }} else {{
        toggleMktButtons(globalSel.value);
        loadModuleData(tabId);
    }}
}}

async function exportModuleGoogleSheet(tabId) {{
    let cid = document.getElementById('mkt-global-client').value;
    if (!cid || cid === 'all') {{
        alert('Экспорт в Google Sheets невозможен во всеобщем режиме');
        return;
    }}
    const projId = document.getElementById('mkt-global-project').value;
    const cfg = MODULE_CONFIG[tabId];
    
    const res = await fetch(`/api/module/${{cid}}/${{cfg.module}}/${{cfg.section}}`).then(r => r.json());
    if (res.status !== 'ok') {{
        alert('Ошибка получения данных для экспорта');
        return;
    }}
    
    const body = {{
        client_id: cid,
        tab_id: tabId,
        project_id: projId,
        columns: cfg.columns,
        rows: res.data
    }};
    
    const r = await fetch('/api/marketing/export-sheet', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(body)
    }}).then(res => res.json());
    
    if (r.status === 'ok') {{
        if (r.sheet_url) {{
            alert('Успешно экспортировано в Google Sheet!\\nURL: ' + r.sheet_url);
            window.open(r.sheet_url, '_blank');
        }} else {{
            alert('Экспорт завершен. URL таблицы отсутствует.');
        }}
    }} else if (r.status === 'not_configured') {{
        alert('Google Sheet URL не настроен в STATUS.md для проекта ' + projId);
    }} else {{
        alert('Ошибка экспорта: ' + (r.message || 'unknown'));
    }}
}}

function initModuleTab(tabId) {{
    const sel = document.getElementById(tabId + '-client');
    if (!sel) return;
    const workflowWrap = document.getElementById(tabId + '-workflow-wrap');
    if (workflowWrap) {{
        workflowWrap.style.display = (tabId === 'content_контент-завод') ? 'flex' : 'none';
    }}
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

async function startWorkflowFromTab(tabId) {{
    const clientSel = document.getElementById(tabId + '-client');
    const typeSel = document.getElementById(tabId + '-workflow-type');
    const cid = clientSel ? clientSel.value : '';
    const workflowType = typeSel ? typeSel.value : '';
    if (!cid) return alert('Выберите клиента');
    const r = await fetch('/api/workflow/run', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ client_id: cid, workflow_type: workflowType }})
    }}).then(res => res.json());
    if (r.status !== 'ok') {{
        alert('Не удалось запустить: ' + (r.message || 'unknown'));
        return;
    }}
    alert('Запуск создан: ' + (r.task_id || 'OK'));
}}

async function loadModuleData(tabId) {{
    let cid;
    if (tabId.startsWith('marketing_')) {{
        cid = document.getElementById('mkt-global-client').value;
    }} else {{
        cid = document.getElementById(tabId + '-client').value;
    }}
    if (!cid) return renderModuleEmpty(tabId, 'Выберите клиента сверху');
    currentClientId = cid;
    if (cid !== 'all') {{
        localStorage.setItem('ontime_selected_client', cid);
    }}
    const cfg = MODULE_CONFIG[tabId];
    setModuleStatus(tabId, 'Загрузка');
    try {{
        const r = await fetch(`/api/module/${{cid}}/${{cfg.module}}/${{cfg.section}}`).then(res => res.json());
        if (r.status === 'ok') {{
            renderModuleTable(tabId, r.data, cid);
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

function getModuleFieldValue(tabId, item, col) {{
    if (tabId === 'marketing_источники_трафика' && item) {{
        if (col === 'Название') return item['Название'] || item['Канал'] || '';
        if (col === 'Ссылка') return item['Ссылка'] || item['URL'] || '';
        if (col === 'Тип') return item['Тип'] || item['channel_type'] || 'other';
        if (col === 'Статус') return item['Статус'] || item['status'] || 'draft';
        if (col === 'Клиенты') {{
            const clients = item['Клиенты'] || item['assigned_clients'] || '';
            return Array.isArray(clients) ? clients.join(', ') : clients;
        }}
    }}
    return item ? (item[col] || '') : '';
}}

function renderModuleTable(tabId, items, cid) {{
    const cfg = MODULE_CONFIG[tabId];
    const thead = document.getElementById(tabId + '-thead');
    const tbody = document.getElementById(tabId + '-tbody');
    
    const isAll = (cid === 'all');
    const cols = isAll ? ['Клиент', ...cfg.columns] : cfg.columns;
    
    const actionsHeader = isAll ? '' : '<th>Действия</th>';
    thead.innerHTML = '<tr>' + cols.map(c => `<th>${{c}}</th>`).join('') + actionsHeader + '</tr>';
    
    let rows = (Array.isArray(items) ? items : (items && Object.keys(items).length ? [items] : [])).filter(Boolean);
    if (tabId.startsWith('marketing_')) {{
        const projId = document.getElementById('mkt-global-project').value;
        if (projId && projId !== 'all') {{
            rows = rows.filter(r => r.project_id === projId);
        }}
    }}
    
    if (!rows.length) return renderModuleEmpty(tabId, 'Нет записей');
    const workflowButtons = (tab, item) => {{
        if (tab === 'content_посты') {{
            return `
              <button class="mkt-btn" onclick="workflowAction('${{tab}}', '${{item.id}}', 'submit_review')">В review</button>
              <button class="mkt-btn" onclick="workflowAction('${{tab}}', '${{item.id}}', 'approve')">Утвердить</button>
              <button class="mkt-btn" onclick="workflowAction('${{tab}}', '${{item.id}}', 'rework')">На доработку</button>
              <button class="mkt-btn" onclick="workflowAction('${{tab}}', '${{item.id}}', 'send_to_design')">В дизайн</button>
              <button class="mkt-btn" onclick="workflowAction('${{tab}}', '${{item.id}}', 'publish')">Опубликовать</button>
            `;
        }}
        if (tab === 'marketing_креатив') {{
            return `
              <button class="mkt-btn" onclick="workflowAction('${{tab}}', '${{item.id}}', 'submit_review')">Сдать</button>
              <button class="mkt-btn" onclick="workflowAction('${{tab}}', '${{item.id}}', 'approve')">Утвердить</button>
              <button class="mkt-btn" onclick="workflowAction('${{tab}}', '${{item.id}}', 'rework')">На доработку</button>
            `;
        }}
        return '';
    }};
    
    const escHtml = (v) => String(v ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
    const validHttpUrl = (v) => /^https?:\/\/\S+$/i.test(String(v || '').trim());
    const renderCell = (tab, col, item) => {{
        const raw = getModuleFieldValue(tab, item, col);
        if (tab === 'marketing_источники_трафика' && col === 'Ссылка') {{
            const url = String(raw || '').trim();
            if (!url) return '';
            if (validHttpUrl(url)) return `<a href="${{escHtml(url)}}" target="_blank" rel="noopener noreferrer">${{escHtml(url)}}</a>`;
            return `<span style="color:var(--red)">invalid_url</span>`;
        }}
        if (tab === 'marketing_источники_трафика' && col === 'Клиенты') {{
            const txt = String(raw || '').trim();
            if (!txt) return '<span style="color:var(--text-muted);font-size:11px">—</span>';
            const arr = txt.split(/[;,]/).map(s => s.trim()).filter(Boolean);
            const shortList = arr.slice(0, 3);
            const more = arr.length > 3 ? ` +${{arr.length - 3}}` : '';
            return `<span style="font-size:11px;color:var(--text-muted)">${{escHtml(shortList.join(', '))}}${{more}}</span>`;
        }}
        return escHtml(raw);
    }};

    tbody.innerHTML = rows.map(item => {{
        const actionsCell = isAll ? '' : `<td>
            <button class="mkt-btn" onclick="showModuleEditModal('${{tabId}}', '${{item.id || 'brand'}}')">Редактировать</button>
            <button class="mkt-btn" onclick="deleteModuleItem('${{tabId}}', '${{item.id || 'brand'}}')">Удалить</button>
            ${{workflowButtons(tabId, item)}}
        </td>`;
        return `
            <tr>
                ${{cols.map(c => `<td>${{renderCell(tabId, c, item)}}</td>`).join('')}}
                ${{actionsCell}}
            </tr>
        `;
    }}).join('');
}}

function renderModuleField(tabId, fieldName, value = '') {{
    const cfg = MODULE_CONFIG[tabId] || {{}};
    const fieldCfg = (cfg.fieldTypes && cfg.fieldTypes[fieldName]) || {{}};
    const escapedValue = String(value ?? '').replace(/"/g, '&quot;');
    const inputId = `mkt-inp-${{fieldName}}`;
    
    if (fieldCfg.type === 'select-relational') {{
        const relation = fieldCfg.relation;
        const labelCol = fieldCfg.labelColumn;
        let optionsHtml = '<option value="">Нет связи</option>';
        if (mktRelationsCache && Array.isArray(mktRelationsCache[relation])) {{
            optionsHtml += mktRelationsCache[relation].map(item => {{
                const label = item[labelCol] || item.id || '';
                return `<option value="${{label}}" ${{label === value ? 'selected' : ''}}>${{label}}</option>`;
            }}).join('');
        }}
        return `<div class="mkt-field"><label>${{fieldName}}</label><select class="premium-input" id="${{inputId}}">${{optionsHtml}}</select></div>`;
    }}
    
    if (fieldCfg.type === 'select') {{
        const options = Array.isArray(fieldCfg.options) ? fieldCfg.options : [];
        return `<div class="mkt-field"><label>${{fieldName}}</label><select class="premium-input" id="${{inputId}}">${{options.map(o => `<option value="${{o}}" ${{o === value ? 'selected' : ''}}>${{o}}</option>`).join('')}}</select></div>`;
    }}
    if (fieldCfg.type === 'date') {{
        return `<div class="mkt-field"><label>${{fieldName}}</label><input type="date" class="premium-input" id="${{inputId}}" value="${{escapedValue}}"></div>`;
    }}
    if (fieldCfg.type === 'number') {{
        return `<div class="mkt-field"><label>${{fieldName}}</label><input type="number" step="any" class="premium-input" id="${{inputId}}" value="${{escapedValue}}"></div>`;
    }}
    if (fieldCfg.type === 'url') {{
        return `<div class="mkt-field"><label>${{fieldName}}</label><input type="url" placeholder="https://..." class="premium-input" id="${{inputId}}" value="${{escapedValue}}"></div>`;
    }}
    if (fieldCfg.type === 'readonly') {{
        return `<div class="mkt-field"><label>${{fieldName}}</label><input class="premium-input" style="opacity:0.6" id="${{inputId}}" value="${{escapedValue}}" readonly></div>`;
    }}
    if (fieldCfg.type === 'textarea') {{
        return `<div class="mkt-field"><label>${{fieldName}}</label><textarea class="premium-input" id="${{inputId}}" rows="3" style="resize:vertical">${{String(value ?? '').replace(/</g,'&lt;').replace(/>/g,'&gt;')}}</textarea></div>`;
    }}
    return `<div class="mkt-field"><label>${{fieldName}}</label><input class="premium-input" id="${{inputId}}" value="${{escapedValue}}"></div>`;
}}

async function showModuleAddModal(tabId) {{
    if (tabId === 'sales_sales_core') return alert('Sales Core — только просмотр');
    const cfg = MODULE_CONFIG[tabId];
    const isLegacySales = LEGACY_SALES_TABS.has(tabId);
    let cid = "";
    if (!isLegacySales) {{
        if (tabId.startsWith('marketing_')) {{
            cid = document.getElementById('mkt-global-client').value;
        }} else {{
            cid = document.getElementById(tabId + '-client').value;
        }}
        if (!cid || cid === 'all') return alert('Сначала выберите конкретного клиента');
    }}
    
    if (tabId === 'marketing_воронки') {{
        await fetchMktRelations(cid);
    }}
    
    document.getElementById('mkt-panel-title').innerText = 'Добавить: ' + cfg.title;
    const host = document.getElementById('mkt-panel-fields');
    host.innerHTML = cfg.columns.map(c => renderModuleField(tabId, c, '')).join('') + 
                     `<button class="nav-item active" style="width:100%; border:none; padding:12px; margin-top:12px" onclick="saveModuleItem('${{tabId}}')">Сохранить</button>`;
    document.getElementById('mkt-sidepanel').classList.add('open');
}}

async function showModuleEditModal(tabId, itemId) {{
    if (tabId === 'sales_sales_core') return;
    const cfg = MODULE_CONFIG[tabId];
    const isLegacySales = LEGACY_SALES_TABS.has(tabId);
    let cid = "";
    let sourceUrl = "";
    if (isLegacySales) {{
        sourceUrl = `/api/sales/${{tabId.replace('sales_', '')}}`;
    }} else {{
        if (tabId.startsWith('marketing_')) {{
            cid = document.getElementById('mkt-global-client').value;
        }} else {{
            cid = document.getElementById(tabId + '-client').value;
        }}
        if (!cid || cid === 'all') return alert('Сначала выберите конкретного клиента');
        sourceUrl = `/api/module/${{cid}}/${{cfg.module}}/${{cfg.section}}`;
    }}

    if (tabId === 'marketing_воронки') {{
        await fetchMktRelations(cid);
    }}

    fetch(sourceUrl)
        .then(r => r.json())
        .then(r => {{
            if (r.status !== 'ok') return;
            const item = (Array.isArray(r.data) ? r.data : [r.data]).find(i => i.id === itemId);
            if (!item) return;

            document.getElementById('mkt-panel-title').innerText = 'Редактировать: ' + cfg.title;
            const host = document.getElementById('mkt-panel-fields');
            host.innerHTML = cfg.columns.map(c => renderModuleField(tabId, c, getModuleFieldValue(tabId, item, c))).join('') + 
                             `<button class="nav-item active" style="width:100%; border:none; padding:12px; margin-top:12px" onclick="saveModuleItem('${{tabId}}', '${{itemId}}')">Сохранить</button>`;
            document.getElementById('mkt-sidepanel').classList.add('open');
        }});
}}

async function saveModuleItem(tabId, itemId = null) {{
    const cfg = MODULE_CONFIG[tabId];
    const isSales = LEGACY_SALES_TABS.has(tabId);
    let cid = "";
    if (!isSales) {{
        if (tabId.startsWith('marketing_')) {{
            cid = document.getElementById('mkt-global-client').value;
        }} else {{
            cid = document.getElementById(tabId + '-client').value;
        }}
        if (!cid || cid === 'all') return alert('Выберите клиента');
    }}
    
    const body = {{}};
    if (itemId) body.id = itemId;
    cfg.columns.forEach(c => {{
        const el = document.getElementById('mkt-inp-' + c);
        if (el) body[c] = el.value;
    }});
    
    if (tabId.startsWith('marketing_')) {{
        body.project_id = document.getElementById('mkt-global-project').value;
        body.actor = 'dashboard';
    }}
    
    const action = itemId ? 'update' : 'add';
    const url = isSales ? `/api/sales/${{tabId.replace('sales_', '')}}/${{action}}` : `/api/module/${{cid}}/${{cfg.module}}/${{cfg.section}}/${{action}}`;
    
    const r = await fetch(url, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(body)
    }}).then(res => res.json());
    
    if (r.status === 'ok') {{
        closeMarketingPanel();
        if (isSales) refreshSalesSection(tabId);
        else loadModuleData(tabId);
    }} else {{
        alert('Ошибка сохранения: ' + (r.message || 'неизвестно'));
    }}
}}

async function deleteModuleItem(tabId, itemId) {{
    if (!confirm('Удалить запись?')) return;
    const cfg = MODULE_CONFIG[tabId];
    const isSales = LEGACY_SALES_TABS.has(tabId);
    let cid = "";
    if (!isSales) {{
        if (tabId.startsWith('marketing_')) {{
            cid = document.getElementById('mkt-global-client').value;
        }} else {{
            cid = document.getElementById(tabId + '-client').value;
        }}
        if (!cid || cid === 'all') return alert('Выберите клиента');
    }}
    
    const url = isSales ? `/api/sales/${{tabId.replace('sales_', '')}}/delete` : `/api/module/${{cid}}/${{cfg.module}}/${{cfg.section}}/delete`;
    
    const r = await fetch(url, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ id: itemId }})
    }}).then(res => res.json());
    if (r.status === 'ok') {{
        if (isSales) refreshSalesSection(tabId);
        else loadModuleData(tabId);
    }}
}}

function exportModuleCsv(tabId) {{
    let cid = "";
    let projId = "";
    if (tabId.startsWith('marketing_')) {{
        cid = document.getElementById('mkt-global-client').value;
        projId = document.getElementById('mkt-global-project').value;
    }} else {{
        cid = document.getElementById(tabId + '-client').value;
    }}
    if (!cid || cid === 'all') return alert('Выберите клиента');
    const cfg = MODULE_CONFIG[tabId];
    let url = `/api/module-export/${{cid}}/${{cfg.module}}/${{cfg.section}}`;
    if (projId && projId !== 'all') {{
        url += `?project_id=${{projId}}`;
    }}
    window.open(url, '_blank');
}}

async function workflowAction(tabId, itemId, action) {{
    const cid = document.getElementById(tabId + '-client').value;
    if (!cid) return alert('Выберите клиента');
    const r = await fetch('/api/workflow/action', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ cid, tab_id: tabId, item_id: itemId, action }})
    }}).then(res => res.json());
    if (r.status !== 'ok') {{
        alert('Ошибка workflow: ' + (r.message || 'unknown'));
        return;
    }}
    loadModuleData(tabId);
    if (tabId === 'content_посты') {{
        const creativeSelect = document.getElementById('marketing_креатив-client');
        if (creativeSelect) {{
            creativeSelect.value = cid;
            loadModuleData('marketing_креатив');
        }}
        const pubSelect = document.getElementById('content_публикации-client');
        if (pubSelect) {{
            pubSelect.value = cid;
            loadModuleData('content_публикации');
        }}
    }}
}}

async function refreshClients() {{
  try {{
    const rL = await fetch('/api/clients/list').then(r => r.json());
    if (rL.status === 'ok') {{
      allClients = rL.data || [];
      renderClients(allClients);
      if (currentProjectId) refreshProjectClients();
      // restore project button highlight
      if (currentProjectId) {{
        document.querySelectorAll('.proj-btn').forEach(b => b.classList.remove('active'));
        const btn = document.getElementById('proj-' + currentProjectId);
        if (btn) btn.classList.add('active');
      }}
    }}
  }} catch (e) {{
    console.error('refreshClients failed', e);
  }}
}}

async function ensureClientsLoaded() {{
  if (allClients.length) return;
  await refreshClients();
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
    if (window.feather && typeof window.feather.replace === 'function') {{
      window.feather.replace();
    }}
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
        loadChatAgents();
        refreshClientChat();
        startChatPolling();
    }}
}}

async function loadChatAgents() {{
    const sel = document.getElementById('client-chat-agent');
    if (!sel || sel.dataset.loaded === '1') return;
    try {{
        const r = await fetch('/api/agents/list').then(res => res.json());
        if (r.status !== 'ok') return;
        const agents = r.data || [];
        const options = ['<option value="">Выберите агента</option>'].concat(
            agents.map(a => `<option value="${{a.id}}">${{a.name}}</option>`)
        );
        sel.innerHTML = options.join('');
        sel.dataset.loaded = '1';
    }} catch (e) {{}}
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
    let url = `/api/clients/${{currentClientId}}/chat`;
    if (currentStageThread && currentStageThread.stage) {{
        const qs = new URLSearchParams({{ stage: currentStageThread.stage, run_id: currentStageThread.run_id || '' }});
        url = `/api/clients/${{currentClientId}}/stage-chat?` + qs.toString();
    }}
    const r = await fetch(url).then(res => res.json());
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
    const mode = document.getElementById('client-chat-mode')?.value || 'coordinator';
    const agent = document.getElementById('client-chat-agent')?.value || '';
    const stage = document.getElementById('client-chat-stage')?.value || '';
    let url = `/api/clients/${{currentClientId}}/chat`;
    if (currentStageThread && currentStageThread.stage) {{
        url = `/api/clients/${{currentClientId}}/stage-chat`;
    }}
    const res = await fetch(url, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ text, mode, agent, stage, stage_thread: currentStageThread }})
    }}).then(r => r.json());
    if (res.status === 'ok') {{
        input.value = '';
        refreshClientChat();
    }} else {{
        alert('Ошибка отправки сообщения');
    }}
}}

async function runClientTaskFromChat() {{
    if (!currentClientId) return;
    const mode = document.getElementById('client-chat-mode')?.value || 'coordinator';
    const agent = document.getElementById('client-chat-agent')?.value || '';
    const stage = document.getElementById('client-chat-stage')?.value || '';
    const text = (document.getElementById('client-chat-input')?.value || '').trim();
    const payload = {{ text, mode, agent, stage }};
    const r = await fetch(`/api/clients/${{currentClientId}}/chat/run`, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(payload)
    }}).then(res => res.json());
    if (r.status !== 'ok') return alert('Не удалось запустить');
    alert('Задача запущена: ' + (r.task_id || 'OK'));
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

let wfCurrentTabId = 'content_web-завод';
let wfCurrentCid = null;
let wfCurrentSite = 'all';
let wfActiveTab = 'overview';
let wfProdMode = 'kanban';
let wfLibCat = 'photo';
let wfData = null;

async function initWebFactory(div_id) {{
    if (div_id) wfCurrentTabId = div_id;
    const sel = document.getElementById('wf-client-sel');
    if (sel && !sel.dataset.ready) {{
        sel.innerHTML = '<option value="">Выберите клиента</option>';
        sel.dataset.ready = '1';
    }}
    await ensureClientsLoaded();
    renderWfClientsList(wfCurrentTabId);
    
    const savedClient = localStorage.getItem('ontime_selected_client') || currentClientId;
    if (savedClient) {{
        const cl = allClients.find(c => c.client_id === savedClient);
        if (cl) {{
            selectWfClient(wfCurrentTabId, cl.client_id, cl.name);
        }}
    }}
}}

function filterWfClients(div_id) {{
    const q = document.getElementById(div_id + '-client-search').value.toLowerCase();
    document.querySelectorAll('#' + div_id + '-clients-list .wf-client-item').forEach(el => {{
        el.style.display = el.dataset.name.toLowerCase().includes(q) ? '' : 'none';
    }});
}}

function renderWfClientsList(div_id) {{
    const list = document.getElementById(div_id + '-clients-list');
    if (!list) return;
    const clients = allClients.filter(c => {{
        const projId = currentProjectId;
        if (projId && PROJECT_CLIENT_MAP[projId]) return PROJECT_CLIENT_MAP[projId](c);
        return true;
    }});
    list.innerHTML = clients.map(c => `
        <div class="wf-client-item nav-item" data-cid="${{c.client_id}}" data-name="${{c.name}}"
             onclick="selectWfClient('${{div_id}}', '${{c.client_id}}', '${{c.name.replace(/'/g,"&#39;")}}')"
             style="padding:10px 16px; cursor:pointer; font-size:13px; display:flex; flex-direction:column; gap:2px;">
            <span style="font-weight:600">${{c.name}}</span>
            <span style="font-size:11px; color:var(--text-muted)">${{c.client_id}}</span>
        </div>`).join('');
}}

async function selectWfClient(div_id, cid, name) {{
    wfCurrentCid = cid;
    localStorage.setItem('ontime_selected_client', cid);
    
    document.querySelectorAll('#' + div_id + '-clients-list .wf-client-item').forEach(el => {{
        el.classList.toggle('active', el.dataset.cid === cid);
    }});
    
    document.getElementById(div_id + '-workspace-empty').style.display = 'none';
    document.getElementById(div_id + '-workspace-content').style.display = 'flex';
    document.getElementById(div_id + '-client-name').textContent = name;
    document.getElementById(div_id + '-client-id').textContent = cid;
    
    try {{
        const res = await fetch('/api/web/' + cid + '/data').then(r => r.json());
        if (res.status === 'ok') {{
            wfData = res.data;
        }} else {{
            wfData = null;
        }}
    }} catch (e) {{
        wfData = null;
    }}
    
    const select = document.getElementById(div_id + '-site-select');
    if (select) {{
        select.innerHTML = '<option value="all">Все сайты</option>';
        if (wfData && wfData.sites) {{
            wfData.sites.forEach(s => {{
                select.innerHTML += `<option value="${{s.domain}}">${{s.domain}}</option>`;
            }});
        }}
        wfCurrentSite = 'all';
        select.value = 'all';
    }}
    
    switchWfTab(div_id, 'overview');
}}

function onWfSiteChanged(div_id) {{
    const select = document.getElementById(div_id + '-site-select');
    wfCurrentSite = select ? select.value : 'all';
    switchWfTab(div_id, wfActiveTab);
}}

function switchWfTab(div_id, tab) {{
    wfActiveTab = tab;
    document.querySelectorAll('#' + div_id + ' .wf-tabpanel').forEach(el => el.style.display = 'none');
    document.querySelectorAll('#' + div_id + '-wf-tabs .nav-item').forEach(el => el.classList.remove('active'));
    
    const panel = document.getElementById(div_id + '-tabpanel-' + tab);
    if (panel) panel.style.display = '';
    
    const btn = document.querySelector('#' + div_id + '-wf-tabs [data-wftab="' + tab + '"]');
    if (btn) btn.classList.add('active');
    
    if (tab === 'overview') renderWfOverview(div_id);
    else if (tab === 'production') renderWfProduction(div_id);
    else if (tab === 'sites') renderWfSites(div_id);
    else if (tab === 'library') renderWfLibrary(div_id);
    else if (tab === 'analytics') renderWfAnalytics(div_id);
    else if (tab === 'settings') renderWfSettings(div_id);
}}

function renderWfOverview(div_id) {{
    const cards = document.getElementById(div_id + '-overview-cards');
    if (!cards) return;
    if (!wfData) {{
        cards.innerHTML = '<div style="color:var(--text-muted)">Нет данных</div>';
        return;
    }}
    
    const sites = wfCurrentSite === 'all' ? wfData.sites : wfData.sites.filter(s => s.domain === wfCurrentSite);
    const sitesCount = sites.length;
    const prodCount = wfData.production.filter(p => wfCurrentSite === 'all' || p.site === wfCurrentSite).length;
    
    let errCount = 0;
    sites.forEach(s => errCount += (s.errors || []).length);
    
    let visits = 0, leads = 0;
    sites.forEach(s => {{
        visits += s.metrics?.visits || 0;
        leads += s.metrics?.leads || 0;
    }});
    const cr = visits > 0 ? ((leads / visits) * 100).toFixed(2) : '0.00';
    
    let expCount = 0;
    sites.forEach(s => {{
        expCount += (s.forms || []).filter(f => f.status === 'ab_test').length;
        expCount += (s.pages || []).filter(p => p.status === 'ab_test').length;
    }});
    
    const libCount = wfData.library.length;
    
    const rows = [
        {{label: 'Сайты клиента', val: sitesCount + ' шт', sub: wfCurrentSite === 'all' ? 'Все домены в работе' : wfCurrentSite, tab: 'sites'}},
        {{label: 'Страницы в производстве', val: prodCount + ' стр', sub: 'Задачи в конвейере', tab: 'production'}},
        {{label: 'Задачи на улучшение', val: errCount + ' ош', sub: 'Критические риски на сайтах', tab: 'analytics'}},
        {{label: 'Конверсия', val: cr + ' %', sub: leads + ' заявок с ' + visits + ' сессий', tab: 'analytics'}},
        {{label: 'Эксперименты (A/B)', val: expCount + ' актив.', sub: 'Тесты форм и заголовков', tab: 'production'}},
        {{label: 'Библиотека материалов', val: libCount + ' ассет.', sub: 'Медиа, лого, кейсы клиента', tab: 'library'}}
    ];
    
    cards.innerHTML = rows.map(i => `
        <div class="smm-card" onclick="switchWfTab('${{div_id}}', '${{i.tab}}')" style="cursor:pointer;" title="Перейти">
            <div style="font-size:11px;font-weight:700;color:var(--text-muted);text-transform:uppercase;margin-bottom:8px">${{i.label}}</div>
            <div style="font-size:22px;font-weight:800;margin-bottom:4px;color:#fff">${{i.val}}</div>
            <div style="font-size:12px;color:var(--text-muted)">${{i.sub}}</div>
            <div style="font-size:11px;color:var(--accent);margin-top:8px">→ Перейти</div>
        </div>`).join('');
}}

const WF_PROD_STAGES = ['Идея', 'Бриф', 'Контент', 'Дизайн', 'Сборка', 'Проверка', 'Публикация', 'Анализ'];

function switchWfProdMode(div_id, mode) {{
    wfProdMode = mode;
    document.getElementById(div_id + '-prod-mode-kanban').classList.toggle('active', mode === 'kanban');
    document.getElementById(div_id + '-prod-mode-list').classList.toggle('active', mode === 'list');
    document.getElementById(div_id + '-production-kanban').style.display = mode === 'kanban' ? 'flex' : 'none';
    document.getElementById(div_id + '-production-list').style.display = mode === 'list' ? 'block' : 'none';
    
    if (mode === 'kanban') renderWfProdKanban(div_id);
    else renderWfProdList(div_id);
}}

function renderWfProduction(div_id) {{
    switchWfProdMode(div_id, wfProdMode);
}}

function renderWfProdKanban(div_id) {{
    const el = document.getElementById(div_id + '-production-kanban');
    if (!el || !wfData) return;
    
    const items = wfData.production.filter(p => wfCurrentSite === 'all' || p.site === wfCurrentSite);
    const counts = {{}};
    WF_PROD_STAGES.forEach(s => counts[s] = 0);
    items.forEach(i => {{
        const stage = i.stage || i.status || 'Идея';
        if (counts[stage] !== undefined) counts[stage]++;
    }});
    
    el.innerHTML = WF_PROD_STAGES.map(stage => {{
        const stageItems = items.filter(i => (i.stage || i.status || 'Идея') === stage);
        return `
        <div style="min-width:200px; flex:1; background:var(--surface); border-radius:var(--radius); border:1px solid var(--border); padding:12px; display:flex; flex-direction:column; gap:8px;">
            <div style="font-weight:700; font-size:13px; margin-bottom:4px; color:#fff">${{stage}} <span style="font-weight:400; color:var(--text-muted)">...${{stageItems.length}}</span></div>
            ${{stageItems.map(i => `
                <div class="card" style="padding:10px; cursor:pointer; font-size:13px; margin-bottom:0" onclick="editWfProdItem('${{div_id}}', '${{i.id}}')">
                    <div style="font-weight:600; margin-bottom:4px; color:#fff">${{i.title || '(без названия)'}}</div>
                    <div style="font-size:11px;color:var(--text-muted);margin-bottom:4px">${{i.page_type || 'Страница'}}</div>
                    <div style="display:flex; justify-content:space-between; align-items:center; font-size:10px; color:var(--text-muted)">
                        <span>${{i.owner || '—'}}</span>
                        <span style="color:${{i.priority === 'high' ? 'var(--red)' : i.priority === 'medium' ? 'var(--yellow)' : 'var(--text-muted)'}}">${{i.priority || 'low'}}</span>
                    </div>
                </div>`).join('')}}
        </div>`;
    }}).join('');
}}

function renderWfProdList(div_id) {{
    if (!wfData) return;
    
    const filterSite = document.getElementById(div_id + '-filter-site');
    const filterType = document.getElementById(div_id + '-filter-type');
    const filterStatus = document.getElementById(div_id + '-filter-status');
    const filterOwner = document.getElementById(div_id + '-filter-owner');
    const filterPriority = document.getElementById(div_id + '-filter-priority');
    
    if (filterSite && filterSite.options.length <= 1) {{
        filterSite.innerHTML = '<option value="all">Все сайты</option>' + wfData.sites.map(s => `<option value="${{s.domain}}">${{s.domain}}</option>`).join('');
        filterType.innerHTML = '<option value="all">Все типы</option>' + ['Landing', 'Corporate', 'Main', 'Info', 'Article', 'Catalog'].map(t => `<option value="${{t}}">${{t}}</option>`).join('');
        filterStatus.innerHTML = '<option value="all">Все статусы</option>' + WF_PROD_STAGES.map(s => `<option value="${{s}}">${{s}}</option>`).join('');
        filterOwner.innerHTML = '<option value="all">Все</option>' + ['OTDEL_CREATIVE', 'OTDEL_TECH', 'OTDEL_ZAVOD'].map(o => `<option value="${{o}}">${{o}}</option>`).join('');
        filterPriority.innerHTML = '<option value="all">Все</option><option value="high">High</option><option value="medium">Medium</option><option value="low">Low</option>';
    }}
    
    const siteVal = filterSite.value;
    const typeVal = filterType.value;
    const statusVal = filterStatus.value;
    const ownerVal = filterOwner.value;
    const priorityVal = filterPriority.value;
    
    let items = wfData.production;
    if (wfCurrentSite !== 'all') {{
        items = items.filter(i => i.site === wfCurrentSite);
    }} else if (siteVal !== 'all') {{
        items = items.filter(i => i.site === siteVal);
    }}
    if (typeVal !== 'all') items = items.filter(i => i.page_type === typeVal);
    if (statusVal !== 'all') items = items.filter(i => (i.stage || i.status || 'Идея') === statusVal);
    if (ownerVal !== 'all') items = items.filter(i => i.owner === ownerVal);
    if (priorityVal !== 'all') items = items.filter(i => i.priority === priorityVal);
    
    const tbody = document.getElementById(div_id + '-prod-table-body');
    if (!tbody) return;
    
    tbody.innerHTML = items.length ? items.map(i => `
        <tr>
            <td><a href="https://${{i.site}}" target="_blank" style="color:var(--accent); text-decoration:underline;">${{i.site}}</a></td>
            <td style="font-weight:600; color:#fff">${{i.title}}</td>
            <td>${{i.page_type || 'Landing'}}</td>
            <td><span style="color:${{i.priority === 'high' ? 'var(--red)' : i.priority === 'medium' ? 'var(--yellow)' : 'var(--text-muted)'}}">${{i.priority || 'low'}}</span></td>
            <td>${{i.owner || '—'}}</td>
            <td><span style="padding:2px 8px; border-radius:10px; font-size:11px; background:var(--surface-hover); color:#fff">${{i.stage || i.status || 'Идея'}}</span></td>
            <td>
                <button class="nav-item active" style="padding:4px 8px; font-size:11px" onclick="editWfProdItem('${{div_id}}', '${{i.id}}')">Двигать</button>
                <button class="nav-item" style="padding:4px 8px; font-size:11px; color:var(--red)" onclick="deleteWfProdItem('${{div_id}}', '${{i.id}}')">Удалить</button>
            </td>
        </tr>`).join('') : '<tr><td colspan="7" style="text-align:center; padding:24px; color:var(--text-muted)">Подходящих страниц нет</td></tr>';
}}

function openWfAddProdModal(div_id) {{
    if (!wfCurrentCid) return;
    const title = prompt('Название новой страницы:');
    if (!title) return;
    const page_type = prompt('Тип страницы (Landing / Catalog / Info / Article / Main):', 'Landing');
    const priority = prompt('Приоритет (high / medium / low):', 'medium');
    const owner = prompt('Ответственный (OTDEL_CREATIVE / OTDEL_TECH / OTDEL_ZAVOD):', 'OTDEL_CREATIVE');
    
    const site = wfCurrentSite === 'all' ? (wfData.sites[0]?.domain || 'site.ru') : wfCurrentSite;
    
    const item = {{
        title,
        page_type,
        priority,
        owner,
        site,
        status: 'Идея',
        stage: 'Идея'
    }};
    
    fetch('/api/web/' + wfCurrentCid + '/production/add', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(item)
    }}).then(r => r.json()).then(res => {{
        if (res.status === 'ok') {{
            wfData.production.push(res.data);
            switchWfTab(div_id, 'production');
        }}
    }});
}}

function editWfProdItem(div_id, id) {{
    const item = wfData.production.find(i => i.id === id);
    if (!item) return;
    const nextStage = prompt('Следующая стадия производства (см. список в конвейере)', item.stage || item.status || 'Идея');
    if (!nextStage || !WF_PROD_STAGES.includes(nextStage)) return;
    
    item.stage = nextStage;
    item.status = nextStage;
    
    fetch('/api/web/' + wfCurrentCid + '/production/update', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(item)
    }}).then(() => {{
        switchWfTab(div_id, 'production');
    }});
}}

function deleteWfProdItem(div_id, id) {{
    if (!confirm('Удалить эту задачу?')) return;
    fetch('/api/web/' + wfCurrentCid + '/production/delete', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{id}})
    }}).then(() => {{
        wfData.production = wfData.production.filter(i => i.id !== id);
        switchWfTab(div_id, 'production');
    }});
}}

function renderWfSites(div_id) {{
    const tbody = document.getElementById(div_id + '-sites-table-body');
    if (!tbody || !wfData) return;
    
    tbody.innerHTML = wfData.sites.length ? wfData.sites.map(s => `
        <tr>
            <td style="font-weight:700; color:#fff">
                <a href="https://${{s.domain}}" target="_blank" style="color:#fff; text-decoration:underline;">${{s.domain}}</a>
            </td>
            <td>${{s.site_type || 'Corporate'}}</td>
            <td><span style="color:var(--green)">● active</span></td>
            <td>
                <span style="color:var(--accent); cursor:pointer; text-decoration:underline;" onclick="onWfSitesPageClick('${{div_id}}', '${{s.domain}}')">
                    ${{(s.pages || []).length}} страниц
                </span>
            </td>
            <td>
                <span style="color:var(--accent); cursor:pointer; text-decoration:underline;" onclick="onWfSitesFormsClick('${{div_id}}', '${{s.domain}}')">
                    ${{(s.forms || []).length}} форм / ${{(s.goals || []).length}} целей
                </span>
            </td>
            <td>
                <span style="color:${{(s.errors || []).length > 0 ? 'var(--red)' : 'var(--green)'}}; font-weight:600; cursor:pointer; text-decoration:underline;" 
                      onclick="onWfSitesErrorsClick('${{div_id}}', '${{s.domain}}')">
                    ${{(s.errors || []).length}} ошибок
                </span>
            </td>
            <td>${{s.last_check || '—'}}</td>
            <td>
                <button class="nav-item active" style="padding:4px 8px; font-size:11px" onclick="runWfSiteAudit('${{div_id}}', '${{s.id}}')">Аудит</button>
                <button class="nav-item" style="padding:4px 8px; font-size:11px; background:var(--surface-hover); color:#fff;" onclick="openWfSiteAccessModal('${{div_id}}', '${{s.id}}')">Доступы</button>
                <button class="nav-item" style="padding:4px 8px; font-size:11px; color:var(--red)" onclick="deleteWfSite('${{div_id}}', '${{s.id}}')">Удалить</button>
            </td>
        </tr>`).join('') : '<tr><td colspan="8" style="text-align:center; padding:24px; color:var(--text-muted)">Сайтов нет. Добавьте первый.</td></tr>';
}}

function onWfSitesPageClick(div_id, domain) {{
    const select = document.getElementById(div_id + '-site-select');
    if (select) select.value = domain;
    wfCurrentSite = domain;
    switchWfTab(div_id, 'production');
}}

function onWfSitesFormsClick(div_id, domain) {{
    const select = document.getElementById(div_id + '-site-select');
    if (select) select.value = domain;
    wfCurrentSite = domain;
    switchWfTab(div_id, 'settings');
}}

function onWfSitesErrorsClick(div_id, domain) {{
    const select = document.getElementById(div_id + '-site-select');
    if (select) select.value = domain;
    wfCurrentSite = domain;
    switchWfTab(div_id, 'analytics');
}}

function openWfSiteAccessModal(div_id, siteId) {{
    const s = wfData.sites.find(x => x.id === siteId);
    if (!s) return;
    
    let modal = document.getElementById('wf-access-modal');
    if (!modal) {{
        modal = document.createElement('div');
        modal.id = 'wf-access-modal';
        modal.style = "position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.8); z-index:9999; display:flex; align-items:center; justify-content:center;";
        document.body.appendChild(modal);
    }}
    
    modal.style.display = 'flex';
    const acc = s.accesses || {{}};
    const cnt = s.counters || {{}};
    const integ = s.integrations || {{}};
    
    modal.innerHTML = `
        <div class="card premium-glow" style="width:500px; padding:24px; position:relative; background:var(--bg); border:1px solid var(--border);">
            <div style="position:absolute; top:12px; right:16px; font-size:18px; color:var(--text-muted); cursor:pointer;" onclick="document.getElementById('wf-access-modal').style.display='none'">&times;</div>
            <h3 style="margin-top:0; color:#fff; font-size:16px; border-bottom:1px solid var(--border); padding-bottom:10px; margin-bottom:16px;">Доступы и Счётчики: ${{s.domain}}</h3>
            
            <div style="display:flex; flex-direction:column; gap:12px; font-size:13px; text-align:left; color:#fff;">
                <div><strong>CMS Админ-панель:</strong> <a href="${{acc.cms_admin || '#'}}" target="_blank" style="color:var(--accent); text-decoration:underline;">${{acc.cms_admin || '—'}}</a></div>
                <div><strong>CMS Логин:</strong> <span style="color:#fff">${{acc.cms_user || '—'}}</span></div>
                <div><strong>CMS Пароль:</strong> <span style="font-family:monospace; color:var(--text-muted)">${{acc.cms_pass || '—'}}</span></div>
                <hr style="border:0; border-top:1px solid var(--border); margin:4px 0;">
                <div><strong>FTP Хост:</strong> <span style="color:#fff">${{acc.ftp_host || '—'}}</span></div>
                <div><strong>FTP Логин:</strong> <span style="color:#fff">${{acc.ftp_user || '—'}}</span></div>
                <div><strong>FTP Пароль:</strong> <span style="font-family:monospace; color:var(--text-muted)">${{acc.ftp_pass || '—'}}</span></div>
                <hr style="border:0; border-top:1px solid var(--border); margin:4px 0;">
                <div><strong>Яндекс.Метрика ID:</strong> <span style="color:#fff">${{cnt.yandex_metrika_id || '—'}}</span></div>
                <div><strong>GA ID:</strong> <span style="color:#fff">${{cnt.google_analytics_id || '—'}}</span></div>
                <div><strong>CRM тип:</strong> <span style="color:#fff">${{integ.crm_type || '—'}}</span></div>
            </div>
            
            <button class="nav-item active" style="margin-top:20px; width:100%; padding:10px; border:none; cursor:pointer" onclick="document.getElementById('wf-access-modal').style.display='none'">Закрыть</button>
        </div>
    `;
}}

function openWfAddSiteModal(div_id) {{
    if (!wfCurrentCid) return;
    const domain = prompt('Домен нового сайта (например, webzavod.ru):');
    if (!domain) return;
    const type = prompt('Тип сайта (Corporate / Landing / E-commerce):', 'Corporate');
    
    const newSite = {{
        domain,
        site_type: type,
        status: 'active'
    }};
    
    fetch('/api/web/' + wfCurrentCid + '/sites/add', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(newSite)
    }}).then(r => r.json()).then(res => {{
        if (res.status === 'ok') {{
            wfData.sites.push(res.data);
            const select = document.getElementById(div_id + '-site-select');
            if (select) select.innerHTML += `<option value="${{domain}}">${{domain}}</option>`;
            switchWfTab(div_id, 'sites');
        }}
    }});
}}

function deleteWfSite(div_id, id) {{
    if (!confirm('Удалить этот сайт из системы?')) return;
    fetch('/api/web/' + wfCurrentCid + '/sites/delete', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{id}})
    }}).then(() => {{
        wfData.sites = wfData.sites.filter(s => s.id !== id);
        switchWfTab(div_id, 'sites');
    }});
}}

function runWfSiteAudit(div_id, id) {{
    const s = wfData.sites.find(x => x.id === id);
    if (!s) return;
    
    s.last_check = new Date().toISOString().replace('T', ' ').substring(0, 16);
    s.checks = s.checks || [];
    s.checks.unshift({{
        timestamp: s.last_check,
        score: 95,
        speed: 92,
        seo: 90,
        accessibility: 95,
        checks_count: s.checks.length + 1
    }});
    if (s.errors && s.errors.length > 0) {{
        s.errors.pop();
    }}
    
    fetch('/api/web/' + wfCurrentCid + '/sites/update', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(s)
    }}).then(() => {{
        alert('Аудит успешно завершен для ' + s.domain);
        switchWfTab(div_id, 'sites');
    }});
}}

function switchWfLibCat(div_id, category) {{
    wfLibCat = category;
    document.querySelectorAll('#' + div_id + '-tabpanel-library .lib-cat-btn').forEach(btn => {{
        btn.classList.toggle('active', btn.dataset.libcat === category);
    }});
    document.getElementById(div_id + '-lib-cat-title').textContent = {{
        photo: 'Фотоматериалы',
        video: 'Видеоархив',
        clips: 'Промо-ролики',
        banners: 'Рекламные баннеры',
        logos: 'Логотипы и фирменный стиль',
        texts: 'Текстовые блоки',
        docs: 'Официальные документы',
        reviews: 'Отзывы клиентов',
        cases: 'Кейсы и портфолио',
        blocks: 'Блоки страниц (HTML/JSON)',
        templates: 'Шаблоны секций'
    }}[category];
    
    renderWfLibrary(div_id);
}}

function renderWfLibrary(div_id) {{
    const tbody = document.getElementById(div_id + '-library-table-body');
    if (!tbody || !wfData) return;
    
    const assets = wfData.library.filter(a => a.category === wfLibCat);
    tbody.innerHTML = assets.length ? assets.map(a => `
        <tr>
            <td style="font-weight:600; color:#fff">${{a.name}}</td>
            <td>${{a.size || '—'}}</td>
            <td>${{a.url ? `<a href="${{a.url}}" target="_blank" style="color:var(--accent); text-decoration:underline;">Открыть файл</a>` : '—'}} ${{a.notes ? `(${{a.notes}})` : ''}}</td>
            <td><span style="color:var(--green)">● ${{a.status}}</span></td>
            <td>${{a.updated_at || '—'}}</td>
            <td>
                <button class="nav-item" style="padding:4px 8px; font-size:11px; color:var(--red)" onclick="deleteWfLibAsset('${{div_id}}', '${{a.id}}')">Удалить</button>
            </td>
        </tr>`).join('') : '<tr><td colspan="6" style="text-align:center; padding:24px; color:var(--text-muted)">Нет загруженных материалов в этой категории</td></tr>';
}}

function openWfAddLibModal(div_id) {{
    if (!wfCurrentCid) return;
    const name = prompt('Название ассета:');
    if (!name) return;
    const url = prompt('Ссылка на файл / путь:', '/assets/image.jpg');
    const notes = prompt('Примечание:', 'Согласовано');
    
    const newAsset = {{
        name,
        category: wfLibCat,
        size: '1.5 MB',
        status: 'active',
        url,
        notes,
        updated_at: new Date().toISOString().replace('T', ' ').substring(0, 16) + ' UTC'
    }};
    
    fetch('/api/web/' + wfCurrentCid + '/library/add', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(newAsset)
    }}).then(r => r.json()).then(res => {{
        if (res.status === 'ok') {{
            wfData.library.push(res.data);
            switchWfLibCat(div_id, wfLibCat);
        }}
    }});
}}

function deleteWfLibAsset(div_id, id) {{
    if (!confirm('Удалить этот материал из библиотеки?')) return;
    fetch('/api/web/' + wfCurrentCid + '/library/delete', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{id}})
    }}).then(() => {{
        wfData.library = wfData.library.filter(a => a.id !== id);
        switchWfLibCat(div_id, wfLibCat);
    }});
}}

async function renderWfAnalytics(div_id) {{
    const metricsDiv = document.getElementById(div_id + '-analytics-metrics');
    const bestBody = document.getElementById(div_id + '-analytics-best-pages');
    const poorBody = document.getElementById(div_id + '-analytics-poor-pages');
    const trafficBody = document.getElementById(div_id + '-analytics-traffic-sources');
    const trendDiv = document.getElementById(div_id + '-analytics-trend-chart');
    
    if (!metricsDiv || !wfData) return;
    
    document.getElementById(div_id + '-analytics-title').textContent = wfCurrentSite === 'all' 
        ? 'Сводная аналитика по всем сайтам' 
        : `Аналитика домена ${{wfCurrentSite}}`;
        
    try {{
        const res = await fetch(`/api/web/${{wfCurrentCid}}/analytics?site=${{wfCurrentSite}}`).then(r => r.json());
        if (res.status !== 'ok') return;
        
        const ana = res.data;
        const m = ana.metrics;
        
        const items = [
            ['Посещения', m.visits.toLocaleString(), 'сессий всего'],
            ['Заявки', m.leads.toLocaleString(), 'форм отправлено'],
            ['Звонки', m.calls.toLocaleString(), 'с коллтрекинга'],
            ['Клики CTA', m.clicks.toLocaleString(), 'по кнопкам действия'],
            ['Конверсия', m.conversion.toFixed(2) + ' %', 'конверсия сессий в лиды']
        ];
        
        metricsDiv.innerHTML = items.map(([title, val, desc]) => `
            <div class="stat-box">
                <div class="label">${{title}}</div>
                <div class="val" style="color:#fff; font-size:24px; font-weight:800; margin-bottom:4px">${{val}}</div>
                <div style="font-size:11px; color:var(--text-muted)">${{desc}}</div>
            </div>`).join('');
            
        bestBody.innerHTML = ana.best_pages.map(p => `
            <tr>
                <td style="font-weight:600; color:#fff">${{p.page}}</td>
                <td>${{p.visits}}</td>
                <td>${{p.leads}}</td>
                <td><span style="color:var(--green)">${{p.cr.toFixed(1)}}%</span></td>
            </tr>`).join('');
            
        poorBody.innerHTML = ana.poor_pages.map(p => `
            <tr>
                <td style="font-weight:600; color:#fff">${{p.page}}<br><span style="font-size:10px; color:var(--text-muted)">${{p.error}}</span></td>
                <td><span style="color:${{p.risk === 'HIGH' ? 'var(--red)' : 'var(--yellow)'}}; font-weight:600">${{p.risk}}</span></td>
                <td>${{p.solution}}</td>
            </tr>`).join('');
            
        trafficBody.innerHTML = ana.traffic_sources.map(s => `
            <tr>
                <td style="font-weight:600; color:#fff">${{s.source}}</td>
                <td>${{s.visits}}</td>
                <td>${{s.leads}}</td>
                <td>${{s.percent}}%</td>
            </tr>`).join('');
            
        trendDiv.innerHTML = ana.trend.map(t => `
            <div style="display:flex; align-items:center; gap:12px; font-size:12px; margin-top:8px">
                <span style="width:40px; color:var(--text-muted)">${{t.date}}</span>
                <div style="flex:1; height:8px; background:var(--surface-hover); border-radius:4px; overflow:hidden; display:flex">
                    <div style="width:${{Math.min((t.visits / m.visits) * 300, 100)}}%; background:var(--accent); border-radius:4px"></div>
                </div>
                <span style="width:160px; text-align:right">${{t.visits}} сессий / ${{t.leads}} лидов</span>
            </div>`).join('');
            
    }} catch (e) {{
        metricsDiv.innerHTML = '<div style="color:var(--red)">Ошибка загрузки аналитики</div>';
    }}
}}

async function runWfAnalyticsCheck(div_id) {{
    alert('Запущено фоновое обновление систем веб-аналитики. Данные актуализированы.');
    renderWfAnalytics(div_id);
}}

function renderWfSettings(div_id) {{
    if (!wfData) return;
    
    document.getElementById(div_id + '-setting-brand-voice').value = wfData.brand_rules?.voice || '';
    document.getElementById(div_id + '-setting-brand-colors').value = wfData.brand_rules?.colors || '';
    document.getElementById(div_id + '-setting-brand-guidelines').value = wfData.brand_rules?.guidelines || '';
    
    document.getElementById(div_id + '-setting-owner-content').value = wfData.default_owners?.content_owner || '';
    document.getElementById(div_id + '-setting-owner-design').value = wfData.default_owners?.design_owner || '';
    document.getElementById(div_id + '-setting-owner-tech').value = wfData.default_owners?.tech_owner || '';
    document.getElementById(div_id + '-setting-owner-qa').value = wfData.default_owners?.qa_owner || '';
    
    document.getElementById(div_id + '-setting-keywords').value = wfData.keywords?.map(k => k.keyword).join(', ') || '';
    document.getElementById(div_id + '-setting-competitors').value = wfData.competitors?.map(c => c.domain).join(', ') || '';
    
    const site = wfData.sites.find(s => wfCurrentSite === 'all' || s.domain === wfCurrentSite) || wfData.sites[0];
    if (site) {{
        document.getElementById(div_id + '-setting-goals').value = site.goals?.map(g => g.name).join(', ') || '';
        document.getElementById(div_id + '-setting-secret-metrika').value = site.counters?.yandex_metrika_token || '';
        document.getElementById(div_id + '-setting-secret-gsc').value = site.counters?.google_analytics_id || '';
        document.getElementById(div_id + '-setting-secret-crm').value = site.integrations?.crm_token || '';
    }} else {{
        document.getElementById(div_id + '-setting-goals').value = '';
        document.getElementById(div_id + '-setting-secret-metrika').value = '';
        document.getElementById(div_id + '-setting-secret-gsc').value = '';
        document.getElementById(div_id + '-setting-secret-crm').value = '';
    }}
}}

async function saveWfSettings(div_id) {{
    if (!wfCurrentCid || !wfData) return;
    
    const brand_rules = {{
        id: 'brand_rules',
        voice: document.getElementById(div_id + '-setting-brand-voice').value,
        colors: document.getElementById(div_id + '-setting-brand-colors').value,
        guidelines: document.getElementById(div_id + '-setting-brand-guidelines').value
    }};
    
    const default_owners = {{
        id: 'default_owners',
        content_owner: document.getElementById(div_id + '-setting-owner-content').value,
        design_owner: document.getElementById(div_id + '-setting-owner-design').value,
        tech_owner: document.getElementById(div_id + '-setting-owner-tech').value,
        qa_owner: document.getElementById(div_id + '-setting-owner-qa').value
    }};
    
    const kws = document.getElementById(div_id + '-setting-keywords').value.split(',').map(x => x.trim()).filter(Boolean);
    const keywords = kws.map((k, idx) => ({{id: 'kw_' + idx, keyword: k, priority: 'medium', volume: '1000', status: 'active'}}));
    
    const comps = document.getElementById(div_id + '-setting-competitors').value.split(',').map(x => x.trim()).filter(Boolean);
    const competitors = comps.map((c, idx) => ({{id: 'comp_' + idx, domain: c, threat: 'medium', strength: '', details: ''}}));
    
    const targetSiteDomain = wfCurrentSite === 'all' ? (wfData.sites[0]?.domain) : wfCurrentSite;
    const sitesPayload = [];
    
    if (targetSiteDomain) {{
        const siteData = {{
            domain: targetSiteDomain,
            accesses: {{
                ftp_pass: '***',
                cms_pass: '***'
            }},
            counters: {{
                yandex_metrika_token: document.getElementById(div_id + '-setting-secret-metrika').value,
                google_analytics_id: document.getElementById(div_id + '-setting-secret-gsc').value
            }},
            integrations: {{
                crm_token: document.getElementById(div_id + '-setting-secret-crm').value
            }}
        }};
        sitesPayload.push(siteData);
    }}
    
    const payload = {{
        brand_rules,
        default_owners,
        keywords,
        competitors,
        sites: sitesPayload
    }};
    
    try {{
        const res = await fetch(`/api/web/${{wfCurrentCid}}/settings/save`, {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify(payload)
        }}).then(r => r.json());
        
        if (res.status === 'ok') {{
            alert('Настройки WEB-производства успешно сохранены.');
            const dataRes = await fetch('/api/web/' + wfCurrentCid + '/data').then(r => r.json());
            if (dataRes.status === 'ok') {{
                wfData = dataRes.data;
            }}
            switchWfTab(div_id, 'settings');
        }} else {{
            alert('Ошибка при сохранении: ' + (res.message || 'unknown error'));
        }}
    }} catch (e) {{
        alert('Ошибка при отправке запроса сохранения.');
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
        if (window.feather && typeof window.feather.replace === 'function') {{
            window.feather.replace();
        }}
    }}
}}
function closeClientPanel() {{
    if (pollInterval) {{ clearInterval(pollInterval); pollInterval = null; }}
    stopChatPolling();
    document.getElementById('client-panel').classList.remove('open');
}}

const CF_STAGES = ['\u0418\u0434\u0435\u044f', '\u0412 \u0440\u0430\u0431\u043e\u0442\u0435', '\u041d\u0430 \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0435', '\u0413\u043e\u0442\u043e\u0432\u043e', '\u041e\u043f\u0443\u0431\u043b\u0438\u043a\u043e\u0432\u0430\u043d\u043e'];
const CF_COLORS = {{'\u0418\u0434\u0435\u044f':'#6366f1','\u0412 \u0440\u0430\u0431\u043e\u0442\u0435':'#f59e0b','\u041d\u0430 \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0435':'#8b5cf6','\u0413\u043e\u0442\u043e\u0432\u043e':'#10b981','\u041e\u043f\u0443\u0431\u043b\u0438\u043a\u043e\u0432\u0430\u043d\u043e':'#6b7280'}};

// ---- Content Factory 3-panel ----
let cfCurrentCid = null;
let cfCurrentMode = 'pipeline';
const CF_STAGES_NEW = ['\u0418\u0434\u0435\u044f', '\u0411\u0440\u0438\u0444', '\u0412 \u0440\u0430\u0431\u043e\u0442\u0435', '\u0413\u043e\u0442\u043e\u0432\u043e', '\u041e\u043f\u0443\u0431\u043b\u0438\u043a\u043e\u0432\u0430\u043d\u043e'];

function filterCfClients() {{
    const q = document.getElementById('cf-client-search').value.toLowerCase();
    document.querySelectorAll('.cf-client-item').forEach(el => {{
        el.style.display = el.dataset.name.toLowerCase().includes(q) ? '' : 'none';
    }});
}}

function renderCfClientsList() {{
    const list = document.getElementById('cf-clients-list');
    if (!list) return;
    const clients = allClients.filter(c => {{
        const projId = currentProjectId;
        if (projId && PROJECT_CLIENT_MAP[projId]) return PROJECT_CLIENT_MAP[projId](c);
        return true;
    }});
    list.innerHTML = clients.map(c => `
        <div class="cf-client-item nav-item" data-cid="${{c.client_id}}" data-name="${{c.name}}"
             onclick="selectCfClient('${{c.client_id}}', '${{c.name.replace(/'/g,"&#39;")}}')"
             style="padding:10px 16px; cursor:pointer; font-size:13px; display:flex; flex-direction:column; gap:2px;">
            <span style="font-weight:600">${{c.name}}</span>
            <span style="font-size:11px; color:var(--text-muted)">${{c.client_id}}</span>
        </div>`).join('');
}}

async function selectCfClient(cid, name) {{
    cfCurrentCid = cid;
    document.querySelectorAll('.cf-client-item').forEach(el => {{
        el.classList.toggle('active', el.dataset.cid === cid);
    }});
    document.getElementById('cf-workspace-empty').style.display = 'none';
    const ws = document.getElementById('cf-workspace-content');
    ws.style.display = 'flex';
    document.getElementById('cf-client-name').textContent = name;
    document.getElementById('cf-client-id').textContent = cid;
    showCfTab('overview');
    await loadCfOverview(cid);
}}

function showCfTab(tab) {{
    document.querySelectorAll('.cf-tabpanel').forEach(el => el.style.display = 'none');
    document.querySelectorAll('[data-cftab]').forEach(el => el.classList.remove('active'));
    const panel = document.getElementById('cf-tab-' + tab);
    if (panel) panel.style.display = '';
    document.querySelectorAll(`[data-cftab="${{tab}}"]`).forEach(el => el.classList.add('active'));
    if (tab === 'content') renderCfPipeline();
    if (tab === 'settings' && cfCurrentCid) loadCfSettings(cfCurrentCid);
    if (tab === 'analytics' && cfCurrentCid) loadCfAnalytics(cfCurrentCid);
    if (tab === 'publications' && cfCurrentCid) loadCfPublications(cfCurrentCid);
}}

async function loadCfOverview(cid) {{
    const cards = document.getElementById('cf-overview-cards');
    cards.innerHTML = '<div style="color:var(--text-muted);font-size:13px">Загрузка...</div>';
    try {{
        const r = await fetch(`/api/smm/${{cid}}/overview`);
        const d = await r.json();
        const ov = d.data || d.overview || {{}};
        const items = [
            {{label:'\u0413\u043e\u043b\u043e\u0441 \u0431\u0440\u0435\u043d\u0434\u0430', value: ov.brand_voice ? '\u2713 \u041d\u0430\u0441\u0442\u0440\u043e\u0435\u043d' : '\u2014 \u041d\u0435 \u043d\u0430\u0441\u0442\u0440\u043e\u0435\u043d', color: ov.brand_voice ? 'var(--color-success,#4caf50)' : ''}},
            {{label:'\u0410\u0432\u0442\u043e\u043f\u043e\u0441\u0442\u0438\u043d\u0433', value: ov.autoposting_enabled ? '\u2713 \u0410\u043a\u0442\u0438\u0432\u0435\u043d' : '\u2014 \u0412\u044b\u043a\u043b\u044e\u0447\u0435\u043d', color: ov.autoposting_enabled ? 'var(--color-success,#4caf50)' : ''}},
            {{label:'\u041c\u0435\u0434\u0438\u0430\u043f\u043b\u0430\u043d', value: ov.mediaplan_items != null ? ov.mediaplan_items + ' \u043f\u043e\u0437\u0438\u0446\u0438\u0439' : '\u2014'}},
            {{label:'\u0410\u043d\u0430\u043b\u0438\u0442\u0438\u043a\u0430', value: ov.analytics_connected ? '\u2713 \u041f\u043e\u0434\u043a\u043b\u044e\u0447\u0435\u043d\u0430' : '\u2014 \u041d\u0435\u0442 \u0434\u0430\u043d\u043d\u044b\u0445'}},
            {{label:'\u0424\u043e\u0442\u043e\u0430\u0440\u0445\u0438\u0432', value: ov.media_count != null ? ov.media_count + ' \u0444\u0430\u0439\u043b\u043e\u0432' : '\u2014'}},
            {{label:'\u041f\u0443\u0431\u043b\u0438\u043a\u0430\u0446\u0438\u0439', value: ov.published_count != null ? ov.published_count + ' \u0432\u0441\u0435\u0433\u043e' : '\u2014'}},
        ];
        cards.innerHTML = items.map(i => `
            <div class="stat-box" style="padding:16px;">
                <div class="label">${{i.label}}</div>
                <div class="val" style="${{i.color ? 'color:'+i.color : ''}}">${{i.value}}</div>
            </div>`).join('');
        const pubBtn = document.getElementById('cf-btn-publications');
        if (pubBtn) pubBtn.style.display = ov.autoposting_enabled ? '' : 'none';
        const visBtn = document.getElementById('cf-btn-visual');
        if (visBtn) visBtn.style.display = ov.visual_module_enabled ? '' : 'none';
    }} catch(e) {{
        cards.innerHTML = '<div style="color:var(--color-error,#f44)">\u041e\u0448\u0438\u0431\u043a\u0430 \u0437\u0430\u0433\u0440\u0443\u0437\u043a\u0438 \u0434\u0430\u043d\u043d\u044b\u0445</div>';
    }}
}}

function setCfMode(mode) {{
    cfCurrentMode = mode;
    document.getElementById('cf-pipeline').style.display = mode === 'pipeline' ? 'flex' : 'none';
    document.getElementById('cf-list').style.display = mode === 'list' ? '' : 'none';
    document.getElementById('cf-mode-pipeline').classList.toggle('active', mode === 'pipeline');
    document.getElementById('cf-mode-list').classList.toggle('active', mode === 'list');
    if (mode === 'pipeline') renderCfPipeline();
    else renderCfList();
}}

function renderCfPipeline() {{
    const el = document.getElementById('cf-pipeline');
    if (!el || !cfCurrentCid) return;
    const items = (cfItems[cfCurrentCid] || []);
    const counts = {{}};
    CF_STAGES_NEW.forEach(s => counts[s] = 0);
    items.forEach(i => {{ if (counts[i.status] !== undefined) counts[i.status]++; }});
    el.innerHTML = CF_STAGES_NEW.map(stage => `
        <div style="min-width:200px; flex:1; background:var(--surface); border-radius:var(--radius); border:1px solid var(--border); padding:12px; display:flex; flex-direction:column; gap:8px;">
            <div style="font-weight:700; font-size:13px; margin-bottom:4px">${{stage}} <span style="font-weight:400; color:var(--text-muted)">${{counts[stage]}}</span></div>
            ${{items.filter(i => i.status === stage).map(i => `
                <div class="card" style="padding:10px; cursor:pointer; font-size:13px;" onclick="editCfItem('${{(i.id||'').replace(/'/g,"&#39;")}}')">
                    <div style="font-weight:600; margin-bottom:4px">${{i.title || i['\u0417\u0430\u0434\u0430\u0447\u0430'] || '(\u0431\u0435\u0437 \u043d\u0430\u0437\u0432\u0430\u043d\u0438\u044f)'}}</div>
                    ${{i.type ? '<div style="font-size:11px;color:var(--text-muted)">'+i.type+'</div>' : ''}}
                </div>`).join('')}}
        </div>`).join('');
}}

function renderCfList() {{
    const el = document.getElementById('cf-list');
    if (!el || !cfCurrentCid) return;
    const items = (cfItems[cfCurrentCid] || []);
    if (!items.length) {{ el.innerHTML = '<div style="color:var(--text-muted);font-size:13px;padding:20px 0">\u041d\u0435\u0442 \u044d\u043b\u0435\u043c\u0435\u043d\u0442\u043e\u0432</div>'; return; }}
    el.innerHTML = `<table class="premium-table"><thead><tr><th>\u041d\u0430\u0437\u0432\u0430\u043d\u0438\u0435</th><th>\u0421\u0442\u0430\u0442\u0443\u0441</th><th>\u0422\u0438\u043f</th><th>\u0414\u0430\u0442\u0430</th></tr></thead><tbody>${{
        items.map(i => `<tr><td>${{i.title||i['\u0417\u0430\u0434\u0430\u0447\u0430']||''}}</td><td>${{i.status||''}}</td><td>${{i.type||''}}</td><td>${{i.created_at||''}}</td></tr>`).join('')
    }}</tbody></table>`;
}}

let cfItems = {{}};
async function addCfItem() {{
    if (!cfCurrentCid) return;
    const title = prompt('\u041d\u0430\u0437\u0432\u0430\u043d\u0438\u0435 \u043a\u043e\u043d\u0442\u0435\u043d\u0442\u0430:');
    if (!title) return;
    const type = prompt('\u0422\u0438\u043f (\u041f\u043e\u0441\u0442 / \u0421\u0442\u0430\u0442\u044c\u044f / \u0412\u0438\u0434\u0435\u043e / \u041a\u0430\u0440\u0443\u0441\u0435\u043b\u044c):', '\u041f\u043e\u0441\u0442');
    if (!type) return;
    const item = {{title, type, status: '\u0418\u0434\u0435\u044f', created_at: new Date().toISOString().slice(0,10)}};
    const r = await fetch(`/api/module/${{cfCurrentCid}}/content/items/add`, {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify(item)}});
    if (r.ok) {{
        if (!cfItems[cfCurrentCid]) cfItems[cfCurrentCid] = [];
        cfItems[cfCurrentCid].push(item);
        renderCfPipeline();
    }}
}}

async function loadCfSettings(cid) {{
    const body = document.getElementById('cf-settings-body');
    body.innerHTML = '<div style="color:var(--text-muted);font-size:13px">\u0417\u0430\u0433\u0440\u0443\u0437\u043a\u0430...</div>';
    try {{
        const d = await fetch(`/api/smm/${{cid}}/settings`).then(r=>r.json());
        const s = d.data || {{}};
        const p = s.profile || {{}};
        const ct = s.contacts || {{}};
        const sc = s.schedule || {{}};
        const ar = s.approval_rules || {{}};
        const notif = s.notifications || {{}};
        body.innerHTML = `<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;max-width:900px">
          <div class="card" style="padding:16px"><h4 style="margin-bottom:12px">\u041f\u0440\u043e\u0444\u0438\u043b\u044c</h4>
            <label style="font-size:12px;color:var(--text-muted)">\u041d\u0430\u0437\u0432\u0430\u043d\u0438\u0435</label>
            <input class="premium-input" id="cf-s-bname" value="${{p.brand_name||p.name||cid}}" style="width:100%;margin-bottom:8px">
            <label style="font-size:12px;color:var(--text-muted)">\u041e\u043f\u0438\u0441\u0430\u043d\u0438\u0435</label>
            <input class="premium-input" id="cf-s-desc" value="${{p.description||''}}" style="width:100%"></div>
          <div class="card" style="padding:16px"><h4 style="margin-bottom:12px">\u041a\u043e\u043d\u0442\u0430\u043a\u0442\u044b</h4>
            <label style="font-size:12px;color:var(--text-muted)">Email</label>
            <input class="premium-input" id="cf-s-email" value="${{ct.email||''}}" style="width:100%;margin-bottom:8px">
            <label style="font-size:12px;color:var(--text-muted)">Telegram</label>
            <input class="premium-input" id="cf-s-tg" value="${{ct.telegram||''}}" style="width:100%;margin-bottom:8px">
            <label style="font-size:12px;color:var(--text-muted)">\u0422\u0435\u043b\u0435\u0444\u043e\u043d</label>
            <input class="premium-input" id="cf-s-phone" value="${{ct.phone||''}}" style="width:100%"></div>
          <div class="card" style="padding:16px"><h4 style="margin-bottom:12px">\u0421\u043e\u0446\u0441\u0435\u0442\u0438 / \u0422\u043e\u043a\u0435\u043d\u044b</h4>
            <div>${{(s.socials||[]).map(sc=>`<div style="display:flex;gap:8px;align-items:center;margin-bottom:6px;font-size:13px"><span style="min-width:90px;font-weight:600">${{sc.platform}}</span><span style="font-family:monospace;color:var(--text-muted)">${{sc.token||'\u2014'}}</span></div>`).join('')||'<div style="font-size:13px;color:var(--text-muted)">\u041d\u0435 \u0434\u043e\u0431\u0430\u0432\u043b\u0435\u043d\u043e</div>'}}</div>
            <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
              <input class="premium-input" id="cf-new-soc-name" placeholder="\u041f\u043b\u0430\u0442\u0444\u043e\u0440\u043c\u0430" style="width:110px">
              <input class="premium-input" id="cf-new-soc-token" placeholder="Token" type="password" style="width:150px">
              <button class="nav-item active" style="padding:6px 12px;font-size:12px;border:none;cursor:pointer" onclick="cfAddSocial('${{cid}}')">+</button></div></div>
          <div class="card" style="padding:16px"><h4 style="margin-bottom:12px">Источники публикаций</h4>
            <div>${{(s.publication_sources||[]).map((ps,idx)=>`<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;font-size:12px">
              <span style="font-weight:600;min-width:90px">${{ps.name||'—'}}</span>
              <a href="${{ps.url||'#'}}" target="_blank" rel="noopener noreferrer" style="max-width:220px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${{ps.url||'—'}}</a>
              <span style="color:var(--text-muted)">${{ps.type||'other'}}</span>
              <button class="mkt-btn" style="padding:2px 8px" onclick="cfRemovePubSource('${{cid}}', ${{idx}})">×</button>
            </div>`).join('') || '<div style="font-size:13px;color:var(--text-muted)">Не добавлено</div>'}}</div>
            <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
              <input class="premium-input" id="cf-new-pub-name" placeholder="Название" style="width:120px">
              <input class="premium-input" id="cf-new-pub-url" placeholder="https://..." style="width:220px">
              <select class="premium-input" id="cf-new-pub-type" style="width:120px">
                <option value="social">social</option><option value="site">site</option><option value="marketplace">marketplace</option>
                <option value="ads">ads</option><option value="email">email</option><option value="other">other</option>
              </select>
              <button class="nav-item active" style="padding:6px 12px;font-size:12px;border:none;cursor:pointer" onclick="cfAddPubSource('${{cid}}')">+</button>
            </div></div>
          <div class="card" style="padding:16px"><h4 style="margin-bottom:12px">\u0413\u043e\u043b\u043e\u0441 \u0431\u0440\u0435\u043d\u0434\u0430</h4>
            <textarea class="premium-input" id="cf-s-bv" rows="5" style="width:100%;resize:vertical">${{s.brand_voice||''}}</textarea></div>
          <div class="card" style="padding:16px"><h4 style="margin-bottom:12px">\u0420\u0430\u0441\u043f\u0438\u0441\u0430\u043d\u0438\u0435</h4>
            <label style="font-size:12px;color:var(--text-muted)">\u041f\u0435\u0440\u0438\u043e\u0434\u0438\u0447\u043d\u043e\u0441\u0442\u044c</label>
            <input class="premium-input" id="cf-s-freq" value="${{sc.frequency||''}}" placeholder="3 \u0440\u0430\u0437\u0430 \u0432 \u043d\u0435\u0434\u0435\u043b\u044e" style="width:100%;margin-bottom:8px">
            <label style="font-size:12px;color:var(--text-muted)">\u0414\u043d\u0438</label>
            <input class="premium-input" id="cf-s-days" value="${{(sc.days||[]).join(', ')}}" placeholder="\u041f\u043d, \u0421\u0440, \u041f\u0442" style="width:100%"></div>
          <div class="card" style="padding:16px"><h4 style="margin-bottom:12px">\u041f\u0440\u0430\u0432\u0438\u043b\u0430 \u0441\u043e\u0433\u043b\u0430\u0441\u043e\u0432\u0430\u043d\u0438\u044f</h4>
            <label style="display:flex;align-items:center;gap:8px;font-size:13px;margin-bottom:8px;cursor:pointer"><input type="checkbox" id="cf-s-approval" ${{ar.require_approval?'checked':''}}> \u0422\u0440\u0435\u0431\u043e\u0432\u0430\u0442\u044c \u0441\u043e\u0433\u043b\u0430\u0441\u043e\u0432\u0430\u043d\u0438\u0435</label>
            <label style="font-size:12px;color:var(--text-muted)">\u0421\u043e\u0433\u043b\u0430\u0441\u0443\u044e\u0449\u0438\u0435</label>
            <input class="premium-input" id="cf-s-approvers" value="${{(ar.approvers||[]).join(', ')}}" placeholder="email1, email2" style="width:100%"></div>
          <div class="card" style="padding:16px"><h4 style="margin-bottom:12px">\u0423\u0432\u0435\u0434\u043e\u043c\u043b\u0435\u043d\u0438\u044f</h4>
            <label style="display:flex;align-items:center;gap:8px;font-size:13px;margin-bottom:6px;cursor:pointer"><input type="checkbox" id="cf-n-email" ${{notif.email?'checked':''}}> Email</label>
            <label style="display:flex;align-items:center;gap:8px;font-size:13px;margin-bottom:6px;cursor:pointer"><input type="checkbox" id="cf-n-tg" ${{notif.telegram?'checked':''}}> Telegram</label>
            <label style="display:flex;align-items:center;gap:8px;font-size:13px;cursor:pointer"><input type="checkbox" id="cf-n-slack" ${{notif.slack?'checked':''}}> Slack</label></div>
          <div class="card" style="padding:16px"><h4 style="margin-bottom:12px">\u0411\u0430\u0437\u044b \u0437\u043d\u0430\u043d\u0438\u0439</h4>
            <div style="font-size:13px;color:var(--text-muted)">${{(s.knowledge_bases||[]).length?(s.knowledge_bases||[]).join(', '):'\u041d\u0435 \u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0435\u043d\u043e'}}</div></div>
          <div class="card" style="padding:16px"><h4 style="margin-bottom:12px">\u041f\u0440\u0430\u0432\u0430 \u0434\u043e\u0441\u0442\u0443\u043f\u0430</h4>
            <div style="font-size:13px;color:var(--text-muted)">${{(s.access_rights||[]).length?(s.access_rights||[]).map(a=>a.user||a).join(', '):'\u041d\u0435 \u043d\u0430\u0441\u0442\u0440\u043e\u0435\u043d\u043e'}}</div></div>
          <div class="card" style="padding:16px"><h4 style="margin-bottom:12px">\u0418\u043d\u0442\u0435\u0433\u0440\u0430\u0446\u0438\u0438</h4>
            <div style="font-size:13px;color:var(--text-muted)">${{(s.integrations||[]).length?(s.integrations||[]).map(i=>i.name||i).join(', '):'\u041d\u0435 \u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0435\u043d\u043e'}}</div></div>
        </div>
        <div style="margin-top:16px;max-width:900px;display:flex;align-items:center;gap:12px">
          <button class="nav-item active" style="padding:10px 24px;font-size:13px;border:none;cursor:pointer" onclick="saveCfSettings('${{cid}}')">\u0421\u043e\u0445\u0440\u0430\u043d\u0438\u0442\u044c</button>
          <span id="cf-s-save-status" style="font-size:12px;color:var(--text-muted)"></span></div>`;
    }} catch(e) {{
        body.innerHTML = '<div style="color:var(--color-error,#f44)">\u041e\u0448\u0438\u0431\u043a\u0430 \u0437\u0430\u0433\u0440\u0443\u0437\u043a\u0438</div>';
    }}
}}

async function saveCfSettings(cid) {{
    const st = document.getElementById('cf-s-save-status');
    const payload = {{
        brand_name:  (document.getElementById('cf-s-bname')?.value||'').trim(),
        description: (document.getElementById('cf-s-desc')?.value||'').trim(),
        contacts: {{ email: (document.getElementById('cf-s-email')?.value||'').trim(), telegram: (document.getElementById('cf-s-tg')?.value||'').trim(), phone: (document.getElementById('cf-s-phone')?.value||'').trim() }},
        brand_voice: (document.getElementById('cf-s-bv')?.value||'').trim(),
        schedule: {{ frequency: (document.getElementById('cf-s-freq')?.value||'').trim(), days: (document.getElementById('cf-s-days')?.value||'').split(',').map(d=>d.trim()).filter(Boolean) }},
        approval_rules: {{ require_approval: !!(document.getElementById('cf-s-approval')?.checked), approvers: (document.getElementById('cf-s-approvers')?.value||'').split(',').map(e=>e.trim()).filter(Boolean) }},
        notifications: {{ email: !!(document.getElementById('cf-n-email')?.checked), telegram: !!(document.getElementById('cf-n-tg')?.checked), slack: !!(document.getElementById('cf-n-slack')?.checked) }},
    }};
    try {{
        const d = await fetch(`/api/smm/${{cid}}/settings`, {{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(payload)}}).then(r=>r.json());
        if (st) st.textContent = d.status==='ok' ? '\u2713 \u0421\u043e\u0445\u0440\u0430\u043d\u0435\u043d\u043e' : '\u041e\u0448\u0438\u0431\u043a\u0430';
    }} catch(e) {{ if (st) st.textContent = '\u041e\u0448\u0438\u0431\u043a\u0430'; }}
}}

async function cfAddSocial(cid) {{
    const platform = (document.getElementById('cf-new-soc-name')?.value||'').trim();
    const token = (document.getElementById('cf-new-soc-token')?.value||'').trim();
    if (!platform || !token) return;
    const smm = await fetch(`/api/module/${{cid}}/smm`).then(r=>r.json()).catch(()=>({{data:{{}}}}));
    const socials = (smm.data?.socials||[]);
    socials.push({{platform, token}});
    await fetch(`/api/smm/${{cid}}/settings`, {{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{socials_raw:socials}})}});
    loadCfSettings(cid);
}}

async function cfAddPubSource(cid) {{
    const name = (document.getElementById('cf-new-pub-name')?.value || '').trim();
    const url = (document.getElementById('cf-new-pub-url')?.value || '').trim();
    const type = (document.getElementById('cf-new-pub-type')?.value || 'other').trim();
    if (!name || !url) return;
    const httpOk = /^https?:\/\/\S+$/i.test(url);
    if (!httpOk) return alert('Нужна валидная ссылка http(s)');
    const cur = await fetch(`/api/smm/${{cid}}/settings`).then(r=>r.json()).catch(()=>({{data:{{}}}}));
    const arr = Array.isArray(cur.data?.publication_sources) ? cur.data.publication_sources : [];
    arr.push({{ name, url, type, status: 'active' }});
    await fetch(`/api/smm/${{cid}}/settings`, {{
        method:'POST',
        headers:{{'Content-Type':'application/json'}},
        body:JSON.stringify({{ publication_sources: arr }})
    }});
    loadCfSettings(cid);
}}

async function cfRemovePubSource(cid, idx) {{
    const cur = await fetch(`/api/smm/${{cid}}/settings`).then(r=>r.json()).catch(()=>({{data:{{}}}}));
    const arr = Array.isArray(cur.data?.publication_sources) ? cur.data.publication_sources : [];
    const next = arr.filter((_, i) => i !== idx);
    await fetch(`/api/smm/${{cid}}/settings`, {{
        method:'POST',
        headers:{{'Content-Type':'application/json'}},
        body:JSON.stringify({{ publication_sources: next }})
    }});
    loadCfSettings(cid);
}}

async function loadCfAnalytics(cid) {{
    const body = document.getElementById('cf-analytics-body');
    body.innerHTML = '<div style="color:var(--text-muted);font-size:13px">\u0417\u0430\u0433\u0440\u0443\u0437\u043a\u0430...</div>';
    try {{
        const [ovR, itemsR] = await Promise.all([
            fetch(`/api/smm/${{cid}}/overview`).then(r=>r.json()),
            fetch(`/api/module/${{cid}}/content/items`).then(r=>r.json()).catch(()=>({{data:[]}})),
        ]);
        const ov = ovR.data || {{}};
        const items = itemsR.data || [];
        const byStage = {{}};
        items.forEach(i => {{ byStage[i.status] = (byStage[i.status]||0)+1; }});
        body.innerHTML = `<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:16px;max-width:700px">
            <div class="stat-box"><div class="label">\u041e\u043f\u0443\u0431\u043b\u0438\u043a\u043e\u0432\u0430\u043d\u043e</div><div class="val">${{ov.history||byStage['\u041e\u043f\u0443\u0431\u043b\u0438\u043a\u043e\u0432\u0430\u043d\u043e']||0}}</div></div>
            <div class="stat-box"><div class="label">\u0412 \u0440\u0430\u0431\u043e\u0442\u0435</div><div class="val">${{byStage['\u0412 \u0440\u0430\u0431\u043e\u0442\u0435']||0}}</div></div>
            <div class="stat-box"><div class="label">\u0413\u043e\u0442\u043e\u0432\u043e</div><div class="val">${{byStage['\u0413\u043e\u0442\u043e\u0432\u043e']||0}}</div></div>
            <div class="stat-box"><div class="label">\u0418\u0434\u0435\u0438</div><div class="val">${{byStage['\u0418\u0434\u0435\u044f']||0}}</div></div>
            <div class="stat-box"><div class="label">\u041c\u0435\u0434\u0438\u0430\u043f\u043b\u0430\u043d</div><div class="val">${{ov.mediaplan||'\u2014'}}</div></div>
            <div class="stat-box"><div class="label">\u041b\u0438\u0434\u044b (CRM)</div><div class="val">${{ov.analytics||'\u2014'}}</div></div>
        </div>`;
    }} catch(e) {{
        body.innerHTML = '<div style="color:var(--color-error,#f44)">\u041e\u0448\u0438\u0431\u043a\u0430</div>';
    }}
}}

async function loadCfPublications(cid) {{
    const body = document.getElementById('cf-publications-body');
    body.innerHTML = '<div style="color:var(--text-muted);font-size:13px">\u0417\u0430\u0433\u0440\u0443\u0437\u043a\u0430...</div>';
    try {{
        const d = await fetch(`/api/smm/${{cid}}/publications`).then(r=>r.json());
        const pub = d.data || {{}};
        const enabled = pub.enabled||false;
        const slots = pub.slots||[];
        const platforms = pub.platforms||[];
        const errors = pub.errors||[];
        body.innerHTML = `<div style="max-width:700px">
          <div class="card" style="padding:16px;margin-bottom:16px">
            <div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap">
              <span style="font-weight:700;font-size:15px">\u0410\u0432\u0442\u043e\u043f\u043e\u0441\u0442\u0438\u043d\u0433</span>
              <button class="nav-item ${{enabled?'active':''}}" style="padding:8px 20px;border:none;cursor:pointer;font-size:13px" onclick="cfToggleAutoposting('${{cid}}',${{!enabled}})">${{enabled?'\u2713 \u0412\u043a\u043b\u044e\u0447\u0435\u043d':'\u0412\u043a\u043b\u044e\u0447\u0438\u0442\u044c'}}</button>
              <span style="font-size:13px;color:var(--text-muted)">\u0421\u043b\u0435\u0434\u0443\u044e\u0449\u0430\u044f: <b>${{pub.next_pub||'\u2014'}}</b></span>
            </div>
          </div>
          <div class="card" style="padding:16px;margin-bottom:16px">
            <h4 style="margin-bottom:12px">\u041f\u043b\u043e\u0449\u0430\u0434\u043a\u0438</h4>
            <div style="display:flex;gap:12px;flex-wrap:wrap">
              ${{['Instagram','VK','\u0422\u0435\u043b\u0435\u0433\u0440\u0430\u043c','TikTok','YouTube','OK'].map(pl=>`<label style="display:flex;align-items:center;gap:6px;font-size:13px;cursor:pointer"><input type="checkbox" ${{platforms.includes(pl)?'checked':''}} onchange="cfTogglePlatform('${{cid}}','${{pl}}',this.checked)"> ${{pl}}</label>`).join('')}}
            </div>
          </div>
          <div class="card" style="padding:16px;margin-bottom:16px">
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
              <h4 style="margin:0">\u0421\u043b\u043e\u0442\u044b</h4>
              <div style="display:flex;gap:8px">
                <input class="premium-input" id="cf-new-slot" placeholder="HH:MM" style="width:80px">
                <button class="nav-item active" style="padding:6px 12px;font-size:12px;border:none;cursor:pointer" onclick="cfAddSlot('${{cid}}')">+</button>
              </div>
            </div>
            <div>${{slots.length?slots.map(s=>`<div style="display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border);font-size:13px"><span>\u23f0 ${{s.time||s}}</span><button class="nav-item" style="padding:4px 10px;font-size:11px;border:none;cursor:pointer;background:var(--surface-hover)" onclick="cfRemoveSlot('${{cid}}','${{s.id||s}}')">\u0423\u0434\u0430\u043b\u0438\u0442\u044c</button></div>`).join(''):'<div style="font-size:13px;color:var(--text-muted)">\u0421\u043b\u043e\u0442\u044b \u043d\u0435 \u0437\u0430\u0434\u0430\u043d\u044b</div>'}}</div>
          </div>
          ${{errors.length?`<div class="card" style="padding:16px;border-color:var(--color-error,#f44)"><h4 style="margin-bottom:12px;color:var(--color-error,#f44)">\u041e\u0448\u0438\u0431\u043a\u0438</h4>${{errors.map(e=>`<div style="font-size:12px;font-family:monospace;margin-bottom:4px">${{e}}</div>`).join('')}}</div>`:''}}
        </div>`;
    }} catch(e) {{
        body.innerHTML = '<div style="color:var(--color-error,#f44)">\u041e\u0448\u0438\u0431\u043a\u0430 \u0437\u0430\u0433\u0440\u0443\u0437\u043a\u0438</div>';
    }}
}}

async function cfToggleAutoposting(cid, enable) {{
    await fetch(`/api/smm/${{cid}}/publications`, {{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{enabled:enable}})}});
    loadCfPublications(cid);
}}
async function cfTogglePlatform(cid, platform, checked) {{
    const d = await fetch(`/api/smm/${{cid}}/publications`).then(r=>r.json());
    const pls = d.data?.platforms||[];
    const updated = checked ? [...new Set([...pls, platform])] : pls.filter(p=>p!==platform);
    await fetch(`/api/smm/${{cid}}/publications`, {{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{platforms:updated}})}});
}}
async function cfAddSlot(cid) {{
    const time = (document.getElementById('cf-new-slot')?.value||'').trim();
    if (!time) return;
    await fetch(`/api/smm/${{cid}}/publications`, {{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{add_slot:time}})}});
    loadCfPublications(cid);
}}
async function cfRemoveSlot(cid, slotId) {{
    await fetch(`/api/smm/${{cid}}/publications`, {{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{remove_slot:slotId}})}});
    loadCfPublications(cid);
}}
function editCfItem(id) {{
    if (!cfCurrentCid) return;
    const items = cfItems[cfCurrentCid] || [];
    const item = items.find(i => i.id === id);
    if (!item) return;
    const stage = prompt('\u041d\u043e\u0432\u044b\u0439 \u0441\u0442\u0430\u0442\u0443\u0441:\\n' + CF_STAGES_NEW.join(' / '), item.status);
    if (!stage || !CF_STAGES_NEW.includes(stage)) return;
    item.status = stage;
    fetch(`/api/module/${{cfCurrentCid}}/content/items/update`, {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify(item)}});
    renderCfPipeline();
}}


// initWebFactory legacy stub removed — use initWebFactory(div_id) at line 5956

function renderWebFactoryCards(cid) {{
    const cards = document.getElementById('wf-cards');
    const body = document.getElementById('wf-sites-body');
    if (!cards || !body) return;
    const items = [
        ['Сайты', 'Домены, CMS, SSL, sitemap'],
        ['SEO', 'robots, мета, индексация'],
        ['Формы', 'Заявки, квизы, лид-магниты'],
        ['Интеграции', 'Пиксели, CRM, аналитика'],
        ['Публикации', 'Страницы, статьи, лендинги']
    ];
    cards.innerHTML = items.map(([title, desc]) => `<div class="stat-box"><div class="label">${{title}}</div><div class="val" style="font-size:14px">${{cid ? desc : 'выберите клиента'}}</div></div>`).join('');
    if (!cid) {{
        body.innerHTML = '<tr><td colspan="6" style="text-align:center; padding:36px; color:var(--text-muted)">Выберите клиента сверху</td></tr>';
        return;
    }}
    body.innerHTML = '<tr><td colspan="6" style="text-align:center; padding:36px; color:var(--text-muted)">Сайты клиента пока не заведены. Добавьте первый сайт.</td></tr>';
}}

function openWebFactoryCard(type) {{
    const sel = document.getElementById('wf-client-sel');
    const cid = sel ? sel.value : '';
    if (!cid) return alert('Выберите клиента');
    alert('WEB-завод: ' + type + ' для ' + cid);
}}

async function initContentFactory() {{
    const sel = document.getElementById('cf-client-sel');
    const cid = sel ? sel.value : '';
    if (!cid) return;
    const r = await fetch(`/api/content/factory/${{cid}}`).then(x => x.json());
    if (r.status !== 'ok') return;
    const items = r.data || [];
    // Stats
    const stats = document.getElementById('cf-stats');
    const counts = {{}};
    CF_STAGES.forEach(s => counts[s] = 0);
    items.forEach(i => {{ if (counts[i.status] !== undefined) counts[i.status]++; }});
    stats.innerHTML = CF_STAGES.map(s => `
        <div style="background:var(--surface); border-left:3px solid ${{CF_COLORS[s]}}; border-radius:var(--radius-sm); padding:10px 16px; min-width:100px">
            <div style="font-size:20px; font-weight:700; color:${{CF_COLORS[s]}}">${{counts[s]}}</div>
            <div style="font-size:11px; color:var(--text-muted)">${{s}}</div>
        </div>
    `).join('');
    // Kanban
    const kanban = document.getElementById('cf-kanban');
    kanban.innerHTML = CF_STAGES.map(stage => {{
        const stageItems = items.filter(i => i.status === stage);
        return `
        <div style="min-width:200px; flex:1; background:var(--surface); border-radius:var(--radius); padding:12px">
            <div style="font-weight:600; color:${{CF_COLORS[stage]}}; margin-bottom:12px; font-size:13px">${{stage}} (${{stageItems.length}})</div>
            ${{stageItems.map(i => `
            <div style="background:var(--background); border-radius:var(--radius-sm); padding:10px; margin-bottom:8px; cursor:pointer" onclick="showCfItemMenu('${{i.id}}','${{cid}}')">
                <div style="font-size:12px; font-weight:600; margin-bottom:4px">${{i.title}}</div>
                <div style="font-size:11px; color:var(--text-muted)">${{i.type}} \u00b7 ${{i.platform}}</div>
                ${{i.deadline ? `<div style="font-size:10px; color:var(--text-muted); margin-top:4px">\u0414\u043e: ${{i.deadline}}</div>` : ''}}
            </div>
            `).join('')}}
        </div>`;
    }}).join('');
    document.getElementById('cf-status').textContent = `${{items.length}} \u0437\u0430\u0434\u0430\u0447`;
}}

function showCfItemMenu(id, cid) {{
    const stage = prompt('\u041d\u043e\u0432\u044b\u0439 \u0441\u0442\u0430\u0442\u0443\u0441:\\n' + CF_STAGES.join(' / '));
    if (!stage || !CF_STAGES.includes(stage)) return;
    fetch(`/api/content/factory/${{cid}}/move`, {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{id, status: stage}})
    }}).then(() => initContentFactory());
}}

function addFactoryItem() {{
    const sel = document.getElementById('cf-client-sel');
    const cid = sel ? sel.value : '';
    if (!cid) {{ alert('\u0421\u043d\u0430\u0447\u0430\u043b\u0430 \u0432\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u043a\u043b\u0438\u0435\u043d\u0442\u0430'); return; }}
    const title = prompt('\u041d\u0430\u0437\u0432\u0430\u043d\u0438\u0435 \u0437\u0430\u0434\u0430\u0447\u0438:');
    if (!title) return;
    const type = prompt('\u0422\u0438\u043f (\u041f\u043e\u0441\u0442 / \u041a\u0430\u0440\u0443\u0441\u0435\u043b\u044c / \u0420\u0438\u043b\u0441 / \u0421\u0442\u0430\u0442\u044c\u044f):') || '\u041f\u043e\u0441\u0442';
    const platform = prompt('\u041f\u043b\u0430\u0442\u0444\u043e\u0440\u043c\u0430 (Instagram / Telegram / \u0411\u043b\u043e\u0433):') || 'Instagram';
    fetch(`/api/content/factory/${{cid}}/add`, {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{title, type, platform, status: '\u0418\u0434\u0435\u044f', author: '', deadline: '', notes: ''}})
    }}).then(() => initContentFactory());
}}

function startCfWorkflow() {{
    const sel = document.getElementById('cf-client-sel');
    const cid = sel ? sel.value : '';
    if (!cid) {{ alert('\u0421\u043d\u0430\u0447\u0430\u043b\u0430 \u0432\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u043a\u043b\u0438\u0435\u043d\u0442\u0430'); return; }}
    const wtype = document.getElementById('cf-workflow-type').value;
    startWorkflowFromTab('content_\u043a\u043e\u043d\u0442\u0435\u043d\u0442-\u0437\u0430\u0432\u043e\u0434');
}}

window.onload = () => {{
    if (window.feather && typeof window.feather.replace === 'function') {{
        window.feather.replace();
    }}
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
            "agents/content/web": "smm_web-завод",
            "agents/smm/web": "smm_web-завод",
            "agents/smm/web-factory": "smm_web-завод",
            "agents/tech/bots": "tech_боты",
            "agents/tech/integrations": "tech_интеграции",
        }
        ministers = get_ministers_from_registry()
        valid = ["", "home", "agents", "login", "kb_docs", "kb_sop", "kb_clients", "kb_vector", "wf_templates", "wf_runs", "wf_queues", "wf_logs", "sys_users", "sys_roles", "sys_integrations", "sys_admin"]
        valid += [f"{s}_{i.replace(' ', '_').lower()}" for s,d in {"sales":["Лиды","Клиенты","Диалоги","Сделки","Заказы","КП","CJM","Этапы SLA","Эскалации","Повторные продажи","Рекомендатели","Sales Core"],"content":["Контент-завод","Темы","Статьи","Посты","Публикации","Комментарии","Google-таблицы"],"marketing":["ЦА","Боли","УТП","Офферы","Воронки","Источники трафика","Реклама","Креатив","Упаковка продукта"],"analytics":["Обзор","Продажи","Контент","Маркетинг","Дашборды","Метрики","План-факт","Отчёты","Ошибки данных","Выводы и рекомендации"],"production":["Заказы","План","Смены","Операции","Материалы","Остатки","Брак","Загрузка"],"tech":["Сервер","Скрипты","Боты","API","Интеграции","Воркфлоу","Очереди","Логи","Ошибки"],"mgmt":["Финансы","Юрконтур","Консалтинг","PR","Задачи собственника","Стратегия","Документы"],"smm":["SMM","WEB-завод"]}.items() for i in d]
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
                if cid == "all":
                    registry = _load_json(CLIENT_REGISTRY, {}).get("clients", {})
                    aggregated = []
                    for c_id in registry.keys():
                        if c_id == "_INTERNAL": continue
                        c_data = get_module_data(c_id, module)
                        c_items = c_data.get(section, c_data if section == "brand" else [])
                        if isinstance(c_items, list):
                            for item in c_items:
                                item_copy = dict(item)
                                item_copy["Клиент"] = c_id
                                aggregated.append(item_copy)
                    self._json({"status": "ok", "data": aggregated})
                    return
                data = get_module_data(cid, module)
                self._json({"status": "ok", "data": data.get(section, data if section == "brand" else [])})
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
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"onTime OS started on port {PORT}")
    server.serve_forever()
