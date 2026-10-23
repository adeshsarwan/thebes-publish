"""Newsroom desk: local web app, standard library only.

    python -m newsroom serve          # http://localhost:8765 (next free port if busy)

Staff sign in with their own accounts (see newsroom/auth.py); every page
enforces the signed-in person's role. Binds to 127.0.0.1 by default
(DESK_BIND to change) and only accepts form posts from the desk's own address
(DESK_HOSTS adds more, e.g. a VPS domain behind HTTPS).
"""
from __future__ import annotations

import difflib
import html
import ipaddress
import sys
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

from pipeline import categories
from pipeline.cluster import cluster_items
from pipeline.db import Database, connect
from pipeline.settings import COUNTRY_NAMES, get_settings

from . import auth
from . import pickup as pick_mod
from . import pull as pull_mod
from . import schedule as sched
from . import sources as src_mod
from . import workflow as wf
from . import blocks as blk
from .store import PROSE_KEYS, SECTION_TITLES, decode_revision, editor_blocks, get_story
from .users import ROLES, list_users

e = html.escape
DIMS = (("wallet_impact", "Wallet"), ("breadth", "Breadth"), ("actionability", "Action"),
        ("urgency", "Urgency"), ("novelty", "Novelty"))
ACTION_LABELS = {
    "approve": "Approve", "assign": "Assign to copy editor", "submit": "Submit for review",
    "send_back": "Send back to copy editor", "publish": "Publish", "unpublish": "Unpublish",
    "reopen": "Reopen for corrections", "kill": "Kill story",
}
PRIMARY = {"approve", "publish", "submit"}
PLACEHOLDER_PILL = '<span class="pill hold">Placeholder</span>'
AUTO_PILL = '<span class="pill hold">Auto-published</span>'
PICK_PILL = '<span class="pill s-in_review">Editor pick</span>'
ROLE_LABELS = {"copywriter": "Copy editor", "editor": "Editor", "publisher": "Publisher", "admin": "Admin"}
SESSION_COOKIE = "fh_session"
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = {"/static/editor.js": (STATIC_DIR / "editor.js", "text/javascript; charset=utf-8"),
                "/static/editor.css": (STATIC_DIR / "editor.css", "text/css; charset=utf-8")}


def role_label(role: str) -> str:
    return ROLE_LABELS.get(role, role)


# --------------------------------------------------------------------------- markdown
def _inline(text: str) -> str:
    out = e(text)
    out = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
                 lambda m: f'<a href="{m.group(2)}" target="_blank" rel="noopener">{m.group(1)}</a>', out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", out)
    return out


def render_markdown(md: str) -> str:
    parts: list[str] = []
    para: list[str] = []
    list_tag: str | None = None

    def flush():
        if para:
            parts.append(f"<p>{_inline(' '.join(para))}</p>")
            para.clear()

    def close():
        nonlocal list_tag
        if list_tag:
            parts.append(f"</{list_tag}>")
            list_tag = None

    for raw in md.splitlines():
        line = raw.rstrip()
        m_ol = re.match(r"^\d+\.\s+(.*)", line)
        m_ul = re.match(r"^[-*]\s+(.*)", line)
        if not line.strip():
            flush(); close(); continue
        m_h = re.match(r"^(#{3,6})\s+(.*)", line)
        if m_h:
            flush(); close(); parts.append(f"<h3>{_inline(m_h.group(2))}</h3>")
        elif line.startswith("# "):
            flush(); close(); parts.append(f"<h1>{_inline(line[2:])}</h1>")
        elif line.startswith("## "):
            flush(); close(); parts.append(f"<h2>{_inline(line[3:])}</h2>")
        elif line.strip() == "---":
            flush(); close(); parts.append("<hr>")
        elif m_ol or m_ul:
            flush()
            tag = "ol" if m_ol else "ul"
            if list_tag != tag:
                close(); parts.append(f"<{tag}>"); list_tag = tag
            parts.append(f"<li>{_inline((m_ol or m_ul).group(1))}</li>")
        else:
            close(); para.append(line.strip())
    flush(); close()
    return "\n".join(parts)


# --------------------------------------------------------------------------- layout
CSS = """
:root{--bg:#f6f5f1;--panel:#fff;--ink:#1c1b19;--muted:#6d6a63;--line:#e4e1d9;--accent:#0f5c4a;
--good:#0f7a4f;--good-bg:#e3f3ea;--warn:#9a6400;--warn-bg:#fbf0d9;--bad:#9b2c2c;--bad-bg:#f8e6e4;--info:#2f4f9a;--info-bg:#e7ecf8;
--serif:"Iowan Old Style","Charter","Georgia",serif;--sans:-apple-system,BlinkMacSystemFont,"Inter","Segoe UI",sans-serif}
@media (prefers-color-scheme:dark){:root{--bg:#141412;--panel:#1d1c1a;--ink:#ecebe6;--muted:#9c998f;--line:#302e2a;--accent:#5cc4a4;
--good:#6fd3a0;--good-bg:#18301f;--warn:#e6b75c;--warn-bg:#33290f;--bad:#ec8f86;--bad-bg:#3a1c19;--info:#9db4ef;--info-bg:#1c2540}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 var(--sans)}
a{color:var(--accent)}code{font-size:12px}
header{border-bottom:1px solid var(--line);background:var(--panel);position:sticky;top:0;z-index:5}
.bar{max-width:1240px;margin:0 auto;padding:12px 20px;display:flex;gap:20px;align-items:center;flex-wrap:wrap}
.brand{font-family:var(--serif);font-size:19px;font-weight:700}.brand small{font-family:var(--sans);font-size:11px;color:var(--muted);font-weight:600;letter-spacing:.08em;text-transform:uppercase;margin-left:6px}
nav{display:flex;gap:14px;flex-wrap:wrap}nav a{color:var(--muted);text-decoration:none;font-weight:500}nav a.on{color:var(--ink)}
.who{margin-left:auto;display:flex;gap:8px;align-items:center;font-size:13px;color:var(--muted)}
select,input[type=text],textarea{font:inherit;color:var(--ink);background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:6px 8px}
textarea{width:100%;line-height:1.5}input[type=text]{width:100%}
main{max-width:1240px;margin:0 auto;padding:22px 20px 60px}
h1.page{font-family:var(--serif);font-size:27px;margin:0 0 4px}.sub{color:var(--muted);margin:0 0 18px}
.flash{padding:10px 14px;border-radius:10px;margin-bottom:16px;font-weight:500}.flash.ok{background:var(--good-bg);color:var(--good)}.flash.err{background:var(--bad-bg);color:var(--bad)}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:16px}
.tab{border:1px solid var(--line);background:var(--panel);border-radius:999px;padding:5px 14px;font-size:13px;color:var(--muted);text-decoration:none;font-weight:500}
.tab b{margin-left:6px;color:var(--ink)}.tab.on{background:var(--ink);color:var(--panel);border-color:var(--ink)}.tab.on b{color:var(--panel)}
.row{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:10px;display:grid;grid-template-columns:60px 1fr auto;gap:14px;align-items:center}
.row .score{font-size:22px;font-weight:700;font-variant-numeric:tabular-nums;line-height:1}.row .score small{display:block;font-size:10px;color:var(--muted);font-weight:500}
.hl{font-weight:600;font-size:16px;margin:2px 0}.hl a{color:var(--ink);text-decoration:none}.hl a:hover{text-decoration:underline}
.meta{color:var(--muted);font-size:13px}
.pill{display:inline-block;border-radius:999px;padding:2px 10px;font-size:11.5px;font-weight:600;letter-spacing:.02em;white-space:nowrap}
.s-ai_draft,.s-in_review{background:var(--info-bg);color:var(--info)}.s-in_copy{background:var(--warn-bg);color:var(--warn)}
.s-approved,.s-published,.run{background:var(--good-bg);color:var(--good)}.s-unpublished,.s-killed,.reject{background:var(--bad-bg);color:var(--bad)}
.hold{background:var(--warn-bg);color:var(--warn)}.pill.cat{background:transparent;border:1px solid var(--line);color:var(--ink)}.pending{background:var(--line);color:var(--muted)}
.bad{color:var(--bad)}.brief{font-size:14px;line-height:1.6}.brief h3{font-size:13px;margin:14px 0 4px}.timeline-line{font-size:13px;color:var(--muted);margin:4px 0 2px}.timeline-line b{color:var(--ink);font-weight:600}.gr-ok{background:var(--good-bg);color:var(--good)}.gr-bad{background:var(--bad-bg);color:var(--bad)}
.layout{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:26px;align-items:start}
article{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:34px 42px;font-family:var(--serif);font-size:18px;line-height:1.65}
article h1{font-size:33px;line-height:1.15;margin:0 0 12px}article h2{font-family:var(--sans);font-size:12.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--accent);margin:30px 0 8px}
article h1+p em{font-size:20px;color:var(--muted);font-style:normal}article li{margin-bottom:6px}
.byline{display:flex;gap:12px;align-items:center;font-family:var(--sans);font-size:14px;margin:4px 0 20px;padding:12px 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.avatar{width:40px;height:40px;border-radius:50%;background:var(--accent);color:#fff;display:grid;place-items:center;font-weight:700;font-size:14px;flex:none}
article.blocks h2{font-family:var(--serif);font-size:26px;line-height:1.25;text-transform:none;letter-spacing:0;color:var(--ink);margin:34px 0 10px}
article.blocks h3{font-family:var(--serif);font-size:21px;margin:26px 0 8px}article .dek{font-size:21px;color:var(--muted);margin:0 0 18px}
article blockquote{margin:22px 0;border-left:3px solid var(--accent);padding:2px 0 2px 20px;font-style:italic}article blockquote p{margin:0}
article blockquote cite{display:block;font-style:normal;font-family:var(--sans);font-size:14px;color:var(--muted);margin-top:6px}
article hr{border:0;border-top:2px solid var(--line);margin:30px 0}.tablewrap{overflow-x:auto;margin:18px 0}
table.data{border-collapse:collapse;width:100%;font-family:var(--sans);font-size:15px;line-height:1.45}
table.data th,table.data td{border:1px solid var(--line);padding:7px 10px;text-align:left;vertical-align:top}table.data th{background:var(--bg);font-size:15px;color:var(--ink);text-transform:none;letter-spacing:0;font-weight:700}table.data{border-radius:0}
.disclosure{font-family:var(--sans);font-size:13px;color:var(--muted);border-top:1px solid var(--line);margin-top:28px;padding-top:14px}
aside .box{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:14px}
aside h3{font-size:11.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:0 0 10px}
.kv{display:grid;grid-template-columns:1fr auto;gap:4px 10px;font-size:13px}.kv span{color:var(--muted)}.kv b{text-align:right;font-variant-numeric:tabular-nums}
.issue{font-size:13px;padding:8px 10px;border-radius:8px;margin-bottom:6px}.issue.error{background:var(--bad-bg);color:var(--bad)}.issue.warning{background:var(--warn-bg);color:var(--warn)}
.okbox{background:var(--good-bg);color:var(--good);padding:8px 10px;border-radius:8px;font-size:13px;font-weight:600}
button,.btn{font:600 13px var(--sans);border:1px solid var(--line);background:var(--bg);color:var(--ink);border-radius:8px;padding:7px 12px;cursor:pointer;text-decoration:none;display:inline-block}
button.primary,.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}button.danger{color:var(--bad)}
form.act{border-top:1px solid var(--line);padding-top:10px;margin-top:10px}form.act:first-of-type{border-top:0;margin-top:0;padding-top:0}
form.act label{display:block;font-size:12px;color:var(--muted);margin:6px 0 4px}form.act textarea{height:54px;font-size:13px}
.timeline{font-size:13px}.timeline div{padding:6px 0;border-bottom:1px solid var(--line)}.timeline div:last-child{border:0}.timeline span{color:var(--muted)}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}
th,td{text-align:left;padding:10px 14px;border-bottom:1px solid var(--line);font-size:14px;vertical-align:top}th{font-size:11.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.empty{color:var(--muted);padding:36px;text-align:center;background:var(--panel);border:1px dashed var(--line);border-radius:12px}
.edit label{display:block;font-weight:600;margin:16px 0 6px}.edit .hint{font-weight:400;color:var(--muted);font-size:13px}
.diff{font:13px/1.55 ui-monospace,Menlo,monospace;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px;white-space:pre-wrap;word-break:break-word}
.diff .add{background:var(--good-bg);color:var(--good);display:block}.diff .del{background:var(--bad-bg);color:var(--bad);display:block;text-decoration:line-through}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:10px;display:grid;grid-template-columns:70px 1fr 220px;gap:16px}
.big{font-size:24px;font-weight:700;font-variant-numeric:tabular-nums}.angle{font-family:var(--serif);margin:6px 0 0}.why{color:var(--muted);font-size:13px;margin-top:4px}
.dims{display:grid;grid-template-columns:58px 1fr 12px;gap:3px 8px;font-size:12px;color:var(--muted);align-items:center}
.dims .b{height:6px;background:var(--line);border-radius:3px;overflow:hidden}.dims .b i{display:block;height:100%;background:var(--muted)}.dims b{color:var(--ink)}
.flag{display:inline-block;font-size:11px;border:1px solid var(--line);border-radius:4px;padding:0 6px;margin:4px 4px 0 0;color:var(--muted)}
button.link{background:none;border:0;padding:0 0 0 6px;color:var(--muted);font:500 13px var(--sans);cursor:pointer}button.link:hover{color:var(--ink)}
.who a{color:var(--ink);text-decoration:none;font-weight:600}
.auth{max-width:380px;margin:60px auto;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:28px}
.auth h1{font-family:var(--serif);margin:0 0 16px;font-size:24px}.auth label,.form label{display:block;font-size:13px;color:var(--muted);margin:12px 0 4px}
.auth input,.form input[type=text],.form input[type=email],.form input[type=password],.form select{width:100%}
input[type=email],input[type=password]{font:inherit;color:var(--ink);background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:7px 9px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:20px;align-items:start}.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-bottom:16px}
.panel h2{font-size:15px;margin:0 0 10px}.secret{font:600 18px ui-monospace,Menlo,monospace;background:var(--warn-bg);color:var(--warn);padding:10px 14px;border-radius:8px;display:inline-block;letter-spacing:.04em}
.inline{display:inline}.row-actions{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
@media (max-width:920px){.grid2{grid-template-columns:1fr}}
.banner{background:var(--info-bg);color:var(--info);border-radius:10px;padding:10px 14px;margin-bottom:16px;font-size:14px}
@media (max-width:920px){.layout{grid-template-columns:1fr}.row{grid-template-columns:48px 1fr}.row>div:last-child{grid-column:1/-1}
article{padding:22px 18px;font-size:17px}article h1{font-size:26px}.card{grid-template-columns:56px 1fr}.card .dims{grid-column:1/-1}.who{margin-left:0}}
"""


