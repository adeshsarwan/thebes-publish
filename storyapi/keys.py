"""API keys for website developers.

A key looks like ``sh_live_<random>``. Only its SHA-256 hash is stored (site.api_clients),
so a leaked database doesn't leak working keys; the key itself is shown once, when an
admin creates it. Keys are created and revoked from the newsroom (admins), and checked
by the API through the read-only site login.
"""
from __future__ import annotations

import hashlib
import secrets

from pipeline.db import Database, utcnow

PREFIX = "sh_live_"


class KeyError_(Exception):
    pass


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def new_key() -> str:
    return PREFIX + secrets.token_urlsafe(30)


def create(db: Database, actor: dict, name: str, notes: str = "") -> tuple[dict, str]:
    """Admin only. Returns (client row, the key: shown once, never stored)."""
    if not actor or actor.get("role") != "admin":
        raise KeyError_("Only admins can create API keys.")
    name = " ".join((name or "").split())
    if not name:
        raise KeyError_("Say who the key is for, e.g. 'Acme frontend (staging)'.")
    key = new_key()
    cid = db.insert(
        """insert into site.api_clients (name, key_prefix, key_hash, notes, created_by, created_at)
           values (?,?,?,?,?,?) returning id""",
        (name[:120], key[:14], hash_key(key), (notes or "").strip()[:500] or None, actor.get("email"), utcnow()))
    db.commit()
    return db.fetchone("select * from site.api_clients where id = ?", (cid,)), key  # type: ignore[return-value]


def revoke(db: Database, actor: dict, client_id: int) -> None:
    if not actor or actor.get("role") != "admin":
        raise KeyError_("Only admins can revoke API keys.")
    row = db.fetchone("select id, revoked_at from site.api_clients where id = ?", (client_id,))
    if not row:
        raise KeyError_("No such key.")
    if not row["revoked_at"]:
        db.execute("update site.api_clients set revoked_at = ? where id = ?", (utcnow(), client_id))
        db.commit()


def list_clients(db: Database) -> list[dict]:
    return db.fetchall("select * from site.api_clients order by revoked_at is not null, created_at desc")


def verify(db: Database, key: str | None) -> dict | None:
    """The active client for a key, or None."""
    key = (key or "").strip()
    if not key.startswith(PREFIX) or len(key) > 200:
        return None
    row = db.fetchone("select id, name, key_prefix, revoked_at from site.api_clients where key_hash = ?", (hash_key(key),))
    if not row or row["revoked_at"]:
        return None
    return row
