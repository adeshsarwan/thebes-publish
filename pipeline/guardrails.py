"""Quality and compliance checks run on every generated story.

errors   -> the story cannot be marked ready (regenerate or human review)
warnings -> recorded for the reviewer, do not block
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .settings import get_settings
from .taxonomy import find_terms, foreign_terms
from .text import words

FACT_FIELDS = ("headline", "dek", "what_happened", "money_impact", "who_is_affected")

# Financial-looking figures: percentages, currency amounts, decimals, "basis points".
_NUM = re.compile(
    r"(?:[$£€]\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:bn|billion|m|million|k))?)"
    r"|(?:\d[\d,]*(?:\.\d+)?\s?(?:%|per ?cent|percentage points?|basis points|bps))"
    r"|(?:\d+\.\d+)"
    r"|(?:\d+\s?[¼½¾]\s?%?)",
    re.I,
)


@dataclass
class Report:
    issues: list[dict] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    def add(self, code: str, severity: str, detail: str) -> None:
        self.issues.append({"code": code, "severity": severity, "detail": detail})

    @property
    def errors(self) -> list[dict]:
        return [i for i in self.issues if i["severity"] == "error"]

    @property
    def passed(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict:
        return {"passed": self.passed, "issues": self.issues, "metrics": self.metrics}

    def feedback(self) -> str:
        return "\n".join(f"- {i['detail']}" for i in self.errors)


# --------------------------------------------------------------------------- originality
def originality(article_text: str, source_text: str, n: int = 5) -> dict:
    a, s = words(article_text), words(source_text)
    if len(a) < n or len(s) < n:
        return {"ngram_overlap": 0.0, "longest_shared_run": 0, "shared_run_example": ""}
    s_ngrams = {tuple(s[i:i + n]) for i in range(len(s) - n + 1)}
    a_ngrams = [tuple(a[i:i + n]) for i in range(len(a) - n + 1)]
    overlap = sum(1 for g in a_ngrams if g in s_ngrams) / len(a_ngrams)

    # Longest verbatim run, found by growing k while any k-gram is shared.
    longest, example = 0, ""
    k = n
    while True:
        sk = {tuple(s[i:i + k]) for i in range(len(s) - k + 1)}
        hit = next((a[i:i + k] for i in range(len(a) - k + 1) if tuple(a[i:i + k]) in sk), None)
        if not hit:
            break
        longest, example = k, " ".join(hit)
        k += 1
    if longest == 0:
        # shorter than n: report 0 (runs under n words are normal shared phrasing)
        example = ""
    return {"ngram_overlap": round(overlap, 3), "longest_shared_run": longest, "shared_run_example": example}


# --------------------------------------------------------------------------- numbers
def _canon_number(tok: str) -> str:
    t = tok.lower().replace(",", "").replace(" ", "")
    t = t.replace("percent", "%").replace("per cent", "%").replace("percentagepoints", "pp").replace("percentagepoint", "pp")
    t = t.replace("¼", ".25").replace("½", ".5").replace("¾", ".75")
    m = re.search(r"\d+(?:\.\d+)?", t)
    if not m:
        return t
    num = m.group(0)
    if "." in num:
        num = num.rstrip("0").rstrip(".")
    return num


def unsupported_numbers(text: str, source_text: str) -> list[str]:
    src = {_canon_number(t) for t in _NUM.findall(source_text)}
    # bare numbers in the source count too (e.g. "rate of 2.25 per cent" split oddly)
    src |= {n.rstrip("0").rstrip(".") if "." in n else n for n in re.findall(r"\d+(?:\.\d+)?", source_text.replace(",", ""))}
    out = []
    for tok in _NUM.findall(text):
        if _canon_number(tok) not in src:
            out.append(tok.strip())
    return sorted(set(out))


# --------------------------------------------------------------------------- main
def check(story: dict, source_text: str, country: str, freeform: bool = False) -> Report:
    """Run the guardrails.

    AI drafts use the fixed sections. `freeform=True` is for stories a person
    wrote in the block editor: the story has `body` (all text) and `facts`
    (text whose figures must be sourced) instead of fixed sections, so the
    section-structure checks don't apply; every other check is the same.
    """
    cfg = get_settings().editorial["guardrails"]
    rep = Report()

    # Structure
    if freeform:
        for f, sev in (("headline", "error"), ("body", "error"), ("dek", "warning")):
            if not (story.get(f) or "").strip():
                rep.add("missing_section", sev, f"The {'standfirst' if f == 'dek' else f} is empty.")
    for f in () if freeform else ("headline", "dek", "what_happened", "money_impact", "who_is_affected",
                                  "illustrative_example", "action_steps", "takeaways"):
        if not story.get(f):
            rep.add("missing_section", "error", f"Section '{f}' is empty.")
    headline = story.get("headline", "")
    if len(headline) > cfg["max_headline_chars"]:
        rep.add("headline_length", "error", f"Headline is {len(headline)} chars; max {cfg['max_headline_chars']}.")
    steps = story.get("action_steps") or []
    if not freeform and not 3 <= len(steps) <= 6:
        rep.add("action_steps_count", "warning", f"{len(steps)} action steps; expected 3-5.")

    full = story_text(story)
    wc = len(full.split())
    rep.metrics["word_count"] = wc
    if wc < cfg["min_words"]:
        rep.add("too_short", "error", f"Story is {wc} words; minimum {cfg['min_words']}. Deepen the money-impact analysis.")
    if wc > cfg["max_words"]:
        rep.add("too_long", "error", f"Story is {wc} words; maximum {cfg['max_words']}. Tighten it.")

    # Padding: repeated sentences inflate word count without adding value
    sentences = [x.strip().lower() for x in re.split(r"(?<=[.!?])\s+", full) if len(x.split()) >= 6]
    if sentences:
        dup_ratio = 1 - len(set(sentences)) / len(sentences)
        rep.metrics["repeated_sentence_ratio"] = round(dup_ratio, 3)
        if dup_ratio > 0.1:
            rep.add("repetition", "error", f"{dup_ratio:.0%} of sentences are repeated. Remove padding and add substance instead.")

    # Originality
    orig = originality(full, source_text)
    rep.metrics.update(orig)
    if orig["longest_shared_run"] > cfg["max_shared_run_words"]:
        rep.add("verbatim_run", "error",
                f"Copies a {orig['longest_shared_run']}-word run from the source: \"{orig['shared_run_example']}\". Rewrite it in our own words.")
    if orig["ngram_overlap"] > cfg["max_ngram_overlap"]:
        rep.add("ngram_overlap", "error",
                f"{orig['ngram_overlap']:.0%} of 5-word phrases match the source (max {cfg['max_ngram_overlap']:.0%}). Write it fresh rather than paraphrasing line by line.")

    # Compliance language
    lowered = full.lower()
    banned = [t for t in cfg.get("banned_terms", []) if t.lower() in lowered]
    if banned:
        rep.add("banned_terms", "error", f"Uses banned terms: {', '.join(banned)}.")
    for pat in cfg.get("advice_patterns", []):
        m = re.search(pat, full)
        if m:
            rep.add("personal_advice", "error", f"Reads as personalised investment advice: \"{m.group(0)}\".")

    # Facts: figures in factual sections must come from the sources
    if cfg.get("require_numbers_in_sources", True):
        facts = story.get("facts", "") if freeform else " ".join(str(story.get(f, "")) for f in FACT_FIELDS)
        bad = unsupported_numbers(facts, source_text)
        rep.metrics["unsupported_numbers"] = bad
        if bad:
            rep.add("unsupported_numbers", "error",
                    f"Figures not found in the source material: {', '.join(bad)}. Remove them or move them into the clearly labelled illustrative example.")

    ex = "" if freeform else story.get("illustrative_example", "")
    if ex and "illustrative" not in ex.lower():
        rep.add("example_label", "warning", "Worked example is not labelled 'Illustrative example'.")

    # Locale
    stray = find_terms(full, foreign_terms(country))
    if stray:
        rep.add("foreign_terms", "warning", f"Uses other markets' terms in a {country} story: {', '.join(stray)}.")

    return rep


def story_text(story: dict) -> str:
    if "body" in story:   # free-form (block editor) story
        return "\n".join(str(story.get(k) or "") for k in ("headline", "dek", "body") if story.get(k))
    parts = [story.get(k, "") for k in ("headline", "dek", "what_happened", "money_impact",
                                         "who_is_affected", "illustrative_example")]
    parts += list(story.get("action_steps") or []) + list(story.get("takeaways") or [])
    return "\n".join(str(p) for p in parts if p)
