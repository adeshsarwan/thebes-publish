"""Story images: stock photos, AI illustrations and uploads.

* Stock photos come from Unsplash and Pexels. They are shown from the providers' own servers
  (their terms ask for this) with a credit to the photographer; we only keep a reference row.
  Unsplash also asks us to report a "download" when a photo is used in a story.
* AI images are made with Nano Banana (Gemini) and, like uploads, saved as WebP in several widths
  to Cloudflare R2, served from MEDIA_BASE_URL (e.g. https://media.thebesads.com).
* Without R2 keys, local runs save files under data/media and the desk serves them at /media/.
  In production without R2, generating and uploading are switched off; stock still works.

Rows live in newsroom.story_images. A story's images go to the site when the story is published
(see workflow.publish_to_site), so readers never see an image nobody approved.
"""
from __future__ import annotations

import io
import logging
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import requests

from pipeline import categories
from pipeline.db import Database, utcnow
from pipeline.settings import ROOT, get_settings

log = logging.getLogger(__name__)

UTM = {"utm_source": "story_hub", "utm_medium": "referral"}
STOCK_HOSTS = ("images.unsplash.com", "images.pexels.com")
ROLES = ("hero", "inline", "gallery")
MANAGE_ROLES = {"copywriter", "editor", "publisher", "admin"}
LOG_DIR = Path(os.getenv("FINANCE_DATA_DIR") or ROOT / "data") / "logs"   # same place as pull and pick-up logs


class MediaError(RuntimeError):
    pass


def cfg() -> dict:
    c = dict(get_settings().editorial.get("images") or {})
    c.setdefault("model", "gemini-nano-banana-2.1")
    c.setdefault("image_size", "2K")
    c.setdefault("default_aspect", "16:9")
    c.setdefault("aspects", ["16:9", "4:3", "1:1", "4:5", "3:2"])
    c.setdefault("max_variations", 4)
    c.setdefault("widths", [1600, 1024, 640, 320])
    c.setdefault("max_upload_mb", 15)
    c.setdefault("auto", {})
    c.setdefault("styles", {})
    if os.getenv("IMAGE_MODEL"):
        c["model"] = os.environ["IMAGE_MODEL"]
    return c


def style_names() -> list[str]:
    return list(cfg()["styles"]) or ["illustration"]


# --------------------------------------------------------------------------- storage
class R2Storage:
    """Cloudflare R2 through its S3-compatible API."""

    name = "Cloudflare R2"

    def __init__(self):
        import boto3   # imported here so the rest of the app runs without it
        from botocore.config import Config

        env = lambda k: os.environ[k].strip().strip('"').strip("'")  # noqa: E731  (stray spaces/quotes in .env)
        self.bucket = env("R2_BUCKET")
        self.base = env("MEDIA_BASE_URL").rstrip("/")
        self.key_id = env("R2_ACCESS_KEY_ID")
        self.endpoint = f"https://{env('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com"
        try:   # newer botocore adds checksum headers by default; send them only when an API requires them
            config = Config(retries={"max_attempts": 4, "mode": "standard"},
                            request_checksum_calculation="when_required", response_checksum_validation="when_required")
        except TypeError:
            config = Config(retries={"max_attempts": 4, "mode": "standard"})
        self.client = boto3.client("s3", endpoint_url=self.endpoint, aws_access_key_id=self.key_id,
                                   aws_secret_access_key=env("R2_SECRET_ACCESS_KEY"), region_name="auto", config=config)

    R2_HINTS = {
        "AccessDenied": "R2 refused it (AccessDenied): the key doesn't have write access to bucket {bucket}. "
                        "Check R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY come from the storyhub-server token "
                        "(Object Read & Write on storyhub-media) and R2_BUCKET is exactly storyhub-media.",
        "InvalidAccessKeyId": "R2 doesn't know this R2_ACCESS_KEY_ID: copy it again from the storyhub-server token.",
        "SignatureDoesNotMatch": "R2_SECRET_ACCESS_KEY doesn't match the key ID: copy the Secret Access Key again "
                                 "(make a new token if you no longer have it).",
        "NoSuchBucket": "There's no bucket called {bucket} in this account: check R2_BUCKET and R2_ACCOUNT_ID.",
    }

    def _error(self, exc) -> "MediaError":
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "") if hasattr(exc, "response") else ""
        hint = self.R2_HINTS.get(code)
        if hint:
            return MediaError(hint.format(bucket=self.bucket))
        return MediaError(f"Couldn't save to Cloudflare R2: {exc}")

    def put(self, key: str, data: bytes, content_type: str) -> str:
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type,
                                   CacheControl="public, max-age=31536000, immutable")
        except Exception as exc:   # botocore errors -> a message an editor can act on
            log.warning("R2 put %s failed: %s", key, exc)
            raise self._error(exc) from exc
        return f"{self.base}/{key}"

    def delete(self, key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            raise self._error(exc) from exc


class LocalStorage:
    """Files under data/media (or MEDIA_LOCAL_DIR), served by the desk at /media/. For local runs and tests."""

    name = "this computer (data/media)"

    def __init__(self, root: Path | None = None):
        self.root = Path(root or os.getenv("MEDIA_LOCAL_DIR") or ROOT / "data" / "media")
        self.base = os.getenv("MEDIA_BASE_URL", "/media").rstrip("/")

    def put(self, key: str, data: bytes, content_type: str) -> str:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return f"{self.base}/{key}"

    def delete(self, key: str) -> None:
        try:
            (self.root / key).unlink()
        except FileNotFoundError:
            pass

    def path_for(self, key: str) -> Path | None:
        if not re.fullmatch(r"[\w./-]{1,300}", key) or ".." in key:
            return None
        p = (self.root / key).resolve()
        return p if p.is_file() and self.root.resolve() in p.parents else None


R2_KEYS = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "MEDIA_BASE_URL")
_storage_cache: dict = {}


