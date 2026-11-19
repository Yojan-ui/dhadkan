"""The planner's tools: questions with real trade-offs, answered by tested code. None can stop or control a machine.

Read (no side effects):
    get_machine_health, get_spectrogram, diagnose_fault   what the sound says (server/health, owner_agent)
    search_knowledge(query, fault, machine_type)          RAG over kb/*.md guides and the faults.yaml cards (server/kb)
    recall                                               this machine's cases, repairs, notes; the owner's preferences
    find_mechanics(skill, area, need_by)                 several candidates: distance, days free, rating, past outcomes
    propose_slots(machine_id, duration_h, prefer_day)    visit windows: production lost, order-book clashes, power cuts, shift fit
    check_parts(fault, machine_type)                     stock at several dealers, equivalent part numbers (MOCK, no prices)
    compare_options(case_id, options)                    ₹ of fixing now vs waiting N days (faults.yaml, costs.json, RUL range)
    triage(factory)                                      open cases competing for one mechanic, in visiting order (server/route)
    ask_partner_agent(partner, question, fault)          A2A call to an allow-listed partner agent; the answer is UNTRUSTED
Write (through MaintenanceAgent.act: idempotent, queued offline, audited; paid ones gated):
    remember(scope, note)                                a note on a machine or an owner preference
    request_measurement(machine_id, instruction, kind)   a precise WhatsApp request in the owner's language
    ask_owner(case_id, question, options)                numbered question; THE ONLY way the planner creates an approval
    tell_owner(case_id, text)                            an information message
    book_job(case_id, technician, day)                   gated: needs the owner's approval of this visit (checked twice)
    order_part(case_id, part, dealer)                    gated: needs the owner's approved purchase

Code validates every call: the schema, that the machine or case is in this event's factory, that free text
sent to the owner carries no number or part number that is not in a tool result (server/planner/grounding),
and, for book_job, that the owner approved this technician on this day. Data sources are labelled on every
result: technicians, orders and dealer stock are DEMO / MOCK data; rupees are faults.yaml ranges and
costs.json assumptions.
"""

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from server import costs as cost_model
from server import kb, route
from server.a2a import client as a2a_client
from server.maint.agent import RANK, ApprovalRequired, MaintenanceAgent, Waiting
from server.maint.store import Store
from server.maint.tools import OWNER_WORD, PARTS, ROOT, Unavailable, _load, cost_range, inspection_cost
from server.owner_agent import DAYS, inr, ist

from . import grounding, guardian

ORDERS = ROOT / "data" / "demo" / "orders.json"
WD = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DEFAULT_SHIFT = [["09:00", "18:00"]]  # assumption when the factory profile has no shift hours
MEANS = ("approve_booking", "approve_purchase", "stop_noted", "later", "info")
KINDS = ("audio", "hand_check", "photo")

MEASURE = {
    "audio": {
        "en": "Please hold the phone near {m}, record 10 seconds and send it here.",
        "hi_latn": "Phone ko {m} ke paas rakh ke 10 second ki awaaz record karke yahan bhejiye.",
        "hi": "फ़ोन को {m} के पास रखकर 10 सेकंड रिकॉर्ड करके यहाँ भेजिए।",
        "mr": "आवाज तपासायचा आहे: फोन {m} जवळ ठेवून 10 सेकंद रेकॉर्ड करून इथे पाठवा.",
        "gu": "ફોન {m} પાસે રાખીને 10 સેકન્ડ રેકોર્ડ કરીને અહીં મોકલો.",
        "ta": "தொலைபேசியை {m} அருகில் வைத்து 10 வினாடி பதிவு செய்து இங்கே அனுப்புங்கள்.",
    },
    "hand_check": {
        "en": "When {m} is switched off, touch its bearing housing. Reply 1 = hot, 2 = loose or shaking, 3 = fine.",
        "hi_latn": "Jab {m} band ho, uski bearing housing ko haath lagaiye. Jawab: 1 = garam, 2 = dheeli/hil rahi, 3 = theek.",
        "hi": "जब {m} बंद हो, उसकी बेयरिंग हाउसिंग को हाथ लगाइए। जवाब: 1 = गरम, 2 = ढीली/हिल रही, 3 = ठीक।",
        "mr": "{m} बंद असताना बेअरिंग हाउसिंगला हात लावा. ती गरम आहे का? उत्तर: 1 = गरम, 2 = सैल/हलते, 3 = ठीक.",
        "gu": "{m} બંધ હોય ત્યારે બેરિંગ હાઉસિંગને હાથ લગાડો. જવાબ: 1 = ગરમ, 2 = ઢીલું/હલે છે, 3 = બરાબર.",
        "ta": "{m} அணைந்திருக்கும் போது பேரிங் ஹவுசிங்கைத் தொடுங்கள். பதில்: 1 = சூடு, 2 = தளர்வு, 3 = சரி.",
    },
    "photo": {
        "en": "Please send one clear photo of {m}'s belt and bearing.",
        "hi_latn": "{m} ki belt aur bearing ki ek saaf photo bhejiye, jo theek se dikhe.",
        "hi": "{m} की बेल्ट और बेयरिंग की एक साफ़ फ़ोटो भेजिए।",
        "mr": "{m} च्या बेल्ट आणि बेअरिंगचा एक स्पष्ट फोटो हवा आहे, इथे पाठवा.",
        "gu": "{m} ના બેલ્ટ અને બેરિંગનો એક સ્પષ્ટ ફોટો મોકલો.",
        "ta": "{m} இன் பெல்ட் மற்றும் பேரிங்கின் ஒரு தெளிவான புகைப்படம் அனுப்புங்கள்.",
    },
}  # fmt: skip


