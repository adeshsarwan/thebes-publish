"""Write from a link: an editor pastes another publication's story and the AI writes our own."""
from __future__ import annotations

import unittest
import urllib.parse
from unittest import mock

from test_pipeline import Base
import test_newsroom_features as tnf

ARTICLE_URL = "https://news.example.com/2026/10/energy-price-cap-rises"
ARTICLE_TEXT = (
    "The energy regulator said on Tuesday that the price cap will rise by 4.2% from January, adding about "
    "£71 a year to a typical dual-fuel bill. Analysts at a consultancy expect a small fall in the spring. "
    "Campaigners called for more help for households on prepayment meters, who pay more on average. "
) * 3


class FromLinkTest(Base):
    def _read_page(self):
        """Pretend the article page and its SEO tags can be read."""
        from newsroom import sourcepages
        from pipeline import seo
        meta = {"title": "Energy price cap to rise 4.2% in January", "description": "The cap rises from January.",
                "keywords": ["energy price cap", "energy bills"], "tags": [], "section": [], "published": "2026-10-27T09:00:00Z"}
        return (mock.patch.object(sourcepages, "_fetch", return_value=("Energy price cap to rise | News", ARTICLE_TEXT)),
                mock.patch.object(seo, "fetch_page_seo", return_value=meta))

    def test_link_to_our_own_story(self):
        from newsroom import pickup, web, workflow as wf
        from newsroom.store import get_story
        from pipeline.llm import MockClient
        db = self.prepared("from_link")
        maya, daniel = self.user(db, "maya"), self.user(db, "daniel")

        # editors see the bar on the Stories screen; copy editors don't
        self.assertIn('action="/from-link"', web.queue_page(db, maya, {})[1])
        self.assertNotIn('action="/from-link"', web.queue_page(db, daniel, {})[1])
        self.assertIn("Write from a link", web.link_page(db, maya)[1])

        with self.assertRaisesRegex(pickup.PickupError, "Only editors"):
            pickup.request_from_link(db, daniel, ARTICLE_URL, country="UK", category="finance")
        with self.assertRaisesRegex(pickup.PickupError, "full web address"):
            pickup.request_from_link(db, maya, "news.example.com/story", country="UK", category="finance")
        with self.assertRaisesRegex(pickup.PickupError, "edition"):
            pickup.request_from_link(db, maya, ARTICLE_URL, country="FR", category="finance")
        with self.assertRaisesRegex(pickup.PickupError, "category"):
            pickup.request_from_link(db, maya, ARTICLE_URL, country="UK", category="cooking")

        rid = pickup.request_from_link(db, maya, ARTICLE_URL, country="UK", category="finance",
                                       notes="Explain what it means for prepayment customers")
        with self.assertRaisesRegex(pickup.PickupError, "already being researched"):
            pickup.request_from_link(db, maya, ARTICLE_URL + "?utm_source=x", country="UK", category="finance")
        self.assertIn("Researching and writing", web.link_page(db, maya)[1])

        p1, p2 = self._read_page()
        with p1, p2:
            story_id = pickup.run_request(rid, MockClient(), resolve_links=False, db=db)
        s = get_story(db, story_id)
        self.assertEqual((s["status"], s["origin"], s["picked_by"], s["cluster_id"], s["country"], s["category"]),
                         ("ai_draft", "from_link", maya["id"], None, "UK", "finance"))
        research = db.unj(s["research"])
        self.assertEqual(research["source_url"], ARTICLE_URL)
        self.assertTrue(research["source_read"])
        self.assertIn("Research brief", research["text"])
        self.assertIn("readers in United Kingdom", s["angle"])          # no angle given: a default one
        urls = [x["url"] for x in s["revision"]["sources"]]
        self.assertEqual(urls[0], ARTICLE_URL)
        self.assertIn("https://example-research.org/allocation", urls)
        self.assertIn("energy price cap", s["revision"]["seo"]["source_keywords"])
        self.assertIn("4.2%", s["revision"]["sections"]["what_happened"])  # the writer saw the page text
        req = db.fetchone("select * from newsroom.pick_requests where id = ?", (rid,))
        self.assertEqual((req["status"], req["story_id"]), ("done", story_id))

        # never auto-published; same workflow as any draft
        db.execute("update newsroom.stories set impact_score = 95 where id = ?", (story_id,))
        db.commit()
        wf.autopublish(db)
        self.assertEqual(get_story(db, story_id)["status"], "ai_draft")
        self.assertIn("assign", wf.allowed_actions(s, maya))

        # the page and the queue show where it came from
        page = web.story_page(db, story_id, maya)[1]
        self.assertIn("Written from a link", page)
        self.assertIn("Pasted a link to write from", page)
        self.assertIn("From a link", web.queue_page(db, maya, {})[1])
        self.assertIn(f'href="/story/{story_id}"', web.link_page(db, maya)[1])

        # the same link can't be written twice, unless that story was killed
        with self.assertRaisesRegex(pickup.PickupError, "already wrote a story"):
            pickup.request_from_link(db, maya, ARTICLE_URL, country="UK", category="finance")
        db.execute("update newsroom.stories set status = 'killed' where id = ?", (story_id,))
        db.commit()
        self.assertTrue(pickup.request_from_link(db, maya, ARTICLE_URL, country="US", category="finance"))

        # edits are re-checked against the article's text, so copied wording is flagged
        from newsroom.store import revision_content
        content = revision_content(s["revision"])
        content["sections"] = dict(content["sections"], what_happened=ARTICLE_TEXT[:400])
        rep = wf.check_content(db, get_story(db, story_id), content)
        self.assertIn("verbatim_run", {i["code"] for i in rep["issues"]})

    def test_unreadable_page_needs_research_and_failures_can_be_retried(self):
        from newsroom import pickup, web
        from pipeline.llm import LLMError, MockClient
        db = self.prepared("from_link_fail")
        maya = self.user(db, "maya")

        rid = pickup.request_from_link(db, maya, ARTICLE_URL, country="UK", category="finance", research=False)
        with self.assertRaisesRegex(pickup.PickupError, "Couldn't read that page"):
            pickup.run_request(rid, MockClient(), fetch_text=False, resolve_links=False, db=db)
        self.assertIn("Try again", web.link_page(db, maya)[1])
        self.assertIn(ARTICLE_URL, web.link_page(db, maya, retry=rid)[1])

        class Broken(MockClient):
            def research(self, **kw):
                raise LLMError("search unavailable")

        rid2 = pickup.request_from_link(db, maya, ARTICLE_URL, country="UK", category="finance")
        with self.assertRaises(LLMError):
            pickup.run_request(rid2, Broken(), fetch_text=False, resolve_links=False, db=db)

        # unreadable page + research: written from the research, and the story says so
        rid3 = pickup.request_from_link(db, maya, ARTICLE_URL, country="UK", category="finance", angle="")
        story_id = pickup.run_request(rid3, MockClient(), fetch_text=False, resolve_links=False, db=db)
        self.assertIn("couldn't be read", web.story_page(db, story_id, maya)[1])
        # the scoring desk's pick-up list ignores link requests
        self.assertNotIn(None, pickup.latest_requests(db))


class FromLinkHttpTest(Base):
    setUp, client, post = tnf.HttpTest.setUp, tnf.HttpTest.client, tnf.HttpTest.post

    def test_post_from_the_stories_screen(self):
        from newsroom import pickup
        op, _ = self.client()
        self.post(op, "/login", {"email": "boss@example.com", "password": "Boss-password-2026"})
        html = op.open(self.base + "/").read().decode()
        self.assertIn("Write from a link", html)
        with mock.patch.object(pickup, "start_background") as bg:
            r = self.post(op, "/from-link", {"url": ARTICLE_URL, "country": "UK", "category": "finance", "research": "1"})
            self.assertTrue(bg.called)
        self.assertIn("/from-link", r.url)
        self.assertIn("Started", urllib.parse.unquote(r.url))
        self.assertIn("news.example.com", r.read().decode())
        with mock.patch.object(pickup, "start_background") as bg:
            r = self.post(op, "/from-link", {"url": "not a link", "country": "UK", "category": "finance"})
            self.assertFalse(bg.called)
        self.assertIn("full web address", r.read().decode())


if __name__ == "__main__":
    unittest.main()