def page(title: str, active: str, body: str, user: dict | None, flash: tuple[str, str] | None = None,
         refresh: int | None = None, wide: bool = False) -> str:
    links = [("queue", "/", "Stories"), ("scoring", "/scoring", "Scoring desk"), ("sources", "/sources", "Sources & pulls"),
             ("site", "/site", "Site feed"), ("team", "/team", "Team")]
    nav = "".join(f'<a href="{href}" class="{"on" if key == active else ""}">{e(label)}</a>' for key, href, label in links) if user else ""
    who = ""
    if user:
        who = (f'<div class="who"><a href="/account" class="{"on" if active == "account" else ""}">{e(user["display_name"])}</a>'
               f'<span class="pill pending">{e(role_label(user["role"]))}</span>'
               '<form method="post" action="/logout" style="display:inline"><button class="link">Sign out</button></form></div>')
    fl = f'<div class="flash {flash[0]}">{e(flash[1])}</div>' if flash else ""
    meta = f'<meta http-equiv="refresh" content="{int(refresh)}">' if refresh else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">{meta}
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)} · Newsroom</title>
<style>{CSS}</style></head><body><header><div class="bar"><div class="brand">{e(wf.publication_name())}<small>Newsroom</small></div>
<nav>{nav}</nav>{who}</div></header><main{' class="wide"' if wide else ''}>{fl}{body}</main></body></html>"""


def status_pill(status: str) -> str:
    return f'<span class="pill s-{e(status)}">{e(wf.LABELS.get(status, status))}</span>'


def gr_pill(g: dict | None) -> str:
    g = g or {}
    errors = sum(1 for i in g.get("issues", []) if i.get("severity") == "error")
    if not g:
        return ""
    return ('<span class="pill gr-ok">Guardrails passed</span>' if not errors
            else f'<span class="pill gr-bad">{errors} guardrail error{"s" if errors > 1 else ""}</span>')


def initials(name: str) -> str:
    return "".join(p[0] for p in name.split()[:2]).upper()


def when(ts: str | None, with_date: bool = True) -> str:
    """ISO timestamp (UTC) -> this computer's local time, e.g. '15 Oct 14:28'."""
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return str(ts)[:16].replace("T", " ")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone()
    return local.strftime("%d %b %H:%M" if with_date else "%H:%M").lstrip("0")


def tz_label() -> str:
    return datetime.now().astimezone().strftime("%Z") or "local time"


def dims_html(r: dict, category: str | None = None) -> str:
    """The desk's dimension scores, labelled with the category's own rubric."""
    cat = category or r.get("category")
    scores = Database.unj(r.get("scores"), None) or {k: r.get(k) for k, _ in DIMS if r.get(k) is not None}
    dims = [(d["key"], d.get("label") or d["key"]) for d in categories.dimensions(cat)] or list(DIMS)
    rows = [f'<span>{e(label)}</span><span class="b"><i style="width:{int(scores[k]) * 20}%"></i></span><b>{int(scores[k])}</b>'
            for k, label in dims if scores.get(k) is not None]
    return f'<div class="dims">{"".join(rows)}</div>' if rows else "<div></div>"


def cat_pill(cat: str | None) -> str:
    cat = cat or categories.default()
    return f'<span class="pill cat">{e(categories.name(cat))}</span>'


def in_scope(rows: list[dict], user: dict | None, key: str = "category") -> list[dict]:
    """Only the rows in categories this person works on (no user: internal call, everything)."""
    if user is None:
        return rows
    scope = categories.user_scope(user)
    if scope is None:
        return rows
    return [r for r in rows if (r.get(key) or categories.default()) in scope]


def category_chips(q: dict, base: str, user: dict | None, counts: dict | None = None) -> str:
    opts = [(c, n) for c, n in categories.choices() if categories.can_access(user, c)]
    if len(opts) < 2:
        return ""
    return f'<div class="tabs">{_filter_chips(q, base, "cat", [("", "All categories")] + opts, counts)}</div>'


def no_access(cat: str | None) -> tuple[str, str]:
    return ("Not your category", f'<div class="empty">This is a {e(categories.name(cat))} story, and you don\'t work on '
            f'{e(categories.name(cat))}. An admin can change your categories on the Team page.</div>')


# --------------------------------------------------------------------------- pages
def queue_page(db: Database, actor: dict | None, q: dict) -> tuple[str, str]:
    scope = categories.user_scope(actor)
    cat = q.get("cat", [""])[0]
    only = {cat} if cat and categories.can_access(actor, cat) else scope
    counts = wf.queue_counts(db, only)
    key = q.get("q", [wf.DEFAULT_QUEUE.get(actor["role"], "editor") if actor else "editor"])[0]
    mine = q.get("mine", [""])[0] == "1"
    tabs = []
    current = wf.QUEUES[0]
    cat_qs = f"&cat={quote(cat)}" if cat else ""
    for k, label, statuses in wf.QUEUES:
        n = sum(counts.get(s, 0) for s in statuses)
        if k == key:
            current = (k, label, statuses)
        tabs.append(f'<a class="tab {"on" if k == key else ""}" href="/?q={k}{cat_qs}">{label}<b>{n}</b></a>')
    if actor and actor["role"] == "copywriter":
        tabs.append(f'<a class="tab {"on" if mine else ""}" href="/?q=copy&mine=1{cat_qs}">Assigned to me</a>')
    rows = wf.queue(db, current[2], assigned_to=actor["id"] if (mine and actor) else None, only_categories=only)
    items = []
    for r in rows:
        score = f'{r["impact_score"]:.0f}' if r.get("impact_score") is not None else "–"
        who = []
        if r.get("assignee_name"):
            who.append(f"Copywriter: {e(r['assignee_name'])}")
        if r.get("editor_name"):
            who.append(f"Editor: {e(r['editor_name'])}")
        origin = "AI draft" if r["author_kind"] == "ai" and r["current_version"] == 1 else f"v{r['current_version']}"
        items.append(f"""<div class="row"><div class="score">{score}<small>impact</small></div>
<div><div class="meta">{cat_pill(r.get('category'))} {status_pill(r['status'])} {gr_pill(r['guardrails'])} {AUTO_PILL if r.get('auto_published') else ''}{PICK_PILL if r.get('origin') == 'editor_pick' else ''} &nbsp;{e(COUNTRY_NAMES.get(r['country'], r['country']))} · {origin} · {r.get('word_count') or 0} words · updated {when(r['updated_at'])}</div>
<div class="hl"><a href="/story/{r['id']}">{e(r['headline'])}</a></div>
<div class="meta">{' · '.join(who) or e(r.get('angle') or '')}</div></div>
<div><a class="btn" href="/story/{r['id']}">Open</a></div></div>""")
    body = (f'<h1 class="page">Stories</h1><p class="sub">AI drafts arrive here from the pipeline. Editors approve or assign them; '
            f'publishers put approved stories live.' + (f' You work on: {e(categories.scope_label(actor))}.' if scope is not None else '')
            + f'</p>{category_chips(q, "/", actor)}<div class="tabs">{"".join(tabs)}</div>'
            + ("".join(items) or '<div class="empty">Nothing in this queue.</div>'))
    return "Stories", body


def _safe_next(url: str | None, default: str) -> str:
    """Only return to our own story or site pages."""
    if url and re.fullmatch(r"/(story/\d+|site/[\w-]+)", url):
        return url
    return default


def actions_box(db: Database, story: dict, actor: dict, g: dict | None = None, here: str | None = None) -> str:
    """Edit button plus every workflow action this person can take now."""
    story_id = story["id"]
    g = g if g is not None else ((story.get("revision") or {}).get("guardrails") or {})
    here = here or f"/story/{story_id}"
    allowed = wf.allowed_actions(story, actor)
    errors = [i for i in g.get("issues", []) if i.get("severity") == "error"]
    forms = []
    if wf.can_edit(story, actor):
        label = "Edit live story" if story.get("live") else "Edit story"
        forms.append(f'<a class="btn primary" style="width:100%;text-align:center" '
                     f'href="/story/{story_id}/edit?next={quote(here)}">{label}</a>')
    if story.get("live") and story["status"] != "published":
        forms.append(f'<div class="meta" style="margin:8px 0">The site is showing v{story.get("live_version")}. '
                     f'v{story["current_version"]} is a correction in progress: it goes live when it is approved and republished.</div>')
    copywriters = None
    for a in allowed:
        extra = ""
        if a == "assign":
            copywriters = copywriters if copywriters is not None else [
                u for u in list_users(db, "copywriter") if categories.can_access(u, story.get("category"))]
            if not copywriters:
                forms.append(f'<div class="meta">No copy editors work on {e(categories.name(story.get("category")))} yet. '
                             'Add one, or give one this category, on the Team page.</div>')
                continue
            opts = "".join(f'<option value="{u["id"]}" {"selected" if u["id"] == story.get("assigned_to") else ""}>{e(u["display_name"])}</option>' for u in copywriters)
            extra = f'<label>Copy editor</label><select name="assignee_id" style="width:100%">{opts}</select>'
        if a == "approve" and errors:
            extra = (f'<label style="color:var(--bad)">This version has {len(errors)} guardrail error(s).</label>'
                     '<label><input type="checkbox" name="override" value="1"> Approve anyway (reason required below)</label>')
        if a == "publish" and story.get("published_version"):
            extra = '<label>Correction / update note shown on the site (optional)</label><input type="text" name="correction_note">'
        if a == "unpublish":
            extra = '<label class="meta">Takes it off the site straight away. It stays in the newsroom, where it can be reopened.</label>'
        danger = "danger" if a in ("kill", "unpublish") else ""
        forms.append(f'''<form class="act" method="post" action="/story/{story_id}/action">
<input type="hidden" name="action" value="{a}"><input type="hidden" name="next" value="{e(here)}">{extra}<label>Note (optional)</label><textarea name="note"></textarea>
<button class="{'primary' if a in PRIMARY else ''} {danger}">{ACTION_LABELS[a]}</button></form>''')
    if not forms:
        forms.append(f'<div class="meta">Nothing for a {e(role_label(actor["role"]).lower())} to do at this stage.</div>')
    return f'<div class="box"><h3>Actions · {e(actor["display_name"])}</h3>{"".join(forms)}</div>'


