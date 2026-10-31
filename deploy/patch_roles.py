#!/usr/bin/env python3
"""Патч: менять роли сотрудников может только владелец (учётка ADMIN_EMAIL из .env).

Запуск на сервере:  /opt/pretenzii/venv/bin/python /opt/pretenzii/deploy/patch_roles.py
Что делает:
  1. POST /users/{id}/role — смена роли; доступ только у владельца (иначе 403).
  2. Создавать пользователей с ролью «Директор» тоже может только владелец.
  3. В списке «Сотрудники» у владельца появляется выпадающий список роли.
Перед правкой делает копии .bak_roles, проверяет, что нужные куски кода на месте
(иначе ничего не меняет), компилирует результат. Повторный запуск безопасен.
"""
import py_compile
import shutil
import sys

APP = "/opt/pretenzii/app"
MAIN = f"{APP}/main.py"
TPL = f"{APP}/templates/users.html"

MARK = "# --- roles-owner-patch ---"

TPL_ANCHOR = 'tpl = Jinja2Templates(directory=os.path.join(BASE, "templates"))\n'
TPL_NEW = TPL_ANCHOR + (
    f'{MARK}\n'
    'OWNER_LOGIN = (os.getenv("ADMIN_EMAIL", "") or "").strip().lower()\n'
    'tpl.env.globals["OWNER_LOGIN"] = OWNER_LOGIN\n'
)

ADD_ANCHOR = '    current_user(request, ("director",))\n    login_key = norm_login(phone)\n'
ADD_NEW = (
    '    _me = current_user(request, ("director",))\n'
    '    if role == "director" and _me["login"] != OWNER_LOGIN:\n'
    '        raise HTTPException(403)\n'
    '    login_key = norm_login(phone)\n'
)

ROUTE_ANCHOR = '@app.post("/users/{uid}/toggle")\n'
ROUTE_NEW = '''@app.post("/users/{uid}/role")
def users_role(request: Request, uid: int, role: str = Form(...)):
    me = current_user(request, ("director",))
    if not OWNER_LOGIN or me["login"] != OWNER_LOGIN:
        raise HTTPException(403)
    if role not in L.ROLES or uid == me["id"]:
        raise HTTPException(400)
    with tx() as c:
        old = c.execute("SELECT name, role FROM users WHERE id=?", (uid,)).fetchone()
        if not old:
            raise HTTPException(404)
        c.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
    import logging
    logging.getLogger("roles").warning("role change: %s (%s) %s -> %s by %s", old["name"], uid, old["role"], role, me["login"])
    flash(request, f"{old['name']}: роль изменена на «{L.ROLES[role]}»")
    return RedirectResponse("/users", 303)


''' + ROUTE_ANCHOR

HTML_ANCHOR = '        <form method="post" action="/users/{{ u.id }}/toggle">'
HTML_NEW = (
    '        {% if user.login == OWNER_LOGIN %}<form method="post" action="/users/{{ u.id }}/role" class="inline-form">'
    '<select name="role" onchange="this.form.submit()">{% for k, v in L.ROLES.items() %}'
    '<option value="{{k}}" {% if u.role==k %}selected{% endif %}>{{v}}</option>{% endfor %}</select></form>{% endif %}\n'
    + HTML_ANCHOR
)


def main():
    main_src = open(MAIN, encoding="utf-8").read()
    tpl_src = open(TPL, encoding="utf-8").read()
    if MARK in main_src:
        print("Патч уже применён, ничего не меняю.")
        return 0
    for name, src, anchor in (("main.py: tpl", main_src, TPL_ANCHOR), ("main.py: users_add", main_src, ADD_ANCHOR),
                              ("main.py: toggle", main_src, ROUTE_ANCHOR), ("users.html", tpl_src, HTML_ANCHOR)):
        if src.count(anchor) != 1:
            print(f"ОШИБКА: не нашёл ожидаемый кусок кода ({name}). Ничего не изменено.")
            return 1
    shutil.copy(MAIN, MAIN + ".bak_roles")
    shutil.copy(TPL, TPL + ".bak_roles")
    main_src = main_src.replace(TPL_ANCHOR, TPL_NEW).replace(ADD_ANCHOR, ADD_NEW).replace(ROUTE_ANCHOR, ROUTE_NEW)
    tpl_src = tpl_src.replace(HTML_ANCHOR, HTML_NEW)
    open(MAIN, "w", encoding="utf-8").write(main_src)
    open(TPL, "w", encoding="utf-8").write(tpl_src)
    try:
        py_compile.compile(MAIN, doraise=True)
    except py_compile.PyCompileError as e:
        shutil.copy(MAIN + ".bak_roles", MAIN)
        shutil.copy(TPL + ".bak_roles", TPL)
        print("ОШИБКА компиляции, вернул прежние файлы:", e)
        return 1
    print("Готово. Перезапусти службу: systemctl restart pretenzii")
    return 0


if __name__ == "__main__":
    sys.exit(main())
