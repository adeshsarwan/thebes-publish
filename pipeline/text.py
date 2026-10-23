"""Text utilities shared by ingest, clustering and guardrails."""
from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

STOPWORDS = frozenset(
    """a an and are as at be by for from has have in into is it its of on or that the
    to was were will with this these those after before over under new says said says
    how what why when who your you our we us more most than up down out about just""".split()
)
_TRACKING_PARAMS = re.compile(r"^(utm_|mc_|fbclid|gclid|ref$|source$|cmp$|icid$)", re.I)
_WORD = re.compile(r"[a-z0-9£$%.']+")


def strip_html(raw: str | None) -> str:
    if not raw:
        return ""
    text = BeautifulSoup(raw, "html.parser").get_text(" ")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query) if not _TRACKING_PARAMS.match(k)]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def fingerprint(*parts: str) -> str:
    return hashlib.sha1("|".join(p or "" for p in parts).encode("utf-8")).hexdigest()


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = text.replace("’", "'").replace("‘", "'")
    return text


def words(text: str) -> list[str]:
    return _WORD.findall(normalize(text))


def _stem(tok: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if len(tok) > 4 + len(suffix) - 1 and tok.endswith(suffix) and not tok.endswith("ss"):
            return tok[: -len(suffix)]
    return tok


def title_key(title: str) -> str:
    toks = [_stem(t.strip(".'")) for t in words(title) if t not in STOPWORDS and len(t) > 1]
    return " ".join(sorted({t for t in toks if t}))


def jaccard(a: str, b: str) -> float:
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def title_similarity(a: str, b: str, min_shared: int = 4) -> float:
    """Overlap coefficient when titles share enough words, else Jaccard.

    Different outlets phrase the same event differently ("Fed holds rates" vs
    "Federal Reserve issues FOMC statement"), which Jaccard under-scores. The
    overlap coefficient (shared / smaller set) catches those, and the
    min_shared floor stops short generic titles from merging.
    """
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    shared = len(sa & sb)
    if shared >= min_shared:
        return shared / min(len(sa), len(sb))
    return shared / len(sa | sb)


def slugify(text: str, max_len: int = 80) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return text[:max_len].rstrip("-")
