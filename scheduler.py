"""Background jobs, run once a minute: expire holds, booking reminders, daily owner summary."""
import threading
import time
from datetime import datetime, timedelta

import config
import db
import flows
import owner
import slots
import store

REMINDERS = (("24h", timedelta(hours=24)), ("1h", timedelta(hours=1)))
_started = False


def templates():
    return config.BUSINESS.get("templates", {})


def send_text_or_template(sender, phone, text, template_key, params):
    """Free text inside the 24 h window; otherwise an approved template (if configured). True if sent."""
    if store.within_window(phone):
        sender.text(phone, text)
        return True
    name = templates().get(template_key)
    if not name:
        print(f"[scheduler] {phone} outside 24 h window and no '{template_key}' template set - skipped")
        return False
    components = [{"type": "body", "parameters": [{"type": "text", "text": str(p)[:1000]} for p in params]}]
    sender.template(phone, name, templates().get("language", "en"), components)
    return True


# ---- jobs ---------------------------------------------------------------------------------

def expire_holds(sender):
    for hold in slots.expire_holds():
        print(f"[booking] hold {hold['id']} expired")
        flows.notify_expired(sender, hold)


def _claim(ticket, kind):
    """Record a reminder as sent; False if it already was (so each one goes out exactly once)."""
    with db.tx() as c:
        cur = c.execute("INSERT OR IGNORE INTO reminders_sent VALUES (?, ?, ?)", (ticket, kind, store.now_iso()))
        return cur.rowcount == 1


def booking_reminders(sender):
    now = config.now()
    for b in slots.upcoming_bookings():
        start = datetime.fromisoformat(b["start"])
        created = datetime.fromisoformat(b.get("created") or b["start"])
        # Reminders that are due now and were booked before their reminder time
        # (no "24 h reminder" right after a same-day booking).
        due = [kind for kind, before in REMINDERS if start - before <= now < start and created < start - before]
        if not due:
            continue
        nearest = due[-1]  # REMINDERS runs farthest -> nearest; a late 24 h reminder must not say "tomorrow"
        claimed = [kind for kind in due if _claim(b["ticket"], kind)]  # mark all due ones as done
        if nearest in claimed:
            _send_reminder(sender, b, nearest)


def _send_reminder(sender, booking, kind):
    ct = slots.call_type(booking["call_type"]) or {"title": "call"}
    label = slots.slot_label(datetime.fromisoformat(booking["start"]))
    when = "tomorrow" if kind == "24h" else "in 1 hour"
    link = f"\nJoin: {booking['meet_link']}" if booking.get("meet_link") else ""
    text = (f"⏰ Reminder: your *{ct['title']}* with {config.BRAND['name']} is {when} ({label}).{link}\n"
            f"Need to change it? Just reply here. Ticket {booking['ticket']}.")
    sent = send_text_or_template(sender, booking["phone"], text, "booking_reminder",
                                 [booking.get("name") or "there", ct["title"], label])
    print(f"[booking] reminder {kind} {booking['ticket']} {'sent' if sent else 'skipped'}")


def daily_summary(sender):
    at = (config.env("OWNER_SUMMARY_TIME") or "").strip()
    if not at or not config.OWNER_PHONE:
        return
    now = config.now()
    if now.strftime("%H:%M") < at:
        return
    key = f"summary_sent:{now.date().isoformat()}"
    if store.kv_get(key):
        return
    store.kv_set(key, store.now_iso())
    text = owner.summary_text()
    numbers = [line.split(": ", 1)[-1] for line in text.splitlines()[1:5]]  # chats, leads, bookings, revenue
    send_text_or_template(sender, config.OWNER_PHONE.lstrip("+"), text, "daily_summary", numbers)


JOBS = (expire_holds, booking_reminders, daily_summary)


def run_once(sender):
    for job in JOBS:
        try:
            job(sender)
        except Exception as e:
            print(f"[error] scheduler {job.__name__}: {type(e).__name__}: {e}")


def start(sender, interval=60):
    """Start the background loop once per process (keep gunicorn at 1 worker)."""
    global _started
    if _started:
        return
    _started = True

    def loop():
        while True:
            run_once(sender)
            time.sleep(interval)

    threading.Thread(target=loop, daemon=True, name="scheduler").start()
