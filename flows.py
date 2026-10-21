"""Scripted menus and flows - instant, exact and free (no AI).

Button / list reply IDs:
  menu:<row id>             welcome-list rows and "Main menu" (menu:main)
  brief:svc|budget|time:<x> brief form steps
  book:type:<call type>     pick a call type
  book:more:<type>:<iso>    more slots after a time
  slot:<type>:<iso start>   pick a slot (holds it)
  pay:done|cancel:<hold>    payment buttons
"""
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import cards
import config
import payments
import slots
import store

GREETINGS = {"hi", "hii", "hiii", "hello", "helo", "hey", "heyy", "hlo", "menu", "start", "namaste",
             "namaskar", "hi there", "hello there", "good morning", "good evening", "good afternoon"}
HUMAN_WORDS = {"human", "agent", "talk to human", "talk to a human", "real person", "owner"}
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
SESSION_TTL = 30 * 60

_sessions = {}
_lock = threading.Lock()


@dataclass
class Ctx:
    sender: object
    phone: str
    name: str = ""


# ---- sessions ---------------------------------------------------------------

def session(phone):
    """Current flow state. Cached in memory, persisted in SQLite so it survives restarts."""
    with _lock:
        s = _sessions.get(phone) or store.load_session(phone)
        if s and time.time() - s["at"] > SESSION_TTL:
            s = None
        if not s:
            s = {"flow": None, "step": None, "data": {}, "at": time.time()}
        s["at"] = time.time()
        _sessions[phone] = s
        return s


def save(phone):
    with _lock:
        s = _sessions.get(phone)
        if s and s.get("flow"):
            store.save_session(phone, s)


def set_step(phone, flow, step, **data):
    s = session(phone)
    if s["flow"] != flow:
        s["data"] = {}
    s["flow"], s["step"] = flow, step
    s["data"].update(data)
    save(phone)


def clear(phone):
    with _lock:
        _sessions.pop(phone, None)
    store.delete_session(phone)


def _norm(text):
    return re.sub(r"[^\w\s]", "", (text or "").lower()).strip()


def is_greeting(text):
    return _norm(text) in GREETINGS


def wants_human(text):
    return _norm(text) in HUMAN_WORDS


def owner():
    return config.BRAND.get("owner_name", "the team")


def after_answer_buttons():
    return [("menu:main", "Main menu"), ("menu:book", "Book a call")]


# ---- welcome / menu ---------------------------------------------------------

def send_welcome(ctx):
    clear(ctx.phone)
    ctx.sender.image(ctx.phone, cards.welcome_banner())
    show_menu(ctx, greeting=True)


def show_menu(ctx, greeting=False):
    m = config.BUSINESS["menu"]
    first = (ctx.name or "").split(" ")[0]
    if greeting:
        body = m["greeting"].format(name=f" {first}" if first else "", assistant=config.BRAND["assistant_name"])
    else:
        body = "What would you like to do next?"
    rows = [(f"menu:{r['id']}", r["title"], r.get("description", "")) for r in m["rows"]]
    ctx.sender.list(ctx.phone, body, rows, button=m.get("button", "Choose an option"),
                    section=config.BRAND["name"], footer="AI assistant · type 'human' anytime")


# ---- tap router ---------------------------------------------------------------

def handle_tap(ctx, reply_id):
    """Route a button / list tap. Returns True if handled."""
    kind, _, rest = reply_id.partition(":")
    if kind == "menu":
        return _menu(ctx, rest)
    if kind == "brief":
        return _brief_tap(ctx, rest)
    if kind == "book":
        action, _, arg = rest.partition(":")
        if action == "type":
            return offer_slots(ctx, arg) is not None
        if action == "more":
            ct_id, _, after = arg.partition(":")
            return offer_slots(ctx, ct_id, after=datetime.fromisoformat(after)) is not None
        if action == "start":
            return start_booking(ctx)
    if kind == "slot":
        ct_id, _, iso = rest.partition(":")
        return pick_slot(ctx, ct_id, datetime.fromisoformat(iso))
    if kind == "pay":
        action, _, hold_id = rest.partition(":")
        return _pay_tap(ctx, action, hold_id)
    return False


