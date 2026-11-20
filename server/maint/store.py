"""The maintenance agent's memory (SQLite, on the factory's own computer or phone).

    machines   identity and profile (preset, type, HP, contacts are elsewhere)
    cases      one per anomaly episode: state, urgency, plan, optimistic-lock version
    steps      the plan's steps with status, attempts, next retry time, result
    actions    every outward action, keyed by a deterministic idempotency key (never sent twice)
    approvals  what the owner was asked, and what they decided, on which channel
    repairs    jobs done and whether the machine came back towards its ORIGINAL baseline
    audit      append-only, hash-chained log of everything the agent and people did
    events     what happened (alarm, owner reply, mechanic no-show, ...), once per idempotency key
    memory     notes per machine and per owner ("prefers Sunday visits"), with the event they came from
    questions  numbered questions the planner asked the owner, and which option each number means
    decisions  every planner turn: event, model, reasoning, tool calls, outcome, tokens, ₹ cost

The audit chain makes later edits visible: each row's hash covers the previous row's hash.
"""

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS machines (
  machine TEXT PRIMARY KEY, name TEXT, factory TEXT, preset TEXT, mtype TEXT, hp REAL, kind TEXT);
CREATE TABLE IF NOT EXISTS cases (
  id TEXT PRIMARY KEY, machine TEXT, opened REAL, updated REAL, state TEXT, urgency TEXT, severity TEXT,
  plan TEXT, context TEXT, version INTEGER DEFAULT 0, closed REAL, outcome TEXT);
CREATE TABLE IF NOT EXISTS steps (
  case_id TEXT, idx INTEGER, name TEXT, status TEXT, attempts INTEGER DEFAULT 0, next_try REAL DEFAULT 0,
  result TEXT, error TEXT, updated REAL, PRIMARY KEY (case_id, idx));
