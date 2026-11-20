"""Owner alerts for the live cluster: diagnosis card -> WhatsApp text + media -> notifier; replies back.

Phone numbers come only from configuration (DHADKAN_PHONE_<factory>, DHADKAN_SUPERVISOR_<factory>
in .env); the made-up numbers in data/sim/cluster.json are never messaged. Without a number the
alert is logged as `no_contact`; without provider keys it is logged as `dry_run`.
"""

import hashlib
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import diagnosis, forecast, provider, understand
from .config import DATA, NODE_DB
from .notify import env, media, policy, whatsapp
from .notify.service import Notifier, parse_choice

if TYPE_CHECKING:
    from .app import Live

ALERT_DIR = DATA / "alerts"


# Labels of the "3 = details" reply. Misuse causes, warranty notes and the repairable reason exist in
# English only in server/faults.yaml, so those stay English.
DETAIL_LABELS = {
    "en": {"repairable": "Repairable", "yes": "yes", "no": "no", "fix": "Fix", "incl_labour": "incl. labour",
           "caused_by": "Usually caused by", "warranty": "Warranty", "ask_dealer": "ask the dealer",
           "money": "Fix now ~₹{lo}-{hi} (with planned downtime). Wait {d} days: {p}% chance of a breakdown costing ~₹{cost}."},
    "hi": {"repairable": "मरम्मत हो सकती है", "yes": "हाँ", "no": "नहीं", "fix": "इलाज", "incl_labour": "मज़दूरी सहित",
           "caused_by": "आम कारण", "warranty": "वारंटी", "ask_dealer": "डीलर से पूछें",
           "money": "अभी ठीक कराएँ ~₹{lo}-{hi} (तय बंदी सहित)। {d} दिन रुकें तो: {p}% संभावना ब्रेकडाउन की, खर्च ~₹{cost}।"},
    "mr": {"repairable": "दुरुस्ती होऊ शकते", "yes": "हो", "no": "नाही", "fix": "उपाय", "incl_labour": "मजुरीसह",
           "caused_by": "नेहमीची कारणे", "warranty": "वॉरंटी", "ask_dealer": "डीलरला विचारा",
           "money": "आत्ता दुरुस्ती ~₹{lo}-{hi} (ठरवलेल्या बंदीसह). {d} दिवस थांबलात तर: {p}% शक्यता ब्रेकडाउनची, खर्च ~₹{cost}."},
    "hi_latn": {"repairable": "Marammat ho sakti hai", "yes": "haan", "no": "nahi", "fix": "Ilaaj", "incl_labour": "mazdoori sahit",
                "caused_by": "Aam kaaran", "warranty": "Warranty", "ask_dealer": "dealer se poochhein",
                "money": "Abhi theek karayein ~₹{lo}-{hi} (tay bandi sahit). {d} din rukein to: {p}% sambhavna breakdown ki, kharcha ~₹{cost}."},
    "gu": {"repairable": "રિપેર થઈ શકે", "yes": "હા", "no": "ના", "fix": "ઉપાય", "incl_labour": "મજૂરી સાથે",
           "caused_by": "સામાન્ય કારણ", "warranty": "વોરંટી", "ask_dealer": "ડીલરને પૂછો",
           "money": "હમણાં રિપેર ~₹{lo}-{hi} (નક્કી બંધી સાથે). {d} દિવસ રાહ જુઓ તો: {p}% શક્યતા બ્રેકડાઉનની, ખર્ચ ~₹{cost}."},
}  # fmt: skip


