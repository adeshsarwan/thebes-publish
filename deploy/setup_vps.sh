#!/usr/bin/env bash
# Story Hub newsroom: one-time server setup (Ubuntu 22.04/24.04). Run as root:
#
#   sudo bash deploy/setup_vps.sh [--letsencrypt you@example.com] [--domain publish.thebesads.com] [--restore /path/storyhub.dump]
#
# Safe to re-run: every step checks what is already there. It never touches
# other sites, MySQL, RabbitMQ or existing nginx configs; it only adds:
#   - PostgreSQL 17 (listening on this machine only) with database "storyhub"
#   - system user "storyhub", code in /opt/story-hub, data in /var/lib/story-hub
#   - systemd service "story-hub-desk" on 127.0.0.1:8765
#   - systemd service "story-hub-api" (read-only Stories API) on 127.0.0.1:8767
#   - /etc/cron.d/story-hub (pipeline schedule every 10 min, nightly backup)
#   - nginx site for the domain, accepting Cloudflare's addresses only, with a Let's Encrypt
#     certificate (certbot renews it and reloads nginx automatically)
set -euo pipefail

DOMAIN="publish.thebesads.com"
API_DOMAIN="storyhub-api.thebesads.com"   # the Stories API for website developers (no Cloudflare Access)
API_PORT=8767
RESTORE=""
LE_EMAIL=""
APP_DIR="/opt/story-hub"
DATA_DIR="/var/lib/story-hub"
BACKUP_DIR="/var/backups/story-hub"
CERT_DIR="/etc/ssl/story-hub"
APP_USER="storyhub"
DB_NAME="storyhub"
DB_ADMIN="story_admin"
PORT=8765
PG_MAJOR=17

while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN="$2"; shift 2 ;;
    --api-domain) API_DOMAIN="$2"; shift 2 ;;
    --restore) RESTORE="$2"; shift 2 ;;
    --letsencrypt) LE_EMAIL="$2"; shift 2 ;;
    *) echo "unknown option: $1"; exit 1 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo)."; exit 1; }
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

[ -f "$SRC/requirements.txt" ] && [ -f "$SRC/deploy/story-hub-desk.service" ] || { echo "Run this from the project folder: sudo bash deploy/setup_vps.sh"; exit 1; }

# ---------------------------------------------------------------- packages
say "Packages"
# Other repositories configured on this server may be broken or retired; that shouldn't stop us.
apt-get update -qq || echo "!! apt-get update reported problems with other repositories (above). Continuing; they aren't needed here."
apt-get install -y -qq curl ca-certificates gnupg git python3-venv python3-pip rsync >/dev/null
if ! command -v "/usr/lib/postgresql/$PG_MAJOR/bin/postgres" >/dev/null 2>&1; then
  install -d /usr/share/postgresql-common/pgdg
  curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
  echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(. /etc/os-release; echo "$VERSION_CODENAME")-pgdg main" \
    > /etc/apt/sources.list.d/pgdg.list
  # refresh only the PostgreSQL repository, and require that one to work
  apt-get update -qq -o Dir::Etc::sourcelist=/etc/apt/sources.list.d/pgdg.list \
    -o Dir::Etc::sourceparts=- -o APT::Get::List-Cleanup=0
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
    echo "Database already has Story Hub tables; not restoring over them."
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
setenv STORY_API_URL "https://$API_DOMAIN"
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
# creates the schemas, the story_pipeline/story_newsroom/story_site logins and their grants; saves their URLs into .env
sudo -u "$APP_USER" .venv/bin/python -m pipeline.cli db-setup --write-env

