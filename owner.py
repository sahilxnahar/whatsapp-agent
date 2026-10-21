"""Owner commands sent to the bot from OWNER_PHONE on WhatsApp, plus the daily summary text.

  /r <#id|phone> <message>   reply to a customer as yourself (pauses the AI for them)
  /resume <#id|phone>        hand the chat back to the AI
  /pause <#id|phone>         stop the AI replying to them
  /handovers                 open handovers with their #ids
  /leads                     latest leads
  /today                     today's bookings
  /summary                   today's numbers
  /confirm <hold_id>         confirm a manually verified UPI payment
  /help                      this list
"""
import re
from datetime import datetime

import config
import flows
import slots
import store

HELP = __doc__.split("\n\n", 1)[1].strip()


def is_owner(phone):
    return bool(config.OWNER_PHONE) and phone == config.OWNER_PHONE.lstrip("+")


def resolve(target):
    """'#12' / '12' -> phone from handover #12; '+91 98…' / '9198…' -> phone digits."""
    target = (target or "").strip()
    num = target.lstrip("#")
    if num.isdigit() and len(num) <= 6:
        for h in reversed(store.read_jsonl("handover.jsonl")):
            if h["_id"] == int(num):
                return h["phone"]
        return None
    digits = re.sub(r"\D", "", target)
    return digits if len(digits) >= 10 else None


def open_handovers():
    """Latest handover per customer that is still paused (i.e. waiting for you)."""
    latest = {}
    for h in store.read_jsonl("handover.jsonl"):
        latest[h["phone"]] = h
    return [h for h in latest.values() if store.is_paused(h["phone"])]


def summary_text():
    today = config.now().date().isoformat()

    def todays(name):
        return [r for r in store.read_jsonl(name) if r.get("ts", "").startswith(today)]

    contacts = store.all_contacts()
    active = sum(1 for c in contacts.values() if (c.get("last_inbound") or "").startswith(today))
    new = sum(1 for c in contacts.values() if (c.get("first_seen") or "").startswith(today))
    leads, bookings = todays("leads.jsonl"), [b for b in todays("bookings.jsonl") if b.get("event") == "confirmed"]
    waitlist = todays("waitlist.jsonl")
    revenue = sum(b.get("amount", 0) for b in bookings if b.get("paid_via") not in ("demo", "free"))
    waiting = open_handovers()
    lines = [f"*Daily summary - {config.BRAND['name']}* ({today})",
             f"Chats: {active} ({new} new)", f"Leads: {len(leads)}", f"Bookings: {len(bookings)}",
             f"Revenue: Rs {revenue:,}", f"Waitlist sign-ups: {len(waitlist)}",
             f"Waiting for you: {len(waiting)}"]
    lines += [f"• Lead: +{x['phone']} {x.get('need') or x.get('service')} / {x.get('budget')}" for x in leads[:5]]
    lines += [f"• #{h['_id']} +{h['phone']} - {h['reason']}" for h in waiting[:5]]
    return "\n".join(lines)


def _fmt_booking(b):
    ct = slots.call_type(b["call_type"]) or {"title": b["call_type"]}
    who = b.get("name") or "+" + b["phone"]
    return f"• {slots.slot_label(datetime.fromisoformat(b['start']))} {ct['title']} - {who} ({b['ticket']})"


def handle(sender, text):
    """Run an owner command. Returns True if it was one."""
    me = config.OWNER_PHONE.lstrip("+")
    cmd, _, rest = text.strip().partition(" ")
    cmd = cmd.lower()
    if cmd in ("/help", "/h"):
        sender.text(me, HELP)
    elif cmd in ("/r", "/reply"):
        target, _, message = rest.strip().partition(" ")
        phone = resolve(target)
        if not phone or not message.strip():
            sender.text(me, "Usage: /r <#id or phone> <message>")
        elif not store.within_window(phone):
            sender.text(me, f"+{phone} last wrote over 24 h ago - WhatsApp only allows an approved template now.")
        else:
            sender.text(phone, message.strip())
            store.pause(phone, config.HANDOVER_PAUSE_HOURS)  # keep the AI quiet while you're talking
            store.append_jsonl("human_replies.jsonl", {"phone": phone, "text": message.strip(), "via": "whatsapp"})
            sender.text(me, f"Sent to +{phone}. AI paused for them; /resume {target} when done.")
    elif cmd in ("/resume", "/pause"):
        phone = resolve(rest)
        if not phone:
            sender.text(me, f"Usage: {cmd} <#id or phone>")
        elif cmd == "/resume":
            store.resume(phone)
            sender.text(me, f"AI is back on for +{phone}.")
        else:
            store.pause(phone, config.HANDOVER_PAUSE_HOURS)
            sender.text(me, f"AI paused for +{phone} ({config.HANDOVER_PAUSE_HOURS} h).")
    elif cmd == "/handovers":
        waiting = open_handovers()
        sender.text(me, "\n".join(f"#{h['_id']} +{h['phone']} ({h.get('name') or '-'}): {h['reason']}"
                                  for h in waiting) or "Nobody is waiting for you 🎉")
    elif cmd == "/leads":
        leads = store.read_jsonl("leads.jsonl", limit=5)
        sender.text(me, "\n".join(f"• {x['ts'][:16]} +{x['phone']} {x.get('need') or x.get('service')} / "
                                  f"{x.get('budget')} / {x.get('timeline')}" for x in reversed(leads)) or "No leads yet.")
    elif cmd == "/today":
        today = config.now().date().isoformat()
        todays = [b for b in slots.upcoming_bookings() if b["start"].startswith(today)]
        sender.text(me, "\n".join(_fmt_booking(b) for b in todays) or "No bookings today.")
    elif cmd == "/summary":
        sender.text(me, summary_text())
    elif cmd == "/confirm":
        booking, problem = slots.confirm_hold(rest.strip(), "manual", payment_ref="verified by owner")
        if booking:
            flows.send_ticket(sender, booking)
            sender.text(me, f"Confirmed {booking['ticket']} - ticket sent.")
        else:
            sender.text(me, f"Could not confirm: {problem}")
    else:
        return False
    return True