def r2_configured() -> bool:
    return all(os.getenv(k) for k in R2_KEYS)


def storage():
    """Where generated and uploaded files go, or None when nowhere is set up (production without R2)."""
    if r2_configured():
        if "r2" not in _storage_cache:
            _storage_cache["r2"] = R2Storage()
        return _storage_cache["r2"]
    if get_settings().app_env == "production":
        return None
    return LocalStorage()


def local_storage() -> LocalStorage | None:
    s = storage()
    return s if isinstance(s, LocalStorage) else None


def media_hosts() -> set[str]:
    """Hosts image blocks may point at: our media domain plus the stock providers' CDNs."""
    hosts = set(STOCK_HOSTS)
    base = os.getenv("MEDIA_BASE_URL", "")
    if base.startswith("http"):
        hosts.add(urlsplit(base).netloc)
    return hosts


def allowed_image_url(url: str) -> bool:
    if not url:
        return False
    if url.startswith("/media/") and ".." not in url:
        return not r2_configured() or os.getenv("MEDIA_BASE_URL", "").startswith("/")
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.netloc in media_hosts()


def status() -> dict:
    """What works on this server, for the desk to show."""
    st = storage()
    return {"storage": st.name if st else None, "unsplash": bool(os.getenv("UNSPLASH_ACCESS_KEY")),
            "pexels": bool(os.getenv("PEXELS_API_KEY")), "pixabay": bool(os.getenv("PIXABAY_API_KEY")),
            "generate": bool(st) and (get_settings().llm_provider == "mock" or bool(get_settings().gemini_api_key)),
            "model": cfg()["model"]}


def check_setup() -> list[tuple[str, bool, str]]:
    """For `python -m newsroom media-check`: what works, and what to fix. Never prints secrets."""
    out = []
    missing = [k for k in R2_KEYS if not os.getenv(k)]
    if missing:
        out.append(("R2 storage", False, "missing in .env: " + ", ".join(missing)))
    else:
        try:
            st = R2Storage()
            kid = st.key_id
            out.append(("R2 settings", True, f"bucket {st.bucket!r}, endpoint {st.endpoint}, key ID {kid[:4]}…{kid[-4:]} "
                                             f"({len(kid)} characters; R2 key IDs have 32), public address {st.base}"))
            key = f"healthcheck/{secrets.token_hex(4)}.txt"
            url = st.put(key, b"ok", "text/plain")
            st.delete(key)
            out.append(("R2 write", True, f"wrote and deleted a test file ({url})"))
        except MediaError as exc:
            out.append(("R2 write", False, str(exc)))
        except Exception as exc:   # noqa: BLE001
            out.append(("R2 write", False, f"{type(exc).__name__}: {exc}"))
    if os.getenv("UNSPLASH_ACCESS_KEY"):
        try:
            r = requests.get("https://api.unsplash.com/search/photos", params={"query": "kitchen", "per_page": 1},
                             timeout=TIMEOUT, headers={"Authorization": f"Client-ID {os.environ['UNSPLASH_ACCESS_KEY'].strip()}",
                                                       "Accept-Version": "v1"})
            left = r.headers.get("X-Ratelimit-Remaining", "?")
            out.append(("Unsplash", r.ok, f"HTTP {r.status_code}, {left} searches left this hour" if r.ok
                        else f"HTTP {r.status_code}: the key was refused"))
        except requests.RequestException as exc:
            out.append(("Unsplash", False, f"couldn't reach Unsplash: {exc}"))
    else:
        out.append(("Unsplash", False, "UNSPLASH_ACCESS_KEY is not in .env"))
    if (os.getenv("PIXABAY_API_KEY") or "").strip():
        try:
            r = requests.get("https://pixabay.com/api/", timeout=TIMEOUT,
                             params={"key": os.environ["PIXABAY_API_KEY"].strip(), "q": "kitchen", "per_page": 3})
            out.append(("Pixabay", r.ok, f"HTTP {r.status_code}, {r.headers.get('X-RateLimit-Remaining', '?')} searches left this minute"
                        if r.ok else f"HTTP {r.status_code}: the key was refused"))
        except requests.RequestException as exc:
            out.append(("Pixabay", False, f"couldn't reach Pixabay: {exc}"))
    else:
        out.append(("Pixabay", False, "PIXABAY_API_KEY is not in .env (optional)"))
    s = get_settings()
    out.append(("Gemini (AI images)", bool(s.gemini_api_key) or s.llm_provider == "mock",
                f"model {cfg()['model']}" if s.gemini_api_key else "GEMINI_API_KEY is not in .env"))
    return out


