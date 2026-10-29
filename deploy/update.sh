#!/bin/bash
# Обновление кода на сервере. База, файлы и .env не трогаются.
# Запуск: curl -fsSL https://raw.githubusercontent.com/dilessons-ux/pretenzii/main/deploy/update.sh | bash
set -e
DST=/opt/pretenzii
T=$(mktemp -d)
curl -fsSL https://codeload.github.com/dilessons-ux/pretenzii/tar.gz/refs/heads/main | tar xz -C "$T"
S="$T/pretenzii-main"
sqlite3 "$DST/data/pretenzii.db" ".backup $DST/backups/before-update-$(date +%Y%m%d%H%M%S).sqlite" 2>/dev/null || true
rm -rf "$DST/app" && cp -r "$S/app" "$S/requirements.txt" "$S/README.md" "$DST/"
cp -r "$S/deploy" "$DST/"
"$DST/venv/bin/pip" install -q -r "$DST/requirements.txt"
chown -R pretenzii:pretenzii "$DST"
systemctl restart pretenzii
sleep 3
H=$(grep -oE -- '--host [0-9.]+' /etc/systemd/system/pretenzii.service | awk '{print $2}')
echo "health: $(curl -s http://${H:-127.0.0.1}:8000/health)"
echo "=== ОБНОВЛЕНО ==="
