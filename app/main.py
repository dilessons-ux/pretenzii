"""Веб-приложение «Претензии»: pretenzii.mebel-arigato.ru"""
import csv
import hashlib
import hmac
import io
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import logic as L
from . import services as S
from .db import audit, init_db, now, now_iso, set_field, tx
from .jobs import start_background

BASE = os.path.dirname(__file__)
os.makedirs(S.UPLOAD_DIR, exist_ok=True)
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".pdf", ".mp4", ".mov", ".doc", ".docx"}
MAX_FILE_MB = 50
TG_BOT_NAME = os.getenv("TELEGRAM_BOT_NAME", "")


def hash_pw(pw: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000)
    return f"{salt}${dk.hex()}"


def check_pw(pw: str, stored: str) -> bool:
    salt, _ = stored.split("$", 1)
    return hmac.compare_digest(hash_pw(pw, salt), stored)


def norm_login(v: str) -> str:
    """Телефон → 7XXXXXXXXXX, email → нижний регистр."""
    v = (v or "").strip().lower()
    if "@" in v:
        return v
    d = "".join(ch for ch in v if ch.isdigit())
    if len(d) == 11 and d[0] == "8":
        d = "7" + d[1:]
    if len(d) == 10:
        d = "7" + d
    return d


def bootstrap_admin():
    email, pw = os.getenv("ADMIN_EMAIL"), os.getenv("ADMIN_PASSWORD")
    with tx() as c:
        if c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0 and email and pw:
            c.execute("INSERT INTO users (name, login, email, role, password_hash, created_at) VALUES (?,?,?,?,?,?)",
                      (os.getenv("ADMIN_NAME", "Директор"), email.lower(), email.lower(), "director", hash_pw(pw),
                       now_iso()))


@asynccontextmanager
async def lifespan(app):
    init_db()
    bootstrap_admin()
    start_background()
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SECRET_KEY") or secrets.token_hex(32),
                   max_age=60 * 60 * 24 * 30, https_only=os.getenv("COOKIE_SECURE", "1") == "1")
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
tpl = Jinja2Templates(directory=os.path.join(BASE, "templates"))
tpl.env.globals.update(L=L, fmt=S.fmt)
tpl.env.filters["money"] = lambda v: f"{(v or 0):,.0f}".replace(",", " ") + " ₽"


class Redirect(Exception):
    def __init__(self, url):
        self.url = url


@app.exception_handler(Redirect)
async def _redir(request, exc):
    return RedirectResponse(exc.url, status_code=303)


def current_user(request: Request, roles: tuple | None = None):
    uid = request.session.get("uid")
    if not uid:
        raise Redirect("/login")
    with tx() as c:
        u = c.execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
    if not u:
        request.session.clear()
        raise Redirect("/login")
    if roles and u["role"] not in roles:
        raise HTTPException(403, "Недостаточно прав")
    return u


def flash(request, msg, kind="ok"):
    request.session.setdefault("flash", []).append([kind, msg])


def render(request, name, user=None, **ctx):
    fl = request.session.pop("flash", [])
    return tpl.TemplateResponse(request, name, {"user": user, "flashes": fl, "today": now().date(), **ctx})


def get_claim(c, cid):
    claim = c.execute("SELECT * FROM claims WHERE id=?", (cid,)).fetchone()
    if not claim:
        raise HTTPException(404)
    return claim


async def save_upload(c, claim_id, kind, up: UploadFile, user):
    if not up or not up.filename:
        return None
    ext = os.path.splitext(up.filename)[1].lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"Тип файла {ext} не поддерживается")
    data = await up.read()
    if len(data) > MAX_FILE_MB * 1024 * 1024:
        raise HTTPException(400, f"Файл больше {MAX_FILE_MB} МБ")
    stored = f"{uuid.uuid4().hex}{ext}"
    with open(os.path.join(S.UPLOAD_DIR, stored), "wb") as f:
        f.write(data)
    c.execute("INSERT INTO claim_files (claim_id, kind, filename, stored_name, uploaded_by, uploaded_at)"
              " VALUES (?,?,?,?,?,?)", (claim_id, kind, up.filename, stored,
                                        None if isinstance(user, str) else user["id"], now_iso()))
    audit(c, claim_id, user, "файл загружен", kind, None, up.filename)
    return stored