CREATE TABLE IF NOT EXISTS actions (
  idem TEXT PRIMARY KEY, case_id TEXT, kind TEXT, payload TEXT, status TEXT, attempts INTEGER DEFAULT 0,
  next_try REAL DEFAULT 0, result TEXT, error TEXT, created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY, case_id TEXT, kind TEXT, summary TEXT, amount_low INTEGER, amount_high INTEGER,
  status TEXT, requested REAL, decided REAL, decided_by TEXT, channel TEXT, ref TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS repairs (
  id INTEGER PRIMARY KEY, machine TEXT, case_id TEXT, technician TEXT, work TEXT, done REAL, outcome TEXT,
  before_median REAL, after_median REAL, against_version INTEGER);
CREATE TABLE IF NOT EXISTS audit (
  seq INTEGER PRIMARY KEY, ts REAL, case_id TEXT, actor TEXT, event TEXT, detail TEXT, prev TEXT, hash TEXT);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS events (
  idem TEXT PRIMARY KEY, seq INTEGER, type TEXT, factory TEXT, machine TEXT, case_id TEXT, payload TEXT,
  source TEXT, ts REAL, status TEXT, attempts INTEGER DEFAULT 0, decision INTEGER, error TEXT, handled REAL);
CREATE INDEX IF NOT EXISTS events_status ON events(status, seq);
CREATE TABLE IF NOT EXISTS memory (
  id INTEGER PRIMARY KEY, scope TEXT, note TEXT, source TEXT, by_ TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS questions (
  id TEXT PRIMARY KEY, case_id TEXT, factory TEXT, question TEXT, options TEXT, status TEXT, asked REAL,
  answered REAL, choice INTEGER, answer TEXT);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY, event TEXT, event_type TEXT, case_id TEXT, machine TEXT, engine TEXT, model TEXT,
  status TEXT, reasoning TEXT, tool_calls TEXT, reply TEXT, outcome TEXT, input_tokens INTEGER DEFAULT 0,
  output_tokens INTEGER DEFAULT 0, cost_inr REAL DEFAULT 0, latency_ms INTEGER, started REAL, ended REAL,
  grounding TEXT);
CREATE INDEX IF NOT EXISTS decisions_case ON decisions(case_id, id);
"""
J = ("payload", "options", "tool_calls", "grounding")  # JSON columns of the planner tables


class Conflict(RuntimeError):
    """Someone else changed the case since it was read (optimistic lock)."""


def _hash(prev: str, ts: float, case_id: str | None, actor: str, event: str, detail: str) -> str:
    return hashlib.sha256(f"{prev}|{ts:.6f}|{case_id}|{actor}|{event}|{detail}".encode()).hexdigest()


class Store:
    def __init__(self, path: str | Path, clock: Any = time.time) -> None:
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.clock = clock

    # -- audit ------------------------------------------------------------------------------------
    def log(self, actor: str, event: str, case_id: str | None = None, **detail: Any) -> int:
        last = self.db.execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
        prev = last["hash"] if last else "genesis"
        ts, d = float(self.clock()), json.dumps(detail, sort_keys=True, default=str)
        cur = self.db.execute(
            "INSERT INTO audit (ts, case_id, actor, event, detail, prev, hash) VALUES (?,?,?,?,?,?,?)",
            (ts, case_id, actor, event, d, prev, _hash(prev, ts, case_id, actor, event, d)),
        )
        self.db.commit()
        return int(cur.lastrowid or 0)

    def audit(self, case_id: str | None = None, since_seq: int = 0) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM audit WHERE seq>?", [since_seq]
        if case_id:
            q, args = q + " AND case_id=?", [*args, case_id]
        return [
            {**dict(r), "detail": json.loads(r["detail"])} for r in self.db.execute(q + " ORDER BY seq", args)
        ]

    def verify_audit(self) -> dict[str, Any]:
        prev = "genesis"
        for r in self.db.execute("SELECT * FROM audit ORDER BY seq"):
            if r["prev"] != prev or r["hash"] != _hash(
                prev, r["ts"], r["case_id"], r["actor"], r["event"], r["detail"]
            ):
                return {"ok": False, "broken_at": r["seq"]}
            prev = r["hash"]
        return {"ok": True}

    # -- machines ---------------------------------------------------------------------------------
    def add_machine(self, machine: str, name: str, factory: str, preset: str, mtype: str, hp: float,
                    kind: str = "mill") -> None:  # fmt: skip
        self.db.execute("INSERT OR REPLACE INTO machines VALUES (?,?,?,?,?,?,?)",
                        (machine, name, factory, preset, mtype, hp, kind))  # fmt: skip
        self.db.commit()

    def machine(self, machine: str) -> dict[str, Any]:
        r = self.db.execute("SELECT * FROM machines WHERE machine=?", (machine,)).fetchone()
        if r is None:
            raise KeyError(machine)
        return dict(r)

    # -- cases ------------------------------------------------------------------------------------
    def case(self, case_id: str) -> dict[str, Any]:
        r = self.db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        if r is None:
            raise KeyError(case_id)
        return {**dict(r), "plan": json.loads(r["plan"]), "context": json.loads(r["context"])}

    def open_case(self, machine: str) -> dict[str, Any] | None:
        r = self.db.execute(
            "SELECT id FROM cases WHERE machine=? AND closed IS NULL ORDER BY opened DESC LIMIT 1", (machine,)
        ).fetchone()
        return self.case(r["id"]) if r else None

    def cases(self, machine: str | None = None, open_only: bool = False) -> list[dict[str, Any]]:
        q, args = "SELECT id FROM cases WHERE 1=1", []
        if machine:
            q, args = q + " AND machine=?", [machine]
        if open_only:
            q += " AND closed IS NULL"
        return [self.case(r["id"]) for r in self.db.execute(q + " ORDER BY opened", args)]

    def create_case(self, case_id: str, machine: str, urgency: str, severity: str, plan: list[str],
                    context: dict[str, Any]) -> dict[str, Any]:  # fmt: skip
        now = self.clock()
        self.db.execute(
            "INSERT INTO cases (id, machine, opened, updated, state, urgency, severity, plan, context)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (case_id, machine, now, now, "open", urgency, severity, json.dumps(plan), json.dumps(context)),
        )
        for i, name in enumerate(plan):
            self.db.execute("INSERT INTO steps (case_id, idx, name, status, updated) VALUES (?,?,?,?,?)",
                            (case_id, i, name, "pending", now))  # fmt: skip
        self.db.commit()
        return self.case(case_id)

    def update_case(self, case_id: str, expect_version: int, **fields: Any) -> dict[str, Any]:
        if "plan" in fields:
            fields["plan"] = json.dumps(fields["plan"])
        if "context" in fields:
            fields["context"] = json.dumps(fields["context"])
        sets = ", ".join(f"{k}=?" for k in fields)
        cur = self.db.execute(
            f"UPDATE cases SET {sets}, updated=?, version=version+1 WHERE id=? AND version=?",
            (*fields.values(), self.clock(), case_id, expect_version),
        )
        if cur.rowcount != 1:
            self.db.rollback()
            raise Conflict(case_id)
        self.db.commit()
        return self.case(case_id)

    def extend_plan(self, case_id: str, names: list[str]) -> None:
        c = self.case(case_id)
        start = len(c["plan"])
        for i, name in enumerate(names):
            self.db.execute("INSERT INTO steps (case_id, idx, name, status, updated) VALUES (?,?,?,?,?)",
                            (case_id, start + i, name, "pending", self.clock()))  # fmt: skip
        self.update_case(case_id, c["version"], plan=c["plan"] + names)

    # -- steps ------------------------------------------------------------------------------------
    def steps(self, case_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM steps WHERE case_id=? ORDER BY idx", (case_id,))
        return [{**dict(r), "result": json.loads(r["result"]) if r["result"] else None} for r in rows]

    def set_step(self, case_id: str, idx: int, **fields: Any) -> None:
        if "result" in fields:
            fields["result"] = json.dumps(fields["result"], default=str)
        sets = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE steps SET {sets}, updated=? WHERE case_id=? AND idx=?",
                        (*fields.values(), self.clock(), case_id, idx))  # fmt: skip
        self.db.commit()

    # -- actions (idempotent outward calls) -------------------------------------------------------
    def action(self, idem: str) -> dict[str, Any] | None:
        r = self.db.execute("SELECT * FROM actions WHERE idem=?", (idem,)).fetchone()
        if r is None:
            return None
        return {
            **dict(r),
            "payload": json.loads(r["payload"]),
            "result": json.loads(r["result"]) if r["result"] else None,
        }

    def put_action(self, idem: str, case_id: str, kind: str, payload: dict[str, Any]) -> bool:
        """True if new; False if this exact action was already recorded (it is never repeated)."""
        now = self.clock()
        cur = self.db.execute(
            "INSERT OR IGNORE INTO actions (idem, case_id, kind, payload, status, created, updated)"
            " VALUES (?,?,?,?,?,?,?)", (idem, case_id, kind, json.dumps(payload, default=str), "queued", now, now),
        )  # fmt: skip
        self.db.commit()
        return cur.rowcount == 1

    def set_action(self, idem: str, **fields: Any) -> None:
        if "result" in fields:
            fields["result"] = json.dumps(fields["result"], default=str)
        sets = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(
            f"UPDATE actions SET {sets}, updated=? WHERE idem=?", (*fields.values(), self.clock(), idem)
        )
        self.db.commit()

    def queued_actions(self) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT idem FROM actions WHERE status='queued' ORDER BY created").fetchall()
        return [a for r in rows if (a := self.action(r["idem"])) is not None]

    # -- approvals --------------------------------------------------------------------------------
    def request_approval(self, approval_id: str, case_id: str, kind: str, summary: str,
                         amount: tuple[int, int] | None = None) -> bool:  # fmt: skip
        cur = self.db.execute(
            "INSERT OR IGNORE INTO approvals (id, case_id, kind, summary, amount_low, amount_high, status, requested)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (approval_id, case_id, kind, summary, *(amount or (None, None)), "requested", self.clock()),
        )
        self.db.commit()
        return cur.rowcount == 1

    def approval(self, approval_id: str) -> dict[str, Any] | None:
        r = self.db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        return dict(r) if r else None

    def approvals(self, case_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM approvals WHERE 1=1", []
        if case_id:
            q, args = q + " AND case_id=?", [*args, case_id]
        if status:
            q, args = q + " AND status=?", [*args, status]
        return [dict(r) for r in self.db.execute(q + " ORDER BY requested", args)]

    def decide(self, approval_id: str, decision: str, by: str, channel: str, ref: str, ts: float | None = None,
               note: str | None = None) -> dict[str, Any]:  # fmt: skip
        """First decision wins. A later, different decision is kept as a conflict and asked again."""
        a = self.approval(approval_id)
        if a is None:
            raise KeyError(approval_id)
        if decision not in ("approved", "rejected"):
            raise ValueError(decision)
        if a["status"] == "requested":
            self.db.execute(
                "UPDATE approvals SET status=?, decided=?, decided_by=?, channel=?, ref=?, note=? WHERE id=?",
                (decision, self.clock() if ts is None else ts, by, channel, ref, note, approval_id),
            )
            self.db.commit()
            return {"applied": True, "status": decision}
        if a["status"] == decision:
            return {"applied": False, "status": decision, "duplicate": True}
        if a["status"] == "conflict":
            return {"applied": False, "status": "conflict"}
        self.db.execute("UPDATE approvals SET status='conflict', note=? WHERE id=?",
                        (f"{a['status']} via {a['channel']} then {decision} via {channel}", approval_id))  # fmt: skip
        self.db.commit()
        return {"applied": False, "status": "conflict", "was": a["status"]}

    def expire_requests(self, case_id: str) -> None:
        self.db.execute("UPDATE approvals SET status='expired', note='case closed' WHERE case_id=? AND status='requested'",
                        (case_id,))  # fmt: skip
        self.db.commit()

    def reopen_approval(self, approval_id: str, summary: str, amount: tuple[int, int]) -> None:
        """Ask the owner again about the same approval (e.g. the visit now costs more); the old decision is
        kept in the note and the audit."""
        self.db.execute("UPDATE approvals SET status='requested', summary=?, amount_low=?, amount_high=?, decided=NULL,"
                        " decided_by=NULL, channel=NULL, ref=NULL, note='reopened: cost changed' WHERE id=?",
                        (summary, int(amount[0]), int(amount[1]), approval_id))  # fmt: skip
        self.db.commit()
        self.log(
            "agent", "approval_reopened", approval_id.split(":")[0], approval=approval_id, summary=summary
        )

    def reset_conflict(self, approval_id: str) -> None:
        self.db.execute("UPDATE approvals SET status='requested', decided=NULL, decided_by=NULL, channel=NULL,"
                        " ref=NULL WHERE id=? AND status='conflict'", (approval_id,))  # fmt: skip
        self.db.commit()

    # -- repairs ----------------------------------------------------------------------------------
    def add_repair(self, **r: Any) -> int:
        cols = ", ".join(r)
        cur = self.db.execute(
            f"INSERT INTO repairs ({cols}) VALUES ({','.join('?' * len(r))})", tuple(r.values())
        )
        self.db.commit()
        return int(cur.lastrowid or 0)

    def repairs(self, machine: str | None = None, technician: str | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM repairs WHERE 1=1", []
        if machine:
            q, args = q + " AND machine=?", [*args, machine]
        if technician:
            q, args = q + " AND technician=?", [*args, technician]
        return [dict(r) for r in self.db.execute(q + " ORDER BY done", args)]

    # -- events (idempotent: the same key is stored once) -----------------------------------------
    @staticmethod
    def _row(r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        for k in J:
            if k in d and d[k] is not None:
                d[k] = json.loads(d[k])
        return d

    def put_event(self, idem: str, type_: str, factory: str | None, machine: str | None, case_id: str | None,
                  payload: dict[str, Any], source: str) -> bool:  # fmt: skip
        """True if new; False if an event with this key was already stored (it is never handled twice)."""
        seq = self.db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM events").fetchone()[0]
        cur = self.db.execute(
            "INSERT OR IGNORE INTO events (idem, seq, type, factory, machine, case_id, payload, source, ts, status)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (idem, seq, type_, factory, machine, case_id, json.dumps(payload, ensure_ascii=False, default=str),
             source, self.clock(), "pending"),
        )  # fmt: skip
        self.db.commit()
        return cur.rowcount == 1

    def event(self, idem: str) -> dict[str, Any] | None:
        r = self.db.execute("SELECT * FROM events WHERE idem=?", (idem,)).fetchone()
        return self._row(r) if r else None

    def events(
        self, status: str | None = None, case_id: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM events WHERE 1=1", []
        if status:
            q, args = q + " AND status=?", [*args, status]
        if case_id:
            q, args = q + " AND case_id=?", [*args, case_id]
        return [self._row(r) for r in self.db.execute(q + " ORDER BY seq LIMIT ?", [*args, limit])]

    def set_event(self, idem: str, **fields: Any) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE events SET {sets} WHERE idem=?", (*fields.values(), idem))
        self.db.commit()

    # -- memory (per machine, per owner) ----------------------------------------------------------
    def remember(self, scope: str, note: str, source: str, by: str) -> int:
        dup = self.db.execute("SELECT id FROM memory WHERE scope=? AND note=?", (scope, note)).fetchone()
        if dup:
            return int(dup["id"])
        cur = self.db.execute("INSERT INTO memory (scope, note, source, by_, ts) VALUES (?,?,?,?,?)",
                              (scope, note, source, by, self.clock()))  # fmt: skip
        self.db.commit()
        return int(cur.lastrowid or 0)

    def memory(self, scope: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute("SELECT * FROM memory WHERE scope=? ORDER BY id", (scope,))]

    # -- questions to the owner -------------------------------------------------------------------
    def ask(self, qid: str, case_id: str | None, factory: str | None, question: str,
            options: list[dict[str, Any]]) -> bool:  # fmt: skip
        cur = self.db.execute(
            "INSERT OR IGNORE INTO questions (id, case_id, factory, question, options, status, asked)"
            " VALUES (?,?,?,?,?,?,?)",
            (qid, case_id, factory, question, json.dumps(options, ensure_ascii=False), "open", self.clock()),
        )
        self.db.commit()
        return cur.rowcount == 1

    def question(self, qid: str) -> dict[str, Any] | None:
        r = self.db.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
        return self._row(r) if r else None

    def questions(self, factory: str | None = None, case_id: str | None = None,
                  open_only: bool = False) -> list[dict[str, Any]]:  # fmt: skip
        q, args = "SELECT * FROM questions WHERE 1=1", []
        if factory:
            q, args = q + " AND factory=?", [*args, factory]
        if case_id:
            q, args = q + " AND case_id=?", [*args, case_id]
        if open_only:
            q += " AND status='open'"
        return [self._row(r) for r in self.db.execute(q + " ORDER BY asked", args)]

    def answer(self, qid: str, choice: int | None, answer: str, status: str = "answered") -> None:
        self.db.execute("UPDATE questions SET status=?, answered=?, choice=?, answer=? WHERE id=?",
                        (status, self.clock(), choice, answer, qid))  # fmt: skip
        self.db.commit()

    # -- planner decisions ------------------------------------------------------------------------
    def start_decision(self, event: str, event_type: str, case_id: str | None, machine: str | None,
                       engine: str, model: str | None) -> int:  # fmt: skip
        cur = self.db.execute(
            "INSERT INTO decisions (event, event_type, case_id, machine, engine, model, status, tool_calls, started)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (event, event_type, case_id, machine, engine, model, "running", "[]", self.clock()),
        )
        self.db.commit()
        return int(cur.lastrowid or 0)

    def set_decision(self, did: int, **fields: Any) -> None:
        for k in J:
            if k in fields:
                fields[k] = json.dumps(fields[k], ensure_ascii=False, default=str)
        sets = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE decisions SET {sets} WHERE id=?", (*fields.values(), did))
        self.db.commit()

    def decision(self, did: int) -> dict[str, Any] | None:
        r = self.db.execute("SELECT * FROM decisions WHERE id=?", (did,)).fetchone()
        return self._row(r) if r else None

    def decisions(self, case_id: str | None = None, status: str | None = None,
                  limit: int = 200) -> list[dict[str, Any]]:  # fmt: skip
        q, args = "SELECT * FROM decisions WHERE 1=1", []
        if case_id:
            q, args = q + " AND case_id=?", [*args, case_id]
        if status:
            q, args = q + " AND status=?", [*args, status]
        rows = self.db.execute(q + " ORDER BY id DESC LIMIT ?", [*args, limit]).fetchall()
        return [self._row(r) for r in rows][::-1]

    def llm_cost_inr(self, case_id: str) -> float:
        r = self.db.execute("SELECT COALESCE(SUM(cost_inr), 0) FROM decisions WHERE case_id=?", (case_id,))
        return float(r.fetchone()[0])

    def meta(self, k: str, v: str | None = None) -> str | None:
        if v is not None:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, v))
            self.db.commit()
            return v
        r = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r["v"] if r else None
