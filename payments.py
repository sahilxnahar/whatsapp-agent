"""Payments for paid calls.

demo     - fake UPI QR clearly labelled DEMO; "I've paid" confirms (testing only).
manual   - real UPI QR to your UPI ID; YOU confirm with `python bot.py confirm <hold_id>`.
razorpay - Razorpay payment link; the booking is confirmed ONLY by Razorpay's signed webhook.
A customer saying "I paid" never confirms a booking outside demo mode.
"""
import hashlib
import hmac
import time
from urllib.parse import quote

import requests

import config
import slots

RAZORPAY_API = "https://api.razorpay.com/v1"


def mode():
    return config.PAYMENT_MODE if config.PAYMENT_MODE in ("demo", "manual", "razorpay") else "demo"


def upi_uri(hold):
    b = config.BRAND
    return (f"upi://pay?pa={quote(b.get('upi_id', ''))}&pn={quote(b['name'])}"
            f"&am={hold['amount']}&cu=INR&tn={quote('Booking ' + hold['id'])}")


def start_payment(hold, phone, name):
    """Returns {"qr": data for the QR code, "link": optional URL, "demo": bool} or None on failure."""
    m = mode()
    if m in ("demo", "manual"):
        slots.update_hold(hold["id"], status="awaiting_payment", payment={"mode": m})
        return {"qr": upi_uri(hold), "link": None, "demo": m == "demo"}
    link = create_razorpay_link(hold, phone, name)
    if not link:
        return None
    slots.update_hold(hold["id"], status="awaiting_payment",
                      payment={"mode": "razorpay", "link_id": link["id"], "url": link["short_url"]})
    return {"qr": link["short_url"], "link": link["short_url"], "demo": False}


def create_razorpay_link(hold, phone, name):
    ct = slots.call_type(hold["call_type"]) or {}
    body = {
        "amount": int(hold["amount"]) * 100,  # paise
        "currency": "INR",
        "accept_partial": False,
        "reference_id": hold["id"],
        "description": f"{config.BRAND['name']} - {ct.get('title', 'Booking')}",
        "customer": {"name": name or "Customer", "contact": "+" + phone.lstrip("+")},
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
        # Razorpay requires expiry >= 15 min ahead; the slot hold itself is shorter.
        "expire_by": int(time.time()) + 16 * 60,
        "notes": {"hold_id": hold["id"], "phone": phone},
    }
    try:
        r = requests.post(f"{RAZORPAY_API}/payment_links", json=body, timeout=20,
                          auth=(config.RAZORPAY_KEY_ID, config.RAZORPAY_KEY_SECRET))
    except requests.RequestException as e:
        print(f"[error] razorpay network: {e}")
        return None
    if r.status_code >= 400:
        print(f"[error] razorpay {r.status_code}: {r.text[:300]}")
        return None
    return r.json()


def verify_razorpay_signature(raw_body, signature):
    if not config.RAZORPAY_WEBHOOK_SECRET or not signature:
        return False
    expected = hmac.new(config.RAZORPAY_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def parse_paid_event(event):
    """Return (hold_id, payment_id) for a successful payment event, else (None, None)."""
    if event.get("event") != "payment_link.paid":
        return None, None
    payload = event.get("payload", {})
    link = payload.get("payment_link", {}).get("entity", {})
    payment = payload.get("payment", {}).get("entity", {})
    hold_id = link.get("reference_id") or link.get("notes", {}).get("hold_id")
    return hold_id, payment.get("id")
