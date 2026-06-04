import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

# --- Constants from app.py ---
KB_ROOT = Path("/mnt/ontime/Книга знаний Агентов")
CLIENTS_ROOT = Path("/mnt/ontime/Клиенты")
QUEUE_ROOT = Path("/data/queue")
WF_LOG_CANDIDATES = [Path("/root/agents/v3/logs"), KB_ROOT / "_runtime/logs"]
AGENT_REGISTRY = KB_ROOT / "_SYSTEM/10_REGISTRY/agent_registry.json"
CLIENT_REGISTRY = KB_ROOT / "_SYSTEM/10_REGISTRY/client_registry.json"
CHAT_STORE = KB_ROOT / "_runtime/shared/dashboard_chats_cli.json"
INTEGRATIONS_REGISTRY = KB_ROOT / "_runtime/integrations/service_registry.json"
SALES_REGISTRY = KB_ROOT / "_runtime/sales/sales_registry.json"

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

SALES_EMPTY = {
    "leads": [], "clients": [], "deals": [], "proposals": [],
    "scripts": [], "objections": [], "kpi": [],
}
MARKETING_EMPTY = {
    "lead_magnets": [], "warmup_chains": [], "attribution": [],
    "audiences": [], "pains": [], "usps": [], "offers": [],
    "funnels": [], "traffic_sources": [], "campaigns": [], "creatives": [], "brand": {}
}
CONTENT_EMPTY = {
    "topics": [], "articles": [], "posts": [],
    "publications": [], "comments": [], "sheets": []
}
TECH_EMPTY = {"bots": []}
MGMT_EMPTY = {
    "finance": [], "legal": [], "consulting": [], "pr": [],
    "owner_tasks": [], "strategy": [], "documents": [],
}
LOYALTY_EMPTY = {"repeat_sales": [], "referrals": [], "promos": [], "reviews": []}
PRODUCTION_EMPTY = {
    "orders": [], "plan": [], "shifts": [], "operations": [],
    "materials": [], "stock": [], "defects": [], "load": [],
}
SMM_EMPTY = {"autoposting": {"enabled": False, "slots": [], "platforms": [], "errors": []}, "socials": []}
WEB_EMPTY = {
    "library": [], "keywords": [], "competitors": [],
    "brand_rules": {"id": "brand_rules", "voice": "", "colors": "", "guidelines": ""},
    "default_owners": {"id": "default_owners", "content_owner": "OTDEL_CREATIVE", "tech_owner": "OTDEL_TECH", "design_owner": "OTDEL_CREATIVE"}
}
MODULE_EMPTY = {
    "sales": SALES_EMPTY, "marketing": MARKETING_EMPTY, "content": CONTENT_EMPTY, "tech": TECH_EMPTY,
    "mgmt": MGMT_EMPTY, "loyalty": LOYALTY_EMPTY, "production": PRODUCTION_EMPTY,
    "smm": SMM_EMPTY, "web": WEB_EMPTY,
}

# --- Core Data Functions ---

def _load_json(path: Path, default=None):
    try:
        if not path.exists(): return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception: return default

def _save_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

def now_ts():
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

def _client_folder(cid):
    reg = _load_json(CLIENT_REGISTRY, {}).get("clients", {})
    folder = reg.get(cid, {}).get("folder")
    if folder: return Path(folder)
    for sp in [CLIENTS_ROOT, CLIENTS_ROOT / "_INTERNAL"]:
        for d in (sp.iterdir() if sp.exists() else []):
            if d.is_dir() and d.name.upper() == cid.upper(): return d
    return CLIENTS_ROOT / cid

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
        test_file.touch()
        test_file.unlink()
        return True
    except: return False

def get_client_runtime_folder(cid):
    if is_clients_writable():
        return get_client_folder(cid) or (CLIENTS_ROOT / cid)
    else:
        fallback_dir = Path("/data/runtime/Клиенты") / cid
        try: fallback_dir.mkdir(parents=True, exist_ok=True)
        except: pass
        return fallback_dir

def get_module_data(cid, module):
    rf = get_client_runtime_folder(cid) / f"{module}.json"
    if rf.exists():
        try: return json.loads(rf.read_text(encoding="utf-8"))
        except: pass
    c_folder = get_client_folder(cid)
    if c_folder:
        of = c_folder / f"{module}.json"
        if of.exists():
            try: return json.loads(of.read_text(encoding="utf-8"))
            except: pass
    return json.loads(json.dumps(MODULE_EMPTY.get(module, {}), ensure_ascii=False))

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
    if not isinstance(data, dict): data = default_integrations_registry()
    data.setdefault("version", "1.0")
    data.setdefault("updated_at", now_ts())
    return data

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

