"""The owner's agent: observe -> diagnose -> plan -> ask owner -> act -> verify, India-first.

Claude Fable 5.2 (Anthropic Python SDK, tool use) when ANTHROPIC_API_KEY is set; otherwise a rule-based
loop that calls the same tools in a fixed order. Either way every step is logged and shown to the owner
as a timeline.

Tools (all strict JSON schemas, all backed by tested code; none can stop or control a machine):
  get_machine_health, get_spectrogram, diagnose_fault, find_local_mechanic, check_part_stock,
  estimate_cost_inr, check_warranty_insurance, read_machine_documents, schedule_repair, send_whatsapp,
  verify_repair, log_warranty_claim
check_warranty_insurance matches the fault against the machine's own uploaded papers (server/coverage.py:
the verdict is code, not the model); read_machine_documents gives the agent the papers' text so it can
answer any question about them ("insurance kab tak hai?", "claim ke liye kisko phone karun?").
Anything that costs money waits for the owner: schedule_repair only *asks* (WhatsApp "1 = Haan,
2 = Nahi"); the visit is booked, and the mechanic told, only when the owner replies 1 (server/fix.py
refuses otherwise). log_warranty_claim needs a signed, verified repair record.

Languages: Hindi, Marathi, Gujarati, Tamil, Kannada, Telugu, English and Hindi in Roman script, detected
from the owner's message (script, then common words). Offline replies come from templates; the fault
description itself exists in faults.yaml for hi/mr/gu/en/hi_latn only, so for ta/kn/te the offline
reply keeps the fault name in English (Claude replies fully in the owner's language).
"""

import json
import re
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

from .notify import env

MODEL = "claude-fable-5-2"
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
STEPS = ["observe", "diagnose", "plan", "ask owner", "act", "verify"]
TOOL_STEP = {"get_machine_health": "observe", "get_spectrogram": "observe", "diagnose_fault": "diagnose",
             "find_local_mechanic": "plan", "check_part_stock": "plan", "estimate_cost_inr": "plan",
             "schedule_repair": "ask owner", "send_whatsapp": "ask owner", "verify_repair": "verify",
             "log_warranty_claim": "act", "check_warranty_insurance": "plan", "read_machine_documents": "plan"}  # fmt: skip


# ---------------------------------------------------------------- India formatting
def inr(v: float, words: bool = True) -> str:
    """₹ with Indian grouping, and lakh/crore words for large amounts: ₹1,56,960 (1.57 lakh)."""
    n = round(v)
    s = str(abs(n))
    head, tail = s[:-3], s[-3:]
    groups: list[str] = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    out = "₹" + ("-" if n < 0 else "") + ",".join([*groups, tail])
    if not words:
        return out
    if abs(n) >= 1e7:
        return f"{out} ({abs(n) / 1e7:.2f} crore)"
    if abs(n) >= 1e5:
        return f"{out} ({abs(n) / 1e5:.2f} lakh)"
    return out


def ist(dt: datetime) -> str:
    return dt.strftime("%a %d %b, %I:%M %p IST")


# ---------------------------------------------------------------- language
SCRIPTS = [("ta", r"[஀-௿]"), ("kn", r"[ಀ-೿]"), ("te", r"[ఀ-౿]"), ("gu", r"[઀-૿]"), ("dev", r"[ऀ-ॿ]")]
MARATHI_WORDS = {"आहे", "का", "नाही", "काय", "झाला", "झाली", "आवाज", "मशीनचा", "कशामुळे", "बदलला"}
HINGLISH_WORDS = {"kyun", "kyon", "kya", "hai", "nahi", "awaaz", "aawaz", "badal", "gayi", "gaya", "kaisa", "mein",
                  "theek", "kab", "kitna", "bulao", "machine ki", "ki", "ka", "ho"}  # fmt: skip


def detect_lang(text: str) -> str:
    for code, rx in SCRIPTS:
        if re.search(rx, text):
            if code == "dev":
                words = set(re.findall(r"[ऀ-ॿ]+", text))
                return "mr" if words & MARATHI_WORDS else "hi"
            return code
    words = set(re.findall(r"[a-z]+", text.lower()))
    return "hi_latn" if len(words & HINGLISH_WORDS) >= 2 else "en"


