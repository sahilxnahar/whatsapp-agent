"""Milestone 0: SQLite storage, migration, persisted sessions, message log, hot reload, cost routing."""
import json
import tempfile
import unittest
from pathlib import Path

from test_offline import (NAME, PHONE, Base, resp, tap, text_block, text_msg, tool_block)  # noqa: F401

import brain
import config
import db
import flows
import store


class Storage(Base):
    def test_migrate_v1_json_files(self):
        src = Path(tempfile.mkdtemp())
        (src / "contacts.json").write_text(json.dumps({PHONE: {"name": "Old", "last_inbound": "2026-10-20T10:00:00+05:30"}}))
        (src / "chats.json").write_text(json.dumps({PHONE: {"fingerprint": "x", "messages": [{"role": "user", "content": "hi"}]}}))
        (src / "bookings.json").write_text(json.dumps({"holds": {}, "bookings": {"YB-AAAA": {
            "ticket": "YB-AAAA", "phone": PHONE, "status": "confirmed", "start": "2026-11-01T10:00:00+05:30",
            "end": "2026-11-01T10:15:00+05:30"}}}))
        (src / "leads.jsonl").write_text(json.dumps({"ts": "2026-10-20T10:00:00+05:30", "phone": PHONE}) + "\n")
        moved = db.migrate_from_json(src)
        self.assertEqual(len(moved), 4)
        self.assertEqual(store.get_contact(PHONE)["name"], "Old")
        self.assertEqual(store.get_chat(PHONE)["messages"][0]["content"], "hi")
        self.assertEqual(store.read_jsonl("leads.jsonl")[0]["phone"], PHONE)
        self.assertTrue((src / "contacts.json.migrated").exists())  # kept, not deleted

    def test_flow_session_survives_restart(self):
        self.send(text_msg("hi"))
        self.send(tap("menu:waitlist"))
        flows._sessions.clear()  # simulate a process restart
        self.send(text_msg("rahul@example.com"))
        self.assertEqual(store.read_jsonl("waitlist.jsonl")[0]["email"], "rahul@example.com")

    def test_message_log_records_both_directions(self):
        self.send(text_msg("hi"))
        log = store.conversation(PHONE)
        self.assertEqual(log[0]["direction"], "in")
        self.assertEqual(log[0]["body"], "hi")
        self.assertTrue(any(m["direction"] == "out" and m["kind"] == "list" for m in log))
        self.assertEqual(store.threads()[0]["phone"], PHONE)


class HotReload(Base):
    def test_knowledge_edit_rebuilds_prompt(self):
        path = config.ROOT / "knowledge.md"
        original = path.read_text(encoding="utf-8")
        try:
            path.write_text(original + "\nSECRET-TEST-FACT-42\n", encoding="utf-8")
            brain.refresh()
            self.assertIn("SECRET-TEST-FACT-42", brain.SYSTEM)
        finally:
            path.write_text(original, encoding="utf-8")
            brain.refresh()
        self.assertNotIn("SECRET-TEST-FACT-42", brain.SYSTEM)


class Routing(Base):
    def setUp(self):
        super().setUp()
        config.ROUTING = "auto"
        store.touch_inbound(PHONE, NAME)

    def tearDown(self):
        config.ROUTING = "off"

    def test_simple_question_stays_on_fast_model(self):
        fake = self.use_claude([resp([text_block("We're open 10 to 7.")])])
        self.send(text_msg("what are your hours?"))
        self.assertEqual([c["model"] for c in fake.calls], [config.CLAUDE_MODEL_FAST])
        self.assertIn("ESCALATE", fake.calls[0]["system"][0]["text"])
        self.assertNotIn("fallbacks", fake.calls[0])  # Haiku has no server-side fallback

    def test_escalation_reruns_on_main_model_and_sticks(self):
        fake = self.use_claude([resp([text_block("ESCALATE")]), resp([text_block("Happy to help with a quote!")]),
                                resp([text_block("Sure.")])])
        self.send(text_msg("can you do a custom deal for 20 videos?"))
        self.assertEqual([c["model"] for c in fake.calls], [config.CLAUDE_MODEL_FAST, config.CLAUDE_MODEL])
        self.assertNotIn("ESCALATE", fake.calls[1]["system"][0]["text"])
        body = self.sender.last("interactive")["interactive"]["body"]["text"]
        self.assertEqual(body, "Happy to help with a quote!")
        self.send(text_msg("thanks"))
        self.assertEqual(fake.calls[2]["model"], config.CLAUDE_MODEL)  # stays escalated

    def test_tool_call_on_fast_model_escalates_before_running(self):
        fake = self.use_claude([
            resp([tool_block("offer_call_slots", {"call_type": "intro", "date": "", "part_of_day": "any"})],
                 stop="tool_use"),
            resp([tool_block("offer_call_slots", {"call_type": "intro", "date": "", "part_of_day": "any"})],
                 stop="tool_use"),
            resp([text_block("Tap a slot above 🙂")]),
        ])
        self.send(text_msg("call book karni hai"))
        self.assertEqual([c["model"] for c in fake.calls][:2], [config.CLAUDE_MODEL_FAST, config.CLAUDE_MODEL])
        slot_lists = [p for p in self.sender.outbox if p["type"] == "interactive"
                      and p["interactive"]["type"] == "list"]
        self.assertEqual(len(slot_lists), 1)  # the tool ran once, on the main model only


if __name__ == "__main__":
    unittest.main()
