"""Create schemas, tables and per-system login roles in PostgreSQL.

    python -m pipeline.cli db-setup                 # uses ADMIN_DATABASE_URL
    python -m pipeline.cli db-setup --write-env     # also writes role URLs into .env

Roles:
    story_pipeline  read/write pipeline; create AI drafts in newsroom
    story_newsroom  read pipeline; read/write newsroom; publish into site
    story_site      read-only on site (this is what the public website uses)

Safe to re-run: tables use IF NOT EXISTS, roles are created once and their
passwords are kept in .env, and grants are reapplied (needed after schema changes).
"""
from __future__ import annotations

import re
import secrets
import string
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .db import SCHEMA_PATH, Database
from .settings import ROOT

ROLES = ("story_pipeline", "story_newsroom", "story_site")
ENV_KEYS = {"story_pipeline": "PIPELINE_DATABASE_URL", "story_newsroom": "NEWSROOM_DATABASE_URL", "story_site": "SITE_DATABASE_URL"}

GRANTS = """
revoke all on schema pipeline, newsroom, site from public;

grant usage on schema pipeline to story_pipeline;
grant select, insert, update, delete on all tables in schema pipeline to story_pipeline;
grant usage, select on all sequences in schema pipeline to story_pipeline;
grant usage on schema newsroom to story_pipeline;
grant select, insert on newsroom.stories, newsroom.story_revisions, newsroom.story_events to story_pipeline;
grant usage, select on sequence newsroom.stories_id_seq, newsroom.story_revisions_id_seq, newsroom.story_events_id_seq to story_pipeline;

grant usage on schema pipeline to story_newsroom;
grant select on all tables in schema pipeline to story_newsroom;
grant insert, update on pipeline.sources to story_newsroom;
grant usage on schema newsroom to story_newsroom;
grant select, insert, update, delete on all tables in schema newsroom to story_newsroom;
grant usage, select on all sequences in schema newsroom to story_newsroom;
grant usage on schema site to story_newsroom;
grant select, insert, update on all tables in schema site to story_newsroom;
grant usage, select on all sequences in schema site to story_newsroom;

grant usage on schema site to story_site;
grant select on all tables in schema site to story_site;
"""


def _password() -> str:
    alphabet = string.ascii_letters + string.digits      # no quoting needed in SQL or URLs
    return "".join(secrets.choice(alphabet) for _ in range(28))


def role_url(admin_url: str, role: str, password: str) -> str:
    parts = urlsplit(admin_url)
    host = parts.hostname or "localhost"
    netloc = f"{role}:{password}@{host}" + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def apply(admin: Database, passwords: dict[str, str], update_passwords: bool = True) -> None:
    """Create schema objects and roles, then (re)apply grants.

    Roles are server-wide in Postgres. Pass update_passwords=False (tests) to
    leave the passwords of roles that already exist untouched.
    """
    for stmt in _statements(SCHEMA_PATH.read_text()):
        admin.execute(stmt)
    for role in ROLES:
        pw = passwords[role]
        if not re.fullmatch(r"[A-Za-z0-9]{12,}", pw):
            raise ValueError(f"password for {role} must be 12+ letters/digits")
        exists = admin.fetchone("select 1 as x from pg_roles where rolname = ?", (role,))
        if not exists:
            admin.execute(f"create role {role} with login password '{pw}'")
        elif update_passwords:
            admin.execute(f"alter role {role} with login password '{pw}'")
    for stmt in _statements(GRANTS):
        admin.execute(stmt)
    admin.commit()


def _statements(sql: str) -> list[str]:
    sql = re.sub(r"--[^\n]*", "", sql)
    return [s.strip() for s in sql.split(";") if s.strip()]


# --------------------------------------------------------------------------- .env
def _read_env(path: Path) -> dict[str, str]:
    out = {}
    if path.exists():
        for line in path.read_text().splitlines():
            m = re.match(r"^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$", line)
            if m:
                out[m.group(1)] = m.group(2).split(" #")[0].strip()
    return out


def _set_env(path: Path, values: dict[str, str]) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    seen = set()
    for i, line in enumerate(lines):
        m = re.match(r"^\s*#?\s*([A-Z_][A-Z0-9_]*)\s*=", line)
        if m and m.group(1) in values and m.group(1) not in seen:
            lines[i] = f"{m.group(1)}={values[m.group(1)]}"
            seen.add(m.group(1))
    for k, v in values.items():
        if k not in seen:
            lines.append(f"{k}={v}")
    path.write_text("\n".join(lines) + "\n")


def setup(admin_url: str, write_env: bool = False, env_path: Path | None = None) -> dict[str, str]:
    env_path = env_path or ROOT / ".env"
    current = _read_env(env_path)
    passwords = {}
    for role, key in ENV_KEYS.items():
        existing = current.get(key, "")
        pw = urlsplit(existing).password if existing.startswith("postgres") else None
        passwords[role] = pw or _password()

    admin = Database(admin_url)
    try:
        apply(admin, passwords)
    finally:
        admin.close()

    urls = {ENV_KEYS[r]: role_url(admin_url, r, passwords[r]) for r in ROLES}
    if write_env:
        _set_env(env_path, {"ADMIN_DATABASE_URL": admin_url, **urls})
    return urls
