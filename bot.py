"""WhatsApp AI agent - webhook server + owner commands.

  python bot.py serve                  run the webhook server (PORT from .env)
  python bot.py chat                   chat with the bot in the terminal (no WhatsApp, real Claude)
  python bot.py check                  show which settings are filled in (never prints secrets)
  python bot.py subscribe              link the app to your WhatsApp Business Account
  python bot.py send <phone> <text>    reply to a customer as a human
  python bot.py resume <phone>         hand a paused chat back to the AI
  python bot.py reset <phone>          clear the AI's memory for a customer
  python bot.py confirm <hold_id>      confirm a manually verified payment and send the ticket
  python bot.py summary [--send]       today's chats, leads, bookings (optionally WhatsApp it to OWNER_PHONE)

Production: gunicorn --workers 1 --threads 8 --bind 0.0.0.0:$PORT "bot:create_app()"
"""
import hashlib
import hmac
import sys
import threading
import time
from collections import deque

from flask import Flask, abort, request

import config
import flows
import payments
import router
import slots
import store
from whatsapp import FakeSender, WhatsAppSender

MAX_AGE_SECONDS = 5 * 60  # Meta redelivers old messages after outages; don't answer stale ones

_sweeper_started = False


def create_app():
    app = Flask(__name__)
    sender = WhatsAppSender()
    seen_ids, seen_set = deque(maxlen=5000), set()
    seen_lock = threading.Lock()

    def first_time(message_id):
        with seen_lock:
            if message_id in seen_set:
                return False
            if len(seen_ids) == seen_ids.maxlen:
                seen_set.discard(seen_ids[0])
            seen_ids.append(message_id)
            seen_set.add(message_id)
            return True

    @app.get("/")
    def health():
        return f"{config.BRAND['name']} WhatsApp agent is running", 200

    @app.get("/webhook")
    def verify():
        if (config.WHATSAPP_VERIFY_TOKEN and request.args.get("hub.mode") == "subscribe"
                and request.args.get("hub.verify_token") == config.WHATSAPP_VERIFY_TOKEN):
            print("[setup] webhook verified by Meta")
            return request.args.get("hub.challenge", ""), 200
        print("[setup] webhook verification failed - check WHATSAPP_VERIFY_TOKEN")
        return "verification failed", 403

    @app.post("/webhook")
    def webhook():
        raw = request.get_data()
        if config.WHATSAPP_APP_SECRET:
            sig = request.headers.get("X-Hub-Signature-256", "")
            expected = "sha256=" + hmac.new(config.WHATSAPP_APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, expected):
                print("[error] bad X-Hub-Signature-256 - request ignored")
                abort(403)
        data = request.get_json(silent=True) or {}
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                _dispatch(sender, change.get("value", {}), first_time)
        return "ok", 200  # answer Meta immediately; work happens in background threads

    @app.post("/razorpay/webhook")
    def razorpay_webhook():
        raw = request.get_data()
        if not payments.verify_razorpay_signature(raw, request.headers.get("X-Razorpay-Signature", "")):
            print("[error] bad Razorpay signature - ignored")
            abort(400)
        hold_id, payment_id = payments.parse_paid_event(request.get_json(silent=True) or {})
        if hold_id:
            threading.Thread(target=_payment_received, args=(sender, hold_id, payment_id), daemon=True).start()
        return "ok", 200

    _start_sweeper(sender)
    return app


def _dispatch(sender, value, first_time):
    names = {c.get("wa_id"): c.get("profile", {}).get("name", "") for c in value.get("contacts", [])}
    for st in value.get("statuses", []):
        if st.get("status") == "failed":
            for err in st.get("errors", []):
                print(f"[whatsapp] delivery failed to {st.get('recipient_id')}: "
                      f"code {err.get('code')} {err.get('title')}")
    for msg in value.get("messages", []):
        if not first_time(msg.get("id")):
            continue
        age = time.time() - int(msg.get("timestamp", time.time()))
        if age > MAX_AGE_SECONDS:
            print(f"[in ] skipped stale message from {msg.get('from')} ({int(age)}s old)")
            continue
        threading.Thread(target=router.handle, args=(sender, msg, names.get(msg.get("from"), "")),
                         daemon=True).start()


def _payment_received(sender, hold_id, payment_id):
    booking, problem = slots.confirm_hold(hold_id, "razorpay", payment_id)
    if booking:
        flows.send_ticket(sender, booking)
        return
    hold = slots.get_hold(hold_id) or {}
    store.append_jsonl("refunds_needed.jsonl", {"hold_id": hold_id, "payment_id": payment_id, "reason": problem})
    print(f"[booking] payment {payment_id} for {hold_id} but {problem} - REFUND / rebook needed")
    if hold.get("phone"):
        sender.text(hold["phone"], "We received your payment, but that slot was taken meanwhile 🙏 "
                                   "The team will contact you to pick a new time or refund you.")
        flows.notify_owner(sender, f"Payment {payment_id} received for a taken slot ({hold_id}). Rebook or refund.")


