"""The named agents and what each one did: a persistent activity log beside the orchestrator's store.

Agents and their jobs (the code that does the work is named in WHO):

    observation    every reading: quality check, summary, this machine's learned normal  (edge/summary, edge/baseline)
    memory         machine history: baselines, earlier cases, repairs and their outcomes  (edge/baseline, maint/store)
    intelligence   is the change persistent, how sure, what changed                     (server/health, detectors.explain)
    prediction     time to the maintenance threshold, or "not enough data"              (edge/rul)
    planning       inspection plan, maintenance window, downtime, priority across cases (maint/agent _step_plan_inspection)
    technician     technicians, parts stock, cited cost ranges                          (maint/tools)
    execution      owner questions, approvals, bookings, messages; sent once, queued offline (maint/agent act/_send)
    verification   post-repair readings against the ORIGINAL normal (baseline v1)       (maint/agent _step_verify_repair)
    orchestrator   opens cases, assigns steps to agents, retries, resumes after restarts (maint/agent MaintenanceAgent)

These are deterministic modules with agent names: their one-line reasoning is written by code. The
decisions an LLM makes (which tools to call, in what order, and why) are made by server/planner and
kept in the store's decisions table; the "planner" step here only marks that a case is driven by it.

Every run is a row: agent, task, machine/case, status, the observation it started from, a one-line
reasoning summary (written by the deterministic code, not an LLM), the tools it called, its result,
start/end, and its retries. Runs are also written to the hash-chained audit log, so they survive
restarts and can be checked for tampering.

Statuses: monitoring, investigating, analysing, planning, executing, awaiting_approval, verifying,
completed, failed, retrying.
"""

import json
from typing import Any

AGENTS = {
    "observation": "Observation",
    "memory": "Memory",
    "intelligence": "Intelligence",
    "prediction": "Prediction",
    "planning": "Planning",
    "technician": "Technician & Procurement",
    "execution": "Execution",
    "verification": "Verification",
    "orchestrator": "Orchestrator",
}
WHO = {
    "observation": "edge/summary.py, edge/baseline.py",
    "memory": "edge/baseline.py, server/maint/store.py",
    "intelligence": "server/health.py, edge/detectors.py",
    "prediction": "edge/rul.py",
    "planning": "server/maint/agent.py (plan_inspection)",
    "technician": "server/maint/tools.py",
    "execution": "server/maint/agent.py (act, approvals, bookings)",
    "verification": "server/maint/agent.py (verify_repair), server/prove.py",
    "orchestrator": "server/maint/agent.py (MaintenanceAgent)",
}
STATUSES = ("monitoring", "investigating", "analysing", "planning", "executing", "awaiting_approval",
            "verifying", "completed", "failed", "retrying")  # fmt: skip
OPEN = ("monitoring", "investigating", "analysing", "planning", "executing", "awaiting_approval", "verifying",
        "retrying")  # fmt: skip

