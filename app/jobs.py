"""Расписание (напоминания, эскалации, опросы) и Telegram-бот (long polling)."""
import logging
import os
import threading
import time
from datetime import timedelta

from . import logic as L
from . import services as S
from .db import audit, job_once, now, tx
from .notify import MAX_TOKEN, TG_TOKEN, max_api, notify_user, send_email, send_max, send_telegram, tg_api

log = logging.getLogger("jobs")

REMIND_HOUR = int(os.getenv("REMIND_HOUR", "9"))
IGNORE_CHECK_HOUR = int(os.getenv("IGNORE_CHECK_HOUR", "18"))
ACTIVE = ("review", "decided", "waiting_consent", "escalated", "in_work")

STATE_BTNS = lambda cid: [[("✅ Сделано", f"ci:{cid}:done"), ("⚠️ Проблема", f"ci:{cid}:problem"),
                           ("🆘 Нужна помощь", f"ci:{cid}:help")]]


def _claim_line(claim, today):
    left = L.days_left(claim, today)
    left_txt = f"осталось {left} дн." if left >= 0 else f"ПРОСРОЧКА {-left} дн."
    return (f"<b>{claim['number']}</b> · {claim['client_name']} · {L.STATUSES[claim['status']]}\n"
            f"Срок {S.fmt(claim['legal_due'])} ({left_txt}). План: {S.fmt(claim['plan_due'])}")


def daily_reminders(c, today):
    claims = c.execute(f"SELECT * FROM claims WHERE status IN ({','.join('?' * len(ACTIVE))})", ACTIVE).fetchall()
    digest: dict[int, list] = {}
    for claim in claims:
        for p in S.participants(c, claim["id"]):
            if p["role"] == "director":
                continue
            if p["telegram_chat_id"]:
                send_telegram(p["telegram_chat_id"],
                              f"Ежедневная отметка по претензии\n{_claim_line(claim, today)}\n"
                              f"Что сделано сегодня и какое решение принято?", STATE_BTNS(claim["id"]))
            elif p["email"]:
                digest.setdefault(p["id"], [p, []])[1].append(claim)
            audit(c, claim["id"], "система", "напоминание отправлено", None, None, p["name"])
    for _, (user, items) in digest.items():
        body = "Отметьте, что сделано по претензиям (кнопка «Отметка» в карточке):\n\n" + "\n\n".join(
            f"{x['number']} · {x['client_name']} · срок {S.fmt(x['legal_due'])}\n{S.claim_url(x)}" for x in items)
        send_email(user["email"], f"Претензии: ежедневная отметка ({len(items)})", body)


def ignore_check(c, today):
    """18:00 — кто не отметился: фиксируем игнор, повторяем, эскалируем."""
    claims = c.execute(f"SELECT * FROM claims WHERE status IN ({','.join('?' * len(ACTIVE))})", ACTIVE).fetchall()
    d = today.isoformat()
    y = (today - timedelta(days=1)).isoformat()
    for claim in claims:
        for p in S.participants(c, claim["id"]):
            if p["role"] == "director":
                continue
            done = c.execute("SELECT 1 FROM checkins WHERE claim_id=? AND user_id=? AND day=?",
                             (claim["id"], p["id"], d)).fetchone()
            if done:
                continue
            audit(c, claim["id"], "система", "игнор напоминания", None, None, p["name"], f"нет отметки за {S.fmt(d)}")
            notify_user(p, f"Нет отметки по {claim['number']}", "Отметьтесь сегодня.", STATE_BTNS(claim["id"]))
            ignored_yesterday = c.execute(
                "SELECT 1 FROM audit_log WHERE claim_id=? AND action='игнор напоминания' AND new_value=?"
                " AND substr(ts,1,10)=?", (claim["id"], p["name"], y)).fetchone()
            if ignored_yesterday:
                for owner in S.participants(c, claim["id"]):
                    if owner["p_role"] == "np" and owner["id"] != p["id"]:
                        notify_user(owner, f"Эскалация {claim['number']}: {p['name']} 2 дня без отметки",
                                    S.claim_url(claim))
        # 3 дня без движения по претензии — директору
        moved = c.execute("SELECT 1 FROM checkins WHERE claim_id=? AND day >= ?",
                          (claim["id"], (today - timedelta(days=2)).isoformat())).fetchone()
        age = (today - L.parse_date(claim["accepted_at"])).days
        if not moved and age >= 3 and job_once(c, "stall", f"{claim['id']}:{d}"):
            audit(c, claim["id"], "система", "эскалация директору", None, None, "3 дня без движения")
            for dr in S.users_by_role(c, "director"):
                notify_user(dr, f"Эскалация {claim['number']}: 3 дня без движения", _plain(_claim_line(claim, today))
                            + f"\n{S.claim_url(claim)}")


