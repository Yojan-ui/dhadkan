"""WhatsApp loops: the mechanic's job on WhatsApp, the owner's chat commands, and demo phones.

    desk = MechanicDesk(brain)            # brain.desk
    desk.send_job(payload)                # the transport's book_job: the job goes to the mechanic's phone
    desk.handle(phone, text, image)       # the mechanic's reply: 1 / 2 / 3, "raste mein 20 min", photo, "ho gaya"
    desk.tick()                           # no answer within DHADKAN_MECHANIC_REPLY_HOURS -> mechanic_no_show
    owner_command(brain, factory, text)   # "status", "kitna kharcha", "kab aayega mistri", "band karo"

The mechanic's job (only after the owner's logged approval: the transport's book_job is gated by the
maintenance agent and the Guardian before it gets here) names the machine, the fault, the part to bring,
the slot and the address, with 1 = accept, 2 = can't come, 3 = need another time. Then "raste mein / on
the way" (ETA in minutes), and "ho gaya / done" with a REQUIRED photo of the old part and a note, which
reports the repair (repair_done -> post-repair readings -> verification against baseline v1 -> the signed
record, server/prove.py). Declining or not answering in time re-plans with the next candidate.

"band karo" never calls a tool: the agent explains that only the owner can stop a machine and logs the
request. Nothing here can stop, start or switch a machine.

Phones: owners DHADKAN_PHONE_<factory>; the factory's own mechanic DHADKAN_MECHANIC_<factory>; any
technician DHADKAN_TECH_<id> (e.g. DHADKAN_TECH_T2). With the dry-run provider only (no WhatsApp keys, or
DHADKAN_DRY_RUN=1), missing numbers get demo numbers with country code +0, which no network routes, so the
simulated phones (?view=phones) work without any setup and nothing can reach a real person.
"""

import hashlib
import json
import re
import time
from datetime import datetime
from typing import Any

from .config import ROOT
from .notify import env

PHOTOS = ROOT / "data" / "alerts" / "photos"
REPLY_HOURS_DEFAULT = 4.0
SCHEMA = """
CREATE TABLE IF NOT EXISTS mech_jobs (
  case_id TEXT, technician TEXT, phone TEXT, day TEXT, machine TEXT, factory TEXT, status TEXT, sent REAL,
  deadline REAL, answered REAL, eta_min INTEGER, photo TEXT, photo_sha256 TEXT, note TEXT, lang TEXT,
  PRIMARY KEY (case_id, technician, day));
"""
# job statuses: sent -> accepted | declined | another_time | no_reply ; accepted -> on_the_way -> done
OPEN = ("sent", "accepted", "on_the_way")

