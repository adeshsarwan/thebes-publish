"""Offline tests:  python -m unittest discover -s tests -v

By default every test runs on SQLite (three attached files stand in for the
pipeline / newsroom / site schemas). To run the same tests on PostgreSQL:

    TEST_PG_URL=postgresql://localhost/postgres python -m unittest discover -s tests -v

Each test then gets its own throwaway database, built with the real schema and
role grants, and the role-permission tests run too. Feeds are local fixtures and
the LLM is the deterministic MockClient, so no network or API key is needed.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures import feeds_yaml, write_fixtures  # noqa: E402

PG_URL = os.getenv("TEST_PG_URL")


def _with_db(url: str, dbname: str, user: str | None = None) -> str:
    p = urlsplit(url)
    netloc = p.netloc
    if user:
        host = p.hostname + (f":{p.port}" if p.port else "")
        netloc = f"{user}@{host}"
    return urlunsplit((p.scheme, netloc, f"/{dbname}", "", ""))


def _has_psycopg() -> bool:
    try:
        import psycopg  # noqa: F401
        return True
    except ImportError:
        return False


def pg_database(url: str, role: str | None = None):
    """Connect to a test database; with `role`, act as that role via SET ROLE (no passwords needed)."""
    from pipeline.db import Database
    if _has_psycopg():
        if not role:
            return Database(url)
        import psycopg
        from psycopg.rows import dict_row
        from psycopg.types.json import Jsonb
        conn = psycopg.connect(url, row_factory=dict_row, autocommit=True)
        conn.execute(f"set role {role}")
        return Database(url, connection=conn, jsonb=Jsonb, translate=True)
    from pg_shim import JsonParam, PsqlConnection
    return Database(url, connection=PsqlConnection(url, f"set role {role};" if role else ""), jsonb=JsonParam)


def server_exec(sql: str) -> None:
    """CREATE/DROP DATABASE can't run inside a transaction, so use autocommit."""
    if _has_psycopg():
        import psycopg
        with psycopg.connect(PG_URL, autocommit=True) as conn:
            conn.execute(sql)
    else:
        from pg_shim import PsqlConnection
        PsqlConnection(PG_URL).execute(sql)


class Base(unittest.TestCase):
    tmp: Path

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="finhub-"))
        cfg = cls.tmp / "config"
        shutil.copytree(ROOT / "config", cfg)
        files = write_fixtures(cls.tmp)
        (cfg / "feeds.yaml").write_text(feeds_yaml(files))
        os.environ["FINANCE_CONFIG_DIR"] = str(cfg)
        os.environ["DATABASE_URL"] = f"sqlite:///{cls.tmp / 'default.db'}"
        # Never touch the real database from .env: blank values win over .env.
        for key in ("PIPELINE_DATABASE_URL", "NEWSROOM_DATABASE_URL", "SITE_DATABASE_URL", "ADMIN_DATABASE_URL"):
            os.environ[key] = ""
        os.environ["LLM_PROVIDER"] = "mock"
        os.environ["APP_ENV"] = "local"
        os.environ["FINANCE_DATA_DIR"] = str(cls.tmp / "data")
        for mod in [m for m in sys.modules if m.startswith(("pipeline", "newsroom"))]:
            del sys.modules[mod]
        from pipeline.settings import get_settings
        get_settings.cache_clear()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---- databases -------------------------------------------------------
    def open_db(self, name: str):
        from pipeline.db import Database
        if not PG_URL:
            db = Database(f"sqlite:///{self.tmp / name}.db")
            db.init_schema()
            self.addCleanup(db.close)
            return db
        from pipeline import dbsetup
        dbname = f"fh_test_{name}_{os.getpid()}"
        server_exec(f"drop database if exists {dbname} with (force)")
        server_exec(f"create database {dbname}")
        self.pg_dbname = dbname
        db = pg_database(_with_db(PG_URL, dbname))
        # roles are server-wide: never change the passwords of real ones from a test
        dbsetup.apply(db, {r: "TestPassw0rdTestPassw0rd" for r in dbsetup.ROLES}, update_passwords=False)
        self.addCleanup(self._drop_pg, dbname, db)
        return db

    def _drop_pg(self, dbname, db):
        db.close()
        server_exec(f"drop database if exists {dbname} with (force)")

    def prepared(self, name: str):
        """A database with pipeline output (AI drafts) and placeholder users."""
        from newsroom.users import seed_placeholders
        from pipeline import cluster, desk, feeds, writer
        from pipeline.llm import MockClient
        db = self.open_db(name)
        feeds.ingest(db); cluster.cluster(db); desk.evaluate(db, MockClient())
        writer.write(db, MockClient(), fetch_text=False)
        seed_placeholders(db)
        return db

    def user(self, db, first_name: str) -> dict:
        from newsroom.users import find_user
        return find_user(db, first_name)