# Offline reply templates (rule-based fallback); Claude writes its own replies in the owner's style.
T: dict[str, dict[str, str]] = {
    "why": {
        "en": "{m}: {what} Fixable now for about {price}. {wait} Shall I ask your mechanic? 1 = Yes, 2 = No",
        "hi_latn": "{m}: {what} Abhi lagbhag {price} mein theek ho sakta hai. {wait} Aapke mistri ko bulaun? 1 = Haan, 2 = Nahi",
        "hi": "{m}: {what} अभी लगभग {price} में ठीक हो सकता है। {wait} आपके मिस्त्री को बुलाऊँ? 1 = हाँ, 2 = नहीं",
        "mr": "{m}: {what} आत्ता सुमारे {price} मध्ये दुरुस्त होऊ शकते. {wait} तुमच्या मिस्त्रीला बोलावू का? 1 = हो, 2 = नाही",
        "gu": "{m}: {what} હમણાં આશરે {price} માં ઠીક થઈ શકે. {wait} તમારા મિસ્ત્રીને બોલાવું? 1 = હા, 2 = ના",
        "ta": "{m}: {what} இப்போது சுமார் {price} செலவில் சரிசெய்யலாம். {wait} உங்கள் மெக்கானிக்கை அழைக்கவா? 1 = ஆம், 2 = இல்லை",
        "kn": "{m}: {what} ಈಗ ಸುಮಾರು {price} ಗೆ ಸರಿಪಡಿಸಬಹುದು. {wait} ನಿಮ್ಮ ಮೆಕ್ಯಾನಿಕ್ ಅನ್ನು ಕರೆಯಲೇ? 1 = ಹೌದು, 2 = ಬೇಡ",
        "te": "{m}: {what} ఇప్పుడు సుమారు {price} ఖర్చుతో బాగుచేయొచ్చు. {wait} మీ మెకానిక్‌ని పిలవనా? 1 = అవును, 2 = వద్దు",
    },
    "fine": {
        "en": "{m} sounds like its normal self. No fault to report.",
        "hi_latn": "{m} ki awaaz normal hai. Koi kharabi nahi dikh rahi.",
        "hi": "{m} की आवाज़ सामान्य है। कोई खराबी नहीं दिख रही।",
        "mr": "{m} चा आवाज नेहमीसारखा आहे. कोणताही बिघाड दिसत नाही.",
        "gu": "{m} નો અવાજ સામાન્ય છે. કોઈ ખામી દેખાતી નથી.",
        "ta": "{m} இயல்பாக ஒலிக்கிறது. எந்த கோளாறும் இல்லை.",
        "kn": "{m} ಸಾಮಾನ್ಯವಾಗಿ ಕೇಳಿಸುತ್ತಿದೆ. ಯಾವುದೇ ದೋಷ ಕಾಣುತ್ತಿಲ್ಲ.",
        "te": "{m} మామూలుగా వినిపిస్తోంది. ఏ లోపం కనిపించడం లేదు.",
    },
    "unsure": {
        "en": "{m}: the sound has changed, but I am not sure why. A mechanic should check it. Shall I ask? 1 = Yes, 2 = No",
        "hi_latn": "{m}: awaaz badli hai, par main pakka nahi keh sakta kyun. Mistri ko dikhana chahiye. Bulaun? 1 = Haan, 2 = Nahi",
        "hi": "{m}: आवाज़ बदली है, पर मैं पक्का नहीं कह सकता क्यों। मिस्त्री को दिखाना चाहिए। बुलाऊँ? 1 = हाँ, 2 = नहीं",
        "mr": "{m}: आवाज बदलला आहे, पण नक्की का ते सांगता येत नाही. मिस्त्रीला दाखवा. बोलावू का? 1 = हो, 2 = नाही",
        "gu": "{m}: અવાજ બદલાયો છે, પણ કેમ તે પાકું કહી શકતો નથી. મિસ્ત્રીને બતાવો. બોલાવું? 1 = હા, 2 = ના",
        "ta": "{m}: ஒலி மாறியுள்ளது, ஆனால் காரணம் உறுதியாகத் தெரியவில்லை. மெக்கானிக் பார்க்க வேண்டும். அழைக்கவா? 1 = ஆம், 2 = இல்லை",
        "kn": "{m}: ಶಬ್ದ ಬದಲಾಗಿದೆ, ಆದರೆ ಕಾರಣ ಖಚಿತವಿಲ್ಲ. ಮೆಕ್ಯಾನಿಕ್ ನೋಡಬೇಕು. ಕರೆಯಲೇ? 1 = ಹೌದು, 2 = ಬೇಡ",
        "te": "{m}: శబ్దం మారింది, కానీ ఎందుకో ఖచ్చితంగా తెలియదు. మెకానిక్ చూడాలి. పిలవనా? 1 = అవును, 2 = వద్దు",
    },
    "which": {
        "en": "Which machine? {names}", "hi_latn": "Kaunsi machine? {names}", "hi": "कौन सी मशीन? {names}",
        "mr": "कोणती मशीन? {names}", "gu": "કયું મશીન? {names}", "ta": "எந்த இயந்திரம்? {names}",
        "kn": "ಯಾವ ಯಂತ್ರ? {names}", "te": "ఏ యంత్రం? {names}",
    },
    "asked": {
        "en": "I have asked on WhatsApp for {when}. Nothing is booked until you reply 1.",
        "hi_latn": "Maine WhatsApp par {when} ke liye poocha hai. Aap 1 bhejenge tabhi booking hogi.",
        "hi": "मैंने व्हाट्सऐप पर {when} के लिए पूछा है। आप 1 भेजेंगे तभी बुकिंग होगी।",
        "mr": "मी व्हॉट्सॲपवर {when} साठी विचारले आहे. तुम्ही 1 पाठवल्यावरच बुकिंग होईल.",
        "gu": "મેં વોટ્સએપ પર {when} માટે પૂછ્યું છે. તમે 1 મોકલશો ત્યારે જ બુકિંગ થશે.",
        "ta": "{when} க்கு வாட்ஸ்அப்பில் கேட்டுள்ளேன். நீங்கள் 1 அனுப்பும் வரை எதுவும் பதிவு செய்யப்படாது.",
        "kn": "{when} ಗಾಗಿ ವಾಟ್ಸಾಪ್‌ನಲ್ಲಿ ಕೇಳಿದ್ದೇನೆ. ನೀವು 1 ಕಳುಹಿಸುವವರೆಗೆ ಏನೂ ಬುಕ್ ಆಗುವುದಿಲ್ಲ.",
        "te": "{when} కోసం వాట్సాప్‌లో అడిగాను. మీరు 1 పంపే వరకు ఏదీ బుక్ కాదు.",
    },
    "wait": {
        "en": "Waiting {d} days: {p}% chance of a breakdown (~{cost}).", "hi_latn": "{d} din rukein to {p}% sambhavna breakdown ki (~{cost}).",
        "hi": "{d} दिन रुकें तो {p}% संभावना ब्रेकडाउन की (~{cost})।", "mr": "{d} दिवस थांबलात तर {p}% शक्यता ब्रेकडाउनची (~{cost}).",
        "gu": "{d} દિવસ રાહ જુઓ તો {p}% શક્યતા બ્રેકડાઉનની (~{cost}).", "ta": "{d} நாட்கள் காத்திருந்தால் பழுது ஏற்பட {p}% வாய்ப்பு (~{cost}).",
        "kn": "{d} ದಿನ ಕಾದರೆ ಕೆಟ್ಟುಹೋಗುವ ಸಾಧ್ಯತೆ {p}% (~{cost}).", "te": "{d} రోజులు ఆగితే పాడయ్యే అవకాశం {p}% (~{cost}).",
    },
}  # fmt: skip
CARD_LANG = {"ta": "en", "kn": "en", "te": "en"}  # faults.yaml has no Tamil/Kannada/Telugu text


