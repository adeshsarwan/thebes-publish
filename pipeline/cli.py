"""Pipeline command line (ingest -> cluster -> desk scoring -> AI drafts).

    python -m pipeline.cli db-setup [--write-env]   # create schemas + roles (Postgres)
    python -m pipeline.cli ingest [--country UK] [--source uk-boe-news]
    python -m pipeline.cli cluster
    python -m pipeline.cli evaluate [--limit 20]
    python -m pipeline.cli write [--limit 5] [--no-fetch]
    python -m pipeline.cli run                     # all stages
    python -m pipeline.cli desk                    # what the scoring desk decided

AI drafts land in the newsroom; review and publish them with  python -m newsroom ...
Add --mock to any command to use the offline LLM stub.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from . import cluster as cluster_stage
from . import desk, feeds, writer
from .db import connect
from .llm import get_client
from .settings import get_settings


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pipeline", description="Finance Hub ingest + AI drafting pipeline")
    ap.add_argument("--mock", action="store_true", help="use the offline mock LLM")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("db-setup", help="create schemas, tables and login roles in Postgres")
    p.add_argument("--admin-url", help="defaults to ADMIN_DATABASE_URL")
    p.add_argument("--write-env", action="store_true", help="save role connection URLs into .env")
    p = sub.add_parser("ingest"); p.add_argument("--country"); p.add_argument("--source")
    sub.add_parser("cluster")
    p = sub.add_parser("evaluate"); p.add_argument("--limit", type=int)
    p = sub.add_parser("write"); p.add_argument("--limit", type=int); p.add_argument("--no-fetch", action="store_true")
    p = sub.add_parser("run"); p.add_argument("--country"); p.add_argument("--limit", type=int); p.add_argument("--no-fetch", action="store_true")
    p = sub.add_parser("desk"); p.add_argument("--limit", type=int, default=30)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.cmd == "db-setup":
        from .dbsetup import setup
        admin_url = args.admin_url or get_settings().component_urls.get("admin")
        if not admin_url:
            print("Set ADMIN_DATABASE_URL (e.g. postgresql://localhost/storyhub) or pass --admin-url", file=sys.stderr)
            return 2
        urls = setup(admin_url, write_env=args.write_env)
        print("Schemas pipeline, newsroom, site are ready. Roles: fh_pipeline, fh_newsroom, fh_site.")
        if args.write_env:
            print("Connection URLs written to .env:", ", ".join(urls))
        return 0

    db = connect("pipeline")
    try:
        if args.cmd == "ingest":
            _print(feeds.ingest(db, args.country, args.source))
        elif args.cmd == "cluster":
            _print(cluster_stage.cluster(db))
        elif args.cmd == "evaluate":
            _print(desk.evaluate(db, get_client(args.mock), args.limit))
        elif args.cmd == "write":
            _print(writer.write(db, get_client(args.mock), fetch_text=not args.no_fetch, limit=args.limit))
        elif args.cmd == "run":
            client = get_client(args.mock)
            out = {"ingest": feeds.ingest(db, args.country)}
            out["cluster"] = cluster_stage.cluster(db)
            out["evaluate"] = desk.evaluate(db, client)
            out["write"] = writer.write(db, client, fetch_text=not args.no_fetch, limit=args.limit)
            _print(out)
        elif args.cmd == "desk":
            for r in desk.desk_report(db, args.limit):
                score = "  -  " if r["impact_score"] is None else f"{r['impact_score']:5.1f}"
                print(f"{r['id']:>5} {r['country']} {score} {str(r['decision'] or r['status']):<7} "
                      f"x{r['item_count']} {r['headline'][:80]}"
                      + (f"  [{', '.join(r['flags'])}]" if r["flags"] else ""))
                if r.get("angle") and r.get("decision") == "run":
                    print(f"{'':>20}angle: {r['angle']}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
