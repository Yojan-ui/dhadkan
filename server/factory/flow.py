"""Who gets which WhatsApp, per machine and stage, and the conversation that follows the owner's reply.

Stages come from the diagnosis card's severity (server/diagnosis.py, from the score trend and the days
range) for the replayed machine:
  watch        -> owner: a heads-up
  repair_soon  -> owner: the decision message (1 / 2 / 3) + supervisor: what to do this shift
  stop_now     -> owner + supervisor, now
  replace      -> owner + supervisor, now, saying plainly it is beyond repair, with the repair-now vs
                  breakdown cost (a new machine's price is not in faults.yaml: "ask the dealer")
One message per machine per stage (stages only go up). An unanswered owner decision gets one reminder
after REMIND_AFTER_S of real time (default 4 h; the replay's clock runs ~1 day in 5 s, so real time).

Replies (POST /whatsapp/twilio): every role defaults to OWNER_PHONE in the demo, so one phone may play all
four roles. A reply goes to the most recent open question for that phone:
  owner decision  1 / Haan -> visit booked through the approval path (messages.approved = 1), then the
                  dealer is asked to reserve the part and the mechanic gets the job (fault, part no., tools,
                  address, slot; reply 1 to accept). 2 / Nahi / Baad -> reminder later. 3 -> full details.
  mechanic        1 -> accepted; "done" / "ho gaya" afterwards -> the owner is asked for a 60 s recording
  dealer          1 -> reserved
  owner recording -> repair verified -> "Theek ho gaya" or "Repair theek nahi laga, part check karein"
  anything else (text or voice note) -> the agent answers.
Nothing here stops a machine; the owner decides.
"""

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import yaml

from .. import whatsapp
from ..notify import env

CONTACTS = Path(__file__).with_name("contacts.yaml")
ORDER = ["watch", "repair_soon", "stop_now", "replace"]
ROLES_FOR = {"watch": {"owner"}, "repair_soon": {"owner", "supervisor"}, "stop_now": {"owner", "supervisor"},
             "replace": {"owner", "supervisor"}}  # fmt: skip
REMIND_AFTER_S = float(os.getenv("DHADKAN_REMIND_AFTER_S", 4 * 3600))
SCHEMA = """
CREATE TABLE IF NOT EXISTS wa_log (id INTEGER PRIMARY KEY, ts REAL, factory_ts TEXT, machine TEXT, direction TEXT,
  role TEXT, phone TEXT, text TEXT, status TEXT, sid TEXT, kind TEXT, stage TEXT);
CREATE TABLE IF NOT EXISTS wa_threads (id INTEGER PRIMARY KEY, ts REAL, machine TEXT, role TEXT, phone TEXT,
  expects TEXT, stage TEXT, answered INTEGER DEFAULT 0, reminded INTEGER DEFAULT 0, context TEXT);
CREATE TABLE IF NOT EXISTS wa_stages (machine TEXT, stage TEXT, ts REAL, PRIMARY KEY (machine, stage));
CREATE TABLE IF NOT EXISTS wa_faults (machine TEXT PRIMARY KEY, fault TEXT);
"""
CHOICE = {
    "1": {"1", "haan", "han", "ha", "yes", "हाँ", "हां", "हो", "mechanic", "bulao", "accept"},
    "2": {"2", "nahi", "na", "no", "baad", "later", "नहीं", "ना", "नाही", "nantar"},
    "3": {"3", "detail", "details", "poori", "jaankari", "mahiti"},
}
DONE = re.compile(
    r"\b(done|ho gaya|ho gya|ho gayi|kaam ho gaya|repair ho gaya|theek kar diya|hogaya)\b", re.I
)


def expand(value: str) -> str:
    """${A} or ${A:-B}: env A, else env B (contacts default to OWNER_PHONE)."""

    def sub(m: re.Match[str]) -> str:
        a, _, b = m.group(1).partition(":-")
        return env.get(a) or (env.get(b) if b else "") or ""

    return re.sub(r"\$\{([^}]+)\}", sub, value)


def parse_choice(text: str) -> str | None:
    words = set(re.findall(r"[\wऀ-ॿ]+", text.lower()))
    hits = {k for k, vocab in CHOICE.items() if words & vocab}
    return hits.pop() if len(hits) == 1 else None


CHOICE_THREADS = ("decision", "accept", "reserve")  # threads a numeric reply (1/2/3, Haan/Nahi) can answer