# orchestrator step -> (agent, status while it runs, tools it calls)
STEP_AGENT: dict[str, tuple[str, str, list[str]]] = {
    "recall": ("memory", "analysing", ["repairs.history", "cases.history", "baseline.versions"]),
    "plan_inspection": ("planning", "planning", ["factory.schedule", "cases.open", "service_hours"]),
    "planner": ("planning", "planning", ["server/planner (Granite, gated tools)"]),
    "request_measurement": ("execution", "executing", ["notify.owner"]),
    "notify_watch": ("execution", "executing", ["notify.owner(digest)"]),
    "await_change": ("observation", "monitoring", ["baseline.observe"]),
    "notify_owner_urgent": ("execution", "executing", ["notify.owner", "approvals.request(production_stop)"]),
    "find_technician": ("technician", "investigating", ["technicians.find", "repairs.history"]),
    "check_parts": ("technician", "investigating", ["parts.stock", "presets.faults"]),
    "estimate_cost": ("technician", "analysing", ["faults.yaml cost_range", "inspection_cost"]),
    "request_approval": ("execution", "executing", ["approvals.request(booking)", "notify.owner"]),
    "await_approval": ("execution", "awaiting_approval", ["approvals.read"]),
    "book_job": ("execution", "executing", ["gate.approval", "jobs.book", "notify.owner"]),
    "await_repair": ("execution", "monitoring", ["jobs.status"]),
    "request_post_repair_readings": ("verification", "verifying", ["notify.owner"]),
    "verify_repair": (
        "verification",
        "verifying",
        ["baseline.model(v1)", "prove.verify_repair", "repairs.add"],
    ),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_runs (
  id INTEGER PRIMARY KEY, agent TEXT, task TEXT, case_id TEXT, machine TEXT, step INTEGER, status TEXT,
  observation TEXT, reasoning TEXT, tools TEXT, result TEXT, started REAL, updated REAL, ended REAL,
  retries INTEGER DEFAULT 0, error TEXT);
CREATE INDEX IF NOT EXISTS agent_runs_agent ON agent_runs(agent, id);
"""


class Activity:
    """Agent runs in the orchestrator's database (one file, one clock)."""

    def __init__(self, store: Any) -> None:
        self.s = store
        self.db = store.db
        self.db.executescript(SCHEMA)

    def start(self, agent: str, task: str, status: str, *, machine: str | None = None, case_id: str | None = None,
              step: int | None = None, observation: str | None = None, tools: list[str] | None = None,
              reasoning: str | None = None) -> int:  # fmt: skip
        if agent not in AGENTS or status not in STATUSES:
            raise ValueError(f"unknown agent or status: {agent}/{status}")
        now = self.s.clock()
        cur = self.db.execute(
            "INSERT INTO agent_runs (agent, task, case_id, machine, step, status, observation, reasoning, tools,"
            " started, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                agent,
                task,
                case_id,
                machine,
                step,
                status,
                observation,
                reasoning,
                json.dumps(tools or []),
                now,
                now,
            ),
        )
        self.db.commit()
        return int(cur.lastrowid or 0)

    def open_for_step(self, case_id: str, step: int) -> dict[str, Any] | None:
        r = self.db.execute(
            "SELECT * FROM agent_runs WHERE case_id=? AND step=? AND ended IS NULL ORDER BY id DESC LIMIT 1",
            (case_id, step),
        ).fetchone()
        return dict(r) if r else None

    def update(self, run_id: int, status: str, *, error: str | None = None, retry: bool = False,
               reasoning: str | None = None) -> None:  # fmt: skip
        self.db.execute(
            "UPDATE agent_runs SET status=?, updated=?, error=COALESCE(?, error), retries=retries+?,"
            " reasoning=COALESCE(?, reasoning) WHERE id=?",
            (status, self.s.clock(), error, int(retry), reasoning, run_id),
        )
        self.db.commit()

    def finish(self, run_id: int, status: str, result: Any = None, reasoning: str | None = None) -> None:
        r = self.db.execute(
            "SELECT agent, task, case_id, machine FROM agent_runs WHERE id=?", (run_id,)
        ).fetchone()
        self.db.execute(
            "UPDATE agent_runs SET status=?, result=?, reasoning=COALESCE(?, reasoning), updated=?, ended=? WHERE id=?",
            (status, json.dumps(result, default=str), reasoning, self.s.clock(), self.s.clock(), run_id),
        )
        self.db.commit()
        if r is not None:
            self.s.log(f"agent:{r['agent']}", "agent_run", r["case_id"], run=run_id, task=r["task"],
                       machine=r["machine"], status=status, reasoning=reasoning)  # fmt: skip

    def record(self, agent: str, task: str, status: str, result: Any = None, **kw: Any) -> int:
        """A run that starts and ends at once (per-reading work of observation, intelligence, prediction)."""
        rid = self.start(agent, task, kw.pop("running_status", "analysing"), **kw)
        self.finish(rid, status, result, kw.get("reasoning"))
        return rid

    def runs(
        self, limit: int = 100, agent: str | None = None, case_id: str | None = None
    ) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM agent_runs WHERE 1=1", []
        if agent:
            q, args = q + " AND agent=?", [*args, agent]
        if case_id:
            q, args = q + " AND case_id=?", [*args, case_id]
        rows = self.db.execute(q + " ORDER BY id DESC LIMIT ?", [*args, limit]).fetchall()
        return [{**dict(r), "tools": json.loads(r["tools"] or "[]"),
                 "result": json.loads(r["result"]) if r["result"] else None} for r in rows]  # fmt: skip

    def board(self) -> list[dict[str, Any]]:
        """One row per agent: its newest run (open runs first), for the agent command centre."""
        out = []
        for key, name in AGENTS.items():
            open_ = self.db.execute(
                "SELECT * FROM agent_runs WHERE agent=? AND ended IS NULL ORDER BY id DESC LIMIT 1", (key,)
            ).fetchone()
            last = (
                open_
                or self.db.execute(
                    "SELECT * FROM agent_runs WHERE agent=? ORDER BY id DESC LIMIT 1", (key,)
                ).fetchone()
            )
            n_open = self.db.execute(
                "SELECT COUNT(*) FROM agent_runs WHERE agent=? AND ended IS NULL", (key,)
            ).fetchone()[0]
            row = dict(last) if last else None
            if row:
                row["tools"] = json.loads(row["tools"] or "[]")
                row["result"] = json.loads(row["result"]) if row["result"] else None
            out.append({"agent": key, "name": name, "code": WHO[key], "open_runs": int(n_open),
                        "status": row["status"] if row else "monitoring" if key == "observation" else "idle",
                        "last": row})  # fmt: skip
        return out
