"""Editorial workflow: who can do what, in which state.

    ai_draft ──approve──────────────────────────────▶ approved ──publish──▶ published
        │                                               ▲   │                  │
        └─assign─▶ in_copy ──submit──▶ in_review ──approve┘   └─send_back       ├─unpublish─▶ unpublished
                      ▲                    │                                   └─reopen─▶ in_review (corrections)
                      └──── send_back ─────┘
    kill: from any state that isn't live

Rules
- The editor can approve an AI draft directly (skipping the copywriter), or
  assign it to a copywriter.
- Editors and copy editors can edit any story that isn't killed, including a live
  one. A copy editor can't take over a story assigned to another copy editor.
- Editing an approved, published or unpublished story needs approval again. A
  copy editor's edit puts the story with them (in_copy) until they submit it.
- Editing a live story is a correction: the site keeps the published version
  until the new version is approved and republished. It can still be unpublished
  meanwhile; it can't be killed while it is live.
- Approval is blocked while the current revision has guardrail errors, unless
  the editor gives an override reason.
- The approving editor becomes the byline. Only a publisher (or admin) publishes.
- Publishing writes a frozen snapshot to site.published_stories; the website
  never reads newsroom tables.
"""
from __future__ import annotations

import os

from pipeline import categories, guardrails
from pipeline.cluster import cluster_items
from pipeline.db import Database, utcnow
from pipeline.settings import get_settings
from pipeline.writer import build_source_material

from . import blocks as blk
from .store import add_revision, get_story, guardrail_view, log_event, revision_content


class WorkflowError(Exception):
    pass


ACTIONS: dict[str, dict] = {
    "assign":    {"roles": {"editor", "admin"}, "from": {"ai_draft", "in_review", "approved", "published", "unpublished"}, "to": "in_copy"},
    "submit":    {"roles": {"copywriter", "editor", "admin"}, "from": {"in_copy"}, "to": "in_review"},
    "approve":   {"roles": {"editor", "admin"}, "from": {"ai_draft", "in_copy", "in_review"}, "to": "approved"},
    "send_back": {"roles": {"editor", "admin"}, "from": {"in_review", "approved"}, "to": "in_copy"},
    "kill":      {"roles": {"editor", "admin"}, "from": {"ai_draft", "in_copy", "in_review", "approved", "unpublished"}, "to": "killed"},
    "publish":   {"roles": {"publisher", "admin"}, "from": {"approved"}, "to": "published"},
    # unpublish also works while a correction to a live story is in progress (see _status_ok)
    "unpublish": {"roles": {"editor", "publisher", "admin"}, "from": {"published"}, "to": "unpublished"},
    "reopen":    {"roles": {"editor", "admin"}, "from": {"published", "unpublished", "killed"}, "to": "in_review"},
}
EDITABLE = {"ai_draft", "in_copy", "in_review", "approved"}
# Live and pulled stories can be corrected too: the edit reopens them, and the site
# keeps showing the published version until the correction is approved and republished.
CORRECTABLE = {"published", "unpublished"}
EDIT_ROLES = {"copywriter", "editor", "admin"}

LABELS = {
    "ai_draft": "AI draft", "in_copy": "With copy editor", "in_review": "In review", "approved": "Approved",
    "published": "Published", "unpublished": "Unpublished", "killed": "Killed",
}


# --------------------------------------------------------------------------- permissions
def _status_ok(story: dict, action: str) -> bool:
    if action == "unpublish":
        return story["status"] == "published" or bool(story.get("live"))
    if action == "kill" and story.get("live"):
        return False  # take it off the site first
    return story["status"] in ACTIONS[action]["from"]


def allowed_actions(story: dict, user: dict | None) -> list[str]:
    if not user or not categories.can_access(user, story.get("category")):
        return []
    out = []
    for name, spec in ACTIONS.items():
        if not _status_ok(story, name) or user["role"] not in spec["roles"]:
            continue
        if name == "submit" and user["role"] == "copywriter" and story.get("assigned_to") != user["id"]:
            continue
        out.append(name)
    return out


def can_edit(story: dict, user: dict | None) -> bool:
    """Editors and copy editors can work on any story that isn't killed.

    A copy editor can't take over a story another copy editor is working on.
    """
    if not user or user["role"] not in EDIT_ROLES or not categories.can_access(user, story.get("category")):
        return False
    status = story["status"]
    if status not in EDITABLE | CORRECTABLE:
        return False
    if user["role"] == "copywriter" and status == "in_copy":
        return story.get("assigned_to") in (None, user["id"])
    return True


