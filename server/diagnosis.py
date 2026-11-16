"""Diagnosis cards: what is wrong, how bad, can it be fixed, what it costs, from server/faults.yaml.

No free text is generated: every sentence comes from faults.yaml (reviewed by people) and every
rupee from it or from the owner's cost profile (data/sim/costs.json). Prices are typical, not quotes.
"""

from functools import cache
from typing import Any

import yaml

from . import costs, forecast
from .config import FAULTS

LANGS = ("en", "hi", "mr", "hi_latn")  # hi_latn: Hindi in Latin script, as many owners type on WhatsApp
SEVERITIES = ("watch", "repair_soon", "stop_now", "replace")
WAIT_DAYS = 7  # "if you wait a week"

REQUIRED_FAULT_KEYS = ("name", "what", "repairable", "fix", "misuse", "warranty", "if_ignored")


class KnowledgeBaseError(ValueError):
    pass


@cache
def load() -> dict[str, Any]:
    kb = yaml.safe_load(FAULTS.read_text(encoding="utf-8"))
    validate(kb)
    return kb


def validate(kb: dict[str, Any]) -> None:
    """Schema check: every fault complete, trilingual owner text, every cited source present."""
    srcs = kb.get("sources", {})
    for sid, s in srcs.items():
        if not {"title", "url", "accessed", "supports"} <= set(s):
            raise KnowledgeBaseError(f"source {sid} is missing a field")
    if list(kb["severity"]["order"]) != list(SEVERITIES):
        raise KnowledgeBaseError("severity order must be watch, repair_soon, stop_now, replace")
    for lvl in SEVERITIES:
        if set(kb["severity"]["labels"][lvl]) != set(LANGS):
            raise KnowledgeBaseError(f"severity {lvl} needs en/hi/mr/hi_latn labels")

    def cited(sid: str | None, where: str) -> None:
        if sid is not None and sid not in srcs:
            raise KnowledgeBaseError(f"{where} cites unknown source {sid}")

    for key, f in kb["faults"].items():
        missing = [k for k in REQUIRED_FAULT_KEYS if k not in f]
        if missing:
            raise KnowledgeBaseError(f"fault {key} is missing {missing}")
        for field in ("name", "what"):
            if set(f[field]) != set(LANGS):
                raise KnowledgeBaseError(f"fault {key}.{field} needs en/hi/mr/hi_latn")
        fix = f["fix"]
        if set(fix["part"]) != set(LANGS) or set(fix["technician"]) != set(LANGS):
            raise KnowledgeBaseError(f"fault {key}.fix needs en/hi/mr/hi_latn part and technician")
        price = fix.get("price_inr")
        if price is not None and not (len(price) == 2 and 0 < price[0] <= price[1]):
            raise KnowledgeBaseError(f"fault {key}.fix.price_inr must be [low, high]")
        if (price is not None or "price_inr_per_hp" in fix) and fix.get("price_source") is None:
            raise KnowledgeBaseError(f"fault {key}: a price needs a source")
        cited(fix.get("price_source"), f"{key}.fix")
        cited(fix.get("also_source"), f"{key}.fix")
        for m in f["misuse"]:
            cited(m.get("source"), f"{key}.misuse")
        cited(f["warranty"].get("source"), f"{key}.warranty")
        if f["warranty"].get("period") and f["warranty"].get("source") is None:
            raise KnowledgeBaseError(f"fault {key}: a warranty period needs a source")
        if f["if_ignored"]["cost_basis"] not in ("rewind", "bearing", "part"):
            raise KnowledgeBaseError(f"fault {key}: unknown cost_basis")
        if set(f["if_ignored"]["what"]) != set(LANGS):
            raise KnowledgeBaseError(f"fault {key}.if_ignored.what needs en/hi/mr/hi_latn")
    for kind, table in kb["naming"].items():
        for src, fault in table.items():
            if fault not in kb["faults"]:
                raise KnowledgeBaseError(f"naming.{kind}.{src} points at unknown fault {fault}")


def severity(status: str, est: dict[str, Any] | None, days_at_fault: float = 0.0) -> str | None:
    """Map the anomaly status and the days-to-fault-level estimate to a severity (None = no card)."""
    r = load()["severity"]["rules"]
    est = est or {}
    if est.get("status") == "at_fault_level" and days_at_fault >= r["replace_after_days_at_fault"]:
        return "replace"
    low = est.get("low") if est.get("status") == "estimate" else None
    if est.get("status") == "at_fault_level" or (low is not None and low <= r["stop_now_within_days"]):
        return "stop_now"
    if status in ("alarm", "early_match") or (low is not None and low <= r["repair_soon_within_days"]):
        return "repair_soon"
    if status == "watch" or low is not None:
        return "watch"
    return None


