"""Stage 4 — write an original story for each commissioned cluster.

The writer gets the desk's angle, the facts from every source in the cluster,
the house style guide and the local glossary. It returns structured sections,
which we check with the guardrails, retry once with feedback if needed, then
assemble into markdown and store for review.
"""
from __future__ import annotations

import logging
import urllib.robotparser
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

from . import categories, guardrails, seo
from .cluster import cluster_items
from .db import Database, country_filter, utcnow
from .desk import commission
from .llm import LLMClient, LLMError, model_name
from .settings import get_settings
from .taxonomy import glossary_for, locale_for
from .text import canonical_url, fingerprint, slugify
from newsroom.store import PROSE_KEYS, create_ai_draft

log = logging.getLogger(__name__)

FULL_TEXT_CHARS = 6000

def write_schema(category: str | None = None) -> dict:
    """The structured story the model returns. Keys are the same for every category;
    what goes in each section comes from the category (config/categories.yaml)."""
    sec = categories.sections(category)
    lens = categories.get(category).get("lens") or "the reader"
    guide = lambda k, fallback: (sec.get(k) or {}).get("guide") or fallback  # noqa: E731
    return {
        "type": "OBJECT",
        "properties": {
            "headline": {"type": "STRING", "description": f"Headline that leads with {lens}, max 90 characters."},
            "dek": {"type": "STRING", "description": "One-sentence standfirst: who is affected and why it matters."},
            "what_happened": {"type": "STRING", "description": guide("what_happened", "2-3 sentences of attributed facts, in our own words.")},
            "money_impact": {"type": "STRING", "description": guide("money_impact", "The core analysis, 3-5 paragraphs separated by blank lines.")},
            "who_is_affected": {"type": "STRING", "description": guide("who_is_affected", "Groups affected and not affected, 1-2 paragraphs.")},
            "illustrative_example": {"type": "STRING", "description": guide("illustrative_example", "Starts with 'Illustrative example:'; round hypothetical figures with the arithmetic shown.")},
            "action_steps": {"type": "ARRAY", "items": {"type": "STRING"}, "description": guide("action_steps", "3-5 general, concrete steps.")},
            "takeaways": {"type": "ARRAY", "items": {"type": "STRING"}, "description": guide("takeaways", "Exactly 3 short bullets.")},
            "meta_description": {"type": "STRING", "description": "SEO description, max 155 characters, with the focus keyword."},
            "seo_title": {"type": "STRING", "description": "Search-results title, max 60 characters, focus keyword near the start."},
            "focus_keyword": {"type": "STRING", "description": "The one search phrase (2-5 words) a reader would type to find this story."},
            "seo_keywords": {"type": "ARRAY", "items": {"type": "STRING"},
                             "description": "5-10 search keywords and phrases for this story, most important first."},
            "topics": {"type": "ARRAY", "items": {"type": "STRING"}},
        },
        "required": ["headline", "dek", "what_happened", "money_impact", "who_is_affected",
                     "illustrative_example", "action_steps", "takeaways", "meta_description",
                     "seo_title", "focus_keyword", "seo_keywords", "topics"],
    }


WRITE_SCHEMA = write_schema("finance")   # kept for callers that predate categories

# --------------------------------------------------------------------------- source material
def _allowed_by_robots(url: str, user_agent: str, timeout: float) -> bool:
    parts = urlsplit(url)
    rp = urllib.robotparser.RobotFileParser()
    try:
        resp = requests.get(f"{parts.scheme}://{parts.netloc}/robots.txt", timeout=timeout,
                            headers={"User-Agent": user_agent})
        if resp.status_code >= 400:
            return True
        rp.parse(resp.text.splitlines())
        return rp.can_fetch(user_agent, url)
    except requests.RequestException:
        return False


def fetch_full_text(url: str) -> str:
    """Main text of a public-sector page, for fact extraction only."""
    s = get_settings()
    if not _allowed_by_robots(url, s.user_agent, s.http_timeout):
        return ""
    try:
        resp = requests.get(url, timeout=s.http_timeout, headers={"User-Agent": s.user_agent})
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.info("full text fetch failed for %s: %s", url, exc)
        return ""
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    paras = [p.get_text(" ", strip=True) for p in root.find_all(["p", "li", "h2", "h3"])]
    text = "\n".join(p for p in paras if len(p) > 30)
    return text[:FULL_TEXT_CHARS]


