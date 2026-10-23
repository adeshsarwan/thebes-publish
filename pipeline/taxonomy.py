"""Regional keyword & taxonomy engine."""
from __future__ import annotations

import re
from functools import lru_cache

from .settings import COUNTRY_NAMES, get_settings
from .text import normalize


@lru_cache(maxsize=8)
def _patterns(country: str) -> dict[str, re.Pattern]:
    topics = get_settings().taxonomy.get("topics", {})
    out: dict[str, re.Pattern] = {}
    for topic, spec in topics.items():
        kws = [str(k) for k in list(spec.get("common", [])) + list(spec.get(country, []))]
        if not kws:
            continue
        alts = "|".join(re.escape(k.lower()) for k in sorted(kws, key=len, reverse=True))
        out[topic] = re.compile(rf"(?<![a-z0-9])(?:{alts})(?![a-z0-9])")
    return out


def tag_topics(country: str, *texts: str) -> list[str]:
    blob = normalize(" ".join(t for t in texts if t))
    return sorted(t for t, pat in _patterns(country).items() if pat.search(blob))


def glossary_for(country: str) -> dict[str, str]:
    gl = get_settings().taxonomy.get("glossary", {})
    return {concept: terms[country] for concept, terms in gl.items() if country in terms}


def locale_for(country: str) -> dict:
    loc = dict(get_settings().taxonomy.get("locale", {}).get(country, {}))
    loc["country_name"] = COUNTRY_NAMES.get(country, country)
    return loc


def foreign_terms(country: str) -> list[str]:
    """Terms exclusive to *other* markets (minus any this market shares)."""
    excl = get_settings().taxonomy.get("exclusive_terms", {})
    mine = {t.lower() for t in excl.get(country, [])}
    return sorted(
        {t for c, terms in excl.items() if c != country for t in terms if t.lower() not in mine}
    )


def find_terms(text: str, terms: list[str]) -> list[str]:
    """Case-sensitive for acronyms (all caps), case-insensitive otherwise."""
    hits = []
    for t in terms:
        flags = 0 if t.isupper() else re.I
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(t)}(?![A-Za-z0-9])", text, flags):
            hits.append(t)
    return hits
