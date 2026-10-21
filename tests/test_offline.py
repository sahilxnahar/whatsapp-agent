"""Offline tests: fake WhatsApp sender + fake Claude. Free to run, no network.

    python -m unittest discover -s tests -v
"""
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

TMP = tempfile.mkdtemp(prefix="wa-bot-test-")
os.environ["DATA_DIR"] = TMP
os.environ["PAYMENT_MODE"] = "demo"
os.environ["CLAUDE_MODEL"] = "claude-opus-5-5"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anthropic  # noqa: E402
import httpx2 as httpx  # noqa: E402 - the SDK's HTTP library

import bot  # noqa: E402
import brain  # noqa: E402
import config  # noqa: E402
import flows  # noqa: E402
import payments  # noqa: E402
import router  # noqa: E402
import slots  # noqa: E402
import store  # noqa: E402
from whatsapp import FakeSender, clip  # noqa: E402

PHONE, NAME = "919900000001", "Test User"


def text_msg(body, phone=PHONE):
    return {"from": phone, "id": f"wamid.{time.time_ns()}", "type": "text", "text": {"body": body}}


def tap(opt_id, phone=PHONE):
    return {"from": phone, "id": f"wamid.{time.time_ns()}", "type": "interactive",
            "interactive": {"list_reply": {"id": opt_id, "title": opt_id}}}


def options(sender):
    return bot.last_options(sender)


def usage():
    return SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0,
                           cache_creation_input_tokens=0)


def resp(blocks, stop="end_turn"):
    return SimpleNamespace(content=blocks, stop_reason=stop, stop_details=None, usage=usage(),
                           model=config.CLAUDE_MODEL)


def text_block(t):
    return SimpleNamespace(type="text", text=t)


def tool_block(name, args, tid="toolu_1"):
    return SimpleNamespace(type="tool_use", id=tid, name=name, input=args)


