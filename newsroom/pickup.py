"""Editor pick-ups: override the desk, research with AI, draft a story.

An editor picks a story the desk rejected or held (or that hasn't been drafted),
sets the angle and notes, and the AI:

  1. researches the topic with Google Search grounding (optional), producing a
     brief with sources;
  2. drafts an original story from the feed items + the research brief, with
     the same guardrails as any AI draft (the brief counts as source material,
     so researched figures aren't flagged as invented).

The draft lands in the newsroom as an AI draft marked "editor pick". It is
never auto-published: the editor approves it or assigns it to a copy editor.

"Write from a link" is the same flow started from a URL instead of a scoring-desk
story: an editor pastes another publication's story, the AI reads that page (robots.txt
respected), researches the topic and writes our own original story. The page's text is
source material, so the originality checks catch any copied wording.

Runs in the background (`python -m newsroom pickup-run <id>`), tracked in
newsroom.pick_requests. Everything uses the newsroom database login.
"""
from __future__ import annotations

import logging
import re
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests

from pipeline import categories, seo
from pipeline.cluster import cluster_items
from pipeline.db import Database, connect, utcnow
from pipeline.llm import LLMClient, get_client, model_name
from pipeline.settings import COUNTRY_NAMES, ROOT, get_settings
from pipeline.text import canonical_url
from pipeline.writer import _slug, build_source_material, draft_story, gather_source_keywords, story_content

from .pull import LOG_DIR
from .store import create_ai_draft, log_event

log = logging.getLogger(__name__)
PICK_ROLES = {"editor", "admin"}
MAX_URLS = 8


class PickupError(Exception):
    pass


def can_pick(user: dict | None) -> bool:
    return bool(user) and user["role"] in PICK_ROLES


# --------------------------------------------------------------------------- requests
def active_request(db: Database, cluster_id: int) -> dict | None:
    return db.fetchone(
        "select * from newsroom.pick_requests where cluster_id = ? and status in ('queued', 'running') order by id desc limit 1",
        (cluster_id,))


def latest_requests(db: Database) -> dict[int, dict]:
    rows = db.fetchall(
        """select p.*, u.display_name from newsroom.pick_requests p join newsroom.users u on u.id = p.requested_by
           where p.id in (select max(id) from newsroom.pick_requests where cluster_id is not null group by cluster_id)""")
    return {r["cluster_id"]: r for r in rows}


def _clean_urls(text_or_list) -> list[str]:
    raw = text_or_list if isinstance(text_or_list, list) else re.split(r"[\s,]+", text_or_list or "")
    out = []
    for u in raw:
        u = u.strip()
        if not u:
            continue
        parts = urlsplit(u)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise PickupError(f"Not a web address: {u}")
        out.append(u)
    if len(out) > MAX_URLS:
        raise PickupError(f"Add at most {MAX_URLS} extra sources.")
    return out


MAX_PROMPTS = 8


def clean_prompts(prompts: list[dict] | None) -> list[dict]:
    """[{prompt, style, aspect, role}] with empty rows dropped; the first one is the main image."""
    from . import media
    out = []
    for p in prompts or []:
        text = re.sub(r"\s+", " ", str(p.get("prompt") or "")).strip()[:2000]
        if not text:
            continue
        out.append({"prompt": text,
                    "style": p.get("style") if p.get("style") in media.style_names() else media.style_names()[0],
                    "aspect": p.get("aspect") if p.get("aspect") in media.cfg()["aspects"] else media.cfg()["default_aspect"],
                    "role": "hero" if not out else "gallery"})
    if len(out) > MAX_PROMPTS:
        raise PickupError(f"Add at most {MAX_PROMPTS} image prompts.")
    return out


