"""WhatsApp for the command centre's maintenance agent (server/brain.py -> server/maint/agent.py).

Outward: the agent's `notify` actions (owner / supervisor) and `book_job` (the factory's own mechanic) go out
through server/whatsapp.py (Twilio). The agent already retries failed sends: a provider error is raised as
ToolError, so the action is queued and retried with backoff.

Inward: the owner's reply "1" / "2" (optionally with the ref, "1 K7Q2") decides the newest booking the agent
asked this phone about. Booking still happens only through MaintenanceAgent.act after that approval.

Safety, in code:
  * Off unless DHADKAN_CC_WHATSAPP=1, and DHADKAN_DRY_RUN=1 still stops every real send (server/whatsapp.py).
  * Only numbers from .env are messaged (DHADKAN_PHONE_<F> / OWNER_PHONE, DHADKAN_SUPERVISOR_<F>,
    DHADKAN_MECHANIC_<F>). The directory numbers in data/ are made up and are never used.
  * At most CAP messages per phone per hour; the rest are logged as "held_rate_cap" (still in the dashboard).
  * Nothing here can stop a machine or spend money: a reply only records the owner's decision.
"""

import hashlib
import re
import time
from typing import Any

from . import whatsapp as wa
from .maint.tools import ToolError
from .notify import env

CAP = 12  # messages per phone per hour
SCHEMA = """
CREATE TABLE IF NOT EXISTS wa_cc (id INTEGER PRIMARY KEY, ts REAL, dir TEXT, phone TEXT, role TEXT,
  machine TEXT, case_id TEXT, approval TEXT, ref TEXT, kind TEXT, text TEXT, status TEXT, sid TEXT);
"""
YES = {"1", "haan", "han", "ha", "yes", "book", "हाँ", "हां"}
NO = {"2", "nahi", "na", "no", "later", "baad", "नहीं", "ना"}


def ref_for(approval_id: str) -> str:
    return hashlib.sha1(approval_id.encode()).hexdigest()[:4].upper()


def digits(phone: str) -> str:
    return "".join(c for c in phone if c.isdigit())


def mask(phone: str) -> str:
    d = digits(phone)
    return f"+{d[:2]}…{d[-3:]}" if len(d) > 5 else "…"


def parse(text: str) -> tuple[str | None, str | None]:
    """'1' -> ('approved', None); '2 k7q2' -> ('rejected', 'K7Q2'); anything else -> (None, None)."""
    words = re.findall(r"[\wऀ-ॿ]+", (text or "").lower())
    if not words:
        return None, None
    decision = "approved" if words[0] in YES else "rejected" if words[0] in NO else None
    ref = next((w.upper() for w in words[1:] if re.fullmatch(r"[0-9a-f]{4}", w)), None)
    return decision, ref