def origin_timeline(db: Database, story: dict) -> str:
    """How the story came to be: feed items, desk scoring, pick-up, drafts, edits and workflow steps."""
    entries: list[tuple[str, int, str, str]] = []   # (time, order, who, what)
    cid = story.get("cluster_id")
    if cid:
        for it in cluster_items(db, cid):
            src_time = f' (published at source {when(it["published_at"])})' if it.get("published_at") else ""
            entries.append((str(it["fetched_at"]), 0, "Feed pull",
                            f'Pulled from <b>{e(it["source_name"])}</b>: <a href="{e(it["url"])}" target="_blank" rel="noopener">'
                            f'{e(it["title"])}</a>{src_time}'))
        for ev in db.fetchall("select * from pipeline.evaluations where cluster_id = ? order by id", (cid,)):
            score = f'{ev["impact_score"]:.0f}' if ev.get("impact_score") is not None else "–"
            entries.append((str(ev["created_at"]), 1, "Scoring desk (AI)",
                            f'Scored <b>{score}</b> → <b>{e(ev["decision"])}</b>. {e(ev.get("angle") or "")}'))
        for pr in db.fetchall(
                """select p.*, u.display_name from newsroom.pick_requests p join newsroom.users u on u.id = p.requested_by
                   where p.cluster_id = ? order by p.id""", (cid,)):
            entries.append((str(pr["created_at"]), 2, pr["display_name"],
                            f'Picked it up over the desk{" with AI research" if pr["research"] else ""}. Angle: {e(pr["angle"])}'
                            + (f' · <span class="bad">failed: {e(pr["error"])}</span>' if pr["status"] == "failed" else "")))
    revs = {r["version"]: r for r in db.fetchall(
        """select r.version, r.author_kind, r.note, r.created_at, r.word_count, u.display_name
           from newsroom.story_revisions r left join newsroom.users u on u.id = r.author_id where r.story_id = ?""", (story["id"],))}
    for v, r in revs.items():
        who = "AI writer" if r["author_kind"] == "ai" else (r["display_name"] or "")
        what = (f'<a href="/story/{story["id"]}?v={v}">v{v}</a> saved · {r["word_count"] or 0} words'
                + (f' · {e(r["note"])}' if r.get("note") else "") + (f' · {diff_link(story["id"], v)}' if v > 1 else ""))
        entries.append((str(r["created_at"]), 3, who, what))
    for ev in db.fetchall(
            """select ev.*, u.display_name from newsroom.story_events ev left join newsroom.users u on u.id = ev.actor_id
               where ev.story_id = ? order by ev.id""", (story["id"],)):
        if ev["action"] in ("drafted", "edited"):
            continue  # shown with the version
        move = (f' {status_pill(ev["from_status"]) if ev.get("from_status") else ""} → {status_pill(ev["to_status"])}'
                if ev.get("to_status") else "")
        entries.append((str(ev["created_at"]), 4, ev["display_name"] or "Newsroom (automatic)",
                        f'<b>{e(ev["action"].replace("_", " ").capitalize())}</b>{move}' + (f' · {e(ev["note"])}' if ev.get("note") else "")))
    def _key(x):
        try:
            dt = datetime.fromisoformat(x[0].replace("Z", "+00:00"))
            dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            dt = datetime.min.replace(tzinfo=timezone.utc)
        return (dt, x[1])
    entries.sort(key=_key)
    rows = "".join(f'<tr><td style="white-space:nowrap">{when(t)}</td><td>{e(who)}</td><td>{what}</td></tr>' for t, _, who, what in entries)
    return (f'<div class="panel" style="margin-top:20px"><h2>How this story came to be</h2>'
            f'<p class="meta">From the feed to the site, oldest first. Times in {tz_label()}.</p>'
            f'<table><tr><th>When</th><th>Who</th><th>What happened</th></tr>{rows}</table></div>')


def story_page(db: Database, story_id: int, actor: dict | None, version: int | None = None) -> tuple[str, str] | None:
    story = get_story(db, story_id)
    if not story:
        return None
    if not categories.can_access(actor, story.get("category")):
        return no_access(story.get("category"))
    rev = story["revision"]
    if version and version != story["current_version"]:
        r = db.fetchone("select * from newsroom.story_revisions where story_id = ? and version = ?", (story_id, version))
        if not r:
            return None
        rev = decode_revision(r)
    g = rev.get("guardrails") or {}
    viewing_old = rev["version"] != story["current_version"]

    actions_html = actions_box(db, story, actor, g, here=f"/story/{story_id}") if (actor and not viewing_old) else ""

    issues = g.get("issues", [])
    m = g.get("metrics", {})
    issues_html = "".join(f'<div class="issue {e(i["severity"])}">{e(i["detail"])}</div>' for i in issues) or (
        '<div class="okbox">Passed every check</div>' if g else '<div class="meta">Not checked</div>')
    kv = "".join(f"<span>{k}</span><b>{e(str(v))}</b>" for k, v in (
        ("Words", m.get("word_count", rev.get("word_count"))),
        ("Phrase overlap with source", f'{(m.get("ngram_overlap") or 0):.0%}'),
        ("Longest copied run", f'{m.get("longest_shared_run", 0)} words'),
        ("Unsupported figures", len(m.get("unsupported_numbers") or [])),
    ))

    people = db.fetchall(
        "select id, display_name from newsroom.users where id in (?, ?, ?)",
        (story.get("assigned_to") or 0, story.get("editor_id") or 0, story.get("picked_by") or 0))
    names = {p["id"]: p["display_name"] for p in people}
    site_row = db.fetchone("select status, version, updated_at from site.published_stories where story_id = ?", (story_id,))
    live = (f'<span class="pill s-published">Live v{site_row["version"]}</span>' if site_row and site_row["status"] == "live"
            else ('<span class="pill s-unpublished">Retracted</span>' if site_row else "Not on site"))
    info = "".join(f"<span>{k}</span><b>{v}</b>" for k, v in (
        ("Status", status_pill(story["status"]) + (" " + AUTO_PILL if story.get("auto_published") else "")
         + (" " + PICK_PILL if story.get("origin") == "editor_pick" else "")), ("On the site", live),
        ("Version", f'v{story["current_version"]}'), ("Copy editor", e(names.get(story.get("assigned_to"), "—"))),
        ("Responsible editor" if story.get("auto_published") and story["status"] == "published" else "Byline (approving editor)",
         e(names.get(story.get("editor_id"), "—"))),
        ("Category", cat_pill(story.get("category"))),
        ("Country", e(story["country"])), ("Impact score", f'{story["impact_score"]:.0f}' if story.get("impact_score") is not None else "—"),
    ))
    events = db.fetchall(
        """select ev.*, u.display_name from newsroom.story_events ev left join newsroom.users u on u.id = ev.actor_id
           where ev.story_id = ? order by ev.id desc limit 8""", (story_id,))
    tl = "".join(f'<div><b>{e(ev["display_name"] or "Pipeline")}</b> {e(ev["action"].replace("_", " "))}'
                 f'{(" → " + e(wf.LABELS.get(ev["to_status"], ev["to_status"]))) if ev.get("to_status") else ""}'
                 f'<br><span>{when(ev["created_at"])}{(" · " + e(ev["note"])) if ev.get("note") else ""}</span></div>' for ev in events)

    ev_row = None
    if story.get("cluster_id"):
        ev_row = db.fetchone("select * from pipeline.evaluations where cluster_id = ? order by id desc limit 1", (story["cluster_id"],))
    desk = ""
    if ev_row:
        desk = (f'<div class="box"><h3>How the desk scored it</h3>{dims_html(ev_row, story.get("category"))}'
                f'<p class="angle" style="font-size:14px">{e(ev_row.get("angle") or "")}</p><div class="why">{e(ev_row.get("rationale") or "")}</div></div>')

    research_html = ""
    rs = Database.unj(story.get("research"), None)
    if rs:
        srcs = "".join(f'<li><a href="{e(x["url"])}" target="_blank" rel="noopener">{e(x.get("title") or x["url"])}</a>'
                       f' · {e(x.get("name") or "")}</li>' for x in rs.get("sources") or [])
        picker = names.get(story.get("picked_by")) or ""
        research_html = (f'<div class="panel" style="margin-top:20px"><h2>Editor pick · research brief</h2>'
                         f'<p class="meta">Picked up{" by " + e(picker) if picker else ""} over the desk\'s '
                         f'{e(rs.get("desk_decision") or "decision")}'
                         f'{" (" + format(rs["desk_score"], ".0f") + ")" if rs.get("desk_score") is not None else ""}. '
                         f'Angle: {e(rs.get("angle") or "")}</p>'
                         + (f'<p><b>Editor\'s notes:</b> {e(rs["notes"])}</p>' if rs.get("notes") else "")
                         + (f'<div class="brief">{render_markdown(rs["text"])}</div>' if rs.get("text") else "")
                         + (f'<h3>Research sources</h3><ul>{srcs}</ul>' if srcs else "")
                         + (f'<p class="meta">Searches: {e("; ".join(rs.get("queries") or []))}</p>' if rs.get("queries") else "")
                         + '</div>')
    banner = (f'<div class="banner">You are viewing version {rev["version"]}. <a href="/story/{story_id}">Back to the current version (v{story["current_version"]})</a></div>'
              if viewing_old else "")
    on_site = (f' · <a href="/site/{quote(story["slug"])}">View on site</a>' if site_row else "")
    body = f"""<p class="sub"><a href="/">← Stories</a> · <a href="/story/{story_id}/history">History &amp; versions</a>{on_site} · slug <code>{e(story['slug'])}</code></p>
{banner}<div class="layout"><div>{article_html(rev)}{research_html}{origin_timeline(db, story)}</div>
<aside>{actions_html}<div class="box"><h3>Story</h3><div class="kv">{info}</div>{move_category_form(story, actor)}</div>
<div class="box"><h3>Guardrails · v{rev['version']}</h3>{issues_html}<div class="kv" style="margin-top:10px">{kv}</div></div>
{desk}<div class="box"><h3>Recent activity</h3><div class="timeline">{tl}</div></div></aside></div>"""
    return rev["headline"], body


def move_category_form(story: dict, actor: dict | None) -> str:
    """Editors and admins can move a story to another category they work on."""
    if not actor or actor["role"] not in ("editor", "admin") or story["status"] == "killed":
        return ""
    cur = story.get("category") or categories.default()
    opts = [(c, n) for c, n in categories.choices() if categories.can_access(actor, c)]
    if len(opts) < 2 or not categories.can_access(actor, cur):
        return ""
    options = "".join(f'<option value="{e(c)}" {"selected" if c == cur else ""}>{e(n)}</option>' for c, n in opts)
    return (f'<form class="act" method="post" action="/story/{story["id"]}/category" style="margin-top:12px">'
            f'<label>Move to another category</label><select name="category" style="width:100%">{options}</select>'
            '<p class="meta" style="margin:6px 0">Changes the queue and the site section. The text keeps its current '
            'section headings until it is edited.</p><button>Move story</button></form>')


