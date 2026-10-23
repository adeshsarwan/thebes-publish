"""Stage 3 — the editorial desk.

For every pending story cluster the model acts as an assignment editor for the
cluster's category (config/categories.yaml): it scores the story on that
category's dimensions (for finance: how much it matters to a reader's money),
flags promotional or irrelevant items, and proposes the *angle* the story
should take. We turn the scores into an impact score, decide run / hold / reject, and
commission the top stories per country.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from . import categories
from .cluster import cluster_items
from .db import Database, country_filter, utcnow
from .llm import LLMClient, LLMError, model_name
from .settings import COUNTRY_NAMES, get_settings

log = logging.getLogger(__name__)

# Finance's dimensions, kept as named columns on pipeline.evaluations; every
# category's scores (including finance's) are also stored in evaluations.scores.
DIMENSIONS = ("wallet_impact", "breadth", "actionability", "urgency", "novelty")


def eval_schema(category: str | None) -> dict:
    """The structured answer the model must give for this category's rubric."""
    keys = categories.dimension_keys(category)
    flags = list(categories.desk(category).get("flags") or [])
    return {
        "type": "OBJECT",
        "properties": {
            **{d: {"type": "INTEGER", "minimum": 1, "maximum": 5} for d in keys},
            "flags": {"type": "ARRAY", "items": {"type": "STRING", "enum": flags}} if flags
            else {"type": "ARRAY", "items": {"type": "STRING"}},
            "angle": {"type": "STRING"},
            "audience": {"type": "ARRAY", "items": {"type": "STRING"}},
            "rationale": {"type": "STRING"},
            "image_query": {"type": "STRING", "description":
                            "3-6 plain words to search stock photo sites (Unsplash, Pexels) for a main image: "
                            "a generic scene, no names of people, brands or places"},
            "image_prompt": {"type": "STRING", "description":
                             "One or two sentences describing an editorial illustration for this story: a generic "
                             "scene, no text, logos or real people"},
        },
        "required": [*keys, "flags", "angle", "audience", "rationale"],
    }


def system_prompt(category: str | None, country: str) -> str:
    return categories.desk(category)["system"].format(country_name=COUNTRY_NAMES.get(country, country))


def _prompt(country: str, items: list[dict], category: str | None = None) -> str:
    lines = [f"Country: {COUNTRY_NAMES.get(country, country)}", f"Category: {categories.name(category)}",
             f"Items in this story cluster: {len(items)}", ""]
    for i, it in enumerate(items, 1):
        lines += [
            f"[{i}] source: {it['source_name']} ({it['source_kind']})",
            f"    published: {it.get('published_at') or 'unknown'}",
            f"    title: {it['title']}",
            f"    summary: {(it.get('summary') or '')[:700]}",
            f"    topics: {', '.join(it.get('topics') or []) or 'none'}",
        ]
    lines.append("\nEvaluate this story cluster.")
    return "\n".join(lines)


def impact_score(scores: dict, items: list[dict], category: str | None = None) -> float:
    dims = categories.dimensions(category)
    total = sum(float(d["weight"]) for d in dims) or 1.0
    weighted = sum(float(d["weight"]) * int(scores[d["key"]]) for d in dims) / total   # 1..5
    score = (weighted - 1) / 4 * 100                                         # 0..100
    if any(it["source_kind"] == "official" for it in items):
        score += 5                                                           # primary source available
    score += min(6, 3 * (len({it["source_id"] for it in items}) - 1))        # corroboration
    return round(max(0.0, min(100.0, score)), 1)


def decide(score: float, flags: list[str], category: str | None = None) -> str:
    desk = categories.desk(category)
    if set(flags) & set(desk.get("reject_flags", [])):
        return "reject"
    if "insufficient_facts" in flags and score < desk["run_threshold"] + 10:
        return "hold"
    if score >= desk["run_threshold"]:
        return "run"
    if score >= desk["hold_threshold"]:
        return "hold"
    return "reject"


