"""Stage 2 — group items about the same event into story clusters.

A cluster is the unit the editorial desk evaluates and the writer works from:
three sources reporting one rate decision become one story with three
references, which gives the writer more facts and the desk a "multiple
sources" signal.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .db import Database, country_filter, utcnow
from .settings import get_settings
from .text import title_similarity


def cluster(db: Database, countries: list[str] | None = None) -> dict:
    run_id = db.start_run("cluster")
    desk = get_settings().editorial["desk"]
    threshold = float(desk.get("cluster_similarity", 0.45))
    since = (datetime.now(timezone.utc) - timedelta(hours=int(desk.get("lookback_hours", 72)))).isoformat()
    stats = {"items": 0, "new_clusters": 0, "joined": 0, "reopened": 0, "too_old": 0}

    # Items the feed published before the desk window are kept (so the desk can show
    # them) but marked, instead of waiting forever for a clustering that never comes.
    hours = int(desk.get("lookback_hours", 72))
    cf_old, cp_old = country_filter(countries)
    old = db.fetchall(
        "select id from pipeline.raw_items where status = 'new' and published_at is not null and published_at < ?" + cf_old,
        (since, *cp_old))
    for r in old:
        db.execute("update pipeline.raw_items set status = 'filtered', filter_reason = ? where id = ?",
                   (f"older than the {hours}-hour news window", r["id"]))
    stats["too_old"] = len(old)

    cf, cp = country_filter(countries, "r.country")
    items = db.fetchall(
        """select r.id, r.country, r.title, r.title_key, s.kind
           from pipeline.raw_items r join pipeline.sources s on s.id = r.source_id
           where r.status = 'new' and (r.published_at is null or r.published_at >= ?)""" + cf + """
           order by case when s.kind = 'official' then 0 else 1 end, r.published_at""",
        (since, *cp),
    )
    # Existing recent clusters, with every member's title_key for matching.
    clusters: dict[int, dict] = {}
    for row in db.fetchall(
        """select c.id, c.country, c.status, r.title_key
           from pipeline.story_clusters c join pipeline.raw_items r on r.cluster_id = c.id
           where c.created_at >= ?""",
        (since,),
    ):
        c = clusters.setdefault(row["id"], {"country": row["country"], "status": row["status"], "keys": []})
        c["keys"].append(row["title_key"])

    for it in items:
        stats["items"] += 1
        best_id, best_sim = None, 0.0
        for cid, c in clusters.items():
            if c["country"] != it["country"]:
                continue
            sim = max(title_similarity(it["title_key"], k) for k in c["keys"])
            if sim > best_sim:
                best_id, best_sim = cid, sim

        if best_id is not None and best_sim >= threshold:
            c = clusters[best_id]
            c["keys"].append(it["title_key"])
            db.execute(
                "update pipeline.story_clusters set item_count = item_count + 1, updated_at = ? where id = ?",
                (utcnow(), best_id),
            )
            # A held story that picks up another source deserves a second look.
            if c["status"] == "evaluated":
                last = db.fetchone(
                    "select decision from pipeline.evaluations where cluster_id = ? order by id desc limit 1", (best_id,)
                )
                if last and last["decision"] == "hold":
                    db.execute("update pipeline.story_clusters set status = 'pending' where id = ?", (best_id,))
                    c["status"] = "pending"
                    stats["reopened"] += 1
            cid = best_id
            stats["joined"] += 1
        else:
            cid = db.insert(
                """insert into pipeline.story_clusters (country, headline, lead_item_id, item_count, status, created_at, updated_at)
                   values (?, ?, ?, 1, 'pending', ?, ?) returning id""",
                (it["country"], it["title"], it["id"], utcnow(), utcnow()),
            )
            clusters[cid] = {"country": it["country"], "status": "pending", "keys": [it["title_key"]]}
            stats["new_clusters"] += 1

        db.execute("update pipeline.raw_items set cluster_id = ?, status = 'clustered' where id = ?", (cid, it["id"]))

    db.commit()
    db.finish_run(run_id, stats)
    return stats


def cluster_items(db: Database, cluster_id: int) -> list[dict]:
    rows = db.fetchall(
        """select r.*, s.name as source_name, s.kind as source_kind, s.reuse as source_reuse,
                  s.full_text as source_full_text
           from pipeline.raw_items r join pipeline.sources s on s.id = r.source_id
           where r.cluster_id = ?
           order by case when s.kind = 'official' then 0 else 1 end, r.published_at""",
        (cluster_id,),
    )
    for r in rows:
        r["topics"] = db.unj(r.get("topics"), [])
    return rows
