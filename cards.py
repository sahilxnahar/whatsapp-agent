"""Branded images generated locally with Pillow + qrcode (no AI cost)."""
import hashlib
import json
from datetime import datetime
from pathlib import Path

import qrcode
from PIL import Image, ImageDraw, ImageFont, ImageOps

import config

W, H = 1200, 630
FONT_CANDIDATES = {
    "bold": ["assets/fonts/Bold.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
             "/Library/Fonts/Arial Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
             "C:/Windows/Fonts/arialbd.ttf"],
    "regular": ["assets/fonts/Regular.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf",
                "/Library/Fonts/Arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "C:/Windows/Fonts/arial.ttf"],
}
GREY = (110, 110, 110)
RED = (214, 40, 40)


def _font(weight, size):
    for cand in FONT_CANDIDATES[weight]:
        p = Path(cand) if Path(cand).is_absolute() else config.ROOT / cand
        if p.exists():
            return ImageFont.truetype(str(p), size)
    return ImageFont.load_default(size=size)


def _rgb(hex_color):
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _colors():
    b = config.BRAND
    return (_rgb(b.get("primary_color", "#FFC700")), _rgb(b.get("dark_color", "#141414")),
            _rgb(b.get("light_color", "#FAFAF7")))


def _out(name):
    config.CARDS_DIR.mkdir(parents=True, exist_ok=True)
    return config.CARDS_DIR / name


def _asset(key):
    rel = config.BRAND.get(key)
    if not rel:
        return None
    p = config.ROOT / rel
    return p if p.exists() else None


def _wrap(draw, text, font, width):
    lines, line = [], ""
    for w in text.split():
        trial = f"{line} {w}".strip()
        if draw.textlength(trial, font=font) <= width:
            line = trial
        else:
            if line:
                lines.append(line)
            line = w
    if line:
        lines.append(line)
    return lines


def _logo_block(img, draw, x, y):
    """Logo image if provided, else a rounded badge with the brand initials."""
    primary, dark, _ = _colors()
    logo = _asset("logo")
    if logo:
        im = ImageOps.fit(Image.open(logo).convert("RGBA"), (72, 72))
        img.paste(im, (x, y), im)
    else:
        draw.rounded_rectangle((x, y, x + 72, y + 72), radius=18, fill=primary)
        initials = "".join(w[0] for w in config.BRAND["name"].split()[:2]).upper()
        draw.text((x + 36, y + 36), initials, font=_font("bold", 30), fill=dark, anchor="mm")
    draw.text((x + 90, y + 36), config.BRAND["name"].upper(), font=_font("bold", 24), fill=dark, anchor="lm")


def _cached(kind, payload):
    """Re-use an image when its inputs haven't changed (banner, rate card)."""
    digest = hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:10]
    return _out(f"{kind}-{digest}.png")


def welcome_banner():
    primary, dark, light = _colors()
    photo = _asset("photo")
    path = _cached("banner", [config.BRAND, photo and photo.stat().st_mtime])
    if path.exists():
        return path
    img = Image.new("RGB", (W, H), light)
    d = ImageDraw.Draw(img)
    d.ellipse((W - 380, -260, W + 260, 380), fill=primary)
    _logo_block(img, d, 60, 56)
    y = 230
    headline = _font("bold", 58)
    for line in _wrap(d, config.BRAND.get("tagline", ""), headline, 700)[:3]:
        d.text((60, y), line, font=headline, fill=dark)
        y += 70
    d.text((60, H - 90), config.BRAND.get("assistant_name", ""), font=_font("regular", 26), fill=dark)
    if photo:
        size = 300
        im = ImageOps.fit(Image.open(photo).convert("RGB"), (size, size))
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
        img.paste(im, (W - size - 90, H - size - 70), mask)
    img.save(path)
    return path


