"""Low-level reads/writes for newsroom stories and revisions.

Both the pipeline (creating AI drafts) and the newsroom desk (human edits)
write revisions through `add_revision`, so every version of every story is
stored the same way.
"""
from __future__ import annotations

from pipeline import categories
from pipeline.db import Database, utcnow

from . import blocks as blk

PROSE_KEYS = ("what_happened", "money_impact", "who_is_affected", "illustrative_example")
SECTION_TITLES = {
    "what_happened": "What happened",
    "money_impact": "What it means for your money",
    "who_is_affected": "Who is affected",
    "illustrative_example": "The numbers",
}


def assemble_markdown(content: dict, category: str | None = None) -> str:
    """Story body as markdown (no disclosure; the site renders that with the byline).

    Section titles come from the story's category (finance: "What it means for your money", ...).
    """
    titles = categories.section_titles(category or content.get("category"))
    md = [f"# {content['headline']}", ""]
    if content.get("dek"):
        md += [f"*{content['dek']}*", ""]
    for key in PROSE_KEYS:
        text = (content.get("sections") or {}).get(key, "").strip()
        if text:
            md += [f"## {titles[key]}", "", text, ""]
    if content.get("action_steps"):
        md += [f"## {titles['action_steps']}", ""] + [f"{i}. {s}" for i, s in enumerate(content["action_steps"], 1)] + [""]
    if content.get("takeaways"):
        md += [f"## {titles['takeaways']}", ""] + [f"- {t}" for t in content["takeaways"]] + [""]
    if content.get("sources"):
        md += ["## Sources", ""] + [f"- {r['name']}: [{r['title']}]({r['url']})" for r in content["sources"]] + [""]
    return "\n".join(md)


def guardrail_view(content: dict) -> dict:
    """Flatten content into the shape guardrails.check() expects."""
    return {
        "headline": content.get("headline", ""),
        "dek": content.get("dek", ""),
        **{k: (content.get("sections") or {}).get(k, "") for k in PROSE_KEYS},
        "action_steps": content.get("action_steps") or [],
        "takeaways": content.get("takeaways") or [],
    }


def add_revision(db: Database, story_id: int, version: int, content: dict, *, author_kind: str,
                 author_id: int | None = None, guardrails: dict | None = None,
                 model: str | None = None, note: str | None = None) -> int:
    """Store a version. AI drafts have fixed sections; block-editor versions carry `blocks`."""
    blocks = content.get("blocks")
    category = content.get("category") or (db.fetchone(
        "select category from newsroom.stories where id = ?", (story_id,)) or {}).get("category")
    body = blk.blocks_markdown(content) if blocks else assemble_markdown(content, category)
    return db.insert(  # type: ignore[return-value]
        """insert into newsroom.story_revisions
           (story_id, version, author_kind, author_id, headline, dek, sections, takeaways, action_steps,
            topics, meta_description, sources, body_md, blocks, seo, word_count, guardrails, model, note, created_at)
           values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) returning id""",
        (
            story_id, version, author_kind, author_id, content["headline"], content.get("dek"),
            db.j({k: (content.get("sections") or {}).get(k, "") for k in PROSE_KEYS}),
            db.j(content.get("takeaways") or []), db.j(content.get("action_steps") or []),
            db.j(content.get("topics") or []), content.get("meta_description"),
            db.j(content.get("sources") or []), body, db.j(blocks) if blocks else None,
            db.j(content["seo"]) if content.get("seo") else None,
            (guardrails or {}).get("metrics", {}).get("word_count") or len(body.split()),
            db.j(guardrails) if guardrails is not None else None, model, note, utcnow(),
        ),
    )


def log_event(db: Database, story_id: int, action: str, *, actor_id: int | None = None,
              from_status: str | None = None, to_status: str | None = None, note: str | None = None) -> None:
    db.insert(
        """insert into newsroom.story_events (story_id, actor_id, action, from_status, to_status, note, created_at)
           values (?,?,?,?,?,?,?) returning id""",
        (story_id, actor_id, action, from_status, to_status, note, utcnow()),
    )


def create_ai_draft(db: Database, *, cluster_id: int, country: str, slug: str, content: dict,
                    guardrails: dict, model: str, impact_score: float | None, angle: str | None,
                    attempts: int, origin: str = "pipeline", picked_by: int | None = None,
                    research: dict | None = None, category: str | None = None) -> int:
    now = utcnow()
    category = category if categories.valid(category) else categories.default()
    story_id = db.insert(
        """insert into newsroom.stories
           (cluster_id, country, category, slug, status, current_version, impact_score, angle, origin, picked_by, research,
            created_at, updated_at)
           values (?,?,?,?,'ai_draft',1,?,?,?,?,?,?,?) returning id""",
        (cluster_id, country, category, slug, impact_score, angle, origin, picked_by,
         db.j(research) if research is not None else None, now, now),
    )
    add_revision(db, story_id, 1, dict(content, category=category), author_kind="ai", guardrails=guardrails, model=model,
                 note=f"AI draft, {attempts} attempt(s)" + (" (written from a link" + (", with AI research)" if (research or {}).get("text") else ")")
                                                            if origin == "from_link" else
                                                            " (editor pick, with AI research)" if research else
                                                            " (editor pick)" if origin == "editor_pick" else ""))
    status = "passed guardrails" if guardrails.get("passed") else "has guardrail issues"
    log_event(db, story_id, "drafted", actor_id=picked_by, to_status="ai_draft", note=f"AI draft {status}")
    return story_id  # type: ignore[return-value]


def get_story(db: Database, story_id: int) -> dict | None:
    s = db.fetchone("select * from newsroom.stories where id = ?", (story_id,))
    if not s:
        return None
    rev = db.fetchone(
        "select * from newsroom.story_revisions where story_id = ? and version = ?", (story_id, s["current_version"])
    )
    s["revision"] = decode_revision(rev) if rev else None
    site = db.fetchone("select status, version from site.published_stories where story_id = ?", (story_id,))
    s["live"] = bool(site and site["status"] == "live")
    s["live_version"] = site["version"] if site else None
    return s


def decode_revision(rev: dict) -> dict:
    rev = dict(rev)
    for key, default in (("sections", {}), ("takeaways", []), ("action_steps", []), ("topics", []),
                         ("sources", []), ("guardrails", None), ("blocks", None), ("seo", None)):
        rev[key] = Database.unj(rev.get(key), default)
    return rev


def revision_content(rev: dict) -> dict:
    return {k: rev.get(k) for k in ("headline", "dek", "sections", "takeaways", "action_steps",
                                    "topics", "meta_description", "sources", "blocks", "seo")}


def editor_blocks(rev: dict, category: str | None = None) -> list[dict]:
    """Blocks to open in the editor: the version's own, or the AI sections converted
    (with the category's section titles)."""
    return rev.get("blocks") or blk.content_to_blocks(revision_content(rev), categories.section_titles(category))