def _start_sweeper(sender):
    """Release expired holds every minute and tell the customer."""
    global _sweeper_started
    if _sweeper_started:
        return
    _sweeper_started = True

    def loop():
        while True:
            try:
                for hold in slots.expire_holds():
                    print(f"[booking] hold {hold['id']} expired")
                    flows.notify_expired(sender, hold)
            except Exception as e:
                print(f"[error] sweeper: {e}")
            time.sleep(60)

    threading.Thread(target=loop, daemon=True).start()


# ---- command line ----------------------------------------------------------------

def cmd_chat():
    """Terminal chat. Tap list/button options by typing /1, /2 ..."""
    phone, name = "910000000000", "Terminal Tester"
    sender = FakeSender()
    print(f"Chatting as {name} (+{phone}) with {config.CLAUDE_MODEL}. "
          "Type /1, /2 to tap options; Ctrl+C to quit.\n")
    while True:
        try:
            text = input("you > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not text:
            continue
        if text.startswith("/") and text[1:].isdigit():
            options = last_options(sender)
            idx = int(text[1:]) - 1
            if not 0 <= idx < len(options):
                print("  (no such option)")
                continue
            msg = tap_message(phone, *options[idx])
        else:
            msg = {"from": phone, "type": "text", "text": {"body": text}}
        router.handle(sender, msg, name)
        for i, (_, title) in enumerate(last_options(sender), 1):
            print(f"     /{i} {title}")


def last_options(sender):
    """(id, title) pairs of the most recent list or buttons message."""
    last = sender.last("interactive")
    if not last:
        return []
    inter = last["interactive"]
    if inter["type"] == "button":
        return [(b["reply"]["id"], b["reply"]["title"]) for b in inter["action"]["buttons"]]
    return [(r["id"], r["title"]) for s in inter["action"]["sections"] for r in s["rows"]]


def tap_message(phone, opt_id, title):
    return {"from": phone, "type": "interactive", "interactive": {"list_reply": {"id": opt_id, "title": title}}}


def cmd_summary(send=False):
    today = config.now().date().isoformat()

    def todays(name):
        return [r for r in store.read_jsonl(name) if r.get("ts", "").startswith(today)]

    contacts = store.read_json("contacts.json", {})
    active = sum(1 for c in contacts.values() if (c.get("last_inbound") or "").startswith(today))
    new = sum(1 for c in contacts.values() if (c.get("first_seen") or "").startswith(today))
    leads, bookings = todays("leads.jsonl"), todays("bookings.jsonl")
    handovers, waitlist = todays("handover.jsonl"), todays("waitlist.jsonl")
    revenue = sum(b.get("amount", 0) for b in bookings if b.get("paid_via") not in ("demo", "free"))
    lines = [f"*Daily summary - {config.BRAND['name']}* ({today})",
             f"Chats: {active} ({new} new)", f"Leads: {len(leads)}", f"Bookings: {len(bookings)}",
             f"Revenue: Rs {revenue:,}", f"Waitlist sign-ups: {len(waitlist)}",
             f"Handovers: {len(handovers)}"]
    lines += [f"• Lead: +{x['phone']} {x.get('need') or x.get('service')} / {x.get('budget')}" for x in leads[:5]]
    lines += [f"• Human needed: +{h['phone']} - {h['reason']}" for h in handovers[:5]]
    text = "\n".join(lines)
    print(text)
    if send:
        if not config.OWNER_PHONE:
            print("Set OWNER_PHONE in .env to receive this on WhatsApp.")
        elif not store.within_window(config.OWNER_PHONE):
            print("Owner hasn't messaged the bot in 24 h - WhatsApp needs an approved template to send this.")
        else:
            WhatsAppSender().text(config.OWNER_PHONE, text)


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "help"
    if cmd == "serve":
        if not config.check_env():
            print("\nSome required settings are missing in .env (see above).")
        print(f"\nServing on http://localhost:{config.PORT}  (webhook: /webhook)")
        create_app().run(host="0.0.0.0", port=config.PORT, threaded=True)
    elif cmd == "chat":
        cmd_chat()
    elif cmd == "check":
        sys.exit(0 if config.check_env() else 1)
    elif cmd == "subscribe":
        WhatsAppSender().subscribe_app()
    elif cmd == "send" and len(argv) >= 4:
        phone, text = argv[2].lstrip("+"), " ".join(argv[3:])
        if not store.within_window(phone):
            print("Outside the 24 h window - WhatsApp only allows an approved template now.")
            return
        WhatsAppSender().text(phone, text)
        store.append_jsonl("human_replies.jsonl", {"phone": phone, "text": text})
    elif cmd == "resume" and len(argv) >= 3:
        store.resume(argv[2].lstrip("+"))
        print("AI replies resumed for", argv[2])
    elif cmd == "reset" and len(argv) >= 3:
        import brain
        brain.reset_history(argv[2].lstrip("+"))
        print("Memory cleared for", argv[2])
    elif cmd == "confirm" and len(argv) >= 3:
        booking, problem = slots.confirm_hold(argv[2], "manual", payment_ref="verified by owner")
        if booking:
            flows.send_ticket(WhatsAppSender(), booking)
            print("Confirmed", booking["ticket"])
        else:
            print("Could not confirm:", problem)
    elif cmd == "summary":
        cmd_summary(send="--send" in argv)
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv)
