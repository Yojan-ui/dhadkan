"""The maintenance agent: observe -> remember -> reason -> plan -> act -> recover, with the owner's
approval before anything that costs money or touches production.

    agent = MaintenanceAgent(store, baselines, tools)
    agent.observe("chakki-1", health)      # every assessed reading (server/health.py)
    agent.tick()                            # advance every open case; resend queued actions when online
    agent.decide(approval_id, "approved", by="owner", channel="whatsapp", ref="wamid...")
    agent.repair_done(case_id, technician="T1", work="replaced bearing 6205")

One CASE per anomaly episode, stored with its plan (a list of named steps). Every step is persisted
before and after it runs, so a restart continues where it stopped. Every outward action has a
deterministic idempotency key (case:kind:purpose), so a retry or a restart never sends it twice.

Reasoning is rules (server/health.py urgency + the machine's memory). No LLM is needed for any step;
the agent works offline and queues outward actions until the network is back.

Guardrails, enforced here and tested:
  * There is no tool that stops, slows or switches a machine. "Consider stopping" is a question to the
    owner; their answer is logged and nothing else happens.
  * book_job (a paid visit) and order_part (a purchase) refuse to run without an APPROVED approval
    of the right kind for the same case (ApprovalRequired).
  * The owner's autonomy policy (factory profile, off by default) is read here, in the gate, never from a
    prompt: {"autonomy": {"auto_approve_under_inr": 1500, "mechanic": "T1"}} lets a booking of THAT
    mechanic whose visit costs at most that much be approved without asking; the approval is logged as
    decided by "autonomy_policy". Purchases and production stops are never auto-approved.

Re-planning (events from server/planner): a technician who declines or does not come is excluded and
another is found; a repair that verifies as failed logs a warranty claim against the signed repair
record and opens a follow-up case for a second opinion (the failed technician excluded).
"""

import time
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from edge.baseline import BaselineStore
from server import prove

from .store import Store
from .tools import (
    OWNER_WORD,
    Network,
    Parts,
    Technicians,
    ToolError,
    Transport,
    Unavailable,
    cost_range,
    inspection_cost,
)

ASSUMPTIONS = {
    "resolve_after": 3,  # consecutive "none" assessments that close a watch/measure case
    "retry_base_s": 60.0,
    "retry_max_s": 3600.0,
    "escalate_after_s": 2 * 3600.0,  # urgent approval unanswered -> supervisor
    "search_days": 3,  # look for a technician this many days ahead
    "verify_readings": 6,  # readings after the repair before verifying
    "recurrence_days": 60,  # an anomaly within this many days of a repair counts as a recurrence
}
RANK = {
    "none": 0,
    "monitor": 1,
    "measure_again": 1,
    "inspect": 2,
    "maintenance": 3,
    "urgent_owner_attention": 4,
}
PLAN_MEASURE = ["recall", "request_measurement", "await_change"]
PLAN_MONITOR = ["recall", "notify_watch", "await_change"]
PLAN_FIX = ["recall", "plan_inspection", "find_technician", "check_parts", "estimate_cost", "request_approval", "await_approval",
            "book_job", "await_repair", "request_post_repair_readings", "verify_repair"]  # fmt: skip
PLAN_PLANNER = ["recall", "planner"]  # the LLM planner chooses the rest, through act() and its gate
GATED = {"book_job": "booking", "order_part": "purchase"}
PRIORITY = {"notify_owner_urgent"}


def summarise(step: str, r: Any) -> str:
    """One line of reasoning for the agent board, from the step's own result (no LLM)."""
    if not isinstance(r, dict):
        return step.replace("_", " ")
    if step == "recall":
        return r.get("note") or (f"{r.get('earlier_cases', 0)} earlier cases, {r.get('repairs', 0)} repairs; "
                                 f"baseline from {r.get('baseline_v1_learned_from')} readings")  # fmt: skip
    if step == "plan_inspection":
        return (f"priority {r['priority']} of {r['of']}; from {r['earliest_day']}; window {r['window']}; "
                f"about {r['expected_downtime_h']} h down")  # fmt: skip
    if step == "find_technician":
        return f"{r.get('name')} on {r.get('day')}: {r.get('why')}"
    if step == "check_parts":
        return "; ".join(f"{x['fault'].replace('_', ' ')}: {'order needed' if x['order_needed'] else 'listed'}"
                         for x in r.get("candidates", []))  # fmt: skip
    if step == "estimate_cost":
        i = r.get("inspection", {})
        return f"inspection Rs {i.get('low_inr')}-{i.get('high_inr')}; {len(r.get('if_found', []))} cited part ranges"
    if step == "await_approval":
        return f"approved by {r.get('approved_by')} via {r.get('channel')}"
    if step == "book_job":
        return f"booked {r.get('technician')} for {r.get('day')}"
    if step == "verify_repair":
        return f"{r.get('outcome')}: median risk {r.get('before_median_risk')} -> {r.get('after_median_risk')} vs v1"
    return str(r.get("summary") or r.get("asked") or r.get("told") or step.replace("_", " "))[:200]


class ApprovalRequired(PermissionError):
    pass


class Waiting(Exception):
    """The step cannot finish yet (waiting for a person, a reading, or the network)."""


def plan_for(urgency: str) -> list[str]:
    if urgency == "measure_again":
        return PLAN_MEASURE
    if urgency == "monitor":
        return PLAN_MONITOR
    if urgency == "urgent_owner_attention":
        return ["recall", "notify_owner_urgent", *PLAN_FIX[1:]]
    return PLAN_FIX


class Tools:
    def __init__(self, transport: Transport | None = None, technicians: Technicians | None = None,
                 parts: Parts | None = None, contacts: dict[str, str] | None = None,
                 factories: dict[str, dict[str, Any]] | None = None,
                 service_hours: dict[str, float] | None = None) -> None:  # fmt: skip
        self.transport = transport or Transport()
        self.technicians = technicians or Technicians()
        self.parts = parts or Parts()
        self.contacts = contacts or {"owner": "owner", "supervisor": "supervisor"}
        self.factories = factories or {}  # id -> profile (power cuts, shifts) for the planning agent
        self.service_hours = service_hours or {}  # machine type -> planned service hours (assumption)

    @property
    def network(self) -> Network:
        return self.transport.network


