"""Story images: stock search (Unsplash, Pexels), AI images, uploads, image blocks, publishing and the API.

Network calls are faked; files go to a temporary folder instead of Cloudflare R2.
"""
from __future__ import annotations

import base64
import http.cookiejar
import io
import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

from test_pipeline import Base


def png(w=800, h=450, color=(200, 80, 40)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


class FakeResp:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status
        self.headers = {"Retry-After": "0"} if status == 429 else {}
        self.ok = status < 400

    def json(self):
        return self.payload

    def raise_for_status(self):
        import requests
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


UNSPLASH = {"results": [{
    "id": "abc123", "width": 4000, "height": 2667, "alt_description": "a family at a kitchen table with bills",
    "urls": {"raw": "https://images.unsplash.com/photo-1?ixid=x", "small": "https://images.unsplash.com/photo-1?w=400"},
    "links": {"html": "https://unsplash.com/photos/abc123", "download_location": "https://api.unsplash.com/photos/abc123/download"},
    "user": {"name": "Jane Doe", "links": {"html": "https://unsplash.com/@jane"}}}]}
PEXELS = {"photos": [{
    "id": 98765, "width": 3000, "height": 2000, "url": "https://www.pexels.com/photo/98765/", "alt": "calculator and receipts",
    "photographer": "Sam Lee", "photographer_url": "https://www.pexels.com/@sam",
    "src": {"original": "https://images.pexels.com/photos/98765/x.jpeg", "medium": "https://images.pexels.com/photos/98765/x.jpeg?h=350"}}]}


PIXABAY = {"total": 1, "hits": [{
    "id": 4455, "pageURL": "https://pixabay.com/photos/signing-contract-4455/", "tags": "contract, signing, business",
    "webformatURL": "https://pixabay.com/get/abc_640.jpg", "largeImageURL": "https://pixabay.com/get/abc_1280.jpg",
    "previewURL": "https://cdn.pixabay.com/photo/2020/01/02/10/11/signing-4455_150.jpg",
    "imageWidth": 5000, "imageHeight": 3000, "user": "Pat", "user_id": 77}]}


def fake_get(calls):
    def get(url, params=None, headers=None, timeout=None, stream=False):
        calls.append((url, params, headers))
        if url == "https://pixabay.com/api/":
            return FakeResp(PIXABAY)
        if url.startswith("https://cdn.pixabay.com/photo/") and url.endswith("_1280.jpg"):
            return FakeResp({}, 429)                                    # rate-limited: falls back to largeImageURL
        if url.startswith("https://pixabay.com/get/") or url.startswith("https://cdn.pixabay.com/"):
            r = FakeResp({}); r.content = png(1280, 768); r.headers = {"Content-Type": "image/png"}

            class Raw:
                def read(self, n, decode_content=False):
                    return r.content[:n]
            r.raw = Raw()
            return r
        if "api.unsplash.com/search" in url:
            return FakeResp(UNSPLASH)
        if "api.pexels.com/v1/search" in url:
            return FakeResp(PEXELS)
        if url.endswith("/download"):
            return FakeResp({"url": "ok"})
        if "api.unsplash.com/photos/abc123" in url:
            return FakeResp(UNSPLASH["results"][0])
        return FakeResp({}, 404)
    return get


class ImagesBase(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        os.environ["MEDIA_LOCAL_DIR"] = str(cls.tmp / "media")
        # the server's own .env must not leak in (the deploy runs these tests on the server)
        for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "MEDIA_BASE_URL", "IMAGE_MODEL",
                  "PIXABAY_API_KEY"):
            os.environ[k] = ""
        os.environ["UNSPLASH_ACCESS_KEY"] = "test-unsplash"
        os.environ["PEXELS_API_KEY"] = "test-pexels"

    def story(self, db):
        return db.fetchone("select id from newsroom.stories order by id limit 1")["id"]


class ImageUnitTest(ImagesBase):
    def test_find_image_in_both_response_shapes(self):
        from pipeline.llm import find_image
        b64 = base64.b64encode(b"jpegbytes").decode()
        interactions = {"id": "x", "steps": [{"type": "model_output", "content": [
            {"type": "text", "text": "here"}, {"type": "image", "data": b64, "mime_type": "image/jpeg"}]}]}
        self.assertEqual(find_image(interactions), (b"jpegbytes", "image/jpeg"))
        gc = {"candidates": [{"content": {"parts": [{"text": "x"}, {"inlineData": {"mimeType": "image/png", "data": b64}}]}}]}
        self.assertEqual(find_image(gc), (b"jpegbytes", "image/png"))
        self.assertIsNone(find_image({"candidates": [{"finishReason": "SAFETY"}]}))

    def test_stock_search_normalises_and_credits(self):
        from newsroom import media
        calls = []
        with mock.patch("newsroom.media.requests.get", side_effect=fake_get(calls)):
            out = media.search_stock("family budget", "all", per_page=6)
        self.assertEqual(out["errors"], {})
        u, p = out["results"]
        self.assertEqual((u["provider"], p["provider"]), ("unsplash", "pexels"))
        self.assertIn("utm_source=story_hub", u["photographer_url"])
        self.assertIn("utm_source=story_hub", u["credit_url"])
        self.assertEqual(u["credit"], "Photo by Jane Doe on Unsplash")
        self.assertTrue(u["url"].startswith("https://images.unsplash.com/") and "w=1600" in u["url"])
        self.assertEqual(p["credit"], "Photo by Sam Lee on Pexels")
        self.assertIn("w=1600", p["url"])
        self.assertEqual(calls[0][2]["Authorization"], "Client-ID test-unsplash")
        self.assertEqual(calls[1][2]["Authorization"], "test-pexels")
        self.assertEqual(calls[0][1]["orientation"], "landscape")

        # a missing key or a refused key is reported per provider, the other still answers
        with mock.patch.dict(os.environ, {"PEXELS_API_KEY": ""}), \
                mock.patch("newsroom.media.requests.get", side_effect=fake_get([])):
            out = media.search_stock("family budget")
        self.assertEqual((out["errors"], [r["provider"] for r in out["results"]]), ({}, ["unsplash"]))  # Pexels skipped
        with mock.patch.dict(os.environ, {"PEXELS_API_KEY": "", "UNSPLASH_ACCESS_KEY": ""}):
            with self.assertRaisesRegex(media.MediaError, "UNSPLASH_ACCESS_KEY"):
                media.search_stock("family budget")
        with mock.patch("newsroom.media.requests.get", return_value=FakeResp({}, 403)):
            out = media.search_stock("x", "unsplash")
        self.assertIn("limit", out["errors"]["unsplash"])
        with self.assertRaises(media.MediaError):
            media.search_stock("   ")

    def test_r2_errors_and_pasted_credits(self):
        from newsroom import media
        st = object.__new__(media.R2Storage); st.bucket = "storyhub-media"

        class ClientError(Exception):
            def __init__(self, code):
                super().__init__(code); self.response = {"Error": {"Code": code}}
        self.assertIn("doesn't have write access to bucket storyhub-media", str(st._error(ClientError("AccessDenied"))))
        self.assertIn("Secret Access Key", str(st._error(ClientError("SignatureDoesNotMatch"))))
        snippet = ('Photo by <a href="https://unsplash.com/@skillscouter?utm_source=unsplash&utm_medium=referral">Lewis Keegan</a>'
                   ' on <a href="https://unsplash.com/photos/x">Unsplash</a>')
        self.assertEqual(media.clean_credit(snippet), ("Photo by Lewis Keegan on Unsplash",
                                                       "https://unsplash.com/@skillscouter?utm_source=unsplash&utm_medium=referral"))
        self.assertEqual(media.clean_credit("Photo: Jane / Agency"), ("Photo: Jane / Agency", None))

    def test_pixabay_search_copy_and_credit(self):
        from newsroom import blocks as blk, media
        calls = []
        with mock.patch.dict(os.environ, {"PIXABAY_API_KEY": "test-pixabay"}), \
                mock.patch("newsroom.media.requests.get", side_effect=fake_get(calls)):
            out = media.search_stock("contract signing", "all", per_page=6)
            provs = [r["provider"] for r in out["results"]]
            self.assertEqual(provs, ["unsplash", "pixabay", "pexels"])
            px = out["results"][1]
            self.assertEqual((px["credit"], px["alt"]), ("Image by Pat from Pixabay", "Contract, signing, business"))
            self.assertEqual(px["photographer_url"], "https://pixabay.com/users/Pat-77/")
            px_call = next(c for c in calls if c[0] == "https://pixabay.com/api/")
            self.assertEqual((px_call[1]["safesearch"], px_call[1]["image_type"], px_call[1]["orientation"]), ("true", "photo", "horizontal"))
            n = len(calls)
            media.search_stock("contract signing", "all", per_page=6)              # Pixabay part cached for 24 hours
            self.assertFalse(any(c[0] == "https://pixabay.com/api/" for c in calls[n:]))
            db = self.prepared("img_pixabay")
            sid = self.story(db)
            iid = media.add_stock(db, self.user(db, "maya"), sid, "pixabay", "4455", role="hero")
        img = media.get_image(db, iid)
        self.assertTrue(img["url"].startswith("/media/stories/"), img["url"])        # our copy, not a Pixabay hotlink
        self.assertEqual((img["provider"], img["width"]), ("pixabay", 1280))
        self.assertTrue(px["thumb"].startswith("/stock-preview?u=https%3A%2F%2Fcdn.pixabay.com%2Fphoto%2F"), px["thumb"])
        self.assertIn("signing-4455_640.jpg", px["thumb"])
        self.assertEqual(px["download_urls"][0], "https://cdn.pixabay.com/photo/2020/01/02/10/11/signing-4455_1280.jpg")
        with mock.patch("newsroom.media.requests.get", side_effect=fake_get([])):
            data, ctype = media.fetch_preview("https://pixabay.com/get/abc_640.jpg")
        self.assertEqual(ctype, "image/png")
        with self.assertRaises(media.MediaError):
            media.fetch_preview("https://evil.example/x.jpg")
        html = blk.figure_html(img)
        self.assertIn('Image by <a target="_blank" rel="noopener" href="https://pixabay.com/users/Pat-77/">Pat</a> from', html)

    def test_country_setting_in_searches_and_prompts(self):
        from newsroom import media
        self.assertEqual(media.with_country("student loan", "US"), "student loan usa")
        self.assertEqual(media.with_country("London terraced houses", "UK"), "London terraced houses")
        self.assertEqual(media.with_country("family in the united states", "US"), "family in the united states")
        self.assertEqual(media.with_country("bus stop", None), "bus stop")
        calls = []
        with mock.patch("newsroom.media.requests.get", side_effect=fake_get(calls)):
            out = media.search_stock("rent bills", "unsplash", country="AU")
        self.assertEqual((out["query"], calls[0][1]["query"]), ("rent bills australia", "rent bills australia"))
        text = media.full_prompt("A family reading bills", "illustration", "CA")
        self.assertIn("Setting: Canada", text)
        self.assertIn("reflecting its diversity", text)

    def test_image_blocks_only_allow_our_hosts(self):
        from newsroom import blocks as blk
        ok = {"type": "image", "data": {"url": "https://images.unsplash.com/photo-1?w=1600", "alt": "<b>Bills</b>",
                                        "caption": "Kitchen <script>x</script><i>table</i>", "credit": "Photo by Jane",
                                        "credit_url": "javascript:alert(1)", "width": 1600, "height": 900,
                                        "sizes": {"640": "https://images.unsplash.com/photo-1?w=640", "999": "https://evil.example/x.jpg"}}}
        bad = {"type": "image", "data": {"url": "https://evil.example/x.jpg"}}
        local = {"type": "image", "data": {"url": "/media/stories/2026/10/1-ai-x-1600.webp"}}
        out = blk.clean_blocks([ok, bad, local])
        self.assertEqual(len(out), 2)
        d = out[0]["data"]
        self.assertEqual(d["alt"], "Bills")
        self.assertNotIn("script", d["caption"])
        self.assertNotIn("credit_url", d)
        self.assertEqual(list(d["sizes"]), ["640"])
        html = blk.render_blocks(out)
        self.assertIn('<figure class="story-image">', html)
        credited = blk.figure_html({"url": "https://images.unsplash.com/p?w=1600", "provider": "unsplash", "credit": "Photo by Jane Doe on Unsplash",
                                    "photographer": "Jane Doe", "photographer_url": "https://unsplash.com/@jane?utm_source=story_hub&utm_medium=referral"})
        self.assertIn('Photo by <a target="_blank" rel="noopener" href="https://unsplash.com/@jane?utm_source=story_hub&amp;utm_medium=referral">Jane Doe</a> on '
                      '<a target="_blank" rel="noopener" href="https://unsplash.com/?utm_source=story_hub&amp;utm_medium=referral">Unsplash</a>', credited)
        self.assertIn('srcset="https://images.unsplash.com/photo-1?w=640 640w"', html)
        self.assertIn('loading="lazy"', html)
        self.assertIn("![Bills](https://images.unsplash.com/photo-1?w=1600)",
                      blk.blocks_markdown({"headline": "H", "blocks": out}))

        with mock.patch.dict(os.environ, {"MEDIA_BASE_URL": "https://media.thebesads.com"}):
            self.assertTrue(blk.clean_blocks([{"type": "image", "data": {"url": "https://media.thebesads.com/stories/a.webp"}}]))

    def test_inline_figures_follow_their_section(self):
        from newsroom import blocks as blk
        html = "<p>intro</p><h2>What happened</h2><p>a</p><h2>Who&#x27;s affected</h2><p>b</p><h2>The numbers</h2><p>c</p>"
        img = {"role": "inline", "url": "https://images.pexels.com/x.jpeg", "alt": "x", "position": "who_is_affected"}
        out = blk.insert_figures(html, [img, {"role": "hero", "url": "https://images.pexels.com/h.jpeg"}],
                                 {"who_is_affected": "Who's affected"})
        self.assertLess(out.index("<figure"), out.index("<h2>The numbers"))
        self.assertGreater(out.index("<figure"), out.index("<p>b</p>"))
        self.assertEqual(out.count("<figure"), 1)                                  # the hero goes above the body


class ImageFlowTest(ImagesBase):
    def test_stock_generate_upload_publish_and_api(self):
        from newsroom import media, workflow as wf
        from pipeline.llm import MockClient
        from storyapi import stories as api_stories
        db = self.prepared("img_flow")
        maya, sara, daniel = self.user(db, "maya"), self.user(db, "sara"), self.user(db, "daniel")
        sid = self.story(db)

        calls = []
        with mock.patch("newsroom.media.requests.get", side_effect=fake_get(calls)):
            media.search_stock("family budget", "unsplash")
            hero = media.add_stock(db, daniel, sid, "unsplash", "abc123", role="hero")
            for t in list(threading.enumerate()):
                if t is not threading.current_thread() and t.daemon and t.name.startswith("Thread"):
                    t.join(timeout=2)
        self.assertTrue(any(u.endswith("/download") for u, _, _ in calls), "Unsplash download must be reported")
        img = media.get_image(db, hero)
        self.assertEqual((img["kind"], img["status"], img["role"]), ("stock", "ready", "hero"))
        self.assertEqual(img["alt"], "a family at a kitchen table with bills")

        # AI image with the mock model: saved as WebP in several widths
        ids = media.queue_generation(db, sara, sid, "A household reviewing bills at a kitchen table", style="illustration",
                                     aspect="16:9", count=2)
        self.assertEqual(len(ids), 2)
        out = media.run_generation(db, ids, MockClient())
        self.assertEqual(out, {"ready": 2, "failed": 0})
        gen = media.get_image(db, ids[0])
        self.assertEqual((gen["status"], gen["width"], gen["height"]), ("ready", 640, 360))
        self.assertEqual(sorted(gen["sizes"], key=int), ["320", "640"])          # never upscaled
        self.assertTrue(gen["url"].startswith("/media/stories/"))
        key = gen["url"][len("/media/"):]
        self.assertTrue((self.tmp / "media" / key).is_file())
        with self.assertRaisesRegex(media.MediaError, "few more words"):
            media.queue_generation(db, sara, sid, "cat")

        # a failing model marks the image failed, it doesn't raise
        class Broken(MockClient):
            def generate_image(self, **kw):
                from pipeline.llm import LLMError
                raise LLMError("no image in the response (it may have been blocked)")
        bad = media.queue_generation(db, sara, sid, "Another scene of a quiet street at dusk")
        self.assertEqual(media.run_generation(db, bad, Broken())["failed"], 1)
        self.assertIn("blocked", media.get_image(db, bad[0])["error"])

        # upload, in-story placement, and garbage files
        up = media.add_upload(db, maya, sid, png(2400, 1200), "chart.png", role="inline", alt="Rate chart", credit="Story Hub")
        media.update_image(db, maya, up, position="who_is_affected", caption="Rates over a year")
        self.assertEqual(sorted(media.get_image(db, up)["sizes"], key=int), ["320", "640", "1024", "1600"])
        with self.assertRaisesRegex(media.MediaError, "isn't an image"):
            media.add_upload(db, maya, sid, b"not an image at all", "x.png")
        with self.assertRaisesRegex(media.MediaError, "unknown section"):
            media.update_image(db, maya, up, position="nope")

        # making another image the main image demotes the old one
        media.update_image(db, maya, ids[0], role="hero")
        self.assertEqual(media.get_image(db, hero)["role"], "gallery")
        media.update_image(db, maya, hero, role="hero")
        self.assertEqual(media.get_image(db, ids[0])["role"], "gallery")
        media.remove_image(db, maya, ids[1])
        self.assertNotIn(ids[1], [i["id"] for i in media.list_images(db, sid)])

        # people outside the story's category can't touch its images
        outsider = dict(maya, role="editor", categories=["sports"])
        with self.assertRaisesRegex(media.MediaError, "can't change images"):
            media.add_upload(db, outsider, sid, png(), "x.png")

        # publishing freezes the main and in-story images into the site copy
        wf.perform(db, sid, "approve", maya, override=True, note="test")
        wf.perform(db, sid, "publish", sara)
        row = db.fetchone("select * from site.published_stories where story_id = ?", (sid,))
        site_imgs = db.unj(row["images"])
        self.assertEqual([i["role"] for i in site_imgs], ["hero", "inline"])
        self.assertEqual(site_imgs[0]["credit"], "Photo by Jane Doe on Unsplash")

        api = api_stories.story(dict(row, display_name="Maya", author_slug="maya", job_title=None, bio=None,
                                     credentials=None, avatar_url=None))
        self.assertEqual(api["hero_image"]["photographer"], "Jane Doe")
        self.assertEqual(len(api["images"]), 2)
        self.assertEqual(api["images"][1]["section"], "who_is_affected")
        self.assertIn("Rates over a year", api["body_html"])
        self.assertIn("<figure", api["body_html"])
        blocky = dict(row, display_name="Maya", author_slug="maya", job_title=None, bio=None, credentials=None, avatar_url=None,
                      blocks=db.j([{"type": "paragraph", "data": {"text": "x"}},
                                   {"type": "image", "data": {"url": "https://images.unsplash.com/z?w=1600", "alt": "z",
                                                              "credit": "Photo by Z on Unsplash", "provider": "unsplash", "photographer": "Z"}}]))
        api2 = api_stories.story(blocky)
        body_img = api2["images"][-1]
        self.assertEqual((body_img["role"], body_img["block"], body_img["photographer"]), ("inline", 1, "Z"))
        self.assertIn("images.unsplash.com/z", api2["body_html"])

        # changing images after publishing doesn't touch the site until someone sends them
        media.update_image(db, maya, up, role="gallery")
        self.assertEqual(len(db.unj(db.fetchone("select images from site.published_stories where story_id = ?", (sid,))["images"])), 2)
        with self.assertRaises(wf.WorkflowError):
            wf.sync_site_images(db, sid, daniel)                              # copy editors can't push to the live site
        self.assertEqual(wf.sync_site_images(db, sid, sara), 1)
        ev = db.fetchone("select * from newsroom.story_events where story_id = ? and action = 'images_live'", (sid,))
        self.assertEqual(ev["actor_id"], sara["id"])

    def test_pickup_prompts_become_drafts(self):
        from newsroom import media, pickup, web
        from pipeline.llm import MockClient
        db = self.prepared("img_pick")
        maya = self.user(db, "maya")
        cid = db.fetchone("""select c.id from pipeline.story_clusters c join pipeline.evaluations e on e.cluster_id = c.id
                             where not exists (select 1 from newsroom.stories s where s.cluster_id = c.id)
                             order by c.id limit 1""")["id"]
        page = web.pickup_page(db, maya, cid)[1]
        self.assertIn("AI image prompts (optional)", page)
        self.assertNotIn('name="img_enable" value="1" checked', page)          # off unless someone wants AI images
        self.assertEqual(web._prompts_from_form({"img_prompt_1": "A scene of something"}), [])
        self.assertIn("A household reviewing bills at a kitchen table", page)       # the desk's suggestion
        rid = pickup.request_pickup(db, maya, cid, angle="What the change means for renters this winter",
                                    image_prompts=[{"prompt": "Renters checking a heating bill", "style": "photo", "aspect": "4:3"},
                                                   {"prompt": "  "}, {"prompt": "A thermostat on a cold morning", "aspect": "9:99"}])
        sid = pickup.run_request(rid, MockClient(), fetch_text=False, resolve_links=False, db=db)
        drafts = media.list_images(db, sid)
        self.assertEqual([(d["status"], d["role"], d["style"], d["aspect"]) for d in drafts],
                         [("draft", "hero", "photo", "4:3"), ("draft", "gallery", "illustration", "16:9")])
        self.assertEqual(media.run_generation(db, [drafts[0]["id"]], MockClient())["ready"], 1)

    def test_autopublish_picks_stock_then_falls_back_to_illustration(self):
        from newsroom import media, workflow as wf
        from pipeline.llm import MockClient
        db = self.prepared("img_auto")
        with mock.patch("newsroom.media.requests.get", side_effect=fake_get([])):
            out = wf.autopublish(db, client=MockClient())
        self.assertGreater(out["published"], 0, out)
        self.assertEqual(out["image_errors"], 0, out)
        rows = db.fetchall("select story_id, images from site.published_stories")
        first = db.unj(rows[0]["images"])
        self.assertEqual(first[0]["role"], "hero")
        self.assertEqual(first[0]["provider"], "unsplash")
        self.assertEqual(first[1]["role"], "inline")                                # the AI's second pick

        # no stock keys: an illustration is generated instead
        db2 = self.prepared("img_auto_gen")
        with mock.patch.dict(os.environ, {"UNSPLASH_ACCESS_KEY": "", "PEXELS_API_KEY": ""}):
            out = wf.autopublish(db2, client=MockClient())
        self.assertGreater(out["published"], 0)
        imgs = db.unj(db2.fetchone("select images from site.published_stories order by story_id limit 1")["images"])
        self.assertEqual((imgs[0]["kind"], imgs[0]["role"]), ("generated", "hero"))

        # images failing never stop publishing
        db3 = self.prepared("img_auto_fail")
        with mock.patch.object(media, "search_stock", side_effect=RuntimeError("network down")):
            out = wf.autopublish(db3, client=MockClient())
        self.assertGreater(out["published"], 0)
        self.assertEqual(out["image_errors"], out["published"])


class ImageHttpTest(ImagesBase):
    def setUp(self):
        from http.server import ThreadingHTTPServer
        from newsroom import auth, web
        from newsroom.users import seed_placeholders
        from pipeline import cluster, desk, feeds, writer
        from pipeline.db import connect
        from pipeline.llm import MockClient
        db = connect("newsroom")
        seed_placeholders(db)
        if not db.fetchone("select id from newsroom.users where email = 'boss@example.com'"):
            auth.create_user(db, None, email="boss@example.com", display_name="Boss", role="admin", password="Boss-password-2026")
        if not db.fetchone("select id from newsroom.stories limit 1"):
            feeds.ingest(db); cluster.cluster(db); desk.evaluate(db, MockClient())
            writer.write(db, MockClient(), fetch_text=False)
        self.sid = self.story(db)
        db.close()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self._post("/login", {"email": "boss@example.com", "password": "Boss-password-2026"})

    def _post(self, path, data=None, raw=None, headers=None):
        body = raw if raw is not None else urllib.parse.urlencode(data or {}).encode()
        req = urllib.request.Request(self.base + path, data=body, method="POST", headers=headers or {})
        try:
            r = self.op.open(req)
            return r.status, r.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def _json(self, path):
        return json.loads(self.op.open(self.base + path).read())

    def test_panel_endpoints(self):
        from newsroom import media
        from pipeline.llm import MockClient
        page = self.op.open(self.base + f"/story/{self.sid}").read().decode()
        self.assertIn('id="img-panel"', page)
        self.assertIn("/static/images.js", page)
        self.assertIn("image", self.op.open(self.base + "/static/images.js").read().decode())
        edit = self.op.open(self.base + f"/story/{self.sid}/edit").read().decode()
        self.assertIn('class="img-panel img-editor"', edit)
        self.assertIn('id="ed-add-img"', edit)
        self.assertIn("<kbd>/image</kbd>", edit)

        st = self._json(f"/story/{self.sid}/images.json")
        self.assertTrue(st["can_manage"])
        self.assertEqual(st["status"]["storage"], "this computer (data/media)")

        with mock.patch("newsroom.media.requests.get", side_effect=fake_get([])):
            res = self._json(f"/story/{self.sid}/images/search?q=family+budget")
            self.assertEqual(len(res["results"]), 2)
            code, body = self._post(f"/story/{self.sid}/images/stock", {"provider": "pexels", "provider_id": "98765", "role": "hero"})
        self.assertEqual(code, 200, body)

        # generation runs in the background; run it inline here
        with mock.patch.object(media, "start_background", lambda ids: media.run_generation(
                __import__("pipeline.db", fromlist=["connect"]).connect("newsroom"), ids, MockClient())):
            code, body = self._post(f"/story/{self.sid}/images/generate", {"prompt": "A quiet street of terraced houses at dusk", "count": "1"})
        self.assertEqual(code, 200, body)
        code, body = self._post(f"/story/{self.sid}/images/generate", {"prompt": "x"})
        self.assertEqual(code, 400)
        self.assertIn("few more words", json.loads(body)["error"])

        code, body = self._post(f"/story/{self.sid}/images/upload?role=inline&alt=A%20chart", raw=png(),
                                headers={"Content-Type": "application/octet-stream", "X-Filename": "chart.png"})
        self.assertEqual(code, 200, body)
        up = json.loads(body)["id"]
        code, body = self._post(f"/story/{self.sid}/images/upload", raw=b"%PDF-1.4 not an image",
                                headers={"Content-Type": "application/octet-stream"})
        self.assertEqual(code, 400)

        st = self._json(f"/story/{self.sid}/images.json")
        kinds = sorted(i["kind"] for i in st["images"])
        self.assertEqual(kinds, ["generated", "stock", "upload"])
        gen = next(i for i in st["images"] if i["kind"] == "generated")
        self.assertEqual(gen["status"], "ready")
        r = self.op.open(self.base + gen["url"])
        self.assertEqual(r.headers["Content-Type"], "image/webp")
        with self.assertRaises(urllib.error.HTTPError):
            self.op.open(self.base + "/media/../../etc/passwd")

        code, body = self._post(f"/images/{up}/update", {"caption": "Rates over a year", "role": "hero"})
        self.assertEqual(json.loads(body)["image"]["role"], "hero")
        code, body = self._post(f"/images/{up}/remove")
        self.assertEqual(code, 200)
        # cross-site requests are refused like every other form post
        code, _ = self._post(f"/images/{gen['id']}/remove", headers={"Origin": "https://evil.example"})
        self.assertEqual(code, 403)

        # the story page shows the main image above the article body
        stock = next(i for i in st["images"] if i["kind"] == "stock")
        self._post(f"/images/{stock['id']}/update", {"role": "hero"})
        page = self.op.open(self.base + f"/story/{self.sid}").read().decode()
        self.assertIn('figure class="story-image hero"', page)


class AutosaveHttpTest(ImageHttpTest):
    def test_panel_endpoints(self):   # covered by ImageHttpTest
        pass

    def test_autosave_round_trip(self):
        state = {"headline": "Draft headline", "dek": "", "blocks": [{"type": "paragraph", "data": {"text": "Work in progress"}}], "side": {}}
        code, body = self._post(f"/story/{self.sid}/autosave", {"state": json.dumps(state), "base_version": "1"})
        self.assertEqual(code, 200, body)
        code, _ = self._post(f"/story/{self.sid}/autosave", {"state": "not json"})
        self.assertEqual(code, 400)
        edit = self.op.open(self.base + f"/story/{self.sid}/edit").read().decode()
        self.assertIn('"autosave": {"base_version": 1', edit)
        self.assertIn("Work in progress", edit)
        # a real save clears it
        form = {"headline": "Saved headline", "dek": "", "blocks": json.dumps(state["blocks"] * 3), "then": "save"}
        self._post(f"/story/{self.sid}/edit", form)
        edit = self.op.open(self.base + f"/story/{self.sid}/edit").read().decode()
        self.assertNotIn('"autosave":', edit)
        # cross-site autosave posts are refused
        code, _ = self._post(f"/story/{self.sid}/autosave", {"state": json.dumps(state)}, headers={"Origin": "https://evil.example"})
        self.assertEqual(code, 403)


class EditorApproveAnywayHttpTest(ImageHttpTest):
    def test_panel_endpoints(self):   # covered by ImageHttpTest
        pass

    def test_save_and_approve_with_reason(self):
        from unittest import mock
        from newsroom import sourcepages
        blocks = [{"type": "paragraph", "data": {"text": "The allowance is £3,000 and the threshold is £35,000. " * 30}}]
        form = {"headline": "Tax story", "dek": "", "blocks": json.dumps(blocks), "then": "approve",
                "sources_json": json.dumps([{"url": "https://example.com/a", "name": "Example", "title": "A"}])}
        with mock.patch.object(sourcepages, "_fetch", return_value=("", "")):
            r = self.op.open(urllib.request.Request(self.base + f"/story/{self.sid}/edit",
                                                    data=urllib.parse.urlencode(form).encode(), method="POST"))
            self.assertIn("/edit?err=", r.url)
            self.assertIn("give a reason", urllib.parse.unquote(r.url))
            page = r.read().decode()
            self.assertIn('name="override_reason"', page)
            self.assertIn("https://example.com/a", page)                          # the source was saved
            form["override_reason"] = "Figures checked on gov.uk"
            r = self.op.open(urllib.request.Request(self.base + f"/story/{self.sid}/edit",
                                                    data=urllib.parse.urlencode(form).encode(), method="POST"))
        self.assertIn("approved", urllib.parse.unquote(r.url).lower())
        form["sources_json"] = json.dumps([{"url": "ftp://nope"}])
        r = self.op.open(urllib.request.Request(self.base + f"/story/{self.sid}/edit",
                                                data=urllib.parse.urlencode(form).encode(), method="POST"))
        self.assertIn("Not a web address", urllib.parse.unquote(r.url))
