"""Block-based story bodies for stories edited by people.

AI drafts keep their fixed sections (what happened, money impact, ...). When a
person edits a story in the block editor, that version is stored as a list of
blocks, and the site serves the blocks when it is published.

Block format (compatible with Editor.js JSON, so a richer editor can be swapped
in later without migrating data):

    {"type": "paragraph", "data": {"text": "<b>inline</b> html"}}
    {"type": "header",    "data": {"text": "...", "level": 2 | 3}}
    {"type": "list",      "data": {"style": "unordered" | "ordered", "items": ["...", ...]}}
    {"type": "quote",     "data": {"text": "...", "caption": "..."}}
    {"type": "delimiter", "data": {}}
    {"type": "table",     "data": {"withHeadings": true, "content": [["...", ...], ...]}}

Inline HTML is limited to <b>, <i>, <a href> and <br>; everything else is
stripped on save and again when rendering.
"""
from __future__ import annotations

import html
import json
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

TYPES = ("paragraph", "header", "list", "quote", "delimiter", "table")
MAX_BLOCKS = 400
MAX_TEXT = 20_000
MAX_TABLE = 40          # rows and columns

SECTION_ORDER = (
    ("what_happened", "What happened"),
    ("money_impact", "What it means for your money"),
    ("who_is_affected", "Who is affected"),
    ("illustrative_example", "The numbers"),
)


class BlockError(ValueError):
    pass


# --------------------------------------------------------------------------- inline HTML
def _safe_href(url: str) -> str | None:
    url = (url or "").strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme in ("http", "https") and parts.netloc:
        return url
    if parts.scheme == "mailto" and parts.path:
        return url
    return None


class _Sanitizer(HTMLParser):
    TAGS = {"b": "b", "strong": "b", "i": "i", "em": "i"}
    DROP = {"script", "style", "template", "iframe", "object", "svg", "math", "noscript"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.DROP:
            self.skip += 1
            return
        if self.skip:
            return
        if tag == "br":
            self.out.append("<br>")
        elif tag in self.TAGS:
            t = self.TAGS[tag]
            self.out.append(f"<{t}>")
            self.stack.append(t)
        elif tag == "a":
            href = _safe_href(dict(attrs).get("href") or "")
            if href:
                self.out.append(f'<a href="{html.escape(href, quote=True)}">')
                self.stack.append("a")
            else:
                self.stack.append("")
        elif tag in ("div", "p") and self.out:
            self.out.append("<br>")   # pasted/browser-made line breaks inside a block

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag):
        if tag in self.DROP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        want = self.TAGS.get(tag, tag)
        if want not in ("b", "i", "a"):
            return
        # close up to the matching open tag
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i] == want or (want == "a" and self.stack[i] == ""):
                for t in reversed(self.stack[i:]):
                    if t:
                        self.out.append(f"</{t}>")
                del self.stack[i:]
                return

    def handle_data(self, data):
        if not self.skip:
            self.out.append(html.escape(data, quote=False))

    def result(self) -> str:
        for t in reversed(self.stack):
            if t:
                self.out.append(f"</{t}>")
        self.stack.clear()
        s = "".join(self.out)
        s = re.sub(r"(<br>\s*)+$", "", s)          # trailing breaks
        s = re.sub(r"^(\s*<br>)+", "", s)
        s = re.sub(r"<(b|i)></\1>", "", s)
        return s.replace(" ", " ").strip()


def clean_inline(value) -> str:
    p = _Sanitizer()
    p.feed(str(value or "")[:MAX_TEXT])
    p.close()
    return p.result()


def inline_text(value: str) -> str:
    """Plain text of sanitized inline HTML."""
    s = re.sub(r"<br>", " ", value or "")
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def inline_markdown(value: str) -> str:
    s = value or ""
    s = re.sub(r'<a href="([^"]+)">(.*?)</a>', lambda m: f"[{m.group(2)}]({html.unescape(m.group(1))})", s)
    s = re.sub(r"</?b>", "**", s)
    s = re.sub(r"</?i>", "*", s)
    s = s.replace("<br>", " ")
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).strip()


def md_inline_to_html(text: str) -> str:
    """The AI's light markdown (**bold**, *italic*, [link](url)) -> safe inline HTML."""
    out = html.escape(text or "", quote=False)
    out = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
                 lambda m: f'<a href="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>', out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", out)
    return out