def build_source_material(items: list[dict], fetch_text: bool = True) -> tuple[str, list[dict]]:
    blocks, refs = [], []
    for i, it in enumerate(items, 1):
        body = ""
        if fetch_text and it.get("source_full_text") and it.get("source_reuse") in ("public_domain", "ogl"):
            body = fetch_full_text(it["url"])
        blocks.append(
            "\n".join(filter(None, [
                f"[{i}] source: {it['source_name']} ({it['source_kind']})",
                f"published: {it.get('published_at') or 'unknown'}",
                f"title: {it['title']}",
                f"summary: {it.get('summary') or ''}",
                f"page text:\n{body}" if body else "",
            ]))
        )
        refs.append({"name": it["source_name"], "title": it["title"], "url": canonical_url(it["url"])})
    return "\n\n".join(blocks), refs


def gather_source_keywords(items: list[dict], fetch_pages: bool = True, max_pages: int = 5) -> list[str]:
    """Keywords the sources use: feed tags, plus each source page's SEO metadata."""
    pages = [seo.fetch_page_seo(it["url"]) for it in items[:max_pages]] if fetch_pages else []
    return seo.source_keywords(items, pages)


# --------------------------------------------------------------------------- prompting
def _system(country: str, category: str | None = None) -> str:
    loc = locale_for(country)
    glossary = "; ".join(f"{k.replace('_', ' ')} = {v}" for k, v in glossary_for(country).items())
    style = (categories.get(category).get("writer") or {}).get("style_guide") or ""
    return style.format(
        country_name=loc["country_name"],
        spelling=loc.get("spelling", "English"),
        currency=loc.get("currency", ""),
        glossary=glossary,
    )


def _prompt(commission_row: dict, source_material: str, feedback: str | None) -> str:
    audience = ", ".join(commission_row.get("audience") or []) or "general readers"
    cat = commission_row.get("category")
    brief = (categories.get(cat).get("writer") or {}).get("brief") or "build the story around the reader"
    if categories.numbers_are_illustrative(cat):
        figures = ("Every figure in the headline, dek, what_happened, money_impact and who_is_affected must "
                   "appear in the source material. Any other figures belong only in the illustrative example.\n")
    else:
        figures = "Every figure anywhere in the story must appear in the source material.\n"
    parts = [
        f"ASSIGNMENT\nCategory: {categories.name(cat)}\nAngle: {commission_row.get('angle')}\nAudience: {audience}\n",
        "Write an original story on this angle. Use the source material only as a source of facts. "
        f"Do not follow its structure or wording; {brief}. " + figures,
        f"SOURCE MATERIAL\n{source_material}\nEND SOURCE MATERIAL",
    ]
    if commission_row.get("source_keywords"):
        parts.append(
            "\nSEARCH KEYWORDS THE SOURCES USE (most common first): " + ", ".join(commission_row["source_keywords"])
            + "\nChoose our own focus keyword and keywords for OUR angle and readers in this country. Use these as "
              "hints where they fit; never add a keyword the story doesn't cover, and don't stuff keywords into the text.")
    if commission_row.get("notes"):
        parts.insert(1, f"EDITOR'S INSTRUCTIONS (follow these)\n{commission_row['notes']}\n")
    if feedback:
        parts.append(
            "\nYOUR PREVIOUS DRAFT FAILED REVIEW. Fix every issue below in a complete new draft:\n" + feedback
        )
    return "\n".join(parts)


def _slug(country: str, headline: str, cluster_id: int) -> str:
    return f"{country.lower()}-{slugify(headline, 70)}-{fingerprint(str(cluster_id), headline)[:6]}"


# --------------------------------------------------------------------------- stage
def draft_story(client: LLMClient, model: str, row: dict, source_material: str) -> tuple[dict, guardrails.Report, int]:
    """Write a story from source material, re-drafting once with feedback if guardrails fail."""
    cat = row.get("category")
    cfg = categories.guardrails(cat)
    system = _system(row["country"], cat)
    story, report, feedback = None, None, None
    attempts = 0
    for attempts in range(1, int(cfg.get("max_regenerations", 1)) + 2):
        story = client.generate_json(
            model=model, system=system, prompt=_prompt(row, source_material, feedback),
            schema=write_schema(cat), temperature=0.6, task="write",
        )
        report = guardrails.check(story, source_material, row["country"], category=cat)
        if report.passed:
            break
        feedback = report.feedback()
        log.info("cluster %s attempt %d failed guardrails:\n%s", row.get("cluster_id"), attempts, feedback)
    assert story is not None and report is not None
    return story, report, attempts


