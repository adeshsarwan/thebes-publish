"""AI assistant in the story editor.

While editing, a copy editor or editor can ask the AI to research something,
check a fact, or draft more text ("find the latest Ofgem figures and add a
paragraph on prepayment customers"). The AI sees the story as it is on screen,
including unsaved changes, and the earlier messages in the story's chat.

With "Search the web" on (the default), the answer uses Google Search
grounding and comes with sources. When the request asks for new text, the AI
also returns text to add, which the editor inserts as blocks with one click;
nothing changes in the story until they do. Research answers count as source
material in the quality checks, as a pick-up's research brief does.

Each question runs in the background (`python -m newsroom assistant-run <id>`)
and is tracked in newsroom.story_chats; the editor polls for the answer.
"""
from __future__ import annotations

import logging
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from pipeline import categories
from pipeline.db import Database, connect, utcnow
from pipeline.llm import LLMClient, get_client, model_name
from pipeline.settings import COUNTRY_NAMES, ROOT
from pipeline.text import canonical_url

from . import blocks as blk
from .pull import LOG_DIR

log = logging.getLogger(__name__)
MAX_QUESTION = 2000
MAX_STORY_CHARS = 16_000
HISTORY = 6                  # earlier exchanges sent with each question
STALE_MINUTES = 10           # a question still running after this is shown as failed
ADD_MARKER = "=== TEXT TO ADD ==="


class AssistantError(Exception):
    pass


# --------------------------------------------------------------------------- story text
def story_text(state: dict) -> str:
    """The story on screen (headline, standfirst, blocks) as plain markdown for the prompt."""
    lines = [f"# {state.get('headline') or ''}".strip()]
    if state.get("dek"):
        lines.append(f"*{state['dek']}*")
    for b in state.get("blocks") or []:
        d, t = b.get("data") or {}, b.get("type")
        if t == "header":
            lines.append(("### " if d.get("level") == 3 else "## ") + blk.inline_text(d.get("text", "")))
        elif t == "paragraph":
            lines.append(blk.inline_markdown(d.get("text", "")))
        elif t == "list":
            ordered = d.get("style") == "ordered"
            lines.append("\n".join((f"{i}. " if ordered else "- ") + blk.inline_markdown(blk._item_text(it))
                                    for i, it in enumerate(d.get("items") or [], 1)))
        elif t == "quote":
            lines.append("> " + blk.inline_text(d.get("text", "")) + (f" ({blk.inline_text(d['caption'])})" if d.get("caption") else ""))
        elif t == "table":
            for row in d.get("content") or []:
                lines.append("| " + " | ".join(blk.inline_text(c) for c in row) + " |")
        elif t == "image" and d.get("caption"):
            lines.append(f"[image: {blk.inline_text(d['caption'])}]")
    text = "\n\n".join(x for x in lines if x.strip())
    return text[:MAX_STORY_CHARS]


def markdown_to_blocks(md: str) -> list[dict]:
    """The AI's text to add -> editor blocks (headings, paragraphs, lists, quotes)."""
    out: list[dict] = []
    para: list[str] = []
    items: list[str] = []
    style = None

    def flush_para():
        if para:
            out.append({"type": "paragraph", "data": {"text": blk.md_inline_to_html(" ".join(para))}})
            para.clear()

    def flush_list():
        nonlocal style
        if items:
            out.append({"type": "list", "data": {"style": style, "items": [blk.md_inline_to_html(i) for i in items]}})
            items.clear()
        style = None

    for raw in (md or "").splitlines():
        line = raw.strip()
        m_ol, m_ul = re.match(r"^\d+[.)]\s+(.*)", line), re.match(r"^[-*•]\s+(.*)", line)
        if not line:
            flush_para(); flush_list(); continue
        if m_h := re.match(r"^(#{1,6})\s+(.*)", line):
            flush_para(); flush_list()
            out.append({"type": "header", "data": {"text": blk.md_inline_to_html(m_h.group(2).strip("* ")),
                                                   "level": 3 if len(m_h.group(1)) >= 3 else 2}})
        elif m_ol or m_ul:
            flush_para()
            st = "ordered" if m_ol else "unordered"
            if style and style != st:
                flush_list()
            style = st
            items.append((m_ol or m_ul).group(1))
        elif line.startswith(">"):
            flush_para(); flush_list()
            out.append({"type": "quote", "data": {"text": blk.md_inline_to_html(line.lstrip("> ").strip()), "caption": ""}})
        else:
            flush_list()
            para.append(line)
    flush_para(); flush_list()
    return blk.clean_blocks(out)


# --------------------------------------------------------------------------- requests
def can_use(story: dict, user: dict | None) -> bool:
    from . import workflow as wf
    return bool(user) and categories.can_access(user, story.get("category")) and wf.can_edit(story, user)


