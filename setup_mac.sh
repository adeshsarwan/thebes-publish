#!/usr/bin/env bash
# Story Hub: one-shot local setup + health check for macOS. Safe to re-run.
#   cd ~/Projects/finance-ingest && bash setup_mac.sh
#
# 1. Python virtualenv + dependencies
# 2. PostgreSQL (Homebrew) running on localhost, database "financehub"
# 3. Schemas pipeline / newsroom / site + login roles (passwords saved in .env)
# 4. Placeholder newsroom staff, your admin login, and a one-time import of the old SQLite data
# 5. The daily schedule (noon New York, London and Sydney time)
# 6. Tests on SQLite and on your local Postgres
set -euo pipefail
cd "$(dirname "$0")"
say() { printf "\n\033[1m==> %s\033[0m\n" "$*"; }
PG_FORMULA="postgresql@17"
DB_NAME="financehub"

say "Python"
PY=$(command -v python3 || true)
[ -z "$PY" ] && { echo "python3 not found. Install it with: brew install python"; exit 1; }
"$PY" -c 'import sys; assert sys.version_info >= (3, 9), "need Python 3.9+"; print(sys.version.split()[0])'
[ -d .venv ] || "$PY" -m venv .venv
source .venv/bin/activate
python -m pip install -q --upgrade pip
pip install -q -r requirements.txt
python -c "import feedparser, psycopg; print('feedparser', feedparser.__version__, '| psycopg', psycopg.__version__)"

say "PostgreSQL"
if command -v brew >/dev/null; then
  PG_BIN="$(brew --prefix)/opt/$PG_FORMULA/bin"
else
  PG_BIN=""
fi
export PATH="${PG_BIN:+$PG_BIN:}$PATH"
if pg_isready -q -h localhost 2>/dev/null; then
  echo "A PostgreSQL server is already running on localhost:5432; using it."
else
  command -v brew >/dev/null || { echo "Homebrew not found. Install PostgreSQL yourself, start it, and re-run."; exit 1; }
  if ! brew list "$PG_FORMULA" >/dev/null 2>&1; then
    echo "Installing $PG_FORMULA with Homebrew (one time)..."
    brew install "$PG_FORMULA"
  fi
  brew services start "$PG_FORMULA"
  for _ in $(seq 1 30); do pg_isready -q -h localhost && break; sleep 1; done
  pg_isready -h localhost
fi
if ! psql -h localhost -d postgres -tAc "select 1 from pg_database where datname='$DB_NAME'" | grep -q 1; then
  createdb -h localhost "$DB_NAME"
  echo "Created database $DB_NAME"
else
  echo "Database $DB_NAME exists"
fi

say ".env"
[ -f .env ] || cp .env.example .env
grep -qE '^ADMIN_DATABASE_URL=.+' .env || echo "ADMIN_DATABASE_URL=postgresql://localhost/$DB_NAME" >> .env
grep -qE '^APP_ENV=' .env || echo "APP_ENV=local" >> .env

say "Schemas, tables and roles"
python -m pipeline.cli db-setup --write-env

say "Newsroom staff (placeholders for local testing)"
python -m newsroom seed-users

if ! python - <<'PY'
import sys
from pipeline.db import connect
from newsroom.auth import has_admin_login
db = connect("newsroom"); ok = has_admin_login(db); db.close()
sys.exit(0 if ok else 1)
PY
then
  say "Create your admin login for the newsroom desk"
  python -m newsroom create-admin
else
  echo "An admin login already exists."
fi

if [ -f data/finance.db ]; then
  say "Importing the first-version SQLite data (runs once)"
  python scripts/import_sqlite.py data/finance.db
fi

say "Daily schedule (noon New York, London and Sydney time)"
python -m newsroom schedule install
python -m newsroom schedule status

say "Tests: SQLite"
python -m unittest discover -s tests 2>&1 | tail -3
say "Tests: PostgreSQL (throwaway test databases, your data is untouched)"
TEST_PG_URL="postgresql://localhost/postgres" python -m unittest discover -s tests 2>&1 | tail -3

say "Done"
cat <<'TXT'
Next:
  source .venv/bin/activate
  python -m newsroom serve              # the newsroom desk  ->  http://localhost:8765 (sign in as your admin)
  python -m newsroom pull               # or use "Pull all feeds now" on the Sources & pulls page
  python -m newsroom site-feed          # what the website can see (read-only site role)
TXT
