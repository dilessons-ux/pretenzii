#!/bin/sh
# Ежедневная копия базы и файлов. В cron: 30 3 * * * /opt/pretenzii/deploy/backup.sh
set -e
cd /opt/pretenzii
mkdir -p backups
sqlite3 data/pretenzii.db ".backup backups/db-$(date +%F).sqlite"
tar czf backups/uploads-$(date +%F).tgz -C data uploads
find backups -type f -mtime +30 -delete
