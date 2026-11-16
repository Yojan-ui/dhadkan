"""Owner alerts for the live cluster: diagnosis card -> WhatsApp text + media -> notifier; replies back.

Phone numbers come only from configuration (DHADKAN_PHONE_<factory>, DHADKAN_SUPERVISOR_<factory>
in .env); the made-up numbers in data/sim/cluster.json are never messaged. Without a number the
alert is logged as `no_contact`; without provider keys it is logged as `dry_run`.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import diagnosis, forecast
from .config import DATA, NODE_DB
from .notify import env, media, whatsapp
from .notify.service import Notifier

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
        row = {"id": mid, "type": m["type"], "status": rank.get("status", "ok"), "known_fault": owner_word,
               "forecast": est, "days_at_fault": at_fault * th / 24}  # fmt: skip
        lang = lang or c.factories[m["factory"]]["language"]
        out = diagnosis.card(
            row, m["factory"], fault, m.get("motor_hp", 2), c.cfg["service_hours"][m["type"]], lang
        )
        if out is not None:
            out["days_rising"] = rank.get("days_rising", 0)
            out["named_by"] = "classifier" if mid in self.classified else ("owner" if owner_word else None)
        return out

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
        return self.notifier.send(
            machine=mid, factory=m["factory"], severity=c["severity"]["level"], to=to or self.contact(m["factory"], machine=mid),
            lang=lang, body=self.text(mid, lang) or "", media=self.media(mid), message_id=message_id,
        )  # fmt: skip

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
        elif choice == "details":
            self.notifier.send(machine=mid, factory=a["factory"], severity=a["severity"], to=a["to_phone"],
                               lang=a["lang"], body=self.details(mid, a["lang"]), kind="details", parent=a["id"])  # fmt: skip
        self.live.event("owner_reply", machine=mid, choice=choice, text=text)
        return {"alert": a["id"], "choice": choice}

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
                    "en": "The owner has not replied in 2 hours. "}.get(lang, "")  # fmt: skip
            return head + row["body"]

        self.notifier.escalate(contact, text)
        self.notifier.retry()
