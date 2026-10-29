"""Действия с претензией, общие для веб-интерфейса, Telegram-бота и расписания."""
import os
import secrets
from datetime import date, timedelta

from . import logic as L
from .db import audit, now, now_iso, set_field
from .notify import BASE_URL, COMPANY, notify_user, send_email

UPLOAD_DIR = os.getenv("UPLOAD_DIR", os.path.join(os.path.dirname(__file__), "..", "data", "uploads"))


def fmt(d) -> str:
    d = L.parse_date(d)
    return d.strftime("%d.%m.%Y") if d else "—"


def claim_url(claim) -> str:
    return f"{BASE_URL}/claims/{claim['id']}"


def client_url(claim) -> str:
    return f"{BASE_URL}/c/{claim['client_token']}"


def next_number(c) -> str:
    year = now().year
    row = c.execute("SELECT COUNT(*) FROM claims WHERE number LIKE ?", (f"Р-{year}-%",)).fetchone()
    return f"Р-{year}-{row[0] + 1:04d}"


def participants(c, claim_id):
    return c.execute(
        "SELECT u.*, p.role AS p_role FROM claim_participants p JOIN users u ON u.id = p.user_id"
        " WHERE p.claim_id = ? AND u.active = 1", (claim_id,)).fetchall()


def users_by_role(c, role):
    return c.execute("SELECT * FROM users WHERE role = ? AND active = 1", (role,)).fetchall()


def add_participant(c, claim_id, user_id, role, actor):
    cur = c.execute("INSERT OR IGNORE INTO claim_participants (claim_id, user_id, role) VALUES (?,?,?)",
                    (claim_id, user_id, role))
    if cur.rowcount:
        u = c.execute("SELECT name FROM users WHERE id=?", (user_id,)).fetchone()
        audit(c, claim_id, actor, "участник добавлен", "participant", None, f"{u['name']} ({L.ROLES.get(role, role)})")


def files(c, claim_id, kinds=None):
    rows = c.execute("SELECT * FROM claim_files WHERE claim_id=? ORDER BY id", (claim_id,)).fetchall()
    return [r for r in rows if not kinds or r["kind"] in kinds]


def summary(claim) -> str:
    return (
        f"Претензия {claim['number']}\n"
        f"Клиент: {claim['client_name']}, {claim['client_phone']}\n"
        f"Договор № {claim['contract_no']} от {fmt(claim['contract_date'])}\n"
        f"Адрес: {claim['address'] or '—'}\n"
        f"Требование: {L.DEMANDS[claim['demand_type']]}\n"
        f"Суть: {claim['description'] or '—'}\n"
        f"Рассмотреть до: {fmt(claim['review_due'])}\n"
        f"Юридический срок: {fmt(claim['legal_due'])}\n"
        f"Карточка: {claim_url(claim)}"
    )


# ---------- письма клиенту ----------

