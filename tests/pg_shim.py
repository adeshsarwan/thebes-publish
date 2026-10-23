"""Test-only PostgreSQL adapter that runs queries through the `psql` binary.

Used when psycopg isn't installed (e.g. CI sandboxes) so the suite can still
exercise the real Postgres schema, SQL and role grants. Parameters are
rendered as SQL literals of unknown type (like psycopg does), each statement
runs in its own session, and rows come back via json_agg.
"""
from __future__ import annotations

import json
import re
import subprocess


class JsonParam:
    def __init__(self, value):
        self.value = value


class PgError(Exception):
    pass


def _literal(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, JsonParam):
        return "'" + json.dumps(v.value, ensure_ascii=False).replace("'", "''") + "'::jsonb"
    return "'" + str(v).replace("'", "''") + "'"


def render(sql: str, params: tuple) -> str:
    it = iter(params)
    out = re.sub(r"\?", lambda _: _literal(next(it)), sql)
    return out


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class PsqlConnection:
    def __init__(self, url: str, preamble: str = ""):
        self.url = url
        self.preamble = preamble          # e.g. "set role story_site;" applied to every statement

    def _run(self, sql: str) -> str:
        proc = subprocess.run(
            ["psql", self.url, "-X", "-q", "-t", "-A", "-v", "ON_ERROR_STOP=1", "-c", self.preamble + sql],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise PgError(proc.stderr.strip())
        return proc.stdout

    def execute(self, sql: str, params: tuple = ()):
        q = render(sql, params).strip().rstrip(";")
        head = q.lstrip("( \n").lower()
        returns_rows = head.startswith(("select", "with")) or re.search(r"\breturning\b", q, re.I)
        if not returns_rows:
            self._run(q)
            return _Cursor([])
        wrapped = f"with __q as ({q}) select coalesce(json_agg(__q), '[]'::json) from __q"
        out = self._run(wrapped).strip()
        return _Cursor(json.loads(out or "[]"))

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass
