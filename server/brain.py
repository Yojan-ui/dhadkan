"""The factory brain: the simulated cluster's readings go through the agents on every tick.

    brain = Brain(live)          # hooks into live.c.on_step; restores everything from SQLite on restart
    brain.state()                # what the command centre shows (/api/cc/state)

Per tick, per machine that produced a reading:
    Observation   quality + summary of the 10 s recording -> this machine's learned normal (edge/baseline)
    Intelligence  health: persistent?, how sure, what changed (server/health)
    Prediction    time to the maintenance threshold or "not enough data" (edge/rul)
    Orchestrator  opens / escalates a case, runs its steps through the other agents (server/maint/agent)
Then the outbox and the audit replica sync if the link is up (the link switch in the top bar).

Events (server/planner/events): an urgency change is a health_change event, a rise to inspect or above
an alarm, a new factory day a daily_tick; owner decisions, replies, voice notes and the agent's own
events (repair done, verify result, ...) join them. The planner (server/planner/agent) handles each one:
with ANTHROPIC_API_KEY on a worker thread (Claude chooses the tool calls, outside the tick lock), without
one at the end of the tick (the rules: the fixed plan runs exactly as before).

State lives in data/brain_edge.db (readings, baselines), data/brain_maint.db (cases, steps, actions,
approvals, repairs, agent runs, audit) and data/brain_cloud.db (the cloud replica). The clock is the
factory clock of the simulation. Readings are recorded MIMII clips replayed by the simulator, so they
carry source "simulated"; the machines, owners and technicians are demo data.
"""

import contextlib
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from edge import adapt, rul, summary
from edge.baseline import BaselineStore
from edge.detectors import AE_COL, WIDTH
from edge.summary import S

from . import health
from .agents import OPEN, Activity
from .config import NODE_DB
from .maint import agent as ag
from .maint.store import Store
from .maint.sync import Cloud, SyncClient
from .maint.tools import Network, Transport
from .planner import llm as planner_llm
from .planner.agent import Planner
from .planner.events import Event, EventBus

RISK_THRESHOLD = health.ASSUMPTIONS["severe"]
BACKFILL_TICKS = 60
PRESET = {"pump": "pump_set", "fan": "flour_mill", "gearbox": "machine_shop"}
KIND = {"pump": "pump", "fan": "blower", "gearbox": "mill"}

OP_SCHEMA = """
CREATE TABLE IF NOT EXISTS op_log (tick INTEGER, ts REAL, machine TEXT, run TEXT, sensor TEXT, reading INTEGER,
  status TEXT, urgency TEXT, PRIMARY KEY (tick, machine));
"""