class FakeClaude:
    """Returns scripted responses (or raises scripted exceptions) in order."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(json.loads(json.dumps(kwargs, default=str)))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def bad_request(message):
    response = httpx.Response(400, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    return anthropic.BadRequestError(message, response=response, body={"error": {"message": message}})


class Base(unittest.TestCase):
    def setUp(self):
        for p in Path(TMP).glob("*.json*"):
            p.unlink()
        flows._sessions.clear()
        router._rate.clear()
        self.sender = FakeSender(echo=False)

    def send(self, msg, name=NAME):
        router.handle(self.sender, msg, name)

    def use_claude(self, script):
        fake = FakeClaude(script)
        brain._client = fake
        return fake


class WelcomeAndMenu(Base):
    def test_first_hi_sends_banner_and_menu(self):
        self.send(text_msg("hi"))
        types = [p["type"] for p in self.sender.outbox]
        self.assertEqual(types[:2], ["image", "interactive"])
        menu = self.sender.last("interactive")["interactive"]
        self.assertEqual(menu["type"], "list")
        self.assertIn("AI assistant", menu["body"]["text"])
        rows = menu["action"]["sections"][0]["rows"]
        self.assertLessEqual(len(rows), 10)
        for r in rows:
            self.assertLessEqual(len(r["title"]), 24)

    def test_limits_are_enforced(self):
        self.assertEqual(len(clip("x" * 40, 24)), 24)
        self.sender.buttons(PHONE, "b", [("a", "A" * 30), ("b", "B"), ("c", "C"), ("d", "D")])
        btns = self.sender.last()["interactive"]["action"]["buttons"]
        self.assertEqual(len(btns), 3)
        self.assertLessEqual(len(btns[0]["reply"]["title"]), 20)


class BriefFlow(Base):
    def test_brief_form_logs_lead(self):
        self.send(text_msg("hi"))
        self.send(tap("menu:brief"))
        self.assertEqual(self.sender.outbox[-2]["type"], "image")  # rate card
        self.send(tap(options(self.sender)[0][0]))   # service
        self.send(tap(options(self.sender)[1][0]))   # budget
        self.send(tap(options(self.sender)[0][0]))   # timeline
        self.send(text_msg("Rahul, Acme Foods, need 3 reels for Diwali"))
        leads = store.read_jsonl("leads.jsonl")
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]["source"], "form")
        self.assertIn("Acme", leads[0]["details"])
        self.assertIn("Book free call", [t for _, t in options(self.sender)])


class Booking(Base):
    def test_free_call_books_and_slot_disappears(self):
        self.send(text_msg("hi"))
        self.send(tap("book:type:intro"))
        first_slot = options(self.sender)[0][0]
        self.assertTrue(first_slot.startswith("slot:intro:"))
        self.send(tap(first_slot))
        bookings = store.read_json("bookings.json", {})["bookings"]
        self.assertEqual(len(bookings), 1)
        self.assertEqual(next(iter(bookings.values()))["paid_via"], "free")
        self.assertEqual(self.sender.outbox[-2]["type"], "image")  # ticket
        # Another customer no longer sees that slot.
        other = FakeSender(echo=False)
        router.handle(other, tap("book:type:intro", phone="919900000002"), "Other")
        self.assertNotIn(first_slot, [i for i, _ in bot.last_options(other)])

    def test_paid_call_demo_payment(self):
        self.send(text_msg("hi"))
        self.send(tap("book:type:consult"))
        self.send(tap(options(self.sender)[0][0]))
        img = self.sender.outbox[-2]
        self.assertEqual(img["type"], "image")
        self.assertIn("DEMO", img["image"]["caption"])
        pay_buttons = options(self.sender)
        self.assertEqual(pay_buttons[0][1], "I've paid")
        self.send(tap(pay_buttons[0][0]))
        booking = next(iter(store.read_json("bookings.json", {})["bookings"].values()))
        self.assertEqual(booking["paid_via"], "demo")
        self.assertEqual(booking["amount"], 999)

    def test_hold_blocks_others_then_expires(self):
        start = slots.free_slots("consult", limit=1)[0]
        hold = slots.create_hold(PHONE, NAME, "consult", start)
        self.assertIsNotNone(hold)
        self.assertNotIn(start, slots.free_slots("consult", limit=200))
        self.assertIsNone(slots.create_hold("919900000002", "B", "consult", start))
        past = (config.now() - timedelta(minutes=1)).isoformat()
        slots.update_hold(hold["id"], expires=past)
        self.assertEqual([h["id"] for h in slots.expire_holds()], [hold["id"]])
        self.assertIn(start, slots.free_slots("consult", limit=200))

    def test_cancel_releases_hold(self):
        self.send(tap("book:type:consult"))
        self.send(tap(options(self.sender)[0][0]))
        self.send(tap(options(self.sender)[1][0]))  # Cancel
        holds = store.read_json("bookings.json", {})["holds"]
        self.assertEqual(next(iter(holds.values()))["status"], "released")


class WaitlistAndHandover(Base):
    def test_waitlist_validates_email(self):
        self.send(text_msg("hi"))
        self.send(tap("menu:waitlist"))
        self.send(text_msg("not an email"))
        self.assertIn("doesn't look like an email", self.sender.texts()[-1])
        self.send(text_msg("rahul@example.com"))
        self.assertEqual(store.read_jsonl("waitlist.jsonl")[0]["email"], "rahul@example.com")

    def test_human_handover_pauses_ai_until_menu(self):
        self.send(text_msg("hi"))
        self.send(text_msg("human"))
        self.assertTrue(store.is_paused(PHONE))
        self.assertEqual(len(store.read_jsonl("handover.jsonl")), 1)
        before = len(self.sender.outbox)
        self.send(text_msg("hello? anyone"))
        self.assertEqual(len(self.sender.outbox), before)  # bot stays quiet for the human
        self.send(text_msg("menu"))
        self.assertFalse(store.is_paused(PHONE))

    def test_file_delivery(self):
        self.send(tap("menu:guide"))
        self.assertTrue(any(p["type"] == "document" for p in self.sender.outbox))


class ClaudeBrain(Base):
    def test_natural_language_booking_uses_tool(self):
        self.send(text_msg("hi"))
        tomorrow = (config.now().date() + timedelta(days=1)).isoformat()
        fake = self.use_claude([
            resp([tool_block("offer_call_slots", {"call_type": "intro", "date": tomorrow,
                                                  "part_of_day": "evening"})], stop="tool_use"),
            resp([text_block("Tap a slot above to lock it 🙂")]),
        ])
        self.send(text_msg("kal shaam call ho sakti hai?"))
        lists = [p for p in self.sender.outbox if p["type"] == "interactive"
                 and p["interactive"]["type"] == "list" and "slot" in p["interactive"]["action"]["button"].lower()]
        self.assertTrue(lists, "slot list should be sent")
        self.assertEqual(self.sender.texts()[-1], "Tap a slot above to lock it 🙂")
        first = fake.calls[0]
        self.assertEqual(first["model"], "claude-opus-5-5")
        self.assertEqual(first["fallbacks"], "default")
        self.assertEqual(first["output_config"], {"effort": "low"})
        self.assertNotIn("tool_choice", first)
        self.assertIn("kal shaam", first["messages"][-1]["content"])
        self.assertTrue(first["messages"][-1]["content"].startswith("["))  # date header
        second = fake.calls[1]["messages"]
        self.assertEqual(second[-1]["content"][0]["type"], "tool_result")
        saved = store.read_json("chats.json", {})[PHONE]["messages"]
        self.assertEqual([m["role"] for m in saved], ["user", "assistant", "user", "assistant"])

    def test_lead_json_is_stripped_and_logged(self):
        lead = ('{"name": "Rahul", "company": "Acme", "need": "3 reels", "budget": "50k", '
                '"timeline": "Nov", "notes": ""}')
        self.use_claude([resp([text_block(f"Great, noted! The team will share a quote.\nLEAD_JSON: {lead}")])])
        store.touch_inbound(PHONE, NAME)
        self.send(text_msg("Need 3 reels for Acme, budget 50k, by November"))
        body = self.sender.last("interactive")["interactive"]["body"]["text"]
        self.assertNotIn("LEAD_JSON", body)
        self.assertEqual(store.read_jsonl("leads.jsonl")[0]["company"], "Acme")

    def test_thinking_signature_error_self_heals(self):
        store.touch_inbound(PHONE, NAME)
        brain.save_history(PHONE, [
            {"role": "user", "content": "[x] hello"},
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "", "signature": "old"},
                                              {"type": "text", "text": "Hi!"}]},
        ])
        fake = self.use_claude([bad_request("Invalid signature in thinking block"), resp([text_block("Sure!")])])
        self.send(text_msg("what are your prices?"))
        retried = fake.calls[1]["messages"]
        self.assertFalse(any(isinstance(m["content"], list) and any(b["type"] == "thinking" for b in m["content"])
                             for m in retried))
        self.assertEqual(self.sender.last("interactive")["interactive"]["body"]["text"], "Sure!")

    def test_api_failure_gives_friendly_line(self):
        store.touch_inbound(PHONE, NAME)
        self.use_claude([anthropic.APIConnectionError(request=httpx.Request("POST", "https://x"))])
        self.send(text_msg("hello there, what do you do?"))
        self.assertIn("technical issue", self.sender.last("interactive")["interactive"]["body"]["text"])

    def test_refusal_gives_polite_line(self):
        store.touch_inbound(PHONE, NAME)
        self.use_claude([resp([], stop="refusal")])
        self.send(text_msg("something odd"))
        self.assertIn("can't help", self.sender.last("interactive")["interactive"]["body"]["text"])

    def test_model_change_strips_thinking_from_history(self):
        chats = {PHONE: {"fingerprint": "old", "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "", "signature": "s"},
                                              {"type": "text", "text": "hello"}]}]}}
        store.write_json("chats.json", chats)
        hist = brain.load_history(PHONE)
        self.assertEqual(hist[1]["content"], [{"type": "text", "text": "hello"}])


class Webhook(Base):
    def setUp(self):
        super().setUp()
        self.handled = []
        self._orig = router.handle
        router.handle = lambda sender, msg, name: self.handled.append((msg["id"], name))
        config.WHATSAPP_VERIFY_TOKEN = "verify-me"
        config.WHATSAPP_APP_SECRET = "app-secret"
        self.client = bot.create_app().test_client()

    def tearDown(self):
        router.handle = self._orig

    def payload(self, mid, ts=None):
        return json.dumps({"entry": [{"changes": [{"value": {
            "contacts": [{"wa_id": PHONE, "profile": {"name": NAME}}],
            "messages": [{"from": PHONE, "id": mid, "timestamp": str(ts or int(time.time())),
                          "type": "text", "text": {"body": "hi"}}]}}]}]}).encode()

    def post(self, body, secret="app-secret"):
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return self.client.post("/webhook", data=body, headers={"X-Hub-Signature-256": sig,
                                                                "Content-Type": "application/json"})

    def test_verify_handshake(self):
        ok = self.client.get("/webhook?hub.mode=subscribe&hub.verify_token=verify-me&hub.challenge=123")
        self.assertEqual((ok.status_code, ok.data), (200, b"123"))
        bad = self.client.get("/webhook?hub.mode=subscribe&hub.verify_token=nope&hub.challenge=123")
        self.assertEqual(bad.status_code, 403)

    def test_signature_dedupe_and_stale(self):
        self.assertEqual(self.post(self.payload("m1"), secret="wrong").status_code, 403)
        self.assertEqual(self.post(self.payload("m1")).status_code, 200)
        self.post(self.payload("m1"))                               # duplicate
        self.post(self.payload("m2", ts=int(time.time()) - 600))   # 10 min old
        time.sleep(0.3)
        self.assertEqual(self.handled, [("m1", NAME)])

    def test_health(self):
        self.assertEqual(self.client.get("/").status_code, 200)


class PaymentsAndWindow(Base):
    def test_razorpay_signature_and_parse(self):
        config.RAZORPAY_WEBHOOK_SECRET = "rzp-secret"
        body = json.dumps({"event": "payment_link.paid", "payload": {
            "payment_link": {"entity": {"reference_id": "Habc"}}, "payment": {"entity": {"id": "pay_1"}}}}).encode()
        sig = hmac.new(b"rzp-secret", body, hashlib.sha256).hexdigest()
        self.assertTrue(payments.verify_razorpay_signature(body, sig))
        self.assertFalse(payments.verify_razorpay_signature(body, "bad"))
        self.assertEqual(payments.parse_paid_event(json.loads(body)), ("Habc", "pay_1"))

    def test_24h_window(self):
        self.assertFalse(store.within_window(PHONE))
        store.touch_inbound(PHONE, NAME)
        self.assertTrue(store.within_window(PHONE))
        old = (config.now() - timedelta(hours=25)).isoformat()
        store.update_contact(PHONE, last_inbound=old)
        self.assertFalse(store.within_window(PHONE))


if __name__ == "__main__":
    unittest.main()
