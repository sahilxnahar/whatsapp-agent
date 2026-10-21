"""WhatsApp Cloud API sender (official API only) plus a fake sender for tests."""
import mimetypes
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

import config
import store

# WhatsApp interactive message limits - Meta rejects anything longer (400).
LIMITS = {
    "text": 4096, "interactive_body": 1024, "header": 60, "footer": 60,
    "button_title": 20, "max_buttons": 3, "list_button": 20, "row_title": 24,
    "row_desc": 72, "max_rows": 10, "section_title": 24, "caption": 1024, "id": 200,
}
MEDIA_CACHE_DAYS = 25  # Meta keeps uploaded media ~30 days


def clip(text, limit):
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class BaseSender:
    """Shared message-building logic. Subclasses implement _post and upload."""

    def text(self, to, body):
        body = (body or "").strip()
        if not body:
            return
        # Split very long replies instead of letting Meta reject them.
        while body:
            chunk, body = body[: LIMITS["text"]], body[LIMITS["text"]:]
            self._post({"to": to, "type": "text",
                        "text": {"body": chunk, "preview_url": "http" in chunk}}, log=f"[out] {to}: {chunk}")

    def buttons(self, to, body, buttons, header=None, footer=None):
        """buttons: list of (id, title). Max 3, titles <= 20 chars."""
        if len(buttons) > LIMITS["max_buttons"]:
            print(f"[warn] {len(buttons)} buttons; WhatsApp allows 3 - extra dropped")
        interactive = {
            "type": "button",
            "body": {"text": clip(body, LIMITS["interactive_body"])},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": clip(i, LIMITS["id"]), "title": clip(t, LIMITS["button_title"])}}
                for i, t in buttons[: LIMITS["max_buttons"]]
            ]},
        }
        self._decorate(interactive, header, footer)
        titles = " | ".join(t for _, t in buttons[:3])
        self._post({"to": to, "type": "interactive", "interactive": interactive},
                   log=f"[out] {to}: {body} [{titles}]")

    def list(self, to, body, rows, button="Choose an option", section="Options", header=None, footer=None):
        """rows: list of (id, title, description). Max 10 rows."""
        if len(rows) > LIMITS["max_rows"]:
            print(f"[warn] {len(rows)} rows; WhatsApp allows 10 - extra dropped")
        interactive = {
            "type": "list",
            "body": {"text": clip(body, LIMITS["interactive_body"])},
            "action": {
                "button": clip(button, LIMITS["list_button"]),
                "sections": [{"title": clip(section, LIMITS["section_title"]), "rows": [
                    {"id": clip(i, LIMITS["id"]), "title": clip(t, LIMITS["row_title"]),
                     **({"description": clip(d, LIMITS["row_desc"])} if d else {})}
                    for i, t, d in rows[: LIMITS["max_rows"]]
                ]}],
            },
        }
        self._decorate(interactive, header, footer)
        titles = " | ".join(t for _, t, _ in rows[:10])
        self._post({"to": to, "type": "interactive", "interactive": interactive},
                   log=f"[out] {to}: {body} <list: {titles}>")

    def image(self, to, path, caption=""):
        media_id = self.upload(path)
        if not media_id:
            if caption:
                self.text(to, caption)
            return
        self._post({"to": to, "type": "image",
                    "image": {"id": media_id, **({"caption": clip(caption, LIMITS["caption"])} if caption else {})}},
                   log=f"[out] {to}: <image {Path(path).name}> {caption}")

    def document(self, to, path, filename, caption=""):
        media_id = self.upload(path)
        if not media_id:
            self.text(to, "Sorry, I couldn't attach the file right now. The team will share it with you shortly.")
            return False
        self._post({"to": to, "type": "document", "document": {
            "id": media_id, "filename": filename, **({"caption": clip(caption, LIMITS["caption"])} if caption else {})}},
            log=f"[out] {to}: <document {filename}>")
        return True

    def template(self, to, name, language="en", components=None):
        """Approved templates are the only way to message someone outside the 24 h window."""
        self._post({"to": to, "type": "template", "template": {
            "name": name, "language": {"code": language}, **({"components": components} if components else {})}},
            log=f"[out] {to}: <template {name}>")

    def _post(self, payload, log=""):
        """Send via the subclass, then record it in the message log (powers the owner inbox)."""
        ok = self._send(payload, log)
        if ok:
            try:
                kind, body = summarize(payload)
                store.log_message(payload["to"], "out", kind, body, payload)
            except Exception as e:  # logging must never block a reply
                print(f"[error] message log: {e}")
        return ok

    @staticmethod
    def _decorate(interactive, header, footer):
        if header:
            interactive["header"] = {"type": "text", "text": clip(header, LIMITS["header"])}
        if footer:
            interactive["footer"] = {"text": clip(footer, LIMITS["footer"])}


