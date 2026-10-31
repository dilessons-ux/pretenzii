#!/usr/bin/env python3
"""Сверка кода на сервере с репозиторием GitHub: печатает только отличия (только чтение).

Запуск на сервере:
  curl -fsSL https://raw.githubusercontent.com/dilessons-ux/pretenzii/main/deploy/sync_diff.py -o /tmp/sync_diff.py \
    && /opt/pretenzii/venv/bin/python /tmp/sync_diff.py | tee /tmp/server_vs_repo.diff

Сравниваются app/, deploy/, requirements.txt, README.md. Не читаются и не печатаются:
.env, data/, backups/, venv/, *.bak*, ADMIN_LOGIN.txt. Ничего на сервере не меняется.
"""
import difflib
import os
import re
import sys
import urllib.error
import urllib.request

ROOT = os.getenv("SYNC_ROOT", "/opt/pretenzii")
RAW = os.getenv("SYNC_RAW", "https://raw.githubusercontent.com/dilessons-ux/pretenzii/main/")
TOP = ("app", "deploy")
TOP_FILES = ("requirements.txt", "README.md")
SKIP_PARTS = ("__pycache__",)
SKIP_SUFFIX = (".pyc", ".bak")
MAX_SIZE = 300_000
SECRET_RE = re.compile(r"[A-Za-z0-9:_\-]{32,}")


def server_files():
    out = []
    for name in TOP_FILES:
        if os.path.isfile(os.path.join(ROOT, name)):
            out.append(name)
    for top in TOP:
        for dp, dns, fns in os.walk(os.path.join(ROOT, top)):
            dns[:] = [d for d in dns if d not in SKIP_PARTS]
            for fn in fns:
                if ".bak" in fn or fn.endswith(SKIP_SUFFIX):
                    continue
                out.append(os.path.relpath(os.path.join(dp, fn), ROOT))
    return sorted(out)


def repo_text(rel):
    try:
        with urllib.request.urlopen(RAW + rel, timeout=30) as r:
            return r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def main():
    changed, same, skipped, warn = 0, 0, [], []
    chunks = []
    for rel in server_files():
        path = os.path.join(ROOT, rel)
        if os.path.getsize(path) > MAX_SIZE:
            skipped.append(f"{rel} (слишком большой)")
            continue
        try:
            srv = open(path, encoding="utf-8").read()
        except UnicodeDecodeError:
            skipped.append(f"{rel} (не текст)")
            continue
        repo = repo_text(rel)
        if repo == srv:
            same += 1
            continue
        changed += 1
        a = [] if repo is None else repo.splitlines(keepends=True)
        b = srv.splitlines(keepends=True)
        diff = list(difflib.unified_diff(a, b, "/dev/null" if repo is None else f"a/{rel}", f"b/{rel}"))
        for line in diff:
            if line.startswith("+") and not line.startswith("+++") and SECRET_RE.search(line):
                warn.append(f"{rel}: {line.strip()[:90]}")
        chunks.append("".join(l if l.endswith("\n") else l + "\n" for l in diff))
    print(f"### Файлов совпадает: {same}, с отличиями: {changed}, пропущено: {len(skipped)}")
    for s in skipped:
        print("### пропущен:", s)
    if warn:
        print("### ВНИМАНИЕ: в добавленных строках есть похожее на ключ — проверь перед отправкой:")
        for w in warn:
            print("###  ", w)
    print("".join(chunks), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
