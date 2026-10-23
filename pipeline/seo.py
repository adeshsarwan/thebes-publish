"""SEO signals from the stories we cover.

Two sources of keywords for each story:
  1. the feed itself: RSS <category> / Atom category tags, captured at ingest
     (pipeline.raw_items.feed_tags);
  2. the source article's page: <meta name="keywords">, <meta name="news_keywords">,
     article:tag, article:section and JSON-LD "keywords", read when the story
     is drafted (robots.txt respected, only the start of the page is read).

They are ranked by how many sources use them and handed to the AI writer,
which picks our own focus keyword and keywords for the angle we take. Source
keywords are a hint, never copied wholesale; editors can change everything.
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from html.parser import HTMLParser

import requests

from .settings import get_settings

log = logging.getLogger(__name__)

MAX_PAGE_BYTES = 400_000
MAX_KEYWORDS = 12
_SPLIT = re.compile(r"[,;|\n]+")


def clean_keywords(values, limit: int = MAX_KEYWORDS) -> list[str]:
    """Trim, de-duplicate (case-insensitive) and drop junk; keeps first-seen order."""
    out, seen = [], set()
    for v in values or []:
        if v is None:
            continue
        for part in (_SPLIT.split(v) if isinstance(v, str) else [str(v)]):
            k = " ".join(part.split()).strip(" .#\"'")
            if not (2 <= len(k) <= 60) or k.lower() in seen or k.isdigit():
                continue
            if re.search(r"https?://|[<>{}]", k):
                continue
            seen.add(k.lower())
            out.append(k)
            if len(out) >= limit:
                return out
    return out


class _HeadParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, list[str]] = {}
        self.jsonld: list[str] = []
        self._in_ld = False
        self._in_title = False
        self.title = ""

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("name") or a.get("property") or a.get("itemprop") or "").lower()
            if key and a.get("content"):
                self.meta.setdefault(key, []).append(a["content"])
        elif tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self._in_ld = True
            self.jsonld.append("")
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_ld = False
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_ld:
            self.jsonld[-1] += data
        elif self._in_title:
            self.title += data


def _jsonld_keywords(blobs: list[str]) -> tuple[list[str], list[str]]:
    keywords, sections = [], []

    def walk(node):
        if isinstance(node, list):
            for n in node:
                walk(n)
        elif isinstance(node, dict):
            kw = node.get("keywords")
            if isinstance(kw, str):
                keywords.append(kw)
            elif isinstance(kw, list):
                keywords.extend(str(k) for k in kw)
            sec = node.get("articleSection")
            if isinstance(sec, str):
                sections.append(sec)
            elif isinstance(sec, list):
                sections.extend(str(s) for s in sec)
            for key in ("@graph", "mainEntity"):
                if key in node:
                    walk(node[key])

    for raw in blobs:
        try:
            walk(json.loads(raw.strip()))
        except (ValueError, TypeError):
            continue
    return keywords, sections


def parse_page_seo(html: str) -> dict:
    """SEO metadata from a page's HTML."""
    p = _HeadParser()
    try:
        p.feed(html)
    except Exception:  # broken markup: keep whatever was parsed
        pass
    m = p.meta
    ld_keywords, ld_sections = _jsonld_keywords(p.jsonld)
    first = lambda *keys: next((m[k][0].strip() for k in keys if m.get(k)), "")  # noqa: E731
    return {
        "keywords": clean_keywords(m.get("keywords", []) + m.get("news_keywords", []) + ld_keywords, 20),
        "tags": clean_keywords(m.get("article:tag", []) + m.get("og:article:tag", []), 20),
        "section": clean_keywords(m.get("article:section", []) + ld_sections, 3),
        "title": first("og:title", "twitter:title") or " ".join(p.title.split()),
        "description": first("description", "og:description", "twitter:description"),
    }


def fetch_page_seo(url: str) -> dict:
    """Read the start of a source page for its SEO metadata (robots.txt respected)."""
    from .writer import _allowed_by_robots   # shared robots check
    s = get_settings()
    if not url.startswith(("http://", "https://")) or not _allowed_by_robots(url, s.user_agent, s.http_timeout):
        return {}
    try:
        with requests.get(url, timeout=s.http_timeout, headers={"User-Agent": s.user_agent}, stream=True) as resp:
            if resp.status_code >= 400 or "html" not in resp.headers.get("Content-Type", "html"):
                return {}
            body = b""
            for chunk in resp.iter_content(32_768):
                body += chunk
                if len(body) >= MAX_PAGE_BYTES or b"</head>" in body.lower():
                    break
        return parse_page_seo(body.decode(resp.encoding or "utf-8", errors="replace"))
    except requests.RequestException as exc:
        log.info("SEO fetch failed for %s: %s", url, exc)
        return {}


def source_keywords(items: list[dict], pages: list[dict] | None = None, limit: int = 15) -> list[str]:
    """Keywords the sources use for this story, most widely used first."""
    counts: Counter = Counter()
    display: dict[str, str] = {}
    def add(values, weight=1):
        for k in clean_keywords(values, 50):
            counts[k.lower()] += weight
            display.setdefault(k.lower(), k)
    for it in items:
        tags = it.get("feed_tags")
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except ValueError:
                tags = []
        add(tags or [])
    for page in pages or []:
        add(page.get("keywords", []), 2)      # editors chose these on purpose
        add(page.get("tags", []))
        add(page.get("section", []))
    return [display[k] for k, _ in counts.most_common(limit)]


def seo_view(raw) -> dict:
    """A stored seo value (json text or dict) as a dict with every key present."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except ValueError:
            raw = {}
    raw = raw or {}
    return {
        "title": (raw.get("title") or "").strip(),
        "focus_keyword": (raw.get("focus_keyword") or "").strip(),
        "keywords": clean_keywords(raw.get("keywords") or [], 15),
        "source_keywords": clean_keywords(raw.get("source_keywords") or [], 20),
    }