def status_after_edit(story: dict, user: dict) -> str:
    """Where a story goes when someone saves an edit."""
    status = story["status"]
    if user["role"] == "copywriter" and status in ("ai_draft", "published", "unpublished"):
        return "in_copy"      # the copy editor takes it; they submit it to an editor when done
    if status in ("approved", "published", "unpublished"):
        return "in_review"    # approval must be given again
    return status


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise WorkflowError(msg)


# --------------------------------------------------------------------------- guardrails
def check_content(db: Database, story: dict, content: dict) -> dict:
    """Re-run guardrails on edited content against the story's original sources.

    For an editor pick, the AI research brief and its sources count as source
    material too, just as they did when the draft was written.
    """
    source_text = ""
    if story.get("cluster_id"):
        items = cluster_items(db, story["cluster_id"])
        source_text, _ = build_source_material(items, fetch_text=False)
    research = Database.unj(story.get("research"), None) or {}
    if research.get("text"):
        source_text += "\n\n" + research["text"]
    if content.get("blocks"):
        rep = guardrails.check(blk.guardrail_view(content), source_text, story["country"], freeform=True,
                               category=story.get("category"))
    else:
        rep = guardrails.check(guardrail_view(content), source_text, story["country"], category=story.get("category"))
    if not source_text:  # human-originated story: no source to compare figures against
        rep.issues = [i for i in rep.issues if i["code"] != "unsupported_numbers"]
    return rep.as_dict()


# --------------------------------------------------------------------------- edits
def save_revision(db: Database, story_id: int, user: dict, content: dict, note: str | None = None) -> dict:
    story = get_story(db, story_id)
    _require(story is not None, f"story {story_id} not found")
    _require(can_edit(story, user), f"{user['display_name']} ({user['role']}) can't edit a story that is {LABELS[story['status']]}")
    _require(bool((content.get("headline") or "").strip()), "headline is required")

    report = check_content(db, story, content)
    version = story["current_version"] + 1
    add_revision(db, story_id, version, content, author_kind="human", author_id=user["id"],
                 guardrails=report, note=note)
    new_status = status_after_edit(story, user)
    reset_approval = story["status"] in ("approved", "published", "unpublished")
    assigned_to = story.get("assigned_to")
    if new_status == "in_copy" and user["role"] == "copywriter":
        assigned_to = user["id"]
    db.execute(
        "update newsroom.stories set current_version = ?, status = ?, editor_id = ?, assigned_to = ?, updated_at = ? where id = ?",
        (version, new_status, None if reset_approval else story["editor_id"], assigned_to, utcnow(), story_id),
    )
    extra = ""
    if story.get("live"):
        extra = f" (correction: the site keeps v{story.get('published_version')} until this is approved and republished)"
    elif reset_approval:
        extra = " (approval reset)"
    log_event(db, story_id, "edited", actor_id=user["id"], from_status=story["status"], to_status=new_status,
              note=(note or f"Saved version {version}") + extra)
    db.commit()
    return report


