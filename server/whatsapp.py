"""WhatsApp through the Twilio sandbox (no Facebook account needed).

    send("whatsapp:+91...", "Dhadkan connected")  -> {"sid": "SM...", "status": "queued"}

Credentials come only from the environment / .env: TWILIO_SID, TWILIO_TOKEN, TWILIO_WA_FROM
(sandbox: whatsapp:+17372508034). Without them, nothing is sent: the call returns
{"sid": None, "status": "dry_run"} so the demo and the tests run without an account.
DHADKAN_DRY_RUN=1 forces the dry run even with credentials (rehearsals, recordings).

Template first (Twilio trial / Try WhatsApp senders refuse free text with error 21654 "ContentSid Required"):
with TWILIO_CONTENT_SID set, a refused free-text message is queued for that phone and the approved template
goes out instead, its variables filled from TWILIO_CONTENT_VARS (JSON; "{short}" = a short alert such as
"Loom 4 (Shed A): repair soon", "{time}" = now). When the person replies, the webhook returns the queued full
messages as a TwiML reply (take_pending / reply_to). TWILIO_TEMPLATE_FIRST=1 skips the free-text attempt.
"""

import contextlib
import json
import re
import threading
import time
from collections.abc import Iterator
from contextvars import ContextVar
from functools import cache
from typing import Any
from xml.sax.saxutils import escape

from .notify import env

LIMIT = 1500  # Twilio's WhatsApp body limit is 1600; keep a margin


@cache
def _client(sid: str, token: str) -> Any:
    from twilio.rest import Client

    return Client(sid, token)


def configured() -> bool:
    return bool(env.get("TWILIO_SID") and env.get("TWILIO_TOKEN") and env.get("TWILIO_WA_FROM"))


def wa(number: str) -> str:
    """'+91 98765 43210' or 'whatsapp:+91...' -> 'whatsapp:+919876543210'."""
    raw = number.replace("whatsapp:", "")
    return "whatsapp:+" + "".join(c for c in raw if c.isdigit())


_lock = threading.Lock()
_pending: dict[str, list[str]] = {}  # whatsapp:+91... -> full messages waiting for that person's reply
_capture: ContextVar[tuple[str, list[str]] | None] = ContextVar("wa_capture", default=None)
NEEDS_TEMPLATE = 21654  # Twilio: "ContentSid Required" (free text not allowed for this sender)


def short_alert(text: str) -> str:
    """'[Owner] Loom 4 (Shed A): awaaz badli hai, ...' -> 'Loom 4 (Shed A): awaaz badli hai' (template variable)."""
    t = re.sub(r"^\[[^\]]+\]\s*", "", text.strip())
    t = re.split(r"[.;\n]", t, maxsplit=1)[0].strip()
    return t[:60].rstrip(" ,:") or "Dhadkan alert"


def _template_vars(short: str) -> str:
    raw = env.get("TWILIO_CONTENT_VARS") or '{"1": "{short}"}'
    tpl = json.loads(raw)
    now = time.strftime("%d %b %H:%M")
    return json.dumps({k: str(v).replace("{short}", short).replace("{time}", now) for k, v in tpl.items()},
                      ensure_ascii=False)  # fmt: skip


def _template(to: str, text: str) -> dict[str, Any]:
    """Queue the full text for this phone; send the template only if none is already waiting for a reply."""
    with _lock:
        q = _pending.setdefault(to, [])
        first = not q
        q.append(text[:LIMIT])
    if not first:
        return {"sid": None, "status": "queued_for_reply"}
    msg = _client(env.get("TWILIO_SID") or "", env.get("TWILIO_TOKEN") or "").messages.create(
        from_=env.get("TWILIO_WA_FROM"),
        to=to,
        content_sid=env.get("TWILIO_CONTENT_SID"),
        content_variables=_template_vars(short_alert(text)),
    )
    return {"sid": msg.sid, "status": "template_sent"}


def send(to: str, text: str, media_urls: list[str] | None = None) -> dict[str, Any]:
    if not configured() or env.get("DHADKAN_DRY_RUN") == "1":
        return {"sid": None, "status": "dry_run"}
    to = wa(to)
    cap = _capture.get()
    if cap is not None and cap[0] == to:  # answering this person's own message: goes back in the TwiML reply
        cap[1].append(text[:LIMIT])
        return {"sid": None, "status": "twiml_reply"}
    content = env.get("TWILIO_CONTENT_SID")
    if content and env.get("TWILIO_TEMPLATE_FIRST") == "1":
        return _template(to, text)
    kw: dict[str, Any] = {"from_": env.get("TWILIO_WA_FROM"), "to": to, "body": text[:LIMIT]}
    if media_urls:
        kw["media_url"] = media_urls
    try:
        msg = _client(env.get("TWILIO_SID") or "", env.get("TWILIO_TOKEN") or "").messages.create(**kw)
    except Exception as e:
        if content and getattr(e, "code", None) == NEEDS_TEMPLATE:
            return _template(to, text)
        raise
    return {"sid": msg.sid, "status": msg.status}


def take_pending(phone: str) -> list[str]:
    """Full messages queued behind a template for this phone (emptied)."""
    with _lock:
        return _pending.pop(wa(phone), [])


@contextlib.contextmanager
def reply_to(phone: str) -> Iterator[list[str]]:
    """While handling this person's message, anything sent to them is collected for the TwiML reply."""
    out: list[str] = []
    tok = _capture.set((wa(phone), out))
    try:
        yield out
    finally:
        _capture.reset(tok)


def twiml(messages: list[str]) -> str:
    return "<Response>" + "".join(f"<Message>{escape(m[:LIMIT])}</Message>" for m in messages) + "</Response>"


def status(sid: str) -> str | None:
    """Delivery status of a sent message (queued / sent / delivered / read / failed / undelivered)."""
    if not configured() or not sid:
        return None
    return str(
        _client(env.get("TWILIO_SID") or "", env.get("TWILIO_TOKEN") or "").messages(sid).fetch().status
    )


def download(url: str) -> bytes:
    """A voice note the owner sent (MediaUrl0), fetched with the account's credentials."""
    import httpx

    r = httpx.get(
        url,
        auth=(env.get("TWILIO_SID") or "", env.get("TWILIO_TOKEN") or ""),
        follow_redirects=True,
        timeout=30,
    )
    r.raise_for_status()
    return r.content