def _schema(props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


S, N, INT = {"type": "string"}, {"type": "number"}, {"type": "integer"}
NS = {"type": ["string", "null"]}
MID = {"machine_id": {**S, "description": "machine id, e.g. A-G1"}}
CID = {"case_id": {**S, "description": "case id, e.g. A-G1#1"}}
OPTION = {"type": "object", "additionalProperties": False, "required": ["label", "means", "technician", "day"],
          "properties": {"label": {**S, "description": "short words the owner reads after the number"},
                         "means": {"type": "string", "enum": list(MEANS)},
                         "technician": {**NS, "description": "technician id, only for approve_booking"},
                         "day": {**NS, "description": "visit day YYYY-MM-DD, only for approve_booking"}}}  # fmt: skip
WAIT = {"type": "object", "additionalProperties": False, "required": ["label", "wait_days"],
        "properties": {"label": S, "wait_days": {**INT, "description": "0 = fix now"}}}  # fmt: skip

SPECS: list[dict[str, Any]] = [
    {"name": "get_machine_health", "description": "Status, urgency, confidence with reasons, plain-word evidence, recent risk, time to the maintenance threshold.", "input_schema": _schema(MID)},
    {"name": "get_spectrogram", "description": "Which frequency band changed most versus this machine's own normal, in words.", "input_schema": _schema(MID)},
    {"name": "diagnose_fault", "description": "The named fault and its card (faults.yaml) if the sound names one; otherwise the likely faults for this kind of machine.", "input_schema": _schema(MID)},
    {"name": "search_knowledge", "description": "Search the maintenance knowledge base (kb/ guides and the faults.yaml fault cards): symptoms, safe hand checks with the machine off, what the mechanic does, prevention, typical rupee ranges with their sources. Returns chunks with ids; cite the ids you rely on.", "input_schema": _schema({"query": S, "fault": {**NS, "description": "faults.yaml key, e.g. bearing_wear, or null"}, "machine_type": {**NS, "description": "pump, fan, valve, slider, gearbox, or null"}})},
    {"name": "recall", "description": "This machine's earlier cases and repairs (who fixed it, did it hold), notes about it, and the owner's preferences.", "input_schema": _schema(MID)},
    {"name": "remember", "description": "Keep a note for later: scope 'machine:<id>' (e.g. 'belt replaced in March') or 'owner:<factory>' (e.g. 'prefers Sunday visits', 'budget limit Rs 2000'). Only facts the owner or a tool said.", "input_schema": _schema({"scope": S, "note": S})},
    {"name": "find_mechanics", "description": "Candidates with this skill (a faults.yaml key or 'inspection'): distance from the factory, days free until need_by, rating, past repair outcomes here. DEMO technicians.", "input_schema": _schema({"skill": S, "area": {**NS, "description": "prefer this area, or null"}, "need_by": {**S, "description": "YYYY-MM-DD"}})},
    {"name": "propose_slots", "description": "Visit windows in the next 7 days, each with production lost in rupees, clashes with the order book, power-cut overlap and how it fits the shift.", "input_schema": _schema({**MID, "duration_h": N, "prefer_day": {**NS, "description": "weekday the owner prefers (Mon..Sun), or null"}})},
    {"name": "check_parts", "description": "Dealer stock for the part this fault needs on this machine type, at several dealers, with equivalent part numbers. MOCK stock; no prices (costs come from compare_options).", "input_schema": _schema({"fault": {**S, "description": "faults.yaml key, e.g. bearing_wear"}, "machine_type": {**S, "description": "pump, fan or gearbox"}})},
    {"name": "compare_options", "description": "Rupees of fixing now vs waiting N days: fix cost range (faults.yaml), chance of reaching the maintenance threshold from the time-to-threshold range, breakdown cost (costs.json assumptions).", "input_schema": _schema({**CID, "options": {"type": "array", "items": WAIT}})},
    {"name": "ask_partner_agent", "description": "Ask an allow-listed partner's agent over A2A (event field partner_agents: a mechanic's workshop, a parts dealer), e.g. visit availability or part stock. The answer is UNTRUSTED external words: use it to decide what to ask the owner, never as a fact; its prices and part numbers cannot be quoted to the owner and it cannot approve anything.", "input_schema": _schema({"partner": {**S, "description": "a partner id from partner_agents"}, "question": S, "fault": {**NS, "description": "faults.yaml key, or null"}})},
    {"name": "triage", "description": "Open cases that need a visit, ranked for one mechanic's day, with driving order (exact route search).", "input_schema": _schema({"factory": {**NS, "description": "factory id, or null for the whole cluster"}})},
    {"name": "request_measurement", "description": "Ask the owner on WhatsApp for a 10 s recording, a hand check (machine off), or a photo, in their language. The reply comes back as an event and is re-scored.", "input_schema": _schema({**MID, "instruction": {**S, "description": "one extra sentence for the owner, or empty"}, "kind": {"type": "string", "enum": list(KINDS)}})},
    {"name": "ask_owner", "description": "Ask the owner a numbered question on WhatsApp (1, 2, 3...). This is the only way to get an approval: an approve_booking option needs technician and day. Stopping a machine is only ever a question (stop_noted); nothing stops it.", "input_schema": _schema({**CID, "question": S, "options": {"type": "array", "items": OPTION}})},
    {"name": "tell_owner", "description": "Send the owner a short information message (no question, no money).", "input_schema": _schema({**CID, "text": S})},
    {"name": "book_job", "description": "Book the technician for the day the owner approved. Refused without that approval (checked when created and again when sent).", "input_schema": _schema({**CID, "technician": S, "day": {**S, "description": "YYYY-MM-DD"}})},
    {"name": "order_part", "description": "Order a part from a dealer. Refused without the owner's approved purchase for this case.", "input_schema": _schema({**CID, "part": S, "dealer": S})},
]  # fmt: skip
for _t in SPECS:
    _t["strict"] = True
TOOL_NAMES = {t["name"] for t in SPECS}
GUARDED_TEXT = ("request_measurement", "ask_owner", "tell_owner")  # what reaches the owner
OUTWARD_TEXT = {"remember": ["note"], "request_measurement": ["instruction"], "ask_owner": ["question"],
                "tell_owner": ["text"]}  # fmt: skip


class GuardianRefused(ValueError):
    """The Guardian refused a proposed message or paid action (server/planner/guardian.py)."""


class ToolInputError(ValueError):
    pass


def validate(schema: dict[str, Any], value: Any, path: str = "input") -> None:
    """The subset of JSON Schema the tool specs use (types, enum, required, additionalProperties: false)."""
    t = schema.get("type")
    types = t if isinstance(t, list) else [t] if t else []
    py = {
        "string": str,
        "number": (int, float),
        "integer": int,
        "array": list,
        "object": dict,
        "null": type(None),
    }
    if types and not any(isinstance(value, py[x]) and not (x in ("number", "integer") and isinstance(value, bool))
                         for x in types):  # fmt: skip
        raise ToolInputError(f"{path}: expected {'/'.join(types)}")
    if "enum" in schema and value not in schema["enum"]:
        raise ToolInputError(f"{path}: must be one of {schema['enum']}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        missing = [k for k in schema.get("required", []) if k not in value]
        extra = [k for k in value if k not in props] if schema.get("additionalProperties") is False else []
        if missing or extra:
            raise ToolInputError(f"{path}: missing {missing}, unexpected {extra}")
        for k, v in value.items():
            validate(props[k], v, f"{path}.{k}")
    if isinstance(value, list) and "items" in schema:
        for i, v in enumerate(value):
            validate(schema["items"], v, f"{path}[{i}]")


def _h(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:10]


def _hm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


@dataclass
class Env:
    """Everything the tools may touch for one event."""

    store: Store
    agent: MaintenanceAgent
    factories: dict[str, dict[str, Any]] = field(default_factory=dict)
    health_of: Callable[[str], dict[str, Any] | None] = lambda _m: None
    live: Any = None
    route_cfg: dict[str, Any] = field(default_factory=dict)
    factory: str | None = None  # the event's factory: tools refuse machines and cases outside it
    lang: str = "en"
    event_idem: str = "-"
    owner_text: str = ""
    on_grounding_reject: Callable[[str, dict[str, Any]], None] | None = None
    ctx: dict[str, Any] | None = None  # the event context the model was given (knowledge, case)
    on_guardian: Callable[[str, dict[str, Any]], None] | None = None  # every Guardian verdict, for the audit
    a2a_http: Any = None  # the HTTP client for partner agents (tests pass a fake)


def load_orders(path: Any = None) -> list[dict[str, Any]]:
    try:
        return list(_load(path or ORDERS)["orders"])
    except FileNotFoundError:
        return []


def order_due(o: dict[str, Any], now: datetime) -> datetime:
    """The next due_weekday at due_time at or after now (demo orders always have a deadline ahead)."""
    wd = [d.lower()[:3] for d in DAYS].index(o["due_weekday"].lower()[:3])
    h, m = _hm(o.get("due_time", "18:00"))
    d = now.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=(wd - now.weekday()) % 7)
    return d if d >= now else d + timedelta(days=7)


class ToolBox:
    def __init__(self, env: Env) -> None:
        self.e = env
        self.s, self.a = env.store, env.agent
        self.results: list[dict[str, Any]] = []  # what the tools returned this run: the grounding sources
        self.rejections: list[dict[str, Any]] = []
        self.verdicts: list[dict[str, Any]] = []  # the Guardian's, in order
        self.asked_options: list[str] = []
        self.legacy = None
        if env.live is not None and hasattr(env.live, "c"):
            from server.owner_agent import AgentTools

            self.legacy = AgentTools(env.live, f"planner-{env.event_idem[:24]}", env.lang)

    # ------------------------------------------------------------------ dispatch
    @property
    def specs(self) -> list[dict[str, Any]]:
        return SPECS

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.a.clock())

    def extra_grounding(self) -> list[str]:
        """Text whose numbers the owner may see again: their own words, machine names, the options asked."""
        names = []
        for mid in self._machines():
            try:
                names += [mid, str(self.s.machine(mid)["name"])]
            except KeyError:
                continue
        return [self.e.owner_text, *names, *self.asked_options]

    def dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Validate, run, record. Unknown tools and bad input come back as errors; nothing else happens."""
        if name not in TOOL_NAMES:
            return {"error": f"unknown tool {name!r}; the tools are: {', '.join(sorted(TOOL_NAMES))}"}
        spec = next(t for t in SPECS if t["name"] == name)
        try:
            validate(spec["input_schema"], args)
            for k in OUTWARD_TEXT.get(name, []):
                self._grounded(name, str(args.get(k) or ""))
            if name == "ask_owner":
                for o in args["options"]:
                    self._grounded(name, o["label"])
            if name in GUARDED_TEXT:  # option labels are the owner's own answers ("Maine band kar di"): no G1
                text = " ".join(str(args.get(k) or "") for k in OUTWARD_TEXT[name])
                labels = " ".join(o["label"] for o in args.get("options", []))
                self._guard(
                    name, guardian.review_text(text, self.facts(), check_numbers=False, labels=labels)
                )
            if name in guardian.PAID:
                self._guard(name, guardian.review_paid(self.s, self.a, name, args))
            out = getattr(self, f"t_{name}")(**args)
        except ToolInputError as e:
            out = {"error": str(e)}
        except ApprovalRequired as e:
            out = {"error": f"refused by the approval gate: {e}", "gate": "approval_required"}
        except GuardianRefused as e:
            out = {"error": f"refused by the Guardian: {e}", "gate": "guardian"}
        except Exception as e:  # a tool bug must not crash the planner turn
            out = {"error": f"{type(e).__name__}: {e}"}
        self.results.append({"tool": name, "result": out})
        return out

    def trusted_results(self) -> list[Any]:
        """Tool results that may ground a figure: not the words of an external (A2A) agent."""
        return [
            r["result"]
            for r in self.results
            if not (isinstance(r["result"], dict) and r["result"].get("untrusted"))
        ]

    def facts(self) -> guardian.Facts:
        return guardian.facts_from(self.results, self.e.ctx, self.extra_grounding())

    def _guard(self, target: str, v: dict[str, Any]) -> None:
        rec = {"target": target, **v}
        self.verdicts.append(rec)
        if self.e.on_guardian:
            self.e.on_guardian(target, rec)
        if not v["ok"]:
            raise GuardianRefused("; ".join(f"{r['rule']}: {r['detail']}" for r in v["reasons"]))

    def _grounded(self, tool: str, text: str) -> None:
        if not text:
            return
        g = grounding.check(text, self.trusted_results(), self.extra_grounding())
        if not g["ok"]:
            rec = {"tool": tool, "text": text[:300], **g}
            self.rejections.append(rec)
            if self.e.on_grounding_reject:
                self.e.on_grounding_reject(tool, rec)
            raise ToolInputError(f"grounding: {g['unknown_numbers'] + g['unknown_parts']} are not in any tool "
                                 "result; use only numbers and part numbers the tools returned")  # fmt: skip

    # ------------------------------------------------------------------ scope
    def _machines(self) -> list[str]:
        rows = self.s.db.execute("SELECT machine, factory FROM machines").fetchall()
        return [r["machine"] for r in rows if not self.e.factory or r["factory"] == self.e.factory]

    def _machine(self, mid: str) -> dict[str, Any]:
        try:
            m = self.s.machine(mid)
        except KeyError as e:
            raise ToolInputError(f"unknown machine {mid}") from e
        if self.e.factory and m["factory"] != self.e.factory:
            raise ToolInputError(f"machine {mid} is not in this factory")
        return m

    def _case(self, cid: str) -> dict[str, Any]:
        try:
            c = self.s.case(cid)
        except KeyError as e:
            raise ToolInputError(f"unknown case {cid}") from e
        self._machine(c["machine"])
        if c["closed"]:
            raise ToolInputError(f"case {cid} is closed ({c['outcome']})")
        return c

    def _profile(self, factory: str) -> dict[str, Any]:
        return self.e.factories.get(factory) or {}

    def _send(self, c: dict[str, Any], purpose: str, text: str) -> dict[str, Any]:
        try:
            r = self.a.act(c["id"], "notify", purpose, {"to": self.a.t.contacts.get("owner", "owner"), "text": text,
                                                         "machine": c["machine"]})  # fmt: skip
            return {"status": "sent", "result": r}
        except Waiting:
            return {"status": "queued", "note": "offline: queued, goes out once when the network is back"}
        except Unavailable as e:
            return {"status": "refused", "error": str(e)}

    # ------------------------------------------------------------------ read tools
    def t_get_machine_health(self, machine_id: str) -> dict[str, Any]:
        self._machine(machine_id)
        h = self.e.health_of(machine_id)
        if not h and self.legacy and machine_id in self.e.live.c.machines:
            return self.legacy.get_machine_health(machine_id)
        if not h:
            return {"status": "no_signal", "summary": "no assessed reading for this machine yet"}
        keep = ("status", "urgency", "severity", "trend", "confidence", "confidence_reasons", "evidence",
                "recent_risk", "time_to_threshold", "next_inspection", "note")  # fmt: skip
        out = {k: h.get(k) for k in keep}
        out["evidence"] = (out.get("evidence") or [])[:5]
        tt = out.get("time_to_threshold") or {}
        out["summary"] = (f"{h.get('status')} ({h.get('urgency')}, {h.get('confidence')} confidence); "
                          f"time to threshold: {tt.get('status', 'unknown')}")  # fmt: skip
        return out

    def t_get_spectrogram(self, machine_id: str) -> dict[str, Any]:
        self._machine(machine_id)
        if self.legacy and machine_id in self.e.live.c.machines:
            try:
                out = self.legacy.get_spectrogram(machine_id)
                if "error" not in out:
                    return out
            except Exception:
                pass
        ex = (self.e.health_of(machine_id) or {}).get("explanation")
        if not ex:
            return {"status": "no explanation", "summary": "no clear band change recorded"}
        return {"explanation": ex, "summary": "what changed versus its own normal (edge/detectors explain)"}

    def t_diagnose_fault(self, machine_id: str) -> dict[str, Any]:
        m = self._machine(machine_id)
        if self.legacy and machine_id in self.e.live.c.machines:
            out = self.legacy.diagnose_fault(machine_id)
            if out.get("fault"):
                return out
        likely = self.a._likely_faults(m)
        c = self.s.open_case(machine_id)
        named = (c or {}).get("context", {}).get("fault")
        return {"fault": named, "likely_faults": likely, "machine_type": m["mtype"],
                "summary": f"named: {named}" if named else
                f"sound does not name the fault; likely for a {m['kind']}: {', '.join(likely)} (inspection decides)"}  # fmt: skip

    def t_search_knowledge(self, query: str, fault: str | None, machine_type: str | None) -> dict[str, Any]:
        hits = kb.search(query, k=4, fault=fault, machine_type=machine_type)
        return {"chunks": kb.brief(hits), "method": hits[0]["method"] if hits else "none",
                "source": "knowledge base: kb/*.md guides and server/faults.yaml (prices TYPICAL, NOT A QUOTE)",
                "summary": f"{len(hits)} chunks: " + ", ".join(h["id"] for h in hits)}  # fmt: skip

    def t_ask_partner_agent(self, partner: str, question: str, fault: str | None) -> dict[str, Any]:
        try:
            a2a_client.partner(partner)
        except KeyError as e:
            raise ToolInputError(str(e).strip('"')) from None
        data = {"fault": fault, "from": self.now().date().isoformat()}
        out = a2a_client.ask(partner, question, {k: v for k, v in data.items() if v}, http=self.e.a2a_http)
        self.s.log("planner", "a2a_call", None, partner=partner, question=question[:200], ok="error" not in out,
                   said=(out.get("said") or out.get("error") or "")[:300])  # fmt: skip
        return out

    def t_recall(self, machine_id: str) -> dict[str, Any]:
        m = self._machine(machine_id)
        c = self.s.open_case(machine_id)
        out = self.a._step_recall({"id": (c or {}).get("id", "-"), "machine": machine_id}, {})
        out["machine_notes"] = [x["note"] for x in self.s.memory(f"machine:{machine_id}")]
        out["owner_preferences"] = [x["note"] for x in self.s.memory(f"owner:{m['factory']}")]
        out["summary"] = (f"{out['earlier_cases']} earlier cases, {out['repairs']} repairs, "
                          f"{len(out['machine_notes'])} notes, {len(out['owner_preferences'])} owner preferences")  # fmt: skip
        return out

    def t_find_mechanics(self, skill: str, area: str | None, need_by: str) -> dict[str, Any]:
        try:
            until = datetime.fromisoformat(need_by).date()
        except ValueError as e:
            raise ToolInputError("need_by must be YYYY-MM-DD") from e
        today = self.now().date()
        until = max(until, today)
        prof = self._profile(self.e.factory or "")
        fxy = prof.get("xy_km")
        booked: dict[str, set[str]] = {}
        for r in self.s.db.execute(
            "SELECT payload FROM actions WHERE kind='book_job' AND status IN ('done','queued')"
        ):
            p = json.loads(r["payload"])
            booked.setdefault(p.get("technician", ""), set()).add(p.get("day", ""))
        declined = set()
        for c in self.s.cases(open_only=True):
            declined |= set(c["context"].get("declined", []))
        people = self.a.t.technicians.people
        out = []
        for p in people:
            exact = skill in p["skills"]
            if not exact and "inspection" not in p["skills"]:
                continue
            days = []
            d = today
            while d <= until and len(days) < 7:
                if WD[d.weekday()] in p["works"] and d.isoformat() not in booked.get(p["id"], set()):
                    days.append(d.isoformat())
                d += timedelta(days=1)
            reps = self.s.repairs(technician=p["id"])
            km = round(math.dist(fxy, p["xy_km"]), 1) if fxy and p.get("xy_km") else p.get("km")
            out.append({"id": p["id"], "name": p["name"], "role": p.get("role"), "area": p.get("area"),
                        "skill_match": "exact" if exact else "inspection only",
                        "km": km, "free_days": days, "rating": p.get("rating"),
                        "past_here": {"jobs": len(reps), "fixed": sum(r["outcome"] == "fixed" for r in reps),
                                      "failed": sum(r["outcome"] == "failed" for r in reps)},
                        "declined_open_case": p["id"] in declined})  # fmt: skip
        out.sort(key=lambda x: (not x["free_days"], x["declined_open_case"], x["skill_match"] != "exact",
                                -(x["past_here"]["fixed"] - 2 * x["past_here"]["failed"]),
                                (area or "").lower() != (x["area"] or "").lower(), x["km"] or 99))  # fmt: skip
        return {"candidates": out[:5], "source": self.a.t.technicians.data["_source"],
                "summary": "; ".join(f"{x['id']} {x['km']} km, {len(x['free_days'])} free days" for x in out[:5])
                or "no technician with that skill"}  # fmt: skip

    def _windows(
        self, factory: str, day: datetime
    ) -> tuple[list[tuple[datetime, datetime]], list[tuple[datetime, datetime]]]:
        """The day's shift windows and power-cut windows (a window ending before it starts runs past midnight)."""
        prof = self._profile(factory)

        def mk(a: str, b: str) -> tuple[datetime, datetime]:
            start = day.replace(hour=_hm(a)[0], minute=_hm(a)[1])
            end = day.replace(hour=_hm(b)[0], minute=_hm(b)[1])
            return start, end + (timedelta(days=1) if b <= a else timedelta())

        shifts = [mk(a, b) for a, b in (prof.get("shift_hours") or DEFAULT_SHIFT)]
        cuts = [mk(a, b) for a, b in prof.get("power_cuts", [])]
        return shifts, cuts

    @staticmethod
    def _overlap(a: tuple[datetime, datetime], b: tuple[datetime, datetime]) -> float:
        return max(0.0, (min(a[1], b[1]) - max(a[0], b[0])).total_seconds() / 3600)

    def _running_hours(self, factory: str, start: datetime, end: datetime) -> float:
        """Hours the line would run between start and end (shifts, minus power cuts and off-days)."""
        prof = self._profile(factory)
        h, d = 0.0, start.replace(hour=0, minute=0, second=0, microsecond=0)
        while d < end:
            if DAYS[d.weekday()] not in prof.get("off_days", []):
                shifts, cuts = self._windows(factory, d)
                for s in shifts:
                    h += self._overlap(s, (start, end)) - sum(
                        self._overlap(c, (max(s[0], start), min(s[1], end))) for c in cuts
                    )
            d += timedelta(days=1)
        return max(0.0, h)

    def t_propose_slots(self, machine_id: str, duration_h: float, prefer_day: str | None) -> dict[str, Any]:
        m = self._machine(machine_id)
        if not 0 < duration_h <= 12:
            raise ToolInputError("duration_h must be between 0 and 12 hours")
        f, prof, now = m["factory"], self._profile(m["factory"]), self.now()
        per_h = cost_model.load()["output_per_hour_inr"].get(
            f, min(cost_model.load()["output_per_hour_inr"].values())
        )
        orders = [o for o in load_orders() if o["factory"] == f and machine_id in o.get("machines", [])]
        start0 = (now + timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
        slots = []
        for k in range(7 * 24):
            s = start0 + timedelta(hours=k)
            if not 7 <= s.hour <= 19:
                continue
            e = s + timedelta(hours=duration_h)
            day = s.replace(hour=0, minute=0, second=0, microsecond=0)
            shifts, cuts = self._windows(f, day)
            off = DAYS[s.weekday()] in prof.get("off_days", [])
            lost_h = 0.0 if off else self._running_hours(f, s, e)
            cut_h = sum(self._overlap(c, (s, e)) for c in cuts)
            in_shift = sum(self._overlap(x, (s, e)) for x in shifts)
            fit = ("off day" if off else "inside the shift" if in_shift >= duration_h - 1e-6
                   else "outside shift hours" if in_shift == 0 else "crosses the shift edge")  # fmt: skip
            clashes = []
            for o in orders:
                due = order_due(o, now)
                if s < due and self._running_hours(f, now, due) - lost_h < o["hours_needed"]:
                    clashes.append(
                        {"order": o["id"], "item": f"{o['qty']} {o['unit']} {o['item']}", "due": ist(due)}
                    )
            lost = round(lost_h * per_h)
            slots.append({"start": ist(s), "start_iso": s.isoformat(timespec="minutes"), "day": s.date().isoformat(),
                          "hours": duration_h, "production_lost_inr": lost, "production_lost": inr(lost),
                          "power_cut_overlap_h": round(cut_h, 2), "shift_fit": fit, "order_clashes": clashes})  # fmt: skip
        want = (prefer_day or "")[:3].lower()
        slots.sort(key=lambda x: (len(x["order_clashes"]), bool(want) and DAYS[datetime.fromisoformat(x["start_iso"]).weekday()][:3].lower() != want,
                                  x["production_lost_inr"], x["start_iso"]))  # fmt: skip
        clashing = [x for x in slots if x["order_clashes"]]
        best, seen = [], set()
        for x in slots:  # at most two per day, so the owner sees a real choice of days
            if sum(y["day"] == x["day"] for y in best) < 2 and x["start_iso"] not in seen:
                best.append(x)
                seen.add(x["start_iso"])
            if len(best) == 5:
                break
        return {"slots": best, "windows_checked": len(slots), "windows_with_order_clash": len(clashing),
                "clash_example": clashing[0] if clashing else None, "basis": "production lost = line running hours in the window x output per hour "
                "(data/sim/costs.json, assumption); shifts from the factory profile (default 09:00-18:00, "
                "assumption); orders are DEMO data (data/demo/orders.json)",
                "summary": "; ".join(f"{x['start']}: {x['production_lost']} lost, {len(x['order_clashes'])} clashes"
                                     for x in best[:3])}  # fmt: skip

    def t_check_parts(self, fault: str, machine_type: str) -> dict[str, Any]:
        word = OWNER_WORD.get(fault, fault)
        main = self.a.t.parts.stock(word, machine_type)
        alts = [{k: v for k, v in r.items() if k != "price_inr"} | {"source": "MOCK dealer stock (demo)"}
                for r in _load(PARTS).get("alternatives", []) if r["fault"] == word and r["type"] == machine_type]  # fmt: skip
        rows = main + alts
        in_stock = [r for r in rows if r["in_stock"]]
        return {"fault": fault, "machine_type": machine_type, "dealers": rows,
                "equivalents": sorted({r["part"] for r in alts}), "any_in_stock": bool(in_stock),
                "note": "MOCK stock; prices are not quoted here (see compare_options)",
                "summary": (f"{len(in_stock)} of {len(rows)} dealers have it now" if rows
                            else "no part listed for this fault and machine type")}  # fmt: skip

    def _p_threshold(self, machine: str, days: int) -> tuple[float | None, str]:
        tt = (self.e.health_of(machine) or {}).get("time_to_threshold") or {}
        st = tt.get("status")
        if st == "at_threshold":
            return 1.0, "already at the maintenance threshold"
        if st == "estimate" and tt.get("low") is not None and tt.get("high") is not None:
            lo, hi = float(tt["low"]), float(tt["high"])
            p = 1.0 if days >= hi else 0.0 if days <= lo else (days - lo) / max(hi - lo, 1e-9)
            return round(p, 2), f"time to threshold {lo:g}-{hi:g} days (edge/rul range, read as uniform)"
        return None, f"time to threshold: {st or 'not enough data'}"

    def t_compare_options(self, case_id: str, options: list[dict[str, Any]]) -> dict[str, Any]:
        c = self._case(case_id)
        m = self.s.machine(c["machine"])
        faults = [c["context"]["fault"]] if c["context"].get("fault") else self.a._likely_faults(m)
        fix = cost_range(faults[0], m["hp"]) if faults and faults[0] in _faults() else None
        insp = inspection_cost()
        lo, hi = (
            insp["low_inr"] + (fix or {}).get("low_inr", 0),
            insp["high_inr"] + (fix or {}).get("high_inr", 0),
        )
        c_all = cost_model.load()
        fac = (
            m["factory"]
            if m["factory"] in c_all["output_per_hour_inr"]
            else min(c_all["output_per_hour_inr"], key=lambda k: c_all["output_per_hour_inr"][k])
        )
        svc = float(self.a.t.service_hours.get(m["mtype"], 2.0))
        down = cost_model.breakdown(fac, m["mtype"], OWNER_WORD.get(faults[0]) if faults else None, svc)
        brk = down["breakdown_cost_inr"] + (fix or {}).get("high_inr", 0)
        rows = []
        for o in options[:4]:
            d = int(o["wait_days"])
            p, why = (0.0, "fix now") if d <= 0 else self._p_threshold(c["machine"], d)
            exp = None if p is None else round(lo + p * brk)
            rows.append({"label": o["label"], "wait_days": d, "fix_inr": [lo, hi], "fix": f"{inr(lo)}-{inr(hi)}",
                         "p_threshold_within_wait": p, "p_basis": why, "breakdown_if_it_fails_inr": brk,
                         "breakdown_if_it_fails": inr(brk), "expected_inr": exp,
                         "expected": inr(exp) if exp is not None else "not enough data"})  # fmt: skip
        return {"case_id": case_id, "fault_basis": faults[0] if faults else None, "options": rows,
                "basis": "fix = inspection visit + typical part and labour (faults.yaml, cited); breakdown = "
                "unplanned downtime, idle wages and same-day part markup (data/sim/costs.json, ASSUMPTIONS) + the "
                "repair; the chance uses reaching the maintenance threshold as a stand-in for a breakdown",
                "summary": "; ".join(f"{r['label']}: {r['expected']}" for r in rows)}  # fmt: skip

    def t_triage(self, factory: str | None) -> dict[str, Any]:
        f = factory or self.e.factory
        if self.e.factory and f != self.e.factory:
            raise ToolInputError("triage is limited to this event's factory")
        stops = []
        for c in self.s.cases(open_only=True):
            m = self.s.machine(c["machine"])
            if (f and m["factory"] != f) or RANK.get(c["urgency"], 0) < RANK["inspect"]:
                continue
            risk = max((c["context"].get("history") or [{}])[-1].get("risk") or [0])
            xy = self._profile(m["factory"]).get("xy_km") or [0, 0]
            stops.append({"id": c["id"], "xy_km": xy, "service_h": float(self.a.t.service_hours.get(m["mtype"], 2.0)),
                          "risk": float(risk), "value": RANK[c["urgency"]] * 10 + float(risk),
                          "urgency": c["urgency"], "machine": c["machine"]})  # fmt: skip
        if not stops:
            return {"order": [], "summary": "no open case needs a visit"}
        rc = self.e.route_cfg
        plan = route.plan(
            stops, rc.get("start_xy", [0, 0]), float(rc.get("hours", 7)), float(rc.get("kmph", 22))
        )
        by = {s["id"]: s for s in stops}
        prio = sorted(stops, key=lambda s: -s["value"])
        return {"priority": [{"case": s["id"], "machine": s["machine"], "urgency": s["urgency"]} for s in prio],
                "order": [{"case": i, "machine": by[i]["machine"], "urgency": by[i]["urgency"]} for i in plan["order"]],
                "skipped": plan["skipped"], "hours": plan["hours"], "km": plan["km"],
                "basis": "value = urgency rank x 10 + recent risk; one mechanic's day (cluster.json technician)",
                "summary": f"visit {', '.join(plan['order']) or 'none'}; later: {', '.join(plan['skipped']) or 'none'}"}  # fmt: skip

    # ------------------------------------------------------------------ write tools
    def t_remember(self, scope: str, note: str) -> dict[str, Any]:
        kind, _, key = scope.partition(":")
        if kind == "machine":
            self._machine(key)
        elif kind == "owner":
            if self.e.factory and key != self.e.factory:
                raise ToolInputError("owner scope must be this factory")
        else:
            raise ToolInputError("scope is 'machine:<id>' or 'owner:<factory>'")
        if not 3 <= len(note) <= 200:
            raise ToolInputError("note must be 3-200 characters")
        nid = self.s.remember(scope, note.strip(), self.e.event_idem, "planner")
        return {"remembered": nid, "scope": scope, "summary": f"noted for {scope}: {note[:60]}"}

    def t_request_measurement(self, machine_id: str, instruction: str, kind: str) -> dict[str, Any]:
        m = self._machine(machine_id)
        c = self.s.open_case(machine_id)
        if c is None:
            raise ToolInputError(f"no open case on {machine_id}")
        lang = self.e.lang if self.e.lang in MEASURE[kind] else "en"
        text = MEASURE[kind][lang].format(m=m["name"]) + (
            f" {instruction.strip()}" if instruction.strip() else ""
        )
        n = len(list(c["context"].get("measurements", []))) + 1
        sent = self._send(c, f"measure:{kind}:{n}", text)
        cur = self.s.case(c["id"])
        meas = [*cur["context"].get("measurements", []),
                {"n": n, "kind": kind, "asked": self.a.clock(), "status": "asked", "text": text}]  # fmt: skip
        self.s.update_case(c["id"], cur["version"], context=cur["context"] | {"measurements": meas})
        return {
            "asked": kind,
            "text": text,
            "send": sent["status"],
            "summary": f"asked for a {kind} ({sent['status']})",
        }

    def t_ask_owner(self, case_id: str, question: str, options: list[dict[str, Any]]) -> dict[str, Any]:
        c = self._case(case_id)
        if not 2 <= len(options) <= 4:
            raise ToolInputError("give 2 to 4 options")
        m = self.s.machine(c["machine"])
        prepared = []
        for i, o in enumerate(options, 1):
            o = dict(o)
            if o["means"] == "approve_booking":
                if not o.get("technician") or not o.get("day"):
                    raise ToolInputError("approve_booking needs technician and day")
                try:
                    self.a.t.technicians.get(o["technician"])
                except StopIteration as e:
                    raise ToolInputError(f"unknown technician {o['technician']}") from e
                if o["day"] < self.now().date().isoformat():
                    raise ToolInputError("the visit day has passed")
                n = len([a for a in self.s.approvals(case_id) if a["kind"] == "booking"])
                aid = f"{case_id}:booking" if n == 0 else f"{case_id}:booking:{n + 1}"
                insp = inspection_cost()
                self.s.request_approval(aid, case_id, "booking",
                                        f"Book {o['technician']} on {o['day']} for {m['name']}: {o['label']}",
                                        (insp["low_inr"], insp["high_inr"]))  # fmt: skip
                o["approval"] = aid
            elif o["means"] == "approve_purchase":
                aid = f"{case_id}:purchase:{_h(o['label'])}"
                self.s.request_approval(aid, case_id, "purchase", f"Buy for {m['name']}: {o['label']}")
                o["approval"] = aid
            elif o["means"] == "stop_noted":
                aid = f"{case_id}:production_stop"
                self.s.request_approval(aid, case_id, "production_stop",
                                        f"{m['name']}: stopping it is the owner's decision. {o['label']}")  # fmt: skip
                o["approval"] = aid
            prepared.append({"n": i, **o})
        qid = f"{case_id}:q:{_h(question, [o['label'] for o in options])}"
        text = question.strip() + "\n" + "\n".join(f"{o['n']} = {o['label']}" for o in prepared)
        new = self.s.ask(qid, case_id, m["factory"], text, prepared)
        self.asked_options += [str(o["n"]) for o in prepared]
        sent = self._send(c, f"ask:{qid}", text) if new else {"status": "already asked"}
        if new:
            booking = [o for o in prepared if o["means"] == "approve_booking"]
            if booking:
                cur = self.s.case(case_id)
                prop = [
                    *cur["context"].get("proposed", []),
                    *(
                        {"technician": o["technician"], "day": o["day"], "approval": o["approval"]}
                        for o in booking
                    ),
                ]
                self.s.update_case(case_id, cur["version"], context=cur["context"] | {"proposed": prop})
        return {"question": qid, "text": text, "send": sent["status"], "options": prepared,
                "summary": f"asked the owner ({len(prepared)} options; {sent['status']}); nothing happens until they reply"}  # fmt: skip

    def t_tell_owner(self, case_id: str, text: str) -> dict[str, Any]:
        c = self._case(case_id)
        if not text.strip():
            raise ToolInputError("empty message")
        sent = self._send(c, f"tell:{_h(text)}", text.strip())
        return {"send": sent["status"], "summary": f"told the owner ({sent['status']})"}

    def t_book_job(self, case_id: str, technician: str, day: str) -> dict[str, Any]:
        c = self._case(case_id)
        prop = [
            p for p in c["context"].get("proposed", []) if p["technician"] == technician and p["day"] == day
        ]
        ok = [p for p in prop if (self.s.approval(p["approval"]) or {}).get("status") == "approved"]
        if not ok and self.a.auto_approve(case_id, "book_job", technician):  # the owner's policy, in code
            ok = [{"technician": technician, "day": day, "approval": "autonomy_policy"}]
        if not ok:
            self.s.log(
                "planner",
                "blocked_without_approval",
                case_id,
                kind="book_job",
                technician=technician,
                day=day,
            )
            raise ApprovalRequired(f"the owner has not approved {technician} on {day} for case {case_id}")
        payload = {"technician": technician, "machine": c["machine"], "day": day,
                   "work": c["context"].get("fault") or "inspection"}  # fmt: skip
        try:
            job = self.a.act(case_id, "book_job", f"{technician}:{day}", payload)
        except Waiting:
            job = {"status": "queued (offline)"}
        except Unavailable as e:
            cur = self.s.case(case_id)
            self.s.update_case(case_id, cur["version"],
                               context=cur["context"] | {"declined": [*cur["context"].get("declined", []), technician]})  # fmt: skip
            return {"status": "declined", "technician": technician, "error": str(e),
                    "summary": f"{technician} declined; choose another technician and ask the owner again"}  # fmt: skip
        cur = self.s.case(case_id)
        self.s.update_case(case_id, cur["version"], context=cur["context"] | {"technician": technician, "visit_day": day,
                                                                             "booked": True})  # fmt: skip
        return {"status": "booked", "job": job, **payload, "summary": f"booked {technician} for {day}"}

    def t_order_part(self, case_id: str, part: str, dealer: str) -> dict[str, Any]:
        self._case(case_id)
        try:
            r = self.a.act(case_id, "order_part", f"{_h(part, dealer)}", {"part": part, "dealer": dealer})
        except Waiting:
            r = {"status": "queued (offline)"}
        return {"status": "ordered", "result": r, "summary": f"ordered {part} from {dealer}"}


def _faults() -> dict[str, Any]:
    from server import diagnosis

    return diagnosis.load()["faults"]