J = {  # the mechanic's messages (hi_latn first: what most mechanics type)
    "job": {
        "hi_latn": "Naya kaam (Dhadkan): {machine}, {factory}, {address}.\nKharabi: {fault}. Saath laayein: {part}.\n"
                   "Samay: {day}, {slot}.\nJawab: 1 = Aaunga, 2 = Nahi aa sakta, 3 = Doosra samay chahiye",
        "hi": "नया काम (Dhadkan): {machine}, {factory}, {address}।\nखराबी: {fault}। साथ लाएँ: {part}।\n"
              "समय: {day}, {slot}।\nजवाब: 1 = आऊँगा, 2 = नहीं आ सकता, 3 = दूसरा समय चाहिए",
        "en": "New job (Dhadkan): {machine}, {factory}, {address}.\nFault: {fault}. Bring: {part}.\n"
              "Slot: {day}, {slot}.\nReply: 1 = I'll come, 2 = I can't come, 3 = I need another time",
    },
    "accepted": {
        "hi_latn": "Dhanyavaad. Nikalte waqt 'raste mein 20 min' bhejiye. Kaam ke baad purane part ki photo aur "
                   "'ho gaya: kya kiya' bhejiye.",
        "hi": "धन्यवाद। निकलते समय 'रास्ते में 20 मिनट' भेजिए। काम के बाद पुराने पार्ट की फ़ोटो और 'हो गया: क्या किया' भेजिए।",
        "en": "Thank you. When you leave, send 'on the way 20 min'. After the job, send a photo of the old part and "
              "'done: what you did'.",
    },
    "photo_first": {
        "hi_latn": "Pehle purane part ki photo bhejiye, phir 'ho gaya: kya kiya' likhiye.",
        "hi": "पहले पुराने पार्ट की फ़ोटो भेजिए, फिर 'हो गया: क्या किया' लिखिए।",
        "en": "First send a photo of the old part, then write 'done: what you did'.",
    },
    "photo_ok": {
        "hi_latn": "Photo mil gayi. Ab 'ho gaya: kya kiya' likhiye.",
        "hi": "फ़ोटो मिल गई। अब 'हो गया: क्या किया' लिखिए।",
        "en": "Photo received. Now write 'done: what you did'.",
    },
    "done": {
        "hi_latn": "Shukriya. Machine ki nayi recording se jaanch hogi; nateeja owner ko jaayega.",
        "hi": "शुक्रिया। मशीन की नई रिकॉर्डिंग से जाँच होगी; नतीजा मालिक को जाएगा।",
        "en": "Thanks. The machine is re-recorded and checked; the owner gets the result.",
    },
    "ok": {"hi_latn": "Theek hai, note kar liya.", "hi": "ठीक है, नोट कर लिया।", "en": "Noted."},
    "unknown": {
        "hi_latn": "Samjha nahi. 1 = Aaunga, 2 = Nahi aa sakta, 3 = Doosra samay, 'raste mein 20 min', ya photo + 'ho gaya'.",
        "hi": "समझा नहीं। 1 = आऊँगा, 2 = नहीं आ सकता, 3 = दूसरा समय, 'रास्ते में 20 मिनट', या फ़ोटो + 'हो गया'।",
        "en": "Not understood. 1 = I'll come, 2 = I can't come, 3 = another time, 'on the way 20 min', or photo + 'done'.",
    },
}  # fmt: skip
OWN = {  # what the owner is told about the visit
    "on_the_way": {"hi_latn": "{name} raste mein hain{eta}.", "hi": "{name} रास्ते में हैं{eta}।", "en": "{name} is on the way{eta}."},
    "done": {"hi_latn": "{name} ne {machine} ka kaam pura bataya: {note}. Ab nayi recording se jaanch hogi.",
             "hi": "{name} ने {machine} का काम पूरा बताया: {note}। अब नई रिकॉर्डिंग से जाँच होगी।",
             "en": "{name} reports the job on {machine} done: {note}. It is now re-recorded and checked."},
}  # fmt: skip
SLOT = "10:00-13:00"  # assumption when the factory profile has no shift hours


def say(table: dict[str, dict[str, str]], key: str, lang: str, **kw: Any) -> str:
    t = table[key]
    return (t.get(lang) or t.get("hi_latn") or t["en"]).format(**kw)


def digits(phone: str | None) -> str:
    return "".join(c for c in str(phone or "") if c.isdigit())


def demo_numbers_allowed(alerts: Any) -> bool:
    return getattr(alerts, "provider", "") == "dry_run"


def demo_phone(role: str, key: str) -> str:
    """+0 numbers: no network routes them, so a demo can never message a real person."""
    n = sum(ord(c) for c in key) % 900 + 100
    return f"+0 {'1' if role == 'owner' else '2'}000 {n:03d}"


def owner_phone(alerts: Any, factory: str, machine: str | None = None) -> str | None:
    got = alerts.contact(factory, machine=machine)
    if not got and demo_numbers_allowed(alerts):
        return demo_phone("owner", factory)
    return got


def mechanic_phone(alerts: Any, tech: dict[str, Any], factory: str) -> str | None:
    got = env.get(f"DHADKAN_TECH_{tech['id']}") or (
        env.get(f"DHADKAN_MECHANIC_{factory}") if tech.get("role") == "own_mechanic" else None
    )
    if not got and demo_numbers_allowed(alerts):
        return demo_phone("mechanic", tech["id"])
    return got


