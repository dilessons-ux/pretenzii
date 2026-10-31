#!/usr/bin/env python3
"""Патч: автозаполнение претензии из amoCRM по номеру договора.

Запуск на сервере:
  curl -fsSL https://raw.githubusercontent.com/dilessons-ux/pretenzii/main/deploy/patch_amocrm.py -o /tmp/patch_amocrm.py \
    && /opt/pretenzii/venv/bin/python /tmp/patch_amocrm.py && systemctl restart pretenzii

Что делает:
  1. Кладёт app/amocrm.py (скачивает с GitHub).
  2. Добавляет в main.py маршрут GET /api/amocrm/contract?number=… (только для вошедших сотрудников).
  3. В форму «Новая претензия» добавляет кнопку «Подтянуть из amoCRM»: заполняет клиента, договор,
     адрес и отмечает сборщиков бригады из сделки.
Перед правкой делает копии .bak_amo; если нужные куски кода не найдены — ничего не меняет.
"""
import py_compile
import shutil
import sys
import urllib.request

APP = "/opt/pretenzii/app"
MAIN = f"{APP}/main.py"
TPL = f"{APP}/templates/new.html"
MODULE = f"{APP}/amocrm.py"
MODULE_URL = "https://raw.githubusercontent.com/dilessons-ux/pretenzii/main/app/amocrm.py"
MARK = "# --- amocrm-patch ---"

ROUTE_ANCHOR = "# ---------------- дашборд и выгрузка ----------------\n"
ROUTE_NEW = f'''{MARK}
@app.get("/api/amocrm/contract")
def amocrm_contract(request: Request, number: str = ""):
    current_user(request)
    from fastapi.responses import JSONResponse
    from . import amocrm
    with tx() as c:
        staff = [(r["id"], r["name"]) for r in
                 c.execute("SELECT id, name FROM users WHERE active=1 AND role='brigade'").fetchall()]
    try:
        return JSONResponse(amocrm.lookup(number, staff))
    except amocrm.AmoError as e:
        return JSONResponse({{"error": str(e)}}, status_code=502)


''' + ROUTE_ANCHOR

INPUT_ANCHOR = '      <label>Номер договора * <input name="contract_no" required></label>\n'
INPUT_NEW = (
    '      <label>Номер договора * <span class="amo-row" style="display:flex;gap:8px">'
    '<input name="contract_no" required style="flex:1">'
    '<button type="button" id="amo-fetch" class="btn btn-sm">Подтянуть из amoCRM</button></span></label>\n'
)