# --------------------------------------------------------------------------- image files
def process(data: bytes, widths: list[int] | None = None) -> tuple[list[tuple[int, bytes]], int, int]:
    """Check an image and make WebP copies at each width (never upscaled). Returns ([(width, bytes)], w, h)."""
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:  # pragma: no cover
        raise MediaError("Pillow isn't installed on this server (pip install Pillow)") from exc
    Image.MAX_IMAGE_PIXELS = 60_000_000
    try:
        img = Image.open(io.BytesIO(data))
        img.verify()
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
    except Exception as exc:
        raise MediaError("That file isn't an image we can read (use JPEG, PNG, WebP or GIF).") from exc
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
    w, h = img.size
    out = []
    for target in sorted({min(int(x), w) for x in (widths or cfg()["widths"])}, reverse=True):
        copy = img if target == w else img.resize((target, max(1, round(h * target / w))), Image.LANCZOS)
        buf = io.BytesIO()
        copy.save(buf, "WEBP", quality=82, method=4)
        out.append((target, buf.getvalue()))
    return out, w, h


def save_file(story_id: int, data: bytes, prefix: str) -> dict:
    st = storage()
    if not st:
        raise MediaError("Image storage isn't set up yet: add the R2 keys to the server's .env.")
    versions, w, h = process(data)
    stamp = datetime.now(timezone.utc).strftime("%Y/%m")
    stem = f"stories/{stamp}/{story_id}-{prefix}-{secrets.token_hex(5)}"
    sizes, keys = {}, []
    for width, blob in versions:
        key = f"{stem}-{width}.webp"
        sizes[str(width)] = st.put(key, blob, "image/webp")
        keys.append(key)
    largest = str(max(int(k) for k in sizes))
    return {"url": sizes[largest], "sizes": sizes, "width": w, "height": h, "storage_keys": keys}


# --------------------------------------------------------------------------- stock search
_cache: dict[tuple[str, str], tuple[float, dict]] = {}
_cache_lock = threading.Lock()
TIMEOUT = 15


def _remember(photo: dict) -> dict:
    with _cache_lock:
        if len(_cache) > 2000:
            _cache.clear()
        _cache[(photo["provider"], photo["provider_id"])] = (time.time(), photo)
    return photo


def _with_utm(url: str) -> str:
    return url + ("&" if "?" in url else "?") + urlencode(UTM) if url else url


def _unsplash(p: dict) -> dict:
    raw = p["urls"]["raw"]
    sizes = {str(w): f"{raw}&w={w}&q=80&fm=jpg&fit=max" for w in (1600, 1024, 640, 320)}
    user = p.get("user") or {}
    name = user.get("name") or "Unknown"
    return {"provider": "unsplash", "provider_id": p["id"], "url": sizes["1600"], "thumb": p["urls"].get("small"),
            "sizes": sizes, "width": p.get("width"), "height": p.get("height"),
            "alt": (p.get("alt_description") or p.get("description") or "").strip()[:300],
            "photographer": name, "photographer_url": _with_utm((user.get("links") or {}).get("html", "")),
            "source_url": _with_utm((p.get("links") or {}).get("html", "")),
            "credit": f"Photo by {name} on Unsplash", "credit_url": _with_utm((p.get("links") or {}).get("html", "")),
            "download_location": (p.get("links") or {}).get("download_location")}


def _pexels(p: dict) -> dict:
    orig = p["src"]["original"]
    sizes = {str(w): f"{orig}?auto=compress&cs=tinysrgb&w={w}" for w in (1600, 1024, 640, 320)}
    name = p.get("photographer") or "Unknown"
    return {"provider": "pexels", "provider_id": str(p["id"]), "url": sizes["1600"],
            "thumb": p["src"].get("medium") or sizes["640"], "sizes": sizes,
            "width": p.get("width"), "height": p.get("height"), "alt": (p.get("alt") or "").strip()[:300],
            "photographer": name, "photographer_url": p.get("photographer_url") or "",
            "source_url": p.get("url") or "", "credit": f"Photo by {name} on Pexels",
            "credit_url": p.get("url") or "https://www.pexels.com", "download_location": None}


PIXABAY_HOSTS = ("pixabay.com", "cdn.pixabay.com")


def _pixabay(h: dict) -> dict:
    """Pixabay forbids permanent hotlinking: these addresses are for the search results only.
    When a photo is used, add_stock copies it into our own storage (R2)."""
    name = h.get("user") or "Unknown"
    page_url = h.get("pageURL") or ""
    tags = (h.get("tags") or "").strip()
    return {"provider": "pixabay", "provider_id": str(h["id"]), "url": h.get("largeImageURL") or h.get("webformatURL"),
            "thumb": h.get("webformatURL") or h.get("previewURL"), "sizes": {},
            "width": h.get("imageWidth"), "height": h.get("imageHeight"),
            "alt": (tags[:1].upper() + tags[1:]) if tags else "",
            "photographer": name, "photographer_url": f"https://pixabay.com/users/{name}-{h.get('user_id')}/" if h.get("user_id") else "",
            "source_url": page_url, "credit": f"Image by {name} from Pixabay", "credit_url": page_url,
            "download_location": None, "copy": True}


_px_cache: dict[tuple, tuple[float, list]] = {}   # Pixabay asks for searches to be cached for 24 hours


