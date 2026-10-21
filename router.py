"""Decides what happens to each inbound WhatsApp message: scripted flow, AI, or human."""
import threading
import time
from collections import defaultdict, deque

import brain
import config
import flows
import store
from flows import Ctx

_user_locks = defaultdict(threading.Lock)  # one message at a time per customer, in order
_rate = defaultdict(deque)
_rate_warned = {}


def handle(sender, msg, name=""):
    phone = msg["from"]
    with _user_locks[phone]:
        try:
            _handle(sender, msg, phone, name)
        except Exception as e:  # never let one bad message kill the worker
            print(f"[error] {phone}: {type(e).__name__}: {e}")
            sender.text(phone, brain.FALLBACK_LINES["error"])


def _handle(sender, msg, phone, name):
    is_new = store.touch_inbound(phone, name)
    _log_inbound(phone, msg)
    if msg.get("id"):
        sender.mark_read(msg["id"])
    ctx = Ctx(sender, phone, name)
    mtype = msg.get("type")

    if mtype == "interactive":
        inter = msg["interactive"]
        reply = inter.get("button_reply") or inter.get("list_reply") or {}
        print(f"[tap] {phone} ({name}): {reply.get('title')} [{reply.get('id')}]")
        store.resume(phone)  # tapping a menu means they want the bot again
        if not flows.handle_tap(ctx, reply.get("id", "")):
            flows.show_menu(ctx)
        return

    if mtype == "button":  # quick-reply button on a template message
        text = msg["button"].get("text") or msg["button"].get("payload", "")
    elif mtype == "text":
        text = msg["text"]["body"]
    else:
        print(f"[in ] {phone} ({name}): <{mtype}>")
        if not store.is_paused(phone):
            sender.text(phone, "I can only read typed messages for now 🙂 Please type your question.")
        return

    print(f"[in ] {phone} ({name}): {text}")
    handle_text(ctx, text, is_new)


def handle_text(ctx, text, is_new=False):
    sender, phone = ctx.sender, ctx.phone
    if store.is_paused(phone):
        if flows.is_greeting(text):
            store.resume(phone)
            flows.send_welcome(ctx)
        else:
            print(f"[handover] {phone} paused - left for a human")
        return

    greeting = flows.is_greeting(text)
    if is_new or greeting:
        flows.send_welcome(ctx)  # banner + menu; introduces the AI assistant
        if greeting:
            return
    if flows.wants_human(text):
        flows.handover(ctx, f"typed: {text[:80]}")
        return
    if flows.handle_text(ctx, text):
        return
    if _rate_limited(phone, sender):
        return

    answer, used = brain.reply(ctx, text, first_message=False)
    if not answer:
        return
    if used:  # a tool already sent a list / file / handover - just add the short line
        sender.text(phone, answer)
    elif len(answer) <= 1024:
        sender.buttons(phone, answer, flows.after_answer_buttons())
    else:
        sender.text(phone, answer)
        sender.buttons(phone, "Anything else?", flows.after_answer_buttons())


def _log_inbound(phone, msg):
    mtype = msg.get("type", "unknown")
    if mtype == "text":
        body = msg["text"]["body"]
    elif mtype == "interactive":
        inter = msg["interactive"]
        body = (inter.get("button_reply") or inter.get("list_reply") or {}).get("title", "")
    elif mtype == "button":
        body = msg["button"].get("text", "")
    else:
        body = (msg.get(mtype) or {}).get("caption") or f"<{mtype}>"
    try:
        store.log_message(phone, "in", mtype, body, msg)
    except Exception as e:
        print(f"[error] message log: {e}")


def _rate_limited(phone, sender=None):
    """Cap AI spend from spam: N typed messages per window per customer."""
    now = time.time()
    window = config.RATE_LIMIT_WINDOW_MIN * 60
    q = _rate[phone]
    while q and now - q[0] > window:
        q.popleft()
    if len(q) < config.RATE_LIMIT_MESSAGES:
        q.append(now)
        return False
    if now - _rate_warned.get(phone, 0) > window:
        _rate_warned[phone] = now
        print(f"[warn] rate limit hit for {phone}")
        if sender:
            sender.buttons(phone, "You're sending messages faster than I can keep up 🙂 "
                                  "Please wait a few minutes, or use the menu.", [("menu:main", "Main menu")])
    return True