def edit_page(db: Database, story_id: int, actor: dict | None, next_url: str | None = None) -> tuple[str, str] | None:
    """The block editor (WordPress-style): toolbar, writing canvas, settings sidebar."""
    story = get_story(db, story_id)
    if not story:
        return None
    if not categories.can_access(actor, story.get("category")):
        return no_access(story.get("category"))
    if not wf.can_edit(story, actor):
        return "Can't edit", f'<div class="empty">You can\'t edit this story at its current stage. <a href="/story/{story_id}">Back</a></div>'
    rev = story["revision"]
    back = _safe_next(next_url, f"/story/{story_id}")
    after = wf.status_after_edit(story, actor)
    nv = story["current_version"] + 1
    role = actor["role"]

    banner = ""
    if story.get("live"):
        banner = (f'Readers see v{story["live_version"]}. Saving makes a correction (v{nv}); readers keep seeing '
                  f'v{story["live_version"]} until it is approved and republished.'
                  + (" It goes to you as copy editor; send it to an editor when you're done." if after == "in_copy" else ""))
    elif story["status"] == "approved":
        banner = "This story is approved. Saving changes sends it back to review for approval again."
    elif story["status"] == "unpublished":
        banner = "This story is unpublished. Saving reopens it for review; it goes back on the site only when approved and republished."
    elif after == "in_copy" and story["status"] != "in_copy":
        banner = "Saving assigns this story to you as copy editor. Send it to an editor when you're done."
    if not rev.get("blocks"):
        banner = ("This is the AI's version, opened as blocks. When you save, your version is stored as blocks and "
                  "that is what the site shows once it's published. " + banner)
    banner_html = f'<div class="banner ed-banner">{e(banner)}</div>' if banner else ""

    # toolbar buttons by role
    primary = ""
    if role == "admin":
        primary = (f'<button class="primary" form="editform" name="then" value="publish">'
                   f'Save &amp; {"update live story" if story.get("live") else "publish"}</button>')
    elif role == "editor":
        primary = '<button class="primary" form="editform" name="then" value="approve">Save &amp; approve</button>'
    elif role == "copywriter" and (after == "in_copy" or story["status"] == "in_copy"):
        primary = '<button class="primary" form="editform" name="then" value="submit">Save &amp; send to editor</button>'
    save_cls = "" if primary else ' class="primary"'
    toolbar = f"""<div class="ed-top"><a class="btn" href="{e(back)}">← Back</a>
<div class="ttl">{e(story['country'])} story<small>v{story['current_version']} · saving creates v{nv}</small></div>
{status_pill(story['status'])}{(' <span class="pill s-published">Live v' + str(story['live_version']) + '</span>') if story.get('live') else ''}
<div class="grow"></div><span id="ed-msg" class="ed-msg" role="status"></span><span id="ed-wc" class="ed-wc"></span>
<button id="ed-save" form="editform" name="then" value="save"{save_cls}>Save draft</button>{primary}</div>"""

    # sidebar
    names = {u["id"]: u["display_name"] for u in db.fetchall(
        "select id, display_name from newsroom.users where id in (?, ?)", (story.get("assigned_to") or 0, story.get("editor_id") or 0))}
    status_kv = "".join(f"<span>{k}</span><b>{v}</b>" for k, v in (
        ("Category", e(categories.name(story.get("category")))),
        ("Status", e(wf.LABELS[story["status"]])),
        ("On the site", f'Live v{story["live_version"]}' if story.get("live") else "Not live"),
        ("Copy editor", e(names.get(story.get("assigned_to"), "—"))),
        ("Byline", e(names.get(story.get("editor_id"), "set when approved"))),
        ("After saving", e(wf.LABELS[after])),
    ))
    srcs = "".join(f'<li><a href="{e(x["url"])}" target="_blank" rel="noopener">{e(x.get("title") or x["url"])}</a> · {e(x.get("name") or "")}</li>'
                   for x in rev["sources"]) or "<li>None</li>"
    g = rev.get("guardrails") or {}
    issues = "".join(f'<div class="issue {e(i["severity"])}">{e(i["detail"])}</div>' for i in g.get("issues", [])) or (
        '<div class="okbox">Passed every check</div>' if g else '<div class="meta">Not checked yet</div>')
    correction = ""
    if role == "admin" and story.get("published_version"):
        correction = ('<section><h3>Correction note</h3><input type="text" form="editform" name="correction_note" '
                      'placeholder="Shown on the site, e.g. Updated the rate figures"><p class="meta">Used with “Save &amp; update live story”.</p></section>')
    rs = Database.unj(story.get("research"), None)
    research = ""
    if rs and rs.get("text"):
        rsrc = "".join(f'<li><a href="{e(x["url"])}" target="_blank" rel="noopener">{e(x.get("title") or x["url"])}</a></li>' for x in rs.get("sources") or [])
        research = (f'<section><details><summary>Research brief</summary><div class="brief">{render_markdown(rs["text"])}</div>'
                    + (f"<ul>{rsrc}</ul>" if rsrc else "") + "</details></section>")
    side = f"""<aside class="ed-side">
<section><h3>Story</h3><div class="kv">{status_kv}</div><p style="margin:10px 0 0"><a href="/story/{story_id}">Story page &amp; actions</a> · <a href="/story/{story_id}/history">History</a></p></section>
<section><h3>What did you change?</h3><input type="text" form="editform" name="note" placeholder="Saved with this version, e.g. Tightened the intro"></section>
{correction}
<section><h3>Search &amp; sharing <span id="ed-meta-count" class="ed-count"></span></h3>
<textarea form="editform" name="meta_description" rows="3" placeholder="About 155 characters for search results">{e(rev.get('meta_description') or '')}</textarea></section>
<section><h3>Sources</h3><ul>{srcs}</ul><p class="meta">Listed at the end of the story.</p></section>
<section><h3>Checks on v{rev['version']}</h3>{issues}<p class="meta">Re-run when you save.</p></section>
{research}
<section><h3>Shortcuts</h3><div class="keys">
<kbd>/</kbd><span>in an empty line: add a block</span><kbd>## </kbd><span>heading</span><kbd>### </kbd><span>subheading</span>
<kbd>- </kbd><span>bulleted list</span><kbd>1. </kbd><span>numbered list</span><kbd>&gt; </kbd><span>quote</span><kbd>---</kbd><span>divider</span>
<kbd>⌘B</kbd><span>bold</span><kbd>⌘I</kbd><span>italic</span><kbd>⌘K</kbd><span>link (select text first)</span><kbd>⌘S</kbd><span>save draft</span>
</div></section></aside>"""

    cfgs = categories.guardrails(story.get("category"))
    data = {"story_id": story_id, "version": story["current_version"], "headline": rev["headline"], "dek": rev.get("dek") or "",
            "blocks": editor_blocks(rev, story.get("category")), "min_words": cfgs["min_words"], "max_words": cfgs["max_words"]}
    data_json = json.dumps(data).replace("<", "\\u003c")
    body = f"""<link rel="stylesheet" href="/static/editor.css">
{toolbar}
<form id="editform" method="post" action="/story/{story_id}/edit">
<input type="hidden" name="next" value="{e(back)}"><input type="hidden" name="headline"><input type="hidden" name="dek"><input type="hidden" name="blocks"></form>
<div class="ed-wrap"><div class="ed-canvas">
<div id="ed-restore" class="ed-restore" hidden><span></span><button type="button" data-act="restore">Restore them</button><button type="button" data-act="discard">Discard</button></div>
{banner_html}
<div class="ed-paper">
<div id="ed-headline" class="ed-title" contenteditable="true" role="textbox" aria-label="Headline" data-placeholder="Add a headline"></div>
<div id="ed-dek" class="ed-dek" contenteditable="true" role="textbox" aria-label="Standfirst" data-placeholder="Standfirst: one sentence on what this means for readers' money"></div>
<div id="ed-blocks" aria-label="Story body"></div>
<button type="button" id="ed-add" class="ed-add">+ Add block</button>
</div></div>{side}</div>
<script id="ed-data" type="application/json">{data_json}</script>
<script src="/static/editor.js"></script>"""
    return "Edit · " + rev["headline"], body


def article_html(rev: dict) -> str:
    """A version as an article: block versions render their blocks, AI versions their markdown."""
    if rev.get("blocks"):
        dek = f'<p class="dek">{e(rev["dek"])}</p>' if rev.get("dek") else ""
        return (f'<article class="blocks"><h1>{e(rev["headline"])}</h1>{dek}{blk.render_blocks(rev["blocks"])}'
                f'{blk.render_sources(rev.get("sources") or [])}</article>')
    return f"<article>{render_markdown(rev['body_md'])}</article>"


def diff_link(story_id: int, version: int) -> str:
    return f'<a href="/story/{story_id}/diff/{version}">diff</a>' if version > 1 else ""


def history_page(db: Database, story_id: int, actor: dict | None = None) -> tuple[str, str] | None:
    story = get_story(db, story_id)
    if not story:
        return None
    if actor is not None and not categories.can_access(actor, story.get("category")):
        return no_access(story.get("category"))
    revs = db.fetchall(
        """select r.version, r.author_kind, r.note, r.created_at, r.word_count, r.guardrails, u.display_name
           from newsroom.story_revisions r left join newsroom.users u on u.id = r.author_id
           where r.story_id = ? order by r.version desc""", (story_id,))
    rows = "".join(
        f'<tr><td><a href="/story/{story_id}?v={r["version"]}">v{r["version"]}</a></td>'
        f'<td>{"AI (pipeline)" if r["author_kind"] == "ai" else e(r["display_name"] or "")}</td><td>{e(r.get("note") or "")}</td>'
        f'<td>{gr_pill(Database.unj(r["guardrails"], {}))}</td><td>{r["word_count"] or 0}</td><td>{when(r["created_at"])}</td>'
        f'<td>{diff_link(story_id, r["version"])}</td></tr>'
        for r in revs)
    events = db.fetchall(
        """select ev.*, u.display_name, u.role from newsroom.story_events ev left join newsroom.users u on u.id = ev.actor_id
           where ev.story_id = ? order by ev.id desc""", (story_id,))
    ev_rows = "".join(
        f'<tr><td>{when(ev["created_at"])}</td><td>{e(ev["display_name"] or "Pipeline")}<br><span class="meta">{e(ev.get("role") or "system")}</span></td>'
        f'<td>{e(ev["action"].replace("_", " "))}</td><td>{status_pill(ev["from_status"]) if ev.get("from_status") else ""} → '
        f'{status_pill(ev["to_status"]) if ev.get("to_status") else ""}</td><td>{e(ev.get("note") or "")}</td></tr>' for ev in events)
    body = f"""<p class="sub"><a href="/story/{story_id}">← Back to story</a></p><h1 class="page">{e(story['revision']['headline'])}</h1>
<p class="sub">Every version and every workflow step, with who did it.</p>
<h3>Versions</h3><table><tr><th>Version</th><th>Author</th><th>Note</th><th>Guardrails</th><th>Words</th><th>Saved</th><th></th></tr>{rows}</table>
<h3 style="margin-top:28px">Audit trail</h3><table><tr><th>When</th><th>Who</th><th>Action</th><th>Status</th><th>Note</th></tr>{ev_rows}</table>"""
    return "History", body


def diff_page(db: Database, story_id: int, version: int, actor: dict | None = None) -> tuple[str, str] | None:
    if actor is not None:
        st = db.fetchone("select category from newsroom.stories where id = ?", (story_id,))
        if st and not categories.can_access(actor, st.get("category")):
            return no_access(st.get("category"))
    a = db.fetchone("select body_md from newsroom.story_revisions where story_id = ? and version = ?", (story_id, version - 1))
    b = db.fetchone("select body_md from newsroom.story_revisions where story_id = ? and version = ?", (story_id, version))
    if not a or not b:
        return None
    out = []
    for line in difflib.ndiff(a["body_md"].splitlines(), b["body_md"].splitlines()):
        tag, text = line[:2], line[2:]
        if tag == "? ":
            continue
        cls = {"+ ": "add", "- ": "del"}.get(tag)
        out.append(f'<span class="{cls}">{e(text) or "&nbsp;"}</span>' if cls else e(text) + "\n")
    body = (f'<p class="sub"><a href="/story/{story_id}/history">← History</a></p><h1 class="page">Changes in v{version}</h1>'
            f'<p class="sub">Compared with v{version - 1}. Added lines in green, removed lines struck through.</p><div class="diff">{"".join(out)}</div>')
    return "Diff", body


SCORING_SORTS = {
    "pulled": ("Newest pulled", lambda r: r.get("first_pulled") or ""),
    "scored": ("Recently scored", lambda r: r.get("scored_at") or ""),
    "published": ("Newest at source", lambda r: r.get("source_published") or ""),
    "score": ("Highest score", lambda r: r.get("impact_score") if r.get("impact_score") is not None else -1),
}


def _qs(q: dict, **changes) -> str:
    params = {k: v[0] for k, v in q.items() if v and v[0]}
    params.update({k: v for k, v in changes.items()})
    return "&".join(f"{k}={quote(str(v))}" for k, v in params.items() if v not in (None, ""))


def _last_pull_start(db: Database) -> str | None:
    row = db.fetchone("select started_at from pipeline.pipeline_runs where stage = 'ingest' order by id desc limit 1")
    return row["started_at"] if row else None


def _view_tabs(active: str) -> str:
    return ('<div class="tabs" style="margin-bottom:10px">'
            f'<a class="tab {"on" if active == "stories" else ""}" href="/scoring">Scored stories</a>'
            f'<a class="tab {"on" if active == "items" else ""}" href="/scoring/items">Pulled items</a></div>')


def _filter_chips(q: dict, base: str, key: str, options: list[tuple[str, str]], counts: dict | None = None) -> str:
    cur = q.get(key, [""])[0]
    out = []
    for val, label in options:
        n = f"<b>{counts.get(val, 0) if val else sum(counts.values())}</b>" if counts is not None else ""
        out.append(f'<a class="tab {"on" if cur == val else ""}" href="{base}?{_qs(q, **{key: val})}">{e(label)}{n}</a>')
    return "".join(out)


