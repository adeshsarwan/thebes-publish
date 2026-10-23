"""RSS sources, managed by editors from the newsroom desk.

Sources live in pipeline.sources. The newsroom login may add sources and switch
them on or off (granted in dbsetup); it cannot touch anything else in the
pipeline schema. A feed is fetched and parsed before it is saved, so a broken
URL never enters the rotation.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

import requests

from pipeline import categories
from pipeline.db import Database, utcnow
from pipeline.feeds import parse_feed
from pipeline.settings import COUNTRY_NAMES, get_settings
from pipeline.text import canonical_url, slugify

KINDS = {"official": "Official (central bank, regulator, tax or statistics agency)",
         "publisher": "Publisher (news site, blog)"}
REUSE = {"link_only": "Facts only, always link out (default)",
         "public_domain": "Public domain (e.g. US federal government)",
         "ogl": "UK Open Government Licence",
         "crown_copyright_ca": "Canadian Crown copyright"}
EDIT_ROLES = {"editor", "admin"}
MAX_BYTES = 5 * 1024 * 1024


class SourceError(Exception):
    pass


def can_manage(user: dict | None) -> bool:
    return bool(user) and user["role"] in EDIT_ROLES


def _check_public_url(url: str) -> str:
    url = (url or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise SourceError("Enter a full feed address starting with https://")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror:
        raise SourceError(f"Couldn't find the website {parts.hostname}.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise SourceError("That address points to a private or local network, which isn't allowed.")
    return url


def test_feed(url: str) -> dict:
    """Fetch and parse a feed. Returns {items, latest: [titles], newest} or raises SourceError."""
    url = _check_public_url(url)
    s = get_settings()
    headers = {"User-Agent": s.user_agent,
               "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.5"}
    try:
        for _ in range(4):                                # follow redirects ourselves, checking every hop
            resp = requests.get(url, timeout=s.http_timeout, stream=True, allow_redirects=False, headers=headers)
            if resp.is_redirect and resp.headers.get("Location"):
                url = _check_public_url(requests.compat.urljoin(url, resp.headers["Location"]))
                resp.close()
                continue
            break
        else:
            raise SourceError("That address redirects too many times.")
        resp.raise_for_status()
        body = resp.raw.read(MAX_BYTES + 1, decode_content=True)
    except requests.RequestException as exc:
        raise SourceError(f"The feed didn't load: {exc}")
    if len(body) > MAX_BYTES:
        raise SourceError("That feed is larger than 5 MB, which is too big to be a news feed.")
    try:
        entries = parse_feed(body)
    except Exception:
        entries = []
    entries = [e for e in entries if e.title and e.url]
    if not entries:
        raise SourceError("That address didn't return an RSS or Atom feed with any stories.")
    dated = sorted((e for e in entries if e.published), key=lambda e: e.published, reverse=True)
    return {
        "items": len(entries),
        "latest": [e.title for e in (dated or entries)[:3]],
        "newest": dated[0].published.isoformat(timespec="minutes") if dated else None,
    }


def add_source(db: Database, user: dict, *, name: str, country: str, url: str, kind: str = "publisher",
               reuse: str = "link_only", full_text: bool = False, tester=test_feed,
               category: str | None = None) -> dict:
    if not can_manage(user):
        raise SourceError("Only editors and admins can add sources.")
    category = category or categories.default()
    if not categories.valid(category):
        raise SourceError("Choose the category this source's stories belong to.")
    if not categories.can_access(user, category):
        raise SourceError(f"You can only add sources for categories you work on ({categories.scope_label(user)}).")
    name = (name or "").strip()
    if not name:
        raise SourceError("Give the source a name.")
    if country not in COUNTRY_NAMES:
        raise SourceError("Choose the country this source covers.")
    if kind not in KINDS:
        raise SourceError("Choose the kind of source.")
    if reuse not in REUSE:
        raise SourceError("Choose how the source's content may be used.")
    if full_text and reuse not in ("public_domain", "ogl"):
        raise SourceError("Full-page text can only be used for public-domain or Open Government Licence sources.")
    canon = canonical_url(url)
    for existing in db.fetchall("select id, name, url from pipeline.sources"):
        if canonical_url(existing["url"]) == canon:
            raise SourceError(f"That feed is already a source: {existing['name']}.")
    report = tester(url)
    base = f"{country.lower()}-{slugify(name, 40)}"
    sid, n = base, 2
    while db.fetchone("select id from pipeline.sources where id = ?", (sid,)):
        sid, n = f"{base}-{n}", n + 1
    db.execute(
        """insert into pipeline.sources (id, name, country, category, url, kind, reuse, full_text, active, added_by, created_at)
           values (?,?,?,?,?,?,?,?,?,?,?)""",
        (sid, name, country, category, url.strip(), kind, reuse, bool(full_text), True, user["email"], utcnow()))
    db.commit()
    return {"id": sid, **report}


def set_active(db: Database, user: dict, source_id: str, active: bool) -> None:
    if not can_manage(user):
        raise SourceError("Only editors and admins can change sources.")
    src = db.fetchone("select id, category from pipeline.sources where id = ?", (source_id,))
    if not src:
        raise SourceError("No such source.")
    if not categories.can_access(user, src.get("category")):
        raise SourceError(f"You don't work on {categories.name(src.get('category'))} sources.")
    db.execute("update pipeline.sources set active = ? where id = ?", (bool(active), source_id))
    db.commit()


def list_sources(db: Database) -> list[dict]:
    rows = db.fetchall(
        """select s.*, (select count(*) from pipeline.raw_items r where r.source_id = s.id) as items,
                  (select max(r.published_at) from pipeline.raw_items r where r.source_id = s.id) as newest
           from pipeline.sources s order by s.active desc, s.category, s.country, s.name""")
    return rows