def request_pickup(db: Database, user: dict, cluster_id: int, *, angle: str, audience: str | list = "",
                   notes: str = "", extra_urls="", research: bool = True,
                   image_prompts: list[dict] | None = None) -> int:
    if not can_pick(user):
        raise PickupError("Only editors and admins can pick up stories.")
    cluster = db.fetchone("select * from pipeline.story_clusters where id = ?", (cluster_id,))
    if not cluster:
        raise PickupError("No such story on the scoring desk.")
    if not categories.can_access(user, cluster.get("category")):
        raise PickupError(f"You don't work on {categories.name(cluster.get('category'))} stories.")
    existing = db.fetchone("select id from newsroom.stories where cluster_id = ?", (cluster_id,))
    if existing:
        raise PickupError(f"This story is already in the newsroom (story {existing['id']}).")
    if active_request(db, cluster_id):
        raise PickupError("This story is already being researched.")
    angle = (angle or "").strip()
    if len(angle) < 10:
        raise PickupError("Describe the angle: what should the story explain about readers' money?")
    aud = audience if isinstance(audience, list) else [a.strip() for a in (audience or "").split(",") if a.strip()]
    rid = db.insert(
        """insert into newsroom.pick_requests (cluster_id, requested_by, angle, audience, notes, extra_urls, research,
                                               image_prompts, status, created_at)
           values (?,?,?,?,?,?,?,?,'queued',?) returning id""",
        (cluster_id, user["id"], angle, db.j(aud), (notes or "").strip() or None, db.j(_clean_urls(extra_urls)),
         bool(research), db.j(clean_prompts(image_prompts)), utcnow()))
    db.commit()
    return rid  # type: ignore[return-value]


# --------------------------------------------------------------------------- write from a link
LINK_PAGE_CHARS = 12_000      # of the pasted story's text, given to the writer as source material


