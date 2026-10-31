"""Чтение данных договора из amoCRM (только чтение).

По номеру договора находит контакт (поле «Номер Договора» в карточке клиента), его сделки
и берёт из сделки «Бригада» и «Дату установки». Настройки: AMOCRM_DOMAIN, AMOCRM_TOKEN в .env.
"""
import os
import re
from datetime import datetime, timedelta, timezone

import httpx

MSK = timezone(timedelta(hours=3))
LOST_STATUS_ID = 143  # «Закрыто и не реализовано» в amoCRM — такие сделки не показываем

F_CONTRACT_NO = "Номер Договора"
F_CONTRACT_DATE = "Дата заключения Договора"
F_CREW = "Бригада"
F_INSTALL_DATE = "Дата установки"
F_ADDRESS = ("Адрес объекта", "Адрес Клиента")


class AmoError(Exception):
    pass


def _cfg():
    domain = (os.getenv("AMOCRM_DOMAIN") or "").strip()
    token = (os.getenv("AMOCRM_TOKEN") or "").strip()
    if not domain or not token:
        raise AmoError("amoCRM не подключена (нет AMOCRM_DOMAIN / AMOCRM_TOKEN)")
    return os.getenv("AMOCRM_BASE") or f"https://{domain}", token


def _get(path, **params):
    base, token = _cfg()
    try:
        r = httpx.get(f"{base}/api/v4/{path}", params=params, timeout=15,
                      headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    except httpx.HTTPError as e:
        raise AmoError(f"amoCRM недоступна: {e.__class__.__name__}")
    if r.status_code == 204 or not r.content:
        return {}
    if r.status_code in (401, 403):
        raise AmoError("amoCRM отклонила токен (401/403) — проверьте токен и права")
    if r.status_code >= 400:
        raise AmoError(f"amoCRM вернула ошибку {r.status_code}")
    return r.json()


def norm_no(s) -> str:
    """'№ 907', '907', 'Договор №907' -> '907'"""
    return re.sub(r"(№|#|договор|\bno\b|\s+)", "", str(s or "").lower())


def _field(entity, name=None, code=None):
    for f in entity.get("custom_fields_values") or []:
        if code and f.get("field_code") == code:
            return [v.get("value") for v in f.get("values", [])]
        if name and (f.get("field_name") or "").strip().lower() == name.strip().lower():
            return [v.get("value") for v in f.get("values", [])]
    return None


def _first(vals):
    for v in vals or []:
        if v not in (None, ""):
            return v
    return None


def _date(v):
    if v in (None, ""):
        return None
    try:
        if str(v).isdigit():
            return datetime.fromtimestamp(int(v), MSK).date().isoformat()
        return datetime.fromisoformat(str(v)).date().isoformat()
    except Exception:
        return None


def match_crew(crew_names, staff):
    """crew_names: ['Юра/Иван']; staff: [(id, 'Петров Юра'), ...] -> (ids, не найденные имена)"""
    ids, missing = [], []
    for crew in crew_names:
        for token in re.split(r"[/,+&]|\sи\s", crew):
            t = token.strip().lower()
            if not t:
                continue
            hits = [uid for uid, name in staff if t in re.split(r"\s+", name.lower())]
            if len(hits) == 1:
                if hits[0] not in ids:
                    ids.append(hits[0])
            else:
                missing.append(token.strip())
    return ids, missing


def _search_contacts(number):
    out, page = [], 1
    while page <= 4:
        data = _get("contacts", query=number, limit=250, page=page, **{"with": "leads"})
        items = (data.get("_embedded") or {}).get("contacts", [])
        out += items
        if len(items) < 250:
            break
        page += 1
    return out


def lookup(number, staff):
    number = (number or "").strip()
    if not number or len(number) > 40:
        raise AmoError("Введите номер договора")
    want = norm_no(number)
    contacts = [c for c in _search_contacts(number)
                if any(norm_no(v) == want for v in (_field(c, F_CONTRACT_NO) or []))]
    options = []
    for c in contacts:
        base = {
            "contact_id": c["id"],
            "client_name": c.get("name") or "",
            "client_phone": _first(_field(c, code="PHONE")),
            "client_email": _first(_field(c, code="EMAIL")),
            "contract_date": _date(_first(_field(c, F_CONTRACT_DATE))),
        }
        lead_ids = [ld["id"] for ld in (c.get("_embedded") or {}).get("leads", [])][:30]
        leads = []
        if lead_ids:
            data = _get("leads", limit=50, **{"filter[id][]": lead_ids})
            leads = [ld for ld in (data.get("_embedded") or {}).get("leads", [])
                     if ld.get("status_id") != LOST_STATUS_ID]
        if not leads:
            options.append({**base, "deal_id": None, "deal_name": None, "address": None, "price": None,
                            "install_date": None, "crew": [], "brigade_ids": [], "crew_missing": []})
        for ld in leads:
            crew = [str(v) for v in (_field(ld, F_CREW) or []) if v]
            ids, missing = match_crew(crew, staff)
            address = next((a for a in (_first(_field(ld, n)) for n in F_ADDRESS) if a), None)
            options.append({**base, "deal_id": ld["id"], "deal_name": ld.get("name"), "address": address,
                            "price": ld.get("price") or None,
                            "install_date": _date(_first(_field(ld, F_INSTALL_DATE))),
                            "crew": crew, "brigade_ids": ids, "crew_missing": missing})
    return {"found": bool(options), "options": options}