def client_letter(c, claim, kind: str, extra: dict | None = None):
    extra = extra or {}
    link = client_url(claim)
    sign = f"\n\nС уважением,\n{COMPANY}"
    if kind == "accepted":
        subj = f"Ваша претензия {claim['number']} принята"
        body = (f"Здравствуйте, {claim['client_name']}!\n\nМы зарегистрировали вашу претензию по договору "
                f"№ {claim['contract_no']} от {fmt(claim['contract_date'])}.\nНомер: {claim['number']}.\n"
                f"Решение сообщим до {fmt(claim['review_due'])}.\n\nСтатус в любой момент: {link}")
    elif kind == "decision":
        subj = f"Решение по претензии {claim['number']}"
        body = (f"Здравствуйте, {claim['client_name']}!\n\nРешение: {claim['decision']}\n"
                f"Плановая дата исполнения: {fmt(claim['plan_due'])}.\n\nСтатус: {link}")
    elif kind == "rejected":
        subj = f"Ответ на претензию {claim['number']}"
        body = (f"Здравствуйте, {claim['client_name']}!\n\nПо результатам проверки: {claim['decision']}\n"
                f"Фото и материалы проверки доступны по ссылке: {link}\n"
                f"Если вы не согласны, ответьте на это письмо — мы организуем независимую экспертизу.")
    elif kind == "extension":
        subj = f"Претензия {claim['number']}: просим согласовать новый срок"
        body = (f"Здравствуйте, {claim['client_name']}!\n\nПо вашей претензии требуются материалы, срок поставки "
                f"которых превышает установленный срок исправления.\nПричина: {extra['reason']}\n"
                f"Ожидаемая поставка: {fmt(extra.get('supply_date'))}.\n"
                f"Предлагаем новый срок исполнения: {fmt(extra['proposed_due'])} (сейчас — {fmt(claim['legal_due'])}).\n\n"
                f"Пожалуйста, подтвердите согласие или откажитесь по ссылке: {link}\n"
                f"Если новый срок не подходит, мы свяжемся с вами и предложим варианты "
                f"(временное решение или компенсация).")
    elif kind == "done":
        subj = f"Работы по претензии {claim['number']} выполнены"
        body = (f"Здравствуйте, {claim['client_name']}!\n\nРаботы по вашей претензии выполнены.\n"
                f"Пожалуйста, подтвердите результат по ссылке: {link}")
    elif kind == "survey":
        subj = f"Оцените решение по претензии {claim['number']}"
        body = (f"Здравствуйте, {claim['client_name']}!\n\nНам важно, что проблема действительно решена.\n"
                f"Пожалуйста, ответьте на 2 вопроса (1 минута): {link}")
    else:
        raise ValueError(kind)
    body += sign
    ok = send_email(claim["client_email"], subj, body) if claim["client_email"] else False
    audit(c, claim["id"], "система", "письмо клиенту" if ok else "письмо клиенту НЕ отправлено",
          "letter", None, subj, None if ok else "нет email клиента или ошибка отправки — сообщить по телефону")
    return ok


# ---------- создание ----------