def scoring_page(db: Database, q: dict, user: dict | None = None) -> tuple[str, str]:
    decision = q.get("decision", [""])[0]
    country = q.get("country", [""])[0]
    sort = q.get("sort", ["pulled"])[0]
    sort = sort if sort in SCORING_SORTS else "pulled"
    only_last = q.get("pull", [""])[0] == "last"
    rows = db.fetchall(
        """select c.id, c.country, c.category, c.headline, c.item_count, c.status,
                  e.impact_score, e.decision, e.flags, e.angle, e.rationale, e.created_at as scored_at,
                  e.wallet_impact, e.breadth, e.actionability, e.urgency, e.novelty, e.scores,
                  (select url from pipeline.raw_items where id = c.lead_item_id) as lead_url,
                  (select min(r.fetched_at) from pipeline.raw_items r where r.cluster_id = c.id) as first_pulled,
                  (select max(r.fetched_at) from pipeline.raw_items r where r.cluster_id = c.id) as last_pulled,
                  (select min(r.published_at) from pipeline.raw_items r where r.cluster_id = c.id) as source_published,
                  s.id as story_id, s.created_at as drafted_at, s.status as story_status, s.auto_published,
                  p.first_published_at as live_at, p.status as site_status
           from pipeline.story_clusters c
           left join pipeline.evaluations e on e.id = (select max(id) from pipeline.evaluations where cluster_id = c.id)
           left join newsroom.stories s on s.cluster_id = c.id
           left join site.published_stories p on p.story_id = s.id""")
    sources: dict[int, list[str]] = {}
    for r in db.fetchall("""select distinct r.cluster_id, so.name from pipeline.raw_items r
                            join pipeline.sources so on so.id = r.source_id where r.cluster_id is not null"""):
        sources.setdefault(r["cluster_id"], []).append(r["name"])
    last_start = _last_pull_start(db)
    picks = pick_mod.latest_requests(db)
    may_pick = pick_mod.can_pick(user)
    rows = in_scope(rows, user)
    cat = q.get("cat", [""])[0]
    if only_last and last_start:
        rows = [r for r in rows if (r.get("last_pulled") or "") >= last_start]
    if country:
        rows = [r for r in rows if r["country"] == country]
    cat_counts: dict[str, int] = {}
    for r in rows:
        cat_counts[r.get("category") or categories.default()] = cat_counts.get(r.get("category") or categories.default(), 0) + 1
    if cat:
        rows = [r for r in rows if (r.get("category") or categories.default()) == cat]
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["decision"] or "pending"] = counts.get(r["decision"] or "pending", 0) + 1
    if decision:
        rows = [r for r in rows if (r["decision"] or "pending") == decision]
    rows.sort(key=SCORING_SORTS[sort][1], reverse=True)

    dec_tabs = _filter_chips(q, "/scoring", "decision", [("", "All"), ("run", "Run"), ("hold", "Hold"),
                                                          ("reject", "Reject"), ("pending", "Pending")], counts)
    sort_links = " ".join(
        f'<a class="tab {"on" if k == sort else ""}" href="/scoring?{_qs(q, sort=k)}">{e(label)}</a>' for k, (label, _) in SCORING_SORTS.items())
    country_chips = _filter_chips(q, "/scoring", "country", [("", "All countries"), ("US", "US"), ("UK", "UK"), ("CA", "CA"), ("AU", "AU")])
    pull_chip = (f'<a class="tab {"on" if only_last else ""}" href="/scoring?{_qs(q, pull="" if only_last else "last")}">'
                 f'Last pull only{" · " + e(when(last_start)) if last_start else ""}</a>')
    cards = []
    for r in rows:
        flags = "".join(f'<span class="flag">{e(f)}</span>' for f in Database.unj(r["flags"], []))
        score = f'{r["impact_score"]:.0f}' if r.get("impact_score") is not None else "–"
        head = (f'<a href="{e(r["lead_url"])}" target="_blank" rel="noopener">{e(r["headline"])}</a>' if r.get("lead_url") else e(r["headline"]))
        link = f' · <a href="/story/{r["story_id"]}">Open story →</a>' if r.get("story_id") else ""
        pick = picks.get(r["id"])
        pick_html = ""
        if not r.get("story_id"):
            if pick and pick["status"] in ("queued", "running"):
                pick_html = (f'<a class="pill hold" href="/scoring/{r["id"]}/pickup">Researching for '
                             f'{e(pick["display_name"])} since {when(pick["created_at"], with_date=False)}…</a>')
            elif pick and pick["status"] == "failed":
                pick_html = f'<a class="pill reject" href="/scoring/{r["id"]}/pickup">Pick-up failed · retry</a>'
            elif may_pick:
                pick_html = f'<a class="btn" href="/scoring/{r["id"]}/pickup">Pick up as editor →</a>'
        pick_html = f'<div style="margin-top:8px">{pick_html}</div>' if pick_html else ""
        angle = f'<p class="angle">{e(r["angle"])}</p>' if r.get("angle") and r["decision"] != "reject" else ""
        steps = [f'Pulled <b>{when(r.get("first_pulled"))}</b>']
        if r.get("last_pulled") and r.get("first_pulled") and r["last_pulled"][:16] != r["first_pulled"][:16]:
            steps.append(f'more sources {when(r["last_pulled"])}')
        if r.get("source_published"):
            steps.append(f'source published {when(r["source_published"])}')
        if r.get("scored_at"):
            label = "became a <b>run</b>" if r["decision"] == "run" else f'scored {e(r["decision"])}'
            steps.append(f'{label} {when(r["scored_at"])}')
        if r.get("drafted_at"):
            steps.append(f'drafted {when(r["drafted_at"])}')
        if r.get("live_at"):
            live = "auto-published" if r.get("auto_published") else "published"
            steps.append(f'{live} {when(r["live_at"])}' + (" (since retracted)" if r.get("site_status") == "retracted" else ""))
        src = ", ".join(sources.get(r["id"], []))
        cards.append(f"""<div class="card" id="c{r['id']}"><div class="big">{score}</div><div>
<div class="meta">{cat_pill(r.get('category'))} <span class="pill {e(r['decision'] or 'pending')}">{e(r['decision'] or 'pending')}</span> {e(r['country'])} · {r['item_count']} item(s) · {e(src)}{link}</div>
<div class="hl">{head}</div><div class="timeline-line">{" → ".join(steps)}</div>{flags}{angle}<div class="why">{e(r.get('rationale') or '')}</div>{pick_html}</div>{dims_html(r)}</div>""")
    body = (f'<h1 class="page">Scoring desk</h1><p class="sub">Every story the pipeline found, when it was pulled, and how the AI '
            f'assignment editor for its category scored it. Stories marked run become AI drafts. Times are this computer\'s time ({e(tz_label())}).</p>'
            f'{_view_tabs("stories")}{category_chips(q, "/scoring", user, cat_counts)}<div class="tabs">{dec_tabs}</div><div class="tabs">{country_chips}<span style="width:8px"></span>{pull_chip}</div>'
            f'<div class="tabs"><span class="meta" style="align-self:center;margin-right:4px">Sort:</span>{sort_links}</div>'
            + ("".join(cards) or '<div class="empty">No stories match. Try another filter, or run a pull.</div>'))
    return "Scoring desk", body


def pickup_page(db: Database, user: dict, cluster_id: int, form: dict | None = None,
                error: str | None = None) -> tuple[str, str] | None:
    c = db.fetchone("select * from pipeline.story_clusters where id = ?", (cluster_id,))
    if not c:
        return None
    if not categories.can_access(user, c.get("category")):
        return no_access(c.get("category"))
    ev = db.fetchone("select * from pipeline.evaluations where cluster_id = ? order by id desc limit 1", (cluster_id,)) or {}
    story = db.fetchone("select id from newsroom.stories where cluster_id = ?", (cluster_id,))
    req = db.fetchone(
        """select p.*, u.display_name from newsroom.pick_requests p join newsroom.users u on u.id = p.requested_by
           where p.cluster_id = ? order by p.id desc limit 1""", (cluster_id,))
    from pipeline.cluster import cluster_items
    items = cluster_items(db, cluster_id)
    item_list = "".join(
        f'<li><a href="{e(it["url"])}" target="_blank" rel="noopener">{e(it["title"])}</a> · {e(it["source_name"])}'
        f'{" · " + e(when(it.get("published_at"))) if it.get("published_at") else ""}</li>' for it in items)
    desk = (f'<span class="pill {e(ev.get("decision") or "pending")}">{e(ev.get("decision") or "not scored")}'
            f'{" " + format(ev["impact_score"], ".0f") if ev.get("impact_score") is not None else ""}</span> '
            f'<span class="meta">{e(ev.get("rationale") or "")}</span>')
    status = ""
    if story:
        status = f'<div class="okbox">Drafted. <a href="/story/{story["id"]}">Open the story →</a></div>'
    elif req and req["status"] in ("queued", "running"):
        status = (f'<div class="banner">The AI is {"researching and drafting" if req["research"] else "drafting"} this story for '
                  f'{e(req["display_name"])} (started {when(req["created_at"])}). This page refreshes every 5 seconds; '
                  'it usually takes a minute or two.</div>')
    elif req and req["status"] == "failed":
        status = f'<div class="issue error">The last attempt failed: {e(req.get("error") or "unknown error")}. You can try again below.</div>'
    err = f'<div class="flash err">{e(error)}</div>' if error else ""
    form_html = ""
    busy = req and req["status"] in ("queued", "running")
    if not story and not busy:
        if not pick_mod.can_pick(user):
            form_html = '<div class="meta">Only editors and admins can pick up stories.</div>'
        else:
            f = form or {}
            if not f and req and req["status"] == "failed":
                f = {"angle": req["angle"], "audience": ", ".join(Database.unj(req.get("audience"), [])),
                     "notes": req.get("notes") or "", "urls": "\n".join(Database.unj(req.get("extra_urls"), []))}
            angle = f.get("angle", ev.get("angle") or "")
            audience = f.get("audience", ", ".join(Database.unj(ev.get("audience"), [])))
            checked = "checked" if f.get("research", "1") == "1" else ""
            form_html = f"""<form class="form panel" method="post" action="/scoring/{cluster_id}/pickup">
<h2>Brief the AI</h2>
<label>Angle: what should the story explain for {e(categories.name(c.get("category")))} readers?</label>
<textarea name="angle" rows="3" required>{e(angle)}</textarea>
<label>Audience (comma-separated)</label><input type="text" name="audience" value="{e(audience)}" placeholder="e.g. savers in their 30s, people near retirement">
<label>Notes for the AI: what to research, check or emphasise</label>
<textarea name="notes" rows="4" placeholder="e.g. Compare the 60/40 split with typical target-date funds; find current bond yields and what advisers recommend by age.">{e(f.get('notes', ''))}</textarea>
<label>Extra sources (optional, one web address per line)</label>
<textarea name="urls" rows="3" placeholder="https://...">{e(f.get('urls', ''))}</textarea>
<label><input type="checkbox" name="research" value="1" {checked}> Research with AI using live Google Search (recommended)</label>
<p><button class="primary">Research and draft</button>
<span class="meta">The draft arrives in "Needs an editor", marked Editor pick. It is never published automatically.</span></p></form>"""
    body = (f'<p class="sub"><a href="/scoring">← Scoring desk</a></p><h1 class="page">Pick up: {e(c["headline"])}</h1>'
            f'<p class="sub">{cat_pill(c.get("category"))} {e(COUNTRY_NAMES.get(c["country"], c["country"]))} · desk decision {desk}</p>{err}{status}'
            f'<div class="panel"><h2>From the feeds</h2><ul>{item_list}</ul></div>{form_html}')
    return "Pick up", body


def _item_outcome(r: dict) -> tuple[str, str]:
    """(outcome key, html) for one pulled item."""
    if r["status"] == "filtered":
        reason = r.get("filter_reason") or ""
        if reason.startswith("older than"):
            return "too_old", f'<span class="pill pending">Too old</span> <span class="meta">{e(reason)}</span>'
        return "filtered", ('<span class="pill reject">Skipped</span> <span class="meta">title filter '
                            '(promotions, reviews, betting and other non-news)</span>')
    if r["status"] == "new":
        return "waiting", '<span class="pill pending">Waiting</span> <span class="meta">not scored yet</span>'
    dec = r.get("decision") or "pending"
    score = f' {r["impact_score"]:.0f}' if r.get("impact_score") is not None else ""
    pill = f'<span class="pill {e(dec)}">{e(dec)}{score}</span>'
    if r.get("lead_item_id") == r["id"]:
        return "new_story", f'{pill} <a class="meta" href="/scoring#c{r["cluster_id"]}">new story →</a>'
    return "joined", f'{pill} <a class="meta" href="/scoring#c{r["cluster_id"]}">joined an existing story →</a>'


def items_page(db: Database, q: dict, user: dict | None = None) -> tuple[str, str]:
    country = q.get("country", [""])[0]
    outcome = q.get("outcome", [""])[0]
    only_last = q.get("pull", [""])[0] == "last"
    rows = db.fetchall(
        """select r.id, r.fetched_at, r.published_at, r.title, r.url, r.status, r.filter_reason, r.country, r.cluster_id,
                  so.name as source, so.category, e.decision, e.impact_score, c.lead_item_id
           from pipeline.raw_items r join pipeline.sources so on so.id = r.source_id
           left join pipeline.story_clusters c on c.id = r.cluster_id
           left join pipeline.evaluations e on e.id = (select max(id) from pipeline.evaluations where cluster_id = r.cluster_id)
           order by r.fetched_at desc, r.id desc limit 500""")
    last_start = _last_pull_start(db)
    if user is not None:
        rows = in_scope(rows, user)
    if only_last and last_start:
        rows = [r for r in rows if (r.get("fetched_at") or "") >= last_start]
    if country:
        rows = [r for r in rows if r["country"] == country]
    cat = q.get("cat", [""])[0]
    if cat:
        rows = [r for r in rows if (r.get("category") or categories.default()) == cat]
    counts: dict[str, int] = {}
    for r in rows:
        r["_key"], r["_html"] = _item_outcome(r)
        counts[r["_key"]] = counts.get(r["_key"], 0) + 1
    if outcome:
        rows = [r for r in rows if r["_key"] == outcome]
    chips = _filter_chips(q, "/scoring/items", "outcome", [("", "All"), ("new_story", "New story"), ("joined", "Joined a story"),
                                                          ("filtered", "Skipped"), ("too_old", "Too old"), ("waiting", "Waiting")], counts)
    country_chips = _filter_chips(q, "/scoring/items", "country", [("", "All countries"), ("US", "US"), ("UK", "UK"), ("CA", "CA"), ("AU", "AU")])
    pull_chip = (f'<a class="tab {"on" if only_last else ""}" href="/scoring/items?{_qs(q, pull="" if only_last else "last")}">'
                 f'Last pull only{" · " + e(when(last_start)) if last_start else ""}</a>')
    trs = "".join(
        f'<tr><td>{when(r["fetched_at"])}</td><td>{e(r["country"])}</td><td>{cat_pill(r.get("category"))}</td><td>{e(r["source"])}</td>'
        f'<td><a href="{e(r["url"])}" target="_blank" rel="noopener">{e(r["title"])}</a></td>'
        f'<td>{when(r.get("published_at")) or "–"}</td><td>{r["_html"]}</td></tr>' for r in rows)
    body = (f'<h1 class="page">Scoring desk</h1><p class="sub">Every item pulled from the feeds, newest first, and what happened to it. '
            f'Times are this computer\'s time ({e(tz_label())}).</p>{_view_tabs("items")}{category_chips(q, "/scoring/items", user)}<div class="tabs">{chips}</div>'
            f'<div class="tabs">{country_chips}<span style="width:8px"></span>{pull_chip}</div>'
            + (f'<table><tr><th>Pulled</th><th>Country</th><th>Category</th><th>Source</th><th>Title</th><th>Published by source</th><th>What happened</th></tr>{trs}</table>'
               if rows else '<div class="empty">No items match.</div>'))
    return "Pulled items", body