def _menu(ctx, row):
    clear(ctx.phone)
    if row == "main":
        show_menu(ctx)
    elif row == "brief":
        start_brief(ctx)
    elif row == "book":
        start_booking(ctx)
    elif row == "waitlist":
        set_step(ctx.phone, "waitlist", "email", tries=0)
        ctx.sender.text(ctx.phone, "Lovely! Please type your *email address* and we'll add you to the waitlist.")
    elif row == "ask":
        ctx.sender.text(ctx.phone, "Sure - type your question and I'll answer right away.")
    elif row == "human":
        handover(ctx, "tapped Talk to a human")
    elif any(f["id"] == row for f in config.BUSINESS.get("files", [])):
        send_file(ctx, row)
        ctx.sender.buttons(ctx.phone, "Anything else I can help with?", after_answer_buttons())
    else:
        return False
    return True


# ---- brief / lead form -------------------------------------------------------

def start_brief(ctx):
    ctx.sender.image(ctx.phone, cards.rate_card(), caption="Here's our rate card.")
    rows = [(f"brief:svc:{s['id']}", s["title"], s.get("price", "")) for s in config.BUSINESS["services"]]
    set_step(ctx.phone, "brief", "service")
    ctx.sender.list(ctx.phone, "What are you looking for?", rows, button="Pick one", section="Services")


def _brief_tap(ctx, rest):
    field, _, value = rest.partition(":")
    brief = config.BUSINESS["brief"]
    if field == "svc":
        svc = next((s for s in config.BUSINESS["services"] if s["id"] == value), None)
        set_step(ctx.phone, "brief", "budget", service=svc["title"] if svc else value)
        rows = [(f"brief:budget:{i}", b, "") for i, b in enumerate(brief["budgets"])]
        ctx.sender.list(ctx.phone, "What's your budget?", rows, button="Pick budget", section="Budget")
    elif field == "budget":
        set_step(ctx.phone, "brief", "timeline", budget=_pick(brief["budgets"], value))
        rows = [(f"brief:time:{i}", t, "") for i, t in enumerate(brief["timelines"])]
        if len(rows) <= 3:
            ctx.sender.buttons(ctx.phone, "And your timeline?", [(i, t) for i, t, _ in rows])
        else:
            ctx.sender.list(ctx.phone, "And your timeline?", rows, button="Pick timeline", section="Timeline")
    elif field == "time":
        set_step(ctx.phone, "brief", "details", timeline=_pick(brief["timelines"], value))
        ctx.sender.text(ctx.phone, "Last step: please type your *name*, *company/brand* and anything "
                                   "we should know (links, references, ideas).")
    else:
        return False
    return True


def _pick(options, idx):
    try:
        return options[int(idx)]
    except (ValueError, IndexError):
        return idx


def _finish_brief(ctx, details):
    data = session(ctx.phone)["data"]
    lead = store.append_jsonl("leads.jsonl", {
        "source": "form", "phone": ctx.phone, "whatsapp_name": ctx.name, "service": data.get("service"),
        "budget": data.get("budget"), "timeline": data.get("timeline"), "details": details})
    print(f"[lead] form {ctx.phone} {lead['service']} / {lead['budget']} / {lead['timeline']}")
    clear(ctx.phone)
    summary = (f"Thanks! Here's your brief:\n\n*Service:* {lead['service']}\n*Budget:* {lead['budget']}\n"
               f"*Timeline:* {lead['timeline']}\n*Details:* {details}\n\n"
               f"We'll review it and get back to you. Want to book a free call to discuss?")
    free = next((c for c in slots.call_types() if not c.get("price_inr")), None)
    first = (f"book:type:{free['id']}", "Book free call") if free else ("book:start", "Book a call")
    ctx.sender.buttons(ctx.phone, summary, [first, ("menu:main", "Main menu")])


# ---- booking ---------------------------------------------------------------------

def start_booking(ctx):
    types = slots.call_types()
    if len(types) == 1:
        return offer_slots(ctx, types[0]["id"]) is not None
    rows = [(f"book:type:{c['id']}", c["title"],
             f"{c['minutes']} min · " + (f"Rs {c['price_inr']:,}" if c.get("price_inr") else "Free"))
            for c in types]
    ctx.sender.list(ctx.phone, "Which session would you like?", rows, button="Pick session", section="Sessions")
    return True


