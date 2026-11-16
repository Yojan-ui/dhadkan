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
                 clock: Any = time.time, a: dict[str, Any] | None = None, activity: Any = None) -> None:  # fmt: skip
        self.s, self.b, self.t = store, baselines, tools or Tools()
        self.clock = clock
        self.s.clock = clock
        self.a = {**ASSUMPTIONS, **(a or {})}
        self.activity = activity  # server/agents.Activity: which named agent ran which step (optional)

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
                   "declined": [], "none_streak": 0}  # fmt: skip
            case = self.s.create_case(
                cid, machine, urgency, health.get("severity", "none"), plan_for(urgency), ctx
            )
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
        in_fix = "book_job" in case["plan"]
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
    def act(self, cid: str, kind: str, purpose: str, payload: dict[str, Any]) -> dict[str, Any]:
        if kind in GATED:
            need = GATED[kind]
            ok = [a for a in self.s.approvals(cid, status="approved") if a["kind"] == need]
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

    def repair_done(self, cid: str, technician: str, work: str) -> None:
        c = self.s.case(cid)
        ctx = c["context"] | {"repair": {"technician": technician, "work": work, "at": self.clock()}}
        self.s.update_case(cid, c["version"], context=ctx)
        self.s.log("technician", "repair_reported", cid, technician=technician, work=work)

    # ---------------------------------------------------------------- run
    def tick(self) -> list[dict[str, Any]]:
        self.sync()
        return [self.run_case(c["id"]) for c in self.s.cases(open_only=True)]

    def run_case(self, cid: str) -> dict[str, Any]:
        for _ in range(50):  # a tick runs steps until one waits
            c = self.s.case(cid)
            if c["closed"]:
                break
            open_steps = [s for s in self.s.steps(cid) if s["status"] not in ("done", "skipped")]
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
        try:
            job = self.act(c["id"], "book_job", f"{ctx['technician']}:{ctx['visit_day']}", payload)
        except Unavailable as e:
            declined = [*ctx.get("declined", []), ctx["technician"]]
            self.s.update_case(c["id"], c["version"], context=ctx | {"declined": declined})
            find = next(s for s in self.s.steps(c["id"]) if s["name"] == "find_technician")
            self.s.set_step(c["id"], find["idx"], status="pending")
            self.s.log("agent", "technician_unavailable", c["id"], technician=ctx["technician"], error=str(e))
            raise Waiting(f"{ctx['technician']} not available; looking for another") from e
        self._owner(c, f"booked:{ctx['technician']}:{ctx['visit_day']}",
                    f"Booked {self.t.technicians.get(ctx['technician'])['name']} for {self._name(c)} on {ctx['visit_day']}.")  # fmt: skip
        return {"job": job, **payload}

    def _step_await_repair(self, c: dict[str, Any], st: dict[str, Any]) -> dict[str, Any]:
        if "repair" not in c["context"]:
            raise Waiting("waiting for the technician to report the work done")
        return c["context"]["repair"]

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
        before = [h for h in before if h["ts"] >= c["opened"]] or before[-6:]
        v1 = self.b.model(m, 1)
        assert v1 is not None
        r_after = v1.risk(self.b.vectors([h["id"] for h in after[: self.a["verify_readings"] * 2]]))
        r_before = v1.risk(self.b.vectors([h["id"] for h in before[-12:]]))
        res = prove.verify_repair(np.asarray(r_before).tolist(), np.asarray(r_after).tolist())
        self.s.add_repair(machine=m, case_id=c["id"], technician=rep["technician"], work=rep["work"], done=rep["at"],
                          outcome=res["outcome"], before_median=res["before_median_risk"],
                          after_median=res["after_median_risk"], against_version=1)  # fmt: skip
        words = {"fixed": "back to its original normal", "partly": "better, but not back to normal",
                 "failed": "not better: the repair did not work (possibly a wrong or poor part)"}[res["outcome"]]  # fmt: skip
        self._owner(c, "verified", f"{self._name(c)} after the repair: {words}.")
        self._close(c["id"], f"repair_{res['outcome']}", verification=res)
        return res
