"""The AI assistant in the story editor."""
from __future__ import annotations

import json
import unittest
import urllib.error
import urllib.parse
from unittest import mock

from test_pipeline import Base
import test_newsroom_features as tnf


class AssistantTest(Base):
    def _story(self, db):
        return db.fetchone("select id from newsroom.stories where status = 'ai_draft' order by id limit 1")["id"]

    def test_markdown_to_blocks(self):
        from newsroom.assistant import markdown_to_blocks, split_reply
        b = markdown_to_blocks("## New section\n\nFirst **bold** line\ncontinues.\n\n- one\n- two\n\n1. step\n\n> A quote")
        self.assertEqual([x["type"] for x in b], ["header", "paragraph", "list", "list", "quote"])
        self.assertEqual(b[0]["data"], {"text": "New section", "level": 2})
        self.assertEqual(b[1]["data"]["text"], "First <b>bold</b> line continues.")
        self.assertEqual((b[2]["data"]["style"], b[2]["data"]["items"]), ("unordered", ["one", "two"]))
        self.assertEqual(b[3]["data"]["style"], "ordered")
        self.assertEqual(split_reply("Answer here.\n=== TEXT TO ADD ===\nNew text"), ("Answer here.", "New text"))
        self.assertEqual(split_reply("Just an answer"), ("Just an answer", ""))
        self.assertEqual(markdown_to_blocks("<script>x</script> hi")[0]["data"]["text"], "&lt;script&gt;x&lt;/script&gt; hi")

    def test_ask_research_insert_and_checks(self):
        from newsroom import assistant, web, workflow as wf
        from newsroom.store import editor_blocks, get_story
        from pipeline.llm import MockClient
        db = self.prepared("assistant")
        maya, sara = self.user(db, "maya"), self.user(db, "sara")
        sid = self._story(db)
        story = get_story(db, sid)

        page = web.edit_page(db, sid, maya)[1]
        self.assertIn('id="ai-panel"', page)
        self.assertIn("/static/assistant.js", page)

        with self.assertRaisesRegex(assistant.AssistantError, "can't edit"):
            assistant.ask(db, sara, story, "Find the latest figures")       # publisher: can't edit
        with self.assertRaisesRegex(assistant.AssistantError, "Type what"):
            assistant.ask(db, maya, story, " ")

        on_screen = {"headline": "Energy bills to rise", "dek": "", "blocks": [
            {"type": "paragraph", "data": {"text": "Bills go up in <b>January</b>."}},
            {"type": "list", "data": {"style": "ordered", "items": ["Check your tariff", "Compare deals"]}}]}
        md = assistant.story_text(on_screen)
        self.assertIn("Bills go up in **January**.", md)
        self.assertIn("1. Check your tariff\n2. Compare deals", md)

        cid = assistant.ask(db, maya, story, "Add a paragraph on prepayment customers", on_screen)
        with self.assertRaisesRegex(assistant.AssistantError, "still answering"):
            assistant.ask(db, maya, story, "Another question")
        self.assertEqual(assistant.messages(db, sid)[-1]["status"], "queued")

        out = assistant.run(cid, MockClient(), resolve_links=False, db=db)
        self.assertIn("4.2%", out["reply"])
        self.assertEqual(out["blocks"][0], {"type": "header", "data": {"text": "What it means for prepayment customers", "level": 2}})
        self.assertEqual(out["sources"][0]["url"], "https://example-regulator.gov/price-cap")
        m = assistant.messages(db, sid)[-1]
        self.assertEqual(m["status"], "done")
        self.assertIn("<li>", m["reply_html"])
        self.assertEqual(m["blocks"], out["blocks"])

        # a question without web search: no sources, no text to add
        cid2 = assistant.ask(db, maya, story, "Is the intro clear?", on_screen, research=False)
        out2 = assistant.run(cid2, MockClient(), db=db)
        self.assertEqual((out2["sources"], out2["blocks"]), ([], []))
        hist = db.fetchone("select story_state from newsroom.story_chats where id = ?", (cid2,))
        self.assertEqual(db.unj(hist["story_state"])["headline"], "Energy bills to rise")

        # the researched figures count as sourced when the editor inserts the text and saves
        content = {"headline": story["revision"]["headline"], "dek": story["revision"].get("dek"),
                   "blocks": editor_blocks(story["revision"], story.get("category")) + out["blocks"],
                   "sources": story["revision"]["sources"] + out["sources"]}
        rep = wf.check_content(db, get_story(db, sid), content)
        flagged = next((i["detail"] for i in rep["issues"] if i["code"] == "unsupported_numbers"), "")
        self.assertNotIn("4.2%", flagged)
        self.assertNotIn("verbatim_run", {i["code"] for i in rep["issues"]})
        db.execute("delete from newsroom.story_chats where story_id = ?", (sid,))
        db.commit()
        rep = wf.check_content(db, get_story(db, sid), content)
        flagged = next((i["detail"] for i in rep["issues"] if i["code"] == "unsupported_numbers"), "")
        self.assertIn("4.2%", flagged)

    def test_failures_and_stale_requests(self):
        from newsroom import assistant
        from newsroom.store import get_story
        from pipeline.llm import LLMError, MockClient
        db = self.prepared("assistant_fail")
        maya = self.user(db, "maya")
        story = get_story(db, self._story(db))

        class Broken(MockClient):
            def research(self, **kw):
                raise LLMError("search unavailable")

        cid = assistant.ask(db, maya, story, "Find the figures")
        with self.assertRaises(LLMError):
            assistant.run(cid, Broken(), db=db)
        m = assistant.messages(db, story["id"])[-1]
        self.assertEqual(m["status"], "failed")
        self.assertIn("search unavailable", m["error"])

        cid = assistant.ask(db, maya, story, "Find the figures again")     # a failure doesn't block the next question
        db.execute("update newsroom.story_chats set created_at = ? where id = ?", ("2020-01-01T00:00:00+00:00", cid))
        db.commit()
        self.assertEqual(assistant.messages(db, story["id"])[-1]["status"], "failed")   # stuck: shown as failed
        self.assertTrue(assistant.ask(db, maya, story, "And again"))                      # and doesn't block