class Flow:
    def __init__(self, live: Any, contacts_path: Path = CONTACTS) -> None:
        self.live = live
        self.cfg = yaml.safe_load(contacts_path.read_text(encoding="utf-8"))
        # the live state's own RLock: the flow shares its sqlite connection, so one lock serialises both
        self.lock = getattr(live, "lock", None) or threading.RLock()
        self.live.c.db.executescript(SCHEMA)

    # ---------------------------------------------------------------- facts (tool handlers use these)
    def contact(self, role: str) -> dict[str, Any]:
        c = dict(self.cfg["contacts"][role])
        c["phone"] = expand(str(c.get("phone", "")))
        return c

    def display(self, mid: str) -> dict[str, str]:
        m = self.live.c.machines[mid]
        return {
            "name": m["name"],
            "shed": (
                self.cfg["factory"]["shed"] if m["factory"] == self.cfg["factory"]["id"] else m["factory"]
            ),
        }

    def card(self, mid: str, lang: str = "en") -> dict[str, Any] | None:
        with self.lock:  # reads the shared connection (history)
            return self.live.alerts.card(mid, lang)

    def alerted(self, mid: str, lang: str = "hi_latn") -> dict[str, Any] | None:
        """The highest stage WhatsApp has alerted for this machine, with its faults.yaml label (phone screen)."""
        from .. import diagnosis as kb

        done = [r[0] for r in self.live.c.db.execute("SELECT stage FROM wa_stages WHERE machine=?", (mid,))]
        if not done:
            return None
        st = max(done, key=ORDER.index)
        labels = kb.load()["severity"]["labels"].get(st, {})
        return {"level": st, "label": labels.get(lang) or labels.get("en")}

    def risk_line(self, mid: str) -> str:
        """'₹1,56,960 khatre mein (2 din)': the owner phone screen's line (chance within 2 days x breakdown cost)."""
        r = next((x for x in self.live.ranking() if x["machine_id"] == mid), {})
        v = int(r.get("rupees_at_risk") or 0)
        return f"{inr(v)} khatre mein (2 din)" if v > 0 else ""

    def health(self, mid: str) -> dict[str, Any]:
        r = next((x for x in self.live.ranking() if x["machine_id"] == mid), {})
        est = r.get("days_to_fault_level") or {}
        if est.get("status") == "estimate":
            left = f"{est.get('low')}-{est.get('high')} days to fault level"
        elif est.get("status") == "at_fault_level":
            left = "already at fault level"
        else:
            left = "no reliable estimate yet"
        return {"machine": mid, "status": r.get("status"), "score_vs_alarm": r.get("risk"), "days_rising": r.get("days_rising"),
                "time_left": left, "trend_last_6": [round(h["risk"], 2) for h in self.live.c.history(mid, 6)],
                "summary": f"{r.get('status')} at {round((r.get('risk') or 0) * 100)}% of alarm; {left}"}  # fmt: skip

    def diagnosis(self, mid: str) -> dict[str, Any]:
        c = self.card(mid, "en")
        if not c or not c.get("fault"):
            return {
                "fault": None,
                "summary": "fault not named yet; a mechanic should check",
                "severity": c and c["severity"]["level"],
            }
        fix, money = c["fix"], c["money"]
        return {"fault": c["fault"], "what": c["what"]["en"], "severity": c["severity"]["level"],
                "repairable": c["repairable"]["answer"], "repairable_why": c["repairable"]["why"],
                "part": fix["part"]["en"], "tools": self._kb_tools(c["fault"]), "price_inr_part": fix.get("price_inr"),
                "fix_now_inr_incl_labour": fix["total_inr"], "labour_hours": fix["labour_hours"],
                "technician": fix["technician"]["en"], "if_it_breaks": c["if_ignored"]["what"]["en"],
                "breakdown_repair_inr": c["if_ignored"]["repair_inr"], "breakdown_total_inr": money["breakdown_inr"],
                "misuse_to_avoid": [x["en"] for x in c["misuse"]], "warranty": c["warranty"].get("period") or c["warranty"].get("note"),
                "note": "prices are typical, confirm with dealer",
                "summary": f"{c['fault']} ({c['severity']['level']}); part {fix['part']['en']}"}  # fmt: skip

    @staticmethod
    def _kb_tools(fault: str) -> list[str]:
        from .. import diagnosis as dx

        return list(dx.load()["faults"].get(fault, {}).get("fix", {}).get("tools", []))

    def mechanic(self, mid: str) -> dict[str, Any]:
        from ..owner_agent import AgentTools, ist

        slot = AgentTools(self.live, "flow", "hi_latn").slot(mid)
        c = self.contact("mechanic")
        return {
            "name": c["name"],
            "own_mechanic": True,
            "free_slot": ist(slot),
            "summary": f"{c['name']}, free {ist(slot)}",
        }

    def stock(self, mid: str, part: str = "") -> dict[str, Any]:
        from .. import costs, fix

        nums = re.findall(
            r"\b\d{4,5}\b", part or ""
        )  # match the dealer list by the part number actually asked for
        rows = [r for r in costs.load()["parts"] if nums and any(n in r["part"] for n in nums)]
        if rows:
            r = rows[0]
            s = f"{r['part']} at {r['dealer']}: " + (
                "in stock" if r["in_stock"] else f"{r['lead_days']} days"
            )
        elif part:
            s = "not in the dealer list; dealer to confirm"
        else:
            c = self.card(mid, "en") or {}
            s = fix.stock(c.get("fault"), self.live.c.machines[mid]["type"])
        dealer = self.contact("dealer")["name"]
        return {
            "dealer": dealer,
            "part_asked": part,
            "stock": s,
            "note": "demo dealer data",
            "summary": f"{dealer}: {s} (demo)",
        }

    # ---------------------------------------------------------------- log and send
    def log(self, mid: str, direction: str, role: str, phone: str | None, text: str, status: str = "",
            sid: str | None = None, kind: str = "message", stage: str | None = None) -> None:  # fmt: skip
        with self.lock:
            now = self.live.c.now().isoformat(timespec="minutes") if self.live.c.tick_no() >= 0 else None
            self.live.c.db.execute(
                "INSERT INTO wa_log (ts, factory_ts, machine, direction, role, phone, text, status, sid, kind, stage)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), now, mid, direction, role, phone, text, status, sid, kind, stage),
            )
            self.live.c.db.commit()
        self.live.event("whatsapp", machine=mid, direction=direction, role=role, msg_kind=kind)

    def send(self, mid: str, role: str, text: str, stage: str | None = None, by: str = "flow",
             expects: str | None = None, context: dict[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
        c = self.contact(role)
        phone = c["phone"]
        if not phone:
            self.log(mid, "out", role, None, text, "no_contact", kind=by, stage=stage)
            return {"status": "no_contact"}
        try:
            res = whatsapp.send(phone, text)
        except Exception as e:  # offline or Twilio error: logged, the demo continues
            res = {"sid": None, "status": f"failed: {type(e).__name__}"}
        self.log(mid, "out", role, phone, text, res["status"], res.get("sid"), kind=by, stage=stage)
        # only the stage's own decision message (or an explicit `expects`) opens a question; confirmations and
        # reminders do not, so a reminder never reminds about itself
        decision = (
            role == "owner"
            and by in ("agent", "template")
            and stage in ("repair_soon", "stop_now", "replace")
        )
        exp = expects or ("decision" if decision else None)
        if exp:
            with self.lock:
                self.live.c.db.execute(
                    "INSERT INTO wa_threads (ts, machine, role, phone, expects, stage, context) VALUES (?,?,?,?,?,?,?)",
                    (time.time(), mid, role, phone, exp, stage, json.dumps(context or {})),
                )
                self.live.c.db.commit()
        return res

    # ---------------------------------------------------------------- stages
    def stage_of(self, mid: str) -> str | None:
        c = self.card(mid, "en")
        return c["severity"]["level"] if c else None

    def on_reading(self, mid: str, background: bool = True) -> str | None:
        """After each replay step: send the stage's messages once, when the stage goes up."""
        st = self.stage_of(mid)
        if st is None:
            return None
        with self.lock:
            done = {
                r[0] for r in self.live.c.db.execute("SELECT stage FROM wa_stages WHERE machine=?", (mid,))
            }
            top = max((ORDER.index(s) for s in done), default=-1)
            caught_up = st in done or ORDER.index(st) <= top
            if not caught_up:
                self.live.c.db.execute(
                    "INSERT INTO wa_stages (machine, stage, ts) VALUES (?,?,?)", (mid, st, time.time())
                )
                self.live.c.db.commit()
        if caught_up:  # outside the lock: the diagnosis reads live state that has locks of its own
            self._fault_update(mid, ORDER[top] if top >= 0 else st)
            return None
        if background:
            threading.Thread(target=self._dispatch, args=(mid, st), daemon=True).start()
        else:
            self._dispatch(mid, st)
        return st

    def _dispatch(self, mid: str, stage: str) -> None:
        from .. import wa_agent

        self.log(
            mid,
            "agent",
            "agent",
            None,
            f"stage {stage}: messages to {sorted(ROLES_FOR[stage])}",
            kind="tool",
            stage=stage,
        )
        wa_agent.compose(self, mid, stage, set(ROLES_FOR[stage]))
        self._note_fault(mid)

    def _note_fault(self, mid: str, fault: str | None = None) -> None:
        f = (
            fault if fault is not None else (self.diagnosis(mid).get("fault") or "")
        )  # read before taking the lock
        with self.lock:
            self.live.c.db.execute("INSERT OR REPLACE INTO wa_faults (machine, fault) VALUES (?,?)", (mid, f))
            self.live.c.db.commit()

    def _fault_update(self, mid: str, stage: str) -> None:
        """A stage message went out before the fault was named; once it is, the owner gets it too (one update)."""
        if stage not in ("repair_soon", "stop_now", "replace"):
            return
        row = self.live.c.db.execute("SELECT fault FROM wa_faults WHERE machine=?", (mid,)).fetchone()
        fault = self.diagnosis(mid).get("fault")
        if row is None or row[0] or not fault:
            return
        self._note_fault(mid, fault)
        with self.lock:  # the new message carries the decision; the earlier one is superseded
            self.live.c.db.execute("UPDATE wa_threads SET answered=1 WHERE machine=? AND role='owner' AND "
                                   "expects='decision' AND answered=0", (mid,))  # fmt: skip
            self.live.c.db.commit()
        d = self.display(mid)
        tag = f"{d['name']} ({d['shed']}):"
        text = self.template(mid, "owner", stage).replace(tag, f"{tag} Update, kharabi pehchan li.", 1)
        self.send(mid, "owner", text, stage=stage, by="update", expects="decision")

    def tick(self) -> list[int]:
        """One reminder per unanswered owner decision after REMIND_AFTER_S of real time."""
        due = self.live.c.db.execute(
            "SELECT * FROM wa_threads WHERE expects='decision' AND answered=0 AND reminded=0 AND ts <= ?",
            (time.time() - REMIND_AFTER_S,),
        ).fetchall()
        for t in due:
            self.live.c.db.execute("UPDATE wa_threads SET reminded=1 WHERE id=?", (t["id"],))
            self.live.c.db.commit()
            d = self.display(t["machine"])
            self.send(t["machine"], "owner", f"[Owner] Yaad dila rahe hain: {d['name']} ({d['shed']}) par aapka jawab baaki hai. "
                      f"{wa_agent_options(self)}", stage=t["stage"], by="reminder")  # fmt: skip
        return [int(t["id"]) for t in due]

    # ---------------------------------------------------------------- templates (also the API-failure fallback)
    def template(self, mid: str, role: str, stage: str) -> str:
        d = self.display(mid)
        c = self.card(mid, "hi_latn") or {}
        dx = self.diagnosis(mid)
        h = self.health(mid)
        tag = f"[{role.capitalize()}] {d['name']} ({d['shed']}):"
        from .. import diagnosis as kb

        # the label of the stage this message is about (faults.yaml, as on the phone screen); the live card's label
        # can swing between readings
        sev = kb.load()["severity"]["labels"].get(stage, {}).get("hi_latn")
        risk = self.risk_line(mid)
        if not dx.get("fault"):
            what, fixc, brk, cause, warr = (
                (c.get("unknown") or {}).get("hi_latn")
                or "awaaz badli hai, kharabi ka naam pakka nahi; mistri ko dikhana chahiye.",
                None,
                None,
                None,
                None,
            )
        else:
            what = c.get("what", {}).get("hi_latn") or dx["what"]
            lo, hi = dx["fix_now_inr_incl_labour"]
            fixc, brk = f"₹{lo:,}-{hi:,}", f"₹{dx['breakdown_repair_inr']:,}"
            cause = dx["misuse_to_avoid"][0] if dx["misuse_to_avoid"] else None
            warr = (
                "dealer se pakka karein"
                if not dx.get("warranty") or "No published" in str(dx["warranty"])
                else dx["warranty"]
            )
        left = {"already at fault level": "abhi fault level par hai", "no reliable estimate yet": "abhi pakka andaaz nahi"}.get(
            h["time_left"], h["time_left"].replace(" days to fault level", " din me fault level"))  # fmt: skip
        if role == "owner":
            if stage == "watch":
                return f"{tag} {what} Andaaz: {left}. Abhi kuch karne ki zaroorat nahi, hum nazar rakh rahe hain."
            head = "Ye machine ab marammat layak nahi lagti (beyond repair). " if stage == "replace" else ""
            label = f"{sev}. " if sev else ""
            parts = [f"{tag} {label}{head}{what}", f"{risk + '. ' if risk else ''}Samay: {left}."]
            if fixc:
                parts.append(
                    f"Abhi theek karayein: {fixc}; tootne par: {brk} se zyada (+ band machine ka nuksaan)."
                )
                if stage == "replace":
                    parts.append("Nayi machine ka daam dealer se poochhein.")
            if cause:
                parts.append(f"Dhyan rakhein: {cause}.")
            if warr:
                parts.append(f"Warranty: {warr}.")
            parts.append(wa_agent_options(self))
            text = " ".join(parts)
            return (
                text
                if len(text) <= 700
                else " ".join([parts[0], parts[1], parts[2] if fixc else "", wa_agent_options(self)])
            )
        if role == "supervisor":
            if stage in ("stop_now", "replace"):
                return (f"{tag} URGENT. {what} Maalik ko bata diya hai. Unke faisle tak load kam rakhein, har ghante aawaz aur garmi check "
                        "karein. Machine band karne ka faisla maalik ka hai.")  # fmt: skip
            return f"{tag} {what} Is shift me load kam rakhein aur har 2 ghante aawaz aur garmi check karein. Faisla maalik ka hai."
        return f"{tag} {what}"

    # ---------------------------------------------------------------- replies
    def latest_open(self, phone: str, expects: tuple[str, ...] | None = None) -> Any:
        """Newest unanswered thread for this phone; `expects` narrows it (in the demo every role shares one phone)."""
        digits = "".join(ch for ch in phone if ch.isdigit())
        for t in self.live.c.db.execute("SELECT * FROM wa_threads WHERE answered=0 ORDER BY id DESC"):
            same = "".join(ch for ch in (t["phone"] or "") if ch.isdigit()) == digits
            if same and (expects is None or t["expects"] in expects):
                return t
        return None

    def machine_for(self, phone: str) -> str:
        t = self.latest_open(phone)
        return t["machine"] if t else self._default_machine()

    def inbound(self, phone: str, text: str | None, audio: bytes | None = None) -> dict[str, Any]:
        if audio is not None:
            want: tuple[str, ...] | None = ("recording",)
        elif text and DONE.search(text):
            want = ("done",)
        elif parse_choice(text or ""):
            want = CHOICE_THREADS
        else:
            want = None
        t = (self.latest_open(phone, want) if want else None) or self.latest_open(phone)
        mid = t["machine"] if t else self._default_machine()
        self.log(
            mid,
            "in",
            t["role"] if t else "unknown",
            phone,
            text if text is not None else "(voice note)",
            kind="reply",
        )
        if audio is not None and t is not None and t["expects"] == "recording":
            return self._verify(t, audio)
        if audio is not None:
            text = self._transcribe(audio)
            self.log(mid, "agent", "agent", None, f"voice note transcribed: {text}", kind="tool")
        if t is not None and text and DONE.search(text) and t["expects"] == "done":
            return self._ask_recording(t)
        choice = parse_choice(text or "")
        if t is not None and choice:
            return self._answer(t, choice)
        return self.send_current(mid)

    def _close(self, tid: int) -> None:
        self.live.c.db.execute("UPDATE wa_threads SET answered=1 WHERE id=?", (tid,))
        self.live.c.db.commit()

    def _answer(self, t: Any, choice: str) -> dict[str, Any]:
        mid, role = t["machine"], t["role"]
        if t["expects"] == "decision":
            if choice == "3":
                self.send(
                    mid,
                    "owner",
                    "[Owner] " + self.live.alerts.details(mid, "hi_latn")[:1400],
                    stage=t["stage"],
                    by="details",
                )
                return {"choice": "details"}
            self._close(t["id"])
            if choice == "2":
                self.send(
                    mid,
                    "owner",
                    "[Owner] Theek hai, baad mein yaad dilayenge.",
                    stage=t["stage"],
                    by="flow",
                    expects="decision",
                )
                return {"choice": "later"}
            return self._book(t)
        self._close(t["id"])
        if t["expects"] == "accept" and choice == "1":
            self.send(
                mid,
                "mechanic",
                "[Mechanic] Shukriya. Kaam ho jaye to 'done' bhejein.",
                stage=t["stage"],
                by="flow",
                expects="done",
            )
            return {"choice": "mechanic_accepted"}
        if t["expects"] == "reserve" and choice == "1":
            self.send(mid, "dealer", "[Dealer] Shukriya, part reserve ho gaya.", stage=t["stage"], by="flow")
            return {"choice": "dealer_reserved"}
        return {"choice": choice, "role": role}

    def _book(self, t: Any) -> dict[str, Any]:
        """The owner said 1: the visit is approved through the existing approval path, then dealer + mechanic."""
        from .. import agent, fix
        from ..owner_agent import DAYS

        mid = t["machine"]
        mech = self.mechanic(mid)
        row = self.live.c.db.execute("SELECT id FROM messages WHERE ask LIKE ? AND approved IS NULL ORDER BY id DESC LIMIT 1",
                                     (f"{mid}|%",)).fetchone()  # fmt: skip
        if row is None:
            day = DAYS[self.live.c.now().weekday()]
            agent.Tools(self.live.c, "wa-flow", "owner").draft_owner_alert(mid, "rising", day)
            row = self.live.c.db.execute(
                "SELECT id FROM messages WHERE ask LIKE ? ORDER BY id DESC LIMIT 1", (f"{mid}|%",)
            ).fetchone()
        self.live.reply(int(row[0]), "1")  # the owner's 1, logged: the only thing that books
        if not fix.approved(self.live.c.db, int(row[0])):
            return {"choice": "book", "booked": False}
        dx = self.diagnosis(mid)
        d = self.display(mid)
        part = dx.get("part") or "inspection only"
        qty = 2 if dx.get("fault") == "bearing_wear" else 1
        stock = self.stock(mid, part)
        addr = self.cfg["factory"]["address"]
        self.send(mid, "owner", f"[Owner] Theek hai. {mech['name']} ko {mech['free_slot']} ke liye bula rahe hain, dealer se part reserve "
                  "karwa rahe hain.", stage=t["stage"], by="flow")  # fmt: skip
        self.send(mid, "dealer", f"[Dealer] {self.contact('dealer')['name']}: {self.cfg['factory']['name']} ke liye {part} x{qty} reserve "
                  f"karein ({mech['free_slot']} tak). Stock (demo): {stock['stock']}. Reserve karne ke liye 1 bhejein.",
                  stage=t["stage"], by="flow", expects="reserve")  # fmt: skip
        self.send(mid, "mechanic", f"[Mechanic] Kaam: {d['name']}, {d['shed']}, {self.cfg['factory']['name']}, {addr}. Kharabi: "
                  f"{dx.get('what') or 'awaaz badli hai, check karein'}. Part: {part} x{qty}. Tools: {', '.join(dx.get('tools') or ['spanner set'])}. "
                  f"Slot: {mech['free_slot']}. Accept karne ke liye 1 bhejein.", stage=t["stage"], by="flow", expects="accept")  # fmt: skip
        return {"choice": "book", "booked": True}

    def _ask_recording(self, t: Any) -> dict[str, Any]:
        self._close(t["id"])
        mid = t["machine"]
        d = self.display(mid)
        self.send(mid, "owner", f"[Owner] {d['name']} ki repair ho gayi. Machine chalu karke phone uske paas rakhein aur 60 second ki "
                  "recording (voice note) bhejein, hum check karenge.", stage=t["stage"], by="flow", expects="recording")  # fmt: skip
        return {"choice": "repair_done"}

    def _verify(self, t: Any, audio: bytes) -> dict[str, Any]:
        from .. import prove

        self._close(t["id"])
        mid = t["machine"]
        rec_dir = Path(env.get("DHADKAN_RECORDINGS") or "data/alerts")
        rec_dir.mkdir(parents=True, exist_ok=True)
        (rec_dir / f"after_{mid}_{int(time.time())}.audio").write_bytes(audio)
        after, note = prove.demo_after_readings(self.live, mid)
        rec = prove.make_record(self.live, mid, after, self.contact("mechanic")["name"], None, demo_note=note)
        ok = rec["record"]["verification"]["outcome"] == "fixed"
        text = "Theek ho gaya" if ok else "Repair theek nahi laga, part check karein"
        self.log(
            mid,
            "agent",
            "agent",
            None,
            f"verify_repair: {rec['record']['verification']['outcome']} ({note})",
            kind="tool",
        )
        self.send(mid, "owner", f"[Owner] {text}. (Demo: checked on the replay's readings; record {rec['sha256'][:10]})",
                  stage=t["stage"], by="flow")  # fmt: skip
        return {"choice": "verified", "outcome": rec["record"]["verification"]["outcome"]}

    def _transcribe(self, audio: bytes) -> str:
        from .. import folk

        path = Path("data/alerts") / f"wa_voice_{int(time.time() * 1000)}.audio"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(audio)
        try:
            text, _ = folk.transcribe(str(path), language="hi")
            return text
        except Exception as e:
            return f"(could not transcribe: {type(e).__name__})"

    def send_current(self, mid: str) -> dict[str, Any]:
        """Any other message from the owner: the machine's current alert, with 1/2/3 when a decision is due."""
        st = (self.alerted(mid) or {}).get("level") or self.stage_of(mid)
        if st in ("repair_soon", "stop_now", "replace"):
            with self.lock:  # this message carries the decision now
                self.live.c.db.execute("UPDATE wa_threads SET answered=1 WHERE machine=? AND role='owner' AND "
                                       "expects='decision' AND answered=0", (mid,))  # fmt: skip
                self.live.c.db.commit()
            self.send(
                mid, "owner", self.template(mid, "owner", st), stage=st, by="current", expects="decision"
            )
            return {"choice": "current_alert", "stage": st}
        d = self.display(mid)
        text = (self.template(mid, "owner", st) if st == "watch"
                else f"[Owner] {d['name']} ({d['shed']}): abhi sab theek chal raha hai. Kuch badla to yahin batayenge.")  # fmt: skip
        self.send(mid, "owner", text, stage=st, by="current")
        return {"choice": "current_status", "stage": st}

    def _ask_agent(self, mid: str, text: str) -> dict[str, Any]:
        from .. import owner_agent

        out = owner_agent.chat(
            self.live, text or self.display(mid)["name"], self.live.c.machines[mid]["factory"], None
        )
        for s in out["steps"]:
            self.log(mid, "agent", "agent", None, f"{s['tool']}: {s['summary']}", kind="tool")
        self.send(mid, "owner", "[Owner] " + out["reply"][:1400], by="agent")
        return {"choice": "agent", "reply": out["reply"]}

    def _default_machine(self) -> str:
        demo = getattr(self.live, "demo", {}) or {}
        return demo.get("machine") or next(iter(self.live.c.machines))

    def timeline(self, mid: str) -> list[dict[str, Any]]:
        rows = self.live.c.db.execute("SELECT * FROM wa_log WHERE machine=? ORDER BY id", (mid,)).fetchall()
        return [{"id": r["id"], "ts": r["ts"], "factory_ts": r["factory_ts"], "direction": r["direction"], "role": r["role"],
                 "text": r["text"], "status": r["status"], "kind": r["kind"], "stage": r["stage"],
                 "phone": mask(r["phone"])} for r in rows]  # fmt: skip

    def reset(self) -> None:
        with self.lock:
            self.live.c.db.executescript("DELETE FROM wa_log; DELETE FROM wa_threads; DELETE FROM wa_stages;")


def inr(v: int) -> str:
    """Indian digit grouping, as on the phone screen: 156960 -> '₹1,56,960'."""
    d = str(abs(int(v)))
    head, tail = d[:-3], d[-3:]
    while len(head) > 2:
        tail = head[-2:] + "," + tail
        head = head[:-2]
    return "₹" + (head + "," + tail if head else tail)


def mask(phone: str | None) -> str | None:
    """Show a phone as +91•••••43210 in the UI."""
    if not phone:
        return phone
    d = "".join(c for c in phone if c.isdigit())
    return f"+{d[:2]}•••••{d[-5:]}" if len(d) > 7 else phone


def wa_agent_options(flow: Flow) -> str:
    from ..wa_agent import OPTIONS, OPTIONS_MR

    return OPTIONS_MR if flow.contact("owner")["language"] == "mr" else OPTIONS