def evaluate(db: Database, client: LLMClient, limit: int | None = None, countries: list[str] | None = None) -> dict:
    run_id = db.start_run("evaluate")
    stats = {"evaluated": 0, "run": 0, "hold": 0, "reject": 0, "errors": 0}
    model = model_name("desk", client)
    cf, cp = country_filter(countries)
    pending = db.fetchall(
        "select id, country, category from pipeline.story_clusters where status = 'pending'" + cf + " order by id"
        + (f" limit {int(limit)}" if limit else ""), cp
    )
    for c in pending:
        items = cluster_items(db, c["id"])
        if not items:
            continue
        cat = c.get("category") or categories.default()
        keys = categories.dimension_keys(cat)
        try:
            out = client.generate_json(
                model=model,
                system=system_prompt(cat, c["country"]),
                prompt=_prompt(c["country"], items, cat),
                schema=eval_schema(cat),
                temperature=0.1,
                task="evaluate",
            )
            scores = {d: max(1, min(5, int(out[d]))) for d in keys}
        except (LLMError, KeyError, ValueError, TypeError) as exc:
            log.warning("evaluation failed for cluster %s: %s", c["id"], exc)
            stats["errors"] += 1
            continue
        flags = list(out.get("flags") or [])
        score = impact_score(scores, items, cat)
        decision = decide(score, flags, cat)
        legacy = [scores.get(d) for d in DIMENSIONS] if set(DIMENSIONS) <= set(scores) else [None] * len(DIMENSIONS)
        db.insert(
            """insert into pipeline.evaluations
               (cluster_id, model, wallet_impact, breadth, actionability, urgency, novelty, scores,
                flags, impact_score, decision, angle, audience, rationale, image_query, image_prompt, created_at)
               values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) returning id""",
            (c["id"], model, *legacy, db.j(scores), db.j(flags), score, decision,
             out.get("angle"), db.j(out.get("audience") or []), out.get("rationale"),
             (out.get("image_query") or "")[:200] or None, (out.get("image_prompt") or "")[:1000] or None, utcnow()),
        )
        db.execute("update pipeline.story_clusters set status = 'evaluated', updated_at = ? where id = ?", (utcnow(), c["id"]))
        db.commit()
        stats["evaluated"] += 1
        stats[decision] += 1
    db.finish_run(run_id, stats)
    return stats


def commission(db: Database, countries: list[str] | None = None) -> list[dict]:
    """Pick the top 'run' stories per country that haven't been written yet."""
    desk = get_settings().editorial["desk"]
    since = (datetime.now(timezone.utc) - timedelta(hours=int(desk.get("lookback_hours", 72)))).isoformat()
    cf, cp = country_filter(countries, "c.country")
    rows = db.fetchall(
        """select c.id as cluster_id, c.country, c.category, c.headline, e.impact_score, e.angle, e.audience, e.image_query, e.image_prompt, e.id as evaluation_id
           from pipeline.story_clusters c
           join pipeline.evaluations e on e.id = (select max(id) from pipeline.evaluations where cluster_id = c.id)
           where c.status = 'evaluated' and e.decision = 'run' and c.created_at >= ?""" + cf + """
           order by e.impact_score desc""",
        (since, *cp),
    )
    per_slot: dict[tuple, int] = {}
    chosen = []
    for r in rows:
        slot = (r["country"], r.get("category") or categories.default())
        n = per_slot.get(slot, 0)
        if n >= int(desk.get("max_stories_per_country", 4)):   # per country and category
            continue
        per_slot[slot] = n + 1
        r["audience"] = db.unj(r["audience"], [])
        chosen.append(r)
        db.execute("update pipeline.story_clusters set status = 'commissioned', updated_at = ? where id = ?", (utcnow(), r["cluster_id"]))
    db.commit()
    return chosen


def desk_report(db: Database, limit: int = 30) -> list[dict]:
    rows = db.fetchall(
        """select c.id, c.country, c.category, c.headline, c.item_count, c.status, e.impact_score, e.decision, e.flags, e.angle
           from pipeline.story_clusters c
           left join pipeline.evaluations e on e.id = (select max(id) from pipeline.evaluations where cluster_id = c.id)
           order by coalesce(e.impact_score, -1) desc, c.id desc limit ?""",
        (limit,),
    )
    for r in rows:
        r["flags"] = db.unj(r.get("flags"), [])
    return rows

