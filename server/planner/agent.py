"""The planner: on every event, build the picture, let Granite choose tool calls, run them through the gate.

    planner = Planner(store, agent, bus, client=llm.make_client(), factories=..., health_of=...)
    bus.subscribe(planner.handle)          # every event, in order, once
    out = planner.handle(event)            # {"decision": id, "engine": "granite" | "rules", "reply": ...}

One brain: alarms from the brain's tick, owner replies (WhatsApp, the owner screen, the command centre),
voice notes, mechanic replies and the agent's own events (repair done, verify result, network back,
part out of stock, no-show) all arrive here.

What code does before the LLM sees the event (never left to the model):
  * an owner's numbered reply is matched to the open question and applied as a decision on the approval
    it names (by the owner, on their channel); the LLM only learns what was decided;
  * a measurement reply (recording, hand check) is re-scored and the case's confidence updated;
  * preferences the owner states ("Sunday", "budget ₹2,000") are noted by simple rules as well.

The LLM turn (Granite on the local Ollama, through server/planner/llm.py) is bounded: turns, tokens per event, rupees per case.
Every tool call goes through ToolBox.dispatch (schema, factory scope, grounding of outgoing text) and
every action through MaintenanceAgent.act (idempotency key, approval gate when created and when sent,
offline queue, hash-chained audit). The final reply is checked by server/planner/grounding: a number,
₹ amount or part number that no tool returned rejects it, the rules reply is used instead, and the
rejection is logged.

Without a local LLM (Ollama not reachable, DHADKAN_LLM=0, over budget, or after an LLM error) the event goes to the rules:
the owner gets the template reply (owner_agent.run_rules) and a planner-driven case is handed to the
fixed plan (maint/agent), which continues exactly as it always has.

Every turn is a row in table decisions (event, model, reasoning, tool calls and results, reply,
grounding, tokens, ₹ cost, latency), so the trace survives a restart; an event interrupted mid-turn is
handled again and its actions are not repeated (idempotency keys).
"""

import json
import re
import threading
import time
from collections.abc import Callable
from typing import Any

from server import provider
from server.maint.agent import MaintenanceAgent
from server.maint.store import Store
from server.notify import env
from server.notify.service import parse_choice

from . import grounding, llm
from .events import Event, EventBus
from .tools import Env, ToolBox

SYSTEM = (
    "You are Dhadkan's maintenance planner for small Indian factories (flour mills, powerlooms, pump and "
    "machine shops). Each turn you get one event (an alarm, an owner's reply, a mechanic's answer, a repair "
    "result...) with the case, the machine's memory, the owner's preferences, the factory's order book and the "
    "other open cases. Decide what should happen next and do it with the tools; you choose which tools and in "
    "what order, and you may do nothing when nothing is needed.\n"
    "Weigh the real trade-offs: production lost in a visit window, orders due, power cuts, which mechanic is "
    "free and has fixed this machine before, parts stock, the rupee cost of fixing now versus waiting, and "
    "other machines competing for the same mechanic. When the diagnosis is unsure, ask for a measurement "
    "(request_measurement) before sending anyone. Respect what the owner told you before (recall).\n"
    "Hard rules, also enforced by code: you cannot stop, slow or switch a machine; a stop is only ever a "
    "question to the owner (ask_owner with a stop_noted option). Nothing that costs money happens without "
    "the owner's approval: ask with ask_owner (an approve_booking option names the technician and day), and "
    "call book_job only after the event tells you the owner approved that visit. Use only numbers, rupee "
    "amounts, dates and part numbers that a tool returned; never estimate your own. Rupees in Indian format; "
    "times in IST.\n"
    "Messages to the owner (ask_owner, tell_owner, request_measurement) are WhatsApp: short, plain, in the "
    "owner's language and script as given in the event (hi_latn = Hindi in Roman script), like a trusted "
    "mechanic. Do not repeat a question that is already open. Demo and mock data are labelled; do not "
    "present them as real.\n"
    "End the turn with one short paragraph: if the event is a chat message from the owner, that paragraph is "
    "your reply to them, in their language; otherwise it is a note for the factory's log saying what you "
    "decided and why."
)
STEP_OF = {"get_machine_health": "observe", "get_spectrogram": "observe", "diagnose_fault": "diagnose",
           "request_measurement": "diagnose", "recall": "plan", "remember": "plan", "find_mechanics": "plan",
           "propose_slots": "plan", "check_parts": "plan", "compare_options": "plan", "triage": "plan",
           "ask_owner": "ask owner", "tell_owner": "ask owner", "book_job": "act", "order_part": "act",
           "log_warranty_claim": "act"}  # fmt: skip
