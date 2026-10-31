#!/usr/bin/env python3
"""Диагностика amoCRM (только чтение): как лежат поля «Номер Договора», «Бригада», «Дата установки».

Запуск на сервере:  /opt/pretenzii/venv/bin/python /opt/pretenzii/deploy/amocrm_probe.py 909
Токен берётся из /opt/pretenzii/.env (AMOCRM_DOMAIN, AMOCRM_TOKEN) и нигде не печатается.
Телефоны и email клиентов не выводятся.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

ENV = os.getenv("PROBE_ENV", "/opt/pretenzii/.env")


def read_env(path):
    out = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


env = read_env(ENV)
BASE = os.getenv("AMOCRM_BASE") or "https://" + env["AMOCRM_DOMAIN"]
TOKEN = env["AMOCRM_TOKEN"]


def api(path, **params):
    url = f"{BASE}/api/v4/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8")
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} на {path}: {e.read().decode('utf-8', 'replace')[:300]}")
        return None


def cf_list(entity):
    data = api(f"{entity}/custom_fields", limit=250)
    return (data or {}).get("_embedded", {}).get("custom_fields", [])


def fval(entity, name):
    """значения кастомного поля по названию"""
    for f in entity.get("custom_fields_values") or []:
        if (f.get("field_name") or "").strip().lower() == name.strip().lower():
            return [v.get("value") for v in f.get("values", [])]
    return None


def main():
    number = sys.argv[1] if len(sys.argv) > 1 else "909"
    msk = timezone(timedelta(hours=3))

    print("=== Поля контакта ===")
    for f in cf_list("contacts"):
        print(f"  {f['id']:>8}  {f['type']:<12} {f['name']}")
    print("\n=== Поля сделки ===")
    for f in cf_list("leads"):
        extra = ""
        if f.get("enums"):
            extra = "  значения: " + ", ".join(e["value"] for e in f["enums"])
        print(f"  {f['id']:>8}  {f['type']:<12} {f['name']}{extra}")

    print(f"\n=== Поиск договора {number} ===")
    data = api("contacts", query=number, **{"with": "leads"}, limit=50)
    contacts = (data or {}).get("_embedded", {}).get("contacts", [])
    print(f"  контактов по запросу: {len(contacts)}")
    exact = []
    for c in contacts:
        vals = fval(c, "Номер Договора")
        mark = ""
        if vals and any(str(v).strip() == number for v in vals):
            exact.append(c)
            mark = "  <-- точное совпадение"
        print(f"  контакт {c['id']}: {c.get('name')!r}, Номер Договора={vals}, "
              f"Дата заключения={fval(c, 'Дата заключения')}{mark}")
    for c in exact:
        leads = c.get("_embedded", {}).get("leads", [])
        print(f"\n  у контакта {c['id']} сделок: {len(leads)}")
        for ld in leads:
            d = api(f"leads/{ld['id']}")
            if not d:
                continue
            inst = fval(d, "Дата установки")
            inst_h = []
            for v in inst or []:
                try:
                    inst_h.append(datetime.fromtimestamp(int(v), msk).strftime("%Y-%m-%d %H:%M") if str(v).isdigit() else v)
                except Exception:
                    inst_h.append(v)
            print(f"    сделка {d['id']}: {d.get('name')!r}, Бригада={fval(d, 'Бригада')}, "
                  f"Дата установки(сырое)={inst}, (дата)={inst_h}")
    if not exact:
        print("  Точного совпадения по полю «Номер Договора» нет.")


if __name__ == "__main__":
    main()