class Brain:
    def __init__(self, live: Any, workdir: Path | None = None, backfill: bool = True) -> None:
        self.live = live
        self.c = live.c
        wd = workdir or Path(NODE_DB).parent
        # one lock with the live server: the simulator's SQLite connection is shared between threads
        self.lock = getattr(live, "lock", None) or threading.RLock()
        self.clock = lambda: self.c.now().timestamp()
        self.base = BaselineStore(wd / "brain_edge.db")
        self.store = Store(wd / "brain_maint.db", clock=self.clock)
        self.store.db.executescript(OP_SCHEMA)
        self.activity = Activity(self.store)
        cfg = self.c.cfg
        self.factories = {f["id"]: f for f in cfg["factories"]}
        start = datetime.fromisoformat(cfg["start"]).timestamp()
        for mid, m in self.c.machines.items():
            self.base.register(mid, m["type"], learning_days=cfg.get("learning_days", 7), now=start)
            self.store.add_machine(mid, m["name"], m["factory"], PRESET.get(m["type"], "flour_mill"), m["type"],
                                   float(m.get("motor_hp", 5)), KIND.get(m["type"], "mill"))  # fmt: skip
        self.net = Network(probe=lambda: bool(self.live.link_up))
        self.transport = Transport(network=self.net)
        tools = ag.Tools(transport=self.transport, factories=self.factories,
                         service_hours=cfg.get("service_hours", {}))  # fmt: skip
        self.agent = ag.MaintenanceAgent(
            self.store, self.base, tools, clock=self.clock, activity=self.activity
        )
        self.cloud = Cloud(wd / "brain_cloud.db")
        self.sync = SyncClient(self.store, self.agent, self._cloud_send, node="cluster-node")
        self.health: dict[str, dict[str, Any]] = {}
        self.version = 0
        # the planner: one event-driven brain over the agent (Claude when keyed, the fixed plan otherwise)
        self.bus = EventBus(self.store, lock=self.lock)
        self.agent.emit_hook = self.bus.emit
        tech = cfg.get("technician", {})
        self.planner = Planner(self.store, self.agent, self.bus, client=planner_llm.make_client(),
                               factories=self.factories, health_of=self.health.get, live=live,
                               route_cfg={"start_xy": tech.get("xy_km", [0, 0]), "hours": tech.get("hours_per_day", 7),
                                          "kmph": cfg.get("road_kmph", 22)},
                               lock=self.lock, rules_reply=self._rules_reply, score_audio=self._score_audio)  # fmt: skip
        if self.planner.llm_on:
            self.agent.driver = "planner"
            self._stop = threading.Event()
            threading.Thread(target=self._planner_worker, name="planner", daemon=True).start()
        live.brain = self
        if backfill:
            self.backfill()
        for mid in self.c.machines:  # health from what is already stored, before the next reading arrives
            self.health[mid] = self.assess(mid)
        self.c.on_step.append(self.on_tick)

    # ------------------------------------------------------------------ planner plumbing
    def _planner_worker(self) -> None:
        """With an LLM: events are handled here, so a slow model call never holds the tick lock."""
        while not self._stop.is_set():
            self.bus.wake.wait(5.0)
            self.bus.wake.clear()
            with contextlib.suppress(Exception):
                if self.bus.drain():
                    self.version += 1
                    self.live.version += 1

    def ingest(self, type_: str, **kw: Any) -> dict[str, Any] | None:
        """An event from outside the tick (owner reply, voice note, mechanic). Without an LLM it is handled
        at once; with one the worker picks it up."""
        ev = self.bus.emit(type_, **kw)
        if ev is None:
            return None
        if self.planner.llm_on:
            return {"queued": ev.idem}
        return self.bus.handle(ev)

    def _rules_reply(self, ev: Event) -> dict[str, Any] | None:
        """The rules reply to the owner (owner_agent.run_rules: tools in a fixed order, templates)."""
        from . import owner_agent

        mid = ev.machine
        text = str(ev.payload.get("text") or (self.c.machines[mid]["name"] if mid in self.c.machines else ""))
        if not text:
            return None
        lang = str(ev.payload.get("lang") or owner_agent.detect_lang(text))
        return owner_agent.run_rules(self.live, text, ev.factory, lang)

    def _score_audio(self, mid: str, path: str) -> float | None:
        """A recording the owner sent back: scored against this machine's own normal (1.0 = its alarm)."""
        from edge import features

        model = self.base.model(mid)
        if model is None:
            return None
        audio = features.load(path)
        row = np.full(WIDTH, np.nan)
        row[: summary.DIM] = summary.summarise(audio)
        return float(np.asarray(model.risk(row[None, :]))[0])

    def _cloud_send(self, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.live.link_up:
            raise ConnectionError("link down (top-bar switch)")
        return self.cloud.handle(op, payload)

    # ------------------------------------------------------------------ the agents, per tick
    def backfill(self) -> int:
        """First start on an existing cluster: learn each machine's normal from its recorded history."""
        n = 0
        for mid in self.c.machines:
            if self.base.history(mid, last=1):
                continue
            rows = [h for h in self.c.history(mid) if h["mode"] != "ims"][-BACKFILL_TICKS:]
            for h in rows:
                with contextlib.suppress(Exception):
                    audio, _ = self.c.audio(mid, h["tick"], h["mode"], h["mix"])
                    self._observe(mid, h["tick"], audio, h["score"], run_agents=False)
                    n += 1
        if n:
            self.activity.record("memory", f"learn normal from {n} recorded readings (first start)", "completed",
                                 reasoning=f"{n} readings replayed into per-machine baselines", tools=["baseline.observe"])  # fmt: skip
        return n

    def _observe(self, mid: str, tick: int, audio: np.ndarray, ae_score: float | None,
                 run_agents: bool = True) -> dict[str, Any]:  # fmt: skip
        row = np.full(WIDTH, np.nan)
        q = summary.quality(audio)
        row[: summary.DIM] = summary.summarise(audio)
        row[AE_COL] = np.nan if ae_score is None else ae_score
        ts = self.c.now(tick).timestamp()
        obs = self.base.observe(mid, row, q, ts=ts, source="simulated", reading_id=f"{mid}@{tick}")
        for ev in obs.get("events") or []:
            if ev.get("kind") == "baseline_learned":
                self.activity.record("memory", "store the learned normal", "completed", machine=mid,
                                     reasoning=f"baseline v{ev['version']} from {ev['n']} readings",
                                     tools=["baseline.versions"])  # fmt: skip
            elif ev.get("kind") == "drift":
                self.activity.record("memory", "drift check", "completed", machine=mid,
                                     reasoning=f"drift {ev['drift']}: {ev['result']}", tools=["baseline.drift_check"])  # fmt: skip
        return obs

    def assess(self, mid: str) -> dict[str, Any]:
        hist = self.base.history(mid)
        cases = [c for c in self.store.cases(mid) if "repair" in c["context"]]
        if cases:  # after a repair, judge the machine on what it sounds like now
            at = max(c["context"]["repair"]["at"] for c in cases)
            hist = [h for h in hist if h["ts"] >= at] or hist[-1:]
        model = self.base.model(mid)
        last = hist[-1] if hist else None
        ex = None
        if model is not None and last and last["risk"] is not None:
            ex = model.explain(self.base.vectors([last["id"]])[0])
        t = [(h["ts"] - hist[0]["ts"]) / 86400 for h in hist] if hist else []
        est = rul.estimate(t, [h["risk"] if h["risk"] is not None else np.nan for h in hist], RISK_THRESHOLD,
                           window_days=3.0, gap_days=1.0) if hist else None  # fmt: skip
        noisy = False
        v = self.base.versions(mid)
        if ex and v and last:
            learned = self.base.vectors(v[0]["reading_ids"])
            row = self.base.vectors([last["id"]])[0]
            noisy = adapt.noisy_shed(10.0 * (row[S.mel_mean] - np.median(learned[:, S.mel_mean], axis=0)))
        reps = [{"what": r["work"], "date": datetime.fromtimestamp(r["done"]).date().isoformat(),
                 "result": r["outcome"]} for r in self.store.repairs(mid)]  # fmt: skip
        h = health.assess(mid, hist, now=self.c.now(), preset=PRESET.get(self.c.machines[mid]["type"], "flour_mill"),
                          explanation=ex, rul=est, baseline_rows=v[0]["n_rows"] if v else None, noisy_shed=noisy,
                          repairs=reps).as_dict()  # fmt: skip
        return h | {"explanation": ex}

    def on_tick(self, tick: int, out: dict[str, tuple[np.ndarray, dict[str, Any]]]) -> None:
        with self.lock:
            t0 = time.perf_counter()
            seen = []
            for mid, (audio, r) in out.items():
                if mid in self.c.ims:
                    continue  # Demo mode's IMS bearing is vibration; the classic screen handles it
                self._observe(mid, tick, audio, r.get("score"))
                seen.append(mid)
            changed = []
            for mid in self.c.machines:
                prev = self.health.get(mid, {}).get("urgency")
                self.health[mid] = self.assess(mid)
                h = self.health[mid]
                run, sensor = self.c.operating(mid)
                self.store.db.execute("INSERT OR REPLACE INTO op_log VALUES (?,?,?,?,?,?,?,?)",
                                      (tick, self.clock(), mid, run, sensor, int(mid in out), h["status"], h["urgency"]))  # fmt: skip
                if h["status"] not in ("learning", "no_signal", "not_running") and mid in out:
                    self.agent.observe(mid, h, reading_ref=f"{mid}@{tick}")
                if prev != h["urgency"]:
                    changed.append(f"{mid}: {prev or '-'} -> {h['urgency']}")
                    self._emit_health(tick, mid, prev, h)
            day = self.c.now().date().isoformat()
            if self.store.meta("planner_day") != day:
                self.store.meta("planner_day", day)
                self.bus.emit("daily_tick", idem=f"day:{day}", payload={"day": day,
                              "open_cases": len(self.store.cases(open_only=True))}, source="brain")  # fmt: skip
            self.store.db.commit()
            quiet = [m for m in self.c.machines if m not in out]
            self.activity.record("observation", f"tick {tick}: read {len(seen)} machines", "completed",
                                 reasoning=f"{len(seen)} readings scored against their own normal"
                                           + (f"; no reading from {', '.join(quiet)}" if quiet else ""),
                                 tools=["summary.quality", "summary.summarise", "baseline.observe"],
                                 running_status="monitoring")  # fmt: skip
            if changed:
                self.activity.record("intelligence", f"tick {tick}: reassess health", "completed",
                                     reasoning="; ".join(changed)[:400], tools=["health.assess", "detectors.explain"])  # fmt: skip
                est = {
                    m: (self.health[m].get("time_to_threshold") or {}).get("status") for m in self.c.machines
                }
                rising = [m for m, s in est.items() if s in ("estimate", "at_threshold")]
                self.activity.record("prediction", f"tick {tick}: time to threshold", "completed",
                                     reasoning=(f"rising trend on {', '.join(rising)}" if rising
                                                else "no rising trend with enough data"), tools=["rul.estimate"])  # fmt: skip
            self.agent.tick()
            self.sync.run()
            if not self.planner.llm_on:
                self.bus.drain()  # rules: cheap, deterministic, in the tick
            self.version += 1
            self.live.version += 1
            self.last_tick_ms = round((time.perf_counter() - t0) * 1000, 1)
        if self.planner.llm_on:
            self.bus.wake.set()

    def _emit_health(self, tick: int, mid: str, prev: str | None, h: dict[str, Any]) -> None:
        f = self.c.machines[mid]["factory"]
        case = self.store.open_case(mid)
        base = {"machine": mid, "factory": f, "case_id": case and case["id"], "source": "brain"}
        p = {"from": prev, "to": h["urgency"], "status": h["status"], "confidence": h.get("confidence"),
             "evidence": (h.get("evidence") or [])[:3]}  # fmt: skip
        self.bus.emit("health_change", idem=f"health:{mid}:{tick}", payload=p, **base)
        if ag.RANK.get(h["urgency"], 0) >= ag.RANK["inspect"] > ag.RANK.get(prev or "none", 0):
            self.bus.emit("alarm", idem=f"alarm:{mid}:{tick}", payload=p, **base)

    # ------------------------------------------------------------------ people
    def decide(self, approval_id: str, decision: str, by: str, channel: str) -> dict[str, Any]:
        with self.lock:
            if self.store.approval(approval_id) is None:
                raise KeyError(approval_id)
            out = self.agent.decide(
                approval_id, decision, by=by, channel=channel, ref=f"{channel}:{int(time.time())}"
            )
            a = self.store.approval(approval_id) or {}
            case = self.store.case(a["case_id"]) if a.get("case_id") else None
            self.ingest("owner_reply", case_id=a.get("case_id"), machine=case and case["machine"],
                        factory=case and self.c.machines.get(case["machine"], {}).get("factory"),
                        idem=f"decide:{approval_id}:{decision}:{channel}", source="owner",
                        payload={"approval_id": approval_id, "decision": decision, "channel": channel, "by": by})  # fmt: skip
            self.agent.tick()
            self.version += 1
            self.live.version += 1
            return out

    def repair_done(self, case_id: str, technician: str, work: str, simulate_recovery: bool) -> None:
        with self.lock:
            c = self.store.case(case_id)
            self.agent.repair_done(case_id, technician, work)
            if simulate_recovery:  # SIMULATED: the machine's recordings go back to its normal clips
                self.c.set_mode(c["machine"], "normal", 0.0)
                self.store.log("system", "simulated_recovery", case_id, machine=c["machine"],
                               note="after-repair readings are the unit's recorded normal clips; no real repair happened")  # fmt: skip
            self.agent.tick()
            self.version += 1
            self.live.version += 1

    # ------------------------------------------------------------------ what the command centre shows
    def machine_rows(self) -> list[dict[str, Any]]:
        rpm = self.c.cfg.get("nominal_rpm", {})
        out = []
        for mid, m in self.c.machines.items():
            h = self.health.get(mid) or {}
            hist = self.base.history(mid, last=1)
            last = hist[-1] if hist else None
            run, sensor = self.c.operating(mid)
            out.append({
                "id": mid, "name": m["name"], "factory": m["factory"], "type": m["type"], "shape": m["shape"],
                "sound_risk": None if not last or last["risk"] is None else round(last["risk"], 3),
                "temperature_c": None, "vibration": None,  # no such sensors on these machines
                "rpm": rpm.get(m["type"]), "rpm_basis": "nameplate (assumption)",
                "health": h.get("status", "no_signal"), "urgency": h.get("urgency"), "confidence": h.get("confidence"),
                "last_ts": datetime.fromtimestamp(last["ts"]).isoformat(timespec="minutes") if last else None,
                "run": run, "sensor": sensor, "source": last["source"] if last else None,
                "case": (self.store.open_case(mid) or {}).get("id"),
            })  # fmt: skip
        return out

    def recommendations(self) -> list[dict[str, Any]]:
        out = []
        for mid, h in self.health.items():
            if h.get("urgency") in (None, "none"):
                continue
            case = self.store.open_case(mid)
            pend = self.store.approvals(case["id"], status="requested") if case else []
            plan = (case or {}).get("context", {}).get("plan")
            out.append({"machine": mid, "name": self.c.machines[mid]["name"], "condition": h["status"],
                        "severity": h.get("severity"), "evidence": h.get("evidence", [])[:4],
                        "action": ACTION[h["urgency"]], "urgency": h["urgency"], "confidence": h.get("confidence"),
                        "confidence_reasons": h.get("confidence_reasons", []),
                        "approval_required": [a["kind"] for a in pend] or (["booking"] if case and "book_job" in case["plan"] else []),
                        "plan": plan, "case": case and case["id"], "by": "intelligence + planning agents",
                        "note": h.get("note")})  # fmt: skip
        return sorted(out, key=lambda r: -ag.RANK[r["urgency"]])

    def workflows(self) -> list[dict[str, Any]]:
        out = []
        for c in self.store.cases()[-12:]:
            steps = self.store.steps(c["id"])
            stages = []
            for name, members in STAGES:
                st = [s for s in steps if s["name"] in members]
                if name == "Observation":
                    stages.append(
                        {"stage": name, "status": "done", "at": c["opened"], "detail": "case opened"}
                    )
                    continue
                if not st:
                    stages.append({"stage": name, "status": "n/a", "at": None, "detail": None})
                    continue
                live_ = [s for s in st if s["status"] not in ("done", "skipped")]
                status = ("done" if not live_ and any(s["status"] == "done" for s in st) else
                          "skipped" if all(s["status"] == "skipped" for s in st) else live_[0]["status"] if live_ else "done")  # fmt: skip
                at = max((s["updated"] or 0) for s in st)
                detail = next(
                    (ag.summarise(s["name"], s["result"]) for s in reversed(st) if s["result"]), None
                )
                stages.append({"stage": name, "status": status, "at": at, "detail": detail,
                               "error": next((s["error"] for s in live_ if s["error"]), None)})  # fmt: skip
            out.append({"case": c["id"], "machine": c["machine"], "urgency": c["urgency"], "state": c["state"],
                        "technician": c["context"].get("technician"),
                        "outcome": c["outcome"], "opened": c["opened"], "closed": c["closed"], "stages": stages})  # fmt: skip
        return out[::-1]

    def performance(self) -> dict[str, Any]:
        """From the op log only: share of ticks each machine was reported running with a working sensor."""
        th = float(self.c.cfg["tick_hours"])
        rows = self.store.db.execute(
            "SELECT machine, COUNT(*) n, SUM(run='running') running, SUM(run='stopped') stopped,"
            " SUM(sensor='lost') lost, SUM(reading) readings, SUM(urgency NOT IN ('none')) flagged FROM op_log GROUP BY machine"
        ).fetchall()  # fmt: skip
        per = []
        for r in rows:
            n = r["n"] or 1
            per.append({"machine": r["machine"], "ticks": r["n"], "running_share": round(r["running"] / n, 3),
                        "downtime_h": r["stopped"] * th, "no_signal_h": r["lost"] * th,
                        "monitored_share": round(r["readings"] / n, 3), "flagged_share": round((r["flagged"] or 0) / n, 3),
                        "repairs": len(self.store.repairs(r["machine"])),
                        "cases": len(self.store.cases(r["machine"]))})  # fmt: skip
        days = self.store.db.execute(
            "SELECT CAST(ts/86400 AS INTEGER) d, AVG(run='running') running, AVG(urgency NOT IN ('none')) flagged,"
            " SUM(run='stopped') stopped FROM op_log GROUP BY d ORDER BY d"
        ).fetchall()  # fmt: skip
        trend = [{"day": datetime.fromtimestamp(r["d"] * 86400).date().isoformat(), "running_share": round(r["running"], 3),
                  "flagged_share": round(r["flagged"], 3), "downtime_h": r["stopped"] * th} for r in days]  # fmt: skip
        return {"per_machine": per, "trend": trend, "tick_hours": th,
                "note": "Measured only as 'reported running' per reading cycle (simulated stops). There is no "
                        "production counter, so availability, utilisation and OEE are not measured."}  # fmt: skip

    def trends(self, mid: str, last: int = 90) -> list[dict[str, Any]]:
        return [{"ts": datetime.fromtimestamp(h["ts"]).isoformat(timespec="minutes"), "risk": h["risk"],
                 "excluded": h["excluded"], "clean": h["clean"]} for h in self.base.history(mid, last=last)]  # fmt: skip

    def kpis(self) -> dict[str, Any]:
        rows = self.machine_rows()
        board = self.activity.board()
        status = [r["health"] for r in rows]
        healthy = sum(s == "normal" for s in status)
        return {"machines": len(rows), "healthy": healthy,
                "watch": sum(s == "watch" for s in status),
                "alerts": sum(s in ("inspect", "repair_soon", "urgent") for s in status),
                "learning": sum(s == "learning" for s in status),
                "offline": sum(r["run"] == "stopped" or r["sensor"] == "lost" for r in rows),
                "factory_health": f"{healthy} of {len(rows)} machines normal",
                "agents_active": sum(b["status"] in OPEN for b in board),
                "pending_approvals": len(self.store.approvals(status="requested")),
                "open_cases": len(self.store.cases(open_only=True)),
                "link_up": bool(self.live.link_up), "sync": self.sync.status(),
                "mode": "DEMO: simulated cluster replaying recorded sounds (MIMII / MIMII DUE)"}  # fmt: skip

    def state(self) -> dict[str, Any]:
        with self.lock:
            return {"version": self.version, "clock": self.c.now().isoformat(timespec="minutes"),
                    "kpis": self.kpis(), "machines": self.machine_rows(), "recommendations": self.recommendations(),
                    "agents": self.activity.board(), "timeline": self.activity.runs(60),
                    "workflows": self.workflows(), "approvals": self.store.approvals(),
                    "performance": self.performance(), "messages": self.transport.sent[-20:],
                    "queued": [{"kind": a["kind"], "attempts": a["attempts"], "error": a["error"]}
                               for a in self.store.queued_actions()],
                    "audit_ok": self.store.verify_audit()["ok"], "cloud_events": self.cloud.count("cluster-node"),
                    "planner": planner_trace(self.store, self.planner, limit=6),
                    "last_tick_ms": getattr(self, "last_tick_ms", None)}  # fmt: skip

    def planner_trace(
        self, case_id: str | None = None, machine: str | None = None, limit: int = 8
    ) -> dict[str, Any]:
        """Per case: event -> reasoning -> tool calls -> approvals -> outcome, with tokens and rupees
        (the planner's decisions table; what the command centre and the twin's agent dock show)."""
        with self.lock:
            return planner_trace(self.store, self.planner, case_id, machine, limit)

    def machine_detail(self, mid: str) -> dict[str, Any]:
        with self.lock:
            cases = self.store.cases(mid)
            return {"machine": next(r for r in self.machine_rows() if r["id"] == mid), "health": self.health.get(mid),
                    "trend": self.trends(mid), "baselines": [{k: v[k] for k in ("version", "reason", "status", "n_rows", "approval_ref")}
                                                             for v in self.base.versions(mid)],
                    "cases": [{k: c[k] for k in ("id", "urgency", "state", "outcome", "opened", "closed")} for c in cases],
                    "repairs": self.store.repairs(mid),
                    "agent_runs": [r for r in self.activity.runs(300) if r["machine"] == mid][:30]}  # fmt: skip


def planner_trace(store: Store, planner: Planner, case_id: str | None = None, machine: str | None = None,
                  limit: int = 8) -> dict[str, Any]:  # fmt: skip
    """The planner's decisions grouped by case, newest cases first."""
    rows = store.decisions(case_id=case_id, limit=400)
    if machine:
        rows = [d for d in rows if d["machine"] == machine]
    by: dict[str, list[dict[str, Any]]] = {}
    for d in rows:
        by.setdefault(d["case_id"] or "-", []).append(d)
    order = sorted(by, key=lambda k: max(x["id"] for x in by[k]), reverse=True)[:limit]
    cases = []
    for cid in order:
        ds = by[cid]
        steps = []
        for d in ds:
            ev = store.event(d["event"]) or {}
            steps.append({"decision": d["id"], "at": d["started"], "event": d["event_type"], "source": ev.get("source"),
                          "said": str((ev.get("payload") or {}).get("text") or "")[:200],
                          "engine": d["engine"], "model": d["model"], "status": d["status"],
                          "reasoning": (d["reasoning"] or "")[:600],
                          "tools": [{"tool": c["tool"], "ok": c["ok"], "input": c["input"],
                                     "summary": str((c["result"] or {}).get("summary") or (c["result"] or {}).get("error") or "")[:200]}
                                    for c in d["tool_calls"] or []],
                          "reply": d["reply"], "outcome": d["outcome"],
                          "grounding_ok": (d["grounding"] or {}).get("ok", True),
                          "input_tokens": d["input_tokens"], "output_tokens": d["output_tokens"],
                          "cost_inr": d["cost_inr"], "latency_ms": d["latency_ms"]})  # fmt: skip
        c = store.case(cid) if cid != "-" else None
        cases.append({"case": cid, "machine": (c or {}).get("machine") or ds[-1]["machine"],
                      "state": (c or {}).get("state"), "driver": (c or {}).get("context", {}).get("driver"),
                      "approvals": [{k: a[k] for k in ("id", "kind", "status", "decided_by", "channel")}
                                    for a in store.approvals(cid)] if c else [],
                      "actions": [dict(r) for r in store.db.execute(
                          "SELECT kind, status, idem FROM actions WHERE case_id=? ORDER BY created", (cid,))] if c else [],
                      "steps": steps, "tokens": sum(s["input_tokens"] + s["output_tokens"] for s in steps),
                      "cost_inr": round(sum(s["cost_inr"] for s in steps), 4)})  # fmt: skip
    return {"llm": planner.llm_on, "models": {t: planner_llm.model_for(t)[0] for t in planner_llm.TIERS},
            "pending_events": len(store.events(status="pending")), "cases": cases,
            "note": "Claude chooses the tool calls" if planner.llm_on
            else "no ANTHROPIC_API_KEY: the fixed plan runs (rules); decisions are still logged"}  # fmt: skip


ACTION = {
    "monitor": "Keep running; watch the next readings (in today's digest).",
    "measure_again": "Record it again for 60 s before anyone is sent.",
    "inspect": "Book an inspection visit.",
    "maintenance": "Plan a repair in the next day.",
    "urgent_owner_attention": "Owner's attention today; whether to stop it is the owner's decision.",
}
STAGES = [
    ("Observation", []),
    ("Investigation", ["recall"]),
    ("Risk assessment", ["request_measurement", "notify_watch", "await_change", "notify_owner_urgent"]),
    ("Maintenance planning", ["plan_inspection", "planner"]),
    ("Technician discovery", ["find_technician", "check_parts", "estimate_cost"]),
    ("Owner approval", ["request_approval", "await_approval"]),
    ("Repair", ["book_job", "await_repair"]),
    ("Verification", ["request_post_repair_readings", "verify_repair"]),
]