def site_feed_page(site_db: Database, slug: str | None = None, db: Database | None = None,
                   user: dict | None = None) -> tuple[str, str] | None:
    """Rendered from the site schema through the read-only site role: exactly what the website gets.

    Signed-in staff also get the desk tools beside the article (edit, history,
    publish and unpublish); those come from the newsroom, never from the site.
    """
    if slug:
        s = site_db.fetchone(
            """select p.*, a.display_name, a.job_title, a.bio from site.published_stories p
               join site.authors a on a.id = p.author_id where p.slug = ?""", (slug,))
        if not s:
            return None
        desk = db is not None and user is not None
        if s["status"] != "live" and not desk:
            return "Retracted", '<div class="empty">This story has been unpublished. The website would return 410 Gone.</div>'
        md = s["body_md"]
        title_line, rest = (md.split("\n", 1) + [""])[:2]
        dek_html = ""
        m = re.match(r"\s*\*(.+?)\*\s*\n", rest)
        if m:
            dek_html = f"<p><em>{e(m.group(1))}</em></p>"
            rest = rest[m.end():]
        updated = (f' · Updated {when(s["updated_at"])}' if s["version"] > 1 or s["updated_at"] != s["first_published_at"] else "")
        corr = f'<p class="banner" style="font-family:var(--sans);font-size:14px">Update: {e(s["correction_note"])}</p>' if s.get("correction_note") else ""
        title = f'{", " + e(s["job_title"]) if s.get("job_title") else ""}'
        if s.get("human_reviewed", True):
            credit = f'By <b>{e(s["display_name"])}</b>{title}'
        else:
            credit = f'<b>{e(wf.publication_name())} newsroom</b> · Responsible editor: {e(s["display_name"])}{title}'
        byline = (f'<div class="byline"><div class="avatar">{initials(s["display_name"])}</div><div>{credit}'
                  f'<br><span class="meta">Published {when(s["first_published_at"])}{updated}</span></div></div>')
        site_blocks = Database.unj(s.get("blocks"), None)
        if site_blocks:   # a version written in the block editor: serve the blocks
            dek = f'<p class="dek">{e(s["dek"])}</p>' if s.get("dek") else ""
            art = (f'<article class="blocks"><h1>{e(s["headline"])}</h1>{dek}{byline}{corr}{blk.render_blocks(site_blocks)}'
                   f'{blk.render_sources(Database.unj(s.get("sources"), []))}<div class="disclosure">{e(s["disclosure"])}</div></article>')
        else:             # AI format
            art = (f'<article><h1>{_inline(title_line.lstrip("# "))}</h1>{dek_html}{byline}{corr}{render_markdown(rest)}'
                   f'<div class="disclosure">{e(s["disclosure"])}</div></article>')
        body = (f'<p class="sub"><a href="/site">← Site feed</a> · rendered from <code>site.published_stories</code> '
                f'via the read-only <code>site</code> connection</p>{art}')
        if desk:
            body = _site_desk(db, user, s, art) or body
        return s["headline"], body
    rows = site_db.fetchall(
        """select p.slug, p.country, p.category, p.headline, p.status, p.version, p.first_published_at, p.updated_at, a.display_name
           from site.published_stories p join site.authors a on a.id = p.author_id order by p.first_published_at desc""")
    trs = "".join(
        f'<tr><td><a href="/site/{quote(r["slug"])}">{e(r["headline"])}</a></td><td>{cat_pill(r.get("category"))}</td><td>{e(r["country"])}</td><td>{e(r["display_name"])}</td>'
        f'<td><span class="pill {"s-published" if r["status"] == "live" else "s-unpublished"}">{e(r["status"])}</span></td>'
        f'<td>v{r["version"]}</td><td>{when(r["first_published_at"])}</td><td>{when(r["updated_at"])}</td></tr>' for r in rows)
    body = ('<h1 class="page">Site feed</h1><p class="sub">The <code>site.published_stories</code> table, which is all the public website can read. '
            'Rows appear when a publisher publishes and change only through the newsroom.</p>'
            + (f'<table><tr><th>Headline</th><th>Category</th><th>Country</th><th>Byline</th><th>Status</th><th>Version</th><th>First published</th><th>Updated</th></tr>{trs}</table>'
               if rows else '<div class="empty">Nothing published yet.</div>'))
    return "Site feed", body


def _site_desk(db: Database, user: dict, site_row: dict, art: str) -> str | None:
    story = get_story(db, site_row["story_id"])
    if not story or not categories.can_access(user, story.get("category")):
        return None     # readers' view only
    here = f"/site/{site_row['slug']}"
    if site_row["status"] != "live":
        state = ('<div class="banner">Unpublished: readers can\'t see this story (the website returns 410 Gone). '
                 'This is the last version that was live.</div>')
    elif story["current_version"] != site_row["version"]:
        state = (f'<div class="banner">Readers see v{site_row["version"]} below. The newsroom has v{story["current_version"]} '
                 f'({e(wf.LABELS[story["status"]])}). <a href="/story/{story["id"]}">Open the newer version</a></div>')
    else:
        state = ""
    info = "".join(f"<span>{k}</span><b>{v}</b>" for k, v in (
        ("On the site", f'<span class="pill s-published">Live v{site_row["version"]}</span>' if site_row["status"] == "live"
         else '<span class="pill s-unpublished">Unpublished</span>'),
        ("Newsroom status", status_pill(story["status"]) + (" " + AUTO_PILL if story.get("auto_published") else "")
         + (" " + PICK_PILL if story.get("origin") == "editor_pick" else "")),
        ("Newsroom version", f'v{story["current_version"]}'),
    ))
    aside = (actions_box(db, story, user, here=here)
             + f'<div class="box"><h3>Story</h3><div class="kv">{info}</div>'
               f'<p style="margin:12px 0 0"><a href="/story/{story["id"]}">Open in the newsroom</a> · '
               f'<a href="/story/{story["id"]}/history">All versions</a></p></div>')
    return (f'<p class="sub"><a href="/site">← Site feed</a> · how the story looks to readers, with your desk tools beside it '
            f'(readers never see the tools)</p>{state}<div class="layout"><div>{art}{origin_timeline(db, story)}</div>'
            f'<aside>{aside}</aside></div>')


# --------------------------------------------------------------------------- auth pages
def login_page(error: str | None = None, email: str = "", first_admin_missing: bool = False) -> str:
    err = f'<div class="flash err">{e(error)}</div>' if error else ""
    hint = ('<p class="meta">No admin account exists yet. In Terminal run <code>python -m newsroom create-admin</code>, '
            'then sign in here.</p>' if first_admin_missing else "")
    body = f"""<div class="auth"><h1>Sign in</h1>{err}{hint}
<form method="post" action="/login"><label>Email</label><input type="email" name="email" value="{e(email)}" autocomplete="username" required autofocus>
<label>Password</label><input type="password" name="password" autocomplete="current-password" required>
<p><button class="primary" style="width:100%">Sign in</button></p></form></div>"""
    return page("Sign in", "", body, None)


def account_page(user: dict, first: bool = False) -> tuple[str, str]:
    intro = ('<div class="banner">Choose a new password before you continue. The one you were given was temporary.</div>'
             if first or user.get("must_change_password") else "")
    body = f"""<h1 class="page">Your account</h1><p class="sub">{e(user['display_name'])} · {e(user['email'])} · {e(role_label(user['role']))}</p>
{intro}<div class="panel" style="max-width:460px"><h2>Change password</h2>
<form class="form" method="post" action="/account/password">
<label>Current password</label><input type="password" name="current" autocomplete="current-password" required>
<label>New password <span class="meta">(at least {auth.MIN_PASSWORD} characters)</span></label><input type="password" name="new" autocomplete="new-password" required>
<label>Repeat new password</label><input type="password" name="confirm" autocomplete="new-password" required>
<p><button class="primary">Save password</button></p></form></div>"""
    return "Account", body


def team_page(db: Database, user: dict, issued: dict | None = None) -> tuple[str, str]:
    is_admin = user["role"] == "admin"
    users = db.fetchall("select * from newsroom.users order by active desc, role, display_name")
    secret = ""
    if issued:
        secret = (f'<div class="panel"><h2>{e(issued["title"])}</h2><p>Give <b>{e(issued["name"])}</b> ({e(issued["email"])}) this temporary password. '
                  f'It is shown only once, and they will be asked to change it when they first sign in.</p>'
                  f'<div class="secret">{e(issued["password"])}</div></div>')
    role_opts = lambda cur: "".join(f'<option value="{r}" {"selected" if r == cur else ""}>{role_label(r)}</option>' for r in ROLES)

    def cat_boxes(selected) -> str:
        sel = set(selected or [])
        return "".join(f'<label class="inline" style="margin-right:10px;white-space:nowrap"><input type="checkbox" name="cat_{e(c)}" value="1" '
                       f'{"checked" if c in sel else ""}> {e(n)}</label>' for c, n in categories.choices())
    rows = []
    for u in users:
        controls = ""
        u_cats = Database.unj(u.get("categories"), []) or []
        if is_admin:
            cat_form = ("" if u["role"] == "admin" else
                        f"""<form class="inline" method="post" action="/team/{u['id']}" style="display:block;margin-top:6px">
<input type="hidden" name="op" value="categories">{cat_boxes(u_cats)} <button>Save categories</button></form>""")
            controls = f"""{cat_form}<div class="row-actions">
<form class="inline" method="post" action="/team/{u['id']}"><input type="hidden" name="op" value="role">
<select name="role">{role_opts(u['role'])}</select> <button>Save role</button></form>
<form class="inline" method="post" action="/team/{u['id']}"><input type="hidden" name="op" value="reset"><button>Reset password</button></form>
<form class="inline" method="post" action="/team/{u['id']}"><input type="hidden" name="op" value="{'deactivate' if u['active'] else 'activate'}">
<button class="{'danger' if u['active'] else ''}">{'Deactivate' if u['active'] else 'Reactivate'}</button></form></div>"""
        status = []
        if not u["active"]:
            status.append('<span class="pill s-killed">Inactive</span>')
        if u["is_placeholder"]:
            status.append(PLACEHOLDER_PILL)
        if not u.get("password_hash"):
            status.append('<span class="pill pending">No login</span>')
        elif u.get("must_change_password"):
            status.append('<span class="pill s-in_copy">Temporary password</span>')
        rows.append(f"""<tr><td><div style="display:flex;gap:10px;align-items:center"><div class="avatar">{initials(u['display_name'])}</div>
<div><b>{e(u['display_name'])}</b><br><span class="meta">{e(u.get('job_title') or '')}</span></div></div></td>
<td>{e(role_label(u['role']))}<br><span class="meta">{e(categories.scope_label(dict(u, categories=u_cats)))}</span></td><td>{e(u['email'])}<br><span class="meta">Last sign-in: {when(u.get('last_login_at')) or 'never'}</span></td>
<td>{' '.join(status)}</td><td>{controls}</td></tr>""")
    add = ""
    if is_admin:
        add = f"""<div class="panel"><h2>Add a team member</h2><form class="form" method="post" action="/team">
<div class="grid2"><div><label>Full name</label><input type="text" name="display_name" required>
<label>Email</label><input type="email" name="email" required></div>
<div><label>Role</label><select name="role">{role_opts('copywriter')}</select>
<label>Job title (shown in bylines for editors)</label><input type="text" name="job_title"></div></div>
<label>Categories they work on (leave all unticked for every category; admins always see everything)</label><div>{cat_boxes([])}</div>
<p><button class="primary">Create account</button> <span class="meta">They get a temporary password to change on first sign-in.</span></p></form></div>"""
    note = ("" if is_admin else '<p class="sub">Only admins can add people or change roles.</p>')
    body = (f'<h1 class="page">Team</h1><p class="sub">Copy editors edit assigned stories, editors approve, assign and unpublish, '
            f'publishers put approved stories live, admins manage everything including the team. People only see and act on '
            f'stories in their categories.</p>{note}{secret}{add}'
            f'<table><tr><th>Person</th><th>Role</th><th>Email</th><th>Status</th><th></th></tr>{"".join(rows)}</table>')
    return "Team", body