def _plain(s):
    return s.replace("<b>", "").replace("</b>", "")


def deadline_watch(c, today):
    claims = c.execute(f"SELECT * FROM claims WHERE status IN ({','.join('?' * len(ACTIVE))}, 'done')",
                       ACTIVE).fetchall()
    for claim in claims:
        used = L.share_used(claim, today)
        ppl = S.participants(c, claim["id"])
        np_rop = [p for p in ppl if p["p_role"] in ("np", "rop")]
        owner = [p for p in ppl if p["p_role"] == "np"]
        directors = S.users_by_role(c, "director")
        for level, who in ((0.5, owner), (0.75, np_rop), (0.9, directors + np_rop)):
            if used >= level and job_once(c, "deadline", f"{claim['id']}:{claim['legal_due']}:{level}"):
                audit(c, claim["id"], "система", f"предупреждение: прошло {int(level * 100)}% срока")
                for u in who:
                    notify_user(u, f"{claim['number']}: прошло {int(level * 100)}% срока",
                                _plain(_claim_line(claim, today)) + f"\n{S.claim_url(claim)}")
        if claim["status"] == "review" and today > L.parse_date(claim["review_due"]) \
                and job_once(c, "review_late", f"{claim['id']}:{today}"):
            audit(c, claim["id"], "система", "просрочено рассмотрение")
            for u in owner + directors:
                notify_user(u, f"{claim['number']}: не рассмотрена в срок", S.claim_url(claim))
        days, amount = L.penalty(claim, today)
        if days > 0 and job_once(c, "overdue", f"{claim['id']}:{today}"):
            audit(c, claim["id"], "система", "просрочка", None, None, f"{days} дн., неустойка {amount:.0f} ₽")
            for u in directors + owner:
                notify_user(u, f"ПРОСРОЧКА {claim['number']}: {days} дн.",
                            f"Неустойка на сегодня: {amount:,.0f} ₽\n{S.claim_url(claim)}".replace(",", " "))


def extension_followup(c, today):
    rows = c.execute(
        "SELECT e.*, c.number FROM extensions e JOIN claims c ON c.id=e.claim_id"
        " WHERE e.answer IS NULL AND e.sent_at IS NOT NULL").fetchall()
    for e in rows:
        if (today - L.parse_date(e["sent_at"])).days >= 3 and job_once(c, "ext_call", str(e["id"])):
            claim = c.execute("SELECT * FROM claims WHERE id=?", (e["claim_id"],)).fetchone()
            audit(c, claim["id"], "система", "клиент 3 дня не ответил на перенос срока")
            for p in S.participants(c, claim["id"]):
                if p["p_role"] in ("np", "rop"):
                    notify_user(p, f"{claim['number']}: позвоните клиенту",
                                f"Нет ответа на новый срок 3 дня. {claim['client_phone']}\n{S.claim_url(claim)}")


def surveys(c, today):
    rows = c.execute("SELECT * FROM claims WHERE client_confirmed_at IS NOT NULL AND csat_sent_at IS NULL").fetchall()
    for claim in rows:
        if now().replace(tzinfo=None) - _dt(claim["client_confirmed_at"]) >= timedelta(hours=48):
            S.client_letter(c, claim, "survey")
            c.execute("UPDATE claims SET csat_sent_at=? WHERE id=?", (now().strftime("%Y-%m-%d %H:%M:%S"), claim["id"]))


