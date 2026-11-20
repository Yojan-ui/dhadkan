"""Does the agent's workflow hold up when things go wrong? Randomised fault-injection runs.

    python -m server.maint.evaluate [--runs 200]   -> docs/runs/agent_<UTC>.json

Each run: one machine with a learned baseline (synthetic summary rows, NOT field data), an anomaly that
opens a case, and a random mix of: network cuts, provider errors, a technician who declines, all
technicians declining, a crash and restart mid-step, repeated observations, the owner answering late,
not at all, or with conflicting answers on two channels, then post-repair readings. The clock is
simulated. Measured on the agent's own records (audit log, actions, approvals), never on its say-so:

  completion_rate        runs whose owner approved and whose case ended verified (repair_*)
  approval_violations    paid actions delivered while their approval (same case, right kind) was not
                         'approved' at that moment, read from the database at delivery (must be 0)
  duplicate_sends        outward actions delivered more than once (must be 0)
  recovered_failures     actions that failed at first (network/provider) and were delivered later
  undelivered            actions still queued at the end although the network was back
  untraced_actions       delivered actions without an action_done audit row (must be 0)
  audit_intact           hash chain verified, and the cloud replica holds every event after sync
"""

import argparse
import json
import random
import tempfile
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from edge.baseline import BaselineStore
from edge.detectors import AE_COL, WIDTH
from edge.summary import S

from . import agent as ag
from .store import Store
from .sync import Cloud, SyncClient
from .tools import Network, Transport

T0 = datetime(2026, 11, 16, 9, 0).timestamp()
H = 3600.0
OK = {"ok": True, "reasons": []}


def _rows(n: int, rng: np.random.Generator, fault: float = 0.0) -> np.ndarray:
    base = np.zeros(WIDTH)
    base[S.log_rms] = -2.0
    base[S.mel_mean] = np.linspace(-3, -6, 64)
    base[S.mel_std] = 0.3
    base[S.mfcc_mean] = np.linspace(5, -5, 20)
    base[S.mfcc_std] = 1.0
    base[S.centroid], base[S.flatness], base[AE_COL] = 20.0, 0.3, 1.0
    x = base + rng.normal(0, 0.05, size=(n, WIDTH))
    x[:, S.log_rms] += 0.3 * fault
    x[:, S.mel_mean.start + 40 : S.mel_mean.start + 48] += 1.5 * fault
    x[:, S.mel_p95] = x[:, S.mel_mean] + 0.5
    return x


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> float:
        return self.t