# ---------------- вход ----------------

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return render(request, "login.html")


@app.post("/login")
def login(request: Request, login: str = Form(...), password: str = Form(...)):
    key = norm_login(login)
    with tx() as c:
        u = c.execute("SELECT * FROM users WHERE (login=? OR email=?) AND active=1", (key, key)).fetchone()
    if not u or not check_pw(password, u["password_hash"]):
        flash(request, "Неверный телефон/email или пароль", "err")
        return RedirectResponse("/login", 303)
    request.session["uid"] = u["id"]
    return RedirectResponse("/", 303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", 303)


# ---------------- список ----------------

@app.get("/", response_class=HTMLResponse)
def index(request: Request, status: str = "open", mine: int = 0, q: str = ""):
    user = current_user(request)
    sql, args = "SELECT c.* FROM claims c WHERE 1=1", []
    if status == "open":
        sql += f" AND c.status IN ({','.join('?' * len(L.OPEN_STATUSES))})"
        args += list(L.OPEN_STATUSES)
    elif status in L.STATUSES:
        sql += " AND c.status = ?"
        args.append(status)
    if mine:
        sql += " AND c.id IN (SELECT claim_id FROM claim_participants WHERE user_id=?)"
        args.append(user["id"])
    if q:
        sql += " AND (c.number LIKE ? OR c.client_name LIKE ? OR c.contract_no LIKE ? OR c.client_phone LIKE ?)"
        args += [f"%{q}%"] * 4
    sql += " ORDER BY c.legal_due"
    with tx() as c:
        rows = c.execute(sql, args).fetchall()
    today = now().date()
    items = [{"c": r, "light": L.traffic(r, today), "left": L.days_left(r, today)} for r in rows]
    return render(request, "list.html", user, items=items, status=status, mine=mine, q=q)


# ---------------- приём ----------------

@app.get("/claims/new", response_class=HTMLResponse)
def new_page(request: Request):
    user = current_user(request)
    with tx() as c:
        staff = c.execute("SELECT * FROM users WHERE active=1 ORDER BY role, name").fetchall()
    return render(request, "new.html", user, staff=staff)


@app.post("/claims/new")
async def new_claim(request: Request):
    user = current_user(request)
    form = await request.form()
    required = ("client_name", "client_phone", "contract_no", "contract_date", "contract_type", "demand_type",
                "received_at")
    data = {k: form.get(k) for k in required + ("client_email", "address", "contract_price", "description")}
    if any(not data[k] for k in required):
        flash(request, "Заполните обязательные поля", "err")
        return RedirectResponse("/claims/new", 303)
    scans = [f for f in form.getlist("scan") if getattr(f, "filename", "")]
    if not scans:
        flash(request, "Загрузите скан или фото претензии", "err")
        return RedirectResponse("/claims/new", 303)
    brigade = [int(x) for x in form.getlist("brigade")]
    extra = [int(x) for x in form.getlist("extra")]
    with tx() as c:
        cid = S.create_claim(c, user, data, brigade, extra)
        for f in scans:
            await save_upload(c, cid, "scan", f, user)
        for f in form.getlist("defect"):
            if getattr(f, "filename", ""):
                await save_upload(c, cid, "defect", f, user)
        S.distribute_new(c, cid)
    flash(request, "Претензия зарегистрирована, копии отправлены участникам, клиенту ушло подтверждение")
    return RedirectResponse(f"/claims/{cid}", 303)


# ---------------- карточка ----------------

@app.get("/claims/{cid}", response_class=HTMLResponse)
def claim_page(request: Request, cid: int):
    user = current_user(request)
    today = now().date()
    with tx() as c:
        claim = get_claim(c, cid)
        ctx = dict(
            claim=claim,
            ppl=S.participants(c, cid),
            files=S.files(c, cid),
            log=c.execute("SELECT * FROM audit_log WHERE claim_id=? AND action NOT IN"
                          " ('напоминание отправлено') ORDER BY id DESC", (cid,)).fetchall(),
            checkins=c.execute("SELECT k.*, u.name FROM checkins k JOIN users u ON u.id=k.user_id"
                               " WHERE claim_id=? ORDER BY k.id DESC", (cid,)).fetchall(),
            exts=c.execute("SELECT * FROM extensions WHERE claim_id=? ORDER BY id DESC", (cid,)).fetchall(),
            costs=c.execute("SELECT k.*, u.name AS charge_name FROM costs k LEFT JOIN users u"
                            " ON u.id=k.charge_user_id WHERE claim_id=? ORDER BY k.id", (cid,)).fetchall(),
            accepted_by=c.execute("SELECT name FROM users WHERE id=?", (claim["accepted_by"],)).fetchone(),
            staff=c.execute("SELECT * FROM users WHERE active=1 ORDER BY role, name").fetchall(),
            missing=S.can_close(c, claim),
            penalty=L.penalty(claim, today),
            light=L.traffic(claim, today),
            left=L.days_left(claim, today),
            checked_today=c.execute("SELECT 1 FROM checkins WHERE claim_id=? AND user_id=? AND day=?",
                                    (cid, user["id"], today.isoformat())).fetchone(),
            client_link=S.client_url(claim),
        )
    total = sum(x["amount"] for x in ctx["costs"] if x["approval"] == "approved")
    charged = sum(x["amount"] for x in ctx["costs"] if x["approval"] == "approved" and x["charge_to"] != "company")
    return render(request, "claim.html", user, total=total, charged=charged, **ctx)


@app.post("/claims/{cid}/decision")
def decision(request: Request, cid: int, decision: str = Form(...), culprit: str = Form(...),
             culprit_user_id: str = Form(""), plan_days: str = Form(""), action: str = Form("accept")):
    user = current_user(request, ("np", "director", "rop"))
    reject = action == "reject"
    if not reject and not plan_days.isdigit():
        flash(request, "Укажите количество дней на исполнение", "err")
        return RedirectResponse(f"/claims/{cid}", 303)
    if reject and culprit not in ("client", "none"):
        flash(request, "Отказ возможен только если виновник — клиент или недостаток не подтверждён", "err")
        return RedirectResponse(f"/claims/{cid}", 303)
    with tx() as c:
        claim = get_claim(c, cid)
        if reject and not S.files(c, cid, ("defect", "other")):
            flash(request, "Для отказа приложите фото/материалы проверки", "err")
            return RedirectResponse(f"/claims/{cid}", 303)
        res = S.apply_decision(c, user, claim, decision, culprit,
                               int(culprit_user_id) if culprit_user_id else None, plan_days or 0, reject)
    if res["needs_extension"]:
        flash(request, "План не укладывается в юридический срок. Оформите запрос нового срока клиенту ниже.", "warn")
    else:
        flash(request, "Решение сохранено, клиенту отправлено письмо")
    return RedirectResponse(f"/claims/{cid}", 303)


@app.post("/claims/{cid}/extension")
def extension(request: Request, cid: int, reason: str = Form(...), supply_date: str = Form(""),
              proposed_due: str = Form(...)):
    user = current_user(request, ("np", "director", "rop", "supply"))
    with tx() as c:
        claim = get_claim(c, cid)
        if L.parse_date(proposed_due) <= L.parse_date(claim["legal_due"]):
            flash(request, "Новая дата должна быть позже действующего срока", "err")
            return RedirectResponse(f"/claims/{cid}", 303)
        S.propose_extension(c, user, claim, reason.strip(), supply_date, proposed_due)
    flash(request, "Клиенту отправлено предложение нового срока. Срок изменится только после его согласия.")
    return RedirectResponse(f"/claims/{cid}", 303)


@app.post("/claims/{cid}/checkin")
def checkin(request: Request, cid: int, state: str = Form(...), comment: str = Form("")):
    user = current_user(request)
    if state in ("problem", "help") and not comment.strip():
        flash(request, "Опишите проблему в комментарии", "err")
        return RedirectResponse(f"/claims/{cid}", 303)
    with tx() as c:
        S.record_checkin(c, user, get_claim(c, cid), state, comment.strip() or None)
    flash(request, "Отметка записана")
    return RedirectResponse(f"/claims/{cid}", 303)


@app.post("/claims/{cid}/comment")
def comment(request: Request, cid: int, text: str = Form(...)):
    user = current_user(request)
    with tx() as c:
        get_claim(c, cid)
        audit(c, cid, user, "комментарий", None, None, None, text.strip())
    return RedirectResponse(f"/claims/{cid}#log", 303)


@app.post("/claims/{cid}/files")
async def upload(request: Request, cid: int, kind: str = Form(...), files: list[UploadFile] = File(...)):
    user = current_user(request)
    with tx() as c:
        get_claim(c, cid)
        for f in files:
            await save_upload(c, cid, kind, f, user)
    flash(request, "Файлы загружены")
    return RedirectResponse(f"/claims/{cid}", 303)


@app.get("/files/{fid}")
def get_file(request: Request, fid: int):
    current_user(request)
    with tx() as c:
        f = c.execute("SELECT * FROM claim_files WHERE id=?", (fid,)).fetchone()
    if not f:
        raise HTTPException(404)
    return FileResponse(os.path.join(S.UPLOAD_DIR, f["stored_name"]), filename=f["filename"])


@app.post("/claims/{cid}/participants")
def add_people(request: Request, cid: int, user_id: int = Form(...)):
    user = current_user(request, ("np", "director", "rop"))
    with tx() as c:
        claim = get_claim(c, cid)
        u = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        S.add_participant(c, cid, user_id, u["role"], user)
        S.notify_user(u, f"Вас добавили в претензию {claim['number']}", S.summary(claim))
    return RedirectResponse(f"/claims/{cid}", 303)


@app.post("/claims/{cid}/done")
def done(request: Request, cid: int):
    user = current_user(request, ("np", "director", "rop", "brigade"))
    with tx() as c:
        claim = get_claim(c, cid)
        if not S.files(c, cid, ("after", "act")):
            flash(request, "Сначала загрузите фото «после» или акт", "err")
            return RedirectResponse(f"/claims/{cid}", 303)
        S.mark_done(c, user, claim)
    flash(request, "Отмечено выполнение. Клиенту отправлена ссылка для подтверждения.")
    return RedirectResponse(f"/claims/{cid}", 303)


@app.post("/claims/{cid}/client-confirm")
def staff_confirm(request: Request, cid: int, how: str = Form(...)):
    """Подтверждение клиента на месте: подпись на акте (акт загружен) — фиксирует сотрудник."""
    user = current_user(request, ("np", "director", "rop", "brigade"))
    with tx() as c:
        claim = get_claim(c, cid)
        if not S.files(c, cid, ("act",)):
            flash(request, "Загрузите подписанный клиентом акт", "err")
            return RedirectResponse(f"/claims/{cid}", 303)
        set_field(c, claim, user, "client_confirmed_at", now_iso(), f"подтверждение: {how}")
    flash(request, "Подтверждение клиента зафиксировано")
    return RedirectResponse(f"/claims/{cid}", 303)


@app.post("/claims/{cid}/rootcause")
def rootcause(request: Request, cid: int, culprit: str = Form(...), culprit_user_id: str = Form(""),
              rc_symptom: str = Form(""), rc_cause: str = Form(""), rc_escape: str = Form(""),
              rc_action: str = Form("")):
    user = current_user(request, ("np", "director"))
    with tx() as c:
        claim = get_claim(c, cid)
        set_field(c, claim, user, "culprit", culprit)
        set_field(c, claim, user, "culprit_user_id", int(culprit_user_id) if culprit_user_id else None)
        for k, v in (("rc_symptom", rc_symptom), ("rc_cause", rc_cause), ("rc_escape", rc_escape),
                     ("rc_action", rc_action)):
            claim = get_claim(c, cid)
            set_field(c, claim, user, k, v.strip() or None)
        # Повторяемость: 3 одинаковые причины за 90 дней
        since = (now().date() - timedelta(days=90)).isoformat()
        n = c.execute("SELECT COUNT(*) FROM claims WHERE culprit=? AND received_at>=? AND culprit NOT IN"
                      " ('client','none')", (culprit, since)).fetchone()[0]
        if n >= 3:
            audit(c, cid, "система", "повторяющаяся причина", "culprit", None,
                  f"{L.CULPRITS[culprit]}: {n} за 90 дней", "нужна задача на изменение процесса")
            for d in S.users_by_role(c, "director") + S.users_by_role(c, "np"):
                S.notify_user(d, f"Повторяющаяся причина рекламаций: {L.CULPRITS[culprit]}",
                              f"{n} претензий за 90 дней. Нужна корректирующая мера.")
    flash(request, "Разбор причины сохранён")
    return RedirectResponse(f"/claims/{cid}", 303)


@app.post("/claims/{cid}/cost")
def cost(request: Request, cid: int, kind: str = Form(...), amount: float = Form(...), note: str = Form(""),
         charge_to: str = Form("company"), charge_user_id: str = Form("")):
    user = current_user(request, ("np", "director", "rop", "supply"))
    with tx() as c:
        approval = S.add_cost(c, user, get_claim(c, cid), kind, amount, note.strip(), charge_to,
                              int(charge_user_id) if charge_user_id else None)
    flash(request, "Затрата добавлена" + (" — ждёт согласования" if approval.startswith("pending") else ""))
    return RedirectResponse(f"/claims/{cid}#costs", 303)


@app.post("/costs/{kid}/approve")
def cost_approve(request: Request, kid: int, ok: int = Form(...)):
    user = current_user(request, ("director", "rop"))
    with tx() as c:
        k = c.execute("SELECT * FROM costs WHERE id=?", (kid,)).fetchone()
        try:
            S.approve_cost(c, user, k, bool(ok))
        except PermissionError as e:
            flash(request, str(e), "err")
    return RedirectResponse(f"/claims/{k['claim_id']}#costs", 303)


@app.post("/claims/{cid}/close")
def close(request: Request, cid: int):
    user = current_user(request, ("np", "director"))
    with tx() as c:
        claim = get_claim(c, cid)
        missing = S.can_close(c, claim)
        if missing:
            flash(request, "Нельзя закрыть: не хватает — " + ", ".join(missing), "err")
            return RedirectResponse(f"/claims/{cid}", 303)
        set_field(c, claim, user, "closed_at", now_iso())
        set_field(c, get_claim(c, cid), user, "status", "closed")
    flash(request, "Претензия закрыта. Через 48 часов после подтверждения клиенту уйдёт опрос.")
    return RedirectResponse(f"/claims/{cid}", 303)


@app.post("/claims/{cid}/status")
def change_status(request: Request, cid: int, status: str = Form(...), reason: str = Form(...)):
    """Ручная смена статуса (переоткрыть, вернуть в работу) — только с причиной."""
    user = current_user(request, ("np", "director"))
    if status not in L.STATUSES or not reason.strip():
        raise HTTPException(400)
    with tx() as c:
        if status == "in_work":
            S.reopen(c, user, get_claim(c, cid), reason.strip())
        else:
            set_field(c, get_claim(c, cid), user, "status", status, reason.strip())
    return RedirectResponse(f"/claims/{cid}", 303)


@app.get("/claims/{cid}/share/{channel}")
def share_client(request: Request, cid: int, channel: str):
    """Сообщение клиенту с телефона сотрудника: WhatsApp / Telegram / SMS. Факт фиксируется в журнале."""
    user = current_user(request)
    from urllib.parse import quote
    with tx() as c:
        claim = get_claim(c, cid)
        kind = S.current_letter_kind(c, claim)
        subj, body = S.letter_text(c, claim, kind)
        text = f"{subj}\n\n{body}"
        phone = norm_login(claim["client_phone"])
        names = {"wa": "WhatsApp", "tg": "Telegram", "sms": "SMS"}
        if channel not in names:
            raise HTTPException(400)
        audit(c, cid, user, f"сообщение клиенту через {names[channel]}", "letter", None, subj,
              "открыто на устройстве сотрудника")
    if channel == "wa":
        url = f"https://wa.me/{phone}?text={quote(text)}"
    elif channel == "tg":
        url = f"https://t.me/+{phone}"
    else:
        url = f"sms:+{phone}?&body={quote(text)}"
    return RedirectResponse(url, 303)


# ---------------- страница клиента ----------------

def client_claim(c, token):
    claim = c.execute("SELECT * FROM claims WHERE client_token=?", (token,)).fetchone()
    if not claim:
        raise HTTPException(404)
    return claim


@app.get("/c/{token}", response_class=HTMLResponse)
def client_page(request: Request, token: str):
    with tx() as c:
        claim = client_claim(c, token)
        ext = c.execute("SELECT * FROM extensions WHERE claim_id=? ORDER BY id DESC LIMIT 1",
                        (claim["id"],)).fetchone()
        after = S.files(c, claim["id"], ("after",))
    return render(request, "client.html", claim=claim, ext=ext, after=after, token=token)


@app.get("/c/{token}/file/{fid}")
def client_file(token: str, fid: int):
    with tx() as c:
        claim = client_claim(c, token)
        f = c.execute("SELECT * FROM claim_files WHERE id=? AND claim_id=? AND kind IN ('after','defect','act')",
                      (fid, claim["id"])).fetchone()
    if not f:
        raise HTTPException(404)
    return FileResponse(os.path.join(S.UPLOAD_DIR, f["stored_name"]), filename=f["filename"])


@app.post("/c/{token}/extension/{eid}")
def client_extension(token: str, eid: int, answer: str = Form(...)):
    if answer not in ("agree", "disagree"):
        raise HTTPException(400)
    with tx() as c:
        claim = client_claim(c, token)
        ext = c.execute("SELECT * FROM extensions WHERE id=? AND claim_id=?", (eid, claim["id"])).fetchone()
        if ext and not ext["answer"]:
            S.client_answer_extension(c, claim, ext, answer)
    return RedirectResponse(f"/c/{token}", 303)


@app.post("/c/{token}/confirm")
def client_confirm(token: str, ok: int = Form(...), comment: str = Form("")):
    with tx() as c:
        claim = client_claim(c, token)
        if claim["status"] != "done" or claim["client_confirmed_at"]:
            return RedirectResponse(f"/c/{token}", 303)
        if ok:
            set_field(c, claim, "клиент", "client_confirmed_at", now_iso(), "подтверждено по ссылке")
        else:
            audit(c, claim["id"], "клиент", "клиент НЕ подтвердил выполнение", None, None, None, comment)
            S.reopen(c, "клиент", claim, "клиент не подтвердил: " + comment)
            for p in S.participants(c, claim["id"]):
                if p["p_role"] in ("np", "rop"):
                    S.notify_user(p, f"{claim['number']}: клиент не подтвердил выполнение",
                                  f"{comment}\n{S.claim_url(claim)}")
    return RedirectResponse(f"/c/{token}", 303)


@app.post("/c/{token}/survey")
def client_survey(token: str, csat: int = Form(...), solved: int = Form(...), comment: str = Form("")):
    with tx() as c:
        claim = client_claim(c, token)
        if not claim["client_confirmed_at"] or (claim["csat_at"] and claim["csat_at"] >= claim["client_confirmed_at"]):
            return RedirectResponse(f"/c/{token}", 303)
        c.execute("UPDATE claims SET csat=?, csat_solved=?, csat_comment=?, csat_at=? WHERE id=?",
                  (max(1, min(5, csat)), 1 if solved else 0, comment.strip() or None, now_iso(), claim["id"]))
        audit(c, claim["id"], "клиент", "отзыв клиента", "csat", None, f"{csat}/5, решено: {'да' if solved else 'нет'}",
              comment.strip() or None)
        if csat <= 2 or not solved:
            S.reopen(c, "система", get_claim(c, claim["id"]), "переоткрыта по отзыву клиента")
            for u in S.users_by_role(c, "director") + S.users_by_role(c, "np"):
                S.notify_user(u, f"{claim['number']} переоткрыта по отзыву клиента",
                              f"Оценка {csat}/5, решено: {'да' if solved else 'нет'}. {comment}\n{S.claim_url(claim)}")
    return RedirectResponse(f"/c/{token}", 303)


# ---------------- сотрудники и профиль ----------------

@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request):
    user = current_user(request, ("director",))
    with tx() as c:
        for u in c.execute("SELECT id FROM users WHERE tg_link_code IS NULL AND telegram_chat_id IS NULL").fetchall():
            c.execute("UPDATE users SET tg_link_code=? WHERE id=?", (secrets.token_urlsafe(9), u["id"]))
        users = c.execute("SELECT * FROM users ORDER BY active DESC, role, name").fetchall()
    return render(request, "users.html", user, users=users, tg_bot=TG_BOT_NAME)