class CCWhatsApp:
    def __init__(self, brain: Any) -> None:
        self.brain = brain
        self.db = brain.store.db
        self.db.executescript(SCHEMA)
        self._lock = brain.lock  # the brain's RLock: one SQLite connection shared between threads

    # ---------------------------------------------------------------- settings
    @staticmethod
    def enabled() -> bool:
        return env.get("DHADKAN_CC_WHATSAPP") == "1"

    def mode(self) -> str:
        if not self.enabled():
            return "off"
        if not wa.configured():
            return "not_configured"
        return "dry_run" if env.get("DHADKAN_DRY_RUN") == "1" else "live"

    def phone(self, role: str, machine: str | None) -> str | None:
        f = (self.brain.c.machines.get(machine) or {}).get("factory", "A") if machine else "A"
        if role == "supervisor":
            return env.get(f"DHADKAN_SUPERVISOR_{f}")
        if role == "mechanic":
            return env.get(f"DHADKAN_MECHANIC_{f}")
        return env.get(f"DHADKAN_PHONE_{f}") or env.get("OWNER_PHONE")

    # ---------------------------------------------------------------- outward (Transport.send)
    def send(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        machine = payload.get("machine")
        case_id = (payload.get("idem") or "").split(":")[0] or None
        if kind == "book_job":
            role = "mechanic"
            tech = self.brain.agent.t.technicians.get(payload["technician"])
            text = (f"Dhadkan job: {self._mname(machine)} on {payload['day']} ({str(payload['work']).replace('_', ' ')}). "
                    f"Booked by the owner (approval {payload.get('approval')}). Technician on record: {tech['name']}.")  # fmt: skip
        elif kind == "notify":
            role = payload.get("to", "owner")
            text = payload["text"]
        else:
            return {"id": f"{kind}:dry", "dry_run": True}
        approval = f"{case_id}:booking" if "Reply 1 to book" in text or "1 to book" in text else None
        ref = ref_for(approval) if approval else None
        if ref:
            text = f"{text} (ref {ref})"
        body = f"Dhadkan · {self._mname(machine)}\n{text}"
        to = self.phone(role, machine)
        status, sid = self._deliver(to, body)
        self._log("out", to, role, machine, case_id, approval, ref, kind, body, status, sid)
        if self.brain.activity:
            self.brain.activity.record(
                "execution", f"WhatsApp to {role}", "completed", machine=machine, case_id=case_id,
                tools=["whatsapp.send"], result={"status": status, "to": mask(to) if to else None},
                reasoning=f"{kind} for {role}: {status}")  # fmt: skip
        return {"id": sid or f"wa:{status}", "status": status, "to": mask(to) if to else None,
                "dry_run": status != "sent"}  # fmt: skip

    def _deliver(self, to: str | None, body: str) -> tuple[str, str | None]:
        if not self.enabled():
            return "off", None
        if not to:
            return "no_number", None
        with self._lock:
            n = self.db.execute("SELECT COUNT(*) FROM wa_cc WHERE dir='out' AND phone=? AND ts>? AND status='sent'",
                                (digits(to), time.time() - 3600)).fetchone()[0]  # fmt: skip
        if n >= CAP:
            return "held_rate_cap", None
        try:
            res = wa.send(to, body)
        except Exception as e:  # provider/network: the agent queues the action and retries
            raise ToolError(f"whatsapp: {type(e).__name__}") from e
        st = res.get("status")
        return ("sent" if res.get("sid") else st or "unknown"), res.get("sid")

    def _mname(self, machine: str | None) -> str:
        m = self.brain.c.machines.get(machine) if machine else None
        if not m:
            return machine or "factory"
        f = self.brain.factories.get(m["factory"], {})
        return f"{m['name']} ({f.get('name', m['factory'])})"

    def _log(self, d: str, phone: str | None, role: str, machine: Any, case_id: Any, approval: Any, ref: Any,
             kind: str, text: str, status: str, sid: Any) -> None:  # fmt: skip
        with self._lock:
            self.db.execute(
                "INSERT INTO wa_cc (ts, dir, phone, role, machine, case_id, approval, ref, kind, text, status, sid)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    time.time(),
                    d,
                    digits(phone or ""),
                    role,
                    machine,
                    case_id,
                    approval,
                    ref,
                    kind,
                    text,
                    status,
                    sid,
                ),
            )
            self.db.commit()

    # ---------------------------------------------------------------- inward (webhook)
    def pending_ask(self, phone: str, ref: str | None) -> dict[str, Any] | None:
        """Newest booking question sent to this phone that is still waiting for an answer."""
        rows = self.db.execute(
            "SELECT * FROM wa_cc WHERE dir='out' AND phone=? AND approval IS NOT NULL ORDER BY id DESC",
            (digits(phone),),
        ).fetchall()
        for r in rows:
            if ref and r["ref"] != ref:
                continue
            a = self.brain.store.approval(r["approval"])
            if a and a["status"] == "requested":
                return dict(r)
        return None

    def inbound(self, phone: str, text: str | None, sid: str | None = None,
                other_ts: float | None = None) -> dict[str, Any] | None:  # fmt: skip
        """Handle the reply if it answers one of this agent's booking questions; None lets the caller route it.
        `other_ts`: when another flow's open question to this phone is newer, a bare "1" belongs to that one.
        """
        if not self.enabled():
            return None
        decision, ref = parse(text or "")
        if decision is None:
            return None
        ask = self.pending_ask(phone, ref)
        if ask is None or (ref is None and other_ts is not None and other_ts > ask["ts"]):
            return None
        self._log("in", phone, "owner", ask["machine"], ask["case_id"], ask["approval"], ask["ref"], "reply",
                  (text or "")[:300], decision, sid)  # fmt: skip
        out = self.brain.decide(ask["approval"], decision, by="owner", channel="whatsapp",
                                ref=f"whatsapp:{sid or int(time.time())}")  # fmt: skip
        word = (
            "Booked: the agent will confirm the mechanic's visit."
            if decision == "approved"
            else "OK, not now. The agent keeps watching the machine and will tell you if it gets worse."
        )
        wa.send(phone, f"Dhadkan · {self._mname(ask['machine'])}\n{word}")
        return {"approval": ask["approval"], "decision": decision, "result": out}

    def messages(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM wa_cc ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(r), "phone": mask(r["phone"]) if r["phone"] else None} for r in rows]