def _pixabay_search(query: str, per_page: int, page: int, orientation: str | None) -> list[dict]:
    key = (os.getenv("PIXABAY_API_KEY") or "").strip()
    if not key:
        raise MediaError("no PIXABAY_API_KEY on this server")
    ck = (query.lower(), per_page, page, orientation)
    with _cache_lock:
        hit = _px_cache.get(ck)
    if hit and time.time() - hit[0] < 23 * 3600:
        return hit[1]
    params = {"key": key, "q": query, "image_type": "photo", "safesearch": "true", "per_page": max(3, per_page),
              "page": page, "lang": "en"}
    if orientation:
        params["orientation"] = orientation
    r = requests.get("https://pixabay.com/api/", params=params, timeout=TIMEOUT)
    r.raise_for_status()
    out = [_remember(_pixabay(h)) for h in r.json().get("hits", [])][:per_page]
    with _cache_lock:
        if len(_px_cache) > 500:
            _px_cache.clear()
        _px_cache[ck] = (time.time(), out)
    return out


ORIENT_PIXABAY = {"landscape": "horizontal", "portrait": "vertical"}
ORIENT = {"landscape": ("landscape", "landscape"), "portrait": ("portrait", "portrait"), "square": ("squarish", "square")}


# Photo sites can't filter by country, so searches add a place word (a setting cue, not a look for
# the people): "student loan" -> "student loan usa". Editors can switch it off in the search box.
COUNTRY_WORDS = {"US": "usa", "UK": "uk", "CA": "canada", "AU": "australia"}
COUNTRY_SYNONYMS = {"US": ("usa", "us", "america", "american", "united states"),
                    "UK": ("uk", "britain", "british", "england", "london", "united kingdom"),
                    "CA": ("canada", "canadian", "toronto", "vancouver"),
                    "AU": ("australia", "australian", "sydney", "melbourne")}


def with_country(query: str, country: str | None) -> str:
    word = COUNTRY_WORDS.get((country or "").upper())
    if not word:
        return query
    words = set(re.findall(r"[a-z]+", query.lower()))
    low = query.lower()
    if any((s in words) if " " not in s else (s in low) for s in COUNTRY_SYNONYMS[country.upper()]):
        return query
    return f"{query} {word}"


