"""Bookable slots, 10-minute holds and confirmed bookings (SQLite tables holds / bookings)."""
import secrets
from datetime import datetime, timedelta, time as dtime

import config
import db
import store

PARTS = {"morning": (5, 12), "afternoon": (12, 17), "evening": (17, 24), "any": (0, 24)}
TICKET_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
LIVE = ("held", "awaiting_payment")

# Extra busy-time providers, e.g. Google Calendar: callables returning [(start, end), ...].
EXTRA_BUSY = []
# Called after a booking is created / moved / cancelled, e.g. calendar + sheets sync: fn(event, booking).
BOOKING_HOOKS = []


def call_types():
    return config.BUSINESS.get("call_types", [])


def call_type(ct_id):
    return next((c for c in call_types() if c["id"] == ct_id), None)


def _parse(s):
    return datetime.fromisoformat(s)


def _holds(c, where="1=1", params=()):
    return [db.loads(r["data"]) for r in c.execute(f"SELECT data FROM holds WHERE {where}", params)]


def _bookings(c, where="1=1", params=()):
    return [db.loads(r["data"]) for r in c.execute(f"SELECT data FROM bookings WHERE {where} ORDER BY start", params)]


def _save_hold(c, h):
    c.execute("INSERT OR REPLACE INTO holds VALUES (?, ?, ?, ?)", (h["id"], h["phone"], h["status"], db.dumps(h)))


def _save_booking(c, b):
    c.execute("INSERT OR REPLACE INTO bookings VALUES (?, ?, ?, ?, ?)",
              (b["ticket"], b["phone"], b["status"], b["start"], db.dumps(b)))


def _live_holds(c):
    now = config.now()
    return [h for h in _holds(c, "status IN (?, ?)", LIVE) if _parse(h["expires"]) > now]


def _busy(c, ignore_ticket=None):
    """Intervals that block new slots: confirmed bookings + live holds + external calendars."""
    out = [(_parse(b["start"]), _parse(b["end"])) for b in _bookings(c, "status = 'confirmed'")
           if b["ticket"] != ignore_ticket]
    out += [(_parse(h["start"]), _parse(h["end"])) for h in _live_holds(c)]
    for provider in EXTRA_BUSY:
        try:
            out += provider()
        except Exception as e:  # an external calendar must never break booking
            print(f"[error] busy provider: {e}")
    return out


def _conflicts(start, end, busy):
    buf = timedelta(minutes=config.BUSINESS["hours"].get("buffer_minutes", 0))
    return any(start < be + buf and bs < end + buf for bs, be in busy)


def free_slots(ct_id, date=None, part="any", limit=10, ignore_ticket=None):
    """Next free start times for a call type, optionally on one date / part of day."""
    ct = call_type(ct_id)
    if not ct:
        return []
    hours = config.BUSINESS["hours"]
    length = timedelta(minutes=ct["minutes"])
    step = timedelta(minutes=hours.get("slot_step_minutes", 30))
    earliest = config.now() + timedelta(hours=hours.get("min_notice_hours", 0))
    lo, hi = PARTS.get(part, PARTS["any"])
    open_t, close_t = dtime.fromisoformat(hours["start"]), dtime.fromisoformat(hours["end"])
    busy = _busy(db.conn(), ignore_ticket)
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
    dt = dt.astimezone(config.TZ)
    clock = dt.strftime("%I:%M %p").lstrip("0")
    return f"{dt.strftime('%a %d %b')} · {clock}" if with_day else clock


# ---- holds ---------------------------------------------------------------------------------

def create_hold(phone, name, ct_id, start):
    """Reserve a slot for HOLD_MINUTES. Returns the hold, or None if someone else took it."""
    ct = call_type(ct_id)
    if not ct:
        return None
    end = start + timedelta(minutes=ct["minutes"])
    with db.tx() as c:
        for h in _holds(c, "phone = ? AND status IN (?, ?)", (phone, *LIVE)):
            h["status"] = "replaced"  # one live hold per customer - a new pick replaces the old one
            _save_hold(c, h)
        if start < config.now() or _conflicts(start, end, _busy(c)):
            return None
        hold = {
            "id": "H" + secrets.token_hex(5), "phone": phone, "name": name, "call_type": ct_id,
            "start": start.isoformat(), "end": end.isoformat(),
            "expires": (config.now() + timedelta(minutes=config.HOLD_MINUTES)).isoformat(timespec="seconds"),
            "amount": ct.get("price_inr", 0), "status": "held", "payment": {},
        }
        _save_hold(c, hold)
    return hold