# --------------------------------------------------------------------------- transitions
def perform(db: Database, story_id: int, action: str, user: dict, *, note: str | None = None,
            assignee_id: int | None = None, override: bool = False, correction_note: str | None = None) -> dict:
    story = get_story(db, story_id)
    _require(story is not None, f"story {story_id} not found")
    _require(action in ACTIONS, f"unknown action {action!r}")
    _require(categories.can_access(user, story.get("category")),
             f"{user['display_name']} doesn't work on {categories.name(story.get('category'))} stories")
    spec = ACTIONS[action]
    _require(user["role"] in spec["roles"], f"a {user['role']} can't {action.replace('_', ' ')} stories")
    if action == "kill" and story.get("live"):
        raise WorkflowError("this story is live on the site; unpublish it before killing it")
    _require(_status_ok(story, action),
             f"can't {action.replace('_', ' ')} a story that is {LABELS[story['status']]}")
    if action not in allowed_actions(story, user):
        raise WorkflowError("this story is assigned to another copywriter")

    sets: dict = {"status": spec["to"], "updated_at": utcnow()}
    rev = story["revision"] or {}

    if action == "assign":
        _require(assignee_id is not None, "choose a copywriter to assign")
        assignee = db.fetchone("select * from newsroom.users where id = ? and active = ?", (assignee_id, True))
        _require(assignee is not None and assignee["role"] == "copywriter", "the assignee must be an active copywriter")
        _require(categories.can_access(assignee, story.get("category")),
                 f"{assignee['display_name']} doesn't work on {categories.name(story.get('category'))} stories")
        sets.update(assigned_to=assignee_id, editor_id=None)
        note = f"Assigned to {assignee['display_name']}" + (f": {note}" if note else "")

    elif action == "send_back":
        _require(story.get("assigned_to") is not None, "no copywriter on this story yet; use assign instead")
        sets["editor_id"] = None

    elif action == "approve":
        g = rev.get("guardrails") or {}
        errors = [i for i in g.get("issues", []) if i.get("severity") == "error"]
        if errors and not override:
            raise WorkflowError(
                f"current version has {len(errors)} guardrail error(s); fix them or approve with an override reason")
        if errors:
            _require(bool((note or "").strip()), "an override needs a reason in the note")
            note = f"Override of {len(errors)} guardrail error(s): {note}"
        sets["editor_id"] = user["id"]

    elif action == "publish":
        _require(story.get("editor_id") is not None, "no approving editor recorded")
        publish_to_site(db, story, correction_note=correction_note)
        sets["published_version"] = story["current_version"]
        sets["auto_published"] = False   # a person has now reviewed and published it

    elif action == "unpublish":
        db.execute("update site.published_stories set status = 'retracted', updated_at = ? where story_id = ?",
                   (utcnow(), story_id))
        if story["status"] != "published":
            sets["status"] = story["status"]   # correction in progress carries on; only the site copy comes down

    cols = ", ".join(f"{k} = ?" for k in sets)
    db.execute(f"update newsroom.stories set {cols} where id = ?", (*sets.values(), story_id))
    log_event(db, story_id, action, actor_id=user["id"], from_status=story["status"], to_status=sets["status"], note=note)
    db.commit()
    return get_story(db, story_id)  # type: ignore[return-value]


def set_category(db: Database, story_id: int, user: dict, category: str, note: str | None = None) -> dict:
    """Move a story to another category (editors and admins with access to both)."""
    story = get_story(db, story_id)
    _require(story is not None, f"story {story_id} not found")
    _require(user["role"] in ("editor", "admin"), "only editors and admins can change a story's category")
    _require(categories.valid(category), f"unknown category {category!r}")
    old = story.get("category") or categories.default()
    _require(categories.can_access(user, old) and categories.can_access(user, category),
             "you can only move stories between categories you work on")
    if category == old:
        return story
    _require(story["status"] not in ("killed",), "a killed story can't be moved")
    db.execute("update newsroom.stories set category = ?, updated_at = ? where id = ?", (category, utcnow(), story_id))
    log_event(db, story_id, "recategorized", actor_id=user["id"],
              note=f"{categories.name(old)} → {categories.name(category)}" + (f": {note}" if note else ""))
    db.commit()
    return get_story(db, story_id)  # type: ignore[return-value]


# --------------------------------------------------------------------------- publishing
def publication_name() -> str:
    return ((get_settings().editorial.get("publication") or {}).get("name") or "Finance Hub").strip()


def disclosure_for(author: dict, ai_assisted: bool, human_reviewed: bool = True, category: str | None = None) -> str:
    """The note under a story: how it was made, plus the category's disclaimer (finance: not advice)."""
    base = categories.disclosure(category)
    who = author["display_name"] + (f", {author['job_title']}" if author.get("job_title") else "")
    if ai_assisted and not human_reviewed:
        text = (f"This story was produced with AI from the sources listed and published automatically by the "
                f"{publication_name()} newsroom. {who} is the editor responsible for it.")
    elif ai_assisted:
        text = f"This story was drafted with AI assistance from the sources listed and was reviewed and edited by {who}."
    else:
        text = ""
    return " ".join(t for t in (text, base) if t)