SCRIPT_ANCHOR = "</form>\n{% endblock %}"
SCRIPT_NEW = r'''</form>
<script>
(function () {
  var f = document.querySelector('form[action="/claims/new"]');
  var no = f.elements['contract_no'];
  var btn = document.getElementById('amo-fetch');
  var info = document.createElement('div');
  info.className = 'hint';
  no.closest('fieldset').appendChild(info);
  var staffNames = {};
  f.querySelectorAll('input[name="brigade"]').forEach(function (cb) {
    staffNames[cb.value] = cb.parentNode.textContent.trim();
  });
  function say(text, bad) { info.textContent = text; info.style.color = bad ? '#b00020' : ''; }
  function put(name, v) {
    if (v === null || v === undefined || v === '') return;
    var el = f.elements[name];
    if (el) el.value = v;
  }
  function apply(o) {
    put('client_name', o.client_name); put('client_phone', o.client_phone); put('client_email', o.client_email);
    put('contract_date', o.contract_date); put('address', o.address); put('contract_price', o.price);
    f.querySelectorAll('input[name="brigade"]').forEach(function (cb) { cb.checked = false; });
    var names = [];
    (o.brigade_ids || []).forEach(function (id) {
      var cb = f.querySelector('input[name="brigade"][value="' + id + '"]');
      if (cb) { cb.checked = true; names.push(staffNames[id]); }
    });
    var parts = ['Подтянуто из amoCRM: ' + (o.client_name || 'клиент')];
    if (o.deal_name) parts.push('сделка «' + o.deal_name + '»');
    if (o.install_date) parts.push('установка ' + o.install_date);
    if (o.crew && o.crew.length) {
      parts.push('бригада в amoCRM: ' + o.crew.join(', ') + (names.length ? ' → отмечены: ' + names.join(', ') : ''));
      if (o.crew_missing && o.crew_missing.length) parts.push('не нашёл в сотрудниках: ' + o.crew_missing.join(', ') + ' — отметьте вручную');
    } else {
      parts.push('бригада в сделке не указана — отметьте сборщиков вручную');
    }
    say(parts.join(' · '), false);
  }
  btn.addEventListener('click', function () {
    var n = no.value.trim();
    if (!n) { say('Сначала введите номер договора', true); return; }
    btn.disabled = true; say('Ищу в amoCRM…', false);
    fetch('/api/amocrm/contract?number=' + encodeURIComponent(n), { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        btn.disabled = false;
        if (d.error) { say(d.error + '. Заполните поля вручную.', true); return; }
        if (!d.found) { say('Договор «' + n + '» в amoCRM не найден. Заполните поля вручную.', true); return; }
        if (d.options.length === 1) { apply(d.options[0]); return; }
        var sel = document.createElement('select');
        var first = document.createElement('option'); first.value = ''; first.textContent = '— выберите сделку —';
        sel.appendChild(first);
        d.options.forEach(function (o, i) {
          var op = document.createElement('option');
          op.value = i;
          op.textContent = (o.client_name || '') + ' · ' + (o.deal_name || 'без сделки') + (o.install_date ? ' · установка ' + o.install_date : '');
          sel.appendChild(op);
        });
        sel.addEventListener('change', function () { if (sel.value !== '') apply(d.options[+sel.value]); });
        info.textContent = 'Найдено несколько вариантов: '; info.style.color = '';
        info.appendChild(sel);
      })
      .catch(function () { btn.disabled = false; say('Не удалось связаться с приложением. Заполните поля вручную.', true); });
  });
})();
</script>
{% endblock %}'''


def main():
    main_src = open(MAIN, encoding="utf-8").read()
    tpl_src = open(TPL, encoding="utf-8").read()
    if MARK in main_src:
        print("Патч уже применён, ничего не меняю.")
        return 0
    for name, src, anchor in (("main.py", main_src, ROUTE_ANCHOR), ("new.html: номер договора", tpl_src, INPUT_ANCHOR),
                              ("new.html: конец формы", tpl_src, SCRIPT_ANCHOR)):
        if src.count(anchor) != 1:
            print(f"ОШИБКА: не нашёл ожидаемый кусок кода ({name}). Ничего не изменено.")
            return 1
    try:
        module_src = urllib.request.urlopen(MODULE_URL, timeout=30).read().decode("utf-8")
    except Exception as e:
        print("ОШИБКА: не удалось скачать amocrm.py:", e)
        return 1
    shutil.copy(MAIN, MAIN + ".bak_amo")
    shutil.copy(TPL, TPL + ".bak_amo")
    open(MODULE, "w", encoding="utf-8").write(module_src)
    open(MAIN, "w", encoding="utf-8").write(main_src.replace(ROUTE_ANCHOR, ROUTE_NEW))
    open(TPL, "w", encoding="utf-8").write(tpl_src.replace(INPUT_ANCHOR, INPUT_NEW).replace(SCRIPT_ANCHOR, SCRIPT_NEW))
    try:
        py_compile.compile(MODULE, doraise=True)
        py_compile.compile(MAIN, doraise=True)
    except py_compile.PyCompileError as e:
        shutil.copy(MAIN + ".bak_amo", MAIN)
        shutil.copy(TPL + ".bak_amo", TPL)
        print("ОШИБКА компиляции, вернул прежние файлы:", e)
        return 1
    print("Готово. Перезапусти службу: systemctl restart pretenzii")
    return 0


if __name__ == "__main__":
    sys.exit(main())
