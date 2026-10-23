"""Staff logins: password hashing, sessions, and team management.

- Passwords are hashed with scrypt (standard library) and a per-user salt.
- A session is a random 32-byte token in an HttpOnly cookie; only its SHA-256
  is stored, so a database leak doesn't hand out live sessions.
- New staff get a one-time temporary password and must change it on first login.
- Failed logins are rate-limited per email and per client address.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import string
import threading
import time
from datetime import datetime, timedelta, timezone

from pipeline.db import Database, utcnow
from pipeline.text import slugify

from .users import ROLES

SESSION_DAYS = 7
MIN_PASSWORD = 10
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AuthError(Exception):
    pass


# --------------------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, maxmem=64 * 1024 * 1024, **_SCRYPT)
    return "scrypt$%d$%d$%d$%s$%s" % (_SCRYPT["n"], _SCRYPT["r"], _SCRYPT["p"],
                                      base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or not stored.startswith("scrypt$"):
        return False
    try:
        _, n, r, p, salt_b64, dk_b64 = stored.split("$")
        expected = base64.b64decode(dk_b64)
        dk = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt_b64), n=int(n), r=int(r),
                            p=int(p), dklen=len(expected), maxmem=64 * 1024 * 1024)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, expected)


def check_password_rules(password: str, email: str = "") -> None:
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"Use at least {MIN_PASSWORD} characters.")
    if email and password.lower() == email.lower():
        raise AuthError("The password can't be your email address.")
    if len(set(password)) < 5:
        raise AuthError("That password is too repetitive.")


def temporary_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(4))


# --------------------------------------------------------------------------- rate limit
class _Limiter:
    def __init__(self, max_attempts: int = 5, window_s: int = 15 * 60):
        self.max, self.window = max_attempts, window_s
        self.hits: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def blocked(self, *keys: str) -> bool:
        now = time.time()
        with self.lock:
            for k in keys:
                self.hits[k] = [t for t in self.hits.get(k, []) if now - t < self.window]
                if len(self.hits[k]) >= self.max:
                    return True
        return False

    def fail(self, *keys: str) -> None:
        with self.lock:
            for k in keys:
                self.hits.setdefault(k, []).append(time.time())

    def clear(self, *keys: str) -> None:
        with self.lock:
            for k in keys:
                self.hits.pop(k, None)


limiter = _Limiter()


# --------------------------------------------------------------------------- sessions
def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def login(db: Database, email: str, password: str, client: str = "") -> tuple[dict, str]:
    """Returns (user, session token). Raises AuthError with a generic message."""
    email = (email or "").strip().lower()
    keys = (f"e:{email}", f"c:{client}")
    if limiter.blocked(*keys):
        raise AuthError("Too many attempts. Wait 15 minutes and try again.")
    user = db.fetchone("select * from newsroom.users where lower(email) = ? and active = ?", (email, True))
    if not user or not verify_password(password, user.get("password_hash")):
        limiter.fail(*keys)
        raise AuthError("Email or password is incorrect.")
    limiter.clear(*keys)
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")
    db.execute("insert into newsroom.sessions (token_hash, user_id, created_at, expires_at) values (?,?,?,?)",
               (_token_hash(token), user["id"], utcnow(), expires))
    db.execute("update newsroom.users set last_login_at = ? where id = ?", (utcnow(), user["id"]))
    db.execute("delete from newsroom.sessions where expires_at < ?", (utcnow(),))
    db.commit()
    return user, token


def user_for_session(db: Database, token: str | None) -> dict | None:
    if not token:
        return None
    row = db.fetchone(
        """select u.* from newsroom.sessions s join newsroom.users u on u.id = s.user_id
           where s.token_hash = ? and s.expires_at > ? and u.active = ?""",
        (_token_hash(token), utcnow(), True))
    return row


def logout(db: Database, token: str | None) -> None:
    if token:
        db.execute("delete from newsroom.sessions where token_hash = ?", (_token_hash(token),))
        db.commit()


def end_all_sessions(db: Database, user_id: int) -> None:
    db.execute("delete from newsroom.sessions where user_id = ?", (user_id,))


# --------------------------------------------------------------------------- account changes
def change_password(db: Database, user: dict, current: str, new: str, confirm: str) -> None:
    if not verify_password(current, user.get("password_hash")):
        raise AuthError("Your current password is incorrect.")
    if new != confirm:
        raise AuthError("The new passwords don't match.")
    if new == current:
        raise AuthError("Choose a password different from your current one.")
    check_password_rules(new, user["email"])
    db.execute("update newsroom.users set password_hash = ?, must_change_password = ? where id = ?",
               (hash_password(new), False, user["id"]))
    db.commit()


def set_password(db: Database, user_id: int, password: str, must_change: bool) -> None:
    db.execute("update newsroom.users set password_hash = ?, must_change_password = ? where id = ?",
               (hash_password(password), must_change, user_id))
    end_all_sessions(db, user_id)
    db.commit()


# --------------------------------------------------------------------------- team (admin)
def _require_admin(actor: dict) -> None:
    if not actor or actor["role"] != "admin":
        raise AuthError("Only an admin can manage the team.")


def _clean_categories(cats) -> list[str]:
    from pipeline import categories
    cats = [c for c in (cats or []) if c]
    bad = [c for c in cats if not categories.valid(c)]
    if bad:
        raise AuthError(f"Unknown category: {', '.join(bad)}.")
    return [c for c in categories.ids() if c in cats]     # config order, no duplicates


def create_user(db: Database, actor: dict | None, *, email: str, display_name: str, role: str,
                job_title: str = "", bio: str = "", password: str | None = None,
                categories: list[str] | None = None) -> tuple[dict, str | None]:
    """Create a staff account. Returns (user, temporary password or None if one was given)."""
    if actor is not None:
        _require_admin(actor)
    email = (email or "").strip().lower()
    display_name = (display_name or "").strip()
    if not _EMAIL.match(email):
        raise AuthError("Enter a valid email address.")
    if not display_name:
        raise AuthError("Enter the person's name.")
    if role not in ROLES:
        raise AuthError("Choose a role.")
    cats = _clean_categories(categories)
    if db.fetchone("select id from newsroom.users where lower(email) = ?", (email,)):
        raise AuthError("Someone on the team already uses that email.")
    temp = None
    if password is None:
        temp = temporary_password()
        password = temp
    else:
        check_password_rules(password, email)
    base = slugify(display_name, 60) or "staff"
    slug, n = base, 2
    while db.fetchone("select id from newsroom.users where slug = ?", (slug,)):
        slug, n = f"{base}-{n}", n + 1
    uid = db.insert(
        """insert into newsroom.users (email, display_name, slug, role, categories, job_title, bio, is_placeholder, active,
                                       password_hash, must_change_password, created_at)
           values (?,?,?,?,?,?,?,?,?,?,?,?) returning id""",
        (email, display_name, slug, role, db.j(cats), job_title.strip() or None, bio.strip() or None, False, True,
         hash_password(password), temp is not None, utcnow()))
    db.commit()
    return db.fetchone("select * from newsroom.users where id = ?", (uid,)), temp


def update_user(db: Database, actor: dict, user_id: int, *, role: str | None = None, active: bool | None = None,
                job_title: str | None = None, display_name: str | None = None, bio: str | None = None,
                categories: list[str] | None = None) -> None:
    _require_admin(actor)
    target = db.fetchone("select * from newsroom.users where id = ?", (user_id,))
    if not target:
        raise AuthError("No such team member.")
    if role is not None and role not in ROLES:
        raise AuthError("Choose a role.")
    losing_admin = target["role"] == "admin" and ((role and role != "admin") or active is False)
    if losing_admin:
        others = db.fetchone(
            "select count(*) as n from newsroom.users where role = 'admin' and active = ? and id <> ? and password_hash is not null",
            (True, user_id))["n"]
        if not others:
            raise AuthError("Keep at least one active admin who can log in.")
    sets = {}
    if role is not None:
        sets["role"] = role
    if active is not None:
        sets["active"] = active
    if job_title is not None:
        sets["job_title"] = job_title.strip() or None
    if display_name is not None and display_name.strip():
        sets["display_name"] = display_name.strip()
    if bio is not None:
        sets["bio"] = bio.strip() or None
    if categories is not None:
        sets["categories"] = db.j(_clean_categories(categories))
    if sets:
        db.execute(f"update newsroom.users set {', '.join(k + ' = ?' for k in sets)} where id = ?", (*sets.values(), user_id))
    if active is False:
        end_all_sessions(db, user_id)
    db.commit()


def reset_password(db: Database, actor: dict, user_id: int) -> str:
    """Admin issues a new temporary password; the person must change it at next login."""
    _require_admin(actor)
    if not db.fetchone("select id from newsroom.users where id = ?", (user_id,)):
        raise AuthError("No such team member.")
    temp = temporary_password()
    set_password(db, user_id, temp, must_change=True)
    return temp


def has_admin_login(db: Database) -> bool:
    return bool(db.fetchone(
        "select id from newsroom.users where role = 'admin' and active = ? and password_hash is not null", (True,)))
