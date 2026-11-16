"""Onboarding over WhatsApp: the owner sends "Namaste", answers short questions in their language, then
sends a 30-second recording of each machine, which starts that machine's learning period.

Questions (one per message; 0 skips the optional ones): language, district, factory type (preset from
data/presets.yaml), how many of each machine, shifts, weekly off-day, power-cut hours, peak season,
next delivery date, daily output value, workers, supervisor / mechanic / dealer numbers. The profile is
saved to data/factories/<phone>.json and the clips to data/factories/<phone>/ (git-ignored). Nothing
here can control a machine, and no message goes to the supervisor, mechanic or dealer until an alert
needs them.
"""

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import yaml

from .config import ROOT

PRESETS = ROOT / "data" / "presets.yaml"
FACTORIES = ROOT / "data" / "factories"
LANG_MENU = {"1": "hi", "2": "mr", "3": "gu", "4": "en", "5": "hi_latn"}
CLIP_SECONDS = 30

Q: dict[str, dict[str, str]] = {
    "language": {"en": "Namaste. Dhadkan listens to your machines and tells you on WhatsApp before they break. "
                       "Choose your language: 1 हिंदी, 2 मराठी, 3 ગુજરાતી, 4 English, 5 Hindi (Roman)"},
    "district": {"en": "Which town or district is the factory in?", "hi": "फैक्ट्री किस शहर या ज़िले में है?",
                 "hi_latn": "Factory kis shehar ya zile mein hai?", "mr": "कारखाना कोणत्या गावात किंवा जिल्ह्यात आहे?",
                 "gu": "ફેક્ટરી કયા શહેર કે જિલ્લામાં છે?"},
    "preset": {"en": "What kind of factory? {menu}", "hi": "कैसी फैक्ट्री है? {menu}", "hi_latn": "Kaisi factory hai? {menu}",
               "mr": "कोणता कारखाना? {menu}", "gu": "કેવી ફેક્ટરી છે? {menu}"},
    "count": {"en": "How many {machine}?", "hi": "कितने {machine} हैं?", "hi_latn": "Kitne {machine} hain?",
              "mr": "किती {machine} आहेत?", "gu": "કેટલા {machine} છે?"},
    "shifts": {"en": "How many shifts a day? (1, 2 or 3)", "hi": "दिन में कितनी शिफ्ट? (1, 2 या 3)",
               "hi_latn": "Din mein kitni shift? (1, 2 ya 3)", "mr": "दिवसात किती शिफ्ट? (1, 2 किंवा 3)",
               "gu": "દિવસમાં કેટલી શિફ્ટ? (1, 2 કે 3)"},
    "off_day": {"en": "Weekly off-day? (e.g. Sunday, 0 = none)", "hi": "हफ़्ते की छुट्टी? (जैसे रविवार, 0 = नहीं)",
                "hi_latn": "Hafte ki chhutti? (jaise Sunday, 0 = nahi)", "mr": "आठवड्याची सुट्टी? (उदा. रविवार, 0 = नाही)",
                "gu": "અઠવાડિયાની રજા? (જેમ કે રવિવાર, 0 = નહીં)"},
    "power_cuts": {"en": "When does the power usually go? (e.g. 14-16, 0 = not fixed)",
                   "hi": "बिजली आमतौर पर कब जाती है? (जैसे 14-16, 0 = तय नहीं)",
                   "hi_latn": "Bijli aam taur par kab jaati hai? (jaise 14-16, 0 = tay nahi)",
                   "mr": "वीज सहसा कधी जाते? (उदा. 14-16, 0 = ठरलेले नाही)", "gu": "વીજળી સામાન્ય રીતે ક્યારે જાય છે? (જેમ કે 14-16, 0 = નક્કી નથી)"},
    "peak_season": {"en": "Busiest months? (e.g. Oct-Dec, 0 = skip)", "hi": "सबसे व्यस्त महीने? (जैसे Oct-Dec, 0 = छोड़ें)",
                    "hi_latn": "Sabse busy mahine? (jaise Oct-Dec, 0 = chhodein)", "mr": "सर्वात व्यस्त महिने? (उदा. Oct-Dec, 0 = सोडा)",
                    "gu": "સૌથી વ્યસ્ત મહિના? (જેમ કે Oct-Dec, 0 = છોડો)"},
    "delivery": {"en": "Next big delivery date? (DD-MM, 0 = skip)", "hi": "अगली बड़ी डिलीवरी कब? (DD-MM, 0 = छोड़ें)",
                 "hi_latn": "Agli badi delivery kab? (DD-MM, 0 = chhodein)", "mr": "पुढची मोठी डिलिव्हरी कधी? (DD-MM, 0 = सोडा)",
                 "gu": "આગલી મોટી ડિલિવરી ક્યારે? (DD-MM, 0 = છોડો)"},
    "output_inr": {"en": "Output value per day in rupees? (0 = skip)", "hi": "रोज़ का उत्पादन कितने रुपये का? (0 = छोड़ें)",
                   "hi_latn": "Roz ka production kitne rupaye ka? (0 = chhodein)", "mr": "रोजचे उत्पादन किती रुपयांचे? (0 = सोडा)",
                   "gu": "રોજનું ઉત્પાદન કેટલા રૂપિયાનું? (0 = છોડો)"},
    "workers": {"en": "How many workers?", "hi": "कितने कामगार?", "hi_latn": "Kitne kaamgaar?", "mr": "किती कामगार?",
                "gu": "કેટલા કામદાર?"},
    "supervisor": {"en": "Supervisor's WhatsApp number? (0 = none)", "hi": "सुपरवाइज़र का व्हाट्सऐप नंबर? (0 = नहीं)",
                   "hi_latn": "Supervisor ka WhatsApp number? (0 = nahi)", "mr": "सुपरवायझरचा व्हॉट्सॲप नंबर? (0 = नाही)",
                   "gu": "સુપરવાઇઝરનો વોટ્સએપ નંબર? (0 = નહીં)"},
    "mechanic": {"en": "Your mechanic's WhatsApp number? (0 = none)", "hi": "आपके मिस्त्री का व्हाट्सऐप नंबर? (0 = नहीं)",
                 "hi_latn": "Aapke mistri ka WhatsApp number? (0 = nahi)", "mr": "तुमच्या मिस्त्रीचा व्हॉट्सॲप नंबर? (0 = नाही)",
                 "gu": "તમારા મિસ્ત્રીનો વોટ્સએપ નંબર? (0 = નહીં)"},
    "dealer": {"en": "Parts dealer's WhatsApp number? (0 = none)", "hi": "पार्ट्स डीलर का व्हाट्सऐप नंबर? (0 = नहीं)",
               "hi_latn": "Parts dealer ka WhatsApp number? (0 = nahi)", "mr": "पार्ट्स डीलरचा व्हॉट्सॲप नंबर? (0 = नाही)",
               "gu": "પાર્ટ્સ ડીલરનો વોટ્સએપ નંબર? (0 = નહીં)"},
    "clip": {"en": "Now hold the phone near {machine} and send a {s}-second voice recording.",
             "hi": "अब फ़ोन {machine} के पास रखकर {s} सेकंड की वॉइस रिकॉर्डिंग भेजें।",
             "hi_latn": "Ab phone {machine} ke paas rakhkar {s} second ki voice recording bhejein.",
             "mr": "आता फोन {machine} जवळ ठेवून {s} सेकंदांचे व्हॉइस रेकॉर्डिंग पाठवा.",
             "gu": "હવે ફોન {machine} પાસે રાખીને {s} સેકન્ડનું વોઇસ રેકોર્ડિંગ મોકલો."},
    "done": {"en": "Done. Every machine is learning its normal sound: day 1 of {days}. No alerts until then.",
             "hi": "हो गया। हर मशीन अपनी सामान्य आवाज़ सीख रही है: दिन 1/{days}। तब तक कोई अलर्ट नहीं।",
             "hi_latn": "Ho gaya. Har machine apni normal aawaz seekh rahi hai: din 1/{days}. Tab tak koi alert nahi.",
             "mr": "झाले. प्रत्येक मशीन आपला सामान्य आवाज शिकत आहे: दिवस 1/{days}. तोपर्यंत अलर्ट नाही.",
             "gu": "થઈ ગયું. દરેક મશીન પોતાનો સામાન્ય અવાજ શીખે છે: દિવસ 1/{days}. ત્યાં સુધી કોઈ એલર્ટ નહીં."},
    "again": {"en": "Sorry, I did not understand. {q}", "hi": "माफ़ कीजिए, समझ नहीं आया। {q}",
              "hi_latn": "Maaf kijiye, samajh nahi aaya. {q}", "mr": "माफ करा, समजले नाही. {q}", "gu": "માફ કરશો, સમજાયું નહીં. {q}"},
}  # fmt: skip
STEPS = ["language", "district", "preset", "count", "shifts", "off_day", "power_cuts", "peak_season", "delivery",
         "output_inr", "workers", "supervisor", "mechanic", "dealer", "clip"]  # fmt: skip