def story_content(story: dict, items: list[dict], refs: list[dict], source_keywords: list[str] | None = None) -> dict:
    topics = sorted(set(story.get("topics") or []) | {t for it in items for t in it.get("topics", [])})
    focus = (story.get("focus_keyword") or "").strip()
    return {
        "seo": {"title": (story.get("seo_title") or "").strip()[:70], "focus_keyword": focus,
                "keywords": seo.clean_keywords(([focus] if focus else []) + list(story.get("seo_keywords") or []), 12),
                "source_keywords": list(source_keywords or [])},
        "headline": story["headline"], "dek": story.get("dek"),
        "sections": {k: story.get(k, "") for k in PROSE_KEYS},
        "action_steps": story.get("action_steps") or [], "takeaways": story.get("takeaways") or [],
        "topics": topics, "meta_description": story.get("meta_description"), "sources": refs,
    }


def write_one(db: Database, client: LLMClient, row: dict, fetch_text: bool = True) -> dict:
    model = model_name("writer", client)
    if db.fetchone("select id from newsroom.stories where cluster_id = ?", (row["cluster_id"],)):
        # an editor already picked this story up; don't spend a draft on it
        db.execute("update pipeline.story_clusters set status = 'drafted', updated_at = ? where id = ?", (utcnow(), row["cluster_id"]))
        db.commit()
        raise ValueError(f"cluster {row['cluster_id']} already has a newsroom story")
    items = cluster_items(db, row["cluster_id"])
    source_material, refs = build_source_material(items, fetch_text=fetch_text)
    row = dict(row, source_keywords=gather_source_keywords(items, fetch_pages=fetch_text))
    story, report, attempts = draft_story(client, model, row, source_material)

    content = story_content(story, items, refs, row["source_keywords"])
    story_id = create_ai_draft(
        db, cluster_id=row["cluster_id"], country=row["country"],
        slug=_slug(row["country"], story["headline"], row["cluster_id"]), content=content,
        guardrails=report.as_dict(), model=model, impact_score=row.get("impact_score"),
        angle=row.get("angle"), attempts=attempts, category=row.get("category"),
    )
    db.execute("update pipeline.story_clusters set status = 'drafted', updated_at = ? where id = ?", (utcnow(), row["cluster_id"]))
    db.commit()
    return {"story_id": story_id, "cluster_id": row["cluster_id"], "country": row["country"],
            "headline": story["headline"], "guardrails_passed": report.passed, "attempts": attempts,
            "issues": [i["detail"] for i in report.issues]}


def write(db: Database, client: LLMClient, fetch_text: bool = True, limit: int | None = None,
          countries: list[str] | None = None) -> dict:
    run_id = db.start_run("write")
    commission(db, countries)
    cf, cp = country_filter(countries, "c.country")
    rows = db.fetchall(
        """select c.id as cluster_id, c.country, c.category, e.angle, e.audience, e.impact_score
           from pipeline.story_clusters c
           join pipeline.evaluations e on e.id = (select max(id) from pipeline.evaluations where cluster_id = c.id)
           where c.status = 'commissioned'""" + cf + " order by e.impact_score desc"
        + (f" limit {int(limit)}" if limit else ""), cp
    )
    stats: dict = {"drafted": 0, "clean": 0, "flagged": 0, "errors": 0, "stories": []}
    for row in rows:
        row["audience"] = db.unj(row.get("audience"), [])
        try:
            result = write_one(db, client, row, fetch_text=fetch_text)
        except (LLMError, KeyError, ValueError, TypeError) as exc:
            log.warning("writing failed for cluster %s: %s", row["cluster_id"], exc)
            db.rollback()
            stats["errors"] += 1
            continue
        stats["drafted"] += 1
        stats["clean" if result["guardrails_passed"] else "flagged"] += 1
        stats["stories"].append(result)
    db.finish_run(run_id, {k: v for k, v in stats.items() if k != "stories"})
    return stats

