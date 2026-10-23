"""Newsroom staff.

Real logins come later. For now the desk runs locally with seeded placeholder
personas so the workflow (copywriter -> editor -> publisher) can be exercised.
Placeholder users are flagged `is_placeholder`; publishing under one is
blocked when APP_ENV=production, so a fictional byline can never go live.
"""
from __future__ import annotations

from pipeline.db import Database, utcnow

ROLES = ("copywriter", "editor", "publisher", "admin")

PLACEHOLDERS = [
    {
        "email": "maya.fernandes@financehub.local",
        "display_name": "Maya Fernandes",
        "slug": "maya-fernandes",
        "role": "editor",
        "job_title": "Senior Personal Finance Editor",
        "bio": "Maya edits Story Hub's money desk, covering interest rates, household budgets, "
               "borrowing and savings for readers in the US, UK, Canada and Australia.",
    },
    {
        "email": "daniel.okafor@financehub.local",
        "display_name": "Daniel Okafor",
        "slug": "daniel-okafor",
        "role": "copywriter",
        "job_title": "Personal Finance Writer",
        "bio": "Daniel writes about everyday money decisions: bills, credit, tax deadlines and saving.",
    },
    {
        "email": "sara.lindqvist@financehub.local",
        "display_name": "Sara Lindqvist",
        "slug": "sara-lindqvist",
        "role": "publisher",
        "job_title": "Managing Editor, Digital",
        "bio": "Sara oversees what goes live on Story Hub and when.",
    },
    {
        "email": "admin@financehub.local",
        "display_name": "Desk Admin",
        "slug": "desk-admin",
        "role": "admin",
        "job_title": "Administrator",
        "bio": None,
    },
]


def seed_placeholders(db: Database) -> int:
    added = 0
    for u in PLACEHOLDERS:
        if db.fetchone("select id from newsroom.users where email = ?", (u["email"],)):
            continue
        db.insert(
            """insert into newsroom.users (email, display_name, slug, role, job_title, bio, is_placeholder, active, created_at)
               values (?,?,?,?,?,?,?,?,?) returning id""",
            (u["email"], u["display_name"], u["slug"], u["role"], u["job_title"], u["bio"], True, True, utcnow()),
        )
        added += 1
    db.commit()
    return added


def get_user(db: Database, user_id: int | None) -> dict | None:
    if not user_id:
        return None
    return db.fetchone("select * from newsroom.users where id = ? and active = ?", (user_id, True))


def find_user(db: Database, ref: str) -> dict | None:
    """Look up by id, slug, email or first name (CLI convenience)."""
    if ref.isdigit():
        return get_user(db, int(ref))
    ref_l = ref.lower()
    for u in list_users(db):
        if ref_l in (u["slug"], u["email"].lower(), u["display_name"].split()[0].lower()):
            return u
    return None


def list_users(db: Database, role: str | None = None) -> list[dict]:
    if role:
        return db.fetchall("select * from newsroom.users where active = ? and role = ? order by id", (True, role))
    return db.fetchall("select * from newsroom.users where active = ? order by id", (True,))
