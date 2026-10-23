"""Pages editors add as sources while editing.

Their text counts as source material for the quality checks, so a figure an editor
checked on, say, gov.uk is no longer flagged once that page is listed as a source.
Pages are read once (robots.txt respected) and kept in newsroom.source_pages.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit

import requests

from pipeline.db import Database, utcnow
from pipeline.settings import get_settings

log = logging.getLogger(__name__)
MAX_CHARS = 60_000
MAX_SOURCES = 25
MAX_NEW_FETCHES = 5          # per save, so saving stays quick


class SourceListError(ValueError):
    pass


def clean_sources(raw) -> list[dict]:
    """[{name, title, url}] from the editor; web addresses only."""
    import json
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except ValueError as exc:
            raise SourceListError("the source list could not be read") from exc
    if not isinstance(raw, list):
        raise SourceListError("the source list could not be read")
    out, seen = [], set()
    for s in raw[:MAX_SOURCES + 5]:
        if not isinstance(s, dict):
            continue
        url = str(s.get("url") or "").strip()
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc or " " in url:
            raise SourceListError(f"Not a web address: {url[:80] or '(empty)'}")
        if url in seen:
            continue
        seen.add(url)
        host = parts.netloc.lower().removeprefix("www.")
        out.append({"name": re.sub(r"\s+", " ", str(s.get("name") or host)).strip()[:120] or host,
                    "title": re.sub(r"\s+", " ", str(s.get("title") or url)).strip()[:300] or url,
                    "url": url[:1000]})
    if len(out) > MAX_SOURCES:
        raise SourceListError(f"At most {MAX_SOURCES} sources.")
    return out


def _fetch(url: str) -> tuple[str, str]:
    """(title, main text) of a page, or ('', '') when it can't be read."""
    from bs4 import BeautifulSoup
    from pipeline.writer import _allowed_by_robots
    s = get_settings()
    if not _allowed_by_robots(url, s.user_agent, s.http_timeout):
        return "", ""
    try:
        r = requests.get(url, timeout=s.http_timeout, headers={"User-Agent": s.user_agent})
        r.raise_for_status()
        if "html" not in (r.headers.get("Content-Type") or "text/html"):
            return "", ""
    except requests.RequestException as exc:
        log.info("source page %s: %s", url, exc)
        return "", ""
    soup = BeautifulSoup(r.text, "html.parser")
    title = (soup.title.get_text(" ", strip=True) if soup.title else "")[:300]
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript"]):
        tag.decompose()
    root = soup.find("main") or soup.find("article") or soup.body or soup
    text = re.sub(r"[ \t]+", " ", root.get_text("\n", strip=True))
    return title, text[:MAX_CHARS]


def page(db: Database, url: str, fetch: bool = True) -> dict | None:
    row = db.fetchone("select url, title, text, fetched_at from newsroom.source_pages where url = ?", (url,))
    if row or not fetch:
        return row
    title, text = _fetch(url)
    db.execute(
        """insert into newsroom.source_pages (url, title, text, fetched_at) values (?,?,?,?)
           on conflict (url) do update set title = excluded.title, text = excluded.text, fetched_at = excluded.fetched_at""",
        (url, title, text, utcnow()))
    db.commit()
    return {"url": url, "title": title, "text": text}


def extra_source_text(db: Database, sources: list[dict], skip_urls: set[str]) -> str:
    """Text of the listed sources that aren't the story's feed items, for the quality checks."""
    parts, fetched = [], 0
    for s in sources or []:
        url = s.get("url")
        if not url or url in skip_urls:
            continue
        known = page(db, url, fetch=False)
        if known is None and fetched < MAX_NEW_FETCHES:
            known = page(db, url)
            fetched += 1
        if known and known.get("text"):
            parts.append(f"[added source] {s.get('name') or ''}: {s.get('title') or url}\n{known['text']}")
    return "\n\n".join(parts)


def fill_titles(db: Database, sources: list[dict]) -> list[dict]:
    """Use the page's own title for sources added without one (reads the page, which the checks reuse)."""
    fetched = 0
    for s in sources:
        if s.get("title") and s["title"] != s["url"]:
            continue
        known = page(db, s["url"], fetch=False)
        if known is None and fetched < MAX_NEW_FETCHES:
            known, fetched = page(db, s["url"]), fetched + 1
        if known and known.get("title"):
            s["title"] = known["title"][:300]
    return sources
