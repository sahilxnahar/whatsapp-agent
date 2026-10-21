"""Settings: secrets from .env, business details from business.toml."""
import os
import tomllib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

DATA = Path(os.getenv("DATA_DIR") or ROOT / "data")
ASSETS = ROOT / "assets"
CARDS_DIR = DATA / "cards"


def env(name, default=""):
    return os.getenv(name, default).strip()


WHATSAPP_TOKEN = env("WHATSAPP_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = env("WHATSAPP_PHONE_NUMBER_ID")
WHATSAPP_WABA_ID = env("WHATSAPP_WABA_ID")
WHATSAPP_VERIFY_TOKEN = env("WHATSAPP_VERIFY_TOKEN")
WHATSAPP_APP_SECRET = env("WHATSAPP_APP_SECRET")
GRAPH_VERSION = env("GRAPH_VERSION", "v25.0")
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY")
CLAUDE_MODEL = env("CLAUDE_MODEL", "claude-opus-5-5")
# Cost routing: "auto" starts chats on the fast model and escalates to CLAUDE_MODEL when needed; "off" = always CLAUDE_MODEL.
ROUTING = env("ROUTING", "off").lower()
CLAUDE_MODEL_FAST = env("CLAUDE_MODEL_FAST", "claude-haiku-5-5")
PORT = int(env("PORT", "8000"))
OWNER_PHONE = env("OWNER_PHONE")  # digits with country code, e.g. 919812345678

# Payments: demo (fake, clearly labelled) | razorpay (webhook-confirmed) | manual (owner confirms)
PAYMENT_MODE = env("PAYMENT_MODE", "demo").lower()
RAZORPAY_KEY_ID = env("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = env("RAZORPAY_KEY_SECRET")
RAZORPAY_WEBHOOK_SECRET = env("RAZORPAY_WEBHOOK_SECRET")

# Limits
RATE_LIMIT_MESSAGES = int(env("RATE_LIMIT_MESSAGES", "20"))
RATE_LIMIT_WINDOW_MIN = int(env("RATE_LIMIT_WINDOW_MIN", "10"))
HISTORY_MAX_MESSAGES = int(env("HISTORY_MAX_MESSAGES", "60"))
HOLD_MINUTES = int(env("HOLD_MINUTES", "10"))
HANDOVER_PAUSE_HOURS = int(env("HANDOVER_PAUSE_HOURS", "12"))


def load_business():
    with open(ROOT / "business.toml", "rb") as f:
        return tomllib.load(f)


BUSINESS = load_business()
BRAND = BUSINESS["brand"]
TZ = ZoneInfo(BRAND.get("timezone", "Asia/Kolkata"))
WATCHED = ("business.toml", "knowledge.md")


def _mtimes():
    return tuple((ROOT / f).stat().st_mtime_ns if (ROOT / f).exists() else 0 for f in WATCHED)


_loaded_mtimes = _mtimes()


def reload_if_changed():
    """Hot reload: pick up edits to business.toml / knowledge.md without a restart. True if reloaded."""
    global BUSINESS, BRAND, TZ, _loaded_mtimes
    current = _mtimes()
    if current == _loaded_mtimes:
        return False
    try:
        business = load_business()
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"[error] business.toml not reloaded (fix the file): {e}")
        _loaded_mtimes = current  # don't retry every message; next save triggers again
        return False
    BUSINESS, BRAND = business, business["brand"]
    TZ = ZoneInfo(BRAND.get("timezone", "Asia/Kolkata"))
    _loaded_mtimes = current
    print("[setup] business.toml / knowledge.md changed - reloaded")
    return True


def now():
    return datetime.now(TZ)


def stamp(dt=None):
    """'Monday 19 Oct 2026, 02:30 PM IST' - prefixed to each user message for Claude."""
    dt = dt or now()
    return dt.strftime("%A %d %b %Y, %I:%M %p ") + (dt.tzname() or "")


def masked(value):
    """Describe a secret without revealing it."""
    return f"set ({len(value)} chars)" if value else "MISSING"


def check_env():
    rows = [
        ("WHATSAPP_TOKEN", WHATSAPP_TOKEN, True),
        ("WHATSAPP_PHONE_NUMBER_ID", WHATSAPP_PHONE_NUMBER_ID, True),
        ("WHATSAPP_WABA_ID", WHATSAPP_WABA_ID, False),
        ("WHATSAPP_VERIFY_TOKEN", WHATSAPP_VERIFY_TOKEN, True),
        ("WHATSAPP_APP_SECRET", WHATSAPP_APP_SECRET, False),
        ("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY, True),
        ("OWNER_PHONE", OWNER_PHONE, False),
    ]
    if PAYMENT_MODE == "razorpay":
        rows += [
            ("RAZORPAY_KEY_ID", RAZORPAY_KEY_ID, True),
            ("RAZORPAY_KEY_SECRET", RAZORPAY_KEY_SECRET, True),
            ("RAZORPAY_WEBHOOK_SECRET", RAZORPAY_WEBHOOK_SECRET, True),
        ]
    ok = True
    for name, value, required in rows:
        flag = "" if value or not required else "  <-- required"
        print(f"  {name:28} {masked(value)}{flag}")
        ok = ok and (bool(value) or not required)
    print(f"  {'CLAUDE_MODEL':28} {CLAUDE_MODEL}")
    print(f"  {'PAYMENT_MODE':28} {PAYMENT_MODE}")
    print(f"  {'GRAPH_VERSION':28} {GRAPH_VERSION}")
    return ok
