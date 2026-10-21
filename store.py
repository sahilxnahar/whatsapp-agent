"""Storage helpers on top of db.py: contacts, event logs, message log, flow sessions, AI memory, kv."""
import time
from datetime import datetime, timedelta

import config
import db

LOCK = db.LOCK


def _kind(name):
    """'leads.jsonl' -> 'leads' (event logs kept their v1 file names as kinds)."""
    return name[:-6] if name.endswith(".jsonl") else name


def now_iso():
    return config.now().isoformat(timespec="seconds")


# ---- event logs (leads, waitlist, handover, files_sent, refunds_needed, ...) -----------

def append_jsonl(name, record):
    record = {"ts": now_iso(), **record}
    with db.tx() as c:
        cur = c.execute("INSERT INTO events (kind, ts, data) VALUES (?, ?, ?)",
                        (_kind(name), record["ts"], db.dumps(record)))
        record["_id"] = cur.lastrowid
    return record


def read_jsonl(name, limit=None):
    rows = db.query("SELECT id, data FROM events WHERE kind = ? ORDER BY id", (_kind(name),))
    out = [{**db.loads(r["data"]), "_id": r["id"]} for r in rows]
    return out[-limit:] if limit else out


# ---- contacts ------------------------------------------------------------------------

def get_contact(phone):
    row = db.one("SELECT data FROM contacts WHERE phone = ?", (phone,))
    return db.loads(row["data"], {}) if row else {}


def all_contacts():
    return {r["phone"]: db.loads(r["data"], {}) for r in db.query("SELECT phone, data FROM contacts")}


def update_contact(phone, **fields):
    with db.tx() as c:
        row = c.execute("SELECT data FROM contacts WHERE phone = ?", (phone,)).fetchone()
        data = db.loads(row["data"], {}) if row else {}
        data.update(fields)
        c.execute("INSERT OR REPLACE INTO contacts VALUES (?, ?)", (phone, db.dumps(data)))
    return data


def touch_inbound(phone, name):
    """Record an inbound message. Returns True if this is a brand-new contact."""
    with db.tx() as c:
        row = c.execute("SELECT data FROM contacts WHERE phone = ?", (phone,)).fetchone()
        data = db.loads(row["data"], {}) if row else {"first_seen": now_iso()}
        data["last_inbound"] = now_iso()
        if name:
            data["name"] = name
        c.execute("INSERT OR REPLACE INTO contacts VALUES (?, ?)", (phone, db.dumps(data)))
    return row is None


def _still_future(iso, delta=timedelta(0)):
    return bool(iso) and datetime.fromisoformat(iso) + delta > config.now()


def within_window(phone):
    """WhatsApp allows free-form replies only within 24 h of the customer's last message."""
    return _still_future(get_contact(phone).get("last_inbound"), timedelta(hours=24))


def is_paused(phone):
    return _still_future(get_contact(phone).get("paused_until"))


def pause(phone, hours):
    update_contact(phone, paused_until=(config.now() + timedelta(hours=hours)).isoformat(timespec="seconds"))


def resume(phone):
    update_contact(phone, paused_until=None)


# ---- message log (powers the owner inbox) ---------------------------------------------

def log_message(phone, direction, kind, body, data=None):
    with db.tx() as c:
        c.execute("INSERT INTO messages (phone, direction, kind, body, data, ts) VALUES (?, ?, ?, ?, ?, ?)",
                  (phone, direction, kind, body, db.dumps(data) if data is not None else None, now_iso()))


def conversation(phone, after_id=0, limit=200):
    rows = db.query("SELECT * FROM messages WHERE phone = ? AND id > ? ORDER BY id DESC LIMIT ?",
                    (phone, after_id, limit))
    return [dict(r) for r in reversed(rows)]


def threads(limit=100):
    """Latest message per contact, newest first."""
    rows = db.query("""SELECT m.* FROM messages m
                       JOIN (SELECT phone, MAX(id) AS id FROM messages GROUP BY phone) last ON last.id = m.id
                       ORDER BY m.id DESC LIMIT ?""", (limit,))
    return [dict(r) for r in rows]


# ---- flow sessions (survive restarts) ---------------------------------------------------

def load_session(phone):
    row = db.one("SELECT data, at FROM sessions WHERE phone = ?", (phone,))
    return {**db.loads(row["data"]), "at": row["at"]} if row else None


def save_session(phone, session):
    data = {k: v for k, v in session.items() if k != "at"}
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?, ?)", (phone, db.dumps(data), time.time()))


def delete_session(phone):
    with db.tx() as c:
        c.execute("DELETE FROM sessions WHERE phone = ?", (phone,))


# ---- AI memory ------------------------------------------------------------------------------

def get_chat(phone):
    row = db.one("SELECT fingerprint, messages FROM chats WHERE phone = ?", (phone,))
    return {"fingerprint": row["fingerprint"], "messages": db.loads(row["messages"], [])} if row else None


def save_chat(phone, fingerprint, messages):
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO chats VALUES (?, ?, ?, ?)",
                  (phone, fingerprint, now_iso(), db.dumps(messages)))


def delete_chat(phone):
    with db.tx() as c:
        c.execute("DELETE FROM chats WHERE phone = ?", (phone,))


# ---- key / value (media cache, misc) ---------------------------------------------------------

def kv_get(key, default=None):
    row = db.one("SELECT value FROM kv WHERE key = ?", (key,))
    return db.loads(row["value"]) if row else default


def kv_set(key, value):
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, db.dumps(value)))