def _t(key: str, lang: str, **kw: Any) -> str:
    return T[key].get(lang, T[key]["en"]).format(**kw)


# ---------------------------------------------------------------- tools
def _schema(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


MID = {"machine_id": {"type": "string", "description": "machine id, e.g. A-G1"}}
TOOLS = [
    {"name": "get_machine_health", "description": "Current status, score vs its alarm level, days rising, days-to-fault range.", "input_schema": _schema(MID, ["machine_id"])},
    {"name": "get_spectrogram", "description": "Which frequency band changed most versus the machine's own normal, by how much, in words.", "input_schema": _schema(MID, ["machine_id"])},
    {"name": "diagnose_fault", "description": "The diagnosis card from server/faults.yaml: fault, severity, repairable, fix, rupee range, misuse, warranty.", "input_schema": _schema(MID, ["machine_id"])},
    {"name": "find_local_mechanic", "description": "The owner's own mechanic first, else the cluster technician.", "input_schema": _schema(MID, ["machine_id"])},
    {"name": "check_part_stock", "description": "Local dealer stock for the part this fault needs (mock dealer data in the demo).", "input_schema": _schema(MID, ["machine_id"])},
    {"name": "estimate_cost_inr", "description": "Fix now vs waiting a week, in rupees, from the factory's cost profile.", "input_schema": _schema(MID, ["machine_id"])},
    {"name": "check_warranty_insurance", "description": "Is this machine's repair covered by its own warranty card, AMC or insurance policy (uploaded by the owner)? Returns the verdict (claimable / check / not_covered / no_docs), which paper, valid till, deductible, claim deadline, claim steps, contact, and what voids the claim. Call it before booking any paid repair.", "input_schema": _schema(MID, ["machine_id"])},
    {"name": "read_machine_documents", "description": "The machine's uploaded papers (warranty card, insurance policy, invoice, AMC, manual): extracted fields and the document text, to answer the owner's questions about them.", "input_schema": _schema(MID, ["machine_id"])},
    {"name": "schedule_repair", "description": "Propose a visit slot (respects shifts, off-days, power cuts, peak season) and ASK the owner on WhatsApp. Books nothing.", "input_schema": _schema(MID, ["machine_id"])},
    {"name": "send_whatsapp", "description": "Send the owner the alert for this machine (plain words, fix, rupees, warranty, buttons).", "input_schema": _schema(MID, ["machine_id"])},
    {"name": "verify_repair", "description": "After a repair: re-record vs baseline -> fixed / partly / failed, signed record.", "input_schema": _schema(MID, ["machine_id"])},
    {"name": "log_warranty_claim", "description": "Log a warranty claim for a machine whose repair failed, citing the signed repair record.", "input_schema": _schema(MID, ["machine_id"])},
]  # fmt: skip
for _tool in TOOLS:
    _tool["strict"] = True


class AgentTools:
    def __init__(self, live: Any, run_id: str, lang: str) -> None:
        self.live, self.run_id, self.lang = live, run_id, lang
        self.steps: list[dict[str, Any]] = []

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        mid = args.get("machine_id", "")
        if mid not in self.live.c.machines:
            out: dict[str, Any] = {"error": f"unknown machine {mid}"}
        else:
            out = getattr(self, name)(mid)
        self.steps.append({"step": TOOL_STEP.get(name, "act"), "tool": name, "machine": mid,
                           "summary": out.get("summary") or out.get("error") or ""})  # fmt: skip
        self.live.event("agent_step", run=self.run_id, tool=name, machine=mid)
        return out

    def _rank(self, mid: str) -> dict[str, Any]:
        return next((r for r in self.live.ranking() if r["machine_id"] == mid), {})

    def get_machine_health(self, mid: str) -> dict[str, Any]:
        r = self._rank(mid)
        est = r.get("days_to_fault_level") or {}
        days = (
            f"{est.get('low')}-{est.get('high')} days"
            if est.get("status") == "estimate"
            else est.get("status", "-")
        )
        return {"status": r.get("status"), "risk": r.get("risk"), "days_rising": r.get("days_rising"),
                "days_to_fault_level": est, "learning": r.get("learning"),
                "summary": f"{r.get('status')} at {round((r.get('risk') or 0) * 100)}% of alarm, fault level in {days}"}  # fmt: skip

    def get_spectrogram(self, mid: str) -> dict[str, Any]:
        try:
            ex = self.live.explain(mid)
        except Exception as e:
            return {"error": str(e)}
        if ex.get("status") != "ok":
            return {"status": ex.get("status"), "summary": "no clear band change"}
        return {"band_hz": [ex["lo_hz"], ex["hi_hz"]], "change_pct": ex["change_pct"], "word": ex["word"],
                "summary": f"{ex['word']} {round(ex['lo_hz'])}-{round(ex['hi_hz'])} Hz, {ex['change_pct']:+.0f}%"}  # fmt: skip

    def diagnose_fault(self, mid: str) -> dict[str, Any]:
        card = self.live.alerts.card(mid, CARD_LANG.get(self.lang, self.lang))
        if not card:
            return {"fault": None, "summary": "no alert: nothing to diagnose"}
        name = (card.get("name") or {}).get("en") if card.get("fault") else None
        return {"card": card, "fault": card.get("fault"), "named_by": card.get("named_by"),
                "summary": f"{name or 'fault not named'} ({card['severity']['label']['en']}), named by {card.get('named_by') or '-'}"}  # fmt: skip

    def find_local_mechanic(self, mid: str) -> dict[str, Any]:
        f = self.live.c.machines[mid]["factory"]
        own = env.get(f"DHADKAN_MECHANIC_{f}")
        tech = self.live.c.cfg["technician"]["name"]
        who = "the owner's own mechanic" if own else f"cluster technician {tech}"
        return {"own_mechanic_on_file": bool(own), "fallback": tech, "summary": who}

    def check_part_stock(self, mid: str) -> dict[str, Any]:
        from . import fix

        card = self.live.alerts.card(mid, "en") or {}
        s = fix.stock(card.get("fault"), self.live.c.machines[mid]["type"])
        return {"stock": s, "summary": f"{s} (mock dealer data)"}

    def estimate_cost_inr(self, mid: str) -> dict[str, Any]:
        card = self.live.alerts.card(mid, "en") or {}
        money = card.get("money")
        if not money:
            return {"summary": "no cost estimate (no named fault)"}
        lo, hi = money["fix_now_inr"]
        return {"fix_now_inr": [lo, hi], "wait_days": money["wait_days"], "p_breakdown": money["p_fault_within_wait"],
                "breakdown_inr": money["breakdown_inr"],
                "summary": f"fix now {inr(lo)}-{inr(hi)}; wait {money['wait_days']} d: "
                           f"{round(money['p_fault_within_wait'] * 100)}% chance of {inr(money['breakdown_inr'])}"}  # fmt: skip

    def check_warranty_insurance(self, mid: str) -> dict[str, Any]:
        from . import coverage

        card = self.live.alerts.card(mid, "en")
        cov = (card or {}).get("coverage") or self.live.alerts.coverage(mid, card)
        lang = CARD_LANG.get(self.lang, self.lang)
        b = cov.get("best") or {}
        summary = {"claimable": f"likely claimable under {b.get('kind')} ({b.get('provider')})",
                   "check": f"{b.get('kind')} ({b.get('provider')}) active, fault not named: ask them",
                   "not_covered": f"not covered ({b.get('why')})", "no_docs": "no warranty/insurance papers uploaded",
                   "unknown_fault": "fault not named yet; papers in force listed"}.get(cov.get("verdict", ""), "-")  # fmt: skip
        return {"coverage": cov, "owner_words": coverage.alert_text(cov, lang), "summary": summary}

    def read_machine_documents(self, mid: str) -> dict[str, Any]:
        from . import coverage

        docs = coverage.for_agent(self.live.alerts.docs, mid, self.live.c.machines[mid])
        return {"documents": docs, "summary": f"{len(docs)} paper(s): " + ", ".join(d["kind"] for d in docs)}

    def slot(self, mid: str) -> datetime:
        """Next working slot: not an off-day, not in a power cut, outside quiet hours, 09:00-18:00, at
        least 2 h from now; in peak season the slot moves to the shift change (13:30) to save output."""
        from .notify.policy import _in

        f = self.live.c.factories[self.live.c.machines[mid]["factory"]]
        now = self.live.c.now()
        t = (now + timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
        for _ in range(24 * 14):
            day = DAYS[t.weekday()]
            cut = any(_in(t, a, b) for a, b in f.get("power_cuts", []))
            if 9 <= t.hour < 18 and day not in f.get("off_days", []) and not cut:
                if f.get("peak_months") and t.month in f["peak_months"]:
                    t = t.replace(hour=13, minute=30)
                return t
            t += timedelta(hours=1)
        return t

    def schedule_repair(self, mid: str) -> dict[str, Any]:
        from . import agent

        t = self.slot(mid)
        tools = agent.Tools(self.live.c, self.run_id, "owner-agent")
        r = self._rank(mid)
        kind = "match" if r.get("status") == "early_match" else "rising"
        drafted = tools.draft_owner_alert(mid, kind, DAYS[t.weekday()])
        if "skipped" in drafted:
            return {"status": "already asked", "summary": drafted["skipped"]}
        msg = self.live.c.db.execute(
            "SELECT id FROM messages WHERE ask LIKE ? ORDER BY id DESC LIMIT 1", (f"{mid}|%",)
        ).fetchone()
        sent = self.live.alerts.send_for(mid, message_id=int(msg[0]))
        return {"status": "waiting_for_owner", "slot": ist(t), "whatsapp": sent and sent["status"],
                "summary": f"asked the owner for {ist(t)}; WhatsApp {sent and sent['status']}; nothing booked until '1'"}  # fmt: skip

    def send_whatsapp(self, mid: str) -> dict[str, Any]:
        sent = self.live.alerts.send_for(mid)
        return {"status": sent and sent["status"], "summary": f"alert {sent and sent['status']}"}

    def verify_repair(self, mid: str) -> dict[str, Any]:
        from . import prove

        approved = self.live.c.db.execute(
            "SELECT 1 FROM messages WHERE ask LIKE ? AND approved=1", (f"{mid}|%",)
        ).fetchone()
        if not approved:
            return {"status": "no approved repair", "summary": "no repair the owner approved yet"}
        after, note = prove.demo_after_readings(self.live, mid)
        rec = prove.make_record(self.live, mid, after, None, None, demo_note=note)
        v = rec["record"]["verification"]
        return {
            "outcome": v["outcome"],
            "record_sha256": rec["sha256"],
            "summary": f"re-record: {v['words']['en']}",
        }

    def log_warranty_claim(self, mid: str) -> dict[str, Any]:
        from . import prove

        recs = prove.records(self.live, mid)
        if not recs or recs[-1]["record"]["verification"]["outcome"] != "failed":
            return {
                "status": "not eligible",
                "summary": "warranty claim needs a signed record of a failed repair",
            }
        claim = {"machine": mid, "record_sha256": recs[-1]["sha256"], "logged": self.live.c.now().isoformat()}
        self.live.event("warranty_claim", **claim)
        return {
            "status": "logged",
            "claim": claim,
            "summary": f"claim logged against record {recs[-1]['sha256'][:12]}",
        }


# ---------------------------------------------------------------- the loop
SYSTEM = (
    "You are Dhadkan, a maintenance assistant for a small Indian factory owner. Reply in the owner's language and "
    "spoken style (the language of their message; Hinglish in Roman script if they wrote that), short and plain, "
    "like a trusted mechanic. Work in this order: observe (get_machine_health, get_spectrogram), diagnose "
    "(diagnose_fault), plan (check_warranty_insurance, find_local_mechanic, check_part_stock, estimate_cost_inr), ask the owner "
    "(schedule_repair asks on WhatsApp and books nothing), and after an approved repair, verify_repair. Rules: "
    "never invent numbers, prices or parts; use only what the tools return. Rupees in Indian format (lakh, crore). "
    "Times in IST. Suggest the owner's own mechanic and dealer first. If you are unsure, say so and suggest a "
    "mechanic check. You cannot stop or control a machine, and nothing that costs money happens until the owner "
    "replies 1 (Haan). If the machine's warranty or insurance covers the repair, say so first, with the "
    "company's number and the claim deadline, and warn that letting a local mechanic open the machine can void "
    "the claim. The coverage verdict comes from check_warranty_insurance; never decide coverage yourself, and "
    "use read_machine_documents to answer questions about the papers, quoting them."
)


def resolve_machine(live: Any, text: str, factory: str | None) -> str | None:
    pool = {mid: m for mid, m in live.c.machines.items() if not factory or m["factory"] == factory}
    t = text.lower()
    for mid, m in pool.items():
        if mid.lower() in t or m["name"].lower() in t or (m.get("name_local") and m["name_local"] in text):
            return mid
    num = re.search(r"(\d+)", t)
    kinds = {"loom": "loom", "लूम": "loom", "pump": "pump", "पंप": "pump", "blower": "fan", "ब्लोअर": "fan", "mill": "mill", "chakki": "mill"}  # fmt: skip
    for word, kind in kinds.items():
        if word in t:
            cands = [
                mid
                for mid, m in pool.items()
                if kind in (m.get("shape"), m["type"]) or word in m["name"].lower()
            ]
            if num:
                cands = [c for c in cands if pool[c]["name"].endswith(num.group(1))] or cands
            if cands:
                return cands[0]
    flagged = [
        r["machine_id"]
        for r in live.ranking()
        if r["machine_id"] in pool and r["status"] in ("alarm", "early_match")
    ]
    return flagged[0] if flagged else None


def run_rules(live: Any, text: str, factory: str | None, lang: str) -> dict[str, Any]:
    run_id = "agent-rules-" + uuid.uuid4().hex[:6]
    tools = AgentTools(live, run_id, lang)
    mid = resolve_machine(live, text, factory)
    if mid is None:
        names = ", ".join(
            m["name"] for m in live.c.machines.values() if not factory or m["factory"] == factory
        )
        return {
            "run_id": run_id,
            "lang": lang,
            "reply": _t("which", lang, names=names),
            "steps": [],
            "engine": "rules",
        }
    m = live.c.machines[mid]
    name = m["name_local"] if lang in ("hi", "mr") else m["name"]
    approved = live.c.db.execute(
        "SELECT 1 FROM messages WHERE ask LIKE ? AND approved=1", (f"{mid}|%",)
    ).fetchone()
    if approved:  # the owner said Haan: the job went out on their reply; now check the repair
        from . import prove

        tools.steps.append({"step": "act", "tool": "mechanic job (on the owner's 1)", "machine": mid,
                            "summary": "visit booked and the mechanic told, by the owner's reply"})  # fmt: skip
        if not prove.records(live, mid):
            v = tools.call("verify_repair", {"machine_id": mid})
            word = {"fixed": "fine", "partly": "unsure", "failed": "unsure"}.get(
                v.get("outcome", ""), "unsure"
            )
            reply = _t(word, lang, m=name) if word == "fine" else _t("unsure", lang, m=name)
            return {"run_id": run_id, "lang": lang, "reply": f"{reply}\n({v.get('summary', '')})", "steps": tools.steps,
                    "engine": "rules", "machine": mid}  # fmt: skip
    health = tools.call("get_machine_health", {"machine_id": mid})
    if health.get("status") in ("ok", "learning", None):
        return {
            "run_id": run_id,
            "lang": lang,
            "reply": _t("fine", lang, m=name),
            "steps": tools.steps,
            "engine": "rules",
            "machine": mid,
        }
    tools.call("get_spectrogram", {"machine_id": mid})
    dx = tools.call("diagnose_fault", {"machine_id": mid})
    tools.call("find_local_mechanic", {"machine_id": mid})
    tools.call("check_part_stock", {"machine_id": mid})
    cost = tools.call("estimate_cost_inr", {"machine_id": mid})
    cover = tools.call("check_warranty_insurance", {"machine_id": mid})
    card = dx.get("card") or {}
    if not card.get("fault"):
        reply = _t("unsure", lang, m=name)
    else:
        what = card["what"].get(CARD_LANG.get(lang, lang)) or card["what"]["en"]
        lo, hi = cost.get("fix_now_inr", [0, 0])
        wait = _t("wait", lang, d=cost.get("wait_days", 7), p=round((cost.get("p_breakdown") or 0) * 100),
                  cost=inr(cost.get("breakdown_inr") or 0, words=False))  # fmt: skip
        reply = _t("why", lang, m=name, what=what, price=f"{inr(lo)}-{inr(hi)}", wait=wait)
    if cover.get("owner_words") and cover["coverage"].get("verdict") != "no_docs":
        reply += "\n" + cover["owner_words"]
    ask = tools.call("schedule_repair", {"machine_id": mid})
    if ask.get("status") == "waiting_for_owner":
        reply += "\n" + _t("asked", lang, when=ask["slot"])
    return {
        "run_id": run_id,
        "lang": lang,
        "reply": reply,
        "steps": tools.steps,
        "engine": "rules",
        "machine": mid,
    }


def run_claude(
    live: Any, text: str, factory: str | None, lang: str, client: Any, max_turns: int = 8
) -> dict[str, Any]:
    run_id = "agent-claude-" + uuid.uuid4().hex[:6]
    tools = AgentTools(live, run_id, lang)
    machines = [{"id": mid, "name": m["name"], "type": m["type"]} for mid, m in live.c.machines.items()
                if not factory or m["factory"] == factory]  # fmt: skip
    now = live.c.now()
    messages: list[dict[str, Any]] = [{"role": "user", "content": (
        f"Owner's machines: {json.dumps(machines, ensure_ascii=False)}. Now: {ist(now)}. Owner language: {lang}.\n"
        f"Owner says: {text}")}]  # fmt: skip
    reply = ""
    for _ in range(max_turns):
        resp = client.beta.messages.create(model=MODEL, max_tokens=4000, system=SYSTEM, tools=TOOLS, messages=messages,
                                           betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                                           output_config={"effort": "high"})  # fmt: skip
        if resp.stop_reason == "refusal":
            break
        messages.append({"role": "assistant", "content": resp.content})
        uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
        reply = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text")
        if not uses or resp.stop_reason != "tool_use":
            break
        results = []
        for u in uses:
            out = tools.call(u.name, dict(u.input))
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": u.id,
                    "content": json.dumps(out, ensure_ascii=False, default=str),
                }
            )
        messages.append({"role": "user", "content": results})
    if not reply:
        return run_rules(live, text, factory, lang)
    return {"run_id": run_id, "lang": lang, "reply": reply, "steps": tools.steps, "engine": "claude"}