def get_hold(hold_id):
    found = _holds(db.conn(), "id = ?", (hold_id,))
    return found[0] if found else None


def update_hold(hold_id, **fields):
    with db.tx() as c:
        found = _holds(c, "id = ?", (hold_id,))
        if not found:
            return None
        found[0].update(fields)
        _save_hold(c, found[0])
    return found[0]


def release_hold(hold_id, status="released"):
    return update_hold(hold_id, status=status)


def expire_holds():
    """Mark holds past their time as expired; returns them so the customer can be told."""
    now = config.now()
    expired = []
    with db.tx() as c:
        for h in _holds(c, "status IN (?, ?)", LIVE):
            if _parse(h["expires"]) <= now:
                h["status"] = "expired"
                _save_hold(c, h)
                expired.append(h)
    return expired


def live_hold(phone):
    holds = [h for h in _live_holds(db.conn()) if h["phone"] == phone]
    return holds[-1] if holds else None


# ---- bookings --------------------------------------------------------------------------------

def _ticket(c):
    prefix = config.BRAND.get("ticket_prefix", "BK")
    while True:
        t = f"{prefix}-" + "".join(secrets.choice(TICKET_CHARS) for _ in range(4))
        if not c.execute("SELECT 1 FROM bookings WHERE ticket = ?", (t,)).fetchone():
            return t


def run_hooks(event, booking):
    for hook in BOOKING_HOOKS:
        try:
            hook(event, booking)
        except Exception as e:
            print(f"[error] booking hook ({event}): {e}")


def confirm_hold(hold_id, paid_via, payment_ref=None):
    """Turn a hold into a booking. Returns (booking, None) or (None, reason)."""
    with db.tx() as c:
        found = _holds(c, "id = ?", (hold_id,))
        if not found:
            return None, "unknown hold"
        h = found[0]
        if h["status"] == "confirmed":
            return get_booking(h.get("ticket")), None
        if h["status"] not in LIVE:
            # Hold lapsed (e.g. payment arrived late): book only if the slot is still free.
            if _conflicts(_parse(h["start"]), _parse(h["end"]), _busy(c)):
                h["status"] = "paid_slot_taken"
                _save_hold(c, h)
                return None, "slot taken"
        booking = {
            "ticket": _ticket(c), "phone": h["phone"], "name": h["name"], "call_type": h["call_type"],
            "start": h["start"], "end": h["end"], "amount": h["amount"], "paid_via": paid_via,
            "payment_ref": payment_ref, "created": store.now_iso(), "status": "confirmed",
        }
        _save_booking(c, booking)
        h["status"], h["ticket"] = "confirmed", booking["ticket"]
        _save_hold(c, h)
    store.append_jsonl("bookings.jsonl", {"event": "confirmed", **booking})
    print(f"[booking] {booking['ticket']} {h['phone']} {h['call_type']} {h['start']} via {paid_via}")
    run_hooks("confirmed", booking)
    return get_booking(booking["ticket"]), None


def get_booking(ticket):
    found = _bookings(db.conn(), "ticket = ?", ((ticket or "").upper(),))
    return found[0] if found else None


def update_booking(ticket, **fields):
    with db.tx() as c:
        found = _bookings(c, "ticket = ?", (ticket,))
        if not found:
            return None
        found[0].update(fields)
        _save_booking(c, found[0])
    return found[0]


def upcoming_bookings(phone=None):
    now = config.now()
    rows = _bookings(db.conn(), "status = 'confirmed'")
    return [b for b in rows if _parse(b["end"]) > now and (phone is None or b["phone"] == phone)]


def all_bookings():
    return _bookings(db.conn())
