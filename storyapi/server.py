"""The Stories API web server (read-only, JSON).

Runs as its own service (`python -m storyapi serve`) with the read-only `site`
database login, separate from the newsroom desk. Every endpoint except the index
and the OpenAPI description needs an API key, sent as `X-API-Key: sh_live_...`
or `Authorization: Bearer sh_live_...`.

  GET /v1/stories             list (filters: category, country, since, updated_since,
                              keyword, include_retracted; paging: page, per_page)
  GET /v1/stories/{slug}      one story with its full body, sources, SEO and disclosure
  GET /v1/categories          categories and how many live stories each has
  GET /v1/openapi.json        machine-readable description (no key needed)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

from pipeline import categories
from pipeline.db import connect
from pipeline.settings import COUNTRY_NAMES

from . import keys, stories

API_VERSION = "1.0"
RATE_PER_MINUTE = int(os.getenv("STORY_API_RATE_PER_MINUTE", "600"))


class _Rate:
    """Requests per key per rolling minute (in memory, per process)."""

    def __init__(self):
        self.hits: dict[int, list[float]] = {}
        self.lock = threading.Lock()

    def allow(self, client_id: int) -> tuple[bool, int]:
        now = time.monotonic()
        with self.lock:
            q = [t for t in self.hits.get(client_id, []) if now - t < 60]
            ok = len(q) < RATE_PER_MINUTE
            if ok:
                q.append(now)
            self.hits[client_id] = q
            return ok, max(0, RATE_PER_MINUTE - len(q))


rate = _Rate()


def public_base() -> str:
    return os.getenv("STORY_API_URL", "").rstrip("/")


class Handler(BaseHTTPRequestHandler):
    server_version = "StoryHubAPI/" + API_VERSION
    sys_version = ""

    # ---- responses ---------------------------------------------------------------
    def _json(self, code: int, payload: dict, headers: dict | None = None, cacheable: bool = False) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
        if cacheable and code == 200 and self.headers.get("If-None-Match") == etag:
            code, body = 304, b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, max-age=60" if cacheable else "no-store")
        if cacheable:
            self.send_header("ETag", etag)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Robots-Tag", "noindex")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Expose-Headers", "ETag, X-RateLimit-Remaining")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, code: int, err: str, message: str, headers: dict | None = None, extra: dict | None = None) -> None:
        self._json(code, {"error": {"code": err, "message": message}, **(extra or {})}, headers)

    def _key(self) -> str | None:
        k = self.headers.get("X-API-Key")
        if not k:
            auth = self.headers.get("Authorization") or ""
            if auth.lower().startswith("bearer "):
                k = auth[7:]
        return k

    def _base(self) -> str:
        if public_base():
            return public_base()
        host = self.headers.get("Host") or f"localhost:{self.server.server_address[1]}"
        return f"http://{host}"

    # ---- routing -----------------------------------------------------------------
    def do_OPTIONS(self):  # noqa: N802  (CORS preflight)
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "X-API-Key, Authorization, If-None-Match")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_HEAD(self):  # noqa: N802
        self.do_GET()

    def do_POST(self):  # noqa: N802
        self._error(405, "method_not_allowed", "This API is read-only: use GET.", {"Allow": "GET, HEAD, OPTIONS"})

    do_PUT = do_PATCH = do_DELETE = do_POST

    def do_GET(self):  # noqa: N802
        url = urlsplit(self.path)
        path = url.path.rstrip("/") or "/"
        q = {k: v[0] for k, v in parse_qs(url.query).items() if v}
        if path in ("/", "/v1"):
            return self._json(200, {"name": "Story Hub Stories API", "version": API_VERSION,
                                    "docs": self._base() + "/v1/openapi.json",
                                    "auth": "Send your key as the X-API-Key header (or Authorization: Bearer <key>).",
                                    "endpoints": ["/v1/stories", "/v1/stories/{slug}", "/v1/categories"]})
        if path == "/v1/openapi.json":
            return self._json(200, openapi(self._base()), cacheable=True)
        if "api_key" in q or "key" in q:
            return self._error(400, "key_in_url", "Send the API key in the X-API-Key header, not in the address.")
        db = connect("site")
        try:
            client = keys.verify(db, self._key())
            if not client:
                return self._error(401, "unauthorized", "Missing, unknown or revoked API key.",
                                   {"WWW-Authenticate": 'Bearer realm="stories"'})
            ok, remaining = rate.allow(client["id"])
            if not ok:
                return self._error(429, "rate_limited", f"Over {RATE_PER_MINUTE} requests a minute; slow down.",
                                   {"Retry-After": "60"})
            limit_h = {"X-RateLimit-Limit": str(RATE_PER_MINUTE), "X-RateLimit-Remaining": str(remaining)}
            if path == "/v1/stories":
                return self._list(db, q, limit_h)
            if m := re.fullmatch(r"/v1/stories/([\w-]{1,200})", path):
                return self._one(db, m.group(1), limit_h)
            if path == "/v1/categories":
                return self._json(200, {"data": stories.category_list(db)}, limit_h, cacheable=True)
            return self._error(404, "not_found", "No such endpoint. See /v1/openapi.json.")
        finally:
            db.close()

    def _list(self, db, q: dict, headers: dict) -> None:
        try:
            page = max(1, int(q.get("page", "1")))
            per_page = max(1, min(stories.MAX_PER_PAGE, int(q.get("per_page", "20"))))
        except ValueError:
            return self._error(400, "bad_request", "page and per_page must be whole numbers.")
        cat = q.get("category")
        if cat and not categories.valid(cat):
            return self._error(400, "bad_request", f"Unknown category {cat!r}. Use one of: {', '.join(categories.ids())}.")
        country = (q.get("country") or "").upper() or None
        if country and country not in COUNTRY_NAMES:
            return self._error(400, "bad_request", f"Unknown country. Use one of: {', '.join(COUNTRY_NAMES)}.")
        try:
            since = stories.parse_since(q.get("since"))
            updated_since = stories.parse_since(q.get("updated_since"))
        except ValueError:
            return self._error(400, "bad_request", "since and updated_since take a date (2026-10-20) or an ISO 8601 time.")
        include_retracted = (q.get("include_retracted") or "").lower() in ("1", "true", "yes")
        rows, total = stories.list_stories(db, category=cat, country=country, since=since, updated_since=updated_since,
                                           keyword=q.get("keyword"), include_retracted=include_retracted,
                                           page=page, per_page=per_page)
        pages = max(1, -(-total // per_page))
        nxt = None
        if page < pages:
            nxt = self._base() + "/v1/stories?" + urlencode({**q, "page": page + 1, "per_page": per_page})
        self._json(200, {"data": rows, "meta": {"page": page, "per_page": per_page, "total": total,
                                                 "pages": pages, "next": nxt}}, headers, cacheable=True)

    def _one(self, db, slug: str, headers: dict) -> None:
        r = stories.get_story(db, slug)
        if not r:
            return self._error(404, "not_found", "No published story with that slug.", headers)
        if r["status"] != "live":
            return self._error(410, "gone", "This story was unpublished. Remove it from your site.", headers,
                               {"data": {"id": r["story_id"], "slug": r["slug"], "status": "retracted",
                                         "updated_at": stories.iso(r.get("updated_at"))}})
        self._json(200, {"data": stories.story(r)}, headers, cacheable=True)

    def log_message(self, fmt, *args):   # never log keys; the path has none (keys in URLs are refused)
        super().log_message(fmt, *args)


# --------------------------------------------------------------------------- OpenAPI
def openapi(base: str) -> dict:
    cat_enum = categories.ids()
    story_summary = {"type": "object", "properties": {k: {} for k in stories.LIST_FIELDS + ("status",)}}
    return {
        "openapi": "3.0.3",
        "info": {"title": "Story Hub Stories API", "version": API_VERSION,
                 "description": "Read-only access to stories published by the Story Hub newsroom. "
                                "Times are UTC (ISO 8601). Bodies come as ready-to-use HTML (body_html) and as "
                                "Markdown; stories edited in the block editor also include their blocks. "
                                "Keep your key on your server: don't put it in browser code."},
        "servers": [{"url": base}],
        "components": {"securitySchemes": {"ApiKey": {"type": "apiKey", "in": "header", "name": "X-API-Key"}},
                       "schemas": {"StorySummary": story_summary}},
        "security": [{"ApiKey": []}],
        "paths": {
            "/v1/stories": {"get": {
                "summary": "List published stories, newest first",
                "parameters": [
                    {"name": "category", "in": "query", "schema": {"type": "string", "enum": cat_enum}},
                    {"name": "country", "in": "query", "schema": {"type": "string", "enum": list(COUNTRY_NAMES)}},
                    {"name": "since", "in": "query", "description": "first published at or after (date or ISO time)", "schema": {"type": "string"}},
                    {"name": "updated_since", "in": "query", "description": "changed at or after: use this to sync", "schema": {"type": "string"}},
                    {"name": "keyword", "in": "query", "description": "stories with this SEO keyword", "schema": {"type": "string"}},
                    {"name": "include_retracted", "in": "query", "description": "also list unpublished stories (status 'retracted') so you can remove them", "schema": {"type": "boolean"}},
                    {"name": "page", "in": "query", "schema": {"type": "integer", "minimum": 1}},
                    {"name": "per_page", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": stories.MAX_PER_PAGE}},
                ],
                "responses": {"200": {"description": "data: StorySummary[]; meta: page, per_page, total, pages, next"},
                              "401": {"description": "missing or revoked key"}, "429": {"description": "rate limited"}}}},
            "/v1/stories/{slug}": {"get": {
                "summary": "One story: body_html, body_markdown, blocks or sections, action steps, takeaways, "
                           "sources, SEO (title, description, focus_keyword, keywords), author, byline, disclosure",
                "parameters": [{"name": "slug", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": {"200": {"description": "data: Story"}, "404": {"description": "not found"},
                              "410": {"description": "unpublished: remove it from your site"}}}},
            "/v1/categories": {"get": {"summary": "Categories and their live story counts",
                                       "responses": {"200": {"description": "data: [{id, name, live_stories}]"}}}},
        },
    }


def serve(port: int = 8767) -> None:
    bind = os.getenv("STORY_API_BIND", "127.0.0.1")
    httpd = ThreadingHTTPServer((bind, port), Handler)
    print(f"Story Hub Stories API on http://{bind}:{port}  (read-only, site database login)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()