def create_claim(c, user, form: dict, brigade_ids: list[int], extra_ids: list[int]) -> int:
    received = L.parse_date(form["received_at"]) or now().date()
    due = L.legal_due(received, form["demand_type"])
    owner = (users_by_role(c, "np") or [None])[0]
    number = next_number(c)
    cur = c.execute(
        """INSERT INTO claims (number, status, received_at, accepted_by, accepted_at, client_name, client_phone,
           client_email, address, contract_no, contract_date, contract_type, contract_price, demand_type,
           description, owner_id, review_due, legal_due, original_legal_due, client_token, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (number, "review", received.isoformat(), user["id"], now_iso(), form["client_name"].strip(),
         form["client_phone"].strip(), (form.get("client_email") or "").strip() or None,
         (form.get("address") or "").strip() or None, form["contract_no"].strip(), form["contract_date"],
         form["contract_type"], float(form["contract_price"]) if form.get("contract_price") else None,
         form["demand_type"], (form.get("description") or "").strip(), owner["id"] if owner else None,
         L.review_due(received).isoformat(), due.isoformat(), due.isoformat(),
         secrets.token_urlsafe(24), now_iso()))
    cid = cur.lastrowid
    audit(c, cid, user, "претензия принята", None, None, number,
          f"Принял: {user['name']}. Требование: {L.DEMANDS[form['demand_type']]}. Срок: {fmt(due)}")
    if owner:
        add_participant(c, cid, owner["id"], "np", user)
    for r in users_by_role(c, "rop"):
        add_participant(c, cid, r["id"], "rop", user)
    for bid in brigade_ids:
        add_participant(c, cid, bid, "brigade", user)
    for uid in extra_ids:
        u = c.execute("SELECT role FROM users WHERE id=?", (uid,)).fetchone()
        if u:
            add_participant(c, cid, uid, u["role"], user)
    return cid


def distribute_new(c, cid):
    """Копии претензии НП, РОП и сборщикам + письмо клиенту."""
    claim = c.execute("SELECT * FROM claims WHERE id=?", (cid,)).fetchone()
    att = [(os.path.join(UPLOAD_DIR, f["stored_name"]), f["filename"]) for f in files(c, cid, ("scan", "defect"))]
    for p in participants(c, cid):
        notify_user(p, f"Новая претензия {claim['number']}", summary(claim), attachments=att)
    audit(c, cid, "система", "рассылка копий", None, None,
          ", ".join(p["name"] for p in participants(c, cid)))
    client_letter(c, claim, "accepted")


# ---------- решения ----------

def apply_decision(c, user, claim, decision, culprit, culprit_user_id, plan_days, reject: bool):
    set_field(c, claim, user, "decision", decision.strip())
    set_field(c, claim, user, "culprit", culprit)
    set_field(c, claim, user, "culprit_user_id", culprit_user_id)
    if reject:
        set_field(c, claim, user, "status", "rejected", "мотивированный отказ")
        claim = c.execute("SELECT * FROM claims WHERE id=?", (claim["id"],)).fetchone()
        client_letter(c, claim, "rejected")
        return {"needs_extension": False}
    plan_due = now().date() + timedelta(days=int(plan_days))
    set_field(c, claim, user, "plan_days", int(plan_days))
    set_field(c, claim, user, "plan_due", plan_due.isoformat())
    needs_ext = plan_due > L.parse_date(claim["legal_due"])
    set_field(c, claim, user, "status", "decided",
              "план не укладывается в срок — нужен перенос" if needs_ext else None)
    claim = c.execute("SELECT * FROM claims WHERE id=?", (claim["id"],)).fetchone()
    client_letter(c, claim, "decision")
    if not needs_ext:
        set_field(c, claim, user, "status", "in_work")
    return {"needs_extension": needs_ext, "plan_due": plan_due}


def propose_extension(c, user, claim, reason, supply_date, proposed_due):
    """Автоматически отправляет клиенту предложение нового срока со ссылкой на согласие."""
    cur = c.execute(
        "INSERT INTO extensions (claim_id, reason, supply_date, proposed_due, created_by, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (claim["id"], reason, supply_date or None, proposed_due, user["id"] if user and not isinstance(user, str) else None,
         now_iso()))
    audit(c, claim["id"], user, "предложен перенос срока", "legal_due", claim["legal_due"], proposed_due, reason)
    set_field(c, claim, user, "status", "waiting_consent")
    if client_letter(c, claim, "extension",
                     {"reason": reason, "supply_date": supply_date, "proposed_due": proposed_due}):
        c.execute("UPDATE extensions SET sent_at=? WHERE id=?", (now_iso(), cur.lastrowid))
    for p in participants(c, claim["id"]):
        if p["p_role"] in ("np", "rop"):
            notify_user(p, f"{claim['number']}: клиенту отправлен запрос на новый срок",
                        f"Новый срок: {fmt(proposed_due)}. Причина: {reason}\n{claim_url(claim)}")


def client_answer_extension(c, claim, ext, answer):
    c.execute("UPDATE extensions SET answer=?, answered_at=? WHERE id=?", (answer, now_iso(), ext["id"]))
    if answer == "agree":
        audit(c, claim["id"], "клиент", "клиент согласился на новый срок", "legal_due",
              claim["legal_due"], ext["proposed_due"], "согласие получено по ссылке")
        c.execute("UPDATE claims SET legal_due=?, status='in_work' WHERE id=?", (ext["proposed_due"], claim["id"]))
        audit(c, claim["id"], "система", "изменение", "status", claim["status"], "in_work")
        msg = f"Клиент СОГЛАСИЛСЯ на новый срок {fmt(ext['proposed_due'])}."
    else:
        audit(c, claim["id"], "клиент", "клиент НЕ согласился на новый срок", "legal_due",
              claim["legal_due"], claim["legal_due"], "срок не изменён")
        c.execute("UPDATE claims SET status='escalated' WHERE id=?", (claim["id"],))
        audit(c, claim["id"], "система", "изменение", "status", claim["status"], "escalated")
        msg = (f"Клиент НЕ согласился на новый срок. Действующий срок {fmt(claim['legal_due'])}. "
               f"Нужно решение директора: временное решение или компенсация.")
    for p in participants(c, claim["id"]):
        if p["p_role"] in ("np", "rop"):
            notify_user(p, f"{claim['number']}: ответ клиента по сроку", f"{msg}\n{claim_url(claim)}")
    for d in users_by_role(c, "director"):
        notify_user(d, f"{claim['number']}: ответ клиента по сроку", f"{msg}\n{claim_url(claim)}")


def record_checkin(c, user, claim, state, comment):
    today = now().date().isoformat()
    c.execute("INSERT INTO checkins (claim_id, user_id, day, state, comment, created_at) VALUES (?,?,?,?,?,?)",
              (claim["id"], user["id"], today, state, comment, now_iso()))
    names = {"done": "сделано", "problem": "проблема", "help": "нужна помощь"}
    audit(c, claim["id"], user, f"отметка: {names.get(state, state)}", None, None, None, comment)
    if state in ("problem", "help"):
        for p in participants(c, claim["id"]):
            if p["p_role"] == "np" and p["id"] != user["id"]:
                notify_user(p, f"{claim['number']}: {names[state]} от {user['name']}",
                            f"{comment or ''}\n{claim_url(claim)}")


def mark_done(c, user, claim):
    set_field(c, claim, user, "done_at", now().date().isoformat())
    set_field(c, claim, user, "status", "done")
    claim = c.execute("SELECT * FROM claims WHERE id=?", (claim["id"],)).fetchone()
    client_letter(c, claim, "done")


def reopen(c, actor, claim, reason):
    """Возврат в работу: снимаем отметки о выполнении, срок продолжает идти, неустойка считается."""
    for f in ("done_at", "client_confirmed_at", "closed_at", "csat_sent_at"):
        claim = c.execute("SELECT * FROM claims WHERE id=?", (claim["id"],)).fetchone()
        set_field(c, claim, actor, f, None, reason)
    claim = c.execute("SELECT * FROM claims WHERE id=?", (claim["id"],)).fetchone()
    set_field(c, claim, actor, "status", "in_work", reason)


def can_close(c, claim) -> list[str]:
    missing = []
    if not files(c, claim["id"], ("after", "act")):
        missing.append("акт или фото «после»")
    if not claim["client_confirmed_at"]:
        missing.append("подтверждение клиента")
    for f, name in (("culprit", "виновник"), ("rc_symptom", "что сломалось"), ("rc_cause", "почему возникло"),
                    ("rc_escape", "почему не поймали до сдачи"), ("rc_action", "что меняем")):
        if not claim[f]:
            missing.append(name)
    if c.execute("SELECT 1 FROM costs WHERE claim_id=? AND approval LIKE 'pending%'", (claim["id"],)).fetchone():
        missing.append("согласование компенсации")
    return missing


def add_cost(c, user, claim, kind, amount, note, charge_to, charge_user_id):
    amount = float(amount)
    if kind == "compensation" and L.compensation_needs_director(amount) and user["role"] != "director":
        approval = "pending_director"
    elif kind == "compensation" and user["role"] not in ("director", "rop"):
        approval = "pending_director" if L.compensation_needs_director(amount) else "pending_rop"
    else:
        approval = "approved"
    c.execute(
        "INSERT INTO costs (claim_id, kind, amount, note, charge_to, charge_user_id, approval, approved_by,"
        " created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (claim["id"], kind, amount, note, charge_to, charge_user_id or None, approval,
         user["id"] if approval == "approved" else None, user["id"], now_iso()))
    audit(c, claim["id"], user, "затрата", kind, None, f"{amount:.0f} ₽ → {L.CHARGE_TO.get(charge_to, charge_to)}",
          f"{note or ''} [{approval}]")
    if approval == "pending_director":
        for d in users_by_role(c, "director"):
            notify_user(d, f"{claim['number']}: согласуйте компенсацию {amount:.0f} ₽",
                        f"Выше лимита РОП {L.ROP_COMPENSATION_LIMIT:.0f} ₽. {note or ''}\n{claim_url(claim)}")
    elif approval == "pending_rop":
        for r in users_by_role(c, "rop"):
            notify_user(r, f"{claim['number']}: согласуйте компенсацию {amount:.0f} ₽",
                        f"{note or ''}\n{claim_url(claim)}")
    return approval


def approve_cost(c, user, cost, ok: bool):
    claim = c.execute("SELECT * FROM claims WHERE id=?", (cost["claim_id"],)).fetchone()
    need_dir = L.compensation_needs_director(cost["amount"])
    if need_dir and user["role"] != "director":
        raise PermissionError("Компенсацию свыше лимита согласует только директор")
    if not need_dir and user["role"] not in ("director", "rop"):
        raise PermissionError("Согласовать может РОП или директор")
    c.execute("UPDATE costs SET approval=?, approved_by=? WHERE id=?",
              ("approved" if ok else "rejected", user["id"], cost["id"]))
    audit(c, claim["id"], user, "компенсация согласована" if ok else "компенсация отклонена", "cost",
          cost["approval"], f"{cost['amount']:.0f} ₽")