def store_photo(data: bytes, mime: str, who: str) -> tuple[str, str]:
    """Keep the photo for the case (no image model: it is for the mechanic and the record). (path, sha256)"""
    PHOTOS.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256(data).hexdigest()
    ext = ".png" if "png" in mime else ".webp" if "webp" in mime else ".jpg"
    path = PHOTOS / f"{re.sub(r'[^A-Za-z0-9_-]', '_', who)}_{int(time.time())}_{sha[:8]}{ext}"
    path.write_bytes(data)
    return str(path), sha


# ---------------------------------------------------------------------- the mechanic on WhatsApp
WORDS = {
    "accept": re.compile(r"^\s*(1|haan|han|ha|yes|ok|aaunga|aaoonga|आऊँगा|हाँ)\b", re.I),
    "decline": re.compile(r"^\s*(2|nahi|nahin|no|नहीं)\b", re.I),
    "another": re.compile(r"^\s*(3|doosra|dusra|another|baad|later|दूसरा)\b", re.I),
    "on_the_way": re.compile(r"raste|rasta|on the way|nikal|aa raha|रास्ते|निकल", re.I),
    "done": re.compile(r"ho gaya|hogaya|done|kaam pura|finished|हो गया", re.I),
}


def parse_mechanic(text: str) -> str | None:
    t = (text or "").strip()
    for k in ("done", "on_the_way", "accept", "decline", "another"):  # longer phrases before 1/2/3
        if WORDS[k].search(t):
            return k
    return None


def eta_minutes(text: str) -> int | None:
    m = re.search(r"(\d{1,3})\s*(min|minute|मिनट|m\b)", text or "", re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d{1,2})\s*(ghanta|ghante|hour|hr|घंटे)", text or "", re.I)
    return int(m.group(1)) * 60 if m else None


