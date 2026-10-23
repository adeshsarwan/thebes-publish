"""Story categories (finance, sports, entertainment, fitness ...).

Every source belongs to one category; its items, story clusters, desk
evaluations, newsroom stories and published site rows carry that category.
Each category brings its own editorial approach from config/categories.yaml:
desk rubric and thresholds, writer style and section titles, extra guardrails,
disclosure and auto-publish rule. Shared settings stay in editorial.yaml.
"""
from __future__ import annotations

import copy

from .settings import get_settings

SECTION_KEYS = ("what_happened", "money_impact", "who_is_affected", "illustrative_example")
LIST_KEYS = ("action_steps", "takeaways")


class CategoryError(ValueError):
    pass


def _all() -> dict:
    return get_settings().categories.get("categories") or {}


def ids() -> list[str]:
    return list(_all())


def default() -> str:
    cats = get_settings().categories
    return cats.get("default") or (ids()[0] if ids() else "finance")


def valid(cat: str | None) -> bool:
    return bool(cat) and cat in _all()


def check(cat: str | None) -> str:
    if not valid(cat):
        raise CategoryError(f"unknown category {cat!r}; choose one of: {', '.join(ids())}")
    return cat  # type: ignore[return-value]


def get(cat: str | None) -> dict:
    """The category's config; an unknown or missing category falls back to the default."""
    return _all().get(cat or "") or _all().get(default()) or {}


def name(cat: str | None) -> str:
    return get(cat).get("name") or (cat or "").title()


def choices() -> list[tuple[str, str]]:
    return [(c, name(c)) for c in ids()]


# --------------------------------------------------------------------------- desk
def desk(cat: str | None) -> dict:
    """Desk settings: editorial.yaml's shared desk block, overridden by the category."""
    base = dict(get_settings().editorial.get("desk") or {})
    base.update(get(cat).get("desk") or {})
    return base


def dimensions(cat: str | None) -> list[dict]:
    return list(desk(cat).get("dimensions") or [])


def dimension_keys(cat: str | None) -> list[str]:
    return [d["key"] for d in dimensions(cat)]


# --------------------------------------------------------------------------- writer
def sections(cat: str | None) -> dict:
    return (get(cat).get("writer") or {}).get("sections") or {}


def section_titles(cat: str | None) -> dict[str, str]:
    """Titles for the story's sections and lists, e.g. {'money_impact': 'Why it matters', ...}."""
    defaults = {"what_happened": "What happened", "money_impact": "What it means for your money",
                "who_is_affected": "Who is affected", "illustrative_example": "The numbers",
                "action_steps": "What to do now", "takeaways": "Key takeaways"}
    return {k: (sections(cat).get(k) or {}).get("title") or v for k, v in defaults.items()}


def numbers_are_illustrative(cat: str | None) -> bool:
    return bool((sections(cat).get("illustrative_example") or {}).get("illustrative", True))


# --------------------------------------------------------------------------- checks, disclosure, auto-publish
def guardrails(cat: str | None) -> dict:
    """editorial.yaml's shared guardrails with the category's additions/overrides."""
    cfg = copy.deepcopy(get_settings().editorial.get("guardrails") or {})
    cfg.update(copy.deepcopy(get(cat).get("guardrails") or {}))
    cfg.setdefault("locale_terms", False)
    cfg["illustrative_numbers"] = numbers_are_illustrative(cat)
    return cfg


def disclosure(cat: str | None) -> str:
    text = get(cat).get("disclosure")
    return " ".join(str(text).split()) if text else ""


def autopublish(cat: str | None) -> dict:
    """editorial.yaml's autopublish block, overridden by the category's.

    The shared `enabled: false` switches auto-publishing off for every category.
    """
    base = dict(get_settings().editorial.get("autopublish") or {})
    own = get(cat).get("autopublish") or {}
    master = bool(base.get("enabled"))
    base.update(own)
    base["enabled"] = master and bool(own.get("enabled", True))
    return base


# --------------------------------------------------------------------------- staff access
def user_scope(user: dict | None) -> set[str] | None:
    """Categories a person works in; None means all (admins, and staff with none assigned)."""
    if not user:
        return set()
    if user.get("role") == "admin":
        return None
    raw = user.get("categories")
    if isinstance(raw, str):
        import json
        try:
            raw = json.loads(raw or "[]")
        except ValueError:
            raw = []
    cats = {c for c in (raw or []) if valid(c)}
    return cats or None


def can_access(user: dict | None, cat: str | None) -> bool:
    scope = user_scope(user)
    return scope is None or (cat or default()) in scope


def scope_label(user: dict) -> str:
    scope = user_scope(user)
    return "All categories" if scope is None else ", ".join(name(c) for c in ids() if c in scope)
