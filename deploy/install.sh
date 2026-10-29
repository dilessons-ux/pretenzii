#!/bin/bash
# Установка «Претензии» на Ubuntu/Debian без Docker: Python venv + systemd + nginx + HTTPS.
# Запуск: sudo bash deploy/install.sh   (из распакованной папки pretenzii)
# Повторный запуск безопасен: обновляет код, .env и база сохраняются.
set -e
DOMAIN="${DOMAIN:-pretenzii.mebel-arigato.ru}"
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@mebel-arigato.ru}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
DST=/opt/pretenzii
LOG=/var/log/pretenzii-install.log
exec > >(tee -a "$LOG") 2>&1
echo "=== Установка $(date) ==="

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3 python3-venv python3-pip nginx certbot python3-certbot-nginx sqlite3 curl dnsutils

id pretenzii >/dev/null 2>&1 || useradd --system --home "$DST" --shell /usr/sbin/nologin pretenzii
mkdir -p "$DST/data/uploads" "$DST/backups"
cp -r "$SRC/app" "$SRC/requirements.txt" "$SRC/deploy" "$SRC/README.md" "$DST/"
[ -f "$DST/.env.example" ] || cp "$SRC/.env.example" "$DST/.env.example"

if [ ! -f "$DST/.env" ]; then
  PW=$(python3 -c "import secrets,string;a=string.ascii_letters+string.digits;print(''.join(secrets.choice(a) for _ in range(12)))")
  sed -e "s|^SECRET_KEY=.*|SECRET_KEY=$(openssl rand -hex 32)|" \
      -e "s|^ADMIN_EMAIL=.*|ADMIN_EMAIL=$ADMIN_EMAIL|" \
      -e "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=$PW|" \
      -e "s|^SMTP_HOST=.*|SMTP_HOST=|" \
      -e "s|^BASE_URL=.*|BASE_URL=https://$DOMAIN|" \
      "$SRC/.env.example" > "$DST/.env"
  echo "DB_PATH=$DST/data/pretenzii.db" >> "$DST/.env"
  echo "UPLOAD_DIR=$DST/data/uploads" >> "$DST/.env"
  echo "TZ_NAME=Europe/Moscow" >> "$DST/.env"
  echo "Логин: $ADMIN_EMAIL  Пароль: $PW" > "$DST/ADMIN_LOGIN.txt"
  chmod 600 "$DST/ADMIN_LOGIN.txt" "$DST/.env"
fi

python3 -m venv "$DST/venv"
"$DST/venv/bin/pip" install -q --upgrade pip
"$DST/venv/bin/pip" install -q -r "$DST/requirements.txt"
chown -R pretenzii:pretenzii "$DST"
chmod 600 "$DST/.env"

cat > /etc/systemd/system/pretenzii.service <<EOF
[Unit]
Description=Pretenzii app
After=network.target

[Service]
User=pretenzii
WorkingDirectory=$DST
EnvironmentFile=$DST/.env
ExecStart=$DST/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips 127.0.0.1
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now pretenzii
systemctl restart pretenzii

sed "s/pretenzii.mebel-arigato.ru/$DOMAIN/" "$DST/deploy/nginx-pretenzii.conf" > /etc/nginx/sites-available/pretenzii
ln -sf /etc/nginx/sites-available/pretenzii /etc/nginx/sites-enabled/pretenzii
nginx -t && systemctl reload nginx

# Бэкап каждую ночь
sed -i "s|cd /opt/pretenzii|cd $DST|" "$DST/deploy/backup.sh"
chmod +x "$DST/deploy/backup.sh"
echo "30 3 * * * root $DST/deploy/backup.sh" > /etc/cron.d/pretenzii-backup

# HTTPS: сразу, если DNS уже указывает сюда; иначе — повтор каждые 10 минут, пока не получится
cat > /usr/local/bin/pretenzii-cert.sh <<EOF
#!/bin/sh
[ -d /etc/letsencrypt/live/$DOMAIN ] && { rm -f /etc/cron.d/pretenzii-cert; exit 0; }
certbot --nginx -d $DOMAIN --non-interactive --agree-tos --register-unsafely-without-email --redirect && rm -f /etc/cron.d/pretenzii-cert
EOF
chmod +x /usr/local/bin/pretenzii-cert.sh
echo "*/10 * * * * root /usr/local/bin/pretenzii-cert.sh >> /var/log/pretenzii-cert.log 2>&1" > /etc/cron.d/pretenzii-cert
/usr/local/bin/pretenzii-cert.sh || echo "HTTPS будет выпущен автоматически, когда DNS обновится"

sleep 2
echo "health: $(curl -s localhost:8000/health)"
echo "IP сервера: $(curl -s -4 ifconfig.me || hostname -I)"
echo "=== ГОТОВО ==="
cat "$DST/ADMIN_LOGIN.txt"