def rate_card():
    primary, dark, light = _colors()
    services = config.BUSINESS.get("services", [])
    path = _cached("rates", [config.BRAND, services])
    if path.exists():
        return path
    img = Image.new("RGB", (W, 280 + max(len(services), 1) * 110), light)
    d = ImageDraw.Draw(img)
    _logo_block(img, d, 60, 50)
    d.text((60, 150), "Services & pricing", font=_font("bold", 44), fill=dark)
    y = 220
    price_font = _font("bold", 26)
    for s in services:
        d.rounded_rectangle((60, y, W - 60, y + 90), radius=18, fill=(255, 255, 255), outline=(225, 225, 220))
        d.text((90, y + 28), s["title"], font=_font("bold", 30), fill=dark, anchor="lm")
        d.text((90, y + 64), s.get("description", ""), font=_font("regular", 22), fill=GREY, anchor="lm")
        pw = d.textlength(s["price"], font=price_font) + 40
        d.rounded_rectangle((W - 90 - pw, y + 25, W - 90, y + 65), radius=14, fill=primary)
        d.text((W - 90 - pw / 2, y + 45), s["price"], font=price_font, fill=dark, anchor="mm")
        y += 110
    img.save(path)
    return path


def _qr(data, size):
    q = qrcode.QRCode(box_size=10, border=2)
    q.add_data(data)
    q.make(fit=True)
    return q.make_image(fill_color="black", back_color="white").convert("RGB").resize((size, size))


def payment_card(hold, qr_data, demo):
    primary, dark, light = _colors()
    img = Image.new("RGB", (W, H), light)
    d = ImageDraw.Draw(img)
    _logo_block(img, d, 60, 56)
    start = datetime.fromisoformat(hold["start"]).astimezone(config.TZ)
    d.text((60, 190), "Scan to pay", font=_font("bold", 56), fill=dark)
    d.text((60, 270), f"Rs {hold['amount']:,}", font=_font("bold", 72), fill=dark)
    d.text((60, 370), start.strftime("%a %d %b, %I:%M %p"), font=_font("regular", 30), fill=dark)
    d.text((60, 415), f"Slot held for {config.HOLD_MINUTES} minutes", font=_font("regular", 26), fill=GREY)
    img.paste(_qr(qr_data, 380), (W - 460, 120))
    if demo:
        d.rectangle((0, H - 70, W, H), fill=RED)
        d.text((W / 2, H - 35), "DEMO ONLY - NO REAL PAYMENT IS TAKEN", font=_font("bold", 30),
               fill=(255, 255, 255), anchor="mm")
    else:
        d.rectangle((0, H - 18, W, H), fill=primary)
    path = _out(f"pay-{hold['id']}.png")
    img.save(path)
    return path


def ticket(booking, call_title):
    primary, dark, light = _colors()
    img = Image.new("RGB", (W, H), light)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((40, 40, W - 40, H - 40), radius=36, fill=(255, 255, 255), outline=(225, 225, 220), width=2)
    d.rounded_rectangle((40, 40, 420, H - 40), radius=36, fill=primary)
    d.rectangle((380, 40, 420, H - 40), fill=primary)
    d.text((230, 200), "BOOKED", font=_font("bold", 54), fill=dark, anchor="mm")
    d.text((230, 290), booking["ticket"], font=_font("bold", 46), fill=dark, anchor="mm")
    d.text((230, 360), config.BRAND["name"], font=_font("regular", 26), fill=dark, anchor="mm")
    for y in range(60, H - 60, 24):  # perforation
        d.ellipse((414, y, 426, y + 12), fill=light)
    start = datetime.fromisoformat(booking["start"]).astimezone(config.TZ)  # "IST", not "UTC+05:30"
    end = datetime.fromisoformat(booking["end"]).astimezone(config.TZ)
    x = 480
    d.text((x, 110), call_title, font=_font("bold", 46), fill=dark)
    d.text((x, 200), start.strftime("%A, %d %B %Y"), font=_font("regular", 34), fill=dark)
    clock = f"{start.strftime('%I:%M %p').lstrip('0')} - {end.strftime('%I:%M %p').lstrip('0')} {start.tzname() or ''}"
    d.text((x, 250), clock, font=_font("bold", 40), fill=dark)
    d.text((x, 340), f"Name: {booking.get('name') or '-'}", font=_font("regular", 28), fill=GREY)
    paid = "Free" if not booking["amount"] else f"Rs {booking['amount']:,} ({booking['paid_via']})"
    d.text((x, 385), f"Payment: {paid}", font=_font("regular", 28), fill=GREY)
    if booking["paid_via"] == "demo":
        d.text((x, 450), "DEMO BOOKING - no real payment taken", font=_font("bold", 26), fill=RED)
    path = _out(f"ticket-{booking['ticket']}.png")
    img.save(path)
    return path