# =========================================================================
class PipelineTest(Base):
    def test_parse_dates(self):
        from pipeline.feeds import parse_date
        self.assertEqual(parse_date("Tue, 13 Oct 2026 16:15:00 +0000").day, 13)
        self.assertEqual(parse_date("2026-10-09T15:00:58Z").hour, 15)
        self.assertEqual(parse_date("Tuesday, October 13, 2026 - 16:15").hour, 16)
        self.assertIsNone(parse_date("not a date"))

    def test_canonical_url_strips_tracking(self):
        from pipeline.text import canonical_url
        self.assertEqual(canonical_url("https://X.com/a/?utm_source=rss&id=3#frag"), "https://x.com/a?id=3")

    def test_topic_tagging_is_regional(self):
        from pipeline.taxonomy import tag_topics
        self.assertIn("savings_investing", tag_topics("UK", "ISA allowance changes"))
        self.assertIn("interest_rates", tag_topics("AU", "RBA leaves the cash rate unchanged"))
        self.assertNotIn("savings_investing", tag_topics("US", "ISA allowance changes"))

    def test_guardrails(self):
        from pipeline import guardrails
        from pipeline.llm import MockClient
        src = "[1] title: Bank of Canada maintains the policy rate at 2¼%\nsummary: The Bank held the overnight rate at 2¼%."
        prompt = f"SOURCE MATERIAL\n{src}\nEND SOURCE MATERIAL"
        story = MockClient().generate_json(model="m", system="", prompt=prompt, schema={}, task="write")
        self.assertEqual(guardrails.check(story, src, "CA").errors, [])

        copied = dict(story, what_happened="The Bank held the overnight rate at 2¼%. " + src)
        self.assertIn("verbatim_run", {i["code"] for i in guardrails.check(copied, src, "CA").errors})
        invented = dict(story, money_impact=story["money_impact"] + " Mortgage rates will fall to 3.9%.")
        rep = guardrails.check(invented, src, "CA")
        self.assertIn("3.9%", rep.metrics["unsupported_numbers"])
        banned = dict(story, dek="This is free money for savers.")
        self.assertIn("banned_terms", {i["code"] for i in guardrails.check(banned, src, "CA").errors})
        padded = dict(story, money_impact=" ".join(["Rates affect household budgets in many different ways."] * 8))
        self.assertIn("repetition", {i["code"] for i in guardrails.check(padded, src, "CA").errors})
        foreign = dict(story, who_is_affected=story["who_is_affected"] + " Check your ISA and 401(k).")
        self.assertIn("foreign_terms", {i["code"] for i in guardrails.check(foreign, src, "CA").issues})

    def test_regenerates_with_feedback(self):
        from pipeline import cluster, desk, feeds, writer
        from pipeline.llm import MockClient

        class Flaky(MockClient):
            def __init__(self):
                self.prompts = []

            def generate_json(self, **kw):
                self.prompts.append(kw["prompt"])
                out = super().generate_json(**kw)
                if kw["task"] == "write" and len(self.prompts) == 1:
                    out["dek"] = "Guaranteed payout for savers: this is free money."
                return out

        db = self.open_db("retry")
        feeds.ingest(db, only_source="ca-boc-press"); cluster.cluster(db); desk.evaluate(db, MockClient())
        row = desk.commission(db)[0]
        client = Flaky()
        result = writer.write_one(db, client, row, fetch_text=False)
        self.assertEqual(result["attempts"], 2)
        self.assertTrue(result["guardrails_passed"])
        self.assertIn("YOUR PREVIOUS DRAFT FAILED REVIEW", client.prompts[1])
        self.assertIn("free money", client.prompts[1])

    def test_end_to_end_creates_newsroom_drafts(self):
        from pipeline import cluster, desk, feeds, writer
        from pipeline.llm import MockClient

        db = self.open_db("e2e")
        s1 = feeds.ingest(db)
        self.assertEqual(s1["errors"], [])
        self.assertEqual(s1["filtered"], 2, s1)                 # car-warranty review + bank note story
        s2 = feeds.ingest(db)
        self.assertEqual(s2["new"], 0)
        self.assertEqual(s2["duplicates"], s1["new"] + s1["filtered"])
        fca = db.fetchone("select published_at from pipeline.raw_items where source_id = 'uk-fca-news'")
        self.assertIsNotNone(fca["published_at"])

        c = cluster.cluster(db)
        self.assertGreaterEqual(c["joined"], 1, c)              # Fed + CFPB report the same decision
        fed = db.fetchone(
            """select c.item_count from pipeline.story_clusters c join pipeline.raw_items r on r.cluster_id = c.id
               where r.source_id = 'us-fed-press' and r.title like 'Federal Reserve issues%'""")
        self.assertEqual(fed["item_count"], 2)

        e = desk.evaluate(db, MockClient())
        self.assertGreater(e["run"], 0)
        self.assertGreater(e["reject"], 0)

        w = writer.write(db, MockClient(), fetch_text=False)
        self.assertEqual(w["errors"], 0, w)
        self.assertEqual(w["drafted"], e["run"])
        story = db.fetchone("select * from newsroom.stories order by id limit 1")
        self.assertEqual(story["status"], "ai_draft")
        self.assertIsNotNone(story["impact_score"])
        rev = db.fetchone("select * from newsroom.story_revisions where story_id = ?", (story["id"],))
        self.assertEqual((rev["version"], rev["author_kind"]), (1, "ai"))
        for heading in ("## What happened", "## What it means for your money", "## Who is affected",
                        "## The numbers", "## What to do now", "## Key takeaways", "## Sources"):
            self.assertIn(heading, rev["body_md"])
        self.assertTrue(all(s["url"].startswith("http") for s in db.unj(rev["sources"])))
        ev = db.fetchone("select * from newsroom.story_events where story_id = ?", (story["id"],))
        self.assertEqual((ev["action"], ev["actor_id"]), ("drafted", None))
        self.assertEqual(db.fetchone("select count(*) as n from site.published_stories")["n"], 0)
        self.assertEqual(writer.write(db, MockClient(), fetch_text=False)["drafted"], 0)   # never twice