def _dt(s):
    from datetime import datetime
    return datetime.fromisoformat(s)


def run_due_jobs():
    t = now()
    today = t.date()
    with tx() as c:
        if t.hour >= REMIND_HOUR and job_once(c, "daily", today.isoformat()):
            daily_reminders(c, today)
        if t.hour >= REMIND_HOUR and job_once(c, "deadlines", today.isoformat()):
            deadline_watch(c, today)
            extension_followup(c, today)
        if t.hour >= IGNORE_CHECK_HOUR and job_once(c, "ignore", today.isoformat()):
            ignore_check(c, today)
        surveys(c, today)


def scheduler_loop():
    while True:
        try:
            run_due_jobs()
        except Exception:
            log.exception("scheduler error")
        time.sleep(60)


# ---------------- Telegram-бот ----------------

def handle_update(upd):
    with tx() as c:
        if "callback_query" in upd:
            q = upd["callback_query"]
            chat = str(q["message"]["chat"]["id"])
            user = c.execute("SELECT * FROM users WHERE telegram_chat_id=? AND active=1", (chat,)).fetchone()
            tg_api("answerCallbackQuery", {"callback_query_id": q["id"]}, timeout=10)
            if not user:
                return
            _, cid, state = q["data"].split(":")
            c.execute("INSERT OR REPLACE INTO tg_pending (chat_id, claim_id, state) VALUES (?,?,?)",
                      (chat, int(cid), state))
            prompt = {"done": "Напишите, что сделано и какое решение принято (или /skip):",
                      "problem": "Опишите проблему:", "help": "Какая помощь нужна?"}[state]
            send_telegram(chat, prompt)
            return
        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat = str(msg.get("chat", {}).get("id", ""))
        if not chat or not text:
            return
        if text.startswith("/start"):
            code = text.split(maxsplit=1)[1] if " " in text else ""
            user = c.execute("SELECT * FROM users WHERE tg_link_code=? AND active=1", (code,)).fetchone() if code else None
            if user:
                c.execute("UPDATE users SET telegram_chat_id=?, tg_link_code=NULL WHERE id=?", (chat, user["id"]))
                send_telegram(chat, f"Готово, {user['name']}. Напоминания по претензиям будут приходить сюда.")
            else:
                send_telegram(chat, "Откройте ссылку привязки из своего профиля в приложении претензий.")
            return
        pend = c.execute("SELECT * FROM tg_pending WHERE chat_id=?", (chat,)).fetchone()
        user = c.execute("SELECT * FROM users WHERE telegram_chat_id=? AND active=1", (chat,)).fetchone()
        if not pend or not user:
            send_telegram(chat, "Используйте кнопки под напоминанием.")
            return
        if pend["state"] in ("problem", "help") and text == "/skip":
            send_telegram(chat, "Здесь нужен комментарий — опишите ситуацию.")
            return
        claim = c.execute("SELECT * FROM claims WHERE id=?", (pend["claim_id"],)).fetchone()
        S.record_checkin(c, user, claim, pend["state"], None if text == "/skip" else text)
        c.execute("DELETE FROM tg_pending WHERE chat_id=?", (chat,))
        send_telegram(chat, f"Записал в журнал претензии {claim['number']}. Спасибо.")


def bot_loop():
    offset = 0
    while True:
        try:
            res = tg_api("getUpdates", {"offset": offset, "timeout": 30}, timeout=40) or {}
            for upd in res.get("result", []):
                offset = upd["update_id"] + 1
                try:
                    handle_update(upd)
                except Exception:
                    log.exception("tg update error")
        except Exception:
            log.exception("tg polling error")
            time.sleep(5)


# ---------------- MAX-бот ----------------

def max_ensure_columns():
    with tx() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()}
        for col in ("max_user_id", "max_link_code"):
            if col not in cols:
                c.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")


