"""SQLite storage (stdlib). One file: data/bot.db. WAL mode, one connection per thread."""
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

import config

LOCK = threading.RLock()  # serialises writers; SQLite allows one writer at a time
_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (phone TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT NOT NULL, direction TEXT NOT NULL,
    kind TEXT NOT NULL, body TEXT, data TEXT, ts TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS messages_phone ON messages(phone, id);
CREATE TABLE IF NOT EXISTS chats (phone TEXT PRIMARY KEY, fingerprint TEXT, updated TEXT, messages TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (phone TEXT PRIMARY KEY, data TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS holds (
    id TEXT PRIMARY KEY, phone TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS holds_status ON holds(status);
CREATE TABLE IF NOT EXISTS bookings (
    ticket TEXT PRIMARY KEY, phone TEXT NOT NULL, status TEXT NOT NULL, start TEXT NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS bookings_status ON bookings(status, start);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, ts TEXT NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_kind ON events(kind, id);
CREATE TABLE IF NOT EXISTS reminders_sent (ticket TEXT NOT NULL, kind TEXT NOT NULL, ts TEXT NOT NULL,
    PRIMARY KEY (ticket, kind));
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
TABLES = ("contacts", "messages", "chats", "sessions", "holds", "bookings", "events", "reminders_sent", "kv")


def path():
    return Path(config.DATA) / "bot.db"


def conn():
    """Per-thread connection (re-opened if DATA_DIR changed, e.g. in tests)."""
    p = str(path())
    c = getattr(_local, "conn", None)
    if c is None or getattr(_local, "path", None) != p:
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(p, timeout=10, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=5000")
        c.executescript(SCHEMA)
        _local.conn, _local.path = c, p
    return c


@contextmanager
def tx():
    """Write transaction: `with db.tx() as c: c.execute(...)` - commits, or rolls back on error."""
    with LOCK:
        c = conn()
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise


def query(sql, params=()):
    return conn().execute(sql, params).fetchall()


def one(sql, params=()):
    return conn().execute(sql, params).fetchone()


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False)


def loads(text, default=None):
    return json.loads(text) if text else default


def reset_all():
    """Empty every table (tests only)."""
    with tx() as c:
        for t in TABLES:
            c.execute(f"DELETE FROM {t}")


def migrate_from_json(data_dir=None):
    """One-time import of v1 data/*.json(l) files. Imported files are renamed *.migrated (kept, not deleted)."""
    data_dir = Path(data_dir or config.DATA)
    moved = []

    def load(name, default):
        p = data_dir / name
        if not p.exists():
            return None
        moved.append(p)
        if name.endswith(".jsonl"):
            return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
        return json.loads(p.read_text(encoding="utf-8") or "null") or default

    with tx() as c:
        for phone, data in (load("contacts.json", {}) or {}).items():
            c.execute("INSERT OR REPLACE INTO contacts VALUES (?, ?)", (phone, dumps(data)))
        for phone, entry in (load("chats.json", {}) or {}).items():
            c.execute("INSERT OR REPLACE INTO chats VALUES (?, ?, ?, ?)",
                      (phone, entry.get("fingerprint"), entry.get("updated"), dumps(entry.get("messages", []))))
        bk = load("bookings.json", {}) or {}
        for h in bk.get("holds", {}).values():
            c.execute("INSERT OR REPLACE INTO holds VALUES (?, ?, ?, ?)", (h["id"], h["phone"], h["status"], dumps(h)))
        for b in bk.get("bookings", {}).values():
            c.execute("INSERT OR REPLACE INTO bookings VALUES (?, ?, ?, ?, ?)",
                      (b["ticket"], b["phone"], b.get("status", "confirmed"), b["start"], dumps(b)))
        for key, value in (load("media_cache.json", {}) or {}).items():
            c.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (f"media:{key}", dumps(value)))
        for p in sorted(data_dir.glob("*.jsonl")):
            for rec in load(p.name, []) or []:
                c.execute("INSERT INTO events (kind, ts, data) VALUES (?, ?, ?)",
                          (p.stem, rec.get("ts", ""), dumps(rec)))
    for p in moved:
        p.rename(p.with_name(p.name + ".migrated"))
    return [p.name for p in moved]
