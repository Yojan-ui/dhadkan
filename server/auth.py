"""Who may do what. Three roles, each with a token in .env:

    DHADKAN_OWNER_TOKEN       owner: approves or rejects bookings, purchases, production decisions
    DHADKAN_TECH_TOKEN        technician: reports a repair done
    DHADKAN_VIEWER_TOKEN      viewer: reads everything, changes nothing

Sign in with POST /api/cc/login {"token": ...}; the server sets an HttpOnly cookie holding a signed
session (HMAC over role + expiry). A Bearer token header works too (scripts, the phone app).

With NO tokens configured the server runs in demo mode: every visitor acts as the owner and the UI
says so ("demo login"). Configure tokens before any real factory uses it.
"""

import base64
import hashlib
import hmac
import os
import secrets
import time
from typing import Any

from fastapi import HTTPException, Request

ROLES = ("owner", "technician", "viewer")
ENV = {"owner": "DHADKAN_OWNER_TOKEN", "technician": "DHADKAN_TECH_TOKEN", "viewer": "DHADKAN_VIEWER_TOKEN"}
COOKIE = "dhadkan_session"
TTL_S = 12 * 3600
_SECRET = os.environ.get("DHADKAN_SESSION_SECRET") or secrets.token_hex(32)


def tokens() -> dict[str, str]:
    return {r: os.environ[v] for r, v in ENV.items() if os.environ.get(v)}


def demo_mode() -> bool:
    return not tokens()


def role_for_token(token: str) -> str | None:
    for role, t in tokens().items():
        if hmac.compare_digest(token.encode(), t.encode()):
            return role
    return None


def session(role: str, now: float | None = None) -> str:
    exp = int((now or time.time()) + TTL_S)
    msg = f"{role}.{exp}"
    sig = hmac.new(_SECRET.encode(), msg.encode(), hashlib.sha256).digest()
    return f"{msg}.{base64.urlsafe_b64encode(sig).decode().rstrip('=')}"


def role_for_session(value: str, now: float | None = None) -> str | None:
    try:
        role, exp_s, sig = value.split(".")
        exp = int(exp_s)
    except ValueError:
        return None  # malformed or tampered cookie
    want = base64.urlsafe_b64encode(
        hmac.new(_SECRET.encode(), f"{role}.{exp_s}".encode(), hashlib.sha256).digest()
    )
    if (
        not hmac.compare_digest(want.decode().rstrip("="), sig)
        or exp < (now or time.time())
        or role not in ROLES
    ):
        return None
    return role


def current(request: Request) -> dict[str, Any]:
    if demo_mode():
        return {"role": "owner", "demo": True, "name": "Owner (demo login)"}
    h = request.headers.get("authorization", "")
    role = role_for_token(h[7:]) if h.lower().startswith("bearer ") else None
    role = role or role_for_session(request.cookies.get(COOKIE, ""))
    if role is None:
        raise HTTPException(401, "sign in")
    return {"role": role, "demo": False, "name": role.capitalize()}


def require(request: Request, *roles: str) -> dict[str, Any]:
    who = current(request)
    if who["role"] not in roles:
        raise HTTPException(403, f"needs the {' or '.join(roles)} role")
    return who