def get_sales_registry():
    return _load_json(SALES_REGISTRY, {"leads": [], "clients": [], "deals": [], "proposals": []})

def _get_loyalty_aggregates():
    deal_counts = {s: 0 for s in FUNNEL_STAGES}
    deal_amounts = {s: 0.0 for s in FUNNEL_STAGES}
    ref_counts = {s: 0 for s in FUNNEL_STAGES}
    reg = _load_json(CLIENT_REGISTRY, {})
    for cid, info in reg.get("clients", {}).items():
        folder = info.get("folder")
        if not folder:
            for sp in [CLIENTS_ROOT, CLIENTS_ROOT / "_INTERNAL"]:
                candidate = sp / cid
                if candidate.is_dir(): folder = str(candidate); break
        if not folder: continue
        lpath = Path(folder) / "loyalty.json"
        if not lpath.exists(): continue
        try:
            l = json.loads(lpath.read_text(encoding="utf-8"))
            for d in l.get("repeat_sales", []):
                st = d.get("Этап")
                if st in deal_counts:
                    deal_counts[st] += 1
                    try: deal_amounts[st] += float(str(d.get("Сумма", 0)).replace(" ", "").replace(",", "."))
                    except: pass
            for r in l.get("referrals", []):
                st = r.get("Этап")
                if st in ref_counts: ref_counts[st] += 1
        except: pass
    return deal_counts, deal_amounts, ref_counts

def get_sales_core_rows():
    reg = get_sales_registry()
    leads = reg.get("leads", []) or []
    clients_reg = reg.get("clients", []) or []
    deals = reg.get("deals", []) or []
    rows = []
    loyalty_deals, loyalty_amounts, loyalty_refs = _get_loyalty_aggregates()
    for d in deals:
        rows.append({"type": "deal", "data": d})
    for l in leads:
        rows.append({"type": "lead", "data": l})
    for c in clients_reg:
        rows.append({"type": "client", "data": c})
    return rows

MODULE_COLUMNS = {
"marketing_lead_magnets": ["Название", "Тип", "Оффер", "Поток", "Канал", "Конверсия", "Статус", "URL"],
    "marketing_warmup_chains": ["Название", "Поток", "Канал", "Триггер", "Шагов", "Конверсия", "Статус"],
    "marketing_attribution": ["Контент ID", "Канал", "UTM Campaign", "Лиды", "Сделки", "Выручка", "CPL", "ROAS", "Период"],
"sales_scripts": ["Название", "Аватар", "Этап воронки", "Канал", "Шаги", "Конверсия", "Статус"],
    "sales_objections": ["Возражение", "Аватар", "Частота", "Ответ", "Доказательство", "Этап воронки", "Источник"],
    "sales_kpi": ["Метрика", "План", "Факт", "Отклонение %", "Период", "Ответственный", "Статус"],
    "loyalty_promos": ["Название", "Тип", "Продукты", "Дата начала", "Дата конца", "Купон", "Этап воронки", "План сделок", "Статус"],
    "loyalty_reviews": ["Клиент", "Аватар", "Продукт", "Рейтинг", "Текст", "Источник", "Дата", "Опубликован", "Использован в контенте", "Статус"],
    "marketing_воронки": ["funnel_id", "Название воронки", "Этап", "Порядок этапа", "Вход", "Выход", "CR%", "Итоговая конверсия", "Связанный оффер", "Источник трафика", "Рекламная кампания", "Статус", "Ответственный", "Комментарий"],
    "marketing_ца": ["id", "Сегмент ЦА", "Слоган", "УТП", "Боли", "Содержание объявления", "CTA", "Маркетинговые архетипы", "Подходящие продукты", "Товары с высокой вероятностью заказа", "Ключевые товары (основной доход)", "Что добавить и чего пока нет", "Комплексное предложение (апсейл)", "Плановая сумма в месяц (₽)", "Приоритет (1–5)", "Примерная доля в выручке", "Оценка вероятности продаж (%)", "Комментарий"],
    "marketing_боли": ["Боль", "Сегмент ЦА", "Интенсивность", "Статус", "Приоритет"],
    "marketing_утп": ["Формулировка", "Сегмент", "Канал", "Статус", "Владелец"],
    "marketing_офферы": ["Заголовок", "Текст", "Канал", "Конверсия", "Статус"],
    "marketing_источники_трафика": ["Название", "Тип", "Клиенты", "Описание канала", "Бюджет", "Лиды", "CPL", "Статус"],
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