def offer_slots(ctx, ct_id, date=None, part="any", after=None):
    """Send a list of free slots. Returns a short summary (used as the Claude tool result) or None."""
    ct = slots.call_type(ct_id)
    if not ct:
        return None
    found = slots.free_slots(ct_id, date=date, part=part, limit=60)
    if after:
        found = [s for s in found if s > after]
    note = ""
    if not found and (date or part != "any"):
        found = slots.free_slots(ct_id, limit=60)
        note = "No free slots at that time - here are the nearest ones. "
    if not found:
        days = config.BUSINESS["hours"].get("days_ahead", 14)
        ctx.sender.buttons(ctx.phone, f"Sorry, no free {ct['title']} slots in the next {days} days. "
                                      f"Want {owner()} to reach out personally?",
                           [("menu:human", "Yes, contact me"), ("menu:main", "Main menu")])
        return "No free slots in the booking window; offered human contact."
    show, more = found[:9], len(found) > 9
    rows = [(f"slot:{ct_id}:{s.isoformat()}", slots.slot_label(s), f"{ct['title']} · {ct['minutes']} min")
            for s in show]
    if more:
        rows.append((f"book:more:{ct_id}:{show[-1].isoformat()}", "More dates »", "See later slots"))
    price = f"Rs {ct['price_inr']:,}" if ct.get("price_inr") else "free"
    body = (f"{note}Free *{ct['title']}* slots ({ct['minutes']} min, {price}). "
            f"Tap one to hold it for {config.HOLD_MINUTES} minutes.")
    ctx.sender.list(ctx.phone, body, rows, button="Pick a slot", section="Free slots")
    set_step(ctx.phone, "book", "slot", call_type=ct_id)
    labels = ", ".join(slots.slot_label(s) for s in show[:5])
    return f"{note}Sent {len(show)} slot options for {ct['title']}: {labels}{' ...' if more else ''}."


def pick_slot(ctx, ct_id, start):
    ct = slots.call_type(ct_id)
    hold = slots.create_hold(ctx.phone, ctx.name, ct_id, start) if ct else None
    if not hold:
        ctx.sender.text(ctx.phone, "Oops, that slot was just taken. Here are the latest free ones:")
        offer_slots(ctx, ct_id)
        return True
    print(f"[booking] hold {hold['id']} {ctx.phone} {ct_id} {hold['start']}")
    if not hold["amount"]:
        booking, _ = slots.confirm_hold(hold["id"], "free")
        send_ticket(ctx.sender, booking)
        clear(ctx.phone)
        return True
    pay = payments.start_payment(hold, ctx.phone, ctx.name)
    if not pay:
        slots.release_hold(hold["id"])
        ctx.sender.text(ctx.phone, "Sorry, payments aren't working right now. I've flagged this for the team.")
        handover(ctx, "payment link failed", quiet=True)
        return True
    caption = (f"*{ct['title']}* · {slots.slot_label(start)}\nAmount: *Rs {hold['amount']:,}*\n"
               f"Held for {config.HOLD_MINUTES} min.")
    if pay["demo"]:
        caption += "\n\n_DEMO MODE - this is a test QR, no real payment is taken._"
    ctx.sender.image(ctx.phone, cards.payment_card(hold, pay["qr"], pay["demo"]), caption=caption)
    if pay["link"]:
        ctx.sender.text(ctx.phone, f"Or pay here: {pay['link']}")
    ctx.sender.buttons(ctx.phone, "Once you've paid, tap below.",
                       [(f"pay:done:{hold['id']}", "I've paid"), (f"pay:cancel:{hold['id']}", "Cancel")])
    set_step(ctx.phone, "book", "pay", hold_id=hold["id"])
    return True


def _pay_tap(ctx, action, hold_id):
    hold = slots.get_hold(hold_id)
    if not hold or hold["phone"] != ctx.phone:
        return False
    if action == "cancel":
        if hold["status"] in slots.LIVE:
            slots.release_hold(hold_id)
        clear(ctx.phone)
        ctx.sender.buttons(ctx.phone, "No problem - I've released that slot.",
                           [("book:start", "Pick another"), ("menu:main", "Main menu")])
        return True
    if hold["status"] == "confirmed":
        ctx.sender.text(ctx.phone, f"You're all set - this booking is confirmed ({hold.get('ticket')}).")
        return True
    if hold["status"] not in slots.LIVE:
        ctx.sender.buttons(ctx.phone, "That hold has expired and the slot was released. Please pick again.",
                           [("book:start", "Pick a slot"), ("menu:main", "Main menu")])
        return True
    m = payments.mode()
    if m == "demo":
        ctx.sender.text(ctx.phone, "Verifying payment… (demo)")
        booking, _ = slots.confirm_hold(hold_id, "demo", payment_ref="DEMO")
        send_ticket(ctx.sender, booking)
        clear(ctx.phone)
    elif m == "razorpay":
        ctx.sender.text(ctx.phone, "Thanks! Your booking is confirmed automatically as soon as Razorpay "
                                   "confirms the payment - usually within a minute. You'll get your ticket here.")
    else:  # manual: the owner checks their UPI app, then runs `python bot.py confirm <hold_id>`
        store.append_jsonl("payments_to_verify.jsonl", {"hold_id": hold_id, "phone": ctx.phone,
                                                        "amount": hold["amount"], "start": hold["start"]})
        print(f"[booking] {ctx.phone} says paid for {hold_id} - verify, then: python bot.py confirm {hold_id}")
        notify_owner(ctx.sender, f"Payment to verify: Rs {hold['amount']} from +{ctx.phone} for "
                                 f"{hold['start'][:16]}. Confirm with: python bot.py confirm {hold_id}")
        ctx.sender.text(ctx.phone, "Thanks! We'll verify the payment and send your ticket shortly. "
                                   "Your slot stays reserved meanwhile.")
        slots.update_hold(hold_id, expires=(config.now() + timedelta(hours=24)).isoformat(timespec="seconds"))
    return True