def _max_link(c, uid, code):
    user = c.execute("SELECT * FROM users WHERE max_link_code=? AND active=1", (code,)).fetchone() if code else None
    if user:
        c.execute("UPDATE users SET max_user_id=?, max_link_code=NULL WHERE id=?", (uid, user["id"]))
        send_max(uid, "", f"Готово, {user['name']}. Напоминания по претензиям будут приходить сюда.")
    else:
        send_max(uid, "", "Откройте ссылку привязки из своего профиля в приложении претензий.")


def handle_max_update(upd):
    kind = upd.get("update_type")
    with tx() as c:
        if kind == "message_callback":
            cb = upd.get("callback") or {}
            uid = str((cb.get("user") or {}).get("user_id", ""))
            try:
                max_api("POST", "/answers", params={"callback_id": cb.get("callback_id")},
                        payload={"notification": "Принято"}, timeout=10)
            except Exception:
                pass
            user = c.execute("SELECT * FROM users WHERE max_user_id=? AND active=1", (uid,)).fetchone()
            if not user or not cb.get("payload"):
                return
            try:
                _, cid, state = cb["payload"].split(":")
            except ValueError:
                return
            c.execute("INSERT OR REPLACE INTO tg_pending (chat_id, claim_id, state) VALUES (?,?,?)",
                      ("max:" + uid, int(cid), state))
            prompt = {"done": "Напишите, что сделано и какое решение принято (или /skip):",
                      "problem": "Опишите проблему:", "help": "Какая помощь нужна?"}.get(state, "Напишите комментарий:")
            send_max(uid, "", prompt)
            return
        if kind == "bot_started":
            uid = str((upd.get("user") or {}).get("user_id", ""))
            if uid:
                _max_link(c, uid, upd.get("payload") or "")
            return
        if kind != "message_created":
            return
        msg = upd.get("message") or {}
        uid = str((msg.get("sender") or {}).get("user_id", ""))
        text = ((msg.get("body") or {}).get("text") or "").strip()
        if not uid or not text:
            return
        if text.startswith("/start"):
            code = text.split(maxsplit=1)[1] if " " in text else ""
            _max_link(c, uid, code)
            return
        pend = c.execute("SELECT * FROM tg_pending WHERE chat_id=?", ("max:" + uid,)).fetchone()
        user = c.execute("SELECT * FROM users WHERE max_user_id=? AND active=1", (uid,)).fetchone()
        if not pend or not user:
            send_max(uid, "", "Используйте кнопки под напоминанием.")
            return
        if pend["state"] in ("problem", "help") and text == "/skip":
            send_max(uid, "", "Здесь нужен комментарий — опишите ситуацию.")
            return
        claim = c.execute("SELECT * FROM claims WHERE id=?", (pend["claim_id"],)).fetchone()
        S.record_checkin(c, user, claim, pend["state"], None if text == "/skip" else text)
        c.execute("DELETE FROM tg_pending WHERE chat_id=?", ("max:" + uid,))
        send_max(uid, "", f"Записал в журнал претензии {claim['number']}. Спасибо.")


def max_loop():
    marker = None
    while True:
        try:
            params = {"timeout": 30, "limit": 100}
            if marker is not None:
                params["marker"] = marker
            st, data = max_api("GET", "/updates", params=params, timeout=45)
            if st != 200:
                log.error("max polling status %s: %s", st, data)
                time.sleep(10)
                continue
            marker = data.get("marker", marker)
            for upd in data.get("updates", []):
                try:
                    handle_max_update(upd)
                except Exception:
                    log.exception("max update error")
        except Exception:
            log.exception("max polling error")
            time.sleep(5)


def start_background():
    try:
        max_ensure_columns()
    except Exception:
        log.exception("max columns")
    if os.getenv("RUN_JOBS", "1") == "1":
        threading.Thread(target=scheduler_loop, daemon=True, name="scheduler").start()
        if MAX_TOKEN:
            threading.Thread(target=max_loop, daemon=True, name="max-bot").start()
        if TG_TOKEN:
            threading.Thread(target=bot_loop, daemon=True, name="tgbot").start()