def search_stock(query: str, provider: str = "all", page: int = 1, per_page: int = 12,
                 orientation: str = "landscape", country: str | None = None) -> dict:
    """Search Unsplash and/or Pexels. Returns {"results": [...], "errors": {provider: message}, "query": used}.
    With `country`, a place word for that country is added to the search."""
    query = re.sub(r"\s+", " ", (query or "")).strip()[:100]
    if not query:
        raise MediaError("Type something to search for.")
    query = with_country(query, country)
    per_page = max(1, min(30, int(per_page)))
    page = max(1, min(50, int(page)))
    o_u, o_p = ORIENT.get(orientation, (None, None))
    results: dict[str, list] = {}
    errors: dict[str, str] = {}
    if provider == "all":   # only the providers this server has keys for (Pexels has paused new keys)
        want = tuple(p for p, k in (("unsplash", "UNSPLASH_ACCESS_KEY"), ("pixabay", "PIXABAY_API_KEY"),
                                    ("pexels", "PEXELS_API_KEY")) if (os.getenv(k) or "").strip())
        if not want:
            raise MediaError("Stock search isn't set up: add UNSPLASH_ACCESS_KEY or PIXABAY_API_KEY to the server's .env.")
    else:
        want = (provider,)
    each = per_page if len(want) == 1 else max(1, -(-per_page // len(want)))
    for prov in want:
        try:
            if prov == "unsplash":
                key = (os.getenv("UNSPLASH_ACCESS_KEY") or "").strip()
                if not key:
                    raise MediaError("no UNSPLASH_ACCESS_KEY on this server")
                params = {"query": query, "per_page": each, "page": page, "content_filter": "high"}
                if o_u:
                    params["orientation"] = o_u
                r = requests.get("https://api.unsplash.com/search/photos", params=params, timeout=TIMEOUT,
                                 headers={"Authorization": f"Client-ID {key}", "Accept-Version": "v1"})
                r.raise_for_status()
                results[prov] = [_remember(_unsplash(p)) for p in r.json().get("results", [])]
            elif prov == "pexels":
                key = (os.getenv("PEXELS_API_KEY") or "").strip()
                if not key:
                    raise MediaError("no PEXELS_API_KEY on this server")
                params = {"query": query, "per_page": each, "page": page}
                if o_p:
                    params["orientation"] = o_p
                r = requests.get("https://api.pexels.com/v1/search", params=params, timeout=TIMEOUT,
                                 headers={"Authorization": key})
                r.raise_for_status()
                results[prov] = [_remember(_pexels(p)) for p in r.json().get("photos", [])]
            elif prov == "pixabay":
                results[prov] = _pixabay_search(query, each, page, ORIENT_PIXABAY.get(orientation))
            else:
                raise MediaError(f"unknown provider {prov!r}")
        except (requests.RequestException, MediaError, KeyError, ValueError) as exc:
            msg = str(exc)
            if isinstance(exc, requests.HTTPError) and exc.response is not None:
                msg = {401: "the API key was refused", 403: "the hourly limit is used up or the key was refused",
                       429: "the hourly limit is used up"}.get(exc.response.status_code, f"HTTP {exc.response.status_code}")
            elif isinstance(exc, requests.RequestException):
                log.warning("stock search %s failed: %s", prov, exc)
                msg = "couldn't reach the photo site; try again in a minute"
            errors[prov] = msg
            results[prov] = []
    merged = []   # alternate providers so both show near the top
    lists = [results.get(p, []) for p in want]
    for i in range(max((len(x) for x in lists), default=0)):
        merged += [x[i] for x in lists if i < len(x)]
    return {"results": merged, "errors": errors, "query": query}


def stock_photo(provider: str, provider_id: str) -> dict:
    """One photo by id: from the recent-search cache, else fetched from the provider."""
    with _cache_lock:
        hit = _cache.get((provider, str(provider_id)))
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    if not re.fullmatch(r"[\w-]{1,64}", str(provider_id)):
        raise MediaError("bad photo id")
    try:
        if provider == "pixabay":
            r = requests.get("https://pixabay.com/api/", timeout=TIMEOUT,
                             params={"key": (os.getenv("PIXABAY_API_KEY") or "").strip(), "id": provider_id})
            r.raise_for_status()
            hits = r.json().get("hits") or []
            if not hits:
                raise MediaError("Pixabay doesn't have that image any more")
            return _remember(_pixabay(hits[0]))
        if provider == "unsplash":
            r = requests.get(f"https://api.unsplash.com/photos/{provider_id}", timeout=TIMEOUT,
                             headers={"Authorization": f"Client-ID {os.getenv('UNSPLASH_ACCESS_KEY', '')}",
                                      "Accept-Version": "v1"})
            r.raise_for_status()
            return _remember(_unsplash(r.json()))
        if provider == "pexels":
            r = requests.get(f"https://api.pexels.com/v1/photos/{provider_id}", timeout=TIMEOUT,
                             headers={"Authorization": os.getenv("PEXELS_API_KEY", "")})
            r.raise_for_status()
            return _remember(_pexels(r.json()))
    except (requests.RequestException, KeyError, ValueError) as exc:
        raise MediaError(f"couldn't load that photo from {provider}: {exc}") from exc
    raise MediaError(f"unknown provider {provider!r}")


def _report_unsplash_download(photo: dict) -> None:
    """Unsplash's terms: tell them when a photo is used. Best effort, never blocks."""
    loc = photo.get("download_location")
    key = (os.getenv("UNSPLASH_ACCESS_KEY") or "").strip()
    if photo.get("provider") != "unsplash" or not loc or not key:
        return
    try:
        requests.get(loc, timeout=TIMEOUT, headers={"Authorization": f"Client-ID {key}", "Accept-Version": "v1"})
    except requests.RequestException as exc:
        log.info("unsplash download report failed: %s", exc)


# --------------------------------------------------------------------------- permissions
def can_manage(user: dict | None, story: dict | None) -> bool:
    return bool(user and story and user["role"] in MANAGE_ROLES and story.get("status") != "killed"
                and categories.can_access(user, story.get("category")))


def _story(db: Database, story_id: int) -> dict:
    s = db.fetchone("select id, status, category, country, slug from newsroom.stories where id = ?", (story_id,))
    if not s:
        raise MediaError("story not found")
    return s


def _check(db: Database, user: dict | None, story_id: int) -> dict:
    s = _story(db, story_id)
    if user is not None and not can_manage(user, s):
        raise MediaError("You can't change images on this story.")
    return s


# --------------------------------------------------------------------------- rows
FIELDS = ("id", "story_id", "kind", "provider", "status", "role", "position", "prompt", "style", "aspect", "url",
          "sizes", "width", "height", "alt", "caption", "credit", "credit_url", "photographer", "photographer_url",
          "source_url", "provider_id", "model", "error", "created_at", "updated_at")


def _row(r: dict) -> dict:
    out = {k: r.get(k) for k in FIELDS}
    out["sizes"] = Database.unj(r.get("sizes"), {}) or {}
    for k in ("created_at", "updated_at"):
        out[k] = str(out[k]) if out[k] is not None else None
    return out


def list_images(db: Database, story_id: int, include_removed: bool = False) -> list[dict]:
    rows = db.fetchall(
        "select * from newsroom.story_images where story_id = ?" + ("" if include_removed else " and status <> 'removed'")
        + " order by case role when 'hero' then 0 when 'inline' then 1 else 2 end, id", (story_id,))
    return [_row(r) for r in rows]


def get_image(db: Database, image_id: int) -> dict | None:
    r = db.fetchone("select * from newsroom.story_images where id = ?", (image_id,))
    return _row(r) if r else None


def _make_hero(db: Database, story_id: int, image_id: int) -> None:
    db.execute("update newsroom.story_images set role = 'gallery', updated_at = ? where story_id = ? and role = 'hero' and id <> ?",
               (utcnow(), story_id, image_id))


def _role(role: str | None) -> str:
    return role if role in ROLES else "gallery"


def add_stock(db: Database, user: dict | None, story_id: int, provider: str, provider_id: str,
              role: str = "gallery", alt: str = "", caption: str = "", position: str | None = None,
              photo: dict | None = None) -> int:
    _check(db, user, story_id)
    photo = dict(photo or stock_photo(provider, provider_id))
    keys: list = []
    if photo.get("copy"):   # Pixabay: no permanent hotlinking, so keep our own copy in R2
        if not storage():
            raise MediaError("Pixabay images are copied to our storage, which isn't set up yet (R2 keys).")
        src = photo["url"] or ""
        if urlsplit(src).scheme != "https" or urlsplit(src).netloc not in PIXABAY_HOSTS:
            raise MediaError("unexpected Pixabay image address")
        try:
            r = requests.get(src, timeout=30)
            r.raise_for_status()
        except requests.RequestException as exc:
            raise MediaError(f"couldn't download the image from Pixabay: {exc}") from exc
        saved = save_file(story_id, r.content, "px")
        photo.update(url=saved["url"], sizes=saved["sizes"], width=saved["width"], height=saved["height"])
        keys = saved["storage_keys"]
    role = _role(role)
    iid = db.insert(
        """insert into newsroom.story_images (story_id, kind, provider, status, role, position, url, sizes, width, height,
             alt, caption, credit, credit_url, photographer, photographer_url, source_url, provider_id, storage_keys,
             created_by, created_at, updated_at)
           values (?, 'stock', ?, 'ready', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) returning id""",
        (story_id, photo["provider"], role, position, photo["url"], db.j(photo["sizes"]), photo.get("width"),
         photo.get("height"), (alt or photo.get("alt") or "").strip()[:300], (caption or "").strip()[:500] or None,
         photo["credit"], photo["credit_url"], photo["photographer"], photo["photographer_url"], photo["source_url"],
         photo["provider_id"], db.j(keys), user["id"] if user else None, utcnow(), utcnow()))
    if role == "hero":
        _make_hero(db, story_id, iid)
    db.commit()
    threading.Thread(target=_report_unsplash_download, args=(photo,), daemon=True).start()
    return iid  # type: ignore[return-value]


def add_prompts(db: Database, user: dict | None, story_id: int, prompts: list[dict]) -> list[int]:
    """Save prompts as 'draft' images, generated later on request."""
    ids = []
    for p in prompts:
        text = (p.get("prompt") or "").strip()
        if not text:
            continue
        ids.append(db.insert(
            """insert into newsroom.story_images (story_id, kind, provider, status, role, prompt, style, aspect,
                 created_by, created_at, updated_at)
               values (?, 'generated', 'gemini', 'draft', ?, ?, ?, ?, ?, ?, ?) returning id""",
            (story_id, _role(p.get("role")), text[:2000], p.get("style") if p.get("style") in style_names() else style_names()[0],
             p.get("aspect") if p.get("aspect") in cfg()["aspects"] else cfg()["default_aspect"],
             user["id"] if user else None, utcnow(), utcnow())))
    db.commit()
    return ids


def queue_generation(db: Database, user: dict | None, story_id: int, prompt: str, style: str | None = None,
                     aspect: str | None = None, count: int = 1, role: str = "gallery",
                     draft_id: int | None = None) -> list[int]:
    """Create 'queued' rows (or queue an existing draft). Run them with run_generation / start_background."""
    _check(db, user, story_id)
    if not status()["generate"]:
        raise MediaError("Image generation isn't set up on this server (needs the Gemini key and R2 storage).")
    prompt = (prompt or "").strip()
    if len(prompt) < 8:
        raise MediaError("Describe the image in a few more words.")
    style = style if style in style_names() else style_names()[0]
    aspect = aspect if aspect in cfg()["aspects"] else cfg()["default_aspect"]
    count = max(1, min(int(cfg()["max_variations"]), int(count or 1)))
    ids = []
    if draft_id:
        d = get_image(db, draft_id)
        if not d or d["story_id"] != story_id or d["status"] not in ("draft", "failed"):
            raise MediaError("That prompt was already used.")
        db.execute("""update newsroom.story_images set status = 'queued', prompt = ?, style = ?, aspect = ?, error = null,
                      updated_at = ? where id = ?""", (prompt[:2000], style, aspect, utcnow(), draft_id))
        ids.append(draft_id)
        count -= 1
    for _ in range(count):
        ids.append(db.insert(
            """insert into newsroom.story_images (story_id, kind, provider, status, role, prompt, style, aspect,
                 created_by, created_at, updated_at)
               values (?, 'generated', 'gemini', 'queued', ?, ?, ?, ?, ?, ?, ?) returning id""",
            (story_id, _role(role) if not ids else "gallery", prompt[:2000], style, aspect,
             user["id"] if user else None, utcnow(), utcnow())))
    db.commit()
    return ids


def full_prompt(prompt: str, style: str | None, country: str | None = None) -> str:
    prefix = (cfg()["styles"].get(style or "") or "").strip()
    text = (prefix + "\n\nScene: " + prompt.strip()) if prefix else prompt.strip()
    from pipeline.settings import COUNTRY_NAMES
    name = COUNTRY_NAMES.get((country or "").upper())
    if name:
        text += (f"\nSetting: {name}. Places, homes, streets, money and signage should look like {name}; "
                 f"any people should look like everyday residents of {name}, reflecting its diversity.")
    return text


def run_generation(db: Database, image_ids: list[int], client=None) -> dict:
    """Generate queued images one after another. Used by the background job and by auto-publish."""
    from pipeline.llm import LLMError, get_client

    stats = {"ready": 0, "failed": 0}
    client = client or get_client()
    c = cfg()
    for iid in image_ids:
        row = get_image(db, iid)
        if not row or row["status"] not in ("queued", "draft", "failed"):
            continue
        db.execute("update newsroom.story_images set status = 'generating', updated_at = ? where id = ?", (utcnow(), iid))
        db.commit()
        try:
            st_row = db.fetchone("select country from newsroom.stories where id = ?", (row["story_id"],)) or {}
            data, _mime = client.generate_image(model=c["model"],
                                                prompt=full_prompt(row["prompt"] or "", row["style"], st_row.get("country")),
                                                aspect=row["aspect"] or c["default_aspect"], size=c["image_size"])
            saved = save_file(row["story_id"], data, "ai")
            db.execute(
                """update newsroom.story_images set status = 'ready', url = ?, sizes = ?, width = ?, height = ?,
                     storage_keys = ?, model = ?, alt = coalesce(alt, ?), credit = ?, error = null, updated_at = ?
                   where id = ?""",
                (saved["url"], db.j(saved["sizes"]), saved["width"], saved["height"], db.j(saved["storage_keys"]),
                 c["model"], (row["prompt"] or "")[:200], "Illustration generated with AI", utcnow(), iid))
            stats["ready"] += 1
        except (LLMError, MediaError, Exception) as exc:   # noqa: BLE001  (a bad image must not stop the rest)
            log.warning("image %s failed: %s", iid, exc)
            db.execute("update newsroom.story_images set status = 'failed', error = ?, updated_at = ? where id = ?",
                       (str(exc)[:500], utcnow(), iid))
            stats["failed"] += 1
        db.commit()
    return stats


def start_background(image_ids: list[int]) -> None:
    if not image_ids:
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_fh = open(LOG_DIR / f"images-{image_ids[0]}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.log", "ab")
    subprocess.Popen([sys.executable, "-m", "newsroom", "image-run", *map(str, image_ids)], cwd=str(ROOT),
                     stdout=log_fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    log_fh.close()


def clean_credit(credit: str) -> tuple[str, str | None]:
    """Plain-text credit plus its first link. Accepts the HTML snippet Unsplash's "copy credit" gives."""
    import html as _html
    raw = (credit or "").strip()
    m = re.search(r'href=["\']([^"\']+)["\']', raw)
    link = m.group(1) if m and m.group(1).startswith("https://") else None
    text = re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]*>", "", raw))).strip()[:200]
    return text, link


def add_upload(db: Database, user: dict | None, story_id: int, data: bytes, filename: str = "",
               role: str = "gallery", alt: str = "", caption: str = "", credit: str = "") -> int:
    _check(db, user, story_id)
    credit, credit_url = clean_credit(credit)
    if len(data) > int(cfg()["max_upload_mb"]) * 1024 * 1024:
        raise MediaError(f"That file is over {cfg()['max_upload_mb']} MB.")
    if not data:
        raise MediaError("The file was empty.")
    saved = save_file(story_id, data, "up")
    role = _role(role)
    iid = db.insert(
        """insert into newsroom.story_images (story_id, kind, provider, status, role, url, sizes, width, height,
             storage_keys, alt, caption, credit, credit_url, prompt, created_by, created_at, updated_at)
           values (?, 'upload', 'upload', 'ready', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) returning id""",
        (story_id, role, saved["url"], db.j(saved["sizes"]), saved["width"], saved["height"], db.j(saved["storage_keys"]),
         alt.strip()[:300] or None, caption.strip()[:500] or None, credit or None, credit_url,
         (filename or "")[:200] or None, user["id"] if user else None, utcnow(), utcnow()))
    if role == "hero":
        _make_hero(db, story_id, iid)
    db.commit()
    return iid  # type: ignore[return-value]


def update_image(db: Database, user: dict | None, image_id: int, **fields) -> dict:
    img = get_image(db, image_id)
    if not img or img["status"] == "removed":
        raise MediaError("image not found")
    _check(db, user, img["story_id"])
    sets, params = [], []
    if "role" in fields and fields["role"] is not None:
        role = _role(fields["role"])
        if role == "hero" and img["status"] != "ready":
            raise MediaError("Only a finished image can be the main image.")
        sets.append("role = ?"); params.append(role)
        if role == "hero":
            _make_hero(db, img["story_id"], image_id)
    for k, n in (("alt", 300), ("caption", 500), ("prompt", 2000)):
        if k in fields and fields[k] is not None:
            sets.append(f"{k} = ?"); params.append(str(fields[k]).strip()[:n] or None)
    if "credit" in fields and fields["credit"] is not None and img["kind"] == "upload":
        text, link = clean_credit(str(fields["credit"]))
        sets.append("credit = ?"); params.append(text or None)
        sets.append("credit_url = ?"); params.append(link)
    if "position" in fields:
        pos = fields["position"] or None
        if pos and pos not in categories.SECTION_KEYS:
            raise MediaError("unknown section")
        sets.append("position = ?"); params.append(pos)
    if sets:
        db.execute(f"update newsroom.story_images set {', '.join(sets)}, updated_at = ? where id = ?",
                   (*params, utcnow(), image_id))
        db.commit()
    return get_image(db, image_id)  # type: ignore[return-value]


def remove_image(db: Database, user: dict | None, image_id: int) -> None:
    """Hide an image from the story. Files stay in R2 while a published copy may still use them."""
    img = get_image(db, image_id)
    if not img:
        raise MediaError("image not found")
    _check(db, user, img["story_id"])
    db.execute("update newsroom.story_images set status = 'removed', role = 'gallery', updated_at = ? where id = ?",
               (utcnow(), image_id))
    db.commit()


# --------------------------------------------------------------------------- publishing
SNAPSHOT_KEYS = ("id", "role", "kind", "provider", "url", "sizes", "width", "height", "alt", "caption", "credit",
                 "credit_url", "photographer", "photographer_url", "source_url", "position")


def snapshot(db: Database, story_id: int) -> list[dict]:
    """The images the site gets: the main image and in-story images, ready ones only."""
    out = []
    for img in list_images(db, story_id):
        if img["status"] == "ready" and img["role"] in ("hero", "inline") and img["url"]:
            out.append({k: img.get(k) for k in SNAPSHOT_KEYS})
    return out


# --------------------------------------------------------------------------- automatic choice
CHOOSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "choice": {"type": "INTEGER", "description": "number of the best photo, or 0 if none fits"},
        "inline": {"type": "ARRAY", "items": {"type": "INTEGER"}, "description": "other good photos for inside the story"},
        "alt": {"type": "STRING", "description": "alt text for the chosen photo, describing what it shows"},
        "reason": {"type": "STRING"},
    },
    "required": ["choice", "alt", "reason"],
}
CHOOSE_SYSTEM = """You are a picture editor for a news website. Choose stock photos for a story.
A good photo fits the story's subject and tone, shows a generic scene (no recognisable brands,
logos, celebrities or text that would mislead), and would not embarrass anyone pictured.
The story is for readers in one country: prefer photos whose setting fits that country (its money,
signage, streets, homes, landmarks) and skip photos clearly set somewhere else. Never choose or reject
a photo because of the ethnicity of the people in it.
Answer 0 when nothing is a reasonable fit; an illustration will be made instead."""