class MechanicDesk:
    def __init__(self, brain: Any) -> None:
        self.b = brain
        self.db = brain.store.db
        self.db.executescript(SCHEMA)
        # wall-clock: the reply window is real hours even when the demo's factory clock runs fast
        self.now = time.time

    @property
    def alerts(self) -> Any:
        return self.b.live.alerts

    def reply_hours(self) -> float:
        try:
            return float(env.get("DHADKAN_MECHANIC_REPLY_HOURS") or REPLY_HOURS_DEFAULT)
        except ValueError:
            return REPLY_HOURS_DEFAULT

    def jobs(self, case_id: str | None = None, open_only: bool = False) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM mech_jobs WHERE 1=1", []
        if case_id:
            q, args = q + " AND case_id=?", [case_id]
        rows = [dict(r) for r in self.db.execute(q + " ORDER BY sent", args)]
        return [r for r in rows if r["status"] in OPEN] if open_only else rows

    def _set(self, job: dict[str, Any], **f: Any) -> dict[str, Any]:
        cols = ", ".join(f"{k}=?" for k in f)
        self.db.execute(f"UPDATE mech_jobs SET {cols} WHERE case_id=? AND technician=? AND day=?",
                        (*f.values(), job["case_id"], job["technician"], job["day"]))  # fmt: skip
        self.db.commit()
        return job | f

    def _lang(self, factory: str) -> str:
        return env.get(f"DHADKAN_LANG_MECHANIC_{factory}") or "hi_latn"

    def _text_to(
        self, phone: str | None, machine: str, factory: str, lang: str, body: str, kind: str
    ) -> dict[str, Any]:
        return self.alerts.notifier.send(machine=machine, factory=factory, severity="info", to=phone, lang=lang,
                                         body=body, kind=kind)  # fmt: skip

    # -- the job ----------------------------------------------------------------------------------
    def job_text(self, case: dict[str, Any], tech: dict[str, Any], day: str, lang: str) -> str:
        from . import diagnosis

        m = self.b.c.machines[case["machine"]]
        f = self.b.c.factories[m["factory"]]
        fault = case["context"].get("fault")
        card = (diagnosis.load()["faults"].get(fault) or {}) if fault else {}
        key = "hi_latn" if lang == "hi_latn" else lang
        fault_w = (card.get("name") or {}).get(key) or (card.get("name") or {}).get("en") or (
            "jaanch (inspection)" if lang != "en" else "inspection")  # fmt: skip
        part = ((card.get("fix") or {}).get("part") or {}).get(key) or ((card.get("fix") or {}).get("part") or {}).get(
            "en") or "-"  # fmt: skip
        shifts = f.get("shift_hours") or []
        slot = f"{shifts[0][0]}-{shifts[0][1]}" if shifts and isinstance(shifts[0], list) else SLOT
        return say(J, "job", lang, machine=m["name"], factory=f["name"], address=f.get("area", ""), fault=fault_w,
                   part=part, day=day, slot=slot)  # fmt: skip

    def send_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The transport's book_job (already gated by the owner's approval): text the job to the mechanic."""
        mid, tid, day = payload["machine"], payload["technician"], payload["day"]
        case = self.b.store.open_case(mid)
        if case is None:
            return {
                "id": f"book_job:{str(payload.get('idem', ''))[:10]}",
                "dry_run": True,
                "note": "no open case",
            }
        tech = self.b.agent.t.technicians.get(tid)
        f = self.b.c.machines[mid]["factory"]
        lang = self._lang(f)
        phone = mechanic_phone(self.alerts, tech, f)
        now = self.now()
        self.db.execute("INSERT OR REPLACE INTO mech_jobs (case_id, technician, phone, day, machine, factory, status, sent,"
                        " deadline, lang) VALUES (?,?,?,?,?,?,'sent',?,?,?)",
                        (case["id"], tid, phone, day, mid, f, now, now + self.reply_hours() * 3600, lang))  # fmt: skip
        self.db.commit()
        a = self._text_to(phone, mid, f, lang, self.job_text(case, tech, day, lang), "job")
        self.b.store.log(
            "agent", "job_sent", case["id"], technician=tid, day=day, alert=a["id"], status=a["status"]
        )
        return {"id": f"job:{a['id']}", "status": a["status"], "to": "mechanic", "technician": tid}

    # -- the mechanic's replies ------------------------------------------------------------------------
    def job_for(self, phone: str) -> dict[str, Any] | None:
        d = digits(phone)
        rows = [j for j in self.jobs() if d and digits(j["phone"]) == d]
        live = [j for j in rows if j["status"] in OPEN]
        return (live or rows or [None])[-1]

    def is_mechanic(self, phone: str) -> bool:
        return self.job_for(phone) is not None

    def handle(
        self, phone: str, text: str | None, image: tuple[bytes, str] | None = None
    ) -> dict[str, Any] | None:
        job = self.job_for(phone)
        if job is None:
            return None
        b, lang, cid = self.b, job["lang"] or "hi_latn", job["case_id"]
        out: dict[str, Any] = {"mechanic": job["technician"], "case": cid}
        if image is not None:  # the old part's photo, required before "done"
            path, sha = store_photo(image[0], image[1], f"part_{job['machine']}")
            job = self._set(job, photo=path, photo_sha256=sha)
            with b.lock:
                c = b.store.case(cid)
                photos = [*c["context"].get("photos", []), {"by": "mechanic", "path": path, "sha256": sha, "at": b.clock(),
                                                            "caption": text or ""}]  # fmt: skip
                b.store.update_case(cid, c["version"], context=c["context"] | {"photos": photos})
                b.store.log("technician", "photo", cid, technician=job["technician"], sha256=sha)
            out["photo"] = sha[:12]
            if not text or parse_mechanic(text) != "done":
                self._text_to(
                    phone, job["machine"], job["factory"], lang, say(J, "photo_ok", lang), "mechanic"
                )
                return out
        word = parse_mechanic(text or "")
        out["answer"] = word
        if word == "done":
            if not job.get("photo"):
                self._text_to(
                    phone, job["machine"], job["factory"], lang, say(J, "photo_first", lang), "mechanic"
                )
                b.store.log("agent", "done_without_photo", cid, technician=job["technician"])
                return out | {"refused": "photo of the old part needed first"}
            note = re.sub(WORDS["done"], "", text or "").strip(" :-,.") or "(no note)"
            self._set(job, status="done", note=note[:300], answered=self.now())
            name = b.agent.t.technicians.get(job["technician"])["name"]
            self._tell_owner(job, say(OWN, "done", self._owner_lang(job), name=name, machine=b.c.machines[job["machine"]]["name"],
                                      note=note[:120]))  # fmt: skip
            self._text_to(phone, job["machine"], job["factory"], lang, say(J, "done", lang), "mechanic")
            simulated = not (getattr(b, "feed", None) and b.feed.is_live(job["machine"]))
            b.repair_done(cid, job["technician"], note[:200], simulate_recovery=simulated)
            return out | {"repair_done": True, "simulated_recovery": simulated}
        if word == "on_the_way":
            eta = eta_minutes(text or "")
            self._set(job, status="on_the_way", eta_min=eta, answered=self.now())
            name = b.agent.t.technicians.get(job["technician"])["name"]
            olang = self._owner_lang(job)
            about = {"en": "about", "hi": "लगभग", "mr": "सुमारे", "gu": "લગભગ"}.get(olang, "lagbhag")
            eta_w = "" if eta is None else f", {about} {eta} min"
            self._tell_owner(job, say(OWN, "on_the_way", olang, name=name, eta=eta_w))
            self._text_to(phone, job["machine"], job["factory"], lang, say(J, "ok", lang), "mechanic")
            return out | {"eta_min": eta}
        if job["status"] != "sent" or word not in ("accept", "decline", "another"):
            if word is None:
                self._text_to(
                    phone, job["machine"], job["factory"], lang, say(J, "unknown", lang), "mechanic"
                )
            return out
        answer = {"accept": "accepted", "decline": "declined", "another": "another_time"}[word]
        self._set(job, status=answer, answered=self.now())
        if word == "accept":
            self._text_to(phone, job["machine"], job["factory"], lang, say(J, "accepted", lang), "mechanic")
        else:
            self._text_to(phone, job["machine"], job["factory"], lang, say(J, "ok", lang), "mechanic")
        got = b.ingest("mechanic_reply", case_id=cid, machine=job["machine"], factory=job["factory"], source="whatsapp",
                       idem=f"mech:{cid}:{job['technician']}:{job['day']}:{answer}",
                       payload={"technician": job["technician"], "answer": answer, "day": job["day"], "text": text,
                                "channel": "whatsapp"})  # fmt: skip
        return out | {"planner": got and got.get("decision")}

    def _owner_lang(self, job: dict[str, Any]) -> str:
        return self.alerts.owner_lang(job["machine"])

    def _tell_owner(self, job: dict[str, Any], text: str) -> None:
        self._text_to(owner_phone(self.alerts, job["factory"], job["machine"]), job["machine"], job["factory"],
                      self._owner_lang(job), text, "visit")  # fmt: skip

    # -- no answer ------------------------------------------------------------------------------
    def tick(self) -> list[str]:
        """A job not answered within the reply window: a no-show for planning purposes (re-plan)."""
        now, out = self.now(), []
        for j in self.jobs():
            if j["status"] == "sent" and j["deadline"] and now > j["deadline"]:
                self._set(j, status="no_reply")
                self.b.store.log("agent", "job_unanswered", j["case_id"], technician=j["technician"],
                                 hours=self.reply_hours())  # fmt: skip
                self.b.bus.emit("mechanic_no_show", case_id=j["case_id"], machine=j["machine"], factory=j["factory"],
                                source="agent", idem=f"noreply:{j['case_id']}:{j['technician']}:{j['day']}",
                                payload={"technician": j["technician"], "day": j["day"], "why": "no answer to the job"})  # fmt: skip
                out.append(j["case_id"])
        return out


# ---------------------------------------------------------------------- the owner's commands
CMD = {
    "stop": re.compile(
        r"\b(band\s*karo|band\s*kar\s*do|band\s*kardo|machine\s*band|stop|shut\s*down|बंद\s*करो|बंद\s*कर\s*दो)\b",
        re.I,
    ),
    "status": re.compile(r"^\s*(status|haal|kya\s*haal|machine\s*kaisi|स्थिति|हाल)\b", re.I),
    "cost": re.compile(r"(kitna\s*kharcha|kharcha|kitne\s*paise|cost|कितना\s*खर्च|खर्च)", re.I),
    "mechanic": re.compile(
        r"(kab\s*aayega|kab\s*aaega|mistri\s*kab|mechanic\s*when|when.*mechanic|मिस्त्री\s*कब|कब\s*आएगा)",
        re.I,
    ),
}
R = {
    "stop": {"hi_latn": "Machine band karna sirf aapka faisla hai. Dhadkan kisi machine ko band ya chalu nahi kar sakta. "
                        "Band karni ho to khud switch se band kijiye. Maine yeh note kar liya hai.",
             "hi": "मशीन बंद करना सिर्फ़ आपका फ़ैसला है। Dhadkan किसी मशीन को बंद या चालू नहीं कर सकता। बंद करनी हो तो खुद "
                   "स्विच से बंद कीजिए। मैंने यह नोट कर लिया है।",
             "en": "Stopping a machine is only your decision. Dhadkan cannot stop or start any machine. If it must stop, "
                   "switch it off yourself. I have noted your message."},
    "none_open": {"hi_latn": "Abhi koi khula kaam nahi hai.", "hi": "अभी कोई खुला काम नहीं है।", "en": "No open job right now."},
    "no_visit": {"hi_latn": "{machine}: abhi koi mistri book nahi hai.", "hi": "{machine}: अभी कोई मिस्त्री बुक नहीं है।",
                 "en": "{machine}: no mechanic booked yet."},
}  # fmt: skip
STATUS_W = {"normal": "theek", "watch": "dhyan do", "inspect": "jaanch chahiye", "repair_soon": "jaldi repair",
            "urgent": "turant dhyan", "learning": "seekh raha hai", "no_signal": "signal nahi",
            "not_running": "band hai"}  # fmt: skip


HINGLISH = re.compile(
    r"\b(band|karo|kar|do|kitna|kitne|kharcha|mistri|kab|aayega|aaega|kaisa|kaisi|haal|hai|nahi|kya|"
    r"bhai|ji|paise|abhi)\b",
    re.I,
)


def command_lang(text: str, default: str) -> str:
    """The language to answer a command in: Devanagari -> hi; Roman script with Hindi words -> hi_latn;
    other Roman text -> en; else the factory's default."""
    if re.search(r"[ऀ-ॿ]", text or ""):
        return "hi"
    if HINGLISH.search(text or ""):
        return "hi_latn"
    if re.search(r"[A-Za-z]", text or ""):
        return "en"
    return default