def publish_to_site(db: Database, story: dict, correction_note: str | None = None,
                    human_reviewed: bool = True) -> None:
    author = db.fetchone("select * from newsroom.users where id = ?", (story["editor_id"],))
    _require(author is not None, "approving editor not found")
    if author["is_placeholder"] and get_settings().app_env == "production":
        raise WorkflowError(f"{author['display_name']} is a placeholder profile; it can't be a byline in production")

    now = utcnow()
    db.execute(
        """insert into site.authors (id, slug, display_name, job_title, bio, credentials, avatar_url, updated_at)
           values (?,?,?,?,?,?,?,?)
           on conflict (id) do update set slug = excluded.slug, display_name = excluded.display_name,
             job_title = excluded.job_title, bio = excluded.bio, credentials = excluded.credentials,
             avatar_url = excluded.avatar_url, updated_at = excluded.updated_at""",
        (author["id"], author["slug"], author["display_name"], author.get("job_title"), author.get("bio"),
         author.get("credentials"), author.get("avatar_url"), now),
    )
    rev = story["revision"]
    content = revision_content(rev)
    ai_assisted = story.get("cluster_id") is not None
    db.execute(
        """insert into site.published_stories
           (story_id, slug, country, category, headline, dek, sections, takeaways, action_steps, topics, sources,
            meta_description, body_md, blocks, author_id, ai_assisted, human_reviewed, disclosure, version, status,
            correction_note, first_published_at, updated_at)
           values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'live',?,?,?)
           on conflict (story_id) do update set slug = excluded.slug, country = excluded.country,
             category = excluded.category,
             headline = excluded.headline, dek = excluded.dek, sections = excluded.sections,
             takeaways = excluded.takeaways, action_steps = excluded.action_steps, topics = excluded.topics,
             sources = excluded.sources, meta_description = excluded.meta_description, body_md = excluded.body_md,
             blocks = excluded.blocks,
             author_id = excluded.author_id, ai_assisted = excluded.ai_assisted,
             human_reviewed = excluded.human_reviewed, disclosure = excluded.disclosure,
             version = excluded.version, status = 'live', correction_note = excluded.correction_note,
             updated_at = excluded.updated_at""",
        (story["id"], story["slug"], story["country"], story.get("category") or categories.default(), content["headline"], content.get("dek"),
         db.j(content["sections"]), db.j(content["takeaways"]), db.j(content["action_steps"]),
         db.j(content["topics"]), db.j(content["sources"]), content.get("meta_description"), rev["body_md"],
         db.j(content["blocks"]) if content.get("blocks") else None, author["id"], ai_assisted, human_reviewed, disclosure_for(author, ai_assisted, human_reviewed, story.get("category")),
         story["current_version"], correction_note, now, now),
    )


