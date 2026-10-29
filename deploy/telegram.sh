#!/bin/bash
# Подключение Telegram-бота. Токен вводится здесь, в чат его отправлять не нужно.
# Запуск: bash <(curl -fsSL https://raw.githubusercontent.com/dilessons-ux/pretenzii/main/deploy/telegram.sh)
set -e
ENV=/opt/pretenzii/.env
read -rp "Токен бота от @BotFather: " TOKEN
TOKEN=$(echo "$TOKEN" | tr -d '[:space:]')
R=$(curl -s "https://api.telegram.org/bot$TOKEN/getMe")
echo "$R" | grep -q '"ok":true' || { echo "Telegram не принял токен: $R"; exit 1; }
NAME=$(echo "$R" | sed -E 's/.*"username":"([^"]+)".*/\1/')
sed -i -e "s|^TELEGRAM_BOT_TOKEN=.*|TELEGRAM_BOT_TOKEN=$TOKEN|" -e "s|^TELEGRAM_BOT_NAME=.*|TELEGRAM_BOT_NAME=$NAME|" "$ENV"
grep -q "^TELEGRAM_BOT_TOKEN=" "$ENV" || echo "TELEGRAM_BOT_TOKEN=$TOKEN" >> "$ENV"
grep -q "^TELEGRAM_BOT_NAME=" "$ENV" || echo "TELEGRAM_BOT_NAME=$NAME" >> "$ENV"
systemctl restart pretenzii
echo "=== Бот @$NAME подключён ==="
