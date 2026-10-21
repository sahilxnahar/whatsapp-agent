"""Tiny thread-safe JSON / JSONL storage in data/."""
import json
import os
import threading
from datetime import datetime, timedelta

import config

LOCK = threading.RLock()


def _path(name):
    config.DATA.mkdir(parents=True, exist_ok=True)
    return config.DATA / name


def read_json(name, default):
    with LOCK:
        p = _path(name)
        if not p.exists():
            return default
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"[error] {name} is corrupt; starting it fresh (old copy kept as .bad)")
            p.replace(p.with_suffix(p.suffix + ".bad"))
            return default


def write_json(name, data):
    with LOCK:
        p = _path(name)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)


def append_jsonl(name, record):
    record = {"ts": config.now().isoformat(timespec="seconds"), **record}
    with LOCK:
        with open(_path(name), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def read_jsonl(name):
    p = _path(name)
    if not p.exists():
        return []
    with LOCK:
        lines = p.read_text(encoding="utf-8").splitlines()
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# ---- contacts -------------------------------------------------------------

def get_contact(phone):
    return read_json("contacts.json", {}).get(phone, {})


def update_contact(phone, **fields):
    with LOCK:
        contacts = read_json("contacts.json", {})
        contacts.setdefault(phone, {}).update(fields)
        write_json("contacts.json", contacts)
        return contacts[phone]


def touch_inbound(phone, name):
    """Record an inbound message. Returns True if this is a brand-new contact."""
    with LOCK:
        contacts = read_json("contacts.json", {})
        is_new = phone not in contacts
        c = contacts.setdefault(phone, {"first_seen": config.now().isoformat(timespec="seconds")})
        c["last_inbound"] = config.now().isoformat(timespec="seconds")
        if name:
            c["name"] = name
        write_json("contacts.json", contacts)
        return is_new


def within_window(phone):
    """WhatsApp allows free-form replies only within 24 h of the customer's last message."""
    last = get_contact(phone).get("last_inbound")
    if not last:
        return False
    return config.now() - datetime.fromisoformat(last) < timedelta(hours=24)


def is_paused(phone):
    until = get_contact(phone).get("paused_until")
    return bool(until) and datetime.fromisoformat(until) > config.now()


def pause(phone, hours):
    until = config.now() + timedelta(hours=hours)
    update_contact(phone, paused_until=until.isoformat(timespec="seconds"))


def resume(phone):
    update_contact(phone, paused_until=None)