# =========================================================================
class WorkflowTest(Base):
    def test_editor_approves_ai_draft_and_publisher_publishes(self):
        from newsroom import workflow as wf
        db = self.prepared("approve")
        maya, sara, daniel = self.user(db, "maya"), self.user(db, "sara"), self.user(db, "daniel")
        sid = db.fetchone("select id from newsroom.stories order by id limit 1")["id"]

        with self.assertRaisesRegex(wf.WorkflowError, "copywriter can't approve"):
            wf.perform(db, sid, "approve", daniel)
        with self.assertRaisesRegex(wf.WorkflowError, "publisher can't approve"):
            wf.perform(db, sid, "approve", sara)
        with self.assertRaisesRegex(wf.WorkflowError, "can't publish a story that is AI draft"):
            wf.perform(db, sid, "publish", sara)

        s = wf.perform(db, sid, "approve", maya, note="Clean draft")
        self.assertEqual((s["status"], s["editor_id"]), ("approved", maya["id"]))
        with self.assertRaisesRegex(wf.WorkflowError, "editor can't publish"):
            wf.perform(db, sid, "publish", maya)
        s = wf.perform(db, sid, "publish", sara)
        self.assertEqual((s["status"], s["published_version"]), ("published", 1))

        live = db.fetchone("select * from site.published_stories where story_id = ?", (sid,))
        self.assertEqual((live["status"], live["version"], live["author_id"]), ("live", 1, maya["id"]))
        self.assertIn("reviewed and edited by Maya Fernandes, Senior Personal Finance Editor", live["disclosure"])
        self.assertTrue(live["ai_assisted"])
        self.assertEqual(db.unj(live["sections"]).keys(),
                         {"what_happened", "money_impact", "who_is_affected", "illustrative_example"})
        author = db.fetchone("select * from site.authors where id = ?", (maya["id"],))
        self.assertEqual(author["display_name"], "Maya Fernandes")

        actions = [r["action"] for r in db.fetchall(
            "select action from newsroom.story_events where story_id = ? order by id", (sid,))]
        self.assertEqual(actions, ["drafted", "approve", "publish"])

    def test_copywriter_path_and_permissions(self):
        from newsroom import workflow as wf
        from newsroom.store import get_story, revision_content
        db = self.prepared("copy")
        maya, daniel, sara = self.user(db, "maya"), self.user(db, "daniel"), self.user(db, "sara")
        sid = db.fetchone("select id from newsroom.stories order by id limit 1")["id"]
        content = revision_content(get_story(db, sid)["revision"])

        self.assertTrue(wf.can_edit(get_story(db, sid), daniel))        # copy editors can pick up any draft
        with self.assertRaisesRegex(wf.WorkflowError, "must be an active copywriter"):
            wf.perform(db, sid, "assign", maya, assignee_id=sara["id"])
        s = wf.perform(db, sid, "assign", maya, assignee_id=daniel["id"], note="Needs a sharper top")
        self.assertEqual((s["status"], s["assigned_to"]), ("in_copy", daniel["id"]))
        self.assertTrue(wf.can_edit(s, daniel))
        self.assertFalse(wf.can_edit(s, sara))

        edited = dict(content, headline="A sharper headline about your monthly bills")
        report = wf.save_revision(db, sid, daniel, edited, note="New top")
        self.assertIn("passed", report)
        s = get_story(db, sid)
        self.assertEqual((s["current_version"], s["revision"]["author_kind"], s["revision"]["headline"]),
                         (2, "human", "A sharper headline about your monthly bills"))

        s = wf.perform(db, sid, "submit", daniel)
        self.assertEqual(s["status"], "in_review")
        self.assertTrue(wf.can_edit(s, daniel))                         # copy editors can still make changes in review
        s = wf.perform(db, sid, "send_back", maya, note="One more pass")
        self.assertEqual(s["status"], "in_copy")
        wf.perform(db, sid, "submit", daniel)
        s = wf.perform(db, sid, "approve", maya)
        self.assertEqual(s["status"], "approved")

        # an edit after approval resets it
        wf.save_revision(db, sid, maya, dict(edited, dek="Tightened standfirst."), note="tweak")
        s = get_story(db, sid)
        self.assertEqual((s["status"], s["editor_id"], s["current_version"]), ("in_review", None, 3))
        with self.assertRaises(wf.WorkflowError):
            wf.perform(db, sid, "publish", sara)
        wf.perform(db, sid, "approve", maya)
        wf.perform(db, sid, "publish", sara)
        live = db.fetchone("select * from site.published_stories where story_id = ?", (sid,))
        self.assertEqual((live["version"], live["headline"]), (3, "A sharper headline about your monthly bills"))

    def test_guardrail_errors_need_an_override(self):
        from newsroom import workflow as wf
        from newsroom.store import get_story, revision_content
        db = self.prepared("override")
        maya = self.user(db, "maya")
        sid = db.fetchone("select id from newsroom.stories order by id limit 1")["id"]
        content = revision_content(get_story(db, sid)["revision"])
        bad = dict(content, dek="Free money for every saver, guaranteed payout.")
        report = wf.save_revision(db, sid, maya, bad)
        self.assertFalse(report["passed"])
        with self.assertRaisesRegex(wf.WorkflowError, "guardrail error"):
            wf.perform(db, sid, "approve", maya)
        with self.assertRaisesRegex(wf.WorkflowError, "needs a reason"):
            wf.perform(db, sid, "approve", maya, override=True)
        s = wf.perform(db, sid, "approve", maya, override=True, note="Quoting a scam ad verbatim on purpose")
        self.assertEqual(s["status"], "approved")
        ev = db.fetchone("select note from newsroom.story_events where story_id = ? and action = 'approve'", (sid,))
        self.assertIn("Override of", ev["note"])

    def test_corrections_republish_and_unpublish(self):
        from newsroom import workflow as wf
        from newsroom.store import get_story, revision_content
        db = self.prepared("republish")
        maya, sara = self.user(db, "maya"), self.user(db, "sara")
        sid = db.fetchone("select id from newsroom.stories order by id limit 1")["id"]
        wf.perform(db, sid, "approve", maya)
        wf.perform(db, sid, "publish", sara)
        first = db.fetchone("select * from site.published_stories where story_id = ?", (sid,))

        self.assertTrue(wf.can_edit(get_story(db, sid), maya))           # live stories can be corrected directly
        wf.perform(db, sid, "reopen", maya, note="Fix a figure")
        content = revision_content(get_story(db, sid)["revision"])
        wf.save_revision(db, sid, maya, dict(content, headline=content["headline"] + " (updated)"))
        # still live at v1 while the correction is in progress
        self.assertEqual(db.fetchone("select version from site.published_stories where story_id = ?", (sid,))["version"], 1)
        wf.perform(db, sid, "approve", maya)
        wf.perform(db, sid, "publish", sara, correction_note="Corrected the headline.")
        second = db.fetchone("select * from site.published_stories where story_id = ?", (sid,))
        self.assertEqual(second["version"], 2)
        self.assertEqual(second["first_published_at"], first["first_published_at"])
        self.assertEqual(second["correction_note"], "Corrected the headline.")
        self.assertEqual(db.fetchone("select count(*) as n from site.published_stories")["n"], 1)

        wf.perform(db, sid, "unpublish", sara, note="Legal asked us to pull it")
        self.assertEqual(db.fetchone("select status from site.published_stories where story_id = ?", (sid,))["status"], "retracted")
        self.assertEqual(get_story(db, sid)["status"], "unpublished")

    def test_production_refuses_placeholder_byline(self):
        from newsroom import workflow as wf
        from pipeline.settings import get_settings
        db = self.prepared("prod")
        maya, sara = self.user(db, "maya"), self.user(db, "sara")
        sid = db.fetchone("select id from newsroom.stories order by id limit 1")["id"]
        wf.perform(db, sid, "approve", maya)
        os.environ["APP_ENV"] = "production"
        get_settings.cache_clear()
        try:
            with self.assertRaisesRegex(wf.WorkflowError, "placeholder"):
                wf.perform(db, sid, "publish", sara)
        finally:
            os.environ["APP_ENV"] = "local"
            get_settings.cache_clear()
        db.rollback()
        self.assertEqual(db.fetchone("select count(*) as n from site.published_stories")["n"], 0)

    def test_web_pages_render(self):
        from newsroom import web, workflow as wf
        db = self.prepared("web")
        maya, sara = self.user(db, "maya"), self.user(db, "sara")
        sid = db.fetchone("select id from newsroom.stories order by id limit 1")["id"]

        html = web.render_markdown("# T\n\n*dek*\n\n## H\n\n1. a\n2. b\n\n- [x](https://e.com) <b>")
        self.assertIn('<a href="https://e.com"', html)
        self.assertIn("&lt;b&gt;", html)

        self.assertIn("Needs an editor", web.queue_page(db, maya, {})[1])
        page = web.story_page(db, sid, maya)[1]
        self.assertIn("Approve", page)
        self.assertIn("Assign to copy editor", page)
        self.assertNotIn(">Publish<", page)
        self.assertNotIn("Approve", web.story_page(db, sid, sara)[1].split("Story</h3>")[0])
        self.assertIn('id="ed-blocks"', web.edit_page(db, sid, maya)[1])
        self.assertIn("Audit trail", web.history_page(db, sid)[1])
        self.assertIn("Scoring desk", web.scoring_page(db, {})[1])
        self.assertIn("Placeholder", web.team_page(db, self.user(db, "desk"))[1])
        self.assertIsNone(web.story_page(db, 9999, maya))

        wf.perform(db, sid, "approve", maya)
        wf.perform(db, sid, "publish", sara)
        slug = db.fetchone("select slug from site.published_stories")["slug"]
        self.assertIn(slug, web.site_feed_page(db)[1])
        art = web.site_feed_page(db, slug)[1]
        self.assertIn("By <b>Maya Fernandes</b>", art)
        self.assertIn("reviewed and edited by Maya Fernandes", art)