DAY_WORDS = {"sunday": "Sunday", "ravivar": "Sunday", "रविवार": "Sunday", "રવિવાર": "Sunday", "ஞாயிறு": "Sunday",
             "saturday": "Saturday", "shanivar": "Saturday", "शनिवार": "Saturday", "શનિવાર": "Saturday"}  # fmt: skip
BUDGET = re.compile(
    r"(?:₹|rs\.?|rupees?|रुपये|रु\.?)\s*([\d,]+)|([\d,]+)\s*(?:₹|rs\.?|rupees?|rupaye|रुपये)", re.I
)


def preferences(text: str) -> list[str]:
    """Owner preferences simple rules can read from a reply (the LLM may note more)."""
    t = grounding.norm(text).lower()
    out = [f"prefers {d} visits" for w, d in DAY_WORDS.items() if w in t]
    for m in BUDGET.finditer(t):
        n = (m.group(1) or m.group(2) or "").replace(",", "")
        if n.isdigit() and int(n) >= 100:
            out.append(f"budget limit ₹{int(n):,} for a repair")
    return sorted(set(out))


def first_number(text: str) -> int | None:
    m = re.search(r"\d+", grounding.norm(text))
    return int(m.group(0)) if m else None


def is_answer(text: str, payload: dict[str, Any]) -> bool:
    """Only a reply on a reply channel that is just the number ("2", "२.", "1)") or a short yes / later
    counts as an answer. A chat question ("Loom 1 ki awaaz?") is never read as option 1."""
    if payload.get("channel") == "chat":
        return False
    t = grounding.norm(text).strip()
    if re.fullmatch(r"\d\s*[.)]?", t):
        return True
    return len(t.split()) <= 3 and parse_choice(t) in ("book", "later")