# --------------------------------------------------------------------------- validation
def clean_blocks(raw) -> list[dict]:
    """Validate and sanitize blocks from the editor. Empty blocks are dropped."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except json.JSONDecodeError as exc:
            raise BlockError(f"the story body could not be read ({exc.msg})") from exc
    if isinstance(raw, dict):
        raw = raw.get("blocks", [])
    if not isinstance(raw, list):
        raise BlockError("the story body must be a list of blocks")
    if len(raw) > MAX_BLOCKS:
        raise BlockError(f"too many blocks (max {MAX_BLOCKS})")
    out: list[dict] = []
    for b in raw:
        if not isinstance(b, dict):
            continue
        t, d = b.get("type"), b.get("data") or {}
        if t not in TYPES or not isinstance(d, dict):
            continue
        if t == "paragraph":
            text = clean_inline(d.get("text"))
            if inline_text(text):
                out.append({"type": t, "data": {"text": text}})
        elif t == "header":
            text = clean_inline(d.get("text"))
            level = d.get("level") if d.get("level") in (2, 3) else 2
            if inline_text(text):
                out.append({"type": t, "data": {"text": text, "level": level}})
        elif t == "list":
            items = [clean_inline(_item_text(i)) for i in (d.get("items") or [])][:200]
            items = [i for i in items if inline_text(i)]
            if items:
                out.append({"type": t, "data": {"style": "ordered" if d.get("style") == "ordered" else "unordered",
                                                "items": items}})
        elif t == "quote":
            text = clean_inline(d.get("text"))
            if inline_text(text):
                out.append({"type": t, "data": {"text": text, "caption": clean_inline(d.get("caption"))}})
        elif t == "delimiter":
            if out and out[-1]["type"] != "delimiter":
                out.append({"type": t, "data": {}})
        elif t == "table":
            rows = [[clean_inline(c) for c in (r or [])[:MAX_TABLE]] for r in (d.get("content") or [])[:MAX_TABLE]
                    if isinstance(r, list)]
            rows = [r for r in rows if any(inline_text(c) for c in r)]
            if rows:
                width = max(len(r) for r in rows)
                rows = [r + [""] * (width - len(r)) for r in rows]
                out.append({"type": t, "data": {"withHeadings": bool(d.get("withHeadings")), "content": rows}})
    while out and out[-1]["type"] == "delimiter":
        out.pop()
    return out


def _item_text(item) -> str:
    # Editor.js list v2 uses {"content": "...", "items": [...]}; flatten nested items
    if isinstance(item, dict):
        text = str(item.get("content") or "")
        subs = [_item_text(i) for i in (item.get("items") or [])]
        return "; ".join([text] + [s for s in subs if s]) if subs else text
    return str(item or "")


# --------------------------------------------------------------------------- conversion
def content_to_blocks(content: dict) -> list[dict]:
    """An AI draft's fixed sections as editable blocks (first manual edit)."""
    blocks: list[dict] = []
    sections = content.get("sections") or {}
    for key, title in SECTION_ORDER:
        text = (sections.get(key) or "").strip()
        if not text:
            continue
        blocks.append({"type": "header", "data": {"text": html.escape(title, quote=False), "level": 2}})
        for para in re.split(r"\n\s*\n", text):
            para = " ".join(line.strip() for line in para.splitlines() if line.strip())
            if para:
                blocks.append({"type": "paragraph", "data": {"text": md_inline_to_html(para)}})
    if content.get("action_steps"):
        blocks.append({"type": "header", "data": {"text": "What to do now", "level": 2}})
        blocks.append({"type": "list", "data": {"style": "ordered",
                                                "items": [md_inline_to_html(s) for s in content["action_steps"]]}})
    if content.get("takeaways"):
        blocks.append({"type": "header", "data": {"text": "Key takeaways", "level": 2}})
        blocks.append({"type": "list", "data": {"style": "unordered",
                                                "items": [md_inline_to_html(s) for s in content["takeaways"]]}})
    return blocks


def blocks_markdown(content: dict) -> str:
    """Markdown body for a block story: headline, dek, blocks, then sources."""
    md = [f"# {content['headline']}", ""]
    if content.get("dek"):
        md += [f"*{content['dek']}*", ""]
    for b in content.get("blocks") or []:
        d = b["data"]
        if b["type"] == "paragraph":
            md += [inline_markdown(d["text"]), ""]
        elif b["type"] == "header":
            md += [("## " if d.get("level", 2) == 2 else "### ") + inline_markdown(d["text"]), ""]
        elif b["type"] == "list":
            md += [(f"{i}. " if d["style"] == "ordered" else "- ") + inline_markdown(x)
                   for i, x in enumerate(d["items"], 1)] + [""]
        elif b["type"] == "quote":
            md += [f"> {inline_markdown(d['text'])}"] + ([f"> — {inline_markdown(d['caption'])}"] if d.get("caption") else []) + [""]
        elif b["type"] == "delimiter":
            md += ["---", ""]
        elif b["type"] == "table":
            rows = [[inline_markdown(c).replace("|", "\\|") for c in r] for r in d["content"]]
            head = rows[0] if d.get("withHeadings") else [""] * len(rows[0])
            body = rows[1:] if d.get("withHeadings") else rows
            md += ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)] + ["| " + " | ".join(r) + " |" for r in body] + [""]
    if content.get("sources"):
        md += ["## Sources", ""] + [f"- {r['name']}: [{r['title']}]({r['url']})" for r in content["sources"]] + [""]
    return "\n".join(md)


