"""WhatsApp and SMS providers behind one interface, plus webhook checks.

    MetaCloud        Meta WhatsApp Cloud API (graph.facebook.com), e.g. the free test number
    TwilioWhatsApp   Twilio WhatsApp sandbox (fallback)
    TwilioSMS        plain SMS (fallback when WhatsApp is unreachable)
    DryRun           no keys configured: records what would have been sent, sends nothing

Every provider's send(to, text, media) returns {"provider", "ids"} or raises:
    ConnectionError  the network or provider is unreachable (the alert is queued and retried)
    ValueError       the provider refused it (bad number, template not approved, ...): not retried

Keys are read from the environment / .env (see .env.example); nothing is hard-coded.
"""

import base64
import hashlib
import hmac
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from . import env


@dataclass
class Media:
    path: Path
    mime: str  # audio/ogg (Opus) or image/png
    caption: str = ""
    public_url: str | None = None  # needed by Twilio, which fetches media itself


class Provider(Protocol):
    name: str

    def send(self, to: str, text: str, media: list[Media]) -> dict[str, Any]: ...


def _number(to: str) -> str:
    """'+91 98xxx-xxxxx' -> '9198xxxxxxxx' (digits only, country code included)."""
    digits = "".join(ch for ch in to if ch.isdigit())
    if len(digits) < 10:
        raise ValueError(f"not a phone number: {to!r}")
    return digits


def _post(client: httpx.Client, url: str, **kw: Any) -> dict[str, Any]:
    try:
        r = client.post(url, **kw)
    except httpx.TransportError as e:
        raise ConnectionError(str(e)) from e
    if r.status_code >= 500 or r.status_code == 429:
        raise ConnectionError(f"provider error {r.status_code}")
    if r.status_code >= 400:
        raise ValueError(f"provider refused: {r.status_code} {r.text[:300]}")
    data: dict[str, Any] = r.json()
    return data


# ---------------------------------------------------------------- Meta WhatsApp Cloud API
def text_payload(to: str, text: str) -> dict[str, Any]:
    return {"messaging_product": "whatsapp", "recipient_type": "individual", "to": _number(to),
            "type": "text", "text": {"preview_url": False, "body": text}}  # fmt: skip