def one_run(seed: int, workdir: Path) -> dict[str, Any]:
    rnd, rng = random.Random(seed), np.random.default_rng(seed)
    clock = Clock()
    b = BaselineStore(workdir / "edge.db", detectors=["rms", "spectral"])
    b.register("m", "gearbox", learning_days=2, now=T0 - 4 * 86400)
    for i, r in enumerate(_rows(40, rng)):
        b.observe("m", r, OK, ts=T0 - 4 * 86400 + i * 1.5 * H, source="simulated")
    for i, r in enumerate(_rows(4, rng, 1.0)):
        b.observe("m", r, OK, ts=T0 - 4 * H + i * H, source="simulated")
    s = Store(workdir / "maint.db", clock=clock)
    s.add_machine("m", "Chakki 1", "eval", "flour_mill", "gearbox", 10, "mill")
    net = Network(online=True)
    scenario = {
        "network_cuts": rnd.randint(0, 3),
        "provider_errors": rnd.choice([0, 0, 1, 3]),
        "decline": rnd.choice([set(), set(), {"T1"}, {"T1", "T2", "T3"}]),
        "crash": rnd.random() < 0.3,
        "owner": rnd.choice(["prompt", "prompt", "late", "never", "conflict", "reject"]),
        "repeat_obs": rnd.randint(1, 4),
        "urgency": rnd.choice(["inspect", "maintenance", "urgent_owner_attention"]),
    }
    import sqlite3

    at_send: list[dict[str, Any]] = []  # approval state at the moment each paid action went out

    def deliver(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        if kind in ag.GATED:
            row = sqlite3.connect(workdir / "maint.db").execute(
                "SELECT status, kind, case_id FROM approvals WHERE id=?", (payload.get("approval"),)).fetchone()  # fmt: skip
            at_send.append({"kind": kind, "approval": row})
        return {"id": f"{kind}:{payload['idem'][:10]}"}

    tr = Transport(
        send=deliver, network=net, fail_next=scenario["provider_errors"], decline=scenario["decline"]
    )
    tools = ag.Tools(transport=tr)
    a = ag.MaintenanceAgent(s, b, tools, clock=clock)
    cloud = Cloud()

    def send(op: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not net.online:
            raise ConnectionError("down")
        return cloud.handle(op, payload)

    sync = SyncClient(s, a, send)
    h = {
        "urgency": scenario["urgency"],
        "severity": "marked",
        "recent_risk": [2.5, 2.7, 3.0],
        "confidence": "high",
    }
    case = a.observe("m", h)
    for _ in range(scenario["repeat_obs"]):
        a.observe("m", h)
    cuts = (
        sorted(rnd.sample(range(2, 30), k=scenario["network_cuts"] * 2)) if scenario["network_cuts"] else []
    )
    approved_at = {"prompt": 2, "late": 12, "conflict": 3, "reject": 2, "never": None}[scenario["owner"]]
    aid = f"{case['id']}:booking"
    crashed = False
    for k in range(40):
        net.online = sum(c <= k for c in cuts) % 2 == 0
        if scenario["crash"] and not crashed and k == 6:
            a = ag.MaintenanceAgent(
                Store(workdir / "maint.db", clock=clock), b, tools, clock=clock
            )  # restart
            sync = SyncClient(a.s, a, send)
            crashed = True
        a.tick()
        sync.run()
        if approved_at is not None and k == approved_at and a.s.approval(aid) is not None:
            if scenario["owner"] == "reject":
                a.decide(aid, "rejected", "owner", "whatsapp", f"w{seed}")
            elif scenario["owner"] == "conflict":
                a.decide(aid, "approved", "owner", "app", f"tap{seed}")
                cloud.add_decision(
                    f"d{seed}", "factory-node", aid, "rejected", "owner", "whatsapp", f"w{seed}", clock.t
                )
            else:
                a.decide(aid, "approved", "owner", "whatsapp", f"w{seed}")
        if (
            scenario["owner"] == "conflict"
            and k == 8
            and a.s.approval(aid)
            and a.s.approval(aid)["status"] == "requested"
        ):
            a.decide(aid, "approved", "owner", "whatsapp", f"again{seed}")
        c = a.s.case(case["id"])
        steps = {x["name"]: x for x in a.s.steps(case["id"])}
        if steps.get("book_job", {}).get("status") == "done" and "repair" not in c["context"]:
            a.repair_done(case["id"], technician=steps["book_job"]["result"]["technician"], work="bearing")
            for i, r in enumerate(_rows(6, rng)):
                b.observe("m", r, OK, ts=clock.t + (i + 1) * 600, source="simulated")
        clock.t += 3 * H
    net.online = True
    for _ in range(4):
        clock.t += 6 * H
        a.tick()
        sync.run()

    st = a.s
    sent = Counter(x["idem"] for x in tr.sent)
    approvals = {x["id"]: x for x in st.approvals()}
    violations = sum(1 for x in at_send if not x["approval"] or x["approval"][0] != "approved"
                     or x["approval"][1] != ag.GATED[x["kind"]] or x["approval"][2] != case["id"])  # fmt: skip
    actions = [dict(r) for r in st.db.execute("SELECT * FROM actions")]
    done_audit = Counter(
        (e["detail"].get("kind"), e["case_id"]) for e in st.audit() if e["event"] == "action_done"
    )
    final = st.case(case["id"])
    return {
        "seed": seed,
        "scenario": {**scenario, "decline": sorted(scenario["decline"])},
        "outcome": final["outcome"] or final["state"],
        "owner_approved": any(
            x["kind"] == "booking" and x["status"] == "approved" for x in approvals.values()
        ),
        "approval_violations": violations,
        "duplicate_sends": sum(n - 1 for n in sent.values() if n > 1),
        "recovered_failures": sum(1 for x in actions if x["status"] == "done" and x["attempts"] > 0),
        "undelivered": sum(1 for x in actions if x["status"] == "queued"),
        "refused": sum(1 for x in actions if x["status"] == "failed"),
        "untraced_actions": max(
            0, sum(1 for x in actions if x["status"] == "done") - sum(done_audit.values())
        ),
        "audit_intact": st.verify_audit()["ok"] and cloud.count("factory-node") == len(st.audit()),
        "booked_without_any_technician": bool(
            scenario["decline"] == {"T1", "T2", "T3"} and any(x["kind"] == "book_job" for x in tr.sent)
        ),
    }


def main() -> None:
    from edge.config import RUNS
    from edge.train import _code_hash, _git_rev

    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=200)
    args = ap.parse_args()
    t0 = time.time()
    runs = []
    for seed in range(args.runs):
        with tempfile.TemporaryDirectory() as d:
            runs.append(one_run(seed, Path(d)))
    approved = [r for r in runs if r["owner_approved"]]
    feasible = [r for r in approved if r["scenario"]["decline"] != ["T1", "T2", "T3"]]
    summary = {
        "runs": len(runs),
        "approved_runs": len(approved),
        "completion_rate_when_approved_and_a_technician_exists": round(
            float(np.mean([r["outcome"].startswith("repair_") for r in feasible])) if feasible else 0.0, 3),
        "approval_violations": sum(r["approval_violations"] for r in runs),
        "duplicate_sends": sum(r["duplicate_sends"] for r in runs),
        "recovered_failures": sum(r["recovered_failures"] for r in runs),
        "undelivered_at_end": sum(r["undelivered"] for r in runs),
        "untraced_actions": sum(r["untraced_actions"] for r in runs),
        "audit_intact_runs": sum(r["audit_intact"] for r in runs),
        "booked_without_any_technician": sum(r["booked_without_any_technician"] for r in runs),
        "outcomes": dict(Counter(r["outcome"] for r in runs)),
        "outcomes_by_owner_behaviour": {
            o: dict(Counter(r["outcome"] for r in runs if r["scenario"]["owner"] == o))
            for o in ("prompt", "late", "never", "conflict", "reject")
        },
    }  # fmt: skip
    rec = {"run_id": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), "git_rev": _git_rev(), "code_sha256": _code_hash(),
           "note": "synthetic machine rows and a simulated clock; this measures the workflow, not detection",
           "summary": summary, "runs": runs, "seconds": round(time.time() - t0, 1)}  # fmt: skip
    out = RUNS / f"agent_{rec['run_id']}.json"
    out.write_text(json.dumps(rec, indent=1, default=str), encoding="utf-8")
    print(out)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