@app.post("/users")
def users_add(request: Request, name: str = Form(...), phone: str = Form(...), role: str = Form(...),
              email: str = Form(""), password: str = Form(...)):
    current_user(request, ("director",))
    login_key = norm_login(phone)
    if role not in L.ROLES or len(password) < 8 or len(login_key) != 11:
        flash(request, "Проверьте телефон (11 цифр) и пароль (не короче 8 символов)", "err")
        return RedirectResponse("/users", 303)
    with tx() as c:
        try:
            c.execute("INSERT INTO users (name, login, email, phone, role, password_hash, tg_link_code, created_at)"
                      " VALUES (?,?,?,?,?,?,?,?)", (name.strip(), login_key, email.strip().lower() or None,
                                                   phone.strip(), role, hash_pw(password), secrets.token_urlsafe(9),
                                                   now_iso()))
        except Exception:
            flash(request, "Сотрудник с таким телефоном уже есть", "err")
            return RedirectResponse("/users", 303)
    flash(request, f"Сотрудник {name} добавлен. Отправьте ему ссылку на Telegram-бота из таблицы.")
    return RedirectResponse("/users", 303)


@app.post("/users/{uid}/password")
def users_reset_pw(request: Request, uid: int, password: str = Form(...)):
    current_user(request, ("director",))
    if len(password) < 8:
        flash(request, "Пароль не короче 8 символов", "err")
    else:
        with tx() as c:
            c.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_pw(password), uid))
        flash(request, "Пароль изменён")
    return RedirectResponse("/users", 303)