from pipeline.settings import COUNTRY_NAMES as COUNTRY_NAMES_  # noqa: E402


def _choose_prompt(story: dict, candidates: list[dict], inline: int) -> str:
    lines = [f"HEADLINE: {story.get('headline')}", f"STANDFIRST: {story.get('dek') or ''}",
             f"CATEGORY: {categories.name(story.get('category'))}",
             f"READERS IN: {COUNTRY_NAMES_.get(story.get('country') or '', story.get('country') or 'any country')}", "",
             f"Pick a main image and up to {inline} other photos for inside the story.", "", "CANDIDATES:"]
    for i, c in enumerate(candidates, 1):
        orient = "landscape" if (c.get("width") or 0) >= (c.get("height") or 0) else "portrait"
        lines.append(f"[{i}] {c.get('alt') or 'no description'} ({c['provider']}, {orient})")
    return "\n".join(lines)


def auto_images(db: Database, story: dict, client=None) -> dict:
    """For auto-published stories: stock photos chosen by AI, an AI illustration only when none fits.

    Never raises: a story publishes without images rather than not at all."""
    from pipeline.llm import get_client, model_name

    result: dict = {"hero": None, "inline": 0, "generated": False, "error": None}
    c = cfg()
    auto = c.get("auto") or {}
    if not auto.get("enabled"):
        return result
    try:
        if any(i["role"] == "hero" and i["status"] == "ready" for i in list_images(db, story["id"])):
            return result
        client = client or get_client()
        rev = story.get("revision") or {}
        info = {"headline": rev.get("headline"), "dek": rev.get("dek"), "category": story.get("category"),
                "country": story.get("country")}
        ev = db.fetchone(
            """select e.image_query, e.image_prompt from pipeline.evaluations e
               where e.cluster_id = ? order by e.id desc limit 1""", (story.get("cluster_id"),)) if story.get("cluster_id") else None
        query = (ev or {}).get("image_query") or rev.get("headline") or ""
        prompt = (ev or {}).get("image_prompt") or f"An editorial scene that illustrates: {rev.get('headline')}"
        candidates = []
        st = status()
        if st["unsplash"] or st["pexels"] or st["pixabay"]:
            found = search_stock(query, "all", per_page=int(auto.get("stock_results", 12)), country=story.get("country"))
            candidates = [x for x in found["results"] if (x.get("width") or 0) >= 1200
                          and (not x.get("copy") or st["storage"])]   # Pixabay needs our storage
        inline_n = int(auto.get("inline_images", 1))
        if candidates:
            out = client.generate_json(model=model_name("desk", client), system=CHOOSE_SYSTEM,
                                       prompt=_choose_prompt(info, candidates, inline_n), schema=CHOOSE_SCHEMA,
                                       temperature=0.2, task="choose_image")
            pick = int(out.get("choice") or 0)
            if 1 <= pick <= len(candidates):
                hero = candidates[pick - 1]
                result["hero"] = add_stock(db, None, story["id"], hero["provider"], hero["provider_id"], role="hero",
                                           alt=out.get("alt") or hero.get("alt") or "", photo=hero)
                sections = [k for k in categories.SECTION_KEYS if k != "what_happened"]
                for n, j in enumerate([j for j in (out.get("inline") or []) if isinstance(j, int) and j != pick][:inline_n]):
                    if 1 <= j <= len(candidates):
                        x = candidates[j - 1]
                        add_stock(db, None, story["id"], x["provider"], x["provider_id"], role="inline",
                                  position=sections[n % len(sections)] if sections else None, photo=x)
                        result["inline"] += 1
                return result
        if auto.get("generate_if_no_stock", True) and st["generate"]:
            ids = queue_generation(db, None, story["id"], prompt, style="illustration", role="hero")
            run_generation(db, ids, client)
            img = get_image(db, ids[0])
            if img and img["status"] == "ready":
                result["hero"], result["generated"] = ids[0], True
            else:
                result["error"] = (img or {}).get("error")
    except Exception as exc:   # noqa: BLE001
        log.warning("auto images for story %s failed: %s", story.get("id"), exc)
        db.rollback()
        result["error"] = str(exc)[:300]
    return result