class WhatsAppSender(BaseSender):
    def __init__(self):
        self.base = f"https://graph.facebook.com/{config.GRAPH_VERSION}"
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {config.WHATSAPP_TOKEN}"
        self.typing_supported = True

    def _request(self, method, url, **kw):
        """Retry 429 / 5xx with backoff; log Meta errors as [whatsapp] <code>."""
        for attempt in range(4):
            try:
                r = self.session.request(method, url, timeout=30, **kw)
            except requests.RequestException as e:
                print(f"[whatsapp] network error: {e}")
                time.sleep(2 ** attempt)
                continue
            if r.status_code < 400:
                return r
            print(f"[whatsapp] {r.status_code} {_error_text(r)}")
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            return r
        return None

    def _send(self, payload, log=""):
        if log:
            print(log)
        payload = {"messaging_product": "whatsapp", "recipient_type": "individual", **payload}
        r = self._request("POST", f"{self.base}/{config.WHATSAPP_PHONE_NUMBER_ID}/messages", json=payload)
        return bool(r is not None and r.status_code < 400)

    def mark_read(self, message_id):
        """Blue ticks + 'typing…' while we think. Falls back to read-only if typing isn't accepted."""
        payload = {"messaging_product": "whatsapp", "status": "read", "message_id": message_id}
        url = f"{self.base}/{config.WHATSAPP_PHONE_NUMBER_ID}/messages"
        if self.typing_supported:
            r = self._request("POST", url, json={**payload, "typing_indicator": {"type": "text"}})
            if r is not None and r.status_code < 400:
                return
            self.typing_supported = False
        self._request("POST", url, json=payload)

    def upload(self, path):
        path = Path(path)
        if not path.is_absolute():
            path = config.ROOT / path
        if not path.exists():
            print(f"[error] file not found: {path}")
            return None
        key = f"{path}|{path.stat().st_mtime_ns}|{path.stat().st_size}"
        hit = store.kv_get(f"media:{key}")
        if hit and datetime.fromisoformat(hit["at"]) > config.now() - timedelta(days=MEDIA_CACHE_DAYS):
            return hit["id"]
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            r = self._request("POST", f"{self.base}/{config.WHATSAPP_PHONE_NUMBER_ID}/media",
                              data={"messaging_product": "whatsapp", "type": mime},
                              files={"file": (path.name, f, mime)})
        if r is None or r.status_code >= 400:
            return None
        media_id = r.json().get("id")
        store.kv_set(f"media:{key}", {"id": media_id, "at": config.now().isoformat(timespec="seconds")})
        return media_id

    def subscribe_app(self):
        """Link this app to the WhatsApp Business Account. Without it, messages never arrive."""
        url = f"{self.base}/{config.WHATSAPP_WABA_ID}/subscribed_apps"
        r = self._request("POST", url)
        print("[setup] subscribe:", "ok" if r is not None and r.status_code < 400 else "failed")
        r = self._request("GET", url)
        if r is not None and r.status_code < 400:
            apps = [a.get("whatsapp_business_api_data", {}).get("name") for a in r.json().get("data", [])]
            print(f"[setup] apps subscribed to this WABA: {apps or 'none'}")
            return bool(apps)
        return False


def summarize(payload):
    """(kind, readable body) for the message log."""
    t = payload.get("type")
    if t == "text":
        return "text", payload["text"]["body"]
    if t == "interactive":
        inter = payload["interactive"]
        if inter["type"] == "button":
            opts = [b["reply"]["title"] for b in inter["action"]["buttons"]]
        else:
            opts = [r["title"] for s in inter["action"]["sections"] for r in s["rows"]]
        return inter["type"], f"{inter['body']['text']}\n[{' | '.join(opts)}]"
    if t == "image":
        return "image", payload["image"].get("caption") or "<image>"
    if t == "document":
        return "document", f"<{payload['document'].get('filename', 'file')}>"
    if t == "template":
        return "template", f"<template {payload['template']['name']}>"
    return t or "unknown", ""


def _error_text(r):
    try:
        e = r.json().get("error", {})
        return f"code {e.get('code')}: {e.get('message')} {e.get('error_data', {}).get('details', '')}".strip()
    except ValueError:
        return r.text[:200]


class FakeSender(BaseSender):
    """Records everything instead of calling Meta. Used by `bot.py chat` and the tests."""

    def __init__(self, echo=True):
        self.outbox = []
        self.echo = echo

    def _send(self, payload, log=""):
        self.outbox.append(payload)
        if self.echo and log:
            print(log.replace("[out]", "  bot >", 1))
        return True

    def mark_read(self, message_id):
        pass

    def upload(self, path):
        path = Path(path)
        if not path.is_absolute():
            path = config.ROOT / path
        return f"fake-media:{path.name}" if path.exists() else None

    def last(self, kind=None):
        items = [p for p in self.outbox if kind is None or p["type"] == kind]
        return items[-1] if items else None

    def texts(self):
        return [p["text"]["body"] for p in self.outbox if p["type"] == "text"]
