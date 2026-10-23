#!/usr/bin/env bash
# Finance Hub newsroom: one-time server setup (Ubuntu 22.04/24.04). Run as root:
#
#   sudo bash deploy/setup_vps.sh [--domain publish.thebesads.com] [--restore /path/storyhub.dump]
#
# Safe to re-run: every step checks what is already there. It never touches
# other sites, MySQL, RabbitMQ or existing nginx configs; it only adds:
#   - PostgreSQL 17 (listening on this machine only) with database "storyhub"
#   - system user "finhub", code in /opt/finance-hub, data in /var/lib/finance-hub
#   - systemd service "finance-hub-desk" on 127.0.0.1:8765
#   - /etc/cron.d/finance-hub (pipeline schedule every 10 min, nightly backup)
#   - nginx site for the domain, accepting Cloudflare's addresses only
set -euo pipefail

DOMAIN="publish.thebesads.com"
RESTORE=""
APP_DIR="/opt/finance-hub"
DATA_DIR="/var/lib/finance-hub"
BACKUP_DIR="/var/backups/finance-hub"
CERT_DIR="/etc/ssl/finance-hub"
APP_USER="finhub"
DB_NAME="storyhub"
DB_ADMIN="fh_admin"
PORT=8765
PG_MAJOR=17

while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN="$2"; shift 2 ;;
    --restore) RESTORE="$2"; shift 2 ;;
    *) echo "unknown option: $1"; exit 1 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo)."; exit 1; }
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

[ -f "$SRC/requirements.txt" ] && [ -f "$SRC/deploy/finance-hub-desk.service" ] || { echo "Run this from the project folder: sudo bash deploy/setup_vps.sh"; exit 1; }

# ---------------------------------------------------------------- packages
say "Packages"
apt-get update -qq
apt-get install -y -qq curl ca-certificates gnupg git python3-venv python3-pip rsync >/dev/null
if ! command -v "/usr/lib/postgresql/$PG_MAJOR/bin/postgres" >/dev/null 2>&1; then
  install -d /usr/share/postgresql-common/pgdg
  curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
  echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(. /etc/os-release; echo "$VERSION_CODENAME")-pgdg main" \
    > /etc/apt/sources.list.d/pgdg.list
  apt-get update -qq
  apt-get install -y -qq "postgresql-$PG_MAJOR" >/dev/null
fi
# Postgres must only listen on this machine
PGCONF="/etc/postgresql/$PG_MAJOR/main/postgresql.conf"
grep -qE "^listen_addresses\s*=\s*'localhost'" "$PGCONF" || {
  sed -i "s/^#\?listen_addresses\s*=.*/listen_addresses = 'localhost'/" "$PGCONF"
  systemctl restart "postgresql@$PG_MAJOR-main" || systemctl restart postgresql
}
PGPORT="$(sudo -u postgres psql -tAc 'show port')"
echo "PostgreSQL $PG_MAJOR on 127.0.0.1:$PGPORT"

# ---------------------------------------------------------------- user and code
say "App user and code"
id "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --home-dir "/home/$APP_USER" --shell /bin/bash "$APP_USER"
install -d -o "$APP_USER" -g "$APP_USER" -m 750 "$APP_DIR" "$DATA_DIR"
install -d -o postgres -g postgres -m 750 "$BACKUP_DIR"
if [ "$SRC" != "$APP_DIR" ] && [ ! -d "$APP_DIR/.git" ] && [ ! -f "$APP_DIR/requirements.txt" ]; then
  # first install from a checkout or upload elsewhere: copy it in (keeps .git for later pulls)
  rsync -a --exclude .venv --exclude .env --exclude data --exclude logs "$SRC/" "$APP_DIR/"
  chown -R "$APP_USER:$APP_USER" "$APP_DIR"
fi
cd "$APP_DIR"
sudo -u "$APP_USER" python3 -m venv .venv
sudo -u "$APP_USER" .venv/bin/pip install -q --upgrade pip
sudo -u "$APP_USER" .venv/bin/pip install -q -r requirements.txt

# ---------------------------------------------------------------- database
say "Database"
ENV_FILE="$APP_DIR/.env"
touch "$ENV_FILE"; chown "$APP_USER:$APP_USER" "$ENV_FILE"; chmod 600 "$ENV_FILE"
getenv() { grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- || true; }
setenv() {
  if grep -qE "^$1=" "$ENV_FILE"; then sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"; else echo "$1=$2" >> "$ENV_FILE"; fi
}
ADMIN_URL="$(getenv ADMIN_DATABASE_URL)"
if [ -z "$ADMIN_URL" ]; then
  ADMIN_PW="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
  ADMIN_URL="postgresql://$DB_ADMIN:$ADMIN_PW@127.0.0.1:$PGPORT/$DB_NAME"
