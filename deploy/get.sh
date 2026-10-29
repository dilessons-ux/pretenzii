#!/bin/bash
# Однострочная установка: curl -fsSL https://raw.githubusercontent.com/dilessons-ux/pretenzii/main/deploy/get.sh | sudo bash
set -e
[ "$(id -u)" = 0 ] || { echo "Запустите от root (sudo)"; exit 1; }
command -v curl >/dev/null || (apt-get update -q && apt-get install -y -q curl)
T=$(mktemp -d)
curl -fsSL https://codeload.github.com/dilessons-ux/pretenzii/tar.gz/refs/heads/main | tar xz -C "$T"
bash "$T/pretenzii-main/deploy/install.sh"