# ---------------------------------------------------------------- service, schedule, backups
say "Desk service, schedule and backups"
install -m 644 deploy/story-hub-desk.service /etc/systemd/system/story-hub-desk.service
systemctl daemon-reload
systemctl enable -q story-hub-desk
systemctl restart story-hub-desk
install -m 644 deploy/story-hub-api.service /etc/systemd/system/story-hub-api.service
systemctl daemon-reload
systemctl enable -q story-hub-api
systemctl restart story-hub-api
install -m 755 deploy/backup.sh /usr/local/sbin/story-hub-backup
cat > /etc/cron.d/story-hub <<CRON
# Story Hub newsroom (installed by deploy/setup_vps.sh)
SHELL=/bin/bash
# pipeline schedule: each region runs once a day at 12:00 local time (New York, London, Sydney)
*/10 * * * * $APP_USER cd $APP_DIR && .venv/bin/python -m newsroom tick >> $DATA_DIR/tick.log 2>&1
# nightly database backup, keeps 14 days
30 2 * * * root /usr/local/sbin/story-hub-backup
CRON
chmod 644 /etc/cron.d/story-hub
cat > /etc/logrotate.d/story-hub <<ROT
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
install -m 755 -o root -g root deploy/update.sh /usr/local/sbin/story-hub-update
DEPLOY_USER=storydeploy
id "$DEPLOY_USER" >/dev/null 2>&1 || useradd --system --create-home --home-dir "/home/$DEPLOY_USER" --shell /bin/bash "$DEPLOY_USER"
echo "$DEPLOY_USER ALL=(root) NOPASSWD: /usr/local/sbin/story-hub-update" > /etc/sudoers.d/story-hub-deploy
chmod 440 /etc/sudoers.d/story-hub-deploy
visudo -cf /etc/sudoers.d/story-hub-deploy >/dev/null
KEYDIR=/root/story-hub-actions-key
AK="/home/$DEPLOY_USER/.ssh/authorized_keys"
install -d -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 700 "/home/$DEPLOY_USER/.ssh"
NEW_ACTIONS_KEY=0
if [ ! -s "$AK" ]; then
  install -d -m 700 "$KEYDIR"
  ssh-keygen -q -t ed25519 -N "" -C "github-actions deploy thebes-publish" -f "$KEYDIR/id_ed25519"
  # this key can only run the update script: no shell, no forwarding, no terminal
  echo "restrict,command=\"sudo /usr/local/sbin/story-hub-update\" $(cat "$KEYDIR/id_ed25519.pub")" > "$AK"
  chown "$DEPLOY_USER:$DEPLOY_USER" "$AK"; chmod 600 "$AK"
  NEW_ACTIONS_KEY=1
fi
if grep -qiE '^\s*AllowUsers' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf 2>/dev/null \
   && ! grep -qiE "^\s*AllowUsers.*\b$DEPLOY_USER\b" /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf 2>/dev/null; then
  echo "!! sshd has an AllowUsers list without $DEPLOY_USER: add it, or GitHub Actions can't connect."
fi

# ---------------------------------------------------------------- nginx (Cloudflare only)
say "nginx"
SNIP=/etc/nginx/snippets/story-hub-cloudflare-only.conf
{
  echo "# Cloudflare's addresses (refreshed by deploy/setup_vps.sh). Everything else is refused."
  for list in ips-v4 ips-v6; do
    curl -fsSL "https://www.cloudflare.com/$list" | sed '/^\s*$/d; s/.*/allow &;/'
  done
  echo "allow 127.0.0.1;"
  echo "deny all;"
} > "$SNIP.new"
if grep -q '^allow .*;' "$SNIP.new"; then mv "$SNIP.new" "$SNIP"; else rm -f "$SNIP.new"; echo "!! Could not download Cloudflare's address list; keeping the old one."; fi
install -d -m 755 "$CERT_DIR" /var/www/letsencrypt

