"""WhatsApp through the Twilio sandbox (no Facebook account needed).

    send("whatsapp:+91...", "Dhadkan connected")  -> {"sid": "SM...", "status": "queued"}

Credentials come only from the environment / .env: TWILIO_SID, TWILIO_TOKEN, TWILIO_WA_FROM
(sandbox: whatsapp:+14155238886). Without them, nothing is sent: the call returns
{"sid": None, "status": "dry_run"} so the demo and the tests run without an account.
DHADKAN_DRY_RUN=1 forces the dry run even with credentials (rehearsals, recordings).
"""

from functools import cache
from typing import Any

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


def send(to: str, text: str, media_urls: list[str] | None = None) -> dict[str, Any]:
    if not configured() or env.get("DHADKAN_DRY_RUN") == "1":
        return {"sid": None, "status": "dry_run"}
    kw: dict[str, Any] = {"from_": env.get("TWILIO_WA_FROM"), "to": wa(to), "body": text[:LIMIT]}
    if media_urls:
        kw["media_url"] = media_urls
    msg = _client(env.get("TWILIO_SID") or "", env.get("TWILIO_TOKEN") or "").messages.create(**kw)
    return {"sid": msg.sid, "status": msg.status}


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