fi
ADMIN_PW="$(python3 -c "import sys,urllib.parse as u; print(u.urlsplit(sys.argv[1]).password)" "$ADMIN_URL")"
sudo -u postgres psql -v ON_ERROR_STOP=1 -q <<SQL
do \$\$ begin
  if not exists (select 1 from pg_roles where rolname = '$DB_ADMIN') then
    create role $DB_ADMIN login createrole password '$ADMIN_PW';
  else
    alter role $DB_ADMIN login createrole password '$ADMIN_PW';
  end if;
end \$\$;
SQL
sudo -u postgres psql -tAc "select 1 from pg_database where datname = '$DB_NAME'" | grep -q 1 \
  || sudo -u postgres createdb -O "$DB_ADMIN" "$DB_NAME"
sudo -u postgres psql -q -d "$DB_NAME" -c "grant all on database $DB_NAME to $DB_ADMIN with grant option; alter schema public owner to $DB_ADMIN;"

if [ -n "$RESTORE" ]; then
  TABLES="$(sudo -u postgres psql -tAd "$DB_NAME" -c "select count(*) from information_schema.tables where table_schema in ('pipeline','newsroom','site')")"
  if [ "$TABLES" != "0" ]; then
    echo "Database already has Finance Hub tables; not restoring over them."
  else
    echo "Restoring $RESTORE ..."
    PGPASSWORD="$ADMIN_PW" pg_restore -h 127.0.0.1 -p "$PGPORT" -U "$DB_ADMIN" -d "$DB_NAME" \
      --no-owner --no-privileges "$RESTORE" \
      || echo "!! pg_restore reported problems (listed above). Check them before using the desk."
  fi
fi

# ---------------------------------------------------------------- .env
say "Settings (.env)"
setenv ADMIN_DATABASE_URL "$ADMIN_URL"
setenv APP_ENV production
setenv DESK_BIND 127.0.0.1
setenv DESK_HOSTS "$DOMAIN"
setenv DESK_TRUST_PROXY 1
setenv FINANCE_DATA_DIR "$DATA_DIR"
grep -qE '^LLM_PROVIDER=' "$ENV_FILE" || setenv LLM_PROVIDER gemini
if [ -z "$(getenv GEMINI_API_KEY)" ]; then
  if [ -t 0 ]; then
    read -rsp "Gemini API key (input hidden, Enter to skip for now): " KEY; echo
    [ -n "$KEY" ] && setenv GEMINI_API_KEY "$KEY"
  fi
  [ -n "$(getenv GEMINI_API_KEY)" ] || echo "!! No GEMINI_API_KEY yet: add it to $ENV_FILE before the first pull."
fi
# creates the schemas, the fh_pipeline/fh_newsroom/fh_site logins and their grants; saves their URLs into .env
sudo -u "$APP_USER" .venv/bin/python -m pipeline.cli db-setup --write-env