class MaintenanceAgent:
    def __init__(self, store: Store, baselines: BaselineStore, tools: Tools | None = None,
                 clock: Any = time.time, a: dict[str, Any] | None = None, activity: Any = None,
                 emit: Any = None) -> None:  # fmt: skip
        self.s, self.b, self.t = store, baselines, tools or Tools()
        self.clock = clock
        self.s.clock = clock
        self.a = {**ASSUMPTIONS, **(a or {})}
        self.activity = activity  # server/agents.Activity: which named agent ran which step (optional)
        self.emit_hook = emit  # server/planner EventBus.emit (optional): what happened, for the planner
        self.driver = "rules"  # "planner": new cases get PLAN_PLANNER and the LLM planner decides
        # called after a repair is verified: (case, result, repair_id, after_risk) -> the signed record
        self.on_verified: Any = None

    def emit(self, type_: str, **kw: Any) -> None:
        """Tell the planner what happened. Never fails the step that emits."""
        if self.emit_hook is None:
            return
        try:
            self.emit_hook(type_, source="agent", **kw)
        except Exception as e:
            self.s.log("system", "emit_failed", kw.get("case_id"), type=type_, error=str(e)[:200])

    # ---------------------------------------------------------------- observe / reason
    def observe(self, machine: str, health: dict[str, Any], reading_ref: Any = None) -> dict[str, Any] | None:
        urgency = health["urgency"]
        case = self.s.open_case(machine)
        if case is None:
            if urgency == "none":
                return None
            n = len(self.s.cases(machine)) + 1
            cid = f"{machine}#{n}"
            ctx = {"history": [self._snap(health, reading_ref)], "opened_risk": health.get("recent_risk", []),
                   "declined": [], "none_streak": 0, "driver": self.driver}  # fmt: skip
            plan = PLAN_PLANNER if self.driver == "planner" else plan_for(urgency)
            case = self.s.create_case(cid, machine, urgency, health.get("severity", "none"), plan, ctx)
            self.b.set_hold(machine, True)  # nothing recorded during a case is learned as normal
            self.s.log("agent", "case_opened", cid, urgency=urgency, severity=health.get("severity"),
                       evidence=health.get("evidence"), confidence=health.get("confidence"))  # fmt: skip
            if self.activity:
                self.activity.record("orchestrator", "open a case and assign the plan", "completed", machine=machine,
                                     case_id=cid, observation="; ".join(health.get("evidence") or [])[:300],
                                     tools=["cases.create", "baseline.set_hold"],
                                     reasoning=f"urgency {urgency} ({health.get('confidence')} confidence): plan of "
                                               f"{len(plan_for(urgency))} steps", running_status="investigating")  # fmt: skip
            return self.s.case(cid)
        ctx = case["context"]
        ctx["history"] = [*ctx["history"], self._snap(health, reading_ref)][-20:]
        ctx["none_streak"] = ctx.get("none_streak", 0) + 1 if urgency == "none" else 0
        fields: dict[str, Any] = {"context": ctx}
        if health.get("severity") and health["severity"] != "none":
            fields["severity"] = health["severity"]
        in_fix = "book_job" in case["plan"] or bool(ctx.get("booked"))
        if self._planner_driven(case):  # the planner re-plans on the health_change event; only record it
            if RANK[urgency] > RANK[case["urgency"]]:
                fields["urgency"] = urgency
                self.s.log("agent", "escalated", case["id"], frm=case["urgency"], to=urgency, by="planner")
            if not in_fix and ctx["none_streak"] >= self.a["resolve_after"]:
                self.s.update_case(case["id"], case["version"], **fields)
                return self._close(case["id"], "resolved_without_action",
                                   why=f"back to normal for {ctx['none_streak']} readings")  # fmt: skip
            return self.s.update_case(case["id"], case["version"], **fields)
        if RANK[urgency] > RANK[case["urgency"]]:
            fields["urgency"] = urgency
            if RANK[urgency] >= RANK["inspect"] and not in_fix:
                self._skip_open_steps(case["id"])
                extra = ["notify_owner_urgent"] if urgency == "urgent_owner_attention" else []
                self.s.update_case(case["id"], case["version"], **fields)
                self.s.extend_plan(case["id"], extra + PLAN_FIX[1:])
                self.s.log("agent", "escalated", case["id"], frm=case["urgency"], to=urgency)
                return self.s.case(case["id"])
            if urgency == "urgent_owner_attention" and "notify_owner_urgent" not in case["plan"]:
                self.s.update_case(case["id"], case["version"], **fields)
                self.s.extend_plan(case["id"], ["notify_owner_urgent"])
                self.s.log("agent", "escalated", case["id"], frm=case["urgency"], to=urgency)
                return self.s.case(case["id"])
        if not in_fix and ctx["none_streak"] >= self.a["resolve_after"]:
            self.s.update_case(case["id"], case["version"], **fields)
            return self._close(case["id"], "resolved_without_action",
                               why=f"back to normal for {ctx['none_streak']} readings")  # fmt: skip
        return self.s.update_case(case["id"], case["version"], **fields)

    def _planner_driven(self, case: dict[str, Any]) -> bool:
        """True while the LLM planner drives this case (not yet handed back to the fixed plan)."""
        live_steps = [s for s in self.s.steps(case["id"]) if s["status"] not in ("done", "skipped")]
        return any(s["name"] == "planner" for s in live_steps)

    def handoff(self, cid: str, reason: str) -> dict[str, Any]:
        """Give a planner-driven case to the fixed plan (no key, offline, over budget, an LLM error).

        The fixed plan starts after recall; its approvals reuse the same ids (case:booking), so an
        approval the owner already gave still counts, and its actions are idempotent."""
        c = self.s.case(cid)
        if not self._planner_driven(c):
            return c
        for st in self.s.steps(cid):
            if st["name"] == "planner" and st["status"] not in ("done", "skipped"):
                self.s.set_step(cid, st["idx"], status="skipped", error=f"handed to the fixed plan: {reason}")
        if c["context"].get("repair"):
            rest = ["request_post_repair_readings", "verify_repair"]
        elif c["context"].get("booked"):
            rest = ["await_repair", "request_post_repair_readings", "verify_repair"]
        else:
            rest = plan_for(c["urgency"])[1:]
        self.s.extend_plan(cid, rest)
        c = self.s.case(cid)
        self.s.update_case(cid, c["version"], context=c["context"] | {"driver": "rules", "handoff": reason})
        self.s.log("agent", "handoff_to_rules", cid, reason=reason, steps=rest)
        return self.s.case(cid)

    def replan_technician(self, cid: str, technician: str, why: str) -> dict[str, Any]:
        """A technician declined or did not come: exclude them and find another.

        Fixed plan: find_technician and book_job run again (the owner's approval of a visit for this case
        still stands, as when a booking is refused). Planner-driven: the booking is cleared and the planner
        decides (a new technician needs the owner's approval of that visit, or the autonomy policy)."""
        c = self.s.case(cid)
        ctx = c["context"]
        declined = sorted({*ctx.get("declined", []), technician})
        ctx = ctx | {"declined": declined, "booked": False, "visit_day": None, "technician": None}
        self.s.update_case(cid, c["version"], context=ctx)
        if not self._planner_driven(c):
            for st in self.s.steps(cid):
                if st["name"] in ("find_technician", "book_job", "await_repair") and st["status"] in (
                    "done", "waiting", "failed", "retry"):  # fmt: skip
                    self.s.set_step(cid, st["idx"], status="pending", error=f"re-plan: {why}")
        self._owner(c, f"replan:{technician}", f"{technician} cannot do the visit for {self._name(c)} ({why}). "
                                                "Finding another mechanic.")  # fmt: skip
        self.s.log("agent", "replanned_technician", cid, technician=technician, why=why)
        return self.s.case(cid)

    def warranty_and_second_opinion(self, c: dict[str, Any], res: dict[str, Any], rep: dict[str, Any],
                                    repair_id: int) -> str:  # fmt: skip
        """A repair that verified as failed: log a warranty claim citing the repair record, and open a
        follow-up case for a second opinion with the failed technician excluded. Returns the new case id."""
        m = c["machine"]
        claim = {"case": c["id"], "repair_id": repair_id, "technician": rep["technician"], "work": rep["work"],
                 "before_median_risk": res["before_median_risk"], "after_median_risk": res["after_median_risk"],
                 "against_baseline_version": 1, "logged": self.clock()}  # fmt: skip
        self.s.log("agent", "warranty_claim", c["id"], **claim)
        cid = f"{m}#{len(self.s.cases(m)) + 1}"
        ctx = {"history": c["context"].get("history", [])[-5:], "declined": [rep["technician"]], "none_streak": 0,
               "follow_up_of": c["id"], "warranty_claim": claim, "driver": self.driver}  # fmt: skip
        if c["context"].get("fault"):
            ctx["fault"] = c["context"]["fault"]
        plan = PLAN_PLANNER if self.driver == "planner" else ["recall", *PLAN_FIX[1:]]
        self.s.create_case(cid, m, "maintenance", c["severity"], plan, ctx)
        self.b.set_hold(m, True)
        self.s.log("agent", "case_opened", cid, urgency="maintenance", reason="second opinion after a failed repair",
                   follow_up_of=c["id"])  # fmt: skip
        self._owner(self.s.case(cid), "warranty", f"{self._name(c)}: the repair by {rep['technician']} did not work. "
                    "A warranty claim is logged against the repair record; I will find a second mechanic.")  # fmt: skip
        return cid

    def verify_after_repair(self, cid: str) -> None:
        """Verification is the deterministic layer's job, also for planner-driven cases: ask for readings,
        then compare with baseline v1 (adds the two steps once)."""
        c = self.s.case(cid)
        if "verify_repair" not in c["plan"]:
            self.s.extend_plan(cid, ["request_post_repair_readings", "verify_repair"])

    @staticmethod
    def _snap(health: dict[str, Any], ref: Any) -> dict[str, Any]:
        return {"urgency": health["urgency"], "severity": health.get("severity"), "risk": health.get("recent_risk"),
                "confidence": health.get("confidence"), "ref": ref}  # fmt: skip

    def _skip_open_steps(self, cid: str) -> None:
        for st in self.s.steps(cid):
            if st["status"] not in ("done", "skipped"):
                self.s.set_step(cid, st["idx"], status="skipped", error="superseded by a new plan")

    def _close(self, cid: str, outcome: str, **detail: Any) -> dict[str, Any]:
        c = self.s.case(cid)
        self._skip_open_steps(cid)
        self.s.update_case(cid, c["version"], state="closed", closed=self.clock(), outcome=outcome)
        self.s.expire_requests(cid)  # questions about a closed case are no longer asked
        self.b.set_hold(c["machine"], False)
        self.s.log("agent", "case_closed", cid, outcome=outcome, **detail)
        return self.s.case(cid)

    # ---------------------------------------------------------------- act (idempotent, gated, offline-safe)
    def autonomy(self, cid: str) -> dict[str, Any] | None:
        """The owner's autonomy policy for this case's factory, or None (the default: always ask)."""
        m = self.s.machine(self.s.case(cid)["machine"])
        pol = (self.t.factories.get(m["factory"]) or {}).get("autonomy")
        if not pol or not pol.get("mechanic") or not pol.get("auto_approve_under_inr"):
            return None
        return dict(pol)

    def auto_approve(self, cid: str, kind: str, technician: str | None) -> dict[str, Any] | None:
        """An approval decided by the owner's policy, or None. Only bookings of the named mechanic whose
        visit cost (inspection range, high end) is within the limit; never a purchase, never a stop."""
        if kind != "book_job":  # order_part, production_stop and anything else: always the owner
            return None
        pol = self.autonomy(cid)
        if pol is None or technician != pol["mechanic"]:
            return None
        cost = inspection_cost()
        if cost["high_inr"] > float(pol["auto_approve_under_inr"]):
            self.s.log("agent", "autonomy_declined", cid, reason="visit cost above the owner's limit",
                       high_inr=cost["high_inr"], limit=pol["auto_approve_under_inr"])  # fmt: skip
            return None
        pending = [a for a in self.s.approvals(cid, status="requested") if a["kind"] == "booking"]
        aid = pending[-1]["id"] if pending else f"{cid}:booking:auto"
        if not pending:
            self.s.request_approval(aid, cid, "booking", f"Visit by {technician} (owner's autonomy policy)",
                                    (cost["low_inr"], cost["high_inr"]))  # fmt: skip
        ref = f"policy: {technician} under Rs {pol['auto_approve_under_inr']}"
        self.s.decide(aid, "approved", "autonomy_policy", "policy", ref)
        self.s.log("autonomy_policy", "auto_approved", cid, approval=aid, technician=technician,
                   high_inr=cost["high_inr"], limit=pol["auto_approve_under_inr"])  # fmt: skip
        return self.s.approval(aid)

    def act(self, cid: str, kind: str, purpose: str, payload: dict[str, Any]) -> dict[str, Any]:
        if kind in GATED:
            need = GATED[kind]
            ok = [a for a in self.s.approvals(cid, status="approved") if a["kind"] == need]
            if not ok and (auto := self.auto_approve(cid, kind, payload.get("technician"))):
                ok = [auto]
            if not ok:
                self.s.log("agent", "blocked_without_approval", cid, kind=kind, need=need)
                raise ApprovalRequired(f"{kind} needs the owner's approved '{need}' for case {cid}")
            payload = {**payload, "approval": ok[-1]["id"], "approval_ref": ok[-1]["ref"]}
        idem = f"{cid}:{kind}:{purpose}"
        prev = self.s.action(idem)
        if prev and prev["status"] == "done":
            return prev["result"]
        if prev and prev["status"] == "failed":
            raise Unavailable(prev["error"])
        if prev and prev["status"] == "held":  # approved again: release it
            self.s.set_action(idem, status="queued", next_try=0, error=None)
            prev = self.s.action(idem)
        if prev is None:
            self.s.put_action(idem, cid, kind, payload)
            self.s.log("agent", "action_created", cid, kind=kind, purpose=purpose)
        elif prev["next_try"] > self.clock():
            raise Waiting(f"{kind} queued, retry at {prev['next_try']:.0f}")
        return self._send(idem)

    def _approved(self, cid: str, kind: str) -> list[dict[str, Any]]:
        return [a for a in self.s.approvals(cid, status="approved") if a["kind"] == GATED[kind]]

    def _send(self, idem: str) -> dict[str, Any]:
        a = self.s.action(idem)
        assert a is not None
        if a["kind"] in GATED and not self._approved(a["case_id"], a["kind"]):
            # checked again at send time: an approval can change while an action waits in the queue
            self.s.set_action(idem, status="held", error="approval no longer valid")
            self.s.log("agent", "blocked_without_approval", a["case_id"], kind=a["kind"], at="send")
            raise ApprovalRequired(f"{a['kind']} held: the owner's approval is no longer valid")
        try:
            result = self.t.transport.call(a["kind"], a["payload"], idem)
        except ToolError as e:
            n = a["attempts"] + 1
            delay = min(self.a["retry_max_s"], self.a["retry_base_s"] * 2 ** (n - 1))
            self.s.set_action(idem, attempts=n, next_try=self.clock() + delay, error=str(e), status="queued")
            self.s.log("system", "action_queued", a["case_id"], kind=a["kind"], error=str(e), attempt=n)
            raise Waiting(f"{a['kind']} queued ({e})") from e
        except Unavailable as e:
            self.s.set_action(idem, status="failed", error=str(e))
            self.s.log("system", "action_refused", a["case_id"], kind=a["kind"], error=str(e))
            raise
        self.s.set_action(idem, status="done", result=result, error=None)
        self.s.log("agent", "action_done", a["case_id"], kind=a["kind"], to=a["payload"].get("to"),
                   technician=a["payload"].get("technician"))  # fmt: skip
        return result

    def sync(self) -> dict[str, int]:
        """Resend queued actions that are due (called by tick; the network may be back)."""
        online = self.t.network.check()
        was = self.s.meta("net_online")
        if was != ("1" if online else "0"):
            self.s.meta("net_online", "1" if online else "0")
            if online and was == "0":
                n = len(self.s.queued_actions())
                self.emit("network_restored", payload={"queued_actions": n},
                          idem=f"net:{self.s.meta('net_flips') or 0}")  # fmt: skip
            self.s.meta("net_flips", str(int(self.s.meta("net_flips") or 0) + 1))
        sent = waiting = 0
        for a in self.s.queued_actions():
            if a["next_try"] > self.clock():
                waiting += 1
                continue
            try:
                self._send(a["idem"])
                sent += 1
            except (Waiting, Unavailable, ApprovalRequired):
                waiting += 1
        return {"sent": sent, "waiting": waiting}

    # ---------------------------------------------------------------- people
    def decide(self, approval_id: str, decision: str, by: str, channel: str, ref: str) -> dict[str, Any]:
        out = self.s.decide(approval_id, decision, by, channel, ref)
        a = self.s.approval(approval_id)
        assert a is not None
        self.s.log(by, "decision", a["case_id"], approval=approval_id, kind=a["kind"], decision=decision,
                   channel=channel, ref=ref, result=out)  # fmt: skip
        return out

    def approval_covers(self, cid: str, technician: str) -> dict[str, Any] | None:
        """After a re-plan (the approved mechanic declined or did not come), the owner's approved booking for
        this case still covers a replacement whose visit costs no more than what was approved. A dearer one
        needs the owner again. Returns the covering approval, or None."""
        ctx = self.s.case(cid)["context"]
        if not ctx.get("declined"):
            return None
        new_high = inspection_cost(self.t.technicians.get(technician))["high_inr"]
        ok = [a for a in self.s.approvals(cid, status="approved") if a["kind"] == "booking"
              and a["amount_high"] is not None and new_high <= a["amount_high"]]  # fmt: skip
        return ok[-1] if ok else None

    def replan_visit_day(self, cid: str, technician: str, day: str) -> dict[str, Any]:
        """The mechanic can come, but not on that day: keep them, find another day (no new approval: same
        mechanic, same cost)."""
        c = self.s.case(cid)
        ctx = c["context"]
        avoid = sorted({*ctx.get("avoid_days", []), f"{technician}:{day}"})
        ctx = ctx | {"avoid_days": avoid, "booked": False, "visit_day": None}
        self.s.update_case(cid, c["version"], context=ctx)
        if not self._planner_driven(c):
            for st in self.s.steps(cid):
                if st["name"] in ("find_technician", "book_job", "await_repair") and st["status"] in (
                    "done", "waiting", "failed", "retry"):  # fmt: skip
                    self.s.set_step(
                        cid, st["idx"], status="pending", error=f"another day: {technician} not on {day}"
                    )
        self.s.log(
            "agent",
            "replanned_visit_day",
            cid,
            technician=technician,
            was=day,
            why="mechanic asked for another time",
        )
        return self.s.case(cid)

    def repair_done(self, cid: str, technician: str, work: str) -> None:
        c = self.s.case(cid)
        ctx = c["context"] | {"repair": {"technician": technician, "work": work, "at": self.clock()}}
        self.s.update_case(cid, c["version"], context=ctx)
        self.s.log("technician", "repair_reported", cid, technician=technician, work=work)
        if self._planner_driven(self.s.case(cid)):
            self.verify_after_repair(cid)
        self.emit("repair_done", case_id=cid, machine=c["machine"], idem=f"repair:{cid}",
                  payload={"technician": technician, "work": work})  # fmt: skip

    # ---------------------------------------------------------------- run
    def tick(self) -> list[dict[str, Any]]:
        self.sync()
        self._no_shows()
        return [self.run_case(c["id"]) for c in self.s.cases(open_only=True)]

    def _no_shows(self) -> None:
        """A booked visit whose day has passed with no repair reported: the mechanic did not come."""
        today = datetime.fromtimestamp(self.clock()).date().isoformat()
        for c in self.s.cases(open_only=True):
            ctx = c["context"]
            day, tech = ctx.get("visit_day"), ctx.get("technician")
            booked = ctx.get("booked") or any(
                s["name"] == "book_job" and s["status"] == "done" for s in self.s.steps(c["id"])
            )
            if booked and day and tech and day < today and "repair" not in ctx:
                self.emit("mechanic_no_show", case_id=c["id"], machine=c["machine"],
                          idem=f"noshow:{c['id']}:{tech}:{day}", payload={"technician": tech, "day": day})  # fmt: skip

    def run_case(self, cid: str) -> dict[str, Any]:
        for _ in range(50):  # a tick runs steps until one waits
            c = self.s.case(cid)
            if c["closed"]:
                break
            open_steps = [s for s in self.s.steps(cid) if s["status"] not in ("done", "skipped")]
            # the planner marker never blocks deterministic steps queued behind it (verification)
            open_steps = [s for s in open_steps if s["name"] != "planner"] or open_steps
            # telling the owner about an urgent change never waits behind another step
            step = next(
                (s for s in open_steps if s["name"] in PRIORITY), open_steps[0] if open_steps else None
            )
            if step is None:
                break
            if step["next_try"] > self.clock():
                break
            self.s.set_step(cid, step["idx"], status="running")
            run = self._run(c, step)
            try:
                result = getattr(self, f"_step_{step['name']}")(c, step)
            except Waiting as w:
                now = next(s for s in self.s.steps(cid) if s["idx"] == step["idx"])
                if now["status"] == "running":  # a step that re-planned (skipped itself) stays skipped
                    self.s.set_step(cid, step["idx"], status="waiting", error=str(w))
                    self._run_wait(run, step, str(w))
                    break
                self._run_end(run, "completed", None, f"re-planned: {w}")
                continue
            except ApprovalRequired as e:
                # the approval changed after it was checked (a conflicting answer arrived): ask again
                wait = next((s for s in self.s.steps(cid) if s["name"] == "await_approval"), None)
                if wait is not None:
                    self.s.set_step(cid, wait["idx"], status="pending")
                self.s.set_step(cid, step["idx"], status="waiting", error=str(e))
                self._run_wait(run, step, str(e), status="awaiting_approval")
                continue
            except ToolError as e:
                n = step["attempts"] + 1
                delay = min(self.a["retry_max_s"], self.a["retry_base_s"] * 2 ** (n - 1))
                self.s.set_step(
                    cid, step["idx"], status="retry", attempts=n, next_try=self.clock() + delay, error=str(e)
                )
                if run is not None and self.activity:
                    self.activity.update(run, "retrying", error=str(e), retry=True)
                break
            except Exception as e:  # a bug or a refused action: the step fails visibly, the case stays open
                self.s.set_step(cid, step["idx"], status="failed", error=f"{type(e).__name__}: {e}")
                self._run_end(run, "failed", None, f"{type(e).__name__}: {e}")
                self.s.log("agent", "step_failed", cid, step=step["name"], error=str(e))
                break
            self.s.set_step(cid, step["idx"], status="done", result=result, error=None)
            self.s.log("agent", "step_done", cid, step=step["name"], result=result)
            self._run_end(run, "completed", result, summarise(step["name"], result))
        return self.s.case(cid)

    # ---------------------------------------------------------------- agent activity
    def _run(self, c: dict[str, Any], step: dict[str, Any]) -> int | None:
        if not self.activity:
            return None
        from server.agents import STEP_AGENT

        agent, status, tools = STEP_AGENT.get(step["name"], ("orchestrator", "executing", []))
        prev = self.activity.open_for_step(c["id"], step["idx"])
        if prev:  # a step that was waiting continues the same run
            self.activity.update(prev["id"], status)
            return int(prev["id"])
        last = (c["context"].get("history") or [{}])[-1]
        return int(self.activity.start(agent, step["name"].replace("_", " "), status, machine=c["machine"],
                                       case_id=c["id"], step=step["idx"], tools=tools,
                                       observation=f"urgency {c['urgency']}, recent risk {last.get('risk')}"))  # fmt: skip

    def _run_wait(self, run: int | None, step: dict[str, Any], why: str, status: str | None = None) -> None:
        if run is None or not self.activity:
            return
        from server.agents import STEP_AGENT

        self.activity.update(run, status or STEP_AGENT.get(step["name"], ("", "monitoring", []))[1], error=why,
                             reasoning=why)  # fmt: skip

    def _run_end(self, run: int | None, status: str, result: Any, reasoning: str) -> None:
        if run is not None and self.activity:
            self.activity.finish(run, status, result, reasoning)

    # ---------------------------------------------------------------- steps
    def _step_plan_inspection(self, c: dict[str, Any], st: dict[str, Any]) -> dict[str, Any]:
        """Planning agent: when to look, how long the machine is down, and in what order across open cases."""
        m = self.s.machine(c["machine"])
        prof = self.t.factories.get(m["factory"], {})
        opened = sorted(self.s.cases(open_only=True), key=lambda x: (-RANK[x["urgency"]], x["opened"]))
        queue = [x["id"] for x in opened if "book_job" in x["plan"]]
        position = queue.index(c["id"]) if c["id"] in queue else 0
        per_day = 2  # assumption: a technician fits two inspection visits in a day
        today = datetime.fromtimestamp(self.clock()).date()
        earliest = today + timedelta(days=position // per_day)
        cuts = prof.get("power_cuts") or []
        window = (f"{cuts[0][0]}-{cuts[0][1]}, during the scheduled power cut (the machine is off anyway)" if cuts
                  else "first hour of the shift (assumption)")  # fmt: skip
        downtime = float(self.t.service_hours.get(m["mtype"], 2.0))
        same_site = [x["machine"] for x in opened if x["id"] != c["id"] and "book_job" in x["plan"]
                     and self.s.machine(x["machine"])["factory"] == m["factory"]]  # fmt: skip
        plan = {"priority": position + 1, "of": len(queue), "earliest_day": earliest.isoformat(), "window": window,
                "expected_downtime_h": downtime, "downtime_basis": "planned service hours by machine type (assumption)",
                "combine_with": same_site,
                "alternatives": ["keep running and re-measure in one cycle (owner's choice)",
                                 "inspect at the next planned stop"]}  # fmt: skip
        cur = self.s.case(c["id"])
        self.s.update_case(c["id"], cur["version"], context=cur["context"] | {"plan": plan})
        return plan

    def _owner(self, c: dict[str, Any], purpose: str, text: str, to: str = "owner") -> dict[str, Any]:
        return self.act(
            c["id"], "notify", purpose, {"to": self.t.contacts[to], "text": text, "machine": c["machine"]}
        )

    def _name(self, c: dict[str, Any]) -> str:
        return str(self.s.machine(c["machine"])["name"])

    def _step_recall(self, c: dict[str, Any], st: dict[str, Any]) -> dict[str, Any]:
        """Memory: earlier cases, repairs and whether they held, technicians who worked on this machine."""
        m = c["machine"]
        reps = self.s.repairs(m)
        past = [x for x in self.s.cases(m) if x["id"] != c["id"]]
        recent = [
            r for r in reps if r["done"] and self.clock() - r["done"] < self.a["recurrence_days"] * 86400
        ]
        by_tech: dict[str, dict[str, int]] = {}
        for r in reps:
            d = by_tech.setdefault(r["technician"], {"jobs": 0, "fixed": 0})
            d["jobs"] += 1
            d["fixed"] += r["outcome"] == "fixed"
        versions = self.b.versions(m)
        out = {"earlier_cases": len(past), "earlier_outcomes": [x["outcome"] for x in past],
               "repairs": len(reps), "technicians": by_tech,
               "recurrence_after_repair": bool(recent), "baseline_versions": len(versions),
               "baseline_v1_learned_from": versions[0]["n_rows"] if versions else None}  # fmt: skip
        if recent:
            out["note"] = (f"Anomaly again {int((self.clock() - recent[-1]['done']) / 86400)} days after a repair by "
                           f"{recent[-1]['technician']} ({recent[-1]['outcome']}). The earlier repair may not have held.")  # fmt: skip
        return out

    def _step_request_measurement(self, c: dict[str, Any], st: dict[str, Any]) -> dict[str, Any]:
        self._owner(c, "measure", f"{self._name(c)} sounded different once. Please record it again for 60 s; "
                                  "nothing to do yet.")  # fmt: skip
        return {"asked": "another reading"}

    def _step_notify_watch(self, c: dict[str, Any], st: dict[str, Any]) -> dict[str, Any]:
        self._owner(c, "watch", f"{self._name(c)}: small change in its sound for a few readings. Watching it; "
                                "it goes in today's digest.")  # fmt: skip
        return {"told": "owner (digest)"}

    def _step_await_change(self, c: dict[str, Any], st: dict[str, Any]) -> dict[str, Any]:
        raise Waiting("watching: closes when normal again, escalates if it grows")

    def _step_planner(self, c: dict[str, Any], st: dict[str, Any]) -> dict[str, Any]:
        raise Waiting("the planner decides the next step (server/planner); nothing runs here")

    def _step_notify_owner_urgent(self, c: dict[str, Any], st: dict[str, Any]) -> dict[str, Any]:
        aid = f"{c['id']}:production_stop"
        self.s.request_approval(aid, c["id"], "production_stop",
                                f"{self._name(c)} changed a lot and keeps rising. Stopping it is your decision.")  # fmt: skip
        self._owner(c, "urgent", f"{self._name(c)} needs attention today: its sound changed a lot and keeps rising. "
                                 f"Whether to stop it is your decision. Reply STOP-NOTED if you stop it.")  # fmt: skip
        return {
            "asked_owner": "production decision (owner only; the agent cannot stop a machine)",
            "approval": aid,
        }

    def _step_find_technician(self, c: dict[str, Any], st: dict[str, Any]) -> dict[str, Any]:
        ctx = c["context"]
        skill = ctx.get("fault") or "inspection"
        reps = self.s.repairs(c["machine"])
        bad = {r["technician"] for r in reps if r["outcome"] in ("failed",)}
        exclude = set(ctx.get("declined", [])) | bad
        today = datetime.fromtimestamp(self.clock()).date()
        start = ctx.get("plan", {}).get("earliest_day")
        if start and start > today.isoformat():  # the planning agent put this case behind more urgent ones
            today = datetime.fromisoformat(start).date()
        for d in range(self.a["search_days"] + 1):
            day = today + timedelta(days=d)
            found = self.t.technicians.find(skill, day, exclude)
            avoid = set(ctx.get("avoid_days", []))
            found = [p for p in found if f"{p['id']}:{day.isoformat()}" not in avoid]
            if found:
                good = {r["technician"] for r in reps if r["outcome"] == "fixed"}
                found.sort(key=lambda p: (p["id"] not in good, p["km"]))
                tech = found[0]
                ctx = self.s.case(c["id"])["context"] | {
                    "technician": tech["id"],
                    "visit_day": day.isoformat(),
                }
                self.s.update_case(c["id"], self.s.case(c["id"])["version"], context=ctx)
                return {"technician": tech["id"], "name": tech["name"], "day": day.isoformat(),
                        "why": "worked here before and fixed it" if tech["id"] in good else "nearest who works that day",
                        "excluded": sorted(exclude), "source": tech["source"]}  # fmt: skip
        self._owner(c, f"no_technician:{len(exclude)}",
                    f"No mechanic for {self._name(c)} found in the next {self.a['search_days']} days. "
                    "Do you know someone? I will keep looking.")  # fmt: skip
        raise ToolError("no technician available; retry later")

    def _step_check_parts(self, c: dict[str, Any], st: dict[str, Any]) -> dict[str, Any]:
        m = self.s.machine(c["machine"])
        faults = [c["context"]["fault"]] if c["context"].get("fault") else self._likely_faults(m)
        out = []
        for f in faults:
            rows = self.t.parts.stock(OWNER_WORD.get(f, f), m["mtype"])
            out.append(
                {
                    "fault": f,
                    "stock": rows or "none listed",
                    "order_needed": bool(rows) and not any(r["in_stock"] for r in rows),
                }
            )
            if out[-1]["order_needed"]:
                self.emit("part_out_of_stock", case_id=c["id"], machine=c["machine"], idem=f"oos:{c['id']}:{f}",
                          payload={"fault": f, "machine_type": m["mtype"], "parts": [r["part"] for r in rows]})  # fmt: skip
        return {"candidates": out, "note": "the inspection decides which part, if any, is needed"}

    @staticmethod
    def _likely_faults(m: dict[str, Any]) -> list[str]:
        import yaml

        from .tools import ROOT

        presets = yaml.safe_load((ROOT / "data" / "presets.yaml").read_text(encoding="utf-8"))["presets"]
        for mach in presets.get(m["preset"], {}).get("machines", []):
            if mach.get("kind") == m["kind"]:
                return list(mach.get("faults", []))
        return ["bearing_wear"]

    def _step_estimate_cost(self, c: dict[str, Any], st: dict[str, Any]) -> dict[str, Any]:
        m = self.s.machine(c["machine"])
        parts = next(s for s in self.s.steps(c["id"]) if s["name"] == "check_parts")["result"] or {}
        faults = [x["fault"] for x in parts.get("candidates", [])]
        return {"inspection": inspection_cost(), "if_found": [cost_range(f, m["hp"]) for f in faults]}

    def _step_request_approval(self, c: dict[str, Any], st: dict[str, Any]) -> dict[str, Any]:
        steps = {s["name"]: s["result"] for s in self.s.steps(c["id"])}
        tech, cost = steps["find_technician"], steps["estimate_cost"]
        insp = cost["inspection"]
        aid = f"{c['id']}:booking"
        summary = (f"Book {tech['name']} on {tech['day']} to inspect {self._name(c)}: about Rs {insp['low_inr']}-"
                   f"{insp['high_inr']} for the visit. If a part is needed: "
                   + "; ".join(f"{x['fault'].replace('_', ' ')} Rs {x['low_inr']}-{x['high_inr']}" for x in cost["if_found"])
                   + " (typical, confirm with dealer).")  # fmt: skip
        self.s.request_approval(aid, c["id"], "booking", summary, (insp["low_inr"], insp["high_inr"]))
        self._owner(c, "approval_booking", summary + " Reply 1 to book, 2 for later.")
        return {"approval": aid, "summary": summary}

    def _step_await_approval(self, c: dict[str, Any], st: dict[str, Any]) -> dict[str, Any]:
        aid = f"{c['id']}:booking"
        a = self.s.approval(aid)
        assert a is not None
        if a["status"] == "approved":
            return {"approved_by": a["decided_by"], "channel": a["channel"], "ref": a["ref"]}
        if a["status"] == "rejected":
            self._skip_open_steps(c["id"])
            self.s.extend_plan(c["id"], ["notify_watch", "await_change"])
            self.s.log("agent", "owner_declined", c["id"], approval=aid)
            raise Waiting("owner said not now; watching instead")
        if a["status"] == "conflict":
            self._owner(c, f"conflict:{a['note']}", f"Two different answers came for {self._name(c)} ({a['note']}). "
                                                      "Please reply once more: 1 to book, 2 for later.")  # fmt: skip
            self.s.reset_conflict(aid)
            raise Waiting("asked the owner again after conflicting answers")
        if (
            c["urgency"] == "urgent_owner_attention"
            and self.clock() - a["requested"] > self.a["escalate_after_s"]
        ):
            self._owner(c, "escalate_supervisor", f"No answer from the owner for 2 h on {self._name(c)}: "
                                                  f"{a['summary']}", to="supervisor")  # fmt: skip
        raise Waiting("waiting for the owner's answer")

    def _step_book_job(self, c: dict[str, Any], st: dict[str, Any]) -> dict[str, Any]:
        ctx = c["context"]
        today = datetime.fromtimestamp(self.clock()).date().isoformat()
        if ctx["visit_day"] < today:  # the owner answered after the planned day: find a new day first
            find = next(s for s in self.s.steps(c["id"]) if s["name"] == "find_technician")
            self.s.set_step(c["id"], find["idx"], status="pending")
            self.s.log("agent", "replanned_visit_day", c["id"], was=ctx["visit_day"], today=today)
            raise Waiting(f"planned day {ctx['visit_day']} has passed; finding a new day")
        payload = {"technician": ctx["technician"], "machine": c["machine"], "day": ctx["visit_day"],
                   "work": "inspection" if not ctx.get("fault") else ctx["fault"]}  # fmt: skip
        if ctx.get("declined") and self._dearer_than_approved(c, ctx["technician"]):
            raise Waiting(
                "the replacement mechanic costs more than the owner approved: asking the owner again"
            )
        try:
            job = self.act(c["id"], "book_job", f"{ctx['technician']}:{ctx['visit_day']}", payload)
        except Unavailable as e:
            declined = [*ctx.get("declined", []), ctx["technician"]]
            self.s.update_case(c["id"], c["version"], context=ctx | {"declined": declined})
            find = next(s for s in self.s.steps(c["id"]) if s["name"] == "find_technician")
            self.s.set_step(c["id"], find["idx"], status="pending")
            self.s.log("agent", "technician_unavailable", c["id"], technician=ctx["technician"], error=str(e))
            self.emit("mechanic_reply", case_id=c["id"], machine=c["machine"],
                      idem=f"declined:{c['id']}:{ctx['technician']}:{ctx['visit_day']}",
                      payload={"technician": ctx["technician"], "answer": "declined", "day": ctx["visit_day"]})  # fmt: skip
            raise Waiting(f"{ctx['technician']} not available; looking for another") from e
        self._owner(c, f"booked:{ctx['technician']}:{ctx['visit_day']}",
                    f"Booked {self.t.technicians.get(ctx['technician'])['name']} for {self._name(c)} on {ctx['visit_day']}.")  # fmt: skip
        return {"job": job, **payload}

    def _step_await_repair(self, c: dict[str, Any], st: dict[str, Any]) -> dict[str, Any]:
        if "repair" not in c["context"]:
            raise Waiting("waiting for the technician to report the work done")
        return c["context"]["repair"]

    def _dearer_than_approved(self, c: dict[str, Any], technician: str) -> bool:
        """Fixed plan, after a re-plan: if the new mechanic's visit costs more than the approved booking, the
        approval is reopened (same id, new amount) and the owner is asked again; await_approval waits for it.
        """
        aid = f"{c['id']}:booking"
        a = self.s.approval(aid)
        if a is None or a["status"] != "approved" or a["amount_high"] is None:
            return False
        tech = self.t.technicians.get(technician)
        cost = inspection_cost(tech)
        if cost["high_inr"] <= a["amount_high"]:
            return False
        summary = (f"{tech['name']} instead, for {self._name(c)}: about Rs {cost['low_inr']}-{cost['high_inr']} for the "
                   f"visit (approved before: up to Rs {a['amount_high']}).")  # fmt: skip
        self.s.reopen_approval(aid, summary, (cost["low_inr"], cost["high_inr"]))
        for st in self.s.steps(c["id"]):
            if st["name"] == "await_approval":
                self.s.set_step(c["id"], st["idx"], status="pending", error="cost changed: owner asked again")
        self._owner(c, f"reapprove:{technician}", summary + " Reply 1 to book, 2 for later.")
        self.s.log("agent", "reapproval_cost_changed", c["id"], technician=technician, was_high=a["amount_high"],
                   now_high=cost["high_inr"])  # fmt: skip
        return True

    def _step_request_post_repair_readings(self, c: dict[str, Any], st: dict[str, Any]) -> dict[str, Any]:
        self._owner(c, "after_repair", f"{self._name(c)} was repaired. Please record it for 60 s with the phone "
                                       "in the same place as always.")  # fmt: skip
        return {"asked": "post-repair readings"}

    def _step_verify_repair(self, c: dict[str, Any], st: dict[str, Any]) -> dict[str, Any]:
        """Compare readings after the repair with the machine's ORIGINAL learned normal (baseline v1)."""
        m, rep = c["machine"], c["context"]["repair"]
        after = [
            h for h in self.b.history(m) if h["ts"] >= rep["at"] and h["running"] and h["risk"] is not None
        ]
        if len(after) < self.a["verify_readings"]:
            raise Waiting(f"{len(after)} of {self.a['verify_readings']} readings after the repair")
        before = [
            h for h in self.b.history(m) if h["ts"] < rep["at"] and h["running"] and h["risk"] is not None
        ]
        since = c["context"].get(
            "verify_since", c["opened"]
        )  # a live machine's readings are on wall-clock time
        before = [h for h in before if h["ts"] >= since] or before[-6:]
        v1 = self.b.model(m, 1)
        assert v1 is not None
        r_after = v1.risk(self.b.vectors([h["id"] for h in after[: self.a["verify_readings"] * 2]]))
        r_before = v1.risk(self.b.vectors([h["id"] for h in before[-12:]]))
        res = prove.verify_repair(np.asarray(r_before).tolist(), np.asarray(r_after).tolist())
        repair_id = self.s.add_repair(machine=m, case_id=c["id"], technician=rep["technician"], work=rep["work"],
                                      done=rep["at"], outcome=res["outcome"], before_median=res["before_median_risk"],
                                      after_median=res["after_median_risk"], against_version=1)  # fmt: skip
        if self.on_verified is not None:  # the signed repair record (server/prove), made by whoever owns it
            try:
                self.on_verified(c, res, repair_id, [float(x) for x in np.asarray(r_after).tolist()])
            except Exception as e:  # a record that cannot be signed must not undo the verification
                self.s.log("system", "record_failed", c["id"], error=f"{type(e).__name__}: {e}"[:200])
        words = {"fixed": "back to its original normal", "partly": "better, but not back to normal",
                 "failed": "not better: the repair did not work (possibly a wrong or poor part)"}[res["outcome"]]  # fmt: skip
        self._owner(c, "verified", f"{self._name(c)} after the repair: {words}.")
        self._close(c["id"], f"repair_{res['outcome']}", verification=res)
        follow = (
            self.warranty_and_second_opinion(c, res, rep, repair_id) if res["outcome"] == "failed" else None
        )
        self.emit("verify_result", case_id=follow or c["id"], machine=m, idem=f"verify:{c['id']}",
                  payload={"outcome": res["outcome"], "technician": rep["technician"], "verified_case": c["id"],
                           "follow_up_case": follow,
                           "before_median_risk": res["before_median_risk"],
                           "after_median_risk": res["after_median_risk"]})  # fmt: skip
        return res
