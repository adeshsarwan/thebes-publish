#!/usr/bin/env bash
# Story Hub newsroom: deploy the latest code. Installed by setup_vps.sh as the root-owned
# /usr/local/sbin/story-hub-update, which GitHub Actions runs after every push to main.
# By hand on the server:  sudo story-hub-update
# Pulls from GitHub (if /opt/story-hub is a git checkout), installs packages,
# applies database changes and grants, runs the tests, then restarts the desk.
set -euo pipefail
exec 9>/run/story-hub-update.lock
flock -n 9 || { echo "Another update is running."; exit 1; }
APP_DIR="/opt/story-hub"
APP_USER="storyhub"
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
systemctl restart story-hub-desk
if systemctl list-unit-files story-hub-api.service >/dev/null 2>&1 && [ -f /etc/systemd/system/story-hub-api.service ]; then
  systemctl restart story-hub-api
fi
sleep 1
curl -fsS -o /dev/null -w "desk answers HTTP %{http_code}\n" http://127.0.0.1:8765/login
if [ -f /etc/systemd/system/story-hub-api.service ]; then
  curl -fsS -o /dev/null -w "Stories API answers HTTP %{http_code}\n" http://127.0.0.1:8767/v1
else
  echo "Note: the Stories API isn't installed yet. Run: sudo bash $APP_DIR/deploy/setup_vps.sh --letsencrypt you@example.com"
fi
if ! cmp -s deploy/update.sh /usr/local/sbin/story-hub-update; then
  echo "Note: deploy/update.sh changed in the repo. Refresh the installed copy with: sudo bash $APP_DIR/deploy/setup_vps.sh"
fi
