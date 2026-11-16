"""Offline-first sync between the factory node (phone or shed PC) and the cloud server.

The node does everything locally: scoring, health, the agent's cases and the audit log. The cloud only
(a) keeps a replica of the node's audit log, for the owner's other devices and for the record, and
(b) receives the owner's WhatsApp replies (webhooks land on the server, not on the shed's phone).

    client = SyncClient(store, agent, transport)   # transport(op, payload) -> dict, raises ConnectionError
    client.run()                                   # push new audit rows, pull decisions, report status

Rules:
  * Push is idempotent: rows are keyed (node, seq) and must continue the node's hash chain; a replayed
    batch is counted as duplicates, a gap or a changed row is refused ("chain_mismatch").
  * Decisions pulled from the cloud go through Store.decide: the first decision wins; a later different
    answer from another channel becomes a CONFLICT and the owner is asked again (agent step).
  * Offline: nothing is lost. The node keeps working; run() reports offline and how much is waiting.
"""

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .store import Store, _hash

CloudSend = Callable[[str, dict[str, Any]], dict[str, Any]]


class Cloud:
    """The server side (FastAPI wraps it). SQLite so it survives restarts."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS events (node TEXT, seq INTEGER, ts REAL, case_id TEXT, actor TEXT, event TEXT,
          detail TEXT, prev TEXT, hash TEXT, PRIMARY KEY (node, seq));
        CREATE TABLE IF NOT EXISTS decisions (id TEXT PRIMARY KEY, node TEXT, approval_id TEXT, decision TEXT,
          by_ TEXT, channel TEXT, ref TEXT, ts REAL, acked INTEGER DEFAULT 0);
        """)  # fmt: skip

    def handle(self, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        if op == "push":
            return self.push(payload["node"], payload["rows"])
        if op == "pull":
            return {"decisions": self.pull(payload["node"])}
        if op == "ack":
            self.ack(payload["ids"])
            return {"ok": True}
        raise ValueError(op)

    def push(self, node: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        last = self.db.execute(
            "SELECT seq, hash FROM events WHERE node=? ORDER BY seq DESC LIMIT 1", (node,)
        ).fetchone()
        accepted = dup = 0
        for r in rows:
            have = self.db.execute(
                "SELECT hash FROM events WHERE node=? AND seq=?", (node, r["seq"])
            ).fetchone()
            if have is not None:
                if have["hash"] != r["hash"]:
                    return {
                        "error": "chain_mismatch",
                        "at": r["seq"],
                        "accepted": accepted,
                        "duplicates": dup,
                    }
                dup += 1
                continue
            prev = last["hash"] if last else "genesis"
            d = json.dumps(r["detail"], sort_keys=True, default=str)
            if r["prev"] != prev or r["hash"] != _hash(
                prev, r["ts"], r["case_id"], r["actor"], r["event"], d
            ):
                return {"error": "chain_mismatch", "at": r["seq"], "accepted": accepted, "duplicates": dup}
            self.db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
                            (node, r["seq"], r["ts"], r["case_id"], r["actor"], r["event"], d, r["prev"], r["hash"]))  # fmt: skip
            last = {"seq": r["seq"], "hash": r["hash"]}
            accepted += 1
        self.db.commit()
        return {"accepted": accepted, "duplicates": dup, "last_seq": last["seq"] if last else 0}

    def add_decision(self, did: str, node: str, approval_id: str, decision: str, by: str, channel: str, ref: str,
                     ts: float) -> None:  # fmt: skip
        self.db.execute("INSERT OR IGNORE INTO decisions VALUES (?,?,?,?,?,?,?,?,0)",
                        (did, node, approval_id, decision, by, channel, ref, ts))  # fmt: skip
        self.db.commit()

    def pull(self, node: str) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM decisions WHERE node=? AND acked=0 ORDER BY ts", (node,))
        return [dict(r) for r in rows]

    def ack(self, ids: list[str]) -> None:
        self.db.executemany("UPDATE decisions SET acked=1 WHERE id=?", [(i,) for i in ids])
        self.db.commit()

    def count(self, node: str) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM events WHERE node=?", (node,)).fetchone()[0])


class SyncClient:
    def __init__(self, store: Store, agent: Any, send: CloudSend, node: str = "factory-node") -> None:
        self.s, self.agent, self.send, self.node = store, agent, send, node

    def status(self) -> dict[str, Any]:
        pushed = int(self.s.meta("pushed_seq") or 0)
        waiting = len(self.s.audit(since_seq=pushed))
        return {"online": self.s.meta("online") == "1", "last_sync": self.s.meta("last_sync"),
                "last_error": self.s.meta("last_error"), "events_waiting": waiting,
                "actions_waiting": len(self.s.queued_actions())}  # fmt: skip

    def run(self, batch: int = 200) -> dict[str, Any]:
        pushed = int(self.s.meta("pushed_seq") or 0)
        report: dict[str, Any] = {"pushed": 0, "duplicates": 0, "decisions": 0}
        try:
            got = self.send("pull", {"node": self.node})["decisions"]
            done = []
            for d in got:
                if self.s.approval(d["approval_id"]) is not None:
                    self.agent.decide(d["approval_id"], d["decision"], d["by_"], d["channel"], d["ref"])
                done.append(d["id"])
            if done:
                self.send("ack", {"ids": done})
            report["decisions"] = len(done)
            while True:
                rows = self.s.audit(since_seq=pushed)[:batch]
                if not rows:
                    break
                res = self.send("push", {"node": self.node, "rows": rows})
                if "error" in res:
                    self.s.meta("last_error", f"{res['error']} at {res.get('at')}")
                    report["error"] = res["error"]
                    break
                pushed = rows[-1]["seq"]
                self.s.meta("pushed_seq", str(pushed))
                report["pushed"] += res["accepted"]
                report["duplicates"] += res["duplicates"]
        except (ConnectionError, OSError, TimeoutError) as e:
            self.s.meta("online", "0")
            self.s.meta("last_error", str(e))
            return {**report, **self.status(), "online": False}
        self.s.meta("online", "1")
        self.s.meta("last_sync", str(self.s.clock()))
        self.s.meta("last_error", "")
        return {**report, **self.status()}
