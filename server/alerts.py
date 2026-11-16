"""Owner-facing text, written the way a mechanic talks. Hindi, Marathi, English.

The agent decides *what* to say (which machine, which finding, which day);
these templates decide *how*, so the wording is ours and checked by people,
not machine-translated on the fly.
"""

import re

FAULT = {
    "hi": {"bearing": "बेयरिंग", "imbalance": "बैलेंस", "loose_belt": "बेल्ट", "clog": "कचरा फँसने"},
    "mr": {"bearing": "बेअरिंग", "imbalance": "बॅलन्स", "loose_belt": "बेल्ट", "clog": "कचरा अडकण्या"},
    "en": {"bearing": "bearing", "imbalance": "imbalance", "loose_belt": "belt", "clog": "clogging"},
}
DAY_ON = {"mr": ["सोमवारी", "मंगळवारी", "बुधवारी", "गुरुवारी", "शुक्रवारी", "शनिवारी", "रविवारी"]}
DAY = {
    "hi": ["सोमवार", "मंगलवार", "बुधवार", "गुरुवार", "शुक्रवार", "शनिवार", "रविवार"],
    "mr": ["सोमवार", "मंगळवार", "बुधवार", "गुरुवार", "शुक्रवार", "शनिवार", "रविवार"],
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
}

T = {
    "rising": {
        "hi": "{name}: {what} {days} बढ़ रही है। {day} से पहले जाँच करवा लें।",
        "mr": "{name}: {what} {days} वाढतोय. {day}च्या आधी तपासून घ्या.",
        "en": "{name}: {what} rising {days}. Check before {day}.",
    },
    "match": {
        "hi": "{name}: आवाज़ वैसी हो रही है जैसी {area} की एक मशीन में {fault} ख़राब होने से पहले थी। अभी अलार्म नहीं है, {day} से पहले एक बार दिखा लें।",
        "mr": "{name}: {area}मधल्या एका मशीनमध्ये {fault} खराब होण्याआधी जसा आवाज होता, तसा आवाज येतोय. अजून अलार्म नाही, {day}च्या आधी एकदा दाखवून घ्या.",
        "en": "{name}: sounds like a machine in {area} did before its {fault} failed. No alarm yet; have it looked at before {day}.",
    },
    "ask": {
        "hi": "{tech} को {day} को भेजें? जवाब दें: हाँ / ना",
        "mr": "{tech}ला {day_on} पाठवू का? उत्तर द्या: हो / नाही",
        "en": "Send {tech} on {day}? Reply YES / NO",
    },
}


def _what(lang: str, fault: str | None) -> str:
    f = FAULT[lang].get(fault)
    if lang == "hi":
        return f"{f} की आवाज़" if f else "आवाज़"
    if lang == "mr":
        return f"{f}चा आवाज" if f else "आवाज"
    return f"{f} sound" if f else "sound"


def _days(lang: str, n: int) -> str:
    if n <= 1:
        return {"hi": "आज से", "mr": "आजपासून", "en": "since today"}[lang]
    return {"hi": f"{n} दिन से", "mr": f"{n} दिवसांपासून", "en": f"for {n} days"}[lang]


def draft(
    lang: str,
    kind: str,
    name: str,
    day_index: int,
    fault: str | None = None,
    days: int = 0,
    area: str = "",
    tech: str = "mistri",
) -> str:
    """kind: rising | match. day_index: 0=Monday. Returns the alert + yes/no question."""
    if lang not in T["rising"]:
        lang = "hi"
    day = DAY[lang][day_index % 7]
    body = T[kind][lang].format(
        name=name,
        what=_what(lang, fault),
        days=_days(lang, days),
        day=day,
        area=area,
        fault=FAULT[lang].get(fault, ""),
    )
    if kind == "match" and not fault:
        body = re.sub(r"\s+", " ", body)
    day_on = DAY_ON.get(lang, DAY[lang])[day_index % 7]
    return body + " " + T["ask"][lang].format(tech=tech, day=day, day_on=day_on)


YES = {"1", "हाँ", "हां", "हो", "haan", "han", "ha", "ho", "yes", "y", "ok", "ठीक"}  # 1 = send mechanic
NO = {"2", "ना", "नहीं", "नही", "नाही", "नको", "na", "nahi", "nahin", "nako", "no", "n"}  # 2 = later


def parse_reply(text: str) -> bool | None:
    """Owner's reply -> True (yes), False (no) or None (unclear: ask again)."""
    words = re.findall(r"[\wऀ-ॿ]+", text.lower())
    yes = any(w in YES for w in words)
    no = any(w in NO for w in words)
    return None if yes == no else yes
