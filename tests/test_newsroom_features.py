"""Tests for auto-publish, sources, logins, the daily schedule, pulls and the desk over HTTP.

Runs on SQLite by default and on Postgres with TEST_PG_URL (see test_pipeline.py).
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from test_pipeline import Base


# =========================================================================
class AutoPublishTest(Base):
    def test_autopublish_then_editor_rolls_back_through_copy_editor(self):
        from newsroom import workflow as wf
        from newsroom.store import get_story, revision_content
        db = self.prepared("auto")
        maya, daniel, sara = self.user(db, "maya"), self.user(db, "daniel"), self.user(db, "sara")
        drafts = db.fetchone("select count(*) as n from newsroom.stories where status = 'ai_draft'")["n"]

        out = wf.autopublish(db)
        self.assertEqual(out["published"], drafts, out)
        live = db.fetchall("select * from site.published_stories")
        self.assertEqual(len(live), drafts)
        row = live[0]
        self.assertFalse(row["human_reviewed"])
        self.assertIn("published automatically", row["disclosure"])
        self.assertIn("Maya Fernandes, Senior Personal Finance Editor is the editor responsible", row["disclosure"])
        self.assertEqual(row["author_id"], maya["id"])
        sid = row["story_id"]
        s = get_story(db, sid)
        self.assertEqual((s["status"], s["auto_published"], s["editor_id"]), ("published", True, maya["id"]))
        ev = db.fetchone("select * from newsroom.story_events where story_id = ? and action = 'auto_publish'", (sid,))
        self.assertIsNone(ev["actor_id"])
        self.assertEqual(wf.autopublish(db)["published"], 0)                     # never twice

        # editor rolls it back and hands it to a copy editor
        wf.perform(db, sid, "unpublish", maya, note="Needs a human pass")
        self.assertEqual(db.fetchone("select status from site.published_stories where story_id = ?", (sid,))["status"], "retracted")
        s = wf.perform(db, sid, "assign", maya, assignee_id=daniel["id"])
        self.assertEqual((s["status"], s["assigned_to"]), ("in_copy", daniel["id"]))
        content = revision_content(get_story(db, sid)["revision"])
        wf.save_revision(db, sid, daniel, dict(content, headline="Rewritten by a human copy editor"))
        wf.perform(db, sid, "submit", daniel)
        wf.perform(db, sid, "approve", maya)
        wf.perform(db, sid, "publish", sara, correction_note="Rewritten and reviewed.")
        row = db.fetchone("select * from site.published_stories where story_id = ?", (sid,))
        self.assertEqual((row["status"], row["version"], row["headline"]), ("live", 2, "Rewritten by a human copy editor"))
        self.assertTrue(row["human_reviewed"])
        self.assertIn("reviewed and edited by Maya Fernandes", row["disclosure"])
        self.assertEqual(wf.autopublish(db)["published"], 0)                     # human-handled story stays human

    def test_autopublish_respects_score_guardrails_and_switch(self):
        from newsroom import workflow as wf
        from pipeline.settings import get_settings
        db = self.prepared("auto_rules")
        cfg = get_settings().editorial["autopublish"]
        saved = dict(cfg)
        try:
            cfg["score_above"] = 99
            out = wf.autopublish(db)
            self.assertEqual(out["published"], 0)
            self.assertGreater(out["below_score"], 0)
            first_score = db.fetchone("select impact_score from newsroom.stories order by id limit 1")["impact_score"]
            cfg["score_above"] = first_score                                # exactly at the bar is not above it
            self.assertGreater(wf.autopublish(db)["below_score"], 0)

            cfg["score_above"] = 60
            first = db.fetchone("select id from newsroom.stories order by id limit 1")["id"]
            db.execute("update newsroom.story_revisions set guardrails = ? where story_id = ?",
                       (db.j({"passed": False, "issues": [{"code": "x", "severity": "error", "detail": "x"}]}), first))
            db.commit()
            out = wf.autopublish(db)
            self.assertEqual(out["guardrail_errors"], 1)
            self.assertEqual(db.fetchone("select status from newsroom.stories where id = ?", (first,))["status"], "ai_draft")

            cfg["enabled"] = False
            self.assertTrue(wf.autopublish(db).get("disabled"))
            cfg["enabled"] = True
            cfg["responsible_editor"] = "daniel-okafor"                        # a copy editor can't be responsible
            self.assertIn("not found", wf.autopublish(db)["error"])
        finally:
            cfg.clear()
            cfg.update(saved)

    def test_autopublish_skips_stories_an_editor_touched(self):
        from newsroom import workflow as wf
        from newsroom.store import get_story, revision_content
        db = self.prepared("auto_touched")
        maya = self.user(db, "maya")
        ids = [r["id"] for r in db.fetchall("select id from newsroom.stories order by id")]
        wf.save_revision(db, ids[0], maya, revision_content(get_story(db, ids[0])["revision"]))   # edited -> v2
        wf.perform(db, ids[1], "kill", maya)
        out = wf.autopublish(db)
        self.assertEqual(out["published"], len(ids) - 2)
        self.assertEqual(db.fetchone("select status from newsroom.stories where id = ?", (ids[0],))["status"], "ai_draft")


    def test_responsible_editor_from_environment(self):
        from newsroom import workflow as wf
        db = self.prepared("autopub_env")
        os.environ["AUTOPUBLISH_RESPONSIBLE_EDITOR"] = "nobody@example.com"
        self.addCleanup(os.environ.pop, "AUTOPUBLISH_RESPONSIBLE_EDITOR", None)
        self.assertIn("not found", wf.autopublish(db).get("error", ""))
        os.environ["AUTOPUBLISH_RESPONSIBLE_EDITOR"] = self.user(db, "maya")["email"].upper()
        self.assertNotIn("error", wf.autopublish(db))

# =========================================================================
class ScoringViewsTest(Base):
    def test_timelines_sorting_and_pulled_items(self):
        from newsroom import web, workflow as wf
        from pipeline import cluster
        db = self.prepared("views")
        wf.autopublish(db)
        # an item published long ago is marked, not silently ignored
        db.execute("""insert into pipeline.raw_items (source_id, country, url, title, published_at, fetched_at, fingerprint,
                      title_key, topics, status) values ('uk-hmrc','UK','https://x.example/old','Old guidance page',
                      '2020-01-01T00:00:00+00:00','2026-10-15T08:00:00+00:00','oldfp','old guidance page','[]','new')""")
        db.commit()
        self.assertEqual(cluster.cluster(db)["too_old"], 1)
        row = db.fetchone("select status, filter_reason from pipeline.raw_items where fingerprint = 'oldfp'")
        self.assertEqual(row["status"], "filtered")
        self.assertIn("news window", row["filter_reason"])

        page = web.scoring_page(db, {})[1]
        self.assertIn("Pulled <b>", page)
        self.assertIn("became a <b>run</b>", page)
        self.assertIn("auto-published", page)
        for sort in ("pulled", "scored", "published", "score"):
            self.assertIn("Sort:", web.scoring_page(db, {"sort": [sort]})[1])
        self.assertIn("No stories match", web.scoring_page(db, {"country": ["AU"]})[1])
        self.assertIn("Last pull only", web.scoring_page(db, {"pull": ["last"]})[1])

        items = web.items_page(db, {})[1]
        self.assertIn("Old guidance page", items)
        self.assertIn("Too old", items)
        self.assertIn("Skipped", items)                       # the car-warranty review and bank-note story
        self.assertIn("new story", items)
        self.assertIn("joined an existing story", items)      # Fed + CFPB
        only_old = web.items_page(db, {"outcome": ["too_old"]})[1]
        self.assertIn("Old guidance page", only_old)
        self.assertNotIn("joined an existing story", only_old)


# =========================================================================
class PickupTest(Base):
    def _rejected_cluster(self, db):
        return db.fetchone(
            """select c.id from pipeline.story_clusters c
               join pipeline.evaluations e on e.cluster_id = c.id
               where e.decision = 'reject' and not exists (select 1 from newsroom.stories s where s.cluster_id = c.id)
               order by c.id limit 1""")["id"]

    def test_editor_overrides_desk_with_ai_research(self):
        from newsroom import pickup, web, workflow as wf
        from newsroom.store import get_story
        from pipeline.llm import MockClient
        db = self.prepared("pickup")
        maya, daniel = self.user(db, "maya"), self.user(db, "daniel")
        cid = self._rejected_cluster(db)

        self.assertIn(f"/scoring/{cid}/pickup", web.scoring_page(db, {}, maya)[1])
        self.assertNotIn("Pick up as editor", web.scoring_page(db, {}, daniel)[1])
        self.assertIn("Brief the AI", web.pickup_page(db, maya, cid)[1])
        with self.assertRaisesRegex(pickup.PickupError, "Only editors"):
            pickup.request_pickup(db, daniel, cid, angle="A long enough angle for readers")
        with self.assertRaisesRegex(pickup.PickupError, "Describe the angle"):
            pickup.request_pickup(db, maya, cid, angle="short")
        with self.assertRaisesRegex(pickup.PickupError, "Not a web address"):
            pickup.request_pickup(db, maya, cid, angle="A long enough angle for readers", extra_urls="ftp://x")

        rid = pickup.request_pickup(db, maya, cid, angle="Whether a 60/40 portfolio still suits savers near retirement",
                                    audience="savers near retirement, young investors", notes="Find current bond yields",
                                    extra_urls="https://example.org/a\nhttps://example.org/b")
        with self.assertRaisesRegex(pickup.PickupError, "already being researched"):
            pickup.request_pickup(db, maya, cid, angle="Another angle that is long enough")
        self.assertIn("Researching for Maya Fernandes", web.scoring_page(db, {}, maya)[1])

        story_id = pickup.run_request(rid, MockClient(), fetch_text=False, resolve_links=False, db=db)
        s = get_story(db, story_id)
        self.assertEqual((s["status"], s["origin"], s["picked_by"], s["cluster_id"]), ("ai_draft", "editor_pick", maya["id"], cid))
        research = db.unj(s["research"])
        self.assertIn("Research brief", research["text"])
        self.assertEqual(research["desk_decision"], "reject")
        urls = {x["url"] for x in s["revision"]["sources"]}
        self.assertTrue({"https://example-research.org/allocation", "https://example.org/a"} <= urls)
        self.assertTrue(s["revision"]["guardrails"]["passed"], s["revision"]["guardrails"])
        req = db.fetchone("select * from newsroom.pick_requests where id = ?", (rid,))
        self.assertEqual((req["status"], req["story_id"]), ("done", story_id))
        ev = db.fetchone("select * from newsroom.story_events where story_id = ? and action = 'picked_up'", (story_id,))
        self.assertEqual(ev["actor_id"], maya["id"])
        self.assertIn("override of the desk's reject", ev["note"])

        # never auto-published, even with a high score
        db.execute("update newsroom.stories set impact_score = 95 where id = ?", (story_id,))
        db.commit()
        wf.autopublish(db)
        self.assertEqual(get_story(db, story_id)["status"], "ai_draft")
        with self.assertRaisesRegex(pickup.PickupError, "already in the newsroom"):
            pickup.request_pickup(db, maya, cid, angle="A long enough angle for readers")

        # the editor can approve it or hand it to a copy editor like any draft
        self.assertIn("assign", wf.allowed_actions(s, maya))
        page = web.story_page(db, story_id, maya)[1]
        self.assertIn("Editor pick · research brief", page)
        self.assertIn("example-research.org", page)
        self.assertIn("Open story", web.scoring_page(db, {}, maya)[1])

    def test_failed_pickup_can_be_retried_and_pipeline_skips_picked_cluster(self):
        from newsroom import pickup
        from pipeline import writer
        from pipeline.llm import LLMError, MockClient
        db = self.prepared("pickup_fail")
        maya = self.user(db, "maya")
        cid = self._rejected_cluster(db)

        class Broken(MockClient):
            def research(self, **kw):
                raise LLMError("search unavailable")

        rid = pickup.request_pickup(db, maya, cid, angle="A long enough angle for readers")
        with self.assertRaises(LLMError):
            pickup.run_request(rid, Broken(), fetch_text=False, resolve_links=False, db=db)
        req = db.fetchone("select status, error from newsroom.pick_requests where id = ?", (rid,))
        self.assertEqual(req["status"], "failed")
        self.assertIn("search unavailable", req["error"])
        rid2 = pickup.request_pickup(db, maya, cid, angle="A long enough angle for readers", research=False)
        story_id = pickup.run_request(rid2, MockClient(), fetch_text=False, resolve_links=False, db=db)
        self.assertIsNone(db.fetchone("select research from newsroom.stories where id = ?", (story_id,))["research"])

        # if the desk later flips this cluster to run, the pipeline doesn't draft it a second time
        db.execute("update pipeline.story_clusters set status = 'commissioned' where id = ?", (cid,))
        db.commit()
        with self.assertRaisesRegex(ValueError, "already has a newsroom story"):
            writer.write_one(db, MockClient(), {"cluster_id": cid, "country": "US", "angle": "x", "audience": []}, fetch_text=False)
        self.assertEqual(db.fetchone("select count(*) as n from newsroom.stories where cluster_id = ?", (cid,))["n"], 1)

    def test_gemini_research_reads_grounding_metadata(self):
        from pipeline.llm import GeminiClient

        class Resp:
            status_code = 200
            def json(self):
                return {"candidates": [{"finishReason": "STOP",
                        "content": {"parts": [{"text": "KEY FACTS\n- Yields were 4.1%."}]},
                        "groundingMetadata": {"webSearchQueries": ["60/40 portfolio 2026"],
                                              "groundingChunks": [{"web": {"uri": "https://r.example/1", "title": "reuters.com"}},
                                                                  {"web": {"uri": "https://r.example/1", "title": "dup"}}]}}]}

        class Session:
            def post(self, url, json=None, timeout=None, headers=None):
                Session.body = json
                return Resp()

        c = GeminiClient("k")
        c.session = Session()
        out = c.research(model="gemini-flash-latest", system="s", prompt="p")
        self.assertEqual(Session.body["tools"], [{"google_search": {}}])
        self.assertNotIn("responseMimeType", Session.body["generationConfig"])
        self.assertEqual(out["sources"], [{"title": "reuters.com", "url": "https://r.example/1"}])
        self.assertEqual(out["queries"], ["60/40 portfolio 2026"])


# =========================================================================
class SourcesTest(Base):
    def test_editor_adds_and_switches_off_sources(self):
        from newsroom import sources as sm
        from pipeline import feeds
        db = self.prepared("sources")
        maya, daniel = self.user(db, "maya"), self.user(db, "daniel")
        ok = lambda url: {"items": 7, "latest": ["A", "B"], "newest": None}

        with self.assertRaisesRegex(sm.SourceError, "Only editors"):
            sm.add_source(db, daniel, name="X", country="US", url="https://x.example/feed", tester=ok)
        with self.assertRaisesRegex(sm.SourceError, "Full-page text"):
            sm.add_source(db, maya, name="X", country="US", url="https://x.example/feed", full_text=True, tester=ok)
        existing = db.fetchone("select url from pipeline.sources where id = 'us-fed-press'")["url"]
        with self.assertRaisesRegex(sm.SourceError, "already a source"):
            sm.add_source(db, maya, name="Fed again", country="US", url=existing, tester=ok)

        out = sm.add_source(db, maya, name="Treasury Press", country="US", url="https://home.treasury.example/rss",
                            kind="official", reuse="public_domain", full_text=True, tester=ok)
        self.assertEqual(out["id"], "us-treasury-press")
        row = db.fetchone("select * from pipeline.sources where id = 'us-treasury-press'")
        self.assertEqual((row["added_by"], bool(row["active"]), bool(row["full_text"])), (maya["email"], True, True))

        def broken(url):
            raise sm.SourceError("That address didn't return an RSS or Atom feed with any stories.")
        with self.assertRaisesRegex(sm.SourceError, "didn't return"):
            sm.add_source(db, maya, name="Broken", country="UK", url="https://broken.example/", tester=broken)
        self.assertIsNone(db.fetchone("select id from pipeline.sources where name = 'Broken'"))

        # switching off a feeds.yaml source survives the next pipeline sync
        sm.set_active(db, maya, "us-nerdwallet", False)
        active = {s["id"] for s in feeds.sync_sources(db)}
        self.assertNotIn("us-nerdwallet", active)
        self.assertIn("us-treasury-press", active)

    def test_feed_tester_refuses_private_addresses(self):
        from newsroom import sources as sm
        for url in ("http://127.0.0.1:5432/", "http://localhost/feed", "file:///etc/hosts", "http://169.254.169.254/"):
            with self.assertRaises(sm.SourceError, msg=url):
                sm.test_feed(url)


# =========================================================================
class AuthTest(Base):
    def test_passwords_sessions_and_team_management(self):
        from newsroom import auth
        db = self.prepared("auth")
        self.assertTrue(auth.verify_password("correct horse 1", auth.hash_password("correct horse 1")))
        self.assertFalse(auth.verify_password("wrong", auth.hash_password("correct horse 1")))
        self.assertFalse(auth.verify_password("x", None))

        self.assertFalse(auth.has_admin_login(db))                       # placeholders have no passwords
        admin, temp = auth.create_user(db, None, email="Owner@Example.com", display_name="Site Owner",
                                       role="admin", password="Owner-pass-2026")
        self.assertIsNone(temp)
        self.assertTrue(auth.has_admin_login(db))
        with self.assertRaisesRegex(auth.AuthError, "at least"):
            auth.create_user(db, None, email="x@example.com", display_name="X", role="admin", password="short")

        user, token = auth.login(db, "owner@example.com", "Owner-pass-2026", "1.2.3.4")
        self.assertEqual(auth.user_for_session(db, token)["id"], admin["id"])
        self.assertIsNone(auth.user_for_session(db, "not-a-token"))
        stored = db.fetchone("select token_hash from newsroom.sessions")["token_hash"]
        self.assertNotEqual(stored, token)                                 # only the hash is stored

        # admin creates a copy editor with a temporary password
        ce, temp = auth.create_user(db, admin, email="ce@example.com", display_name="Priya Shah", role="copywriter")
        self.assertTrue(temp and ce["must_change_password"])
        with self.assertRaisesRegex(auth.AuthError, "already uses"):
            auth.create_user(db, admin, email="CE@example.com", display_name="Dup", role="editor")
        with self.assertRaisesRegex(auth.AuthError, "Only an admin"):
            auth.create_user(db, ce, email="y@example.com", display_name="Y", role="editor")
        u, t = auth.login(db, "ce@example.com", temp, "5.6.7.8")
        self.assertTrue(u["must_change_password"])
        with self.assertRaisesRegex(auth.AuthError, "don't match"):
            auth.change_password(db, u, temp, "Brand-new-pass-1", "Brand-new-pass-2")
        auth.change_password(db, u, temp, "Brand-new-pass-1", "Brand-new-pass-1")
        self.assertFalse(db.fetchone("select must_change_password from newsroom.users where id = ?", (ce["id"],))["must_change_password"])

        # deactivating ends sessions; the last admin is protected
        auth.update_user(db, admin, ce["id"], active=False)
        self.assertIsNone(auth.user_for_session(db, t))
        with self.assertRaisesRegex(auth.AuthError, "at least one active admin"):
            auth.update_user(db, admin, admin["id"], role="editor")
        temp2 = auth.reset_password(db, admin, admin["id"])
        self.assertIsNone(auth.user_for_session(db, token))                # reset ends the admin's sessions too
        self.assertTrue(auth.verify_password(temp2, db.fetchone("select password_hash from newsroom.users where id = ?", (admin["id"],))["password_hash"]))

    def test_login_rate_limit(self):
        from newsroom import auth
        db = self.prepared("ratelimit")
        auth.create_user(db, None, email="rl@example.com", display_name="RL", role="admin", password="Rate-limit-pass-1")
        for _ in range(5):
            with self.assertRaisesRegex(auth.AuthError, "incorrect"):
                auth.login(db, "rl@example.com", "wrong-password", "9.9.9.9")
        with self.assertRaisesRegex(auth.AuthError, "Too many"):
            auth.login(db, "rl@example.com", "Rate-limit-pass-1", "9.9.9.9")
        auth.limiter.clear("e:rl@example.com", "c:9.9.9.9")


# =========================================================================
class ScheduleTest(Base):
    def test_regions_run_once_a_day_at_local_noon(self):
        from newsroom import schedule
        from newsroom.pull import PullBusy

        os.environ["DATABASE_URL"] = f"sqlite:///{self.tmp / 'schedule'}.db"
        from pipeline.settings import get_settings
        get_settings.cache_clear()
        calls = []

        def runner(countries, trigger):
            calls.append((tuple(countries), trigger))
            return {"ingest": {"new": 3}, "write": {"drafted": 1}, "autopublish": {"published": 1}}

        try:
            # 16:05 UTC on 15 Oct 2026 = 12:05 New York (EDT), 17:05 London (BST), 03:05 Sydney
            now = datetime(2026, 10, 15, 16, 5, tzinfo=timezone.utc)
            self.assertEqual({r["region"] for r in schedule.tick(now, runner=runner, dry_run=True)}, {"new-york", "london"})
            res = schedule.tick(now, runner=runner)
            self.assertEqual(sorted(c for c, _ in calls), [("UK",), ("US", "CA")])
            self.assertTrue(all(r["ok"] for r in res))
            self.assertEqual(schedule.tick(now, runner=runner), [])                  # already ran today
            # 22:30 UTC: New York 18:30 and London 23:30 are past the catch-up window; Sydney is before noon
            self.assertEqual(schedule.tick(datetime(2026, 10, 15, 22, 30, tzinfo=timezone.utc), runner=runner), [])
            # 01:10 UTC on 16 Oct = 12:10 Sydney (AEDT)
            schedule.tick(datetime(2026, 10, 16, 1, 10, tzinfo=timezone.utc), runner=runner)
            self.assertEqual(calls[-1], (("AU",), "schedule:sydney"))

            # busy: the claim is released so the next tick retries
            def busy(countries, trigger):
                raise PullBusy("busy")
            out = schedule.tick(datetime(2026, 10, 16, 16, 5, tzinfo=timezone.utc), runner=busy)
            self.assertTrue(out and all("busy" in r["error"] for r in out))
            schedule.tick(datetime(2026, 10, 16, 16, 15, tzinfo=timezone.utc), runner=runner)
            self.assertEqual(calls[-1][1], "schedule:london")

            # daylight saving: London switches to GMT on 25 Oct, New York to EST on 1 Nov
            plan = {r["id"]: r for r in schedule.plan(datetime(2026, 10, 26, 0, 0, tzinfo=timezone.utc))}
            self.assertEqual(plan["london"]["next_slot_utc"].hour, 12)
            self.assertEqual(plan["new-york"]["next_slot_utc"].hour, 16)
            plan = {r["id"]: r for r in schedule.plan(datetime(2026, 11, 2, 0, 0, tzinfo=timezone.utc))}
            self.assertEqual(plan["new-york"]["next_slot_utc"].hour, 17)
        finally:
            os.environ["DATABASE_URL"] = f"sqlite:///{self.tmp / 'default.db'}"
            get_settings.cache_clear()

    def test_launchd_plist(self):
        import plistlib
        from newsroom import schedule
        p = plistlib.loads(schedule.plist_bytes("/x/.venv/bin/python"))
        self.assertEqual(p["ProgramArguments"], ["/x/.venv/bin/python", "-m", "newsroom", "tick"])
        self.assertEqual(p["StartInterval"], 600)


# =========================================================================
class PullTest(Base):
    def test_full_pull_and_lock(self):
        from newsroom import pull
        from newsroom.users import seed_placeholders
        from pipeline.db import connect
        from pipeline.llm import MockClient
        db = connect("pipeline")                       # default SQLite database for this test class
        seed_placeholders(db)
        db.close()
        result = pull.run_pull(["US", "CA"], trigger="test", client=MockClient(), fetch_text=False)
        self.assertEqual(result["ingest"]["errors"], [])
        self.assertGreater(result["write"]["drafted"], 0)
        self.assertEqual(result["autopublish"]["published"], result["write"]["drafted"])
        site = connect("site")
        countries = {r["country"] for r in site.fetchall("select country from site.published_stories")}
        site.close()
        self.assertTrue(countries <= {"US", "CA"} and countries)
        st = pull.status()
        self.assertEqual((st["state"], st["running"]), ("finished", False))
        self.assertEqual(st["summary"]["auto_published"], result["autopublish"]["published"])
        with pull._lock():
            self.assertTrue(pull.is_running())
            with self.assertRaises(pull.PullBusy):
                pull.run_pull(["UK"], client=MockClient())
        with self.assertRaises(ValueError):
            pull.normalise_countries("US,XX")


# =========================================================================
class LiveEditTest(Base):
    """Correcting live stories, copy editors on any story, unpublishing while a correction is open."""

    def _published(self, name):
        from newsroom import workflow as wf
        db = self.prepared(name)
        maya, sara = self.user(db, "maya"), self.user(db, "sara")
        sid = db.fetchone("select id from newsroom.stories order by id limit 1")["id"]
        wf.perform(db, sid, "approve", maya)
        wf.perform(db, sid, "publish", sara)
        return db, sid

    def test_copy_editor_corrects_live_story(self):
        from newsroom import workflow as wf
        from newsroom.store import get_story, revision_content
        db, sid = self._published("live_copy")
        maya, daniel = self.user(db, "maya"), self.user(db, "daniel")
        s = get_story(db, sid)
        self.assertTrue(s["live"])
        self.assertTrue(wf.can_edit(s, daniel))
        content = revision_content(s["revision"])
        wf.save_revision(db, sid, daniel, dict(content, headline=content["headline"] + " (copy edited)"), note="Tidy")
        s = get_story(db, sid)
        self.assertEqual((s["status"], s["assigned_to"], s["current_version"]), ("in_copy", daniel["id"], 2))
        site = db.fetchone("select status, version from site.published_stories where story_id = ?", (sid,))
        self.assertEqual((site["status"], site["version"]), ("live", 1))   # readers still see v1

        # another copy editor can't take it over; a publisher can't edit at all
        other = dict(daniel, id=daniel["id"] + 999)
        self.assertFalse(wf.can_edit(s, other))
        self.assertFalse(wf.can_edit(s, self.user(db, "sara")))

        # can't kill while live; can unpublish while the correction is open, which keeps the work in progress
        self.assertNotIn("kill", wf.allowed_actions(s, maya))
        with self.assertRaisesRegex(wf.WorkflowError, "unpublish it before"):
            wf.perform(db, sid, "kill", maya)
        self.assertIn("unpublish", wf.allowed_actions(s, maya))
        s = wf.perform(db, sid, "unpublish", maya, note="Pull while we fix it")
        self.assertEqual(s["status"], "in_copy")
        self.assertFalse(s["live"])
        self.assertEqual(db.fetchone("select status from site.published_stories where story_id = ?", (sid,))["status"], "retracted")

        # copy editor submits, editor approves, publisher republishes v2
        wf.perform(db, sid, "submit", daniel)
        wf.perform(db, sid, "approve", maya)
        s = wf.perform(db, sid, "publish", self.user(db, "sara"), correction_note="Copy edited.")
        site = db.fetchone("select status, version from site.published_stories where story_id = ?", (sid,))
        self.assertEqual((site["status"], site["version"]), ("live", 2))

    def test_editor_corrects_live_story_and_autopub_flag_clears(self):
        from newsroom import workflow as wf
        from newsroom.store import get_story, revision_content
        db, sid = self._published("live_editor")
        db.execute("update newsroom.stories set auto_published = ? where id = ?", (True, sid)); db.commit()
        maya = self.user(db, "maya")
        content = revision_content(get_story(db, sid)["revision"])
        wf.save_revision(db, sid, maya, dict(content, dek="A clearer standfirst."))
        s = get_story(db, sid)
        self.assertEqual((s["status"], s["editor_id"]), ("in_review", None))
        self.assertTrue(s["live"])
        ev = db.fetchone("select note from newsroom.story_events where story_id = ? and action = 'edited'", (sid,))
        self.assertIn("site keeps v1", ev["note"])
        wf.perform(db, sid, "approve", maya)
        s = wf.perform(db, sid, "publish", self.user(db, "sara"))
        self.assertFalse(s["auto_published"])
        self.assertEqual(db.fetchone("select human_reviewed from site.published_stories where story_id = ?", (sid,))["human_reviewed"] in (True, 1), True)

    def test_killed_is_not_editable(self):
        from newsroom import workflow as wf
        from newsroom.store import get_story
        db = self.prepared("killed")
        maya, daniel = self.user(db, "maya"), self.user(db, "daniel")
        sid = db.fetchone("select id from newsroom.stories order by id limit 1")["id"]
        wf.perform(db, sid, "kill", maya)
        self.assertFalse(wf.can_edit(get_story(db, sid), daniel))
        self.assertFalse(wf.can_edit(get_story(db, sid), maya))


# =========================================================================
class BlockEditorTest(Base):
    """Manual edits are stored as blocks; the site serves the blocks; AI drafts are untouched."""

    def test_sanitiser(self):
        from newsroom import blocks as blk
        dirty = ('<strong>Rates</strong> <em>rose</em> <script>alert(1)</script><span onclick="x()">here</span> '
                 '<a href="javascript:alert(1)">bad</a> <a href="https://fed.gov/x" onclick="y">good</a> <b>open')
        out = blk.clean_inline(dirty)
        self.assertNotIn("script", out); self.assertNotIn("alert", out); self.assertNotIn("onclick", out)
        self.assertNotIn("javascript", out)
        self.assertIn("<b>Rates</b>", out); self.assertIn("<i>rose</i>", out)
        self.assertIn('<a href="https://fed.gov/x">good</a>', out)
        self.assertTrue(out.endswith("<b>open</b>"))
        self.assertEqual(blk.clean_inline("a < b & c"), "a &lt; b &amp; c")

    def test_clean_blocks(self):
        from newsroom import blocks as blk
        raw = [{"type": "paragraph", "data": {"text": "  "}}, {"type": "video", "data": {}},
               {"type": "header", "data": {"text": "Costs", "level": 5}},
               {"type": "list", "data": {"style": "ordered", "items": [{"content": "One", "items": []}, "", "Two"]}},
               {"type": "table", "data": {"withHeadings": True, "content": [["Fuel", "Change"], ["Oil", "+21%", "x"], ["", ""]]}},
               {"type": "delimiter", "data": {}}]
        out = blk.clean_blocks(json.dumps(raw))
        self.assertEqual([b["type"] for b in out], ["header", "list", "table"])   # empty, unknown, trailing divider dropped
        self.assertEqual(out[0]["data"]["level"], 2)
        self.assertEqual(out[1]["data"]["items"], ["One", "Two"])
        self.assertEqual(out[2]["data"]["content"], [["Fuel", "Change", ""], ["Oil", "+21%", "x"]])
        with self.assertRaises(blk.BlockError):
            blk.clean_blocks("{not json")

    def test_ai_sections_open_as_blocks(self):
        from newsroom import blocks as blk
        content = {"headline": "H", "dek": "D", "sections": {"what_happened": "One **two**.\n\nThree [link](https://a.gov/x).",
                   "money_impact": "M", "who_is_affected": "", "illustrative_example": "Illustrative example: 5"},
                   "action_steps": ["Do it"], "takeaways": ["Keep it"]}
        b = blk.content_to_blocks(content)
        self.assertEqual(b[0], {"type": "header", "data": {"text": "What happened", "level": 2}})
        self.assertEqual(b[1]["data"]["text"], "One <b>two</b>.")
        self.assertIn('<a href="https://a.gov/x">link</a>', b[2]["data"]["text"])
        self.assertIn({"type": "list", "data": {"style": "ordered", "items": ["Do it"]}}, b)
        md = blk.blocks_markdown(dict(content, blocks=b, sources=[{"name": "Fed", "title": "T", "url": "https://fed.gov"}]))
        self.assertIn("## What happened", md); self.assertIn("One **two**.", md); self.assertIn("1. Do it", md)
        self.assertIn("[link](https://a.gov/x)", md); self.assertIn("## Sources", md)

    def test_freeform_checks_and_illustrative_section(self):
        from newsroom import workflow as wf
        from newsroom.store import editor_blocks, get_story
        db = self.prepared("blocks_checks")
        sid = db.fetchone("select id from newsroom.stories order by id limit 1")["id"]
        s = get_story(db, sid)
        base = editor_blocks(s["revision"])
        content = {"headline": s["revision"]["headline"], "dek": "", "blocks": base,
                   "sources": s["revision"]["sources"], "topics": []}
        rep = wf.check_content(db, s, content)
        codes = {i["code"]: i["severity"] for i in rep["issues"]}
        self.assertEqual(codes.get("missing_section"), "warning")     # empty standfirst only warns
        self.assertNotIn("action_steps_count", codes)
        flagged = dict(content, blocks=base + [{"type": "paragraph", "data": {"text": "Rates could hit 7.5% next year."}}])
        self.assertIn("7.5%", " ".join(wf.check_content(db, s, flagged)["metrics"]["unsupported_numbers"]))
        example = dict(content, blocks=base + [{"type": "header", "data": {"text": "A worked example", "level": 2}},
                                                {"type": "paragraph", "data": {"text": "Say your rate is 7.5% on 10,000."}}])
        self.assertNotIn("7.5%", " ".join(wf.check_content(db, s, example)["metrics"].get("unsupported_numbers") or []))

    def test_block_version_is_what_the_site_serves(self):
        from newsroom import web, workflow as wf
        from newsroom.store import editor_blocks, get_story
        from pipeline.db import Database
        db = self.prepared("blocks_site")
        maya, sara = self.user(db, "maya"), self.user(db, "sara")
        ids = [r["id"] for r in db.fetchall("select id from newsroom.stories order by id")]
        sid, ai_sid = ids[0], ids[1]
        # an AI story published as-is keeps the AI format on the site
        wf.perform(db, ai_sid, "approve", maya); wf.perform(db, ai_sid, "publish", sara)
        self.assertIsNone(Database.unj(db.fetchone("select blocks from site.published_stories where story_id = ?", (ai_sid,))["blocks"], None))

        s = get_story(db, sid)
        body = editor_blocks(s["revision"]) + [
            {"type": "quote", "data": {"text": "Rates are on hold.", "caption": "Federal Reserve"}},
            {"type": "table", "data": {"withHeadings": True, "content": [["Product", "Effect"], ["Variable loan", "First to move"]]}}]
        wf.save_revision(db, sid, maya, {"headline": "Rates on hold: what changes for you", "dek": s["revision"]["dek"],
                                         "blocks": body, "meta_description": "m", "topics": [], "sources": s["revision"]["sources"]},
                         note="Rewrote in blocks")
        rev = get_story(db, sid)["revision"]
        self.assertEqual(rev["version"], 2); self.assertTrue(rev["blocks"])
        self.assertIn("| Product | Effect |", rev["body_md"])
        page = web.story_page(db, sid, maya)[1]
        self.assertIn('<article class="blocks">', page); self.assertIn("<blockquote>", page)

        wf.perform(db, sid, "approve", maya, override=True, note="test"); wf.perform(db, sid, "publish", sara)
        row = db.fetchone("select slug, blocks, body_md from site.published_stories where story_id = ?", (sid,))
        self.assertTrue(Database.unj(row["blocks"], None))
        site = web.site_feed_page(db, row["slug"])[1]
        self.assertIn('<article class="blocks">', site)
        self.assertIn('<table class="data"><thead><tr><th>Product</th>', site)
        self.assertIn("<cite>Federal Reserve</cite>", site)
        self.assertIn("<h2>Sources</h2>", site)
        # the AI drafting path is unchanged: v1 still has no blocks
        self.assertIsNone(Database.unj(db.fetchone("select blocks from newsroom.story_revisions where story_id = ? and version = 1", (sid,))["blocks"], None))


# =========================================================================
class CategoryTest(Base):
    """Feeds decide the category; each category has its own desk, sections, checks and staff."""

    SPORTS_XML = """<?xml version="1.0"?><rss version="2.0"><channel><title>Sports wire</title>
