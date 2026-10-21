"""Owner WhatsApp commands, booking reminders and the scheduled daily summary."""
import os
import unittest
from datetime import timedelta

from test_offline import NAME, PHONE, Base, text_msg

import config
import db
import scheduler
import slots
import store
from whatsapp import FakeSender

OWNER = "919811000000"


def to(sender, phone):
    """Everything sent to one phone, as readable strings."""
    out = []
    for p in sender.outbox:
        if p["to"] != phone:
            continue
        if p["type"] == "text":
            out.append(p["text"]["body"])
        elif p["type"] == "template":
            out.append(f"<template {p['template']['name']}>")
        elif p["type"] == "interactive":
            out.append(p["interactive"]["body"]["text"])
    return out


class OwnerBase(Base):
    def setUp(self):
        super().setUp()
        self._owner = config.OWNER_PHONE
        config.OWNER_PHONE = OWNER
        store.touch_inbound(OWNER, "Owner")

    def tearDown(self):
        config.OWNER_PHONE = self._owner

    def owner_says(self, text):
        self.send(text_msg(text, phone=OWNER), name="Owner")


class OwnerCommands(OwnerBase):
    def handover(self):
        self.send(text_msg("hi"))
        self.send(text_msg("human"))
        return store.read_jsonl("handover.jsonl")[-1]["_id"]

    def test_handover_alert_carries_short_id(self):
        hid = self.handover()
        self.assertIn(f"/r {hid}", to(self.sender, OWNER)[-1])

    def test_reply_by_handover_id_reaches_customer(self):
        hid = self.handover()
        self.owner_says(f"/r {hid} Hi, this is the owner - how can I help?")
        self.assertEqual(to(self.sender, PHONE)[-1], "Hi, this is the owner - how can I help?")
        self.assertTrue(store.is_paused(PHONE))
        self.assertIn("Sent to", to(self.sender, OWNER)[-1])
        self.assertEqual(store.read_jsonl("human_replies.jsonl")[-1]["via"], "whatsapp")

    def test_reply_by_phone_and_resume(self):
        self.handover()
        self.owner_says(f"/r +{PHONE} hello")
        self.assertEqual(to(self.sender, PHONE)[-1], "hello")
        self.owner_says(f"/resume {PHONE}")
        self.assertFalse(store.is_paused(PHONE))

    def test_reply_outside_24h_window_is_refused(self):
        hid = self.handover()
        store.update_contact(PHONE, last_inbound=(config.now() - timedelta(hours=30)).isoformat())
        self.owner_says(f"/r {hid} hello")
        self.assertIn("over 24 h", to(self.sender, OWNER)[-1])
        self.assertNotEqual(to(self.sender, PHONE)[-1], "hello")

    def test_handovers_summary_and_help(self):
        hid = self.handover()
        self.owner_says("/handovers")
        self.assertIn(f"#{hid}", to(self.sender, OWNER)[-1])
        self.owner_says("/summary")
        self.assertIn("Daily summary", to(self.sender, OWNER)[-1])
        self.owner_says("/help")
        self.assertIn("/resume", to(self.sender, OWNER)[-1])

    def test_customer_cannot_run_owner_commands(self):
        self.send(text_msg("hi"))
        before = to(self.sender, OWNER)
        store.pause(PHONE, 1)  # keep the AI out of it; the text must simply not be treated as a command
        self.send(text_msg(f"/r {OWNER} hacked"))
        self.assertEqual(to(self.sender, OWNER), before)


class Reminders(OwnerBase):
    def booking(self, starts_in, created_ago=timedelta(days=2), phone=PHONE):
        start = (config.now() + starts_in).replace(microsecond=0)
        b = {"ticket": "YB-TEST", "phone": phone, "name": NAME, "call_type": "intro",
             "start": start.isoformat(), "end": (start + timedelta(minutes=15)).isoformat(),
             "amount": 0, "paid_via": "free", "created": (config.now() - created_ago).isoformat(timespec="seconds"),
             "status": "confirmed"}
        with db.tx() as c:
            slots._save_booking(c, b)
        return b

    def test_24h_reminder_sent_exactly_once(self):
        store.touch_inbound(PHONE, NAME)
        self.booking(timedelta(hours=23))
        scheduler.booking_reminders(self.sender)
        scheduler.booking_reminders(self.sender)
        sent = [m for m in to(self.sender, PHONE) if "Reminder" in m]
        self.assertEqual(len(sent), 1)
        self.assertIn("tomorrow", sent[0])

    def test_1h_reminder(self):
        store.touch_inbound(PHONE, NAME)
        self.booking(timedelta(minutes=50))
        scheduler.booking_reminders(self.sender)
        self.assertIn("in 1 hour", [m for m in to(self.sender, PHONE) if "Reminder" in m][0])

    def test_outside_window_uses_template(self):
        store.touch_inbound(PHONE, NAME)
        store.update_contact(PHONE, last_inbound=(config.now() - timedelta(hours=30)).isoformat())
        self.booking(timedelta(hours=23))
        scheduler.booking_reminders(self.sender)
        tpl = self.sender.last("template")
        self.assertEqual(tpl["template"]["name"], "booking_reminder")
        self.assertEqual(len(tpl["template"]["components"][0]["parameters"]), 3)

    def test_no_24h_reminder_right_after_a_late_booking(self):
        store.touch_inbound(PHONE, NAME)
        self.booking(timedelta(hours=5), created_ago=timedelta(minutes=5))
        scheduler.booking_reminders(self.sender)
        self.assertFalse([m for m in to(self.sender, PHONE) if "Reminder" in m])

    def test_daily_summary_once_per_day(self):
        os.environ["OWNER_SUMMARY_TIME"] = "00:00"
        try:
            sender = FakeSender(echo=False)
            scheduler.daily_summary(sender)
            scheduler.daily_summary(sender)
        finally:
            del os.environ["OWNER_SUMMARY_TIME"]
        self.assertEqual(len([m for m in to(sender, OWNER) if "Daily summary" in m]), 1)


if __name__ == "__main__":
    unittest.main()
