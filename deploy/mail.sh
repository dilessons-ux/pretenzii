#!/bin/bash
# Подключение почты для писем клиентам. Пароль вводится здесь, в чат его отправлять не нужно.
# Запуск: bash <(curl -fsSL https://raw.githubusercontent.com/dilessons-ux/pretenzii/main/deploy/mail.sh)
set -e
ENV=/opt/pretenzii/.env
PY=/opt/pretenzii/venv/bin/python
read -rp "Адрес общей почты (например info@mebel-arigato.ru): " ADDR
ADDR=$(echo "$ADDR" | tr -d '[:space:]' | tr 'A-Z' 'a-z')
DOMAIN=${ADDR#*@}
MX=$(dig +short MX "$DOMAIN" | tr 'A-Z' 'a-z')
case "$MX" in
  *mail.ru*|*vk*)   HOST=smtp.mail.ru;    NOTE="VK WorkSpace / Mail.ru: нужен «пароль для внешнего приложения» (Настройки → Безопасность)";;
  *yandex*)         HOST=smtp.yandex.ru;  NOTE="Яндекс 360: нужен «пароль приложения» (id.yandex.ru → Безопасность)";;
  *beget*)          HOST=smtp.beget.com;  NOTE="Почта Beget: обычный пароль ящика";;
  *) read -rp "Не удалось определить сервис (MX: $MX). SMTP-сервер: " HOST; NOTE="";;
esac
echo "Почтовый сервер: $HOST:465"
[ -n "$NOTE" ] && echo "$NOTE"
read -rsp "Пароль: " PASS; echo
export ADDR HOST PASS
$PY - <<'PYEOF'
import os, smtplib, ssl, sys
from email.message import EmailMessage
a, h, p = os.environ["ADDR"], os.environ["HOST"], os.environ["PASS"]
try:
    with smtplib.SMTP_SSL(h, 465, context=ssl.create_default_context(), timeout=30) as s:
        s.login(a, p)
        m = EmailMessage(); m["From"] = f"Мебель Аригато <{a}>"; m["To"] = a
        m["Subject"] = "Проверка: приложение «Претензии» подключено к почте"
        m.set_content("Это тестовое письмо. Письма клиентам по претензиям будут уходить с этого адреса.")
        s.send_message(m)
    print("Вход выполнен, тестовое письмо отправлено на", a)
except Exception as e:
    print("ОШИБКА подключения:", e); sys.exit(1)
PYEOF
export ENV
$PY - <<'PYEOF'
import os
env = os.environ["ENV"]; a = os.environ["ADDR"]
vals = {"SMTP_HOST": os.environ["HOST"], "SMTP_PORT": "465", "SMTP_USER": a,
        "SMTP_PASSWORD": os.environ["PASS"], "SMTP_FROM": f"Мебель Аригато <{a}>"}
q = lambda v: '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
lines = [l for l in open(env, encoding="utf-8").read().splitlines() if l.split("=", 1)[0] not in vals]
lines += [f"{k}={q(v)}" for k, v in vals.items()]
open(env, "w", encoding="utf-8").write("\n".join(lines) + "\n")
PYEOF
systemctl restart pretenzii
echo "=== Почта $ADDR подключена ==="
