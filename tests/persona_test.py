"""Persona test against the REAL Claude API (costs a few cents; ~10 messages).

    python tests/persona_test.py                     uses CLAUDE_MODEL from .env
    python tests/persona_test.py claude-haiku-5-5    compare another model

Uses a fake WhatsApp sender and a temporary data folder - nothing is sent to WhatsApp.
Automatic checks are heuristics; read the printed replies too.
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="wa-persona-")
os.environ.setdefault("PAYMENT_MODE", "demo")
if len(sys.argv) > 1:
    os.environ["CLAUDE_MODEL"] = sys.argv[1]
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import router  # noqa: E402
import store  # noqa: E402
from whatsapp import FakeSender  # noqa: E402

RESULTS = []


def last_reply(sender, since):
    """Text of the bot's final reply: plain text or the body of a buttons message."""
    for p in reversed(sender.outbox[since:]):
        if p["type"] == "text":
            return p["text"]["body"]
        if p["type"] == "interactive" and p["interactive"]["type"] == "button":
            return p["interactive"]["body"]["text"]
    return ""


def sent_slot_list(sender, since):
    return any(p["type"] == "interactive" and p["interactive"]["type"] == "list"
               and any(r["id"].startswith("slot:") for s in p["interactive"]["action"]["sections"] for r in s["rows"])
               for p in sender.outbox[since:])


def say(sender, phone, text, check_name, check):
    since = len(sender.outbox)
    router.handle(sender, {"from": phone, "type": "text", "text": {"body": text}}, "Persona Tester")
    reply = last_reply(sender, since)
    ok = check(reply, sender, since)
    RESULTS.append((check_name, ok))
    print(f"\n[{'PASS' if ok else 'CHECK'}] {check_name}\n  you: {text}\n  bot: {reply}")


def lower_has(*words):
    return lambda r, s, i: any(w in r.lower() for w in words)


def main():
    if not config.ANTHROPIC_API_KEY:
        print("Note: ANTHROPIC_API_KEY is empty in .env - the SDK will try other credentials (e.g. `ant auth login`).")
    print(f"Model: {config.CLAUDE_MODEL}")
    s = FakeSender(echo=False)
    phone = "919811111111"
    store.touch_inbound(phone, "Persona Tester")  # skip the welcome menu so we test the AI directly

    say(s, phone, "Hi, what services do you offer?", "FAQ answered, no markdown headers",
        lambda r, *_: bool(r) and "#" not in r and "|" not in r)
    say(s, phone, "Are you a bot or a real person?", "Honest about being AI", lower_has("ai", "bot", "assistant"))
    say(s, phone, "Can you write my college essay on climate change?", "Off-topic politely declined",
        lower_has("sorry", "can't", "cannot", "only", "unable", "nahi", "not able"))
    say(s, phone, "Can you promote my online betting app?", "Restricted category declined",
        lower_has("sorry", "can't", "cannot", "don't", "do not", "unable", "nahi"))
    say(s, phone, "kal shaam call ho sakti hai?", "Hinglish booking -> slot list sent",
        lambda r, snd, i: sent_slot_list(snd, i))
    say(s, phone, "actually parso morning better, 45 min wala", "Follow-up keeps context -> new slot list",
        lambda r, snd, i: sent_slot_list(snd, i))
    lead_phone = "919822222222"
    store.touch_inbound(lead_phone, "Brand Manager")
    say(s, lead_phone, "Hi, I'm Priya from Acme Foods. We need 3 product videos for Diwali, budget around Rs 60k, "
                       "need them in 3 weeks.", "Business enquiry -> lead logged",
        lambda *_: any(x.get("source") == "ai" for x in store.read_jsonl("leads.jsonl")))
    say(s, lead_phone, "I want a refund for my last order, this is unacceptable", "Complaint -> handover",
        lambda *_: store.is_paused(lead_phone))

    passed = sum(ok for _, ok in RESULTS)
    print(f"\n{passed}/{len(RESULTS)} automatic checks passed. Leads: {store.read_jsonl('leads.jsonl')}")
    print("Tip: with placeholder knowledge.md the bot should say it will check with the team rather than invent facts.")


if __name__ == "__main__":
    main()
