"""One-off import of the first-version SQLite database into PostgreSQL.

    python scripts/import_sqlite.py data/finance.db

Copies sources, raw items, story clusters, desk evaluations and run logs into
the `pipeline` schema (keeping their ids), and turns each old `articles` row
into a newsroom AI draft. Runs as ADMIN_DATABASE_URL. Refuses to run if the
pipeline schema already has data, so it can't double-import.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from newsroom.store import PROSE_KEYS, create_ai_draft  # noqa: E402
from pipeline.db import Database  # noqa: E402
from pipeline.settings import get_settings  # noqa: E402

TABLES = {
    "sources": ["id", "name", "country", "url", "kind", "reuse", "full_text", "active", "etag", "last_modified",
                "last_fetched_at", "last_status"],
    "story_clusters": ["id", "country", "headline", "lead_item_id", "item_count", "status", "created_at", "updated_at"],
    "raw_items": ["id", "source_id", "country", "guid", "url", "title", "summary", "author", "published_at", "fetched_at",
                  "fingerprint", "title_key", "topics", "cluster_id", "status", "filter_reason"],
    "evaluations": ["id", "cluster_id", "model", "wallet_impact", "breadth", "actionability", "urgency", "novelty", "flags",
                    "impact_score", "decision", "angle", "audience", "rationale", "created_at"],
    "pipeline_runs": ["id", "stage", "started_at", "finished_at", "stats", "error"],
}
JSON_COLS = {"topics", "flags", "audience", "stats"}
BOOL_COLS = {"full_text", "active"}
TS_COLS = {"last_fetched_at", "created_at", "updated_at", "published_at", "fetched_at", "started_at", "finished_at"}


def _ts(v):
    if v and isinstance(v, str) and not re.search(r"(Z|[+-]\d\d:?\d\d)$", v):
        return v.replace(" ", "T") + "+00:00"      # SQLite CURRENT_TIMESTAMP is UTC without an offset
    return v


def main(path: str) -> int:
    old = sqlite3.connect(path)
    old.row_factory = sqlite3.Row
    admin_url = get_settings().component_urls.get("admin")
    if not admin_url:
        print("ADMIN_DATABASE_URL is not set (run setup_mac.sh first)", file=sys.stderr)
        return 2
    db = Database(admin_url)
    if db.fetchone("select count(*) as n from pipeline.sources")["n"]:
        print("pipeline schema already has data; nothing imported.")
        return 0

    counts = {}
    for table, cols in TABLES.items():
        rows = old.execute(f"select {', '.join(cols)} from {table}").fetchall()
        for r in rows:
            vals = []
            for c in cols:
                v = r[c]
                if c in JSON_COLS:
                    v = db.j(json.loads(v) if v else ([] if c != "stats" else {}))
                elif c in BOOL_COLS:
                    v = bool(v)
                elif c in TS_COLS:
                    v = _ts(v)
                elif table == "story_clusters" and c == "status" and v == "written":
                    v = "drafted"
                vals.append(v)
            db.execute(f"insert into pipeline.{table} ({', '.join(cols)}) values ({', '.join('?' for _ in cols)})", vals)
        if "id" in cols and table != "sources":
            db.execute(f"select setval(pg_get_serial_sequence('pipeline.{table}', 'id'), "
                       f"coalesce((select max(id) from pipeline.{table}), 1))")
        counts[table] = len(rows)

    drafts = 0
    for a in old.execute("select * from articles order by id").fetchall():
        sections = json.loads(a["sections"] or "{}")
        ev = db.fetchone("select impact_score, angle from pipeline.evaluations where cluster_id = ? order by id desc limit 1",
                         (a["cluster_id"],)) or {}
        content = {
            "headline": a["headline"], "dek": a["dek"],
            "sections": {k: sections.get(k, "") for k in PROSE_KEYS},
            "action_steps": json.loads(a["action_steps"] or "[]"), "takeaways": json.loads(a["takeaways"] or "[]"),
            "topics": json.loads(a["topics"] or "[]"), "meta_description": a["meta_description"],
            "sources": json.loads(a["sources"] or "[]"),
        }
        create_ai_draft(db, cluster_id=a["cluster_id"], country=a["country"], slug=a["slug"], content=content,
                        guardrails=json.loads(a["guardrails"] or "{}"), model=a["model"] or "unknown",
                        impact_score=ev.get("impact_score"), angle=ev.get("angle"), attempts=a["attempts"] or 1)
        db.execute("update pipeline.story_clusters set status = 'drafted' where id = ?", (a["cluster_id"],))
        drafts += 1
    db.commit()
    db.close()
    print("imported:", ", ".join(f"{k}={v}" for k, v in counts.items()), f"| newsroom AI drafts={drafts}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "data" / "finance.db")))
