"""Newsroom command line.

    python -m newsroom seed-users                    # placeholder editor, copywriter, publisher, admin
    python -m newsroom serve [--port 8765]           # the desk web app
    python -m newsroom list [--status ai_draft]
    python -m newsroom show 12
    python -m newsroom act 12 approve --as maya [--note "..."] [--override]
    python -m newsroom act 12 assign --as maya --to daniel
    python -m newsroom act 12 publish --as sara
    python -m newsroom site-feed                     # what the website can read (site role)

    python -m newsroom create-admin                  # first login (asks for name, email, password)
    python -m newsroom set-password maya@...         # set someone's password (asks for it)
    python -m newsroom pull [--countries US,CA]      # fetch, score, draft and auto-publish now
    python -m newsroom tick [--dry-run]              # run any region whose daily slot is due
    python -m newsroom schedule install|status|uninstall
    python -m newsroom sources                       # list RSS sources
    python -m newsroom pickup 42 --as maya --angle "..." [--notes "..."] [--no-research]
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys

from pipeline.db import connect

from . import workflow as wf
from .store import get_story
from .users import find_user, list_users, seed_placeholders


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="newsroom", description="Story Hub newsroom")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed-users")
    p = sub.add_parser("serve"); p.add_argument("--port", type=int, default=8765)
    p = sub.add_parser("list"); p.add_argument("--status")
    p = sub.add_parser("show"); p.add_argument("id", type=int)
    p = sub.add_parser("act"); p.add_argument("id", type=int); p.add_argument("action", choices=sorted(wf.ACTIONS))
    p.add_argument("--as", dest="actor", required=True, help="user id, slug, email or first name")
    p.add_argument("--to", help="copywriter to assign"); p.add_argument("--note")
    p.add_argument("--override", action="store_true"); p.add_argument("--correction")
    sub.add_parser("users")
    sub.add_parser("site-feed")
    p = sub.add_parser("create-admin"); p.add_argument("--email"); p.add_argument("--name")
    p = sub.add_parser("set-password"); p.add_argument("user", help="email, slug or first name")
    p = sub.add_parser("pull"); p.add_argument("--countries"); p.add_argument("--trigger", default="cli")
    p.add_argument("--started-by"); p.add_argument("--mock", action="store_true"); p.add_argument("--no-fetch", action="store_true")
    p = sub.add_parser("tick"); p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("schedule"); p.add_argument("op", choices=["install", "uninstall", "status"])
    sub.add_parser("sources")
    p = sub.add_parser("pickup-run"); p.add_argument("request_id", type=int); p.add_argument("--mock", action="store_true")
    p = sub.add_parser("image-run", help="generate queued AI images (started by the desk)")
    p.add_argument("image_ids", type=int, nargs="+"); p.add_argument("--mock", action="store_true")
    p = sub.add_parser("pickup", help="editor override: research and draft a desk story now")
    p.add_argument("cluster_id", type=int); p.add_argument("--as", dest="actor", required=True)
    p.add_argument("--angle", required=True); p.add_argument("--audience", default=""); p.add_argument("--notes", default="")
    p.add_argument("--urls", default=""); p.add_argument("--no-research", action="store_true"); p.add_argument("--mock", action="store_true")
    args = ap.parse_args(argv)

    if args.cmd == "pickup-run":
        from pipeline.llm import get_client
        from .pickup import run_request
        story_id = run_request(args.request_id, get_client(args.mock))
        print(f"pick request {args.request_id} done: story {story_id}")
        return 0

    if args.cmd == "image-run":
        from pipeline.db import connect
        from pipeline.llm import get_client
        from .media import run_generation
        db = connect("newsroom")
        try:
            print(f"images {args.image_ids}: {run_generation(db, args.image_ids, get_client(args.mock))}")
        finally:
            db.close()
        return 0

    if args.cmd == "pull":
        from pipeline.llm import get_client
        from .pull import PullBusy, run_pull, summarise
        try:
            result = run_pull(args.countries, trigger=args.trigger, client=get_client(args.mock),
                              fetch_text=not args.no_fetch, started_by=args.started_by)
        except PullBusy as exc:
            print(exc, file=sys.stderr)
            return 1
        print(json.dumps(summarise(result), indent=2))
        return 0

    if args.cmd == "tick":
        from .schedule import tick
        for r in tick(dry_run=args.dry_run):
            print(json.dumps(r, default=str))
        return 0

    if args.cmd == "schedule":
        from . import schedule as sch
        if args.op == "install":
            print(f"installed {sch.install()}\nIt checks every 10 minutes; logs go to data/logs/scheduler.log")
        elif args.op == "uninstall":
            sch.uninstall()
            print("schedule removed")
        else:
            print(f"{sch.scheduler_name()}:", "installed" if sch.installed() else "not installed")
            for r in sch.plan():
                print(f"  {r['id']:<10} {','.join(r['countries']):<7} next {r['next_slot']:%a %d %b %H:%M} {r['timezone']}"
                      f"  = {r['next_slot_utc'].astimezone():%a %d %b %H:%M} on this machine")
        return 0

    if args.cmd == "serve":
        from .web import serve
        serve(args.port)
        return 0

    if args.cmd == "site-feed":
        site = connect("site")
        try:
            rows = site.fetchall(
                """select p.id, p.country, p.status, p.version, p.headline, a.display_name
                   from site.published_stories p join site.authors a on a.id = p.author_id order by p.id""")
            for r in rows:
                print(f"{r['id']:>4} {r['country']} {r['status']:<9} v{r['version']} {r['headline']}  — {r['display_name']}")
            if not rows:
                print("nothing published yet")
        finally:
            site.close()
        return 0

    db = connect("newsroom")
    try:
        if args.cmd == "seed-users":
            n = seed_placeholders(db)
            print(f"added {n} placeholder user(s)")
            for u in list_users(db):
                print(f"  {u['id']:>3} {u['role']:<10} {u['display_name']}{'  (placeholder)' if u['is_placeholder'] else ''}")
        elif args.cmd == "create-admin":
            from .auth import AuthError, create_user
            name = args.name or input("Full name: ").strip()
            email = args.email or input("Email: ").strip()
            pw = getpass.getpass("Password (10+ characters): ")
            if pw != getpass.getpass("Repeat password: "):
                print("passwords don't match", file=sys.stderr)
                return 1
            try:
                user, _ = create_user(db, None, email=email, display_name=name, role="admin", password=pw)
            except AuthError as exc:
                print(f"refused: {exc}", file=sys.stderr)
                return 1
            print(f"admin {user['display_name']} <{user['email']}> created; log in at http://localhost:8765")
        elif args.cmd == "set-password":
            from .auth import AuthError, check_password_rules, set_password
            u = find_user(db, args.user)
            if not u:
                print(f"no user {args.user!r}", file=sys.stderr)
                return 1
            pw = getpass.getpass(f"New password for {u['display_name']}: ")
            if pw != getpass.getpass("Repeat password: "):
                print("passwords don't match", file=sys.stderr)
                return 1
            try:
                check_password_rules(pw, u["email"])
            except AuthError as exc:
                print(f"refused: {exc}", file=sys.stderr)
                return 1
            set_password(db, u["id"], pw, must_change=False)
            print(f"password set for {u['display_name']}")
        elif args.cmd == "pickup":
            from pipeline.llm import get_client
            from .pickup import PickupError, request_pickup, run_request
            actor = find_user(db, args.actor)
            if not actor:
                print(f"no user {args.actor!r}", file=sys.stderr)
                return 1
            try:
                rid = request_pickup(db, actor, args.cluster_id, angle=args.angle, audience=args.audience,
                                     notes=args.notes, extra_urls=args.urls, research=not args.no_research)
            except PickupError as exc:
                print(f"refused: {exc}", file=sys.stderr)
                return 1
            story_id = run_request(rid, get_client(args.mock))
            print(f"story {story_id} drafted; open http://localhost:8765/story/{story_id}")
        elif args.cmd == "sources":
            from .sources import list_sources
            for r in list_sources(db):
                print(f"{'on ' if r['active'] else 'off'} {r['country']} {r['id']:<22} {r['items']:>4} items  "
                      f"{(r.get('last_status') or '-')[:20]:<20} {r['url']}")
        elif args.cmd == "users":
            for u in list_users(db):
                print(f"  {u['id']:>3} {u['role']:<10} {u['display_name']}  <{u['email']}>")
        elif args.cmd == "list":
            statuses = (args.status,) if args.status else tuple(wf.LABELS)
            for r in wf.queue(db, statuses):
                print(f"{r['id']:>4} {r['country']} {r['status']:<11} v{r['current_version']} {r['headline']}")
        elif args.cmd == "show":
            s = get_story(db, args.id)
            if not s:
                print(f"no story {args.id}", file=sys.stderr)
                return 1
            print(s["revision"]["body_md"])
            print(f"\nstatus={s['status']} version={s['current_version']} guardrails_passed="
                  f"{(s['revision'].get('guardrails') or {}).get('passed')}")
        elif args.cmd == "act":
            actor = find_user(db, args.actor)
            if not actor:
                print(f"no user {args.actor!r}; run: python -m newsroom users", file=sys.stderr)
                return 1
            assignee = find_user(db, args.to) if args.to else None
            try:
                s = wf.perform(db, args.id, args.action, actor, note=args.note,
                               assignee_id=assignee["id"] if assignee else None,
                               override=args.override, correction_note=args.correction)
            except wf.WorkflowError as exc:
                db.rollback()
                print(f"refused: {exc}", file=sys.stderr)
                return 1
            print(f"story {args.id}: {args.action} by {actor['display_name']} -> {s['status']}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