class Planner:
    def __init__(self, store: Store, agent: MaintenanceAgent, bus: EventBus | None = None, *, client: Any = None,
                 factories: dict[str, dict[str, Any]] | None = None,
                 health_of: Callable[[str], dict[str, Any] | None] | None = None, live: Any = None,
                 route_cfg: dict[str, Any] | None = None, lock: Any = None,
                 rules_reply: Callable[[Event], dict[str, Any] | None] | None = None,
                 score_audio: Callable[[str, Any], float | None] | None = None) -> None:  # fmt: skip
        self.s, self.agent, self.bus, self.client = store, agent, bus, client
        self.factories = factories or {}
        self.health_of = health_of or (lambda _m: None)
        self.live, self.route_cfg = live, route_cfg or {}
        self.lock = lock or (bus.lock if bus else threading.RLock())
        self.rules_reply, self.score_audio = rules_reply, score_audio
        for d in self.s.decisions(status="running"):  # a restart in the middle of a turn
            self.s.set_decision(d["id"], status="interrupted", ended=self.s.clock())
        if bus is not None:
            bus.subscribe(self.handle)

    @property
    def llm_on(self) -> bool:
        return self.client is not None

    def lang_of(self, factory: str | None, ev: Event | None = None) -> str:
        if ev and ev.payload.get("lang"):
            return str(ev.payload["lang"])
        if factory:
            return (
                env.get(f"DHADKAN_LANG_{factory}")
                or self.factories.get(factory, {}).get("language")
                or "hi_latn"
            )
        return "hi_latn"

    # ------------------------------------------------------------------ the turn
    def handle(self, ev: Event) -> dict[str, Any]:
        t0 = time.perf_counter()
        with self.lock:
            case = self._case_for(ev)
            effects = self._apply(ev, case)
            if case is None and effects.get("case_id"):  # a reply or a mechanic message that named its case
                case = self.s.case(effects["case_id"])
            case = self._case_for(ev) if case is None else self.s.case(case["id"])
            cid = case["id"] if case else None
            reason = self._no_llm_reason(case)
            tier = llm.tier_for(ev.type, ev.payload | {"unsure": self._unsure(ev, case)})
            model, effort = llm.model_for(tier)
            did = self.s.start_decision(ev.idem, ev.type, cid, ev.machine or (case or {}).get("machine"),
                                        "rules" if reason else provider.NAME, None if reason else model)  # fmt: skip
        out: dict[str, Any] | None = None
        if not reason:
            try:
                out = self._llm(ev, case, effects, did, model, effort, tier)
            except Exception as e:  # offline, rate limit, an SDK error: the rules answer
                reason = f"LLM error: {type(e).__name__}"
                with self.lock:
                    self.s.log(
                        "planner", "llm_error", cid, error=f"{type(e).__name__}: {e}"[:300], decision=did
                    )
        if out is None:
            out = self._rules(ev, case, effects, did, reason or "rules")
        with self.lock:
            self.s.set_decision(
                did, latency_ms=round((time.perf_counter() - t0) * 1000), ended=self.s.clock()
            )
            self.s.log("planner", "decision", cid, decision=did, idem=ev.idem, engine=out["engine"],
                       tools=[c["tool"] for c in out.get("calls", [])], cost_inr=out.get("cost_inr", 0))  # fmt: skip
        return {"decision": did, "case": cid, "effects": effects, **out}

    def _no_llm_reason(self, case: dict[str, Any] | None) -> str | None:
        if not self.llm_on:
            return "no local LLM: Ollama not reachable or DHADKAN_LLM=0 (rules)"
        if case is not None:
            cap = llm.setting("DHADKAN_PLANNER_CASE_CAP_INR", 50)
            spent = self.s.llm_cost_inr(case["id"])
            if spent >= cap:
                return f"case LLM budget used (₹{spent:.2f} of ₹{cap:g})"
            if case["context"].get("driver") == "rules" and case["context"].get("handoff"):
                return f"case handed to the fixed plan ({case['context']['handoff']})"
        return None

    def _unsure(self, ev: Event, case: dict[str, Any] | None) -> bool:
        mid = ev.machine or (case or {}).get("machine")
        h = self.health_of(mid) if mid else None
        return bool(h and h.get("confidence") == "low" and ev.type in ("alarm", "health_change"))

    # ------------------------------------------------------------------ case and code-side effects
    def _case_for(self, ev: Event) -> dict[str, Any] | None:
        if ev.case_id:
            try:
                return self.s.case(ev.case_id)
            except KeyError:
                return None
        if not ev.machine:
            return self._reply_case(ev) if ev.type == "owner_reply" else None
        c = self.s.open_case(ev.machine)
        if c is None and ev.type == "alarm" and self.llm_on:
            try:
                self.s.machine(ev.machine)
            except KeyError:
                return None
            h = ev.payload.get("health") or self.health_of(ev.machine)
            if h and h.get("urgency", "none") != "none":
                prev, self.agent.driver = self.agent.driver, "planner"
                try:
                    c = self.agent.observe(ev.machine, h, reading_ref=ev.idem)
                finally:
                    self.agent.driver = prev
        return c

    def _reply_case(self, ev: Event) -> dict[str, Any] | None:
        """A WhatsApp reply carries no machine: it answers the newest open question or measurement request
        in the owner's factory."""
        if not ev.factory:
            return None
        best: tuple[float, str] | None = None
        for q in self.s.questions(factory=ev.factory, open_only=True):
            if q["case_id"] and (best is None or q["asked"] > best[0]):
                best = (q["asked"], q["case_id"])
        for c in self.s.cases(open_only=True):
            if self.s.machine(c["machine"])["factory"] != ev.factory:
                continue
            for m in c["context"].get("measurements", []):
                if m["status"] == "asked" and (best is None or m["asked"] > best[0]):
                    best = (m["asked"], c["id"])
        return self.s.case(best[1]) if best else None

    def _apply(self, ev: Event, case: dict[str, Any] | None) -> dict[str, Any]:
        p, out = ev.payload, {}
        if ev.type == "owner_reply":
            text = str(p.get("text") or "")
            if p.get("approval_id"):  # decided elsewhere (command centre / owner screen): already applied
                out["decision"] = {"approval": p["approval_id"], "decision": p.get("decision")}
            elif is_answer(text, p):
                meas = self._open_measurement(case, "hand_check")
                q = self._match_question(ev, case)
                n = first_number(text)
                if meas and n in (1, 2, 3) and (not q or meas["asked"] >= q["asked"]):
                    out["measurement"] = self._hand_check(case, meas, n)
                elif q:
                    out["answer"] = self._answer(ev, q, text)
            prefs = preferences(text)
            if prefs and ev.factory:
                for n in prefs:
                    self.s.remember(f"owner:{ev.factory}", n, ev.idem, "rules")
                out["noted"] = prefs
        elif ev.type == "owner_voice_note" and case is not None:
            out["measurement"] = self._audio_reply(case, p)
        elif ev.type in ("mechanic_no_show", "mechanic_reply"):
            out.update(self._mechanic(ev, case))
        elif ev.type == "part_out_of_stock" and case is not None:
            out["part_alternatives"] = self._alternatives(case, p)
        elif ev.type == "verify_result" and p.get("outcome") == "failed":
            out["warranty"] = {"claim_logged": True, "follow_up_case": p.get("follow_up_case"),
                               "excluded_technician": p.get("technician")}  # fmt: skip
        return out

    def _mechanic(self, ev: Event, case: dict[str, Any] | None) -> dict[str, Any]:
        """A no-show, or the mechanic declining: exclude them and re-plan (maint/agent.replan_technician)."""
        p = ev.payload
        tech = p.get("technician")
        if (
            case is None and ev.type == "mechanic_reply" and ev.factory
        ):  # a WhatsApp message from the mechanic
            own = next(
                (t["id"] for t in self.agent.t.technicians.people if t.get("role") == "own_mechanic"), None
            )
            tech = (
                tech or own
            )  # the factory's own mechanic's number (DHADKAN_MECHANIC_<factory>) unless named
            case = next((c for c in self.s.cases(open_only=True) if c["context"].get("technician") == tech
                         and self.s.machine(c["machine"])["factory"] == ev.factory), None)  # fmt: skip
        if case is None or not tech:
            return {"mechanic": "no open visit to re-plan"}
        if (
            ev.type == "mechanic_reply" and ev.source == "agent"
        ):  # a refused booking: book_job already re-planned
            return {"mechanic": "declined", "technician": tech, "replanned_by": "maint/agent book_job"}
        if ev.type == "mechanic_reply" and p.get("answer") not in ("declined",):
            self.s.log("technician", "mechanic_message", case["id"], technician=tech, answer=p.get("answer"))
            return {"mechanic": p.get("answer"), "technician": tech}
        why = "did not come on the booked day" if ev.type == "mechanic_no_show" else "declined the visit"
        self.agent.replan_technician(case["id"], tech, why)
        return {"replanned": {"case": case["id"], "excluded": tech, "why": why}, "case_id": case["id"]}

    def _alternatives(self, case: dict[str, Any], p: dict[str, Any]) -> list[dict[str, Any]]:
        from server.maint.tools import PARTS, _load

        word = {"bearing_wear": "bearing", "imbalance": "imbalance", "belt_slip": "loose_belt",
                "cavitation": "clog"}.get(p.get("fault", ""), p.get("fault"))  # fmt: skip
        alts = [{k: v for k, v in r.items() if k != "price_inr"} | {"source": "MOCK dealer stock (demo)"}
                for r in _load(PARTS).get("alternatives", [])
                if r["fault"] == word and r["type"] == p.get("machine_type") and r["in_stock"]]  # fmt: skip
        cur = self.s.case(case["id"])
        self.s.update_case(case["id"], cur["version"], context=cur["context"] | {"part_alternatives": alts})
        self.s.log("agent", "part_alternatives", case["id"], found=len(alts), fault=p.get("fault"))
        return alts

    def _match_question(self, ev: Event, case: dict[str, Any] | None) -> dict[str, Any] | None:
        qs = self.s.questions(case_id=case["id"], open_only=True) if case else []
        if not qs and ev.factory:
            qs = self.s.questions(factory=ev.factory, open_only=True)
        return qs[-1] if qs else None

    def _answer(self, ev: Event, q: dict[str, Any], text: str) -> dict[str, Any]:
        """The owner's number (or the words for book / later) -> the option -> a decision on its approval."""
        opts = q["options"]
        n = first_number(text)
        pick = next((o for o in opts if o["n"] == n), None)
        if pick is None:
            word = parse_choice(text)
            want = {"book": "approve_booking", "later": "later"}.get(word or "")
            pick = next((o for o in opts if o["means"] == want), None)
        if pick is None:
            return {"question": q["id"], "understood": False, "text": text[:200]}
        channel = str(ev.payload.get("channel") or "whatsapp")
        self.s.answer(q["id"], pick["n"], text[:200])
        decided = []
        if pick["means"] in ("approve_booking", "approve_purchase", "stop_noted"):
            decided.append(
                self.agent.decide(pick["approval"], "approved", by="owner", channel=channel, ref=ev.idem)
            )
        elif pick["means"] == "later":
            for o in opts:
                a = self.s.approval(o.get("approval") or "")
                if a and a["status"] == "requested" and a["kind"] in ("booking", "purchase"):
                    decided.append(
                        self.agent.decide(a["id"], "rejected", by="owner", channel=channel, ref=ev.idem)
                    )
        return {"question": q["id"], "chose": pick["n"], "means": pick["means"], "label": pick["label"],
                "technician": pick.get("technician"), "day": pick.get("day"), "approval": pick.get("approval"),
                "applied": decided}  # fmt: skip

    def _open_measurement(
        self, case: dict[str, Any] | None, kind: str | None = None
    ) -> dict[str, Any] | None:
        if not case:
            return None
        ms = [m for m in case["context"].get("measurements", []) if m["status"] == "asked"]
        ms = [m for m in ms if kind is None or m["kind"] == kind]
        return ms[-1] if ms else None

    def _set_measurement(self, case: dict[str, Any], n: int, **fields: Any) -> dict[str, Any]:
        cur = self.s.case(case["id"])
        ms = [m | fields if m["n"] == n else m for m in cur["context"].get("measurements", [])]
        self.s.update_case(case["id"], cur["version"], context=cur["context"] | {"measurements": ms})
        return next(m for m in ms if m["n"] == n)

    def _hand_check(self, case: dict[str, Any], meas: dict[str, Any], n: int) -> dict[str, Any]:
        word = {1: "hot", 2: "loose or shaking", 3: "fine"}[n]
        conf = "higher" if n in (1, 2) else "lower"
        m = self._set_measurement(case, meas["n"], status="received", result=word, confidence_change=conf)
        self.s.log("owner", "measurement", case["id"], kind="hand_check", result=word)
        return {"kind": "hand_check", "result": word, "fault_confidence": conf, "n": m["n"]}

    def _audio_reply(self, case: dict[str, Any], p: dict[str, Any]) -> dict[str, Any]:
        meas = self._open_measurement(case, "audio")
        risk = p.get("risk")
        if risk is None and self.score_audio and p.get("audio_path"):
            try:
                risk = self.score_audio(case["machine"], p["audio_path"])
            except Exception as e:
                self.s.log("system", "rescore_failed", case["id"], error=str(e)[:200])
        res: dict[str, Any] = {"kind": "audio", "transcript": p.get("text"), "risk_vs_alarm": risk}
        if risk is not None:
            res["fault_confidence"] = "higher" if risk >= 1.0 else "lower"
            res["basis"] = (
                "the new recording scored against this machine's own learned normal (1.0 = its alarm level)"
            )
        if meas:
            self._set_measurement(case, meas["n"], status="received", risk=risk,
                                  confidence_change=res.get("fault_confidence"))  # fmt: skip
        self.s.log("owner", "measurement", case["id"], kind="audio", risk=risk)
        return res

    # ------------------------------------------------------------------ rules
    def _rules(self, ev: Event, case: dict[str, Any] | None, effects: dict[str, Any], did: int,
               reason: str) -> dict[str, Any]:  # fmt: skip
        legacy = None
        with self.lock:
            if case is not None and self.agent._planner_driven(case):
                self.agent.handoff(case["id"], reason)
                self.agent.run_case(case["id"])
        if self.rules_reply and (ev.payload.get("channel") == "chat" or ev.payload.get("say_to_owner")):
            legacy = self.rules_reply(ev)
        reply = (legacy or {}).get("reply", "")
        with self.lock:
            self.s.set_decision(did, status="done", engine="rules", reply=reply,
                                reasoning=f"{reason}; the deterministic plan (server/maint/agent) handles this event",
                                outcome="rules reply" if legacy else "deterministic plan continues")  # fmt: skip
        return {"engine": "rules", "reply": reply, "legacy": legacy, "reason": reason, "calls": []}

    # ------------------------------------------------------------------ the LLM
    def context(self, ev: Event, case: dict[str, Any] | None, effects: dict[str, Any]) -> dict[str, Any]:
        from .tools import load_orders, order_due

        f = ev.factory or ((self.s.machine(case["machine"])["factory"]) if case else None)
        prof = self.factories.get(f or "", {})
        now = self.agent_now()
        from server.owner_agent import ist

        ctx: dict[str, Any] = {
            "event": {"type": ev.type, "source": ev.source, "machine": ev.machine, "payload": ev.payload},
            "now": ist(now), "today": now.date().isoformat(), "owner_language": self.lang_of(f, ev),
            "factory": {"id": f, **{k: prof.get(k) for k in ("name", "area", "language", "shift_hours",
                                                              "off_days", "power_cuts", "peak_months")}},
            "owner_preferences": [m["note"] for m in self.s.memory(f"owner:{f}")] if f else [],
            "order_book": [o | {"due": ist(order_due(o, now))} for o in load_orders() if o["factory"] == f],
            "applied_by_code": effects,
        }  # fmt: skip
        if case:
            c = case
            keep = ("fault", "technician", "visit_day", "booked", "declined", "proposed", "measurements",
                    "repair", "warranty_claim", "plan")  # fmt: skip
            ctx["case"] = {"id": c["id"], "machine": c["machine"], "name": self.s.machine(c["machine"])["name"],
                           "urgency": c["urgency"], "severity": c["severity"], "state": c["state"],
                           "context": {k: c["context"][k] for k in keep if k in c["context"]},
                           "steps": [{"name": s["name"], "status": s["status"]} for s in self.s.steps(c["id"])][-8:],
                           "approvals": [{k: a[k] for k in ("id", "kind", "status", "summary")} for a in self.s.approvals(c["id"])],
                           "open_questions": [{"id": q["id"], "text": q["question"]} for q in self.s.questions(case_id=c["id"], open_only=True)],
                           "recent_actions": [dict(r) for r in self.s.db.execute(
                               "SELECT kind, status, error FROM actions WHERE case_id=? ORDER BY created DESC LIMIT 6", (c["id"],))],
                           "earlier_decisions": [d["outcome"] for d in self.s.decisions(case_id=c["id"], limit=4) if d["outcome"]],
                           "machine_memory": [m["note"] for m in self.s.memory(f"machine:{c['machine']}")]}  # fmt: skip
        ctx["open_cases_in_factory"] = [
            {"id": x["id"], "machine": x["machine"], "urgency": x["urgency"],
             "technician": x["context"].get("technician"), "visit_day": x["context"].get("visit_day")}
            for x in self.s.cases(open_only=True) if not f or self.s.machine(x["machine"])["factory"] == f
        ]  # fmt: skip
        return ctx

    def agent_now(self) -> Any:
        from datetime import datetime

        return datetime.fromtimestamp(self.agent.clock())

    def _llm(self, ev: Event, case: dict[str, Any] | None, effects: dict[str, Any], did: int, model: str,
             effort: str, tier: str) -> dict[str, Any] | None:  # fmt: skip
        with self.lock:
            ctx = self.context(ev, case, effects)
            f = ctx["factory"]["id"]
            spent = self.s.llm_cost_inr(case["id"]) if case else 0.0

        def reject(tool: str, rec: dict[str, Any]) -> None:
            self.s.log("planner", "grounding_rejected", case and case["id"], tool=tool, decision=did,
                       unknown=rec["unknown_numbers"] + rec["unknown_parts"])  # fmt: skip

        box = ToolBox(Env(store=self.s, agent=self.agent, factories=self.factories, health_of=self.health_of,
                          live=self.live, route_cfg=self.route_cfg, factory=f, lang=ctx["owner_language"],
                          event_idem=ev.idem, owner_text=str(ev.payload.get("text") or ""),
                          on_grounding_reject=reject))  # fmt: skip

        def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
            with self.lock:
                return box.dispatch(name, args)

        messages = [{"role": "user", "content": json.dumps(ctx, ensure_ascii=False, default=str)}]
        res = llm.run_loop(self.client, model, effort, SYSTEM, box.specs, messages, dispatch,
                           max_turns=int(llm.setting("DHADKAN_PLANNER_MAX_TURNS", 6)),
                           max_tokens=int(llm.setting("DHADKAN_PLANNER_MAX_TOKENS", 40000)),
                           cost_cap_inr=llm.setting("DHADKAN_PLANNER_CASE_CAP_INR", 50), spent_inr=spent)  # fmt: skip
        sources = [r["result"] for r in box.results] + [ctx]
        g = grounding.check(res.reply, sources, box.extra_grounding()) if res.reply else {"ok": True}
        status = {"end": "done", "refusal": "refusal"}.get(res.stop, "budget")
        reply, engine = res.reply, provider.NAME
        if not g["ok"]:
            reject("reply", {"unknown_numbers": g["unknown_numbers"], "unknown_parts": g["unknown_parts"]})
            status = "grounding_rejected"
            legacy = (
                self.rules_reply(ev) if self.rules_reply and ev.payload.get("channel") == "chat" else None
            )
            reply = (legacy or {}).get("reply", "")
            engine = provider.NAME + "+rules"
        changed = [c["tool"] for c in res.calls if (c["ok"] and STEP_OF.get(c["tool"]) in ("ask owner", "act"))
                   or c["tool"] in ("remember", "request_measurement")]  # fmt: skip
        outcome = (res.reply.split("\n")[0][:240] if res.reply and g["ok"] else
                   f"{', '.join(changed) or 'no action'} ({res.stop})")  # fmt: skip
        with self.lock:
            self.s.set_decision(did, status=status, engine=engine, model=model, reply=reply,
                                reasoning="\n".join(res.reasoning)[:6000],
                                tool_calls=[{**c, "result": _clip(c["result"])} for c in res.calls],
                                outcome=outcome, input_tokens=res.input_tokens, output_tokens=res.output_tokens,
                                cost_inr=res.cost_inr, grounding=g | {"tool_rejections": box.rejections})  # fmt: skip
            if res.stop == "cost" and case is not None:
                self.agent.handoff(case["id"], "case LLM budget used")
            elif res.stop == "refusal" and case is not None:
                self.agent.handoff(case["id"], "the model declined this case")
        return {"engine": engine, "model": model, "tier": tier, "reply": reply, "calls": res.calls,
                "stop": res.stop, "grounding": g, "input_tokens": res.input_tokens,
                "output_tokens": res.output_tokens, "cost_inr": res.cost_inr, "turns": res.turns,
                "steps": [{"step": STEP_OF.get(c["tool"], "plan"), "tool": c["tool"],
                           "machine": c["input"].get("machine_id") or (case or {}).get("machine"),
                           "summary": (c["result"].get("summary") or c["result"].get("error") or "")[:200]}
                          for c in res.calls]}  # fmt: skip


def _clip(v: Any, n: int = 1500) -> Any:
    s = json.dumps(v, ensure_ascii=False, default=str)
    return v if len(s) <= n else {"clipped": s[:n]}