# --------------------------------------------------------------------------- sources & pulls
def run_result(r: dict) -> str:
    if r.get("error"):
        return f'<span class="bad">{e(r["error"][:120])}</span>'
    if not r.get("finished_at"):
        return "running"
    st = r.get("stats") or {}
    return (f'{st.get("new_items", 0)} new, {st.get("drafted", 0)} drafted, '
            f'{st.get("auto_published", 0)} auto-published')


def sources_page(db: Database, user: dict, test_result: dict | None = None, form: dict | None = None,
                 q: dict | None = None) -> tuple[str, str]:
    can = src_mod.can_manage(user)
    q = q or {}
    cat = q.get("cat", [""])[0]
    st = pull_mod.status()
    running = st.get("running")
    # pull panel
    if running:
        state = (f'<div class="banner">Pull running: <b>{e(st.get("stage", ""))}</b> for {e(", ".join(st.get("countries") or []))}, '
                 f'started {when(st.get("started_at"))}{" by " + e(st["started_by"]) if st.get("started_by") else ""}. '
                 'This page refreshes every 10 seconds.</div>')
    elif st.get("state"):
        sm = st.get("summary") or {}
        flow = ""
        if "new_stories" in sm:
            flow = (f' ({sm.get("new_stories", 0)} became new stories, {sm.get("joined_stories", 0)} joined existing stories, '
                    f'{sm.get("too_old", 0)} were older than the news window) plus {sm.get("prefiltered", 0)} skipped by the title filter')
        line = (f'{sm.get("new_items", 0)} new items{flow}, {sm.get("scored", 0)} scored, {sm.get("drafted", 0)} drafted, '
                f'{sm.get("auto_published", 0)} auto-published' + (f' ({e(sm["autopublish_note"])})' if sm.get("autopublish_note") else ""))
        cls = "okbox" if st["state"] == "finished" else "issue error"
        err = f'<br>{e(st.get("error", ""))}' if st.get("error") else ""
        state = (f'<div class="{cls}">Last pull {e(st["state"])} {when(st.get("finished_at") or st.get("started_at"))} '
                 f'({e(", ".join(st.get("countries") or []))}, {e(st.get("trigger", ""))}): {line}{err}</div>')
    else:
        state = '<div class="meta">No pull has run from the desk yet.</div>'
    buttons = ""
    if can:
        dis = "disabled" if running else ""
        buttons = ('<form method="post" action="/pull" class="row-actions" style="margin-top:12px">'
                   f'<button class="primary" name="countries" value="" {dis}>Pull all feeds now</button>'
                   + "".join(f'<button name="countries" value="{c}" {dis}>{c} only</button>' for c in ("US", "UK", "CA", "AU"))
                   + '</form><p class="meta">A pull fetches the feeds, scores new stories, writes AI drafts and auto-publishes '
                     'those that qualify. It can take a few minutes.</p>')
    # schedule panel
    try:
        regions = sched.plan()
    except Exception as exc:  # missing/invalid schedule.yaml shouldn't break the page
        regions, sched_err = [], str(exc)
    else:
        sched_err = ""
    local_tz = datetime.now().astimezone().tzinfo
    sched_rows = "".join(
        f'<tr><td>{e(r["id"])}</td><td>{e(", ".join(r["countries"]))}</td><td>{r["next_slot"]:%a %d %b, %H:%M} {e(r["timezone"].split("/")[-1].replace("_", " "))}</td>'
        f'<td>{r["next_slot_utc"].astimezone(local_tz):%a %d %b, %H:%M}</td></tr>' for r in regions)
    runs = sched.recent_runs(db, 8)
    run_rows = "".join(
        f'<tr><td>{e(r["region"])}</td><td>{e(r["local_date"])}</td><td>{when(r["started_at"])}</td>'
        f'<td>{run_result(r)}</td></tr>' for r in runs)
    agent = ("installed" if sched.installed() else
             ('not installed: run <code>python -m newsroom schedule install</code> in Terminal' if sys.platform == "darwin"
              else 'not installed: run <code>deploy/setup_vps.sh</code>'))
    schedule_html = (f'<div class="panel"><h2>Daily schedule</h2><p class="meta">Scheduler ({e(sched.scheduler_name())}): {agent}.</p>'
                     + (f'<div class="issue error">{e(sched_err)}</div>' if sched_err else "")
                     + f'<table><tr><th>Region</th><th>Countries</th><th>Next pull (local time there)</th><th>On this computer</th></tr>{sched_rows}</table>'
                     + (f'<h2 style="margin-top:14px">Recent scheduled pulls</h2><table><tr><th>Region</th><th>Day</th><th>Started</th><th>Result</th></tr>{run_rows}</table>' if runs else "")
                     + '</div>')
    # sources table
    rows = []
    srcs = src_mod.list_sources(db)
    cat_counts: dict[str, int] = {}
    for r in srcs:
        cat_counts[r.get("category") or categories.default()] = cat_counts.get(r.get("category") or categories.default(), 0) + 1
    if cat:
        srcs = [r for r in srcs if (r.get("category") or categories.default()) == cat]
    for r in srcs:
        toggle = ""
        if can and categories.can_access(user, r.get("category")):
            toggle = (f'<form class="inline" method="post" action="/sources/{e(r["id"])}"><input type="hidden" name="active" value="{"0" if r["active"] else "1"}">'
                      f'<button class="{"danger" if r["active"] else ""}">{"Switch off" if r["active"] else "Switch on"}</button></form>')
        status = (r.get("last_status") or "not fetched yet")
        status_cls = "bad" if status.startswith("error") else ""
        rows.append(f"""<tr><td><b>{e(r['name'])}</b><br><a class="meta" href="{e(r['url'])}" target="_blank" rel="noopener">{e(r['url'][:70])}</a></td>
<td>{cat_pill(r.get('category'))}</td><td>{e(r['country'])}</td><td>{e(r['kind'])}<br><span class="meta">{e(r['reuse'])}</span></td><td>{r['items']}</td>
<td><span class="{status_cls}">{e(status[:60])}</span><br><span class="meta">{when(r.get('last_fetched_at'))}</span></td>
<td>{'<span class="pill s-approved">On</span>' if r['active'] else '<span class="pill s-killed">Off</span>'}<br><span class="meta">{e(r.get('added_by') or '')}</span></td><td>{toggle}</td></tr>""")
    add = ""
    if can:
        f = form or {}
        tr = ""
        if test_result:
            if test_result.get("error"):
                tr = f'<div class="issue error">{e(test_result["error"])}</div>'
            else:
                items = "".join(f"<li>{e(t)}</li>" for t in test_result.get("latest", []))
                tr = (f'<div class="okbox">{"Added. " if test_result.get("added") else "Feed works. "}{test_result["items"]} stories found'
                      f'{", newest " + e(test_result["newest"]) if test_result.get("newest") else ""}.</div><ul class="meta">{items}</ul>')
        opt = lambda name, choices, cur: "".join(f'<option value="{k}" {"selected" if k == cur else ""}>{e(v)}</option>' for k, v in choices.items())
        countries = {c: f"{c} · {n}" for c, n in COUNTRY_NAMES.items()}
        cats = {c: n for c, n in categories.choices() if categories.can_access(user, c)}
        first_cat = cat if cat in cats else next(iter(cats), categories.default())
        add = f"""<div class="panel"><h2>Add an RSS feed</h2>{tr}<form class="form" method="post" action="/sources">
<div class="grid2"><div><label>Name</label><input type="text" name="name" value="{e(f.get('name', ''))}" placeholder="e.g. Bank of England news" required>
<label>Feed address (RSS or Atom)</label><input type="text" name="url" value="{e(f.get('url', ''))}" placeholder="https://..." required>
<label>Category: its stories are scored and written with this category's approach</label><select name="category">{opt('category', cats, f.get('category', first_cat))}</select>
<label>Country it covers</label><select name="country">{opt('country', countries, f.get('country', 'US'))}</select></div>
<div><label>Kind</label><select name="kind">{opt('kind', src_mod.KINDS, f.get('kind', 'publisher'))}</select>
<label>How we may use its content</label><select name="reuse">{opt('reuse', src_mod.REUSE, f.get('reuse', 'link_only'))}</select>
<label><input type="checkbox" name="full_text" value="1" {'checked' if f.get('full_text') else ''}> Read the full page for facts (public-domain and OGL sources only)</label></div></div>
<p class="row-actions"><button name="op" value="test">Test feed</button><button class="primary" name="op" value="add">Test and add</button></p></form></div>"""
    body = (f'<h1 class="page">Sources & pulls</h1><p class="sub">The RSS feeds the pipeline reads, the daily schedule, and a button to pull now.</p>'
            f'<div class="grid2"><div class="panel"><h2>Pull now</h2>{state}{buttons}</div>{schedule_html}</div>{add}'
            + (f'<div class="tabs">{_filter_chips(q, "/sources", "cat", [("", "All categories")] + categories.choices(), cat_counts)}</div>')
            + f'<table><tr><th>Source</th><th>Category</th><th>Country</th><th>Kind / use</th><th>Items</th><th>Last fetch</th><th>Status</th><th></th></tr>{"".join(rows)}</table>')
    return "Sources & pulls", body


