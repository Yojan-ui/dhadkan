"""When an alert goes out: quiet hours, power cuts, and the daily digest.

Per factory (data/sim/cluster.json, assumptions a real factory replaces): quiet_hours ["21:00", "07:00"],
power_cuts [["14:00", "16:00"], ...] (the owner is dealing with the cut, machines are off), digest_at
"19:00". Times are the factory's local clock (in the demo, the simulated clock).

- watch          -> the daily digest (one message a day, never a ping on its own)
- repair_soon    -> now, unless it is quiet hours or a power cut: then held until that ends
- stop_now/replace -> always now. A machine that may destroy itself or hurt someone outranks quiet
                    hours; the owner still decides, and the agent has no stop tool.
"""

from datetime import datetime, time, timedelta
from typing import Any

TEMPLATES = {
    "watch": "alert_watch",
    "repair_soon": "alert_repair",
    "stop_now": "alert_stop",
    "replace": "alert_stop",
}
DIGEST_TEMPLATE = "daily_digest"
# Reply buttons (WhatsApp allows 3, at most 20 characters each); ids 1/2/3 = book / later / details.
BUTTONS = {
    "hi": ["मिस्त्री बुलाओ", "कल", "पूरी जानकारी"],
    "hi_latn": ["Mechanic bulao", "Kal", "Details"],
    "mr": ["मिस्त्री बोलवा", "उद्या", "माहिती"],
    "gu": ["મિસ્ત્રી બોલાવો", "કાલે", "માહિતી"],
    "en": ["Send mechanic", "Later", "Details"],
}
DIGEST_HEAD = {
    "hi": "आज की मशीन रिपोर्ट (ध्यान दें):", "hi_latn": "Aaj ki machine report (dhyan do):",
    "mr": "आजचा मशीन अहवाल (लक्ष ठेवा):", "gu": "આજનો મશીન રિપોર્ટ (ધ્યાન રાખો):",
    "en": "Today's machine report (watch):",
}  # fmt: skip
DEFAULTS = {"quiet_hours": ["21:00", "07:00"], "power_cuts": [], "digest_at": "19:00"}


def _t(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _in(now: datetime, start: str, end: str) -> datetime | None:
    """If `now` is inside [start, end) (may wrap midnight), the datetime it ends; else None."""
    a, b = _t(start), _t(end)
    t = now.time()
    inside = a <= t < b if a < b else (t >= a or t < b)
    if not inside:
        return None
    end_dt = datetime.combine(now.date(), b)
    return end_dt if end_dt > now else end_dt + timedelta(days=1)


def next_digest(now: datetime, profile: dict[str, Any]) -> datetime:
    at = datetime.combine(now.date(), _t(profile.get("digest_at", DEFAULTS["digest_at"])))
    return at if at > now else at + timedelta(days=1)


def decide(severity: str, now: datetime, profile: dict[str, Any]) -> tuple[str, datetime | None]:
    """('send', None) | ('hold', until) | ('digest', at)."""
    if severity in ("stop_now", "replace"):
        return "send", None
    if severity == "watch":
        return "digest", next_digest(now, profile)
    windows = [profile.get("quiet_hours", DEFAULTS["quiet_hours"]), *profile.get("power_cuts", [])]
    ends = [e for w in windows if w and (e := _in(now, w[0], w[1])) is not None]
    if ends:
        return "hold", max(ends)
    return "send", None
