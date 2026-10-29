"""Уведомления: email (SMTP) и Telegram. Без настроек — пишет в data/outbox.log (режим проверки)."""
import json
import logging
import mimetypes
import os
import smtplib
import ssl
from email.message import EmailMessage

import httpx

from .db import DB_PATH, now_iso

log = logging.getLogger("notify")

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER)
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
BASE_URL = os.getenv("BASE_URL", "https://pretenzii.mebel-arigato.ru").rstrip("/")
COMPANY = os.getenv("COMPANY_NAME", "Мебель Аригато")
MAX_ATTACH_MB = 15

OUTBOX = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "outbox.log")


def _outbox(kind, to, subject, body):
    os.makedirs(os.path.dirname(OUTBOX), exist_ok=True)
    with open(OUTBOX, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now_iso(), "kind": kind, "to": to, "subject": subject, "body": body},
                           ensure_ascii=False) + "\n")


def send_email(to: list[str] | str, subject: str, body: str, attachments: list[tuple[str, str]] = ()) -> bool:
    """attachments: [(путь, имя файла)]. Возвращает True при успешной отправке."""
    recipients = [to] if isinstance(to, str) else [t for t in to if t]
    if not recipients:
        return False
    if not SMTP_HOST:
        _outbox("email", recipients, subject, body)
        return True
    msg = EmailMessage()
    msg["From"] = SMTP_FROM
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(body)
    total = 0
    for path, name in attachments:
        if not os.path.exists(path):
            continue
        size = os.path.getsize(path)
        if total + size > MAX_ATTACH_MB * 1024 * 1024:
            continue
        total += size
        ctype, _ = mimetypes.guess_type(name)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        with open(path, "rb") as f:
            msg.add_attachment(f.read(), maintype=maintype, subtype=subtype, filename=name)
    try:
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context(), timeout=30) as s:
                s.login(SMTP_USER, SMTP_PASSWORD)
                s.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(SMTP_USER, SMTP_PASSWORD)
                s.send_message(msg)
        return True
    except Exception as e:  # письмо не должно ронять приложение
        log.error("email error %s: %s", recipients, e)
        _outbox("email-failed", recipients, subject, f"{e}\n\n{body}")
        return False


def tg_api(method: str, payload: dict, timeout=35):
    if not TG_TOKEN:
        return None
    r = httpx.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", json=payload, timeout=timeout)
    return r.json()


def send_telegram(chat_id: str | None, text: str, buttons: list[list[tuple[str, str]]] | None = None) -> bool:
    if not chat_id:
        return False
    if not TG_TOKEN:
        _outbox("telegram", chat_id, "", text)
        return True
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row]
                                                       for row in buttons]}
    try:
        res = tg_api("sendMessage", payload, timeout=15)
        return bool(res and res.get("ok"))
    except Exception as e:
        log.error("telegram error %s: %s", chat_id, e)
        return False


def send_tg_files(chat_id, attachments):
    """Файлы претензии (скан, фото) в Telegram документами, до 20 МБ каждый."""
    if not chat_id or not attachments:
        return
    for path, name in attachments:
        if not os.path.exists(path) or os.path.getsize(path) > 20 * 1024 * 1024:
            continue
        if not TG_TOKEN:
            _outbox("telegram-file", chat_id, name, path)
            continue
        try:
            with open(path, "rb") as f:
                httpx.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
                           data={"chat_id": chat_id}, files={"document": (name, f)}, timeout=60)
        except Exception as e:
            log.error("telegram file error %s: %s", chat_id, e)


def notify_user(user, subject: str, text: str, buttons=None, attachments=()) -> bool:
    """Сотруднику: Telegram (основной канал), если не привязан — email, если он указан."""
    if user["telegram_chat_id"]:
        ok = send_telegram(user["telegram_chat_id"], f"<b>{subject}</b>\n{text}", buttons)
        if ok:
            send_tg_files(user["telegram_chat_id"], attachments)
            return True
    if user["email"]:
        return send_email(user["email"], subject, text, attachments)
    _outbox("undelivered", user["name"], subject, "нет Telegram и email")
    return False