@app.post("/users/{uid}/tg-reset")
def users_tg_reset(request: Request, uid: int):
    current_user(request, ("director",))
    with tx() as c:
        c.execute("UPDATE users SET telegram_chat_id=NULL, tg_link_code=? WHERE id=?", (secrets.token_urlsafe(9), uid))
    flash(request, "Привязка Telegram сброшена, выдана новая ссылка")
    return RedirectResponse("/users", 303)


@app.post("/users/{uid}/toggle")
def users_toggle(request: Request, uid: int):
    me = current_user(request, ("director",))
    if uid == me["id"]:
        raise HTTPException(400)
    with tx() as c:
        c.execute("UPDATE users SET active = 1 - active WHERE id=?", (uid,))
    return RedirectResponse("/users", 303)


@app.get("/profile", response_class=HTMLResponse)
def profile(request: Request):
    user = current_user(request)
    if not user["tg_link_code"] and not user["telegram_chat_id"]:
        with tx() as c:
            c.execute("UPDATE users SET tg_link_code=? WHERE id=?", (secrets.token_urlsafe(9), user["id"]))
        user = current_user(request)
    tg_link = f"https://t.me/{TG_BOT_NAME}?start={user['tg_link_code']}" if TG_BOT_NAME and user["tg_link_code"] else None
    return render(request, "profile.html", user, tg_link=tg_link)