def send_ticket(sender, booking):
    ct = slots.call_type(booking["call_type"]) or {"title": "Call"}
    start = datetime.fromisoformat(booking["start"])
    caption = (f"Booked! ✅ Ticket *{booking['ticket']}*\n{ct['title']} · {slots.slot_label(start)}\n\n"
               f"{config.BRAND.get('booking_next_steps', 'We will share the call link before the call.')}")
    sender.image(booking["phone"], cards.ticket(booking, ct["title"]), caption=caption)
    sender.buttons(booking["phone"], "Anything else?", [("menu:main", "Main menu")])


def notify_expired(sender, hold):
    if not store.within_window(hold["phone"]):
        return
    label = slots.slot_label(datetime.fromisoformat(hold["start"]))
    sender.buttons(hold["phone"], f"Your hold on {label} expired, so the slot was released.",
                   [("book:start", "Pick again"), ("menu:main", "Main menu")])


# ---- text steps, files, handover -------------------------------------------------

def handle_text(ctx, text):
    """Consume typed text when a flow is waiting for it. Returns True if consumed."""
    s = session(ctx.phone)
    if s["flow"] == "brief" and s["step"] == "details":
        _finish_brief(ctx, text.strip()[:1500])
        return True
    if s["flow"] == "waitlist" and s["step"] == "email":
        email = text.strip()
        if EMAIL_RE.match(email):
            store.append_jsonl("waitlist.jsonl", {"phone": ctx.phone, "name": ctx.name, "email": email})
            print(f"[lead] waitlist {ctx.phone} {email}")
            clear(ctx.phone)
            ctx.sender.buttons(ctx.phone, f"You're on the list! We'll write to {email} first.",
                               [("menu:main", "Main menu")])
            return True
        s["data"]["tries"] = s["data"].get("tries", 0) + 1
        save(ctx.phone)
        if s["data"]["tries"] >= 3:
            clear(ctx.phone)
            return False  # let the AI take over
        ctx.sender.text(ctx.phone, "Hmm, that doesn't look like an email. Please type it like name@example.com")
        return True
    return False


def send_file(ctx, file_id):
    f = next((f for f in config.BUSINESS.get("files", []) if f["id"] == file_id), None)
    if not f:
        return False
    ok = ctx.sender.document(ctx.phone, f["path"], f["filename"], f.get("caption", ""))
    store.append_jsonl("files_sent.jsonl", {"phone": ctx.phone, "file": file_id, "ok": ok})
    return ok


def handover(ctx, reason, quiet=False):
    store.append_jsonl("handover.jsonl", {"phone": ctx.phone, "name": ctx.name, "reason": reason})
    store.pause(ctx.phone, config.HANDOVER_PAUSE_HOURS)
    clear(ctx.phone)
    print(f"[handover] {ctx.phone} ({ctx.name}): {reason}")
    notify_owner(ctx.sender, f"Handover: +{ctx.phone} ({ctx.name or 'no name'}) - {reason}")
    if not quiet:
        ctx.sender.text(ctx.phone, f"Done - I've flagged this chat for {owner()}, who will reply here "
                                   "personally soon. Type *menu* anytime to come back to me.")


def notify_owner(sender, text):
    """Best effort: WhatsApp only delivers if the owner messaged this number in the last 24 h."""
    if config.OWNER_PHONE and store.within_window(config.OWNER_PHONE):
        sender.text(config.OWNER_PHONE, f"[owner alert] {text}")