<item><title>Rivers City win the championship final with a late goal</title><link>https://sports.example.com/final</link>
<description>Rivers City beat Harbour United 3-2 in the championship final on Saturday, their 12th title.</description><pubDate>{d1}</pubDate></item>
<item><title>Best bets and odds for Sunday's games</title><link>https://sports.example.com/odds</link>
<description>Our picks.</description><pubDate>{d1}</pubDate></item>
<item><title>Federal Reserve issues FOMC statement holding the federal funds rate target range</title>
<link>https://sports.example.com/fed-and-sport</link><description>A sports outlet repeats the rate news.</description><pubDate>{d1}</pubDate></item>
</channel></rss>"""

    def _with_sports(self, name):
        from fixtures import rfc822
        from pipeline import cluster, desk, feeds, writer
        from pipeline.db import utcnow
        from pipeline.llm import MockClient
        db = self.prepared(name)
        path = self.tmp / f"{name}-sports.xml"
        path.write_text(self.SPORTS_XML.format(d1=rfc822(2)), encoding="utf-8")
        db.execute("""insert into pipeline.sources (id, name, country, category, url, kind, reuse, full_text, active, added_by, created_at)
                      values (?,?,?,?,?,?,?,?,?,?,?)""",
                   (f"us-sports-{name}", "Sports wire", "US", "sports", f"file://{path}", "publisher", "link_only", False, True, "test", utcnow()))
        db.commit()
        feeds.ingest(db); cluster.cluster(db); desk.evaluate(db, MockClient()); writer.write(db, MockClient(), fetch_text=False)
        return db

    def test_sports_story_uses_the_sports_desk(self):
        from newsroom import workflow as wf
        from newsroom.store import get_story
        from pipeline import categories
        from pipeline.db import Database
        db = self._with_sports("cat_sports")
        odds = db.fetchone("select status, filter_reason from pipeline.raw_items where url like '%/odds'")
        self.assertEqual(odds["status"], "filtered")                      # sports title filter (betting)
        final = db.fetchone("""select c.id, c.category from pipeline.raw_items r join pipeline.story_clusters c on c.id = r.cluster_id
                               where r.url = 'https://sports.example.com/final'""")
        self.assertEqual(final["category"], "sports")
        ev = db.fetchone("select * from pipeline.evaluations where cluster_id = ? order by id desc limit 1", (final["id"],))
        self.assertEqual(set(Database.unj(ev["scores"], {})), set(categories.dimension_keys("sports")))
        self.assertIsNone(ev["wallet_impact"])                              # finance columns stay empty
        self.assertEqual(ev["decision"], "run")
        # the same headline from a sports feed starts its own sports story; it doesn't join the finance one
        fed = db.fetchall("""select distinct c.category from pipeline.raw_items r join pipeline.story_clusters c on c.id = r.cluster_id
                             where r.title like 'Federal Reserve issues FOMC%'""")
        self.assertEqual(sorted(r["category"] for r in fed), ["finance", "sports"])
        # finance evaluations keep their columns
        fin = db.fetchone("""select e.* from pipeline.evaluations e join pipeline.story_clusters c on c.id = e.cluster_id
                             where c.category = 'finance' and e.decision = 'run' limit 1""")
        self.assertIsNotNone(fin["wallet_impact"])

        st = db.fetchone("select id from newsroom.stories where cluster_id = ?", (final["id"],))
        story = get_story(db, st["id"])
        self.assertEqual(story["category"], "sports")
        self.assertIn("## Why it matters", story["revision"]["body_md"])
        self.assertIn("## What to watch next", story["revision"]["body_md"])
        self.assertNotIn("What it means for your money", story["revision"]["body_md"])

        maya, sara = self.user(db, "maya"), self.user(db, "sara")
        wf.perform(db, story["id"], "approve", maya, override=True, note="test")
        wf.perform(db, story["id"], "publish", sara)
        row = db.fetchone("select category, disclosure from site.published_stories where story_id = ?", (story["id"],))
        self.assertEqual(row["category"], "sports")
        self.assertNotIn("financial", row["disclosure"])                     # finance disclaimer only on finance

    def test_fitness_is_never_auto_published(self):
        from newsroom import workflow as wf
        from newsroom.store import create_ai_draft, get_story
        from pipeline import categories
        db = self.prepared("cat_fitness")
        self.assertFalse(categories.autopublish("fitness")["enabled"])
        self.assertTrue(categories.autopublish("sports")["enabled"])
        content = {"headline": "Walking more helps", "dek": "d", "sections": {"what_happened": "x"}, "action_steps": [],
                   "takeaways": [], "topics": [], "sources": []}
        sid = create_ai_draft(db, cluster_id=None, country="US", slug="us-walking-fit", content=content,
                              guardrails={"passed": True, "issues": [], "metrics": {}}, model="m", impact_score=95.0,
                              angle="a", attempts=1, category="fitness")
        db.commit()
        out = wf.autopublish(db)
        self.assertGreaterEqual(out["category_off"], 1)
        s = get_story(db, sid)
        self.assertEqual((s["status"], s["category"]), ("ai_draft", "fitness"))
        self.assertIn("## What's new", s["revision"]["body_md"])          # fitness section title

    def test_staff_only_work_in_their_categories(self):
        from newsroom import auth, workflow as wf
        from newsroom import sources as src_mod
        from newsroom.store import get_story
        db = self._with_sports("cat_staff")
        admin = {"id": 0, "role": "admin", "display_name": "Admin"}
        ed, _ = auth.create_user(db, admin, email="sports.ed@example.com", display_name="Sam Sports", role="editor",
                                 categories=["sports"], password="Sports-editor-pass-1")
        cw, _ = auth.create_user(db, admin, email="sports.copy@example.com", display_name="Cara Copy", role="copywriter",
                                 categories=["sports"], password="Sports-copy-pass-1")
        ed = db.fetchone("select * from newsroom.users where id = ?", (ed["id"],))
        cw = db.fetchone("select * from newsroom.users where id = ?", (cw["id"],))
        fin = get_story(db, db.fetchone("select id from newsroom.stories where category = 'finance' order by id limit 1")["id"])
        spo = get_story(db, db.fetchone("select id from newsroom.stories where category = 'sports' order by id limit 1")["id"])
        self.assertEqual(wf.allowed_actions(fin, ed), [])
        self.assertFalse(wf.can_edit(fin, ed))
        with self.assertRaisesRegex(wf.WorkflowError, "doesn't work on Finance"):
            wf.perform(db, fin["id"], "approve", ed)
        self.assertIn("approve", wf.allowed_actions(spo, ed))
        self.assertTrue(wf.can_edit(spo, cw))
        # queues only list their categories
        cats = {r["category"] for r in wf.queue(db, ("ai_draft",), only_categories={"sports"})}
        self.assertEqual(cats, {"sports"})
        # a sports copy editor can't be given a finance story
        maya = self.user(db, "maya")
        with self.assertRaisesRegex(wf.WorkflowError, "doesn't work on Finance"):
            wf.perform(db, fin["id"], "assign", maya, assignee_id=cw["id"])
        wf.perform(db, spo["id"], "assign", ed, assignee_id=cw["id"])
        # moving between categories needs access to both
        with self.assertRaises(wf.WorkflowError):
            wf.set_category(db, spo["id"], ed, "finance")
        moved = wf.set_category(db, fin["id"], maya, "entertainment", note="celebrity money story")
        self.assertEqual(moved["category"], "entertainment")
        self.assertIn("recategorized", [r["action"] for r in db.fetchall(
            "select action from newsroom.story_events where story_id = ?", (fin["id"],))])
        # sources: a sports editor adds sports feeds only
        ok = lambda url: {"items": 3, "latest": [], "newest": None}   # noqa: E731
        src_mod.add_source(db, ed, name="Sports Two", country="US", url="https://sports2.example.com/rss",
                           category="sports", tester=ok)
        with self.assertRaises(src_mod.SourceError):
            src_mod.add_source(db, ed, name="Money Two", country="US", url="https://money2.example.com/rss",
                               category="finance", tester=ok)
        self.assertEqual(db.fetchone("select category from pipeline.sources where name = 'Sports Two'")["category"], "sports")
        # unassigned staff and admins see everything
        from pipeline import categories
        self.assertIsNone(categories.user_scope(maya))
        self.assertIsNone(categories.user_scope({"role": "admin", "categories": ["sports"]}))


# =========================================================================
class HttpTest(Base):
    """Drive the real desk server over HTTP: login, sessions, roles, CSRF."""

    def setUp(self):
        from http.server import ThreadingHTTPServer
        from newsroom import auth, web
        from newsroom.users import seed_placeholders
        from pipeline.db import connect
        db = connect("newsroom")
        seed_placeholders(db)
        if not db.fetchone("select id from newsroom.users where email = 'boss@example.com'"):
            auth.create_user(db, None, email="boss@example.com", display_name="Boss", role="admin", password="Boss-password-2026")
        db.close()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def client(self):
        jar = http.cookiejar.CookieJar()
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar)), jar

    def post(self, opener, path, data, headers=None):
        req = urllib.request.Request(self.base + path, data=urllib.parse.urlencode(data).encode(), method="POST",
                                     headers=headers or {})
        return opener.open(req)

    def test_login_roles_and_csrf(self):
        op, jar = self.client()
        r = op.open(self.base + "/")
        self.assertTrue(r.url.endswith("/login"))
        r = self.post(op, "/login", {"email": "boss@example.com", "password": "nope-nope-nope"})
        self.assertIn("incorrect", r.read().decode())
        r = self.post(op, "/login", {"email": "boss@example.com", "password": "Boss-password-2026"})
        self.assertTrue(r.url.endswith("/"))
        html = r.read().decode()
        self.assertIn("Sign out", html)
        self.assertIn("Sources &amp; pulls", html)
        cookie = next(c for c in jar if c.name == "fh_session")
        self.assertTrue(cookie.has_nonstandard_attr("HttpOnly"))

        # admin creates a copy editor; temp password is shown once, not put in a URL
        r = self.post(op, "/team", {"display_name": "Copy Person", "email": "copy@example.com", "role": "copywriter", "job_title": ""})
        page = r.read().decode()
        self.assertTrue(r.url.endswith("/team"))
        import re
        temp = re.search(r'<div class="secret">([^<]+)</div>', page).group(1)

        # cross-site form posts are refused even with a valid session
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.post(op, "/pull", {"countries": "US"}, headers={"Origin": "https://evil.example"})
        self.assertEqual(cm.exception.code, 403)

        # the copy editor must change the temporary password first, and can't manage team or pull
        op2, _ = self.client()
        r = self.post(op2, "/login", {"email": "copy@example.com", "password": temp})
        self.assertIn("/account", r.url)
        r = op2.open(self.base + "/sources")
        self.assertIn("/account", r.url)                                   # forced to change password
        r = self.post(op2, "/account/password", {"current": temp, "new": "Copy-editor-pass-1", "confirm": "Copy-editor-pass-1"})
        self.assertIn("Password changed", urllib.parse.unquote(r.url))
        r = self.post(op2, "/pull", {"countries": "US"})
        self.assertIn("Only editors and admins", urllib.parse.unquote(r.url))
        r = self.post(op2, "/team", {"display_name": "Sneaky", "email": "s@example.com", "role": "admin"})
        self.assertIn("Only an admin", urllib.parse.unquote(r.url))
        self.assertNotIn("Add a team member", op2.open(self.base + "/team").read().decode())

        # sign out ends the session
        self.post(op, "/logout", {})
        self.assertTrue(op.open(self.base + "/").url.endswith("/login"))

    def test_site_page_desk_tools(self):
        from newsroom import workflow as wf
        from newsroom.users import find_user
        from pipeline import cluster, desk, feeds, writer
        from pipeline.db import connect
        from pipeline.llm import MockClient
        db = connect("newsroom")
        if not db.fetchone("select id from newsroom.stories limit 1"):
            feeds.ingest(db); cluster.cluster(db); desk.evaluate(db, MockClient())
            writer.write(db, MockClient(), fetch_text=False)
        sid = db.fetchone("select id from newsroom.stories where status = 'ai_draft' order by id limit 1")["id"]
        wf.perform(db, sid, "approve", find_user(db, "maya"))
        story = wf.perform(db, sid, "publish", find_user(db, "sara"))
        slug = story["slug"]
        db.close()

        op, _ = self.client()
        self.post(op, "/login", {"email": "boss@example.com", "password": "Boss-password-2026"})
        page = op.open(self.base + f"/site/{slug}").read().decode()
        for text in ("Edit live story", "Unpublish", "How this story came to be", "Pulled from", "Scored", "Open in the newsroom"):
            self.assertIn(text, page)

        # edit form remembers where it came from and warns that this is a correction
        page = op.open(self.base + f"/story/{sid}/edit?next=/site/{slug}").read().decode()
        self.assertIn("Readers see v1", page)
        self.assertIn("update live story", page)

        # admin: save, approve and update the live story in one go -> back on the site page at v2
        from newsroom.store import get_story, revision_content
        db = connect("newsroom")
        c = revision_content(get_story(db, sid)["revision"])
        db.close()
        form = {"headline": c["headline"] + " today", "dek": c.get("dek") or "", "action_steps": "\n".join(c["action_steps"]),
                "takeaways": "\n".join(c["takeaways"]), "meta_description": c.get("meta_description") or "",
                "note": "Sharper headline", "next": f"/site/{slug}", "then": "publish", "correction_note": "Headline updated."}
        form.update({k: v for k, v in c["sections"].items()})
        r = self.post(op, f"/story/{sid}/edit", form)
        if "/site/" in r.url:
            self.assertIn(c["headline"] + " today", r.read().decode())
        else:  # mock drafts can carry guardrail errors; then it must save but not publish
            self.assertIn("Not approved", urllib.parse.unquote(r.url))

        # block editor save (what the browser sends) as admin: save, approve and update live
        blocks = [{"type": "header", "data": {"text": "What changed", "level": 2}},
                  {"type": "paragraph", "data": {"text": "Edited <b>in blocks</b> <script>x</script>"}},
                  {"type": "table", "data": {"withHeadings": True, "content": [["A", "B"], ["1", "2"]]}}]
        r = self.post(op, f"/story/{sid}/edit", {"headline": "Block headline", "dek": "Block dek", "blocks": json.dumps(blocks),
                                                 "meta_description": "m", "note": "blocks", "then": "save", "next": f"/site/{slug}"})
        self.assertIn(f"/story/{sid}", r.url)
        page = r.read().decode()
        self.assertIn('<article class="blocks">', page)
        self.assertIn("Edited <b>in blocks</b>", page)
        self.assertNotIn("<script>x", page)
        js = op.open(self.base + "/static/editor.js")
        self.assertIn("javascript", js.headers["Content-Type"])
        self.assertIn("FH_EDITOR", js.read().decode())

        # unpublish from the site page returns to the site page, which still shows the desk tools
        r = self.post(op, f"/story/{sid}/action", {"action": "unpublish", "next": f"/site/{slug}", "note": "test"})
        self.assertIn(f"/site/{slug}", r.url)
        page = r.read().decode()
        self.assertIn("Unpublished: readers can", page)
        self.assertIn("Edit story", page)            # the correction in review carries on

        # an off-site "next" is ignored
        r = self.post(op, f"/story/{sid}/action", {"action": "reopen", "next": "https://evil.example/x"})
        self.assertTrue(urllib.parse.urlsplit(r.url).path == f"/story/{sid}")

    def test_production_proxy_headers(self):
        """Behind nginx + Cloudflare: real visitor IPs for the sign-in limit, never indexed."""
        op, _ = self.client()
        r = op.open(self.base + "/login")
        self.assertEqual(r.headers["X-Robots-Tag"], "noindex, nofollow")
        os.environ["DESK_TRUST_PROXY"] = "1"
        self.addCleanup(os.environ.pop, "DESK_TRUST_PROXY", None)
        for i in range(5):   # one visitor fails five times...
            self.post(op, "/login", {"email": f"nobody{i}@example.com", "password": "wrong-password-1"},
                      headers={"CF-Connecting-IP": "203.0.113.5"})
        r = self.post(op, "/login", {"email": "boss@example.com", "password": "Boss-password-2026"},
                      headers={"CF-Connecting-IP": "203.0.113.5"})
        self.assertIn("Too many attempts", r.read().decode())
        op2, _ = self.client()   # ...and someone else behind the same proxy can still sign in
        r = self.post(op2, "/login", {"email": "boss@example.com", "password": "Boss-password-2026"},
                      headers={"CF-Connecting-IP": "198.51.100.7"})
        self.assertTrue(r.url.endswith("/"))
        r = self.post(op2, "/login", {"email": "boss@example.com", "password": "x"}, headers={"CF-Connecting-IP": "not-an-ip"})
        self.assertIn("incorrect", r.read().decode())   # junk header falls back to the real peer address

    def test_category_editor_pages(self):
        from newsroom import auth
        from pipeline import cluster, desk, feeds, writer
        from pipeline.db import connect
        from pipeline.llm import MockClient
        db = connect("newsroom")
        if not db.fetchone("select id from newsroom.stories limit 1"):
            feeds.ingest(db); cluster.cluster(db); desk.evaluate(db, MockClient())
            writer.write(db, MockClient(), fetch_text=False)
        fin_id = db.fetchone("select id from newsroom.stories where category = 'finance' order by id limit 1")["id"]
        if not db.fetchone("select id from newsroom.users where email = 'ent.ed@example.com'"):
            auth.create_user(db, None, email="ent.ed@example.com", display_name="Ena Ent", role="editor",
                             categories=["entertainment"], password="Ent-editor-pass-12")
        db.close()
        op, _ = self.client()
        self.post(op, "/login", {"email": "ent.ed@example.com", "password": "Ent-editor-pass-12"})
        page = op.open(self.base + f"/story/{fin_id}").read().decode()
        self.assertIn("Not your category", page.split("<title>")[1])
        self.assertNotIn(f'href="/story/{fin_id}"', op.open(self.base + "/?q=editor").read().decode())
        self.assertIn("No stories match", op.open(self.base + "/scoring").read().decode())
        # admin sees the category everywhere and can move the story
        op2, _ = self.client()
        self.post(op2, "/login", {"email": "boss@example.com", "password": "Boss-password-2026"})
        page = op2.open(self.base + f"/story/{fin_id}").read().decode()
        self.assertIn('<span class="pill cat">Finance</span>', page)
        self.assertIn("Move to another category", page)
        team = op2.open(self.base + "/team").read().decode()
        self.assertIn('name="cat_sports"', team)
        src = op2.open(self.base + "/sources").read().decode()
        self.assertIn('<select name="category">', src)


if __name__ == "__main__":
    unittest.main()