def command_of(text: str) -> str | None:
    for k in ("stop", "mechanic", "cost", "status"):
        if CMD[k].search(text or ""):
            return k
    return None


def owner_command(
    brain: Any, factory: str, text: str, lang: str = "hi_latn", log: bool = True
) -> dict[str, Any] | None:
    """Answer the owner's command from stored facts only (numbers from the case, approvals and faults.yaml).
    None when the text is not a command."""
    kind = command_of(text)
    if kind is None:
        return None
    s = brain.store
    lang = lang if lang in ("hi_latn", "hi", "en") else "hi_latn"
    mids = [m for m, x in brain.c.machines.items() if x["factory"] == factory]
    cases = [c for c in s.cases(open_only=True) if c["machine"] in mids]
    if kind == "stop" and not log:  # the same answer in another script (a voice note): logged once already
        return {"command": "stop", "reply": say(R, "stop", lang), "tool_called": None}
    if kind == "stop":
        # no tool, no action: an explanation and a log line. Nothing here can stop a machine.
        s.log("owner", "stop_requested_by_owner", cases[0]["id"] if cases else None, factory=factory, text=text[:200],
              note="explained that only the owner can stop a machine; no action taken")  # fmt: skip
        return {"command": "stop", "reply": say(R, "stop", lang), "tool_called": None}
    if kind == "status":
        rows = []
        for m in mids:
            h = brain.health.get(m) or {}
            st = h.get("status", "no_signal")
            rows.append(
                f"{brain.c.machines[m]['name']}: {STATUS_W.get(st, st) if lang != 'en' else st.replace('_', ' ')}"
            )
        return {"command": "status", "reply": "\n".join(rows) or say(R, "none_open", lang)}
    if not cases:
        return {"command": kind, "reply": say(R, "none_open", lang)}
    lines = []
    for c in cases:
        name = brain.c.machines[c["machine"]]["name"]
        if kind == "cost":
            ap = [a for a in s.approvals(c["id"]) if a["amount_high"] is not None]
            if ap:
                a = ap[-1]
                word = {"approved": "manzoor", "requested": "aapki manzoori baaki"}.get(a["status"], a["status"]) \
                    if lang != "en" else a["status"]  # fmt: skip
                lines.append(
                    f"{name}: Rs {a['amount_low']:,}-{a['amount_high']:,} ({word}). {a['summary'][:160]}"
                )
            else:
                lines.append(
                    f"{name}: " + ("abhi koi kharcha tay nahi" if lang != "en" else "no cost agreed yet")
                )
        else:  # when is the mechanic coming
            ctx = c["context"]
            jobs = (
                [j for j in brain.desk.jobs(c["id"]) if j["status"] in OPEN] if hasattr(brain, "desk") else []
            )
            if not ctx.get("technician") and not jobs:
                lines.append(say(R, "no_visit", lang, machine=name))
                continue
            j = jobs[-1] if jobs else None
            tname = brain.agent.t.technicians.get((j or {}).get("technician") or ctx["technician"])["name"]
            day = (j or {}).get("day") or ctx.get("visit_day") or "-"
            state = {"sent": "jawab ka intezaar", "accepted": "aane ko taiyar", "on_the_way": "raste mein"}.get(
                (j or {}).get("status", ""), "book") if lang != "en" else (j or {}).get("status", "booked")  # fmt: skip
            eta = f", ~{j['eta_min']} min" if j and j.get("eta_min") else ""
            lines.append(f"{name}: {tname}, {day} ({state}{eta})")
    return {"command": kind, "reply": "\n".join(lines)}


