"""python -m storyapi serve [--port 8767]
python -m storyapi keys list
python -m storyapi keys create "Acme frontend" [--notes "..."]     (prints the key once)
python -m storyapi keys revoke <id>
"""
from __future__ import annotations

import argparse
import sys

from pipeline.db import connect


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="storyapi", description="Story Hub Stories API")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("serve"); p.add_argument("--port", type=int, default=8767)
    k = sub.add_parser("keys"); ks = k.add_subparsers(dest="op", required=True)
    ks.add_parser("list")
    c = ks.add_parser("create"); c.add_argument("name"); c.add_argument("--notes", default="")
    r = ks.add_parser("revoke"); r.add_argument("id", type=int)
    args = ap.parse_args(argv)

    if args.cmd == "serve":
        from .server import serve
        serve(args.port)
        return 0

    from . import keys
    db = connect("newsroom")        # key management writes site.api_clients with the newsroom login
    cli_admin = {"role": "admin", "email": "command line"}
    try:
        if args.op == "list":
            for row in keys.list_clients(db):
                state = "revoked" if row["revoked_at"] else "active"
                print(f"{row['id']:>4}  {row['key_prefix']}…  {state:<8} {row['name']}")
        elif args.op == "create":
            row, key = keys.create(db, cli_admin, args.name, args.notes)
            print(f"Key for {row['name']} (id {row['id']}). Shown once; store it safely:\n\n  {key}\n")
        elif args.op == "revoke":
            keys.revoke(db, cli_admin, args.id)
            print("revoked")
    except keys.KeyError_ as exc:
        print(exc, file=sys.stderr)
        return 1
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