class AssistantHttpTest(Base):
    setUp, client, post = tnf.HttpTest.setUp, tnf.HttpTest.client, tnf.HttpTest.post

    def test_ask_over_http(self):
        from newsroom import assistant
        from newsroom.users import find_user
        from pipeline.db import connect
        from pipeline.llm import MockClient
        from newsroom.store import create_ai_draft
        db = connect("newsroom")
        maya = find_user(db, "maya")
        sid = create_ai_draft(db, cluster_id=None, country="UK", slug="uk-assistant-http-test",
                              content={"headline": "A story to edit", "dek": "Dek", "sections": {"what_happened": "Text."}},
                              guardrails={"passed": True, "issues": [], "metrics": {}}, model="mock", impact_score=None,
                              angle=None, attempts=1)
        db.commit(); db.close()
        op, _ = self.client()
        self.post(op, "/login", {"email": "boss@example.com", "password": "Boss-password-2026"})
        self.assertIn('id="ai-panel"', op.open(self.base + f"/story/{sid}/edit").read().decode())
        self.assertEqual(json.loads(op.open(self.base + f"/story/{sid}/assistant.json").read())["messages"], [])
        state = json.dumps({"headline": "A story to edit", "dek": "", "blocks": [{"type": "paragraph", "data": {"text": "Hi"}}]})
        with mock.patch.object(assistant, "start_background") as bg:
            r = self.post(op, f"/story/{sid}/assistant", {"question": "Write a closing paragraph", "research": "1", "state": state})
            d = json.loads(r.read())
            self.assertTrue(bg.called)
        self.assertEqual(d["messages"][-1]["status"], "queued")
        db = connect("newsroom")
        assistant.run(d["id"], MockClient(), resolve_links=False, db=db)
        db.close()
        msgs = json.loads(op.open(self.base + f"/story/{sid}/assistant.json").read())["messages"]
        self.assertEqual(msgs[-1]["status"], "done")
        self.assertTrue(msgs[-1]["blocks"])
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.post(op, f"/story/{sid}/assistant", {"question": "x"})
        self.assertEqual(cm.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