def fault_for(owner_word: str | None = None, classifier: str | None = None) -> str | None:
    """Name the fault from the CWRU-trained classifier (vibration) or the owner's word (weak label)."""
    naming = load()["naming"]
    if classifier and classifier in naming["cwru_classifier"]:
        return naming["cwru_classifier"][classifier]
    if owner_word and owner_word in naming["owner_word"]:
        return naming["owner_word"][owner_word]
    return None


def _fix_cost(f: dict[str, Any], hp: float, rate: float) -> tuple[int, int] | None:
    fix = f["fix"]
    if "price_inr_per_hp" in fix:
        part = (fix["price_inr_per_hp"] * hp,) * 2
    elif fix.get("price_inr"):
        part = tuple(fix["price_inr"])
    else:
        part = (0, 0)
    lab = fix["labour_hours"]
    return round(part[0] + lab[0] * rate), round(part[1] + lab[1] * rate)


def _ignored_cost(f: dict[str, Any], kb: dict[str, Any], hp: float, rate: float) -> int:
    basis = f["if_ignored"]["cost_basis"]
    if basis == "rewind":
        return round(kb["faults"]["motor_winding"]["fix"]["price_inr_per_hp"] * hp)
    if basis == "bearing":
        bw = kb["faults"]["bearing_wear"]["fix"]
        return round(2 * bw["price_inr"][1] + bw["labour_hours"][1] * rate)
    fc = _fix_cost(f, hp, rate)
    return fc[1] if fc else 0


def card(
    machine: dict[str, Any], factory: str, fault: str | None, hp: float, service_h: float, lang: str = "en"
) -> dict[str, Any] | None:
    """One diagnosis card for a machine row from Live.state() (needs status, forecast, days_rising)."""
    kb = load()
    est = machine.get("forecast") or {}
    sev = severity(machine.get("status", "ok"), est, machine.get("days_at_fault", 0.0))
    if sev is None:
        return None
    lang = lang if lang in LANGS else "en"
    c = costs.load()
    rate = c["technician_rate_inr_per_hour"]
    out: dict[str, Any] = {
        "machine": machine["id"],
        "severity": {"level": sev, "label": kb["severity"]["labels"][sev]},
        "disclaimer": kb["disclaimer"],
        "fault": fault,
    }
    if fault is None:
        out["unknown"] = {
            "en": "The sound has changed but the fault is not named yet. A mechanic should look at it.",
            "hi": "आवाज़ बदली है, पर खराबी का नाम अभी पता नहीं। मिस्त्री को दिखाएँ।",
            "mr": "आवाज बदलला आहे, पण बिघाड अजून कळलेला नाही. मिस्त्रीला दाखवा.",
            "hi_latn": "Aawaz badli hai, par kharabi ka naam abhi pata nahi. Mistri ko dikhayein.",
        }
        return out
    f = kb["faults"][fault]
    fix_now = _fix_cost(f, hp, rate)
    ignored = _ignored_cost(f, kb, hp, rate)
    down = costs.breakdown(factory, machine["type"], machine.get("known_fault"), service_h)
    p_week = forecast.p_before({**est, "crossings": est.get("crossings", [])}, WAIT_DAYS) if est else 0.0
    planned = down["planned_service_h"] * c["output_per_hour_inr"][factory]
    unplanned = down["unplanned_downtime_h"] * c["output_per_hour_inr"][factory]
    out.update(
        name=f["name"],
        what=f["what"],
        repairable=f["repairable"],
        fix={
            **{k: f["fix"][k] for k in ("part", "technician", "labour_hours", "price_unit")},
            "price_inr": f["fix"].get("price_inr")
            or ([round(f["fix"]["price_inr_per_hp"] * hp)] * 2 if "price_inr_per_hp" in f["fix"] else None),
            "total_inr": list(fix_now) if fix_now else None,
            "sources": [s for s in (f["fix"].get("price_source"), f["fix"].get("also_source")) if s],
        },
        misuse=f["misuse"],
        warranty=f["warranty"],
        if_ignored={"what": f["if_ignored"]["what"], "repair_inr": ignored},
        money={
            "fix_now_inr": [fix_now[0] + round(planned), fix_now[1] + round(planned)],
            "wait_days": WAIT_DAYS,
            "p_fault_within_wait": round(p_week, 2),
            "breakdown_inr": ignored + round(unplanned),
            "expected_if_wait_inr": round(p_week * (ignored + unplanned)),
            "basis": "fix now = part + labour at the assumed technician rate + planned downtime; "
            "breakdown = repair after failure + unplanned downtime (data/sim/costs.json, assumptions)",
        },
        days=(
            est.get("low")
            if est.get("status") == "estimate"
            else (0 if est.get("status") == "at_fault_level" else None)
        ),
    )
    return out


