#!/usr/bin/env python3
"""Патч: дата установки и бригада сохраняются в самой претензии.

Запуск на сервере:
  curl -fsSL https://raw.githubusercontent.com/dilessons-ux/pretenzii/main/deploy/patch_install.py -o /tmp/patch_install.py \
    && /opt/pretenzii/venv/bin/python /tmp/patch_install.py && systemctl restart pretenzii

Что делает:
  1. В таблицу претензий добавляет поля install_date и crew (при старте приложения, данные не теряются).
  2. Кнопка «Подтянуть из amoCRM» передаёт дату установки и бригаду в претензию.
  3. Они показываются в карточке претензии и в копии, которую получают участники.
Перед правкой делает копии .bak_install; если нужные куски кода не найдены — ничего не меняет.
"""
import py_compile
import shutil
import sys

APP = "/opt/pretenzii/app"
FILES = {k: f"{APP}/{v}" for k, v in {
    "db": "db.py", "services": "services.py", "main": "main.py",
    "new": "templates/new.html", "claim": "templates/claim.html"}.items()}
MARK = "install_date"  # признак уже применённого патча (в db.py)

# (файл, что искать, на что заменить)
EDITS = [
    ("db", "        c.executescript(SCHEMA)\n",
     "        c.executescript(SCHEMA)\n"
     "        _ccols = [r[1] for r in c.execute(\"PRAGMA table_info(claims)\")]\n"
     "        for _col in (\"install_date\", \"crew\"):\n"
     "            if _col not in _ccols:\n"
     "                c.execute(f\"ALTER TABLE claims ADD COLUMN {_col} TEXT\")\n"),
    ("services", "    number = next_number(c)\n    cur = c.execute(",
     "    number = next_number(c)\n"
     "    try:\n"
     "        _inst = L.parse_date(form.get(\"install_date\"))\n"
     "    except ValueError:\n"
     "        _inst = None\n"
     "    install = _inst.isoformat() if _inst else None\n"
     "    crew = (form.get(\"crew\") or \"\").strip()[:200] or None\n"
     "    cur = c.execute("),
    ("services",
     "           description, owner_id, review_due, legal_due, original_legal_due, client_token, created_at)\n"
     "           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)\"\"\",",
     "           description, owner_id, review_due, legal_due, original_legal_due, client_token, created_at,\n"
     "           install_date, crew)\n"
     "           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)\"\"\","),
    ("services", "         secrets.token_urlsafe(24), now_iso()))\n",
     "         secrets.token_urlsafe(24), now_iso(), install, crew))\n"),
    ("services", "def summary(claim) -> str:\n",
     "def _install_line(claim) -> str:\n"
     "    try:\n"
     "        d, crew = claim[\"install_date\"], claim[\"crew\"]\n"
     "    except (KeyError, IndexError):\n"
     "        return \"\"\n"
     "    if not d and not crew:\n"
     "        return \"\"\n"
     "    return \"Установка: \" + (fmt(d) if d else \"—\") + (f\", бригада {crew}\" if crew else \"\") + \"\\n\"\n"
     "\n\n"
     "def summary(claim) -> str:\n"),
    ("services", "        f\"Адрес: {claim['address'] or '—'}\\n\"\n",
     "        f\"Адрес: {claim['address'] or '—'}\\n\"\n        f\"{_install_line(claim)}\"\n"),
    ("main", "    data = {k: form.get(k) for k in required + (\"client_email\", \"address\", \"contract_price\", \"description\")}\n",
     "    data = {k: form.get(k) for k in required + (\"client_email\", \"address\", \"contract_price\", \"description\",\n"
     "                                                \"install_date\", \"crew\")}\n"),
    ("new", "<form method=\"post\" action=\"/claims/new\" enctype=\"multipart/form-data\" class=\"card\">\n",
     "<form method=\"post\" action=\"/claims/new\" enctype=\"multipart/form-data\" class=\"card\">\n"
     "  <input type=\"hidden\" name=\"install_date\"><input type=\"hidden\" name=\"crew\">\n"),
    ("new", "    put('contract_date', o.contract_date); put('address', o.address); put('contract_price', o.price);\n",
     "    put('contract_date', o.contract_date); put('address', o.address); put('contract_price', o.price);\n"
     "    f.elements['install_date'].value = o.install_date || '';\n"
     "    f.elements['crew'].value = (o.crew || []).join(', ');\n"),
    ("claim", "{% if c.contract_price %} · {{ c.contract_price|money }}{% endif %}</dd>\n",
     "{% if c.contract_price %} · {{ c.contract_price|money }}{% endif %}</dd>\n"
     "    {% if c.install_date or c.crew %}<dt>Установка</dt><dd>{% if c.install_date %}{{ fmt(c.install_date) }}{% else %}—{% endif %}"
     "{% if c.crew %} · бригада: {{ c.crew }}{% endif %}</dd>{% endif %}\n"),
]


def main():
    src = {k: open(p, encoding="utf-8").read() for k, p in FILES.items()}
    if MARK in src["db"]:
        print("Патч уже применён, ничего не меняю.")
        return 0
    for key, old, _ in EDITS:
        if src[key].count(old) != 1:
            print(f"ОШИБКА: не нашёл ожидаемый кусок кода в {FILES[key]}:\n  {old.strip()[:80]}\nНичего не изменено.")
            return 1
    for p in FILES.values():
        shutil.copy(p, p + ".bak_install")
    for key, old, new in EDITS:
        src[key] = src[key].replace(old, new)
    for key, p in FILES.items():
        open(p, "w", encoding="utf-8").write(src[key])
    try:
        for key in ("db", "services", "main"):
            py_compile.compile(FILES[key], doraise=True)
    except py_compile.PyCompileError as e:
        for p in FILES.values():
            shutil.copy(p + ".bak_install", p)
        print("ОШИБКА компиляции, вернул прежние файлы:", e)
        return 1
    print("Готово. Перезапусти службу: systemctl restart pretenzii")
    return 0


if __name__ == "__main__":
    sys.exit(main())