# ---------------------------------------------------------------------- incoming log (for the demo phones)
INBOX = """
CREATE TABLE IF NOT EXISTS wa_inbox (id INTEGER PRIMARY KEY, ts REAL, phone TEXT, text TEXT, kind TEXT, media TEXT,
  simulated INTEGER);
"""


def log_incoming(
    db: Any, phone: str, text: str | None, kind: str, media: str | None, simulated: bool
) -> None:
    db.executescript(INBOX)
    db.execute("INSERT INTO wa_inbox (ts, phone, text, kind, media, simulated) VALUES (?,?,?,?,?,?)",
               (time.time(), phone, text, kind, media, int(simulated)))  # fmt: skip
    db.commit()


def thread(db: Any, phone: str, limit: int = 40) -> list[dict[str, Any]]:
    """One phone's conversation: what Dhadkan sent (alerts table) and what came back (wa_inbox), in order."""
    db.executescript(INBOX)
    d = digits(phone)
    out = [{"ts": r["created"], "dir": "out", "text": r["body"], "kind": r["kind"], "status": r["status"]}
           for r in db.execute("SELECT created, body, kind, status, to_phone FROM alerts ORDER BY id DESC LIMIT 400")
           if digits(r["to_phone"]) == d]  # fmt: skip
    out += [{"ts": r["ts"], "dir": "in", "text": r["text"], "kind": r["kind"], "media": bool(r["media"]),
             "simulated": bool(r["simulated"])}
            for r in db.execute("SELECT * FROM wa_inbox ORDER BY id DESC LIMIT 400") if digits(r["phone"]) == d]  # fmt: skip
    return sorted(out, key=lambda x: x["ts"])[-limit:]


def iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts).isoformat(timespec="minutes") if ts else None


def dumps(x: Any) -> str:
    return json.dumps(x, ensure_ascii=False, default=str)
