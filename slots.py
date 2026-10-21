"""Bookable slots, 10-minute holds and confirmed bookings (data/bookings.json)."""
import secrets
from datetime import datetime, timedelta, time as dtime

import config
import store

FILE = "bookings.json"
PARTS = {"morning": (5, 12), "afternoon": (12, 17), "evening": (17, 24), "any": (0, 24)}
TICKET_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
LIVE = ("held", "awaiting_payment")


def call_types():
    return config.BUSINESS.get("call_types", [])


def call_type(ct_id):
    return next((c for c in call_types() if c["id"] == ct_id), None)


def _load():
    return store.read_json(FILE, {"holds": {}, "bookings": {}})


def _parse(s):
    return datetime.fromisoformat(s)


def _busy(data):
    """Intervals that block new slots: confirmed bookings + live holds."""
    now = config.now()
    out = [(_parse(b["start"]), _parse(b["end"])) for b in data["bookings"].values()
           if b.get("status", "confirmed") == "confirmed"]
    out += [(_parse(h["start"]), _parse(h["end"])) for h in data["holds"].values()
            if h["status"] in LIVE and _parse(h["expires"]) > now]
    return out


def _conflicts(start, end, busy):
    buf = timedelta(minutes=config.BUSINESS["hours"].get("buffer_minutes", 0))
    return any(start < be + buf and bs < end + buf for bs, be in busy)


def free_slots(ct_id, date=None, part="any", limit=10):
    """Next free start times for a call type, optionally on one date / part of day."""
    ct = call_type(ct_id)
    if not ct:
        return []
    hours = config.BUSINESS["hours"]
    length = timedelta(minutes=ct["minutes"])
    step = timedelta(minutes=hours.get("slot_step_minutes", 30))
    earliest = config.now() + timedelta(hours=hours.get("min_notice_hours", 0))
    lo, hi = PARTS.get(part, PARTS["any"])
    open_t = dtime.fromisoformat(hours["start"])
    close_t = dtime.fromisoformat(hours["end"])
    with store.LOCK:
        busy = _busy(_load())
    today = config.now().date()
    days = [date] if date else [today + timedelta(days=i) for i in range(hours.get("days_ahead", 14) + 1)]
    out = []
    for day in days:
        if day.strftime("%a") not in hours["days"]:
            continue
        t = datetime.combine(day, open_t, tzinfo=config.TZ)
        close = datetime.combine(day, close_t, tzinfo=config.TZ)
        while t + length <= close:
            if t >= earliest and lo <= t.hour < hi and not _conflicts(t, t + length, busy):
                out.append(t)
                if len(out) >= limit:
                    return out
            t += step
    return out


def slot_label(dt, with_day=True):
    """'Tue 20 Oct · 6:00 PM'"""
    clock = dt.strftime("%I:%M %p").lstrip("0")
    return f"{dt.strftime('%a %d %b')} · {clock}" if with_day else clock


def create_hold(phone, name, ct_id, start):
    """Reserve a slot for HOLD_MINUTES. Returns the hold, or None if someone else took it."""
    ct = call_type(ct_id)
    if not ct:
        return None
    end = start + timedelta(minutes=ct["minutes"])
    with store.LOCK:
        data = _load()
        # One live hold per customer - a new pick replaces the old one.
        for h in data["holds"].values():
            if h["phone"] == phone and h["status"] in LIVE:
                h["status"] = "replaced"
        if start < config.now() or _conflicts(start, end, _busy(data)):
            return None
        hold_id = "H" + secrets.token_hex(5)
        hold = {
            "id": hold_id, "phone": phone, "name": name, "call_type": ct_id,
            "start": start.isoformat(), "end": end.isoformat(),
            "expires": (config.now() + timedelta(minutes=config.HOLD_MINUTES)).isoformat(timespec="seconds"),
            "amount": ct.get("price_inr", 0), "status": "held", "payment": {},
        }
        data["holds"][hold_id] = hold
        store.write_json(FILE, data)
    return hold


def get_hold(hold_id):
    return _load()["holds"].get(hold_id)


def update_hold(hold_id, **fields):
    with store.LOCK:
        data = _load()
        if hold_id in data["holds"]:
            data["holds"][hold_id].update(fields)
            store.write_json(FILE, data)
            return data["holds"][hold_id]
    return None


def release_hold(hold_id, status="released"):
    return update_hold(hold_id, status=status)


def expire_holds():
    """Mark holds past their time as expired; returns them so the customer can be told."""
    now = config.now()
    expired = []
    with store.LOCK:
        data = _load()
        for h in data["holds"].values():
            if h["status"] in LIVE and _parse(h["expires"]) <= now:
                h["status"] = "expired"
                expired.append(h)
        if expired:
            store.write_json(FILE, data)
    return expired


def _ticket(data):
    prefix = config.BRAND.get("ticket_prefix", "BK")
    while True:
        t = f"{prefix}-" + "".join(secrets.choice(TICKET_CHARS) for _ in range(4))
        if t not in data["bookings"]:
            return t


def confirm_hold(hold_id, paid_via, payment_ref=None):
    """Turn a hold into a booking. Returns (booking, None) or (None, reason)."""
    with store.LOCK:
        data = _load()
        h = data["holds"].get(hold_id)
        if not h:
            return None, "unknown hold"
        if h["status"] == "confirmed":
            return data["bookings"].get(h.get("ticket")), None
        if h["status"] not in LIVE:
            # Hold lapsed (e.g. payment arrived late): book only if the slot is still free.
            if _conflicts(_parse(h["start"]), _parse(h["end"]), _busy(data)):
                h["status"] = "paid_slot_taken"
                store.write_json(FILE, data)
                return None, "slot taken"
        ticket = _ticket(data)
        booking = {
            "ticket": ticket, "phone": h["phone"], "name": h["name"], "call_type": h["call_type"],
            "start": h["start"], "end": h["end"], "amount": h["amount"], "paid_via": paid_via,
            "payment_ref": payment_ref, "created": config.now().isoformat(timespec="seconds"),
            "status": "confirmed",
        }
        data["bookings"][ticket] = booking
        h["status"], h["ticket"] = "confirmed", ticket
        store.write_json(FILE, data)
    store.append_jsonl("bookings.jsonl", {"event": "confirmed", **booking})
    print(f"[booking] {ticket} {h['phone']} {h['call_type']} {h['start']} via {paid_via}")
    return booking, None


def upcoming_bookings(phone=None):
    now = config.now()
    out = [b for b in _load()["bookings"].values()
           if b.get("status") == "confirmed" and _parse(b["end"]) > now and (phone is None or b["phone"] == phone)]
    return sorted(out, key=lambda b: b["start"])


def live_hold(phone):
    holds = [h for h in _load()["holds"].values()
             if h["phone"] == phone and h["status"] in LIVE and _parse(h["expires"]) > config.now()]
    return holds[-1] if holds else None
