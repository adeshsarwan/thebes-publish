"""What the Stories API returns: published stories from the site schema, as JSON-ready dicts.

Everything comes from site.published_stories + site.authors (the frozen copy made at
publish time), never from newsroom tables, so the API only ever shows what an editor
published, exactly as published.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from pipeline import categories
from pipeline.db import Database

MAX_PER_PAGE = 100
LIST_FIELDS = ("id", "slug", "category", "country", "headline", "dek", "hero_image", "author", "byline",
               "published_at", "updated_at", "version", "seo", "path")
IMAGE_FIELDS = ("url", "sizes", "width", "height", "alt", "caption", "credit", "credit_url", "photographer",
                "photographer_url", "source_url", "provider", "kind")


def iso(ts) -> str | None:
    """Any stored timestamp as ISO 8601 UTC ('2026-10-23T09:15:00Z')."""
    if ts is None or ts == "":
        return None
    if isinstance(ts, datetime):
        dt = ts
    else:
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            return str(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_since(value: str | None) -> str | None:
    """Accept '2026-10-20', '2026-10-20T08:00:00Z' or with an offset; return ISO UTC, or raise ValueError."""
    if not value:
        return None
    v = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        v += "T00:00:00+00:00"
    dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _author(r: dict) -> dict:
    return {"name": r.get("display_name"), "slug": r.get("author_slug"), "job_title": r.get("job_title"),
            "bio": r.get("bio"), "credentials": r.get("credentials"), "avatar_url": r.get("avatar_url")}


def _byline(r: dict) -> str:
    title = f", {r['job_title']}" if r.get("job_title") else ""
    if r.get("human_reviewed", True) in (False, 0):
        from newsroom.workflow import publication_name
        return f"{publication_name()} newsroom · Responsible editor: {r.get('display_name')}{title}"
    return f"By {r.get('display_name')}{title}"


def _seo(r: dict) -> dict:
    s = Database.unj(r.get("seo"), None) or {}
    return {"title": s.get("title") or r.get("headline"),
            "description": s.get("description") or r.get("meta_description") or r.get("dek") or "",
            "focus_keyword": s.get("focus_keyword") or "", "keywords": s.get("keywords") or []}


def _images(r: dict) -> list[dict]:
    """Every image the story shows: the main image, in-story images, and images placed in the body
    with the block editor (role "inline", `block` = their position among the blocks)."""
    out, seen = [], set()
    for img in Database.unj(r.get("images"), []) or []:
        item = {k: img.get(k) for k in IMAGE_FIELDS}
        item["role"] = img.get("role")
        item["section"] = img.get("position")
        item["block"] = None
        out.append(item)
        seen.add(img.get("url"))
    for n, b in enumerate(Database.unj(r.get("blocks"), None) or []):
        if b.get("type") != "image":
            continue
        d = b.get("data") or {}
        if d.get("url") in seen:
            next(i for i in out if i["url"] == d.get("url"))["block"] = n
            continue
        item = {k: d.get(k) for k in IMAGE_FIELDS}
        item.update(role="inline", section=None, block=n, sizes=d.get("sizes") or {})
        out.append(item)
        seen.add(d.get("url"))
    return out


def _hero(r: dict) -> dict | None:
    return next((i for i in _images(r) if i["role"] == "hero"), None)


def _body(r: dict, cat: str) -> tuple[str, str]:
    """(format, body_html) without the headline and standfirst, which have their own fields."""
    from newsroom import blocks as blk
    from newsroom.web import render_markdown
    blocks = Database.unj(r.get("blocks"), None)
    if blocks:
        return "blocks", blk.render_blocks(blocks) + blk.render_sources(Database.unj(r.get("sources"), []))
    md = r.get("body_md") or ""
    _, rest = (md.split("\n", 1) + [""])[:2]
    rest = re.sub(r"^\s*\*(.+?)\*\s*\n", "", rest, count=1)       # the standfirst line
    return "sections", blk.insert_figures(render_markdown(rest), Database.unj(r.get("images"), []) or [],
                                          categories.section_titles(cat))


def story(r: dict, full: bool = True) -> dict:
    cat = r.get("category") or categories.default()
    out = {
        "id": r["story_id"],
        "slug": r["slug"],
        "path": f"/{cat}/{r['slug']}",
        "category": {"id": cat, "name": categories.name(cat)},
        "country": r["country"],
        "headline": r["headline"],
        "dek": r.get("dek"),
        "hero_image": _hero(r),
        "author": _author(r),
        "byline": _byline(r),
        "published_at": iso(r.get("first_published_at")),
        "updated_at": iso(r.get("updated_at")),
        "version": r.get("version"),
        "seo": _seo(r),
    }
    if not full:
        return out
    fmt, body_html = _body(r, cat)
    titles = categories.section_titles(cat)
    sections = Database.unj(r.get("sections"), {}) or {}
    out.update({
        "correction_note": r.get("correction_note"),
        "format": fmt,
        "body_html": body_html,
        "body_markdown": r.get("body_md"),
        "blocks": Database.unj(r.get("blocks"), None),
        "sections": None if fmt == "blocks" else [
            {"key": k, "title": titles[k], "text": sections.get(k, "")} for k in categories.SECTION_KEYS if sections.get(k)],
        "action_steps": None if fmt == "blocks" else {"title": titles["action_steps"],
                                                      "items": Database.unj(r.get("action_steps"), [])},
        "takeaways": None if fmt == "blocks" else {"title": titles["takeaways"],
                                                   "items": Database.unj(r.get("takeaways"), [])},
        "images": _images(r),
        "sources": Database.unj(r.get("sources"), []),
        "topics": Database.unj(r.get("topics"), []),
        "ai_assisted": bool(r.get("ai_assisted")),
        "human_reviewed": bool(r.get("human_reviewed", True)),
        "disclosure": r.get("disclosure"),
    })
    return out


SELECT = """select p.*, a.display_name, a.slug as author_slug, a.job_title, a.bio, a.credentials, a.avatar_url
            from site.published_stories p join site.authors a on a.id = p.author_id"""


def list_stories(db: Database, *, category: str | None = None, country: str | None = None,
                 since: str | None = None, updated_since: str | None = None, keyword: str | None = None,
                 include_retracted: bool = False, page: int = 1, per_page: int = 20) -> tuple[list[dict], int]:
    where, params = [], []
    if not include_retracted:
        where.append("p.status = 'live'")
    if category:
        where.append("p.category = ?"); params.append(category)
    if country:
        where.append("p.country = ?"); params.append(country)
    if since:
        where.append("p.first_published_at >= ?"); params.append(since)
    if updated_since:
        where.append("p.updated_at >= ?"); params.append(updated_since)
    sql_where = (" where " + " and ".join(where)) if where else ""
    rows = db.fetchall(SELECT + sql_where + " order by p.first_published_at desc, p.story_id desc", params)
    if keyword:   # match the story's SEO keywords (case-insensitive)
        k = keyword.strip().lower()
        rows = [r for r in rows if any(k == x.lower() for x in _seo(r)["keywords"]) or k == _seo(r)["focus_keyword"].lower()]
    total = len(rows)
    page_rows = rows[(page - 1) * per_page: page * per_page]
    out = []
    for r in page_rows:
        if r["status"] == "live":
            out.append(story(r, full=False) | {"status": "live"})
        else:   # retracted: tell sites to take it down, nothing more
            out.append({"id": r["story_id"], "slug": r["slug"], "status": "retracted",
                        "category": {"id": r.get("category"), "name": categories.name(r.get("category"))},
                        "updated_at": iso(r.get("updated_at"))})
    return out, total


def get_story(db: Database, slug: str) -> dict | None:
    return db.fetchone(SELECT + " where p.slug = ?", (slug,))


def category_list(db: Database) -> list[dict]:
    counts = {r["category"]: r["n"] for r in db.fetchall(
        "select category, count(*) as n from site.published_stories where status = 'live' group by category")}
    return [{"id": c, "name": n, "live_stories": int(counts.get(c, 0))} for c, n in categories.choices()]
