#!/bin/bash
# Подключение «Претензий» к уже работающему Caddy в Docker (без остановки других сайтов).
# Запуск: curl -fsSL https://raw.githubusercontent.com/dilessons-ux/pretenzii/main/deploy/caddy.sh | bash
set -e
DOMAIN="${DOMAIN:-pretenzii.mebel-arigato.ru}"
C=$(docker ps --format '{{.Names}} {{.Image}}' | awk '$2 ~ /^caddy/ {print $1; exit}')
[ -n "$C" ] || { echo "Контейнер Caddy не найден"; exit 1; }
echo "Caddy: $C"

# Адрес хоста, видимый из сети контейнера Caddy
GW=$(docker inspect "$C" -f '{{range .NetworkSettings.Networks}}{{.Gateway}} {{end}}' | awk '{print $1}')
[ -n "$GW" ] || { echo "Не найден шлюз сети Caddy"; exit 1; }
echo "Приложение будет слушать $GW:8000 (доступно только с сервера и из Docker)"

# Где лежит Caddyfile на хосте
CF=$(docker inspect "$C" -f '{{range .Mounts}}{{if eq .Destination "/etc/caddy/Caddyfile"}}{{.Source}}{{end}}{{end}}')
if [ -z "$CF" ]; then
  D=$(docker inspect "$C" -f '{{range .Mounts}}{{if eq .Destination "/etc/caddy"}}{{.Source}}{{end}}{{end}}')
  [ -n "$D" ] && CF="$D/Caddyfile"
fi
[ -n "$CF" ] && [ -f "$CF" ] || { echo "Caddyfile не найден среди подключённых файлов контейнера"; exit 1; }
echo "Caddyfile: $CF"

# 1. Приложение слушает шлюз Docker
sed -i "s|--host [0-9.]* --port 8000|--host $GW --port 8000|; s|--forwarded-allow-ips [^ ]*|--forwarded-allow-ips '*'|" /etc/systemd/system/pretenzii.service
grep -q "^After=.*docker" /etc/systemd/system/pretenzii.service || sed -i "s|^After=network.target|After=network.target docker.service|" /etc/systemd/system/pretenzii.service
systemctl daemon-reload && systemctl restart pretenzii
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow from 172.16.0.0/12 to any port 8000 proto tcp >/dev/null && echo "ufw: разрешён доступ Docker → 8000"
fi
sleep 2

# 2. Блок сайта в Caddyfile (с резервной копией и проверкой)
if grep -q "^$DOMAIN" "$CF"; then
  echo "Блок $DOMAIN уже есть в Caddyfile"
else
  cp "$CF" "$CF.bak-$(date +%Y%m%d%H%M%S)"
  printf '\n%s {\n\treverse_proxy %s:8000\n}\n' "$DOMAIN" "$GW" >> "$CF"
fi
if ! docker exec "$C" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1; then
  B=$(ls -t "$CF".bak-* 2>/dev/null | head -1)
  [ -n "$B" ] && cp "$B" "$CF"
  echo "ОШИБКА: Caddyfile не прошёл проверку — изменения откатил, бот не затронут"; exit 1
fi
docker exec "$C" caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile

# 3. Наш certbot не нужен — сертификат выпускает Caddy
rm -f /etc/cron.d/pretenzii-cert

echo "Проверка из Caddy: $(docker exec "$C" wget -qO- http://$GW:8000/health 2>&1)"
echo "DNS $DOMAIN -> $(dig +short $DOMAIN | tail -1)   (нужно: $(curl -s -4 ifconfig.me))"
echo "=== ГОТОВО ==="
cat /opt/pretenzii/ADMIN_LOGIN.txt