def template_payload(to: str, name: str, lang: str, params: list[str]) -> dict[str, Any]:
    """An approved template with body variables {{1}}, {{2}}, ... (needed outside the 24 h window)."""
    return {
        "messaging_product": "whatsapp",
        "to": _number(to),
        "type": "template",
        "template": {
            "name": name,
            "language": {"code": lang},
            "components": [{"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}],
        },
    }


def interactive_payload(to: str, text: str, buttons: list[str]) -> dict[str, Any]:
    """Reply buttons (inside the 24 h session); ids 1/2/3 are what parse_choice reads back."""
    return {"messaging_product": "whatsapp", "recipient_type": "individual", "to": _number(to), "type": "interactive",
            "interactive": {"type": "button", "body": {"text": text[:1024]}, "action": {"buttons": [
                {"type": "reply", "reply": {"id": str(i), "title": b[:20]}} for i, b in enumerate(buttons[:3], 1)]}}}  # fmt: skip


META_LANG = {"hi": "hi", "mr": "mr", "gu": "gu", "en": "en", "hi_latn": "en"}  # template language buckets


def media_payload(to: str, kind: str, media_id: str, caption: str = "") -> dict[str, Any]:
    body: dict[str, Any] = {"id": media_id}
    if caption and kind == "image":
        body["caption"] = caption
    return {"messaging_product": "whatsapp", "to": _number(to), "type": kind, kind: body}


class MetaCloud:
    name = "whatsapp_meta"

    def __init__(self, token: str, phone_id: str, version: str = "v21.0", template: str | None = None,
                 client: httpx.Client | None = None) -> None:  # fmt: skip
        self.root = f"https://graph.facebook.com/{version}"
        self.base = f"{self.root}/{phone_id}"
        self.template = template
        self.http = client or httpx.Client(timeout=20, headers={"Authorization": f"Bearer {token}"})

    def upload(self, m: Media) -> str:
        files = {"file": (m.path.name, m.path.read_bytes(), m.mime)}
        data = {"messaging_product": "whatsapp", "type": m.mime}
        return str(_post(self.http, f"{self.base}/media", data=data, files=files)["id"])

    def download(self, audio: dict[str, Any]) -> bytes:
        """A voice note the owner sent: media id -> URL (Graph API) -> bytes, both with the token."""
        r = self.http.get(f"{self.root}/{audio['id']}")
        r.raise_for_status()
        got = self.http.get(r.json()["url"])
        got.raise_for_status()
        return got.content

    def send(self, to: str, text: str, media: list[Media], lang: str = "en", template: str | None = None,
             buttons: list[str] | None = None) -> dict[str, Any]:  # fmt: skip
        """Approved template when WA_TEMPLATES=1 (needed outside the 24 h window), else buttons, else text."""
        name = template or self.template
        if name and env.get("WA_TEMPLATES") == "1":
            payload = template_payload(to, name, META_LANG.get(lang, "en"), [text])
        elif buttons:
            payload = interactive_payload(to, text, buttons)
        else:
            payload = text_payload(to, text)
        ids = [_post(self.http, f"{self.base}/messages", json=payload)["messages"][0]["id"]]
        for m in media:
            kind = "audio" if m.mime.startswith("audio/") else "image"
            msg = media_payload(to, kind, self.upload(m), m.caption)
            ids.append(_post(self.http, f"{self.base}/messages", json=msg)["messages"][0]["id"])
        return {"provider": self.name, "ids": ids}


# ---------------------------------------------------------------- Twilio
class _Twilio:
    def __init__(self, sid: str, token: str, sender: str, client: httpx.Client | None = None) -> None:
        self.url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
        self.sender = sender
        self.http = client or httpx.Client(timeout=20, auth=(sid, token))

    def download(self, audio: dict[str, Any]) -> bytes:
        """A voice note from Twilio: MediaUrl0 is fetched with the account's basic auth."""
        r = self.http.get(audio["url"], follow_redirects=True)
        r.raise_for_status()
        return r.content

    def _send(self, to: str, body: str, media_urls: list[str]) -> str:
        data: dict[str, str | list[str]] = {"From": self.sender, "To": to, "Body": body}
        if media_urls:
            data["MediaUrl"] = media_urls  # repeated form field
        return str(_post(self.http, self.url, data=data)["sid"])


class TwilioWhatsApp(_Twilio):
    name = "whatsapp_twilio"

    def send(self, to: str, text: str, media: list[Media], lang: str = "en", **_: Any) -> dict[str, Any]:
        urls = [m.public_url for m in media if m.public_url]  # Twilio fetches media from a public URL
        return {"provider": self.name, "ids": [self._send(f"whatsapp:+{_number(to)}", text, urls)]}


class TwilioSMS(_Twilio):
    name = "sms_twilio"

    def send(self, to: str, text: str, media: list[Media], lang: str = "en", **_: Any) -> dict[str, Any]:
        return {"provider": self.name, "ids": [self._send(f"+{_number(to)}", text, [])]}


class DryRun:
    """No keys: nothing leaves the machine; the alert log shows what would have been sent."""

    name = "dry_run"

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(self, to: str, text: str, media: list[Media], lang: str = "en", **_: Any) -> dict[str, Any]:
        self.sent.append({"to": to, "text": text, "media": [str(m.path) for m in media]})
        return {"provider": self.name, "ids": []}


def from_env() -> tuple[Any, Any]:
    """(WhatsApp provider, SMS provider) from configuration; DryRun where keys are missing.

    DHADKAN_DRY_RUN=1 forces DryRun for both even when keys are set (recordings, rehearsals)."""
    if env.get("DHADKAN_DRY_RUN") == "1":
        return DryRun(), DryRun()
    wa: Any = DryRun()
    if env.get("WA_TOKEN") and env.get("WA_PHONE_NUMBER_ID"):
        wa = MetaCloud(env.get("WA_TOKEN") or "", env.get("WA_PHONE_NUMBER_ID") or "",
                       env.get("WA_API_VERSION", "v21.0") or "v21.0", env.get("WA_TEMPLATE"))  # fmt: skip
    elif env.get("TWILIO_ACCOUNT_SID") and env.get("TWILIO_AUTH_TOKEN"):
        wa = TwilioWhatsApp(env.get("TWILIO_ACCOUNT_SID") or "", env.get("TWILIO_AUTH_TOKEN") or "",
                            env.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886") or "")  # fmt: skip
    sms: Any = DryRun()
    if env.get("TWILIO_ACCOUNT_SID") and env.get("TWILIO_AUTH_TOKEN") and env.get("TWILIO_SMS_FROM"):
        sms = TwilioSMS(env.get("TWILIO_ACCOUNT_SID") or "", env.get("TWILIO_AUTH_TOKEN") or "",
                        env.get("TWILIO_SMS_FROM") or "")  # fmt: skip
    return wa, sms


# ---------------------------------------------------------------- webhooks
def verify_meta_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    """X-Hub-Signature-256: sha256=<hex HMAC of the raw body with the app secret>."""
    if not header or not header.startswith("sha256="):
        return False
    want = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, header[7:])


def verify_twilio_signature(auth_token: str, url: str, params: dict[str, str], header: str | None) -> bool:
    """X-Twilio-Signature: base64 HMAC-SHA1 of the URL followed by each sorted key and value."""
    if not header:
        return False
    s = url + "".join(k + params[k] for k in sorted(params))
    want = base64.b64encode(hmac.new(auth_token.encode(), s.encode(), hashlib.sha1).digest()).decode()
    return hmac.compare_digest(want, header)


def parse_meta(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Incoming WhatsApp messages from a Meta webhook body -> [{from, text, id}]."""
    out = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for m in change.get("value", {}).get("messages", []) or []:
                kind = m.get("type")
                text = (
                    m.get("text", {}).get("body")
                    if kind == "text"
                    else (
                        m.get("button", {}).get("text")
                        if kind == "button"
                        else (
                            (
                                m.get("interactive", {}).get("button_reply", {}).get("id")
                                or m.get("interactive", {}).get("button_reply", {}).get("title")
                            )
                            if kind == "interactive"
                            else None
                        )
                    )
                )
                if text is not None:
                    out.append({"from": m.get("from", ""), "text": text, "id": m.get("id", "")})
                elif kind in ("audio", "voice") and m.get(kind, {}).get("id"):  # a voice note
                    out.append({"from": m.get("from", ""), "text": None, "id": m.get("id", ""),
                                "audio": {"id": m[kind]["id"], "mime": m[kind].get("mime_type", "audio/ogg")}})  # fmt: skip
    return out


def parse_twilio(form: dict[str, str]) -> list[dict[str, Any]]:
    who = form.get("From", "").replace("whatsapp:", "")
    if not who:
        return []
    row: dict[str, Any] = {"from": who, "text": form.get("Body", ""), "id": form.get("MessageSid", "")}
    if form.get("NumMedia", "0") != "0" and form.get("MediaContentType0", "").startswith("audio/"):
        row["audio"] = {"url": form.get("MediaUrl0", ""), "mime": form["MediaContentType0"]}
        row["text"] = None
    return [row]