def on_alarm(live: Any, mid: str) -> dict[str, Any]:
    """The agent's own pass when a machine alarms (no owner message needed): observe -> ... -> ask owner."""
    f = live.c.machines[mid]["factory"]
    lang = live.alerts.owner_lang(mid)
    return chat(live, live.c.machines[mid]["name"], f, lang if lang in T["why"] else "hi_latn")


def chat(
    live: Any, text: str, factory: str | None = None, lang: str | None = None, client: Any = None
) -> dict[str, Any]:
    """One owner message -> the agent's reply and its tool timeline. Claude when keyed; rules otherwise."""
    lang = lang or detect_lang(text)
    t0 = time.time()
    key = env.get("ANTHROPIC_API_KEY")
    if client is None and key and env.get("DHADKAN_LLM") != "0":
        import anthropic

        client = anthropic.Anthropic(api_key=key)
    try:
        out = (
            run_claude(live, text, factory, lang, client)
            if client is not None
            else run_rules(live, text, factory, lang)
        )
    except Exception as e:  # offline, rate limit: the rules still answer
        out = run_rules(live, text, factory, lang)
        out["fallback_reason"] = type(e).__name__
    out["seconds"] = round(time.time() - t0, 2)
    out["checklist"] = [{"step": s, "done": any(x["step"] == s for x in out["steps"])} for s in STEPS]
    live.event("agent_chat", run=out["run_id"], lang=lang, engine=out["engine"])
    live.last_agent = out
    return out