# ---------------------------------------------------------------- service, schedule, backups
say "Desk service, schedule and backups"
install -m 644 deploy/finance-hub-desk.service /etc/systemd/system/finance-hub-desk.service
systemctl daemon-reload
systemctl enable -q finance-hub-desk
systemctl restart finance-hub-desk
install -m 755 deploy/backup.sh /usr/local/sbin/finance-hub-backup
cat > /etc/cron.d/finance-hub <<CRON
# Finance Hub newsroom (installed by deploy/setup_vps.sh)
SHELL=/bin/bash
# pipeline schedule: each region runs once a day at 12:00 local time (New York, London, Sydney)
*/10 * * * * $APP_USER cd $APP_DIR && .venv/bin/python -m newsroom tick >> $DATA_DIR/tick.log 2>&1
# nightly database backup, keeps 14 days
30 2 * * * root /usr/local/sbin/finance-hub-backup
CRON
chmod 644 /etc/cron.d/finance-hub
cat > /etc/logrotate.d/finance-hub <<ROT
$DATA_DIR/*.log $DATA_DIR/logs/*.log {
  weekly
  rotate 8
  compress
  missingok
  notifempty
  copytruncate
}
ROT

# ---------------------------------------------------------------- automatic deploys from GitHub Actions
say "Automatic deploys (GitHub Actions -> this server)"
# The update script runs as root from a root-owned copy, so a code push can't change what runs as root;
# the app code itself only ever runs as $APP_USER.
install -m 755 -o root -g root deploy/update.sh /usr/local/sbin/finance-hub-update
DEPLOY_USER=fhdeploy
id "$DEPLOY_USER" >/dev/null 2>&1 || useradd --system --create-home --home-dir "/home/$DEPLOY_USER" --shell /bin/bash "$DEPLOY_USER"
echo "$DEPLOY_USER ALL=(root) NOPASSWD: /usr/local/sbin/finance-hub-update" > /etc/sudoers.d/finance-hub-deploy
chmod 440 /etc/sudoers.d/finance-hub-deploy
visudo -cf /etc/sudoers.d/finance-hub-deploy >/dev/null
KEYDIR=/root/finance-hub-actions-key
AK="/home/$DEPLOY_USER/.ssh/authorized_keys"
install -d -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 700 "/home/$DEPLOY_USER/.ssh"
NEW_ACTIONS_KEY=0
if [ ! -s "$AK" ]; then
  install -d -m 700 "$KEYDIR"
  ssh-keygen -q -t ed25519 -N "" -C "github-actions deploy thebes-publish" -f "$KEYDIR/id_ed25519"
  # this key can only run the update script: no shell, no forwarding, no terminal
  echo "restrict,command=\"sudo /usr/local/sbin/finance-hub-update\" $(cat "$KEYDIR/id_ed25519.pub")" > "$AK"
  chown "$DEPLOY_USER:$DEPLOY_USER" "$AK"; chmod 600 "$AK"
  NEW_ACTIONS_KEY=1
fi
if grep -qiE '^\s*AllowUsers' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf 2>/dev/null \
   && ! grep -qiE "^\s*AllowUsers.*\b$DEPLOY_USER\b" /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf 2>/dev/null; then
  echo "!! sshd has an AllowUsers list without $DEPLOY_USER: add it, or GitHub Actions can't connect."
fi

# ---------------------------------------------------------------- nginx (Cloudflare only)
say "nginx"
SNIP=/etc/nginx/snippets/finance-hub-cloudflare-only.conf
{
  echo "# Cloudflare's addresses (refreshed by deploy/setup_vps.sh). Everything else is refused."
  for list in ips-v4 ips-v6; do
    curl -fsSL "https://www.cloudflare.com/$list" | sed '/^\s*$/d; s/.*/allow &;/'
  done
  echo "allow 127.0.0.1;"
  echo "deny all;"
} > "$SNIP.new"
if grep -q '^allow .*;' "$SNIP.new"; then mv "$SNIP.new" "$SNIP"; else rm -f "$SNIP.new"; echo "!! Could not download Cloudflare's address list; keeping the old one."; fi
install -d -m 755 "$CERT_DIR"
SITE=/etc/nginx/sites-available/finance-hub
sed -e "s/__DOMAIN__/$DOMAIN/g" -e "s/__PORT__/$PORT/g" -e "s#__CERT_DIR__#$CERT_DIR#g" deploy/nginx-finance-hub.conf > "$SITE"
if [ -s "$CERT_DIR/origin.pem" ] && [ -s "$CERT_DIR/origin.key" ]; then
  chmod 600 "$CERT_DIR/origin.key"
  ln -sf "$SITE" /etc/nginx/sites-enabled/finance-hub
  nginx -t && systemctl reload nginx
  NGINX_STATE="enabled for https://$DOMAIN"
else
  rm -f /etc/nginx/sites-enabled/finance-hub
  NGINX_STATE="NOT enabled yet: save the Cloudflare origin certificate as $CERT_DIR/origin.pem and its key as $CERT_DIR/origin.key, then re-run this script"
fi

# ---------------------------------------------------------------- summary
say "Done"
sleep 1
systemctl --no-pager --lines=3 status finance-hub-desk | sed -n '1,3p' || true
curl -fsS -o /dev/null -w "desk on 127.0.0.1:$PORT answers HTTP %{http_code}\n" "http://127.0.0.1:$PORT/login" || echo "!! desk not answering yet: journalctl -u finance-hub-desk -n 50"
cat <<NEXT

nginx: $NGINX_STATE
Database: $DB_NAME on 127.0.0.1:$PGPORT (local only). Backups: $BACKUP_DIR (nightly, 14 days).

Next:
  - No admin yet?   sudo -u $APP_USER -H bash -c 'cd $APP_DIR && .venv/bin/python -m newsroom create-admin'
  - Auto-publish byline: set AUTOPUBLISH_RESPONSIBLE_EDITOR=<your email> in $ENV_FILE,
    then: systemctl restart finance-hub-desk
  - Updates later:  sudo bash $APP_DIR/deploy/update.sh
See deploy/README.md for the Cloudflare steps.
NEXT
if [ "$NEW_ACTIONS_KEY" = 1 ]; then
  cat <<KEYS

==================== GitHub Actions secrets (one-time) ====================
In GitHub: adeshsarwan/thebes-publish > Settings > Secrets and variables > Actions > New repository secret

DEPLOY_HOST         = the server's IP, e.g. 172.236.151.195
DEPLOY_KNOWN_HOSTS  = everything between the lines below:
-----
$(for k in /etc/ssh/ssh_host_*_key.pub; do echo "$(hostname -I | awk '{print $1}') $(cut -d' ' -f1,2 "$k")"; done)
-----
DEPLOY_SSH_KEY      = everything between the lines below (a private key: paste it only into GitHub):
-----
$(cat "$KEYDIR/id_ed25519")
-----
After saving the secrets, delete the server copy:  rm -rf $KEYDIR
===========================================================================
KEYS
fi