# --------------------------------------------------------------------------- auto-publish
def autopublish(db: Database, countries: list[str] | None = None) -> dict:
    """Publish untouched AI drafts that clear the score and guardrail bars.

    Only first-version drafts still in `ai_draft` qualify, so anything an editor
    has opened and edited, assigned or killed is left alone. The configured
    responsible editor is named on the story, and the disclosure says plainly
    that it was published automatically without an editor's review.
    """
    cfg = get_settings().editorial.get("autopublish") or {}
    stats: dict = {"published": 0, "below_score": 0, "guardrail_errors": 0, "category_off": 0, "stories": []}
    if not cfg.get("enabled"):
        stats["disabled"] = True
        return stats

    def find_editor(who: str):
        who = (who or "").strip()
        return who, db.fetchone(
            "select * from newsroom.users where (slug = ? or lower(email) = ?) and active = ? and role in ('editor', 'admin')",
            (who, who.lower(), True))

    # AUTOPUBLISH_RESPONSIBLE_EDITOR (a user's slug or email) overrides the config file per server
    who, editor = find_editor(os.getenv("AUTOPUBLISH_RESPONSIBLE_EDITOR") or cfg.get("responsible_editor", ""))
    if not editor:
        stats["error"] = f"responsible editor {who!r} not found or not an active editor or admin"
        return stats
    if editor["is_placeholder"] and get_settings().app_env == "production":
        stats["error"] = f"{editor['display_name']} is a placeholder profile; set a real responsible_editor"
        return stats
    default_editor = editor

    def editor_for(cat: str):
        """A category can name its own responsible editor (AUTOPUBLISH_RESPONSIBLE_EDITOR_SPORTS or
        categories.yaml autopublish.responsible_editor); otherwise the shared one."""
        own = os.getenv(f"AUTOPUBLISH_RESPONSIBLE_EDITOR_{cat.upper()}") or (categories.get(cat).get("autopublish") or {}).get("responsible_editor")
        if not own:
            return default_editor
        _, ed = find_editor(own)
        if not ed or (ed["is_placeholder"] and get_settings().app_env == "production"):
            return None
        return ed

    marks = ""
    params: list = ["ai_draft", False]
    if countries:
        marks = f" and country in ({','.join('?' for _ in countries)})"
        params += list(countries)
    rows = db.fetchall(
        "select id from newsroom.stories where status = ? and current_version = 1 and auto_published = ?"
        " and origin = 'pipeline'" + marks
        + " order by id", params)
    for r in rows:
        story = get_story(db, r["id"])
        cat = story.get("category") or categories.default()
        ccfg = categories.autopublish(cat)
        if not ccfg.get("enabled"):
            stats["category_off"] += 1      # e.g. fitness & health: always reviewed by a person
            continue
        threshold = float(ccfg.get("score_above", 60))
        score = story.get("impact_score")
        if score is None or score <= threshold:
            stats["below_score"] += 1
            continue
        g = (story["revision"] or {}).get("guardrails") or {}
        if ccfg.get("require_guardrails_pass", True) and not g.get("passed"):
            stats["guardrail_errors"] += 1
            continue
        editor = editor_for(cat)
        if not editor:
            stats["error"] = f"no usable responsible editor for {categories.name(cat)}"
            continue
        story["editor_id"] = editor["id"]
        publish_to_site(db, story, human_reviewed=False)
        db.execute(
            """update newsroom.stories set status = 'published', editor_id = ?, published_version = ?,
               auto_published = ?, updated_at = ? where id = ?""",
            (editor["id"], story["current_version"], True, utcnow(), story["id"]))
        log_event(db, story["id"], "auto_publish", from_status="ai_draft", to_status="published",
                  note=f"Impact score {score:.1f} (above {threshold:.0f}), guardrails passed. "
                       f"Responsible editor: {editor['display_name']}")
        db.commit()
        stats["published"] += 1
        stats["stories"].append({"id": story["id"], "country": story["country"], "headline": story["revision"]["headline"]})
    return stats


# --------------------------------------------------------------------------- queues
QUEUES = [
    ("editor", "Needs an editor", ("ai_draft", "in_review")),
    ("copy", "With copy editors", ("in_copy",)),
    ("publish", "Ready to publish", ("approved",)),
    ("live", "Published", ("published",)),
    ("off", "Unpublished & killed", ("unpublished", "killed")),
]
DEFAULT_QUEUE = {"editor": "editor", "admin": "editor", "copywriter": "copy", "publisher": "publish"}


def queue(db: Database, statuses: tuple[str, ...], assigned_to: int | None = None,
          only_categories: set[str] | list[str] | None = None) -> list[dict]:
    marks = ",".join("?" for _ in statuses)
    sql = f"""select s.*, r.headline, r.dek, r.guardrails, r.author_kind, r.word_count,
                     a.display_name as assignee_name, ed.display_name as editor_name
              from newsroom.stories s
              join newsroom.story_revisions r on r.story_id = s.id and r.version = s.current_version
              left join newsroom.users a on a.id = s.assigned_to
              left join newsroom.users ed on ed.id = s.editor_id
              where s.status in ({marks})"""
    params: list = list(statuses)
    if assigned_to:
        sql += " and s.assigned_to = ?"
        params.append(assigned_to)
    if only_categories is not None:
        cats = list(only_categories) or ["-none-"]
        sql += f" and s.category in ({','.join('?' for _ in cats)})"
        params += cats
    sql += " order by coalesce(s.impact_score, 0) desc, s.updated_at desc"
    rows = db.fetchall(sql, params)
    for r in rows:
        r["guardrails"] = Database.unj(r.get("guardrails"), {}) or {}
    return rows


def queue_counts(db: Database, only_categories: set[str] | list[str] | None = None) -> dict[str, int]:
    counts = {s: 0 for s in LABELS}
    where, params = "", []
    if only_categories is not None:
        cats = list(only_categories) or ["-none-"]
        where, params = f" where category in ({','.join('?' for _ in cats)})", cats
    for r in db.fetchall("select status, count(*) as n from newsroom.stories" + where + " group by status", params):
        counts[r["status"]] = r["n"]
    return counts