class OwnerAlerts:
    def __init__(
        self, live: "Live", node_db: Path | None = None, providers: tuple[Any, Any] | None = None
    ) -> None:
        self.live = live
        wa, sms = providers or whatsapp.from_env()
        self.notifier = Notifier(live.c.db, wa, sms, Path(node_db or NODE_DB).with_name("alerts_outbox.db"))
        self.classified: dict[str, str] = {}  # machine -> fault class from the CWRU-trained classifier
        # Demo mode: machine -> the judge's number / language. Per machine, not per factory, so the
        # judge only hears about the demo machine (never about other machines' false alarms).
        self.contact_override: dict[str, str] = {}
        self.lang_override: dict[str, str] = {}
        self.expert: dict[str, dict[str, Any]] = (
            {}
        )  # machine -> Granite's second look (or the rules fallback)
        from .onboard import Onboarding

        self.onboard = Onboarding(live.c.db, learning_days=int(live.c.cfg.get("learning_days", 7)))

    @property
    def provider(self) -> str:
        return str(self.notifier.wa.name)

    def owner_lang(self, mid: str) -> str:
        """WhatsApp language: Demo mode's choice, else DHADKAN_LANG_<factory> (e.g. hi_latn), else the factory's."""
        f = self.live.c.machines[mid]["factory"]
        return (
            self.lang_override.get(mid)
            or env.get(f"DHADKAN_LANG_{f}")
            or self.live.c.factories[f]["language"]
        )

    def contact(self, factory: str, role: str = "owner", machine: str | None = None) -> str | None:
        if role == "owner" and machine in self.contact_override:
            return self.contact_override[machine]
        return env.get(f"DHADKAN_{'PHONE' if role == 'owner' else 'SUPERVISOR'}_{factory}")

    # -- cards ----------------------------------------------------------------
    def card(self, mid: str, lang: str | None = None) -> dict[str, Any] | None:
        c = self.live.c
        m = c.machines[mid]
        hist = c.history(mid)
        if not hist:
            return None
        rank = next((r for r in self.live.ranking() if r["machine_id"] == mid), {})
        th = c.cfg["tick_hours"]
        est = forecast.from_history(hist, th, m["source"]) if forecast.known(m["source"]) else {}
        level = est.get("level", float("inf"))
        at_fault = 0
        for h in reversed(hist):
            if h["risk"] < level:
                break
            at_fault += 1
        owner_word = rank.get("known_fault")
        fault = diagnosis.fault_for(owner_word=owner_word, classifier=self.classified.get(mid))
        expert = None
        if fault is None and rank.get("status") in ("alarm", "early_match"):  # only when needed
            expert = self.second_look(mid, lang or self.owner_lang(mid), [h["risk"] for h in hist])
            if expert.get("source") == provider.NAME and not expert["mechanic_check_needed"]:
                fault = expert["fault"]
        row = {"id": mid, "type": m["type"], "status": rank.get("status", "ok"), "known_fault": owner_word,
               "forecast": est, "days_at_fault": at_fault * th / 24}  # fmt: skip
        lang = lang or c.factories[m["factory"]]["language"]
        out = diagnosis.card(
            row, m["factory"], fault, m.get("motor_hp", 2), c.cfg["service_hours"][m["type"]], lang
        )
        if out is not None:
            out["days_rising"] = rank.get("days_rising", 0)
            out["named_by"] = (
                "classifier" if mid in self.classified else "owner" if owner_word
                else provider.NAME if expert and expert.get("source") == provider.NAME and fault else None
            )  # fmt: skip
            if expert is not None:  # evidence, confidence, hand check; or "expert check pending" (rules)
                out["expert"] = {k: v for k, v in expert.items() if k != "cost"}
        return out

    def second_look(self, mid: str, lang: str, trend: list[float]) -> dict[str, Any]:
        """Granite on the spectrogram note + trend + profile, once per alert episode (server/understand.py)."""
        if mid in self.expert:
            return self.expert[mid]
        try:
            ex = self.live.explain(mid)
            with self.live.lock:
                before = self.live._audio_of(mid, ex["before"])
                now = self.live._audio_of(mid, ex["now"])
            hot = (ex["lo_band"], ex["hi_band"]) if ex.get("status") == "ok" else None
            pair = media.spectrogram_png(before, now, hot)
        except Exception:
            self.expert[mid] = {
                "source": "rules",
                "expert_check": "pending",
                "why": "no before/now reading yet",
            }
            return self.expert[mid]
        m = self.live.c.machines[mid]
        f = self.live.c.factories[m["factory"]]
        per_day = max(1, round(24 / self.live.c.cfg["tick_hours"]))
        req = understand.build_request(pair, trend[-14 * per_day :], m, f, lang)
        self.expert[mid] = understand.diagnose(req, machine_id=mid)
        return self.expert[mid]

    def text(self, mid: str, lang: str) -> str | None:
        c = self.card(mid, lang)
        if c is None:
            return None
        m = self.live.c.machines[mid]
        name = m["name_local"] if lang in ("hi", "mr") else m["name"]
        return diagnosis.message(c, name, c.get("days_rising", 0), lang)

    def details(self, mid: str, lang: str) -> str:
        """Reply '3': the whole card in the owner's language (labels too; misuse causes are English-only)."""
        c = self.card(mid, lang)
        if c is None or c.get("fault") is None:
            return self.text(mid, lang) or ""
        lg = lang if lang in DETAIL_LABELS else "en"
        L = DETAIL_LABELS[lg]
        fx, money = c["fix"], c["money"]
        lines = [
            f"{c['severity']['label'][lg]} · {c['what'][lg]}",
            f"{L['repairable']}: {L['yes'] if c['repairable']['answer'] else L['no']}. {c['repairable']['why']}",
            f"{L['fix']}: {fx['part'][lg]}; ~₹{fx['total_inr'][0]:,}-{fx['total_inr'][1]:,} {L['incl_labour']};"
            f" {fx['technician'][lg]}.",
            f"{L['caused_by']}: " + "; ".join(x["en"] for x in c["misuse"]) + ".",
            f"{L['warranty']}: " + (c["warranty"].get("period") or c["warranty"].get("note", L["ask_dealer"])),
            L["money"].format(lo=f"{money['fix_now_inr'][0]:,}", hi=f"{money['fix_now_inr'][1]:,}", d=money["wait_days"],
                              p=round(money["p_fault_within_wait"] * 100), cost=f"{money['breakdown_inr']:,}"),
            c["disclaimer"][lg],
        ]  # fmt: skip
        return "\n".join(lines)

    # -- media ----------------------------------------------------------------
    def media(self, mid: str) -> list[dict[str, Any]]:
        """5 s before/now clip and the spectrogram image, from the same readings the explanation uses."""
        try:
            ex = self.live.explain(mid)
        except Exception:
            return []
        if not ex.get("before"):
            return []
        with self.live.lock:
            before = self.live._audio_of(mid, ex["before"])
            now = self.live._audio_of(mid, ex["now"])
        ALERT_DIR.mkdir(parents=True, exist_ok=True)
        stem = ALERT_DIR / f"{mid}_{ex['now']['tick']}"
        clip, png = stem.with_suffix(".ogg"), stem.with_suffix(".png")
        clip.write_bytes(media.before_now_clip(before, now, ex.get("start_s", 0.0)))
        hot = (ex["lo_band"], ex["hi_band"]) if ex.get("status") == "ok" else None
        png.write_bytes(media.spectrogram_png(before, now, hot))
        base = env.get("PUBLIC_BASE_URL")
        return [
            {"path": str(clip), "mime": "audio/ogg", "caption": "",
             "public_url": f"{base}/api/alert-media/{clip.name}" if base else None},
            {"path": str(png), "mime": "image/png", "caption": "before | now",
             "public_url": f"{base}/api/alert-media/{png.name}" if base else None},
        ]  # fmt: skip

    # -- sending --------------------------------------------------------------
    def send_for(
        self, mid: str, message_id: int | None = None, to: str | None = None, lang: str | None = None
    ) -> dict[str, Any] | None:
        m = self.live.c.machines[mid]
        lang = lang or self.owner_lang(mid)
        c = self.card(mid, lang)
        if c is None:
            return None
        sev = c["severity"]["level"]
        now = self.live.c.now()
        when, at = policy.decide(sev, now, self.live.c.factories[m["factory"]])
        return self.notifier.send(
            machine=mid, factory=m["factory"], severity=sev, to=to or self.contact(m["factory"], machine=mid),
            lang=lang, body=self.text(mid, lang) or "", media=self.media(mid), message_id=message_id,
            template=policy.TEMPLATES.get(sev), buttons=policy.BUTTONS.get(lang),
            hold_until=at.timestamp() if when == "hold" and at else None, digest=when == "digest",
        )  # fmt: skip

    def release_due(self) -> dict[str, Any]:
        """On the factory clock: send held alerts whose quiet hours / power cut are over; the daily digest."""
        now = self.live.c.now()
        released = self.notifier.release(now.timestamp())
        digests = self.digest(now)
        return {"released": released, "digests": digests}

    def digest(self, now: Any) -> list[int]:
        """One message per phone with every watch-level alert since the last digest, once digest time has come."""
        out = []
        by_phone: dict[str, list[dict[str, Any]]] = {}
        for a in self.notifier.pending_digest():
            by_phone.setdefault(a["to_phone"], []).append(a)
        for phone, rows in by_phone.items():
            f = self.live.c.factories[rows[0]["factory"]]
            at = now.replace(
                hour=int(f.get("digest_at", "19:00")[:2]), minute=int(f.get("digest_at", "19:00")[3:5])
            )
            if now < at:
                continue
            lang = rows[0]["lang"]
            body = "\n".join(
                [policy.DIGEST_HEAD.get(lang, policy.DIGEST_HEAD["en"]), *(f"- {r['body']}" for r in rows)]
            )
            d = self.notifier.send(machine=rows[0]["machine"], factory=rows[0]["factory"], severity="watch", to=phone,
                                   lang=lang, body=body, kind="digest", template=policy.DIGEST_TEMPLATE)  # fmt: skip
            self.notifier.mark_digested([int(r["id"]) for r in rows], int(d["id"]))
            out.append(int(d["id"]))
        return out

    def after_digest(self, run_id: str) -> list[dict[str, Any]]:
        """One WhatsApp alert per owner message the agent drafted in this run."""
        rows = self.live.c.db.execute("SELECT id, ask FROM messages WHERE run_id=?", (run_id,)).fetchall()
        out = []
        for r in rows:
            sent = self.send_for(r["ask"].split("|")[0], message_id=r["id"])
            if sent:
                out.append(sent)
        return out

    def on_reply(self, phone: str, text: str) -> dict[str, Any] | None:
        """Only '1' books the technician (through the agent's reply path); nothing touches a machine."""
        got = self.notifier.reply(phone, text)
        if got is None:
            return None
        a, choice = got["alert"], got["choice"]
        mid = a["machine"]
        if choice in ("book", "later") and a["message_id"]:
            self.live.reply(a["message_id"], "1" if choice == "book" else "2")
            if choice == "book":  # the owner said yes: their mechanic gets the job (refused without approval)
                from . import fix

                fix.send_job(self, a)
        elif choice == "details":
            self.notifier.send(machine=mid, factory=a["factory"], severity=a["severity"], to=a["to_phone"],
                               lang=a["lang"], body=self.details(mid, a["lang"]), kind="details", parent=a["id"])  # fmt: skip
        self.live.event("owner_reply", machine=mid, choice=choice, text=text)
        self._to_planner("owner_reply", phone, mid, a["factory"],
                         {"text": text, "choice": choice, "channel": "whatsapp", "alert": a["id"]})  # fmt: skip
        return {"alert": a["id"], "choice": choice}

    def on_message(self, phone: str, text: str) -> dict[str, Any] | None:
        """Any text: a mechanic with a job (server/wa_loops.MechanicDesk) first; the owner's commands
        ("status", "kitna kharcha", "kab aayega mistri", "band karo"); then a reply to an open alert; then
        an answer to one of the planner's questions or measurement requests; otherwise onboarding."""
        brain = getattr(self.live, "brain", None)
        desk = getattr(brain, "desk", None)
        if desk is not None and desk.is_mechanic(phone):
            got = desk.handle(phone, text)
            if got is not None:
                return got
        owner = self.factory_of_owner(phone)
        if owner and brain is not None:
            from .wa_loops import command_lang, owner_command

            default = env.get(f"DHADKAN_LANG_{owner}") or self.live.c.factories[owner]["language"]
            lang = command_lang(text, default)  # answer in the language and script the owner typed
            cmd = owner_command(brain, owner, text, lang)
            if cmd is not None:
                self.notifier.send(machine="-", factory=owner, severity="info", to=phone, lang=lang, body=cmd["reply"],
                                   kind="chat")  # fmt: skip
                self.live.event("owner_command", factory=owner, command=cmd["command"])
                return cmd
        got = self.on_reply(phone, text)
        if got is not None:
            return got
        got = self._planner_message(phone, text)
        if got is not None:
            return got
        q = self.onboard.handle(phone, text)
        if q is None:
            return None
        lang = (self.onboard._load(phone) or ({}, {}))[1].get("language", "en")
        self.notifier.send(
            machine="-", factory="-", severity="info", to=phone, lang=lang, body=q, kind="onboarding"
        )
        return {"onboarding": True, "next": q}

    def decide(self, mid: str, choice: str, lang: str | None = None) -> dict[str, Any]:
        """The owner screen's buttons: the same decision path as a WhatsApp reply (only "book" books, and the
        mechanic's job still needs the logged approval). details -> the full card text."""
        lang = lang or self.owner_lang(mid)
        if choice == "details":
            return {"choice": "details", "text": self.details(mid, lang)}
        if choice not in ("book", "later"):
            raise ValueError(choice)
        msg = self.live.c.db.execute(
            "SELECT id FROM messages WHERE ask LIKE ? AND approved IS NULL ORDER BY id DESC LIMIT 1",
            (f"{mid}|%",),
        ).fetchone()
        if msg is None:
            return {"choice": choice, "booked": False, "why": "no open visit request for this machine"}
        self.live.reply(int(msg[0]), "1" if choice == "book" else "2")
        alert = next(
            (a for a in self.notifier.recent(200) if a["machine"] == mid and a["kind"] == "alert"), None
        )
        if alert:
            self.notifier.db.execute(
                "UPDATE alerts SET reply=COALESCE(reply, ?), choice=COALESCE(choice, ?), replied_at=? WHERE id=?",
                ("owner screen", choice, self.notifier.clock(), alert["id"]),
            )
            self.notifier.db.commit()
        job = None
        if choice == "book":
            from . import fix

            row = alert or {"machine": mid, "severity": "repair_soon", "id": None}
            job = fix.send_job(self, {**row, "message_id": int(msg[0])})
        self.live.event("owner_decision", machine=mid, choice=choice, via="owner screen")
        return {"choice": choice, "booked": choice == "book", "job_status": job and job["status"]}

    def on_audio(self, phone: str, audio: bytes, mime: str = "audio/ogg") -> dict[str, Any] | None:
        """A recording: an onboarding baseline clip if this phone is setting up, else a voice note on an alert."""
        if self.onboard.active(phone):
            q = self.onboard.clip(phone, audio, ".ogg" if "ogg" in mime else ".audio")
            if q is None:
                return None
            lang = (self.onboard._load(phone) or ({}, {}))[1].get("language", "en")
            self.notifier.send(
                machine="-", factory="-", severity="info", to=phone, lang=lang, body=q, kind="onboarding"
            )
            return {"onboarding": True, "next": q}
        return self.on_voice(phone, audio, mime)

    def on_voice(self, phone: str, audio: bytes, mime: str = "audio/ogg") -> dict[str, Any] | None:
        """A WhatsApp voice note: local Whisper -> lexicon (Granite for unknown words) -> weak label on the
        machine of this owner's latest alert. The owner's words are a weak label, not an inspection."""
        a = self.notifier.latest_for(phone)
        if a is None:
            return self._voice_for_investigation(phone, audio, mime)
        from . import folk, registry

        ALERT_DIR.mkdir(parents=True, exist_ok=True)
        ext = ".ogg" if "ogg" in mime else ".m4a" if "mp4" in mime or "aac" in mime else ".audio"
        path = ALERT_DIR / f"voice_{a['machine']}_{int(time.time())}{ext}"
        path.write_bytes(audio)
        lang = (
            a["lang"] if a["lang"] in ("hi", "mr", "en") else "hi"
        )  # Whisper has no hi_latn; speech is Hindi
        text, heard = folk.transcribe(str(path), language=lang)
        llm = folk.llm_label if provider.available() else None
        lab = folk.label_text(text, folk.load_lexicon(), llm)
        with self.live.lock:
            registry.add_weak_label(self.live.c.reg, a["factory"], a["machine"], text, heard, lab)
        self.notifier.db.execute("UPDATE alerts SET last_reply=? WHERE id=?", (f"[voice] {text}", a["id"]))
        self.notifier.db.commit()
        self.live.event(
            "owner_voice", machine=a["machine"], text=text, sound=lab.get("sound"), fault=lab.get("fault")
        )
        self._to_planner("owner_voice_note", phone, a["machine"], a["factory"],
                         {"text": text, "audio_path": str(path), "sound": lab.get("sound"), "fault": lab.get("fault"),
                          "channel": "whatsapp", "alert": a["id"]})  # fmt: skip
        return {"alert": a["id"], "machine": a["machine"], "text": text, "label": lab}

    def _voice_for_investigation(self, phone: str, audio: bytes, mime: str) -> dict[str, Any] | None:
        """A recording sent in answer to the planner's request (request_measurement, kind audio): scored
        like a reading of that machine (planner._audio_reply)."""
        brain = getattr(self.live, "brain", None)
        owner = self.factory_of_owner(phone)
        inv = brain.open_investigation(owner) if brain is not None and owner else None
        if inv is None:
            return None
        mid = inv["case"]["machine"]
        ALERT_DIR.mkdir(parents=True, exist_ok=True)
        ext = (
            ".ogg"
            if "ogg" in mime
            else ".wav" if "wav" in mime else ".m4a" if "mp4" in mime or "aac" in mime else ".audio"
        )
        path = ALERT_DIR / f"voice_{mid}_{int(time.time())}{ext}"
        path.write_bytes(audio)
        self.live.event("owner_voice", machine=mid, text=None, measurement=inv["measurement"]["n"])
        got = self._to_planner("owner_voice_note", phone, mid, owner,
                               {"text": "", "audio_path": str(path), "channel": "whatsapp", "alert": f"meas{inv['measurement']['n']}"})  # fmt: skip
        return {
            "machine": mid,
            "measurement": inv["measurement"]["n"],
            "planner": got and got.get("decision"),
        }

    # -- the planner ------------------------------------------------------------
    def _to_planner(self, type_: str, phone: str, machine: str | None, factory: str | None,
                    payload: dict[str, Any]) -> dict[str, Any] | None:  # fmt: skip
        """One event into the planner (server/planner). Keyed by the message, so a webhook retried by the
        provider is handled once."""
        brain = getattr(self.live, "brain", None)
        if brain is None:
            return None
        digits = "".join(c for c in phone if c.isdigit())
        key = hashlib.sha256(f"{digits}|{payload.get('text')}|{payload.get('alert')}|{int(self.notifier.clock()) // 60}"
                             .encode()).hexdigest()[:16]  # fmt: skip
        try:
            return brain.ingest(type_, machine=machine, factory=factory, payload=payload, source="whatsapp",
                                idem=f"{type_}:{key}")  # fmt: skip
        except Exception as e:  # the reply was logged already; the planner must not break the webhook
            self.live.event("planner_failed", type=type_, error=type(e).__name__)
            return None

    def factory_of_owner(self, phone: str) -> str | None:
        """The owner's factory by their number (DHADKAN_PHONE_<f>), or a demo number in a dry run."""
        f = self._factory_of(phone, "PHONE")
        if f:
            return f
        from .wa_loops import demo_numbers_allowed, demo_phone, digits

        if demo_numbers_allowed(self):
            return next(
                (x for x in self.live.c.factories if digits(demo_phone("owner", x)) == digits(phone)), None
            )
        return None

    def on_image(
        self, phone: str, data: bytes, mime: str = "image/jpeg", caption: str | None = None
    ) -> dict[str, Any] | None:
        """A photo: the mechanic's old part (MechanicDesk), or the owner's answer to a photo request,
        stored and attached to the case. No image model looks at it."""
        brain = getattr(self.live, "brain", None)
        desk = getattr(brain, "desk", None)
        if desk is not None and desk.is_mechanic(phone):
            return desk.handle(phone, caption, image=(data, mime))
        owner = self.factory_of_owner(phone)
        if not owner or brain is None:
            return None
        from .wa_loops import store_photo

        inv = brain.open_investigation(owner)
        case = (inv or {}).get("case") or next((c for c in brain.store.cases(open_only=True)
                                                if self.live.c.machines[c["machine"]]["factory"] == owner), None)  # fmt: skip
        if case is None:
            return None
        path, sha = store_photo(data, mime, f"owner_{case['machine']}")
        self.live.event("owner_photo", machine=case["machine"], sha256=sha[:12])
        got = self._to_planner("owner_photo", phone, case["machine"], owner,
                               {"text": caption or "", "path": path, "sha256": sha, "channel": "whatsapp", "alert": sha[:12]})  # fmt: skip
        return {"photo": sha[:12], "case": case["id"], "planner": got and got.get("decision")}

    def _factory_of(self, phone: str, role: str) -> str | None:
        digits = "".join(c for c in phone if c.isdigit())
        for f in self.live.c.factories:
            num = env.get(f"DHADKAN_{role}_{f}")
            if num and "".join(c for c in num if c.isdigit()) == digits:
                return str(f)
        return None

    def _planner_message(self, phone: str, text: str) -> dict[str, Any] | None:
        """Text that is not a reply to an alert: the factory's mechanic answering a job, or the owner
        answering one of the planner's numbered questions."""
        mech = self._factory_of(phone, "MECHANIC")
        if mech:
            word = parse_choice(text)
            answer = {"book": "accepted", "later": "declined"}.get(word or "", "message")
            got = self._to_planner("mechanic_reply", phone, None, mech, {"text": text, "answer": answer,
                                                                        "channel": "whatsapp"})  # fmt: skip
            return {
                "mechanic": True,
                "factory": mech,
                "answer": answer,
                "planner": got and got.get("decision"),
            }
        owner = self.factory_of_owner(phone)
        brain = getattr(self.live, "brain", None)
        if owner and brain is not None and (brain.store.questions(factory=owner, open_only=True)
                                            or brain.open_investigation(owner) or brain.open_approval(owner)):  # fmt: skip
            got = self._to_planner("owner_reply", phone, None, owner, {"text": text, "channel": "whatsapp"})
            return {"planner": got and got.get("decision"), "factory": owner}
        return None

    def tick(self) -> None:
        """Escalate unanswered Stop-now alerts after 2 h; resend anything waiting for the network."""

        def contact(factory: str) -> tuple[str | None, str]:
            return self.contact(factory, "supervisor"), self.live.c.factories[factory]["language"]

        def text(row: Any) -> str:
            lang = (
                row["lang"] or self.live.c.factories[row["factory"]]["language"]
            )  # same language as the alert
            head = {"hi": "मालिक ने 2 घंटे में जवाब नहीं दिया। ", "mr": "मालकांनी 2 तासांत उत्तर दिले नाही. ",
                    "hi_latn": "Maalik ne 2 ghante mein jawab nahi diya. ",
                    "gu": "માલિકે 2 કલાકમાં જવાબ નથી આપ્યો. ",
                    "en": "The owner has not replied in 2 hours. "}.get(lang, "")  # fmt: skip
            return head + row["body"]

        self.notifier.escalate(contact, text)
        self.release_due()
        self.notifier.retry()
