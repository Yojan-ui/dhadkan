"""The end-to-end demonstration: one small flour mill, one chakki, from learning to a verified repair.

    python -m server.flourmill --pack     build edge/models/flourmill_pack.npz from the recordings (data/raw)
    python -m server.flourmill --auto     run the whole story headless; writes docs/runs/flourmill_<UTC>.json
    (the dashboard drives the same class step by step: /api/mill/*, web ?view=mill)

What is real and what is not (shown on screen):
  RECORDED   every reading is a real 10 s recording of MIMII DUE gearbox section 01 (DCASE 2021 Task 2
             development set): normal training clips while learning and running normally, the recorded
             ANOMALOUS test clips when the fault appears. We picked section 01 because loudness separates
             its faults (docs/runs/compare_*.json, AUC 0.998 on its test clips); on section 00 it does not
             (AUC 0.51). A chakki is not a gearbox: this is a stand-in recording of a geared drive.
  DEMO DATA  the mill, its owner, the technicians (data/demo/technicians.json) and dealer stock (MOCK).
  SIMULATED  the clock (one reading every 3 hours), the network cut, the technician's visit, and the
             post-repair readings, which are the same unit's held-out NORMAL test recordings: no real
             repair happened. Prices are cited typical prices from server/faults.yaml.
"""

import argparse
import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from edge import adapt, data, features, rul, summary
from edge.baseline import BaselineStore
from edge.config import MODELS, ROOT, RUNS
from edge.detectors import WIDTH
from edge.summary import S

from . import health
from .maint import agent as ag
from .maint.store import Store
from .maint.sync import Cloud, SyncClient
from .maint.tools import Network, Transport

PACK = MODELS / "flourmill_pack.npz"
UNIT = "gearbox/section_01"
MACHINE = "chakki-1"
START = datetime(2026, 11, 16, 6, 0)  # a Monday, 6 am
STEP_H = 3.0
LEARNING_DAYS = 3  # the spec allows 3-7; the demo uses the short end
SNIPPET_S = 5.0
RISK_THRESHOLD = health.ASSUMPTIONS["severe"]  # time-to-threshold target: the "severe" level (assumption)

# The script: (phase, how many readings, which recordings). Network and repair events are by reading index.
SCRIPT = [("learning", 24, "train"), ("normal", 12, "train_rest"), ("fault", 10, "anomaly"),
          ("after_repair", 8, "test_normal")]  # fmt: skip
NETWORK_DOWN_AT, NETWORK_UP_AT = 38, 43  # reading indices (simulated network cut)