def clean_link(url: str) -> str:
    url = (url or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or " " in url:
        raise PickupError("Paste the full web address of the story, starting with https://")
    if len(url) > 1000:
        raise PickupError("That web address is too long.")
    return url


def link_request_for(db: Database, url: str) -> dict | None:
    """The latest link request for this story that is running or produced a story still in the newsroom."""
    key = canonical_url(url)
    rows = db.fetchall(
        """select p.*, u.display_name from newsroom.pick_requests p join newsroom.users u on u.id = p.requested_by
           where p.cluster_id is null and p.status in ('queued', 'running', 'done') order by p.id desc""")
    for r in rows:
        if canonical_url(r["source_url"] or "") != key:
            continue
        if r["status"] == "done":
            st = db.fetchone("select status from newsroom.stories where id = ?", (r["story_id"],))
            if not st or st["status"] == "killed":
                continue
        return r
    return None


def request_from_link(db: Database, user: dict, url: str, *, country: str, category: str, angle: str = "",
                      audience: str | list = "", notes: str = "", extra_urls="", research: bool = True,
                      image_prompts: list[dict] | None = None) -> int:
    """Queue "write our own story from this link". The angle is optional: without one, the AI finds it."""
    if not can_pick(user):
        raise PickupError("Only editors and admins can write stories from a link.")
    url = clean_link(url)
    if country not in COUNTRY_NAMES:
        raise PickupError("Choose which edition (country) the story is for.")
    if not categories.valid(category):
        raise PickupError("Choose a category.")
    if not categories.can_access(user, category):
        raise PickupError(f"You don't work on {categories.name(category)} stories.")
    prior = link_request_for(db, url)
    if prior:
        if prior["status"] == "done":
            raise PickupError(f"We already wrote a story from this link (story {prior['story_id']}).")
        raise PickupError(f"This link is already being researched for {prior['display_name']}.")
    angle = (angle or "").strip()
    if angle and len(angle) < 10:
        raise PickupError("Describe the angle in a sentence, or leave it empty and the AI will choose one.")
    aud = audience if isinstance(audience, list) else [a.strip() for a in (audience or "").split(",") if a.strip()]
    rid = db.insert(
        """insert into newsroom.pick_requests (cluster_id, source_url, country, category, requested_by, angle, audience,
                                               notes, extra_urls, research, image_prompts, status, created_at)
           values (null,?,?,?,?,?,?,?,?,?,?,'queued',?) returning id""",
        (url, country, category, user["id"], angle, db.j(aud), (notes or "").strip() or None,
         db.j(_clean_urls(extra_urls)), bool(research), db.j(clean_prompts(image_prompts)), utcnow()))
    db.commit()
    return rid  # type: ignore[return-value]


def link_requests(db: Database, user: dict | None = None, limit: int = 30) -> list[dict]:
    rows = db.fetchall(
        """select p.*, u.display_name, s.status as story_status from newsroom.pick_requests p
           join newsroom.users u on u.id = p.requested_by
           left join newsroom.stories s on s.id = p.story_id
           where p.cluster_id is null order by p.id desc limit ?""", (limit * 3,))
    if user is not None:
        rows = [r for r in rows if categories.can_access(user, r.get("category"))]
    return rows[:limit]


def default_angle(category: str | None, country: str) -> str:
    lens = categories.get(category).get("lens") or "what it means for readers"
    return (f"Our own take on this story for readers in {COUNTRY_NAMES.get(country, country)}: "
            f"explain {lens}, with the latest facts and context, rather than retelling the original article.")


def read_link(db: Database, url: str, fetch_text: bool = True) -> dict:
    """The pasted story as a feed-like item: {title, summary, text, keywords}. Empty when it can't be read."""
    from . import sourcepages
    text, title, meta = "", "", {}
    if fetch_text:
        pg = sourcepages.page(db, url) or {}
        text, title = pg.get("text") or "", pg.get("title") or ""
        meta = seo.fetch_page_seo(url) or {}
    host = (urlsplit(url).hostname or "source").removeprefix("www.")
    summary = (meta.get("description") or "").strip() or text[:400]
    return {"source_name": host, "source_kind": "publisher", "title": (meta.get("title") or title or url).strip()[:300],
            "url": url, "summary": summary, "text": text, "published_at": meta.get("published") or None,
            "topics": [], "feed_tags": meta.get("keywords") or [], "seo": meta}


LINK_NOTE = ("This story was suggested from another publication's article (source [1]). Write OUR OWN original story: "
             "use the article and the research only for facts, attribute facts to whoever reported them, and never "
             "reuse its wording, structure or headline.")


def _run_link(db: Database, req: dict, client: LLMClient, fetch_text: bool, resolve_links: bool) -> int:
    url, country, category = req["source_url"], req["country"], req.get("category")
    item = read_link(db, url, fetch_text=fetch_text)
    if not item["text"] and not req["research"]:
        raise PickupError("Couldn't read that page (it may block robots or need a login). "
                          "Tick \"Research with AI\" so the AI can find the facts elsewhere.")
    source_material, refs = build_source_material([item], fetch_text=False)
    if item["text"]:
        source_material += "\npage text:\n" + item["text"][:LINK_PAGE_CHARS]
    extra_urls = Database.unj(req.get("extra_urls"), [])
    for u in extra_urls:
        refs.append({"name": urlsplit(u).hostname or "source", "title": u, "url": u})

    angle = (req.get("angle") or "").strip() or default_angle(category, country)
    notes = "\n".join(filter(None, [LINK_NOTE, req.get("notes")]))
    brief = None
    if req["research"]:
        research_item = dict(item, summary=(item["summary"] or "")[:400])
        brief = research(client, country=country, headline=item["title"], items=[research_item], angle=angle,
                         notes=notes, extra_urls=extra_urls, resolve_links=resolve_links, category=category)
        source_material += "\n\n[R] AI research brief (Google Search)\n" + brief["text"]
        if brief["sources"]:
            source_material += "\nResearch sources:\n" + "\n".join(f"- {s['name']}: {s['url']}" for s in brief["sources"])
        known = {canonical_url(r["url"]) for r in refs}
        refs += [s for s in brief["sources"] if canonical_url(s["url"]) not in known]

    row = {"cluster_id": None, "country": country, "category": category,
           "source_keywords": seo.source_keywords([item], [item["seo"]] if item["seo"] else []),
           "angle": angle, "audience": Database.unj(req.get("audience"), []), "notes": notes}
    model = model_name("writer", client)
    story, report, attempts = draft_story(client, model, row, source_material)
    research_doc = {"text": (brief or {}).get("text"), "sources": (brief or {}).get("sources", []),
                    "queries": (brief or {}).get("queries", []), "notes": req.get("notes"), "angle": angle,
                    "source_url": url, "source_title": item["title"], "source_read": bool(item["text"])}
    story_id = create_ai_draft(
        db, cluster_id=None, country=country, slug=_slug(country, story["headline"], f"link{req['id']}"),
        content=story_content(story, [item], refs, row["source_keywords"]), guardrails=report.as_dict(), model=model,
        impact_score=None, angle=angle, attempts=attempts, origin="from_link", picked_by=req["requested_by"],
        research=research_doc, category=category)
    log_event(db, story_id, "written_from_link", actor_id=req["requested_by"],
              note=f"From {item['source_name']}: {item['title']}" + ("" if item["text"] else " (page couldn't be read; facts from research)"))
    db.commit()
    return story_id


def start_background(request_id: int) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_fh = open(LOG_DIR / f"pickup-{request_id}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.log", "ab")
    subprocess.Popen([sys.executable, "-m", "newsroom", "pickup-run", str(request_id)], cwd=str(ROOT),
                     stdout=log_fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    log_fh.close()


# --------------------------------------------------------------------------- research
RESEARCH_SYSTEM = """You are a research assistant for the {category_name} desk of a newsroom in {country_name}.
Use Google Search to gather current, verifiable facts for an editor. Prefer primary and official
sources ({preferred}) and established publishers.
Write a research brief in plain English with:
- KEY FACTS: bullet points, each with the specific figure, date and who said it.
- CONTEXT: 2-4 bullets on background a reader needs.
- WHAT IT MEANS FOR READERS IN {country_name_upper} ({lens}): 2-4 bullets.
- COUNTERPOINTS AND CAVEATS: 1-3 bullets.
Only include facts you found in your search results. Do not give personalised financial,
medical or betting advice, and do not repeat unverified rumours."""

PREFERRED_SOURCES = {
    "finance": "central banks, regulators, tax and statistics agencies",
    "sports": "governing bodies, leagues, clubs and official match data",
    "entertainment": "studios, labels, broadcasters, award bodies and the people involved, on the record",
    "fitness": "health agencies, peer-reviewed journals and university research announcements",
}


def _resolve(url: str, timeout: float = 6.0) -> str:
    """Grounding links are Google redirects; store the real article address when we can."""
    try:
        r = requests.head(url, allow_redirects=True, timeout=timeout,
                          headers={"User-Agent": get_settings().user_agent})
        return r.url or url
    except requests.RequestException:
        return url


def research(client: LLMClient, *, country: str, headline: str, items: list[dict], angle: str,
             notes: str | None, extra_urls: list[str], resolve_links: bool = True,
             category: str | None = None) -> dict:
    name = COUNTRY_NAMES.get(country, country)
    cat = category if categories.valid(category) else categories.default()
    lines = [f"TOPIC: {headline}", f"COUNTRY: {name}", f"ANGLE THE EDITOR WANTS: {angle}"]
    if notes:
        lines.append(f"EDITOR'S NOTES: {notes}")
    lines.append("FEED ITEMS THAT RAISED THIS TOPIC:")
    for it in items:
        lines.append(f"- {it['source_name']}: {it['title']} ({it['url']}). {(it.get('summary') or '')[:400]}")
    if extra_urls:
        lines.append("ALSO CONSIDER THESE SOURCES THE EDITOR SUPPLIED:")
        lines += [f"- {u}" for u in extra_urls]
    lines.append(f"Research this for a {categories.name(cat).lower()} story and write the brief.")
    model = model_name("writer", client)
    system = RESEARCH_SYSTEM.format(
        category_name=categories.name(cat).lower(), country_name=name, country_name_upper=name.upper(),
        lens=categories.get(cat).get("lens") or "what it means for readers",
        preferred=PREFERRED_SOURCES.get(cat, "official bodies and the organisations involved"))
    out = client.research(model=model, system=system, prompt="\n".join(lines))
    sources = []
    for s in out.get("sources", [])[:12]:
        url = _resolve(s["url"]) if resolve_links else s["url"]
        host = urlsplit(url).hostname or s.get("title") or "source"
        sources.append({"name": host.removeprefix("www."), "title": s.get("title") or host, "url": url})
    return {"text": out["text"], "sources": sources, "queries": out.get("queries", []), "model": model}


# --------------------------------------------------------------------------- run
def run_request(request_id: int, client: LLMClient | None = None, *, fetch_text: bool = True,
                resolve_links: bool = True, db: Database | None = None) -> int:
    """Research + draft for one pick request. Returns the new story id."""
    own = db is None
    db = db or connect("newsroom")
    try:
        req = db.fetchone("select * from newsroom.pick_requests where id = ?", (request_id,))
        if not req:
            raise PickupError(f"no pick request {request_id}")
        if req["status"] not in ("queued", "failed"):
            raise PickupError(f"pick request {request_id} is {req['status']}")
        db.execute("update newsroom.pick_requests set status = 'running', error = null where id = ?", (request_id,))
        db.commit()
        try:
            run = _run_link if req.get("cluster_id") is None else _run
            story_id = run(db, req, client or get_client(), fetch_text, resolve_links)
        except Exception as exc:
            db.rollback()
            db.execute("update newsroom.pick_requests set status = 'failed', error = ?, finished_at = ? where id = ?",
                       (f"{type(exc).__name__}: {exc}"[:800], utcnow(), request_id))
            db.commit()
            raise
        db.execute("update newsroom.pick_requests set status = 'done', story_id = ?, finished_at = ? where id = ?",
                   (story_id, utcnow(), request_id))
        db.commit()
        prompts = db.unj(req.get("image_prompts"), []) or []
        if prompts:   # saved as drafts on the story; an editor generates them when wanted
            from . import media
            media.add_prompts(db, None, story_id, prompts)
        return story_id
    finally:
        if own:
            db.close()


def _run(db: Database, req: dict, client: LLMClient, fetch_text: bool, resolve_links: bool) -> int:
    cluster = db.fetchone("select * from pipeline.story_clusters where id = ?", (req["cluster_id"],))
    ev = db.fetchone("select * from pipeline.evaluations where cluster_id = ? order by id desc limit 1", (req["cluster_id"],)) or {}
    if db.fetchone("select id from newsroom.stories where cluster_id = ?", (req["cluster_id"],)):
        raise PickupError("this story is already in the newsroom")
    items = cluster_items(db, req["cluster_id"])
    source_material, refs = build_source_material(items, fetch_text=fetch_text)
    extra_urls = Database.unj(req.get("extra_urls"), [])
    for u in extra_urls:
        refs.append({"name": urlsplit(u).hostname or "source", "title": u, "url": u})

    brief = None
    if req["research"]:
        brief = research(client, country=cluster["country"], headline=cluster["headline"], items=items,
                         angle=req["angle"], notes=req.get("notes"), extra_urls=extra_urls, resolve_links=resolve_links,
                         category=cluster.get("category"))
        source_material += "\n\n[R] AI research brief (Google Search)\n" + brief["text"]
        if brief["sources"]:
            source_material += "\nResearch sources:\n" + "\n".join(f"- {s['name']}: {s['url']}" for s in brief["sources"])
        known = {canonical_url(r["url"]) for r in refs}
        refs += [s for s in brief["sources"] if canonical_url(s["url"]) not in known]

    row = {"cluster_id": req["cluster_id"], "country": cluster["country"], "category": cluster.get("category"),
           "source_keywords": gather_source_keywords(items, fetch_pages=fetch_text),
           "angle": req["angle"],
           "audience": Database.unj(req.get("audience"), []), "notes": req.get("notes")}
    model = model_name("writer", client)
    story, report, attempts = draft_story(client, model, row, source_material)
    research_doc = None
    if brief or req.get("notes"):
        research_doc = {"text": (brief or {}).get("text"), "sources": (brief or {}).get("sources", []),
                        "queries": (brief or {}).get("queries", []), "notes": req.get("notes"),
                        "angle": req["angle"], "desk_decision": ev.get("decision"),
                        "desk_score": ev.get("impact_score")}
    story_id = create_ai_draft(
        db, cluster_id=req["cluster_id"], country=cluster["country"],
        slug=_slug(cluster["country"], story["headline"], req["cluster_id"]),
        content=story_content(story, items, refs, row["source_keywords"]), guardrails=report.as_dict(), model=model,
        impact_score=ev.get("impact_score"), angle=req["angle"], attempts=attempts,
        origin="editor_pick", picked_by=req["requested_by"], research=research_doc,
        category=cluster.get("category"))
    decision = ev.get("decision") or "not scored"
    score = f" ({ev['impact_score']:.0f})" if ev.get("impact_score") is not None else ""
    log_event(db, story_id, "picked_up", actor_id=req["requested_by"],
              note=f"Editor override of the desk's {decision}{score}. Angle: {req['angle']}")
    db.commit()
    return story_id
