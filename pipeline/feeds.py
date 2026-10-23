"""Stage 1 — RSS/Atom ingest.

fetch -> parse -> normalise -> pre-filter -> tag topics -> dedupe-insert.

Uses `feedparser` when installed; otherwise falls back to a small built-in
parser that handles RSS 2.0, RSS 1.0 (RDF) and Atom. Dates go through our own
parser too, because some official feeds (e.g. FCA) emit non-RFC822 dates.
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

from .db import Database, utcnow
from .settings import get_settings
from .taxonomy import tag_topics
from .text import canonical_url, fingerprint, strip_html, title_key

try:
    import feedparser  # type: ignore
except ImportError:  # pragma: no cover - exercised when feedparser is absent
    feedparser = None

log = logging.getLogger(__name__)

MAX_AGE_DAYS = 14          # ignore backfill older than this on first fetch
MAX_SUMMARY_CHARS = 2000


@dataclass
class Entry:
    guid: str | None
    url: str
    title: str
    summary: str
    author: str | None
    published: datetime | None


# --------------------------------------------------------------------------- dates
_DATE_FORMATS = (
    "%A, %B %d, %Y - %H:%M",     # FCA: "Tuesday, October 13, 2026 - 16:15"
    "%B %d, %Y",
    "%d %B %Y",
    "%Y-%m-%d",
    "%Y-%m-%d %H:%M:%S",
)


def parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    raw = raw.strip()
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        dt = None
    if dt is None:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            dt = None
    if dt is None:
        for fmt in _DATE_FORMATS:
            try:
                dt = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# --------------------------------------------------------------------------- parsing
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(el: ET.Element, *names: str) -> str | None:
    for child in el:
        if _local(child.tag) in names:
            if child.text and child.text.strip():
                return child.text.strip()
    return None


def _atom_link(el: ET.Element) -> str | None:
    fallback = None
    for child in el:
        if _local(child.tag) == "link":
            href = child.get("href")
            rel = child.get("rel", "alternate")
            if href and rel == "alternate":
                return href
            fallback = fallback or href
    return fallback


def _builtin_parse(body: bytes) -> list[Entry]:
    body = body.lstrip(b"\xef\xbb\xbf \r\n\t")
    root = ET.fromstring(body)
    items = [el for el in root.iter() if _local(el.tag) in ("item", "entry")]
    out: list[Entry] = []
    for el in items:
        is_atom = _local(el.tag) == "entry"
        link = _atom_link(el) if is_atom else _child_text(el, "link")
        if not link:
            link = el.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about")
        title = _child_text(el, "title") or ""
        summary = _child_text(el, "description", "summary", "encoded", "content") or ""
        published = _child_text(el, "pubdate", "published", "updated", "date", "issued")
        out.append(
            Entry(
                guid=_child_text(el, "guid", "id"),
                url=(link or "").strip(),
                title=strip_html(title),
                summary=strip_html(summary)[:MAX_SUMMARY_CHARS],
                author=_child_text(el, "creator", "author", "name"),
                published=parse_date(published),
            )
        )
    return out


def _feedparser_parse(body: bytes) -> list[Entry]:
    parsed = feedparser.parse(body)
    out: list[Entry] = []
    for e in parsed.entries:
        published = None
        for key in ("published_parsed", "updated_parsed"):
            if e.get(key):
                published = datetime(*e[key][:6], tzinfo=timezone.utc)
                break
        if published is None:
            published = parse_date(e.get("published") or e.get("updated") or e.get("dc_date"))
        summary = e.get("summary") or ""
        if not summary and e.get("content"):
            summary = e["content"][0].get("value", "")
        out.append(
            Entry(
                guid=e.get("id"),
                url=(e.get("link") or "").strip(),
                title=strip_html(e.get("title", "")),
                summary=strip_html(summary)[:MAX_SUMMARY_CHARS],
                author=e.get("author"),
                published=published,
            )
        )
    return out


def parse_feed(body: bytes) -> list[Entry]:
    if feedparser is not None:
        entries = _feedparser_parse(body)
        if entries:
            return entries
    return _builtin_parse(body)


# --------------------------------------------------------------------------- fetching
def fetch(source: dict, session: requests.Session) -> tuple[bytes | None, dict]:
    """Returns (body or None if unchanged, cache headers)."""
    url = source["url"]
    if url.startswith("file://") or not re.match(r"^https?://", url):
        path = Path(url.removeprefix("file://"))
        return path.read_bytes(), {}
    s = get_settings()
    headers = {"User-Agent": s.user_agent, "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.5"}
    if source.get("etag"):
        headers["If-None-Match"] = source["etag"]
    if source.get("last_modified"):
        headers["If-Modified-Since"] = source["last_modified"]
    resp = session.get(url, headers=headers, timeout=s.http_timeout)
    if resp.status_code == 304:
        return None, {}
    resp.raise_for_status()
    return resp.content, {"etag": resp.headers.get("ETag"), "last_modified": resp.headers.get("Last-Modified")}


# --------------------------------------------------------------------------- registry
def sync_sources(db: Database) -> list[dict]:
    """Seed the sources table from feeds.yaml, then return the active sources.

    The database is the source of truth: editors add, edit and switch off feeds
    in the newsroom desk. feeds.yaml only adds sources that don't exist yet, so a
    feed an editor switched off stays off.
    """
    for src in get_settings().feeds.get("sources", []):
        if db.fetchone("select id from pipeline.sources where id = ?", (src["id"],)):
            continue
        db.execute(
            """insert into pipeline.sources (id, name, country, url, kind, reuse, full_text, active, added_by, created_at)
               values (?,?,?,?,?,?,?,?,?,?)""",
            (src["id"], src["name"], src["country"], src["url"], src["kind"], src.get("reuse", "link_only"),
             bool(src.get("full_text", False)), bool(src.get("active", True)), "config", utcnow()),
        )
    db.commit()
    return db.fetchall("select * from pipeline.sources where active = ? order by country, id", (True,))


def _prefilter(title: str) -> str | None:
    for pat in get_settings().editorial["desk"].get("prefilter_reject_patterns", []):
        if re.search(pat, title):
            return f"prefilter:{pat}"
    return None


# --------------------------------------------------------------------------- stage
def ingest(db: Database, only_country: str | None = None, only_source: str | None = None,
           countries: list[str] | None = None) -> dict:
    run_id = db.start_run("ingest")
    stats = {"sources": 0, "fetched": 0, "new": 0, "duplicates": 0, "filtered": 0, "too_old": 0, "errors": []}
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    session = requests.Session()
    try:
        for src in sync_sources(db):
            if only_country and src["country"] != only_country:
                continue
            if countries and src["country"] not in countries:
                continue
            if only_source and src["id"] != only_source:
                continue
            stats["sources"] += 1
            try:
                body, cache = fetch(src, session)
                if body is None:
                    db.execute("update pipeline.sources set last_fetched_at=?, last_status=? where id=?", (utcnow(), "304", src["id"]))
                    continue
                entries = parse_feed(body)
            except Exception as exc:  # one bad feed must not stop the run
                log.warning("feed %s failed: %s", src["id"], exc)
                stats["errors"].append(f"{src['id']}: {exc}")
                db.execute("update pipeline.sources set last_fetched_at=?, last_status=? where id=?", (utcnow(), f"error: {exc}"[:200], src["id"]))
                db.commit()
                continue

            for e in entries:
                if not e.url or not e.title:
                    continue
                stats["fetched"] += 1
                if e.published and e.published < cutoff:
                    stats["too_old"] += 1
                    continue
                reason = _prefilter(e.title)
                fp = fingerprint(canonical_url(e.url))
                new_id = db.insert(
                    """insert into pipeline.raw_items
                       (source_id, country, guid, url, title, summary, author, published_at, fetched_at,
                        fingerprint, title_key, topics, status, filter_reason)
                       values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       on conflict (fingerprint) do nothing
                       returning id""",
                    (
                        src["id"], src["country"], e.guid, e.url, e.title, e.summary, e.author,
                        e.published.isoformat() if e.published else None, utcnow(),
                        fp, title_key(e.title), db.j(tag_topics(src["country"], e.title, e.summary)),
                        "filtered" if reason else "new", reason,
                    ),
                )
                if new_id is None:
                    stats["duplicates"] += 1
                elif reason:
                    stats["filtered"] += 1
                else:
                    stats["new"] += 1

            db.execute(
                "update pipeline.sources set last_fetched_at=?, last_status=?, etag=?, last_modified=? where id=?",
                (utcnow(), "ok", cache.get("etag"), cache.get("last_modified"), src["id"]),
            )
            db.commit()
    except Exception as exc:
        db.rollback()
        db.finish_run(run_id, stats, error=str(exc))
        raise
    db.finish_run(run_id, stats)
    return stats
