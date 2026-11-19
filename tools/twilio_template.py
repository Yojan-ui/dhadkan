"""Create Dhadkan's own Twilio WhatsApp template, so alerts arrive in Dhadkan's words.

    python -m tools.twilio_template            # create it, ask WhatsApp to approve it, save it in .env, send a test
    python -m tools.twilio_template --status   # check whether WhatsApp has approved it yet, and send a test

Reads TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_WHATSAPP_FROM / DHADKAN_DEMO_TO from .env. The template is
"Dhadkan alert: {{1}}" (category Utility); Dhadkan puts each alert's text into {{1}}. It writes
TWILIO_CONTENT_SID=<HX...> into .env and empties TWILIO_CONTENT_VARIABLES (so {{1}} carries the alert).
Some Twilio senders deliver a new template only after WhatsApp approves it (minutes to a day): run --status later.
"""

import argparse
import re
import sys
from typing import Any

import httpx

from server.notify import env, whatsapp

CONTENT = "https://content.twilio.com/v1/Content"
NAME = "dhadkan_alert"
BODY = "Dhadkan alert: {{1}}"
SAMPLE = "Loom 1 - the motor bearing is wearing out. Fix now ~Rs 315-680. Reply 1 = send mechanic, 2 = later."


def _client() -> httpx.Client:
    sid, token = env.get("TWILIO_ACCOUNT_SID"), env.get("TWILIO_AUTH_TOKEN")
    if not sid or not token:
        sys.exit("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set in .env (and the file saved).")
    return httpx.Client(timeout=30, auth=(sid, token))


def _ok(r: httpx.Response) -> dict[str, Any]:
    if r.status_code >= 400:
        sys.exit(f"Twilio said {r.status_code}: {r.text[:400]}")
    out: dict[str, Any] = r.json()
    return out


def set_env(updates: dict[str, str]) -> None:
    """Change (or add) KEY=value lines in .env, keeping everything else as it is."""
    path = env.ROOT / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    for key, value in updates.items():
        pat = re.compile(rf"^\s*{re.escape(key)}\s*=")
        hit = [i for i, line in enumerate(lines) if pat.match(line)]
        if hit:
            lines[hit[0]] = f"{key}={value}"
        else:
            lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    env._dotenv.cache_clear()


def create(http: httpx.Client) -> str:
    for c in _ok(http.get(CONTENT, params={"PageSize": 100})).get("contents", []):
        if c.get("friendly_name") == NAME:
            print(f"Template already exists: {c['sid']}")
            return str(c["sid"])
    body = {"friendly_name": NAME, "language": "en", "variables": {"1": SAMPLE},
            "types": {"twilio/text": {"body": BODY}}}  # fmt: skip
    sid = str(_ok(http.post(CONTENT, json=body))["sid"])
    print(f'Created template {sid}: "{BODY}"')
    return sid


def request_approval(http: httpx.Client, sid: str) -> None:
    r = http.post(f"{CONTENT}/{sid}/ApprovalRequests/whatsapp", json={"name": NAME, "category": "UTILITY"})
    if r.status_code < 400:
        print("Asked WhatsApp to approve it (category Utility).")
    else:
        print(f"Approval request not accepted ({r.status_code}): {r.text[:300]}")


def status(http: httpx.Client, sid: str) -> str:
    r = http.get(f"{CONTENT}/{sid}/ApprovalRequests")
    if r.status_code >= 400:
        return f"unknown ({r.status_code})"
    wa = r.json().get("whatsapp") or {}
    st = str(wa.get("status") or "not submitted")
    return st + (f" ({wa['rejection_reason']})" if wa.get("rejection_reason") else "")


def test_send(sid: str) -> None:
    to = env.get("DHADKAN_DEMO_TO")
    if not to:
        print("Set DHADKAN_DEMO_TO=+91... in .env to get a test message.")
        return
    t = whatsapp.TwilioWhatsApp(env.get("TWILIO_ACCOUNT_SID") or "", env.get("TWILIO_AUTH_TOKEN") or "",
                                env.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886") or "", content_sid=sid)  # fmt: skip
    try:
        res = t.send(to, "TEST " + SAMPLE, [])
        print(
            f"Test message sent to {to} ({res['ids'][0]}). Check WhatsApp: it should start with 'Dhadkan alert:'."
        )
    except (ValueError, ConnectionError) as e:
        print(f"Test message not sent yet: {e}")
        print("If WhatsApp hasn't approved the template yet, run this again later with --status.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--status", action="store_true", help="check approval and send a test")
    args = ap.parse_args()
    http = _client()
    if args.status:
        sid = env.get("TWILIO_CONTENT_SID") or create(http)
    else:
        sid = create(http)
        request_approval(http, sid)
        set_env({"TWILIO_CONTENT_SID": sid, "TWILIO_CONTENT_VARIABLES": ""})
        print(f"Saved in .env: TWILIO_CONTENT_SID={sid} (and emptied TWILIO_CONTENT_VARIABLES).")
    print(f"WhatsApp approval: {status(http, sid)}")
    test_send(sid)


if __name__ == "__main__":
    main()