SCHEMA = "CREATE TABLE IF NOT EXISTS onboarding (phone TEXT PRIMARY KEY, state TEXT, profile TEXT);"


def presets() -> dict[str, Any]:
    return yaml.safe_load(PRESETS.read_text(encoding="utf-8"))


def digits(phone: str) -> str:
    return "".join(c for c in phone if c.isdigit())


def _int(text: str) -> int | None:
    m = re.search(r"\d+", text)
    return int(m.group()) if m else None


class Onboarding:
    def __init__(self, db: sqlite3.Connection, root: Path = FACTORIES, learning_days: int = 7) -> None:
        self.db, self.root, self.learning_days = db, root, learning_days
        self.db.executescript(SCHEMA)

    def _load(self, phone: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        r = self.db.execute(
            "SELECT state, profile FROM onboarding WHERE phone=?", (digits(phone),)
        ).fetchone()
        return (json.loads(r[0]), json.loads(r[1])) if r else None

    def _save(self, phone: str, state: dict[str, Any], profile: dict[str, Any]) -> None:
        self.db.execute("INSERT OR REPLACE INTO onboarding (phone, state, profile) VALUES (?,?,?)",
                        (digits(phone), json.dumps(state), json.dumps(profile, ensure_ascii=False)))  # fmt: skip
        self.db.commit()
        if state["step"] in ("clip", "done"):
            self.root.mkdir(parents=True, exist_ok=True)
            (self.root / f"{digits(phone)}.json").write_text(
                json.dumps(profile, ensure_ascii=False, indent=1)
            )

    def active(self, phone: str) -> bool:
        s = self._load(phone)
        return s is not None and s[0]["step"] != "done"

    def _ask(self, state: dict[str, Any], profile: dict[str, Any]) -> str:
        lang, step = profile.get("language", "en"), state["step"]
        q = Q[step].get(lang) or Q[step]["en"]
        if step == "preset":
            p = presets()["presets"]
            menu = ", ".join(
                f"{i} {v['name'].get(lang, v['name']['en'])}" for i, v in enumerate(p.values(), 1)
            )
            return q.format(menu=menu)
        if step == "count":
            kind = state["kinds"][state["k"]]
            return q.format(machine=kind["name"].get(lang, kind["name"]["en"]))
        if step == "clip":
            return q.format(machine=profile["machines"][state["clip_i"]]["name"], s=CLIP_SECONDS)
        if step == "done":
            return q.format(days=self.learning_days)
        return q

    def start(self, phone: str) -> str:
        state, profile = {"step": "language"}, {"phone_digits": digits(phone), "demo": False}
        self._save(phone, state, profile)
        return Q["language"]["en"]

    def handle(self, phone: str, text: str) -> str | None:
        """A text from this phone. Returns the next question, or None if this phone is not onboarding."""
        got = self._load(phone)
        if got is None:
            return (
                self.start(phone)
                if re.search(r"\bnamaste\b|नमस्ते|નમસ્તે|\bhello\b|\bhi\b", text.lower())
                else None
            )
        state, profile = got
        step, t = state["step"], text.strip()
        if step == "done":
            return None
        ok = self._apply(state, profile, step, t)
        self._save(phone, state, profile)
        q = self._ask(state, profile)
        return q if ok else (Q["again"].get(profile.get("language", "en")) or Q["again"]["en"]).format(q=q)

    def _apply(self, state: dict[str, Any], profile: dict[str, Any], step: str, t: str) -> bool:
        skip = t == "0"
        if step == "language":
            if t not in LANG_MENU:
                return False
            profile["language"] = LANG_MENU[t]
        elif step == "district":
            profile["district"] = t
            profile["district_default_language"] = presets()["district_language"].get(t.lower())
        elif step == "preset":
            keys = list(presets()["presets"])
            n = _int(t)
            if not n or not 1 <= n <= len(keys):
                return False
            profile["preset"] = keys[n - 1]
            state["kinds"], state["k"] = presets()["presets"][keys[n - 1]]["machines"], 0
            profile["machines"] = []
        elif step == "count":
            n = _int(t)
            if n is None or n > 200:
                return False
            kind = state["kinds"][state["k"]]
            lang = profile.get("language", "en")
            for i in range(1, n + 1):
                profile["machines"].append({"id": f"{kind['kind']}-{len(profile['machines']) + 1}",
                                            "name": f"{kind['name'].get(lang, kind['name']['en'])} {i}",
                                            "kind": kind["kind"], "model": kind["model"], "hp": kind["hp"],
                                            "watch_faults": kind["faults"], "learning_day": None})  # fmt: skip
            state["k"] += 1
            if state["k"] < len(state["kinds"]):
                return True  # ask the next machine kind
            if not profile["machines"]:
                return False
        elif step == "shifts":
            n = _int(t)
            if n not in (1, 2, 3):
                return False
            profile["shifts"] = n
        elif step == "off_day":
            profile["off_days"] = [] if skip else [t]
        elif step == "power_cuts":
            m = re.findall(r"(\d{1,2})\s*[-–:to]+\s*(\d{1,2})", t)
            profile["power_cuts"] = [] if skip else [[f"{int(a):02d}:00", f"{int(b):02d}:00"] for a, b in m]
            if not skip and not m:
                return False
        elif step == "peak_season":
            profile["peak_season"] = None if skip else t
        elif step == "delivery":
            profile["next_delivery"] = None if skip else t
        elif step == "output_inr":
            n = _int(t)
            if n is None:
                return False
            profile["output_per_day_inr"] = None if n == 0 else n
        elif step == "workers":
            n = _int(t)
            if n is None:
                return False
            profile["workers"] = n
        elif step in ("supervisor", "mechanic", "dealer"):
            d = digits(t)
            if not skip and len(d) < 10:
                return False
            profile.setdefault("contacts", {})[step] = None if skip else "+" + d
        if step != "count" or state["k"] >= len(state.get("kinds", [])):
            nxt = STEPS.index(step) + 1
            state["step"] = STEPS[nxt]
            if state["step"] == "clip":
                state["clip_i"] = 0
        return True

    def clip(self, phone: str, audio: bytes, ext: str = ".ogg") -> str | None:
        """A voice recording during onboarding: the current machine's baseline clip; asks for the next."""
        got = self._load(phone)
        if got is None or got[0]["step"] != "clip":
            return None
        state, profile = got
        m = profile["machines"][state["clip_i"]]
        d = self.root / digits(phone)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{m['id']}_baseline{ext}").write_bytes(audio)
        m["learning_day"] = 1
        m["baseline_clip"] = f"{m['id']}_baseline{ext}"
        state["clip_i"] += 1
        if state["clip_i"] >= len(profile["machines"]):
            state["step"] = "done"
        self._save(phone, state, profile)
        return self._ask(state, profile)