# --------------------------------------------------------------------------- server
def _form(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    length = min(int(handler.headers.get("Content-Length") or 0), 200_000)
    data = parse_qs(handler.rfile.read(length).decode("utf-8"), keep_blank_values=True)
    return {k: v[0] for k, v in data.items()}


def _lines(text: str) -> list[str]:
    return [re.sub(r"^\s*(?:\d+[.)]|[-*•])\s*", "", ln).strip() for ln in text.splitlines() if ln.strip()]


class Handler(BaseHTTPRequestHandler):
    server_version = "StoryHubNewsroom/0.3"

    # -- helpers
    def _token(self) -> str | None:
        c = SimpleCookie(self.headers.get("Cookie") or "")
        v = c.get(SESSION_COOKIE)
        return v.value if v else None

    def _cookie(self, token: str, max_age: int) -> str:
        secure = "; Secure" if get_settings().app_env == "production" else ""
        return f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}{secure}"

    def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8", headers: dict | None = None) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Robots-Tag", "noindex, nofollow")   # the desk is never for search engines
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str, cookie: str | None = None) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _flash(self, q: dict) -> tuple[str, str] | None:
        if q.get("ok"):
            return ("ok", q["ok"][0])
        if q.get("err"):
            return ("err", q["err"][0])
        return None

    def _client_ip(self) -> str:
        """The visitor's address, for sign-in rate limiting.

        Behind nginx + Cloudflare every request arrives from 127.0.0.1, so with
        DESK_TRUST_PROXY=1 the address Cloudflare reports (CF-Connecting-IP,
        else X-Real-IP) is used, but only for requests from this machine.
        nginx must accept traffic from Cloudflare's addresses only.
        """
        peer = self.client_address[0]
        if os.getenv("DESK_TRUST_PROXY") == "1" and peer in ("127.0.0.1", "::1"):
            for header in ("CF-Connecting-IP", "X-Real-IP"):
                value = (self.headers.get(header) or "").strip()
                try:
                    return str(ipaddress.ip_address(value))
                except ValueError:
                    continue
        return peer

    def _allowed_hosts(self) -> set[str]:
        port = self.server.server_address[1]
        hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}
        hosts |= {h.strip() for h in os.getenv("DESK_HOSTS", "").split(",") if h.strip()}
        return hosts

    def _same_origin(self) -> bool:
        allowed = self._allowed_hosts()
        origin = self.headers.get("Origin")
        return self.headers.get("Host") in allowed and (not origin or urlsplit(origin).netloc in allowed)

    def _render(self, db, user, out, active, q=None, code=200, refresh=None, wide=False):
        if out is None:
            self._send(404, page("Not found", active, '<div class="empty">Not found.</div>', user))
        else:
            self._send(code, page(out[0], active, out[1], user, self._flash(q or {}), refresh=refresh, wide=wide))

    # -- GET
    def do_GET(self):  # noqa: N802
        url = urlsplit(self.path)
        q = parse_qs(url.query)
        path = url.path
        if path in STATIC_FILES:
            f, ctype = STATIC_FILES[path]
            return self._send(200, f.read_text(encoding="utf-8"), ctype)
        db = connect("newsroom")
        try:
            user = auth.user_for_session(db, self._token())
            if path == "/login":
                if user:
                    return self._redirect("/")
                return self._send(200, login_page(first_admin_missing=not auth.has_admin_login(db)))
            if not user:
                return self._redirect("/login")
            if user.get("must_change_password") and path != "/account":
                return self._redirect("/account?first=1")
            if path == "/":
                return self._render(db, user, queue_page(db, user, q), "queue", q)
            if m := re.fullmatch(r"/story/(\d+)", path):
                v = q.get("v", [""])[0]
                return self._render(db, user, story_page(db, int(m.group(1)), user, int(v) if v.isdigit() else None), "queue", q)
            if m := re.fullmatch(r"/story/(\d+)/edit", path):
                out = edit_page(db, int(m.group(1)), user, q.get("next", [""])[0])
                return self._render(db, user, out, "queue", q, wide=bool(out and out[0].startswith("Edit · ")))
            if m := re.fullmatch(r"/story/(\d+)/history", path):
                return self._render(db, user, history_page(db, int(m.group(1)), user), "queue", q)
            if m := re.fullmatch(r"/story/(\d+)/diff/(\d+)", path):
                return self._render(db, user, diff_page(db, int(m.group(1)), int(m.group(2)), user), "queue", q)
            if path == "/scoring":
                return self._render(db, user, scoring_page(db, q, user), "scoring", q)
            if path == "/scoring/items":
                return self._render(db, user, items_page(db, q, user), "scoring", q)
            if m := re.fullmatch(r"/scoring/(\d+)/pickup", path):
                cid = int(m.group(1))
                busy = pick_mod.active_request(db, cid) is not None
                return self._render(db, user, pickup_page(db, user, cid), "scoring", q, refresh=5 if busy else None)
            if path == "/sources":
                return self._render(db, user, sources_page(db, user, q=q), "sources", q,
                                    refresh=10 if pull_mod.is_running() else None)
            if path == "/team":
                return self._render(db, user, team_page(db, user), "team", q)
            if path == "/account":
                return self._render(db, user, account_page(user, first=bool(q.get("first"))), "account", q)
            if path == "/site" or path.startswith("/site/"):
                site_db = connect("site")
                try:
                    slug = path[len("/site/"):] if path.startswith("/site/") else None
                    return self._render(db, user, site_feed_page(site_db, slug or None, db, user), "site", q)
                finally:
                    site_db.close()
            return self._render(db, user, None, "")
        finally:
            db.close()

    # -- POST
    def do_POST(self):  # noqa: N802
        if not self._same_origin():
            return self._send(403, "forbidden", "text/plain")
        path = urlsplit(self.path).path
        form = _form(self)
        db = connect("newsroom")
        try:
            if path == "/login":
                try:
                    user, token = auth.login(db, form.get("email", ""), form.get("password", ""), self._client_ip())
                except auth.AuthError as exc:
                    return self._send(200, login_page(str(exc), form.get("email", "")))
                dest = "/account?first=1" if user.get("must_change_password") else "/"
                return self._redirect(dest, cookie=self._cookie(token, auth.SESSION_DAYS * 86400))
            if path == "/logout":
                auth.logout(db, self._token())
                return self._redirect("/login", cookie=self._cookie("", 0))

            user = auth.user_for_session(db, self._token())
            if not user:
                return self._redirect("/login")
            if path == "/account/password":
                try:
                    auth.change_password(db, user, form.get("current", ""), form.get("new", ""), form.get("confirm", ""))
                except auth.AuthError as exc:
                    db.rollback()
                    return self._redirect(f"/account?err={quote(str(exc))}" + ("&first=1" if user.get("must_change_password") else ""))
                return self._redirect(f"/?ok={quote('Password changed.')}")
            if user.get("must_change_password"):
                return self._redirect("/account?first=1")

            if m := re.fullmatch(r"/story/(\d+)/action", path):
                sid = int(m.group(1))
                try:
                    wf.perform(db, sid, form.get("action", ""), user, note=form.get("note") or None,
                               assignee_id=int(form["assignee_id"]) if form.get("assignee_id", "").isdigit() else None,
                               override=form.get("override") == "1", correction_note=form.get("correction_note") or None)
                    nxt = _safe_next(form.get("next"), f"/story/{sid}")
                    return self._redirect(f"{nxt}?ok={quote(ACTION_LABELS.get(form.get('action', ''), 'Done') + ': done.')}")
                except wf.WorkflowError as exc:
                    db.rollback()
                    nxt = _safe_next(form.get("next"), f"/story/{sid}")
                    return self._redirect(f"{nxt}?err={quote(str(exc))}")
            if m := re.fullmatch(r"/story/(\d+)/edit", path):
                sid = int(m.group(1))
                story = get_story(db, sid)
                if not story:
                    return self._send(404, "not found", "text/plain")
                rev = story["revision"]
                if "blocks" in form:   # block editor
                    try:
                        body_blocks = blk.clean_blocks(form.get("blocks") or "[]")
                    except blk.BlockError as exc:
                        return self._redirect(f"/story/{sid}?err={quote('Not saved: ' + str(exc))}")
                    content = {
                        "headline": form.get("headline", "").strip(), "dek": form.get("dek", "").strip(),
                        "blocks": body_blocks, "meta_description": form.get("meta_description", "").strip(),
                        "topics": rev["topics"], "sources": rev["sources"],
                    }
                else:                  # fixed-section form (CLI and older clients)
                    content = {
                        "headline": form.get("headline", "").strip(), "dek": form.get("dek", "").strip(),
                        "sections": {k: form.get(k, "").strip() for k in PROSE_KEYS},
                        "action_steps": _lines(form.get("action_steps", "")), "takeaways": _lines(form.get("takeaways", "")),
                        "meta_description": form.get("meta_description", "").strip(),
                        "topics": rev["topics"], "sources": rev["sources"],
                    }
                try:
                    report = wf.save_revision(db, sid, user, content, note=form.get("note") or None)
                except wf.WorkflowError as exc:
                    db.rollback()
                    return self._redirect(f"/story/{sid}?err={quote(str(exc))}")
                errs = sum(1 for i in report["issues"] if i["severity"] == "error")
                msg = "Saved." + (f" {errs} guardrail error(s) to look at." if errs else " Guardrails passed.")
                then = form.get("then")
                nxt = _safe_next(form.get("next"), f"/story/{sid}")
                if then == "submit":
                    try:
                        wf.perform(db, sid, "submit", user, note=form.get("note") or None)
                    except wf.WorkflowError as exc:
                        db.rollback()
                        return self._redirect(f"/story/{sid}?err={quote(msg + ' Not sent: ' + str(exc))}")
                    return self._redirect(f"/story/{sid}?ok={quote(msg + ' Sent to an editor for review.')}")
                if then in ("approve", "publish") and errs:
                    return self._redirect(f"/story/{sid}?err={quote(msg + ' Not approved: fix the errors or approve with an override reason.')}")
                if then in ("approve", "publish"):
                    try:
                        st = wf.perform(db, sid, "approve", user, note=form.get("note") or None)
                        msg = "Saved and approved."
                        if then == "publish":
                            wf.perform(db, sid, "publish", user, correction_note=form.get("correction_note") or None)
                            return self._redirect(f"/site/{quote(st['slug'])}?ok={quote('Saved, approved and live on the site.')}")
                    except wf.WorkflowError as exc:
                        db.rollback()
                        return self._redirect(f"/story/{sid}?err={quote(msg + ' ' + str(exc))}")
                    return self._redirect(f"/story/{sid}?ok={quote(msg)}")
                if nxt.startswith("/site/") and not errs and story.get("live"):
                    msg += " Readers still see the published version until this is approved and republished."
                return self._redirect(f"/story/{sid}?{'err' if errs else 'ok'}={quote(msg)}")

            if m := re.fullmatch(r"/story/(\d+)/category", path):
                sid = int(m.group(1))
                try:
                    wf.set_category(db, sid, user, form.get("category", ""), note=form.get("note") or None)
                except wf.WorkflowError as exc:
                    db.rollback()
                    return self._redirect(f"/story/{sid}?err={quote(str(exc))}")
                return self._redirect(f"/story/{sid}?ok={quote('Moved to ' + categories.name(form.get('category')) + '.')}")

            if path == "/team":
                try:
                    new, temp = auth.create_user(db, user, email=form.get("email", ""), display_name=form.get("display_name", ""),
                                                 role=form.get("role", ""), job_title=form.get("job_title", ""),
                                                 categories=[c for c in categories.ids() if form.get(f"cat_{c}") == "1"])
                except auth.AuthError as exc:
                    db.rollback()
                    return self._redirect(f"/team?err={quote(str(exc))}")
                issued = {"title": "Account created", "name": new["display_name"], "email": new["email"], "password": temp}
                return self._send(200, page("Team", "team", team_page(db, user, issued)[1], user))
            if m := re.fullmatch(r"/team/(\d+)", path):
                uid, op = int(m.group(1)), form.get("op")
                try:
                    if op == "reset":
                        temp = auth.reset_password(db, user, uid)
                        target = db.fetchone("select * from newsroom.users where id = ?", (uid,))
                        issued = {"title": "Password reset", "name": target["display_name"], "email": target["email"], "password": temp}
                        return self._send(200, page("Team", "team", team_page(db, user, issued)[1], user))
                    if op == "role":
                        auth.update_user(db, user, uid, role=form.get("role"))
                    elif op == "categories":
                        auth.update_user(db, user, uid, categories=[c for c in categories.ids() if form.get(f"cat_{c}") == "1"])
                    elif op in ("deactivate", "activate"):
                        if uid == user["id"] and op == "deactivate":
                            raise auth.AuthError("You can't deactivate your own account.")
                        auth.update_user(db, user, uid, active=(op == "activate"))
                    else:
                        raise auth.AuthError("Unknown change.")
                except auth.AuthError as exc:
                    db.rollback()
                    return self._redirect(f"/team?err={quote(str(exc))}")
                return self._redirect(f"/team?ok={quote('Saved.')}")

            if m := re.fullmatch(r"/scoring/(\d+)/pickup", path):
                cid = int(m.group(1))
                f = {k: form.get(k, "") for k in ("angle", "audience", "notes", "urls")}
                f["research"] = "1" if form.get("research") == "1" else "0"
                try:
                    rid = pick_mod.request_pickup(db, user, cid, angle=f["angle"], audience=f["audience"], notes=f["notes"],
                                                  extra_urls=f["urls"], research=f["research"] == "1")
                except pick_mod.PickupError as exc:
                    db.rollback()
                    out = pickup_page(db, user, cid, form=f, error=str(exc))
                    if out is None:
                        return self._send(404, "not found", "text/plain")
                    return self._send(200, page(out[0], "scoring", out[1], user))
                pick_mod.start_background(rid)
                return self._redirect(f"/scoring/{cid}/pickup")
            if path == "/sources":
                if not src_mod.can_manage(user):
                    return self._redirect(f"/sources?err={quote('Only editors and admins can add sources.')}")
                f = {k: form.get(k, "") for k in ("name", "url", "country", "kind", "reuse", "category")}
                f["full_text"] = form.get("full_text") == "1"
                try:
                    if form.get("op") == "test":
                        result = src_mod.test_feed(f["url"])
                    else:
                        result = src_mod.add_source(db, user, name=f["name"], country=f["country"], url=f["url"],
                                                    kind=f["kind"], reuse=f["reuse"], full_text=f["full_text"],
                                                    category=f["category"] or None)
                        result["added"] = True
                        f = {}
                except src_mod.SourceError as exc:
                    db.rollback()
                    result = {"error": str(exc)}
                return self._send(200, page("Sources & pulls", "sources", sources_page(db, user, result, f)[1], user))
            if m := re.fullmatch(r"/sources/([\w.-]+)", path):
                try:
                    src_mod.set_active(db, user, m.group(1), form.get("active") == "1")
                except src_mod.SourceError as exc:
                    return self._redirect(f"/sources?err={quote(str(exc))}")
                return self._redirect(f"/sources?ok={quote('Source updated.')}")
            if path == "/pull":
                if not src_mod.can_manage(user):
                    return self._redirect(f"/sources?err={quote('Only editors and admins can start a pull.')}")
                try:
                    pull_mod.start_background(form.get("countries") or None, started_by=user["email"])
                except (pull_mod.PullBusy, ValueError) as exc:
                    return self._redirect(f"/sources?err={quote(str(exc))}")
                return self._redirect(f"/sources?ok={quote('Pull started. This page will update as it runs.')}")
            return self._send(404, "not found", "text/plain")
        finally:
            db.close()

    def log_message(self, fmt, *args):
        if "favicon" not in str(args[0]):
            super().log_message(fmt, *args)


def serve(port: int = 8765) -> None:
    bind = os.getenv("DESK_BIND", "127.0.0.1")
    httpd = None
    # On a server nginx expects one fixed port, so never wander to another one there.
    strict = get_settings().app_env == "production" or os.getenv("DESK_STRICT_PORT") == "1"
    for candidate in ([port] if strict else range(port, port + 20)):
        try:
            httpd = ThreadingHTTPServer((bind, candidate), Handler)
            break
        except OSError as exc:
            if exc.errno not in (48, 98):
                raise
            print(f"port {candidate} is busy, trying {candidate + 1}...")
    if httpd is None:
        raise SystemExit(f"Port {port} is in use." if strict else f"No free port between {port} and {port + 19}; pass --port to choose one.")
    db = connect("newsroom")
    try:
        if not auth.has_admin_login(db):
            print("No admin can sign in yet. Create one first:  python -m newsroom create-admin")
    finally:
        db.close()
    print(f"\nStory Hub newsroom running at  http://{'localhost' if bind in ('127.0.0.1', '::1') else bind}:{httpd.server_address[1]}   (Ctrl+C to stop)\n", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