def ask(db: Database, user: dict, story: dict, question: str, state: dict | None = None, research: bool = True) -> int:
    if not can_use(story, user):
        raise AssistantError("You can't edit this story, so you can't use its assistant.")
    question = re.sub(r"[ \t]+", " ", (question or "").strip())[:MAX_QUESTION]
    if len(question) < 3:
        raise AssistantError("Type what you'd like the AI to do.")
    if db.fetchone("select id from newsroom.story_chats where story_id = ? and user_id = ? and status in ('queued', 'running') "
                   "and created_at > ?", (story["id"], user["id"], _stale_cutoff())):
        raise AssistantError("The AI is still answering your last question.")
    state = state or {}
    rid = db.insert(
        """insert into newsroom.story_chats (story_id, user_id, question, research, story_state, status, created_at)
           values (?,?,?,?,?,'queued',?) returning id""",
        (story["id"], user["id"], question, bool(research),
         db.j({"headline": state.get("headline") or "", "dek": state.get("dek") or "", "blocks": state.get("blocks") or []}),
         utcnow()))
    db.commit()
    return rid  # type: ignore[return-value]


def start_background(chat_id: int) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_fh = open(LOG_DIR / f"assistant-{chat_id}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.log", "ab")
    subprocess.Popen([sys.executable, "-m", "newsroom", "assistant-run", str(chat_id)], cwd=str(ROOT),
                     stdout=log_fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    log_fh.close()


def _stale_cutoff() -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=STALE_MINUTES)).isoformat(timespec="seconds")


def messages(db: Database, story_id: int, limit: int = 40) -> list[dict]:
    rows = db.fetchall(
        """select c.*, u.display_name from newsroom.story_chats c join newsroom.users u on u.id = c.user_id
           where c.story_id = ? order by c.id desc limit ?""", (story_id, limit))
    cutoff = _stale_cutoff()
    out = []
    for r in reversed(rows):
        status = r["status"]
        if status in ("queued", "running") and str(r["created_at"]) < cutoff:
            status, r["error"] = "failed", "The AI took too long to answer. Try again."
        out.append({"id": r["id"], "who": r["display_name"], "question": r["question"], "research": bool(r["research"]),
                    "status": status, "error": r.get("error"),
                    "reply_html": render_reply(r.get("reply") or ""),
                    "blocks": Database.unj(r.get("blocks"), []) or [],
                    "sources": Database.unj(r.get("sources"), []) or [],
                    "queries": Database.unj(r.get("queries"), []) or [],
                    "created_at": r["created_at"]})
    return out


def render_reply(md: str) -> str:
    from .web import render_markdown   # the desk's safe markdown (escapes everything)
    return render_markdown(md) if md else ""


def research_text(db: Database, story_id: int) -> tuple[str, set[str]]:
    """Answers researched on the web for this story, and their source addresses, for the quality checks."""
    rows = db.fetchall("select reply, blocks, sources from newsroom.story_chats where story_id = ? and status = 'done' and research = ?",
                       (story_id, True))
    texts, urls = [], set()
    for r in rows:
        if r.get("reply"):
            texts.append(r["reply"])
        # figures in researched text-to-add count as sourced; only the figures, so inserting the
        # text itself isn't flagged as copied from the source material
        added = blk.guardrail_view({"blocks": Database.unj(r.get("blocks"), []) or []})
        figures = re.findall(r"\S*\d\S*", added["body"])
        if figures:
            texts.append("Figures researched by the assistant: " + " ; ".join(figures))
        urls |= {s.get("url") for s in Database.unj(r.get("sources"), []) or [] if s.get("url")}
    return "\n\n".join(texts), urls


# --------------------------------------------------------------------------- the AI
SYSTEM = """You are the research and writing assistant for the {category} desk of a newsroom, working with an
editor on a story for readers in {country}. You can see the story as it is on the editor's screen.

Answer the editor's request directly and briefly, in plain English. Rules:
- Only state facts you can support{search_rule}. Give the specific figure, date and who said it.
- Never invent quotes, people, studies or figures. If you can't find something, say so.
- Write in the story's voice and {spelling} spelling. Don't give personalised financial, medical or
  betting advice. Don't copy wording from your sources; write it fresh.
- When the editor asks you to write, add, expand or rewrite something, put the ready-to-insert text
  after a line that says exactly {marker} and nothing else. Use '## ' for a new section heading,
  plain paragraphs, and '- ' or '1. ' for lists. Keep the text to add free of commentary and links.
- When the editor only asks a question or wants a check, don't include {marker}."""

