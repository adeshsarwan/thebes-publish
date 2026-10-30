"""Database layer shared by the pipeline and the newsroom.

PostgreSQL (production and local dev): one database with three schemas —
pipeline, newsroom, site — and one login role per system.

SQLite (offline tests only): each schema is a separate attached database
file, so the same fully-qualified SQL (`pipeline.raw_items`, `site.published_stories`)
runs unchanged.

SQL is written once with `?` placeholders. JSON columns go through `db.j()`
when writing and `db.unj()` when reading. Rows always come back as plain
dicts with ISO-8601 strings for timestamps and floats for numerics, whatever
the backend.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "db" / "schema.sql"
SCHEMAS = ("pipeline", "newsroom", "site")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _norm_row(row: Any) -> dict:
    return {k: _norm(v) for k, v in dict(row).items()}


# --------------------------------------------------------------------------- sqlite DDL
def sqlite_ddl(pg_sql: str) -> str:
    s = re.sub(r"--[^\n]*", "", pg_sql)
    s = re.sub(r"create schema if not exists \w+;", "", s, flags=re.I)
    s = re.sub(r"alter table [^;]+ add column if not exists [^;]+;", "", s, flags=re.I)   # tables are created fresh
    s = re.sub(r"alter table [^;]+ alter column [^;]+;", "", s, flags=re.I)
    s = re.sub(r"\bbigserial primary key\b", "integer primary key autoincrement", s, flags=re.I)
    s = re.sub(r"\bjsonb\b", "text", s, flags=re.I)
    s = re.sub(r"\btimestamptz\b", "text", s, flags=re.I)
    s = re.sub(r"default now\(\)", "default (CURRENT_TIMESTAMP)", s, flags=re.I)
    s = re.sub(r"\bnumeric\(\d+,\d+\)", "real", s, flags=re.I)
    # SQLite cannot enforce foreign keys across attached databases.
    s = re.sub(r"\s+references\s+\w+\.\w+\(\w+\)", "", s, flags=re.I)
    # "create index i on schema.t (...)"  ->  "create index schema.i on t (...)"
    s = re.sub(r"create (unique )?index if not exists (\w+) on (\w+)\.(\w+)",
               r"create \1index if not exists \3.\2 on \4", s, flags=re.I)
    return s


# --------------------------------------------------------------------------- database
class Database:
    """Wraps a DB-API style connection. Construct with `Database(url)`."""

    def __init__(self, url: str, *, connection: Any = None, jsonb: Callable[[Any], Any] | None = None,
                 translate: bool = False):
        self.url = url
        if connection is not None:                      # injected Postgres connection (tests, admin tools)
            self.kind = "postgres"
            self.conn = connection
            self._jsonb = jsonb or (lambda v: json.dumps(v))
            self._translate = translate                 # True for psycopg (%s placeholders)
            return
        if url.startswith("sqlite:///"):
            self.kind = "sqlite"
            self._translate = False
            self._open_sqlite(Path(url[len("sqlite:///"):]))
        elif url.startswith(("postgres://", "postgresql://")):
            self.kind = "postgres"
            import psycopg
            from psycopg.rows import dict_row
            from psycopg.types.json import Jsonb

            self.conn = psycopg.connect(url, row_factory=dict_row, autocommit=False)
            self._jsonb = Jsonb
            self._translate = True
        else:
            raise ValueError(f"Unsupported database URL: {url!r}")

    def _open_sqlite(self, main: Path) -> None:
        main.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(main))
        self.conn.row_factory = sqlite3.Row
        for schema in SCHEMAS:
            self.conn.execute("attach database ? as " + schema, (str(main.with_suffix(f".{schema}.db")),))
        for schema in ("main", *SCHEMAS):
            try:  # WAL is faster; some network/synced folders don't support it
                self.conn.execute(f"pragma {schema}.journal_mode = wal")
            except sqlite3.OperationalError:
                pass

    # ---- helpers -------------------------------------------------------
    def j(self, value: Any) -> Any:
        """Prepare a Python value for a JSON column."""
        if self.kind == "postgres":
            return self._jsonb(value)
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def unj(value: Any, default: Any = None) -> Any:
        if value is None:
            return default
        if isinstance(value, (list, dict)):
            return value
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default

    def _sql(self, sql: str) -> str:
        if self._translate:  # psycopg uses %s placeholders, so literal % must be doubled
            return sql.replace("%", "%%").replace("?", "%s")
        return sql

    # ---- queries -------------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        self.conn.execute(self._sql(sql), tuple(params))

    def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        cur = self.conn.execute(self._sql(sql), tuple(params))
        return [_norm_row(r) for r in cur.fetchall()]

    def fetchone(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        cur = self.conn.execute(self._sql(sql), tuple(params))
        row = cur.fetchone()
        return _norm_row(row) if row is not None else None

    def insert(self, sql: str, params: Iterable[Any] = ()) -> int | None:
        """Run an INSERT ... RETURNING id; returns the id, or None on conflict."""
        row = self.fetchone(sql, params)
        return None if row is None else row["id"]

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def close(self) -> None:
        self.conn.close()

    # ---- schema --------------------------------------------------------
    def init_schema(self) -> None:
        """Create tables. SQLite only; Postgres is set up by `db-setup`."""
        if self.kind != "sqlite":
            return
        self.conn.executescript(sqlite_ddl(SCHEMA_PATH.read_text()))
        self.commit()

    # ---- run log -------------------------------------------------------
    def start_run(self, stage: str) -> int:
        rid = self.insert(
            "insert into pipeline.pipeline_runs (stage, started_at) values (?, ?) returning id",
            (stage, utcnow()),
        )
        self.commit()
        return rid  # type: ignore[return-value]

    def finish_run(self, run_id: int, stats: dict, error: str | None = None) -> None:
        self.execute(
            "update pipeline.pipeline_runs set finished_at = ?, stats = ?, error = ? where id = ?",
            (utcnow(), self.j(stats), error, run_id),
        )
        self.commit()


def connect(component: str) -> Database:
    """Open the database as one of: pipeline, newsroom, site."""
    from .settings import get_settings

    db = Database(get_settings().db_url(component))
    db.init_schema()
    return db


def country_filter(countries, column: str = "country") -> tuple[str, list]:
    """SQL fragment + params restricting a query to the given countries (no-op when empty)."""
    if not countries:
        return "", []
    return f" and {column} in ({','.join('?' for _ in countries)})", list(countries)