def _rs(lo: int, hi: int | None = None) -> str:
    fmt = lambda v: f"{round(v):,}"  # noqa: E731
    return f"₹{fmt(lo)}" if hi is None or hi == lo else f"₹{fmt(lo)}-{fmt(hi)}"


TEMPLATE = {
    "hi": "⚠ {sev} · {name} – {what} {rising}अभी ठीक हो सकता है: {part}, ~{price}, {hours} घंटा। {ignore}"
    "जवाब दें: 1 = मिस्त्री बुलाओ, 2 = बाद में, 3 = पूरी जानकारी। ({disc})",
    "mr": "⚠ {sev} · {name} – {what} {rising}आत्ता दुरुस्त होऊ शकते: {part}, ~{price}, {hours} तास. {ignore}"
    "उत्तर द्या: 1 = मिस्त्री बोलवा, 2 = नंतर, 3 = सविस्तर माहिती. ({disc})",
    "en": "⚠ {sev} · {name} – {what} {rising}Fixable now: {part}, ~{price}, {hours} h. {ignore}"
    "Reply 1 = send mechanic, 2 = later, 3 = details. ({disc})",
    "hi_latn": "⚠ {sev} · {name} – {what} {rising}Abhi theek ho sakta hai: {part}, ~{price}, {hours} ghanta. {ignore}"
    "Reply 1 = mistri bulao, 2 = baad mein, 3 = poori jaankari. ({disc})",
}
RISING = {
    "hi": "{n} दिन से आवाज़ बढ़ रही है। ",
    "mr": "{n} दिवसांपासून आवाज वाढतोय. ",
    "en": "Sound rising for {n} days. ",
    "hi_latn": "{n} din se aawaz badh rahi hai. ",
}
IGNORE_DAYS = {
    "hi": "{d} दिन से ज़्यादा चलाया तो {what} (~{cost})। ",
    "mr": "{d} दिवसांपेक्षा जास्त चालवले तर {what} (~{cost}). ",
    "en": "Run it more than {d} days and {what} (~{cost}). ",
    "hi_latn": "{d} din se zyada chalaya to {what} (~{cost}). ",
}
IGNORE = {
    "hi": "चलाते रहे तो {what} (~{cost})। ",
    "mr": "चालू ठेवले तर {what} (~{cost}). ",
    "en": "Keep running it and {what} (~{cost}). ",
    "hi_latn": "Chalate rahe to {what} (~{cost}). ",
}
UNKNOWN = {
    "hi": "⚠ {sev} · {name} – {text} जवाब दें: 1 = मिस्त्री बुलाओ, 2 = बाद में, 3 = पूरी जानकारी।",
    "mr": "⚠ {sev} · {name} – {text} उत्तर द्या: 1 = मिस्त्री बोलवा, 2 = नंतर, 3 = सविस्तर माहिती.",
    "en": "⚠ {sev} · {name} – {text} Reply 1 = send mechanic, 2 = later, 3 = details.",
    "hi_latn": "⚠ {sev} · {name} – {text} Reply 1 = mistri bulao, 2 = baad mein, 3 = poori jaankari.",
}


def message(c: dict[str, Any], machine_name: str, days_rising: int, lang: str) -> str:
    """The WhatsApp alert body (template style) in the owner's language."""
    lang = lang if lang in LANGS else "en"
    sev = c["severity"]["label"][lang]
    if c.get("fault") is None:
        return UNKNOWN[lang].format(sev=sev, name=machine_name, text=c["unknown"][lang])
    price = c["fix"]["total_inr"]  # part + labour at the assumed technician rate
    hours = c["fix"]["labour_hours"]
    hours_txt = f"{hours[0]:g}" if hours[0] == hours[1] else f"{hours[0]:g}-{hours[1]:g}"
    cost = _rs(c["if_ignored"]["repair_inr"])
    what_ignored = c["if_ignored"]["what"][lang]
    ignore = (
        IGNORE_DAYS[lang].format(d=f"{c['days']:g}", what=what_ignored, cost=cost)
        if c.get("days")
        else IGNORE[lang].format(what=what_ignored, cost=cost)
    )
    return TEMPLATE[lang].format(
        sev=sev,
        name=machine_name,
        what=c["what"][lang],
        rising=RISING[lang].format(n=days_rising) if days_rising > 1 else "",
        part=c["fix"]["part"][lang],
        price=_rs(*price) if price else "—",
        hours=hours_txt,
        ignore=ignore,
        disc=load()["disclaimer"][lang],
    )
