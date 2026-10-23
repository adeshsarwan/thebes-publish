#!/usr/bin/env bash
# Nightly Finance Hub database backup (installed as /usr/local/sbin/finance-hub-backup, run by cron).
# Keeps 14 days in /var/backups/finance-hub. Copy these off the server too
# (Linode Backups, or sync the folder to object storage).
set -euo pipefail
DIR=/var/backups/finance-hub
install -d -o postgres -g postgres -m 750 "$DIR"
FILE="$DIR/storyhub-$(date +%F).dump"
sudo -u postgres pg_dump -Fc -d storyhub -f "$FILE.tmp"
mv "$FILE.tmp" "$FILE"
find "$DIR" -name 'storyhub-*.dump' -mtime +14 -delete