@app.post("/profile/password")
def change_pw(request: Request, old: str = Form(...), new: str = Form(...)):
    user = current_user(request)
    if not check_pw(old, user["password_hash"]) or len(new) < 8:
        flash(request, "Старый пароль неверен или новый короче 8 символов", "err")
    else:
        with tx() as c:
            c.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_pw(new), user["id"]))
        flash(request, "Пароль изменён")
    return RedirectResponse("/profile", 303)


# ---------------- дашборд и выгрузка ----------------

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, days: int = 90):
    user = current_user(request, ("director", "np", "rop"))
    since = (now().date() - timedelta(days=days)).isoformat()
    today = now().date()
    with tx() as c:
        rows = c.execute("SELECT * FROM claims WHERE received_at >= ?", (since,)).fetchall()
        costs = c.execute("SELECT k.* FROM costs k JOIN claims c ON c.id=k.claim_id WHERE c.received_at>=?"
                          " AND k.approval='approved'", (since,)).fetchall()
        brig = c.execute("""SELECT u.name, COUNT(*) n FROM claims c JOIN users u ON u.id=c.culprit_user_id
                            WHERE c.received_at>=? GROUP BY u.id ORDER BY n DESC""", (since,)).fetchall()
    finished = [r for r in rows if r["done_at"]]
    on_time = [r for r in finished if L.parse_date(r["done_at"]) <= L.parse_date(r["legal_due"])]
    overdue_open = [r for r in rows if r["status"] in L.OPEN_STATUSES and L.days_left(r, today) < 0]
    durations = sorted((L.parse_date(r["done_at"]) - L.parse_date(r["received_at"])).days for r in finished)
    rated = [r for r in rows if r["csat"]]
    by_culprit = {}
    for r in rows:
        if r["culprit"]:
            by_culprit[r["culprit"]] = by_culprit.get(r["culprit"], 0) + 1
    m = dict(
        total=len(rows),
        open=sum(r["status"] in L.OPEN_STATUSES for r in rows),
        on_time_pct=round(100 * len(on_time) / len(finished)) if finished else None,
        overdue_open=len(overdue_open),
        median_days=durations[len(durations) // 2] if durations else None,
        csat_pct=round(100 * sum(r["csat"] >= 4 for r in rated) / len(rated)) if rated else None,
        csat_n=len(rated),
        cost_total=sum(k["amount"] for k in costs),
        cost_charged=sum(k["amount"] for k in costs if k["charge_to"] != "company"),
        penalties=sum(L.penalty(r, today)[1] for r in rows),
    )
    return render(request, "dashboard.html", user, m=m, by_culprit=sorted(by_culprit.items(), key=lambda x: -x[1]),
                  brig=brig, days=days)


@app.get("/export.csv")
def export(request: Request):
    current_user(request, ("director", "np", "rop"))
    with tx() as c:
        rows = c.execute("SELECT * FROM claims ORDER BY id").fetchall()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    cols = ["number", "status", "received_at", "client_name", "client_phone", "contract_no", "contract_type",
            "demand_type", "culprit", "plan_due", "legal_due", "original_legal_due", "done_at", "closed_at", "csat"]
    w.writerow(cols)
    for r in rows:
        w.writerow([r[k] for k in cols])
    data = "﻿" + buf.getvalue()
    return StreamingResponse(iter([data]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=pretenzii.csv"})


@app.get("/health")
def health():
    return {"ok": True}
