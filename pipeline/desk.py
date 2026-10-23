"""Stage 3 — the editorial desk.

For every pending story cluster the model acts as an assignment editor: it
scores how much the event matters to a reader's money, flags promotional or
irrelevant items, and proposes the personal-finance *angle* the story should
take. We turn the scores into an impact score, decide run / hold / reject, and
commission the top stories per country.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .cluster import cluster_items
from .db import Database, country_filter, utcnow
from .llm import LLMClient, LLMError, model_name
from .settings import COUNTRY_NAMES, get_settings

log = logging.getLogger(__name__)

DIMENSIONS = ("wallet_impact", "breadth", "actionability", "urgency", "novelty")

EVAL_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        **{d: {"type": "INTEGER", "minimum": 1, "maximum": 5} for d in DIMENSIONS},
        "flags": {
            "type": "ARRAY",
            "items": {
                "type": "STRING",
                "enum": ["promotional", "product_review", "not_personal_finance", "corporate_admin",
                         "opinion_only", "insufficient_facts", "local_only"],
            },
        },
        "angle": {"type": "STRING"},
        "audience": {"type": "ARRAY", "items": {"type": "STRING"}},
        "rationale": {"type": "STRING"},
    },
    "required": [*DIMENSIONS, "flags", "angle", "audience", "rationale"],
}

SYSTEM = """You are the assignment editor of a personal-finance news site for {country_name} readers.
You decide which incoming items deserve an original story. We only run stories where
we can explain a concrete effect on ordinary people's money: borrowing costs, savings
returns, tax bills, benefits, prices, fees, consumer protections, or scams.

Score each dimension 1-5:
- wallet_impact: 5 = directly changes what many people pay or receive (rate decision,
  tax change, CPI print, new fee rules); 1 = no plausible effect on household money.
- breadth: 5 = most adults in {country_name}; 3 = a large group (all mortgage holders,
  all renters); 1 = a niche group or a single firm's customers.
- actionability: 5 = there are clear, general steps a reader can take now; 1 = nothing to do.
- urgency: 5 = deadline or effective date within weeks; 1 = no time element.
- novelty: 5 = genuinely new information; 1 = evergreen explainer or recycled news.

Flags (hard rejects): promotional (affiliate/product pitch), product_review,
not_personal_finance (markets/corporate news with no household angle),
corporate_admin (appointments, internal governance, events, banknote designs).
Soft flags: opinion_only, insufficient_facts, local_only.

The angle is one sentence naming the specific reader effect our story will explain,
e.g. "How the hold affects variable-rate mortgage payments and easy-access savings rates".
Audience is a list of concrete reader groups. Be strict: most items should NOT run."""


def _prompt(country: str, items: list[dict]) -> str:
    lines = [f"Country: {COUNTRY_NAMES.get(country, country)}", f"Items in this story cluster: {len(items)}", ""]
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


def impact_score(scores: dict, items: list[dict]) -> float:
    weights = get_settings().editorial["desk"]["weights"]
    weighted = sum(weights[d] * int(scores[d]) for d in DIMENSIONS)          # 1..5
    score = (weighted - 1) / 4 * 100                                         # 0..100
    if any(it["source_kind"] == "official" for it in items):
        score += 5                                                           # primary source available
    score += min(6, 3 * (len({it["source_id"] for it in items}) - 1))        # corroboration
    return round(max(0.0, min(100.0, score)), 1)


def decide(score: float, flags: list[str]) -> str:
    desk = get_settings().editorial["desk"]
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
        "select id, country from pipeline.story_clusters where status = 'pending'" + cf + " order by id"
        + (f" limit {int(limit)}" if limit else ""), cp
    )
    for c in pending:
        items = cluster_items(db, c["id"])
        if not items:
            continue
        try:
            out = client.generate_json(
                model=model,
                system=SYSTEM.format(country_name=COUNTRY_NAMES.get(c["country"], c["country"])),
                prompt=_prompt(c["country"], items),
                schema=EVAL_SCHEMA,
                temperature=0.1,
                task="evaluate",
            )
            scores = {d: max(1, min(5, int(out[d]))) for d in DIMENSIONS}
        except (LLMError, KeyError, ValueError, TypeError) as exc:
            log.warning("evaluation failed for cluster %s: %s", c["id"], exc)
            stats["errors"] += 1
            continue
        flags = list(out.get("flags") or [])
        score = impact_score(scores, items)
        decision = decide(score, flags)
        db.insert(
            """insert into pipeline.evaluations
               (cluster_id, model, wallet_impact, breadth, actionability, urgency, novelty,
                flags, impact_score, decision, angle, audience, rationale, created_at)
               values (?,?,?,?,?,?,?,?,?,?,?,?,?,?) returning id""",
            (c["id"], model, *[scores[d] for d in DIMENSIONS], db.j(flags), score, decision,
             out.get("angle"), db.j(out.get("audience") or []), out.get("rationale"), utcnow()),
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
        """select c.id as cluster_id, c.country, c.headline, e.impact_score, e.angle, e.audience, e.id as evaluation_id
           from pipeline.story_clusters c
           join pipeline.evaluations e on e.id = (select max(id) from pipeline.evaluations where cluster_id = c.id)
           where c.status = 'evaluated' and e.decision = 'run' and c.created_at >= ?""" + cf + """
           order by e.impact_score desc""",
        (since, *cp),
    )
    per_country: dict[str, int] = {}
    chosen = []
    for r in rows:
        n = per_country.get(r["country"], 0)
        if n >= int(desk.get("max_stories_per_country", 4)):
            continue
        per_country[r["country"]] = n + 1
        r["audience"] = db.unj(r["audience"], [])
        chosen.append(r)
        db.execute("update pipeline.story_clusters set status = 'commissioned', updated_at = ? where id = ?", (utcnow(), r["cluster_id"]))
    db.commit()
    return chosen


def desk_report(db: Database, limit: int = 30) -> list[dict]:
    rows = db.fetchall(
        """select c.id, c.country, c.headline, c.item_count, c.status, e.impact_score, e.decision, e.flags, e.angle
           from pipeline.story_clusters c
           left join pipeline.evaluations e on e.id = (select max(id) from pipeline.evaluations where cluster_id = c.id)
           order by coalesce(e.impact_score, -1) desc, c.id desc limit ?""",
        (limit,),
    )
    for r in rows:
        r["flags"] = db.unj(r.get("flags"), [])
    return rows