def build_pack() -> Path:
    clips = [c for c in data.gearbox() if c.unit == UNIT]
    train = [c for c in clips if c.split in ("train", "val")]
    test = [c for c in clips if c.split == "test" and c.condition == "source"]
    pools = {"train": train[:24], "train_rest": train[24:36], "anomaly": [c for c in test if c.label == 1][:10],
             "test_normal": [c for c in test if c.label == 0][:8]}  # fmt: skip
    rows, quals, names, phases = [], [], [], []
    for phase, n, pool in SCRIPT:
        for c in pools[pool][:n]:
            a = features.load(c.path)
            r = np.full(WIDTH, np.nan)
            r[: summary.DIM] = summary.summarise(a)
            rows.append(r)
            quals.append(json.dumps(summary.quality(a)))
            names.append(c.path.name)
            phases.append(phase)
    before = features.load(pools["train"][0].path)[: int(SNIPPET_S * features.SR)]
    now = features.load(pools["anomaly"][-1].path)[: int(SNIPPET_S * features.SR)]
    after = features.load(pools["test_normal"][0].path)[: int(SNIPPET_S * features.SR)]
    to16 = lambda x: np.clip(x * 32767, -32768, 32767).astype(np.int16)  # noqa: E731
    np.savez_compressed(PACK, rows=np.vstack(rows), quality=np.array(quals), clips=np.array(names),
                        phases=np.array(phases), before=to16(before), now=to16(now), after=to16(after),
                        provenance=np.array(json.dumps({"unit": UNIT, "dataset": "MIMII DUE (DCASE 2021 Task 2 dev)",
                                                        "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})))  # fmt: skip
    return PACK


@dataclass
class Clock:
    t: float

    def __call__(self) -> float:
        return self.t


class FlourMillDemo:
    def __init__(self, workdir: Path, auto_owner: bool = False) -> None:
        workdir.mkdir(parents=True, exist_ok=True)
        for f in ("edge.db", "maint.db", "cloud.db"):
            (workdir / f).unlink(missing_ok=True)
        p = np.load(PACK)
        self.rows, self.quality = p["rows"], [json.loads(q) for q in p["quality"]]
        self.clips, self.phases = [str(c) for c in p["clips"]], [str(x) for x in p["phases"]]
        self.audio = {k: p[k] for k in ("before", "now", "after")}
        self.provenance = json.loads(str(p["provenance"]))
        self.clock = Clock(START.timestamp())
        self.i = 0
        self.auto_owner = auto_owner
        self.base = BaselineStore(workdir / "edge.db")
        self.base.register(MACHINE, "gearbox", learning_days=LEARNING_DAYS, now=self.clock.t)
        self.store = Store(workdir / "maint.db", clock=self.clock)
        self.store.add_machine(MACHINE, "Chakki 1", "Shree Ganesh Aata Chakki (demo data)", "flour_mill", "gearbox",
                               10, "mill")  # fmt: skip
        self.net = Network(online=True)
        self.transport = Transport(network=self.net)
        self.agent = ag.MaintenanceAgent(
            self.store, self.base, ag.Tools(transport=self.transport), clock=self.clock
        )
        self.cloud = Cloud(workdir / "cloud.db")
        self.cloud_up = True
        self.sync = SyncClient(self.store, self.agent, self._cloud_send)
        self.timeline: list[dict[str, Any]] = []
        self.last_health: dict[str, Any] | None = None
        self.events: list[dict[str, Any]] = []

    def close(self) -> None:
        """Close the SQLite files so a reset can delete them (Windows refuses to delete an open file)."""
        for db in (self.base.db, self.store.db, self.cloud.db):
            db.close()

    def _cloud_send(self, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.net.online:
            raise ConnectionError("network down (simulated)")
        return self.cloud.handle(op, payload)

    @property
    def done(self) -> bool:
        return self.i >= len(self.rows) or self.declined

    @property
    def repaired(self) -> bool:
        return any("repair" in c["context"] for c in self.store.cases(MACHINE))

    @property
    def declined(self) -> bool:
        return any(a["kind"] == "booking" and a["status"] == "rejected" for a in self.store.approvals())

    @property
    def waiting_for(self) -> str | None:
        if self.i < len(self.rows) and self.phases[self.i] == "after_repair" and not self.repaired:
            return "the owner's decision on the inspection visit"
        return None

    def set_network(self, online: bool, why: str = "demo switch") -> None:
        self.net.online = online
        self.store.log("system", "network_up" if online else "network_down", None, why=why)
        self.events.append({"i": self.i, "kind": "network_up" if online else "network_down"})

    def _band_delta_db(self, row: np.ndarray) -> np.ndarray:
        v = self.base.versions(MACHINE)
        if not v:
            return np.zeros(64)
        learned = self.base.vectors(v[0]["reading_ids"])
        return 10.0 * (row[S.mel_mean] - np.median(learned[:, S.mel_mean], axis=0))

    def step(self) -> dict[str, Any]:
        """One reading: score against this chakki's own normal, assess, let the agent work, sync."""
        if self.done:
            return self.state()
        i = self.i
        if i == NETWORK_DOWN_AT:
            self.set_network(False, "scripted network cut")
        if i == NETWORK_UP_AT:
            self.set_network(True, "scripted network back")
        phase = self.phases[i]
        if phase == "after_repair":
            self._ensure_repaired()
            if not self.repaired:
                self.agent.tick()
                self.sync.run()
                return self.state()  # no visit without the owner's yes: the story waits here
        row = self.rows[i]
        obs = self.base.observe(MACHINE, row, self.quality[i], ts=self.clock.t, source="simulated",
                                reading_id=f"{self.clips[i]}@{i}")  # fmt: skip
        hist = self.base.history(MACHINE)
        reps = self.store.repairs(MACHINE)
        fixed_at = [
            c["context"]["repair"]["at"] for c in self.store.cases(MACHINE) if "repair" in c["context"]
        ]
        if fixed_at:  # after a repair, judge the machine on what it sounds like now
            hist = [h for h in hist if h["ts"] >= max(fixed_at)] or hist[-1:]
        model = self.base.model(MACHINE)
        ex = model.explain(row) if model is not None and obs.get("risk") is not None else None
        t = [(h["ts"] - hist[0]["ts"]) / 86400 for h in hist]
        est = rul.estimate(t, [h["risk"] if h["risk"] is not None else np.nan for h in hist], RISK_THRESHOLD,
                           window_days=1.0, gap_days=0.5)  # fmt: skip
        v = self.base.versions(MACHINE)
        h = health.assess(MACHINE, hist, now=datetime.fromtimestamp(self.clock.t), preset="flour_mill",
                          explanation=ex, rul=est, baseline_rows=v[0]["n_rows"] if v else None,
                          noisy_shed=bool(ex) and adapt.noisy_shed(self._band_delta_db(row)),
                          repairs=[{"what": r["work"], "date": datetime.fromtimestamp(r["done"]).date().isoformat(),
                                    "result": r["outcome"]} for r in reps])  # fmt: skip
        self.last_health = h.as_dict() | {"explanation": ex}
        if h.status not in ("learning", "no_signal", "not_running"):
            self.agent.observe(MACHINE, h.as_dict(), reading_ref=obs.get("id"))
        self.agent.tick()
        if self.auto_owner:
            self._auto_owner()
        sync = self.sync.run()
        self.timeline.append({"i": i, "ts": datetime.fromtimestamp(self.clock.t).isoformat(timespec="minutes"),
                              "phase": phase, "clip": self.clips[i], "risk": obs.get("risk"), "state": obs.get("state"),
                              "clean": obs.get("clean"), "status": h.status, "urgency": h.urgency,
                              "online": self.net.online, "events_waiting": sync["events_waiting"]})  # fmt: skip
        self.i += 1
        self.clock.t += STEP_H * 3600
        return self.state()

    def _ensure_repaired(self) -> None:
        """Before the post-repair readings: the technician's visit happened (simulated)."""
        case = self.store.open_case(MACHINE)
        if case is None or "repair" in case["context"]:
            return
        aid = f"{case['id']}:booking"
        if self.store.approval(aid) and self.store.approval(aid)["status"] == "requested":  # type: ignore[index]
            return  # the owner has not answered: no visit, the repair cannot happen
        self.agent.tick()
        steps = {s["name"]: s for s in self.store.steps(case["id"])}
        if steps.get("book_job", {}).get("status") == "done":
            tech, day = steps["book_job"]["result"]["technician"], steps["book_job"]["result"]["day"]
            visit = datetime.fromisoformat(day).replace(hour=10).timestamp()
            if self.clock.t < visit:  # SIMULATED: the clock moves on to the booked visit
                self.store.log("system", "clock_moved_to_visit", None, day=day)
                self.clock.t = visit
            self.agent.repair_done(
                case["id"], technician=tech, work="inspection; bearing replaced (simulated visit)"
            )
            self.events.append({"i": self.i, "kind": "repair_done", "technician": tech})
            self.agent.tick()

    def _auto_owner(self) -> None:
        for a in self.store.approvals(status="requested"):
            if a["kind"] == "booking":
                self.decide(a["id"], "approved", channel="auto-owner (headless demo)")

    def decide(self, approval_id: str, decision: str, channel: str = "owner screen") -> dict[str, Any]:
        out = self.agent.decide(approval_id, decision, by="owner", channel=channel, ref=f"{channel}@{self.i}")
        self.events.append({"i": self.i, "kind": f"owner_{decision}", "approval": approval_id})
        self.agent.tick()
        return out

    def run(self) -> dict[str, Any]:
        while not self.done:
            before = self.i
            self.step()
            if self.i == before:  # waiting for the owner (only when nobody answers): stop here
                break
        for _ in range(3):  # let the last steps finish
            self.clock.t += STEP_H * 3600
            self.agent.tick()
            self.sync.run()
        return self.state()

    # ------------------------------------------------------------------ what the dashboard shows
    def state(self) -> dict[str, Any]:
        cases = self.store.cases(MACHINE)
        case = cases[-1] if cases else None
        steps = self.store.steps(case["id"]) if case else []
        versions = self.base.versions(MACHINE)
        return {
            "machine": {"id": MACHINE, **self.store.machine(MACHINE), "learning_days": LEARNING_DAYS},
            "provenance": {
                "readings": f"RECORDED: {self.provenance['dataset']}, {UNIT}",
                "people_and_stock": "DEMO DATA (invented mill, owner, technicians; MOCK dealer stock)",
                "simulated": "clock (3 h per reading), network cut, technician visit, post-repair readings "
                "(the unit's own held-out normal recordings; no real repair happened)",
            },
            "i": self.i,
            "n": len(self.rows),
            "done": self.done,
            "now": datetime.fromtimestamp(self.clock.t).isoformat(timespec="minutes"),
            "phase": self.phases[min(self.i, len(self.rows) - 1)],
            "waiting_for": self.waiting_for,
            "declined": self.declined,
            "health": self.last_health,
            "timeline": self.timeline,
            "events": self.events,
            "baseline": [
                {k: b[k] for k in ("version", "reason", "status", "n_rows", "created", "approval_ref")}
                for b in versions
            ],
            "case": case
            and {k: case[k] for k in ("id", "state", "urgency", "severity", "outcome", "opened", "closed")},
            "steps": [
                {k: s[k] for k in ("idx", "name", "status", "attempts", "error", "result")} for s in steps
            ],
            "approvals": self.store.approvals(case["id"]) if case else [],
            "messages": [
                {
                    "kind": x["kind"],
                    "to": x.get("to"),
                    "text": x.get("text"),
                    "technician": x.get("technician"),
                    "day": x.get("day"),
                }
                for x in self.transport.sent
            ],
            "queued": [
                {"kind": a["kind"], "attempts": a["attempts"], "error": a["error"]}
                for a in self.store.queued_actions()
            ],
            "audit": self.store.audit()[-40:],
            "audit_ok": self.store.verify_audit()["ok"],
            "sync": self.sync.status()
            | {
                "network": "online" if self.net.online else "offline",
                "cloud_events": self.cloud.count("factory-node"),
            },
            "repairs": self.store.repairs(MACHINE),
            "costs": _downtime_at_risk(),
        }


def _downtime_at_risk() -> dict[str, Any]:
    c = json.loads((ROOT / "data" / "sim" / "costs.json").read_text(encoding="utf-8"))
    hours = c["unplanned_downtime_hours"]["gearbox"]
    out_h = c["output_per_hour_inr"]["A"]
    return {"unplanned_downtime_hours": hours, "output_per_hour_inr": out_h, "at_risk_inr": hours * out_h,
            "note": "ASSUMPTIONS (data/sim/costs.json), not measurements: a mill enters its own numbers"}  # fmt: skip


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", action="store_true")
    ap.add_argument("--auto", action="store_true")
    args = ap.parse_args()
    if args.pack:
        print(build_pack())
        return
    demo = FlourMillDemo(ROOT / "data" / "demo_run", auto_owner=args.auto)
    st = demo.run()
    for row in st["timeline"]:
        r = row["risk"]
        print(f"{row['i']:3d} {row['ts']} {row['phase']:12s} risk={'-' if r is None else f'{r:5.2f}'} "
              f"{row['status']:12s} {row['urgency']:22s} {'online' if row['online'] else 'OFFLINE'} "
              f"waiting={row['events_waiting']}")  # fmt: skip
    print("case:", st["case"])
    for s in st["steps"]:
        print(f"  {s['name']:30s} {s['status']}")
    print("repairs:", st["repairs"])
    if args.auto:
        rec = {"run_id": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), "provenance": st["provenance"],
               "timeline": st["timeline"], "case": st["case"], "steps": st["steps"], "approvals": st["approvals"],
               "repairs": st["repairs"], "audit_ok": st["audit_ok"], "sync": st["sync"], "messages": st["messages"],
               "owner": "auto-approved (headless run)"}  # fmt: skip
        out = RUNS / f"flourmill_{rec['run_id']}.json"
        out.write_text(json.dumps(rec, indent=1, default=str), encoding="utf-8")
        print(out)


if __name__ == "__main__":
    main()