def _render_inline(value: str) -> str:
    s = clean_inline(value)   # never trust stored HTML
    return s.replace("<a href=", '<a target="_blank" rel="noopener" href=')


def render_blocks(blocks: list[dict]) -> str:
    out = []
    for b in blocks or []:
        t, d = b.get("type"), b.get("data") or {}
        if t == "paragraph":
            out.append(f"<p>{_render_inline(d.get('text'))}</p>")
        elif t == "header":
            tag = "h3" if d.get("level") == 3 else "h2"
            out.append(f"<{tag}>{_render_inline(d.get('text'))}</{tag}>")
        elif t == "list":
            tag = "ol" if d.get("style") == "ordered" else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{_render_inline(_item_text(i))}</li>" for i in d.get("items") or []) + f"</{tag}>")
        elif t == "quote":
            cap = f"<cite>{_render_inline(d['caption'])}</cite>" if d.get("caption") else ""
            out.append(f"<blockquote><p>{_render_inline(d.get('text'))}</p>{cap}</blockquote>")
        elif t == "delimiter":
            out.append("<hr>")
        elif t == "table":
            rows = d.get("content") or []
            if not rows:
                continue
            head = ""
            if d.get("withHeadings"):
                head = "<thead><tr>" + "".join(f"<th>{_render_inline(c)}</th>" for c in rows[0]) + "</tr></thead>"
                rows = rows[1:]
            body = "".join("<tr>" + "".join(f"<td>{_render_inline(c)}</td>" for c in r) + "</tr>" for r in rows)
            out.append(f'<div class="tablewrap"><table class="data">{head}<tbody>{body}</tbody></table></div>')
    return "\n".join(out)


def render_sources(sources: list[dict]) -> str:
    if not sources:
        return ""
    items = "".join(
        f'<li>{html.escape(s.get("name") or "")}: <a target="_blank" rel="noopener" href="{html.escape(s["url"], quote=True)}">'
        f'{html.escape(s.get("title") or s["url"])}</a></li>' for s in sources if _safe_href(s.get("url") or ""))
    return f"<h2>Sources</h2><ul>{items}</ul>" if items else ""


# --------------------------------------------------------------------------- guardrails
ILLUSTRATIVE = re.compile(r"illustrat|the numbers|worked example|example", re.I)


def _block_text(b: dict) -> str:
    d = b["data"]
    if b["type"] in ("paragraph", "header"):
        return inline_text(d["text"])
    if b["type"] == "list":
        return "\n".join(inline_text(i) for i in d["items"])
    if b["type"] == "quote":
        return inline_text(d["text"]) + (" " + inline_text(d["caption"]) if d.get("caption") else "")
    if b["type"] == "table":
        return "\n".join(" ".join(inline_text(c) for c in r) for r in d["content"])
    return ""


def guardrail_view(content: dict) -> dict:
    """Free-form shape for guardrails.check(freeform=True).

    Figures in an illustrative section (a heading mentioning 'example',
    'illustrative' or 'the numbers', or a paragraph starting 'Illustrative')
    are not required to appear in the sources; everything else is.
    """
    body, facts = [], []
    illustrative = False
    for b in content.get("blocks") or []:
        text = _block_text(b)
        if b["type"] == "header":
            illustrative = bool(ILLUSTRATIVE.search(text))
            body.append(text)
            continue
        body.append(text)
        if not illustrative and not text.lower().startswith("illustrative"):
            facts.append(text)
    return {"headline": content.get("headline", ""), "dek": content.get("dek", ""),
            "body": "\n".join(body), "facts": "\n".join([content.get("headline", ""), content.get("dek") or ""] + facts)}


def word_count(content: dict) -> int:
    v = guardrail_view(content)
    return len(" ".join([v["headline"], v["dek"] or "", v["body"]]).split())
