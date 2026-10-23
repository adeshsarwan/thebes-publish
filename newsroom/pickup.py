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

from pipeline.cluster import cluster_items
from pipeline.db import Database, connect, utcnow
from pipeline.llm import LLMClient, get_client, model_name
from pipeline.settings import COUNTRY_NAMES, ROOT, get_settings
from pipeline.text import canonical_url
from pipeline.writer import _slug, build_source_material, draft_story, story_content

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
           where p.id in (select max(id) from newsroom.pick_requests group by cluster_id)""")
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


def request_pickup(db: Database, user: dict, cluster_id: int, *, angle: str, audience: str | list = "",
                   notes: str = "", extra_urls="", research: bool = True) -> int:
    if not can_pick(user):
        raise PickupError("Only editors and admins can pick up stories.")
    cluster = db.fetchone("select * from pipeline.story_clusters where id = ?", (cluster_id,))
    if not cluster:
        raise PickupError("No such story on the scoring desk.")
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
                                               status, created_at)
           values (?,?,?,?,?,?,?,'queued',?) returning id""",
        (cluster_id, user["id"], angle, db.j(aud), (notes or "").strip() or None, db.j(_clean_urls(extra_urls)),
         bool(research), utcnow()))
    db.commit()
    return rid  # type: ignore[return-value]


def start_background(request_id: int) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_fh = open(LOG_DIR / f"pickup-{request_id}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.log", "ab")
    subprocess.Popen([sys.executable, "-m", "newsroom", "pickup-run", str(request_id)], cwd=str(ROOT),
                     stdout=log_fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    log_fh.close()


# --------------------------------------------------------------------------- research
RESEARCH_SYSTEM = """You are a research assistant for a personal-finance newsroom in {country_name}.
Use Google Search to gather current, verifiable facts for an editor. Prefer official sources
(central banks, regulators, tax and statistics agencies) and established financial publishers.
Write a research brief in plain English with:
- KEY FACTS: bullet points, each with the specific figure, date and who said it.
- CONTEXT: 2-4 bullets on background a reader needs.
- WHAT IT MEANS FOR HOUSEHOLDS IN {country_name_upper}: 2-4 bullets.
- COUNTERPOINTS AND CAVEATS: 1-3 bullets.
Only include facts you found in your search results. Do not give personalised investment advice."""


def _resolve(url: str, timeout: float = 6.0) -> str:
    """Grounding links are Google redirects; store the real article address when we can."""
    try:
        r = requests.head(url, allow_redirects=True, timeout=timeout,
                          headers={"User-Agent": get_settings().user_agent})
        return r.url or url
    except requests.RequestException:
        return url


def research(client: LLMClient, *, country: str, headline: str, items: list[dict], angle: str,
             notes: str | None, extra_urls: list[str], resolve_links: bool = True) -> dict:
    name = COUNTRY_NAMES.get(country, country)
    lines = [f"TOPIC: {headline}", f"COUNTRY: {name}", f"ANGLE THE EDITOR WANTS: {angle}"]
    if notes:
        lines.append(f"EDITOR'S NOTES: {notes}")
    lines.append("FEED ITEMS THAT RAISED THIS TOPIC:")
    for it in items:
        lines.append(f"- {it['source_name']}: {it['title']} ({it['url']}). {(it.get('summary') or '')[:400]}")
    if extra_urls:
        lines.append("ALSO CONSIDER THESE SOURCES THE EDITOR SUPPLIED:")
        lines += [f"- {u}" for u in extra_urls]
    lines.append("Research this for a personal-finance story and write the brief.")
    model = model_name("writer", client)
    out = client.research(model=model, system=RESEARCH_SYSTEM.format(country_name=name, country_name_upper=name.upper()),
                          prompt="\n".join(lines))
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
            story_id = _run(db, req, client or get_client(), fetch_text, resolve_links)
        except Exception as exc:
            db.rollback()
            db.execute("update newsroom.pick_requests set status = 'failed', error = ?, finished_at = ? where id = ?",
                       (f"{type(exc).__name__}: {exc}"[:800], utcnow(), request_id))
            db.commit()
            raise
        db.execute("update newsroom.pick_requests set status = 'done', story_id = ?, finished_at = ? where id = ?",
                   (story_id, utcnow(), request_id))
        db.commit()
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
                         angle=req["angle"], notes=req.get("notes"), extra_urls=extra_urls, resolve_links=resolve_links)
        source_material += "\n\n[R] AI research brief (Google Search)\n" + brief["text"]
        if brief["sources"]:
            source_material += "\nResearch sources:\n" + "\n".join(f"- {s['name']}: {s['url']}" for s in brief["sources"])
        known = {canonical_url(r["url"]) for r in refs}
        refs += [s for s in brief["sources"] if canonical_url(s["url"]) not in known]

    row = {"cluster_id": req["cluster_id"], "country": cluster["country"], "angle": req["angle"],
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
        content=story_content(story, items, refs), guardrails=report.as_dict(), model=model,
        impact_score=ev.get("impact_score"), angle=req["angle"], attempts=attempts,
        origin="editor_pick", picked_by=req["requested_by"], research=research_doc)
    decision = ev.get("decision") or "not scored"
    score = f" ({ev['impact_score']:.0f})" if ev.get("impact_score") is not None else ""
    log_event(db, story_id, "picked_up", actor_id=req["requested_by"],
              note=f"Editor override of the desk's {decision}{score}. Angle: {req['angle']}")
    db.commit()
    return story_id