ASSIST_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "reply": {"type": "STRING", "description": "Your answer to the editor, in markdown."},
        "text_to_add": {"type": "STRING", "description": "Ready-to-insert story text in markdown, or empty."},
    },
    "required": ["reply", "text_to_add"],
}


def _prompt(story_md: str, history: list[dict], question: str) -> str:
    parts = [f"THE STORY ON SCREEN\n{story_md}\nEND OF STORY"]
    if history:
        parts.append("EARLIER IN THIS CHAT")
        for h in history:
            parts.append(f"Editor: {h['question']}\nAssistant: {(h.get('reply') or '')[:1500]}")
    parts.append(f"EDITOR'S REQUEST: {question}")
    return "\n\n".join(parts)


def split_reply(text: str) -> tuple[str, str]:
    """(answer, text to add) from a grounded answer that may contain the marker line."""
    m = re.search(r"^\s*=+\s*TEXT TO ADD\s*=+\s*$", text or "", re.M | re.I)
    if not m:
        return (text or "").strip(), ""
    return text[:m.start()].strip(), text[m.end():].strip()


def run(chat_id: int, client: LLMClient | None = None, *, resolve_links: bool = True, db: Database | None = None) -> dict:
    own = db is None
    db = db or connect("newsroom")
    try:
        row = db.fetchone("select * from newsroom.story_chats where id = ?", (chat_id,))
        if not row:
            raise AssistantError(f"no assistant request {chat_id}")
        db.execute("update newsroom.story_chats set status = 'running' where id = ?", (chat_id,))
        db.commit()
        try:
            result = _answer(db, row, client or get_client(), resolve_links)
        except Exception as exc:
            db.rollback()
            db.execute("update newsroom.story_chats set status = 'failed', error = ?, finished_at = ? where id = ?",
                       (f"{type(exc).__name__}: {exc}"[:800], utcnow(), chat_id))
            db.commit()
            raise
        db.execute(
            """update newsroom.story_chats set status = 'done', reply = ?, blocks = ?, sources = ?, queries = ?, model = ?,
               finished_at = ? where id = ?""",
            (result["reply"], db.j(result["blocks"]), db.j(result["sources"]), db.j(result["queries"]), result["model"],
             utcnow(), chat_id))
        db.commit()
        return result
    finally:
        if own:
            db.close()


def _answer(db: Database, row: dict, client: LLMClient, resolve_links: bool) -> dict:
    from pipeline.taxonomy import locale_for
    from .pickup import _resolve
    story = db.fetchone("select id, country, category from newsroom.stories where id = ?", (row["story_id"],))
    if not story:
        raise AssistantError("the story no longer exists")
    state = Database.unj(row.get("story_state"), {}) or {}
    if not state.get("headline"):
        from .store import get_story, editor_blocks
        s = get_story(db, story["id"])
        rev = s["revision"]
        state = {"headline": rev["headline"], "dek": rev.get("dek"), "blocks": editor_blocks(rev, story.get("category"))}
    history = db.fetchall(
        """select question, reply from newsroom.story_chats where story_id = ? and id < ? and status = 'done'
           order by id desc limit ?""", (story["id"], row["id"], HISTORY))[::-1]
    loc = locale_for(story["country"])
    system = SYSTEM.format(
        category=categories.name(story.get("category")).lower(), country=COUNTRY_NAMES.get(story["country"], story["country"]),
        spelling=loc.get("spelling", "English"), marker=ADD_MARKER,
        search_rule=" with your Google Search results, and prefer primary and official sources" if row["research"] else
                    " from the story itself or well-established general knowledge; say when something needs checking")
    prompt = _prompt(story_text(state), history, row["question"])
    model = model_name("writer", client)
    sources, queries = [], []
    if row["research"]:
        out = client.research(model=model, system=system, prompt=prompt)
        reply, add = split_reply(out.get("text") or "")
        queries = out.get("queries") or []
        seen = set()
        for s in (out.get("sources") or [])[:12]:
            url = _resolve(s["url"]) if resolve_links else s["url"]
            key = canonical_url(url)
            if key in seen:
                continue
            seen.add(key)
            host = (urlsplit(url).hostname or s.get("title") or "source").removeprefix("www.")
            sources.append({"name": host, "title": s.get("title") or host, "url": url})
    else:
        out = client.generate_json(model=model, system=system, prompt=prompt, schema=ASSIST_SCHEMA, temperature=0.5,
                                   task="assist")
        reply, add = (out.get("reply") or "").strip(), (out.get("text_to_add") or "").strip()
        if not add:
            reply, add = split_reply(reply)
    if not reply and add:
        reply = "Here is text you can add to the story."
    return {"reply": reply, "blocks": markdown_to_blocks(add) if add else [], "sources": sources,
            "queries": queries, "model": model}