# =========================================================================
@unittest.skipUnless(PG_URL, "set TEST_PG_URL to run Postgres role tests")
class PostgresRolesTest(Base):
    def test_each_system_can_only_touch_its_own_data(self):
        from newsroom import workflow as wf
        db = self.prepared("roles")
        sid = db.fetchone("select id from newsroom.stories order by id limit 1")["id"]
        wf.perform(db, sid, "approve", self.user(db, "maya"))
        wf.perform(db, sid, "publish", self.user(db, "sara"))

        def as_role(role):
            d = pg_database(_with_db(PG_URL, self.pg_dbname), role=role)
            self.addCleanup(d.close)
            return d

        site, pipe, news = as_role("fh_site"), as_role("fh_pipeline"), as_role("fh_newsroom")
        # site: read published stories, nothing else
        self.assertEqual(len(site.fetchall("select * from site.published_stories")), 1)
        for sql in ("select * from newsroom.stories", "select * from pipeline.raw_items",
                    "update site.published_stories set headline = 'x'", "delete from site.authors"):
            with self.assertRaises(Exception, msg=sql):
                site.execute(sql)
        # pipeline: own schema + create drafts, but cannot publish or touch staff
        self.assertGreater(len(pipe.fetchall("select id from pipeline.raw_items")), 0)
        for sql in ("select * from site.published_stories", "select * from newsroom.users",
                    "update newsroom.stories set status = 'published'", "insert into site.authors (id, slug, display_name) values (99,'x','x')"):
            with self.assertRaises(Exception, msg=sql):
                pipe.execute(sql)
        # newsroom: reads pipeline, cannot change it
        self.assertGreater(len(news.fetchall("select id from pipeline.evaluations")), 0)
        for sql in ("delete from pipeline.raw_items", "update pipeline.evaluations set decision = 'run'"):
            with self.assertRaises(Exception, msg=sql):
                news.execute(sql)


if __name__ == "__main__":
    unittest.main()
