#!/usr/bin/env bash
# Finance Hub newsroom: deploy the latest code. Installed by setup_vps.sh as the root-owned
# /usr/local/sbin/finance-hub-update, which GitHub Actions runs after every push to main.
# By hand on the server:  sudo finance-hub-update
# Pulls from GitHub (if /opt/finance-hub is a git checkout), installs packages,
# applies database changes and grants, runs the tests, then restarts the desk.
set -euo pipefail
exec 9>/run/finance-hub-update.lock
flock -n 9 || { echo "Another update is running."; exit 1; }
APP_DIR="/opt/finance-hub"
APP_USER="finhub"
cd "$APP_DIR"
as_app() { sudo -u "$APP_USER" -H "$@"; }

if [ -d .git ]; then
  echo "== Pulling latest code"
  as_app git pull --ff-only
  as_app git log -1 --format='   now at %h %s (%an, %cr)'
fi
echo "== Packages"
as_app .venv/bin/pip install -q -r requirements.txt
echo "== Tests"
# the tests use their own throwaway SQLite files, never the real database
as_app env -u ADMIN_DATABASE_URL .venv/bin/python -m unittest discover -s tests -q
echo "== Database changes and grants"
as_app .venv/bin/python -m pipeline.cli db-setup --write-env
echo "== Restarting the desk"
systemctl restart finance-hub-desk
sleep 1
curl -fsS -o /dev/null -w "desk answers HTTP %{http_code}\n" http://127.0.0.1:8765/login
if ! cmp -s deploy/update.sh /usr/local/sbin/finance-hub-update; then
  echo "Note: deploy/update.sh changed in the repo. Refresh the installed copy with: sudo bash $APP_DIR/deploy/setup_vps.sh"
fi