# site_for NAME DOMAIN PORT ORIGIN_CERT_BASENAME  ->  sets SITE_STATE
site_for() {
  local name="$1" domain="$2" port="$3" origin="$4"
  local site="/etc/nginx/sites-available/$name" le="/etc/letsencrypt/live/$domain" cert="" key="" kind=""
  render() { sed -e "s/__DOMAIN__/$domain/g" -e "s/__PORT__/$port/g" -e "s#__CERT__#$1#g" -e "s#__KEY__#$2#g" "$3"; }
  # 1. port 80 first: needed for the Let's Encrypt check, redirects everything else to HTTPS
  render "" "" deploy/nginx-story-hub-http.conf > "$site"
  ln -sf "$site" "/etc/nginx/sites-enabled/$name"
  nginx -t && systemctl reload nginx
  # 2. certificate
  if [ -n "$LE_EMAIL" ] && [ ! -s "$le/fullchain.pem" ]; then
    command -v certbot >/dev/null || apt-get install -y -qq certbot >/dev/null
    echo "Requesting a Let's Encrypt certificate for $domain ..."
    certbot certonly --webroot -w /var/www/letsencrypt -d "$domain" --email "$LE_EMAIL" --agree-tos \
      --non-interactive --deploy-hook "systemctl reload nginx" \
      || echo "!! Let's Encrypt could not verify $domain. See 'Certificate' in deploy/README.md (usually Cloudflare's 'Always Use HTTPS')."
  fi
  if [ -s "$le/fullchain.pem" ]; then
    cert="$le/fullchain.pem"; key="$le/privkey.pem"; kind="Let's Encrypt (renews automatically)"
  elif [ -s "$CERT_DIR/$origin.pem" ] && [ -s "$CERT_DIR/$origin.key" ]; then
    chmod 600 "$CERT_DIR/$origin.key"
    cert="$CERT_DIR/$origin.pem"; key="$CERT_DIR/$origin.key"; kind="Cloudflare origin certificate"
  fi
  # 3. HTTPS once there is a certificate
  if [ -n "$cert" ]; then
    { render "" "" deploy/nginx-story-hub-http.conf; echo; render "$cert" "$key" deploy/nginx-story-hub.conf; } > "$site"
    nginx -t && systemctl reload nginx
    SITE_STATE="https://$domain is live ($kind)"
  else
    SITE_STATE="$domain: HTTP only, no certificate yet: run  sudo bash deploy/setup_vps.sh --letsencrypt you@example.com"
  fi
}
site_for story-hub "$DOMAIN" "$PORT" origin
NGINX_STATE="$SITE_STATE"
site_for story-hub-api "$API_DOMAIN" "$API_PORT" api-origin
API_STATE="$SITE_STATE"

# ---------------------------------------------------------------- summary
say "Done"
sleep 1
systemctl --no-pager --lines=3 status story-hub-desk | sed -n '1,3p' || true
curl -fsS -o /dev/null -w "desk on 127.0.0.1:$PORT answers HTTP %{http_code}\n" "http://127.0.0.1:$PORT/login" || echo "!! desk not answering yet: journalctl -u story-hub-desk -n 50"
curl -fsS -o /dev/null -w "Stories API on 127.0.0.1:$API_PORT answers HTTP %{http_code}\n" "http://127.0.0.1:$API_PORT/v1" || echo "!! API not answering yet: journalctl -u story-hub-api -n 50"
cat <<NEXT

Desk: $NGINX_STATE
Stories API: $API_STATE  (keys: desk > API page)
Database: $DB_NAME on 127.0.0.1:$PGPORT (local only). Backups: $BACKUP_DIR (nightly, 14 days).

Next:
  - No admin yet?   sudo -u $APP_USER -H bash -c 'cd $APP_DIR && .venv/bin/python -m newsroom create-admin'
  - Auto-publish byline: set AUTOPUBLISH_RESPONSIBLE_EDITOR=<your email> in $ENV_FILE,
    then: systemctl restart story-hub-desk
  - Updates: automatic on every push to main, or by hand:  sudo story-hub-update
See deploy/README.md for the Cloudflare steps.
NEXT
if [ "$NEW_ACTIONS_KEY" = 1 ]; then
  cat <<KEYS

==================== GitHub Actions secrets (one-time) ====================
In GitHub: adeshsarwan/thebes-publish > Settings > Secrets and variables > Actions > New repository secret

DEPLOY_HOST         = the server's IP, e.g. 172.105.48.107
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
