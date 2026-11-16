"""What "normal" means for ONE machine, learned on the phone, versioned, and never taught a fault.

    store = BaselineStore("node_edge.db", detectors=["rms", "spectral"])
    store.register("chakki-1", mtype="pump", learning_days=5, now=t0)
    out = store.observe("chakki-1", row, quality, ts=t, source="live")   # every reading

Life of a machine:

1. LEARNING  (learning_days, an assumption; "Chakki 1: din 4/7"). Every usable, running reading is kept.
   No alarms. When the period is over and there are at least MIN_ROWS readings, baseline v1 is fitted
   (edge/detectors.MachineModel on those readings only) and becomes active.
2. MONITORING. Each reading gets a risk against the active version (1.0 = this machine's alarm level).
   A reading is learned from ("clean") only if it is usable, the machine was running, its risk is
   below FREEZE_AT, and the machine has no open anomaly (`hold`, set by the maintenance agent).
3. DRIFT CHECK every ADAPT_EVERY clean readings: d = median risk of the recent clean readings under the
   active version (normal ~ 0).
     |d| <  DRIFT_SMALL : nothing to do.
     |d| <  DRIFT_LARGE : the slow change is small: a new version is fitted on the recent clean readings
                          (reason "auto_drift"), but only if it still flags the clear anomalies (risk >= 2)
                          the active version saw (anomaly-period exclusion check). Otherwise it is rejected.
     |d| >= DRIFT_LARGE : too big to learn silently; it could be wear. No adaptation; a "drift" event is
                          returned and a version is only made if the OWNER approves a recalibration
                          (after an inspection, a repair, a new belt).
4. RECALIBRATION (owner-approved): `propose(...)` fits a candidate on readings the caller names (e.g.
   readings after a verified repair); `approve(...)` activates it with the approval reference. Repair
   verification always compares with v1, the machine's first learned normal, not the newest version.

Readings that are too quiet compared with the learned normal (machine OFF) are stored as "not running"
and never scored. All cut-offs are assumptions, listed in ASSUMPTIONS and shown in docs.
"""

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .detectors import MIN_ROWS, WIDTH, MachineModel, chosen
from .summary import S

ASSUMPTIONS = {
    "learning_days_default": 7,  # spec allows 3-7
    "freeze_at": 0.8,  # never learn from a reading at or above 80% of the alarm level
    "adapt_every": 30,  # clean readings between drift checks
    "drift_window": 120,  # recent clean readings a drift check / new version uses
    "drift_small": 0.25,  # |median risk| below this: no drift
    "drift_large": 0.6,  # at or above: owner-approved recalibration only
    "clear_anomaly": 2.0,  # readings at or above 2x the alarm level are clear anomalies (not chance crossings)
    "keep_flagged_share": 0.8,  # a new version must still flag 80% of the clear anomalies of the old one
    "off_log_rms_drop": 1.0,  # log10 RMS 1.0 (= 20 dB) below the learned median: machine is off
    "gap_hours": 12.0,  # a stop longer than this = restart; first readings after it are "settling"
    "settle_readings": 3,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS machines (
  machine TEXT PRIMARY KEY, mtype TEXT, registered REAL, learning_days REAL, state TEXT,
  active_version INTEGER, hold INTEGER DEFAULT 0, detectors TEXT, fusion TEXT);
CREATE TABLE IF NOT EXISTS summaries (
  id INTEGER PRIMARY KEY, machine TEXT, reading_id TEXT UNIQUE, ts REAL, source TEXT, vec BLOB,
  quality TEXT, running INTEGER, risk REAL, version INTEGER, clean INTEGER, excluded TEXT,
  after_gap INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS summaries_m ON summaries(machine, ts);
CREATE TABLE IF NOT EXISTS baselines (
  machine TEXT, version INTEGER, created REAL, parent INTEGER, reason TEXT, status TEXT,
  reading_ids TEXT, n_rows INTEGER, alarm REAL, fused_median REAL, drift REAL,
  approved_by TEXT, approval_ref TEXT, note TEXT, PRIMARY KEY (machine, version));
"""

SOURCES = ("live", "upload", "simulated")


@dataclass
class Observation:
    state: str  # learning | monitoring | not_running | unusable
    risk: float | None
    version: int | None
    clean: bool
    excluded: str | None
    after_gap: bool
    progress: dict[str, int] | None = None
    parts: dict[str, float] | None = None
    events: list[dict[str, Any]] | None = None

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__.items())


class BaselineStore:
    def __init__(
        self,
        path: str | Path,
        detectors: list[str] | None = None,
        fusion: str = "mean",
        assumptions: dict[str, float] | None = None,
    ) -> None:
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        pick = chosen()
        self.default_detectors = detectors or pick["names"]
        self.default_fusion = fusion if detectors else pick["fusion"]
        self.a = {**ASSUMPTIONS, **(assumptions or {})}
        self._models: dict[tuple[str, int], MachineModel] = {}

    # -- machines -------------------------------------------------------------------------------
    def register(
        self, machine: str, mtype: str, learning_days: float | None = None, now: float | None = None,
        detectors: list[str] | None = None, fusion: str | None = None,
    ) -> None:  # fmt: skip
        self.db.execute(
            "INSERT OR IGNORE INTO machines (machine, mtype, registered, learning_days, state, detectors, fusion)"
            " VALUES (?,?,?,?,?,?,?)",
            (machine, mtype, time.time() if now is None else now,
             float(learning_days or self.a["learning_days_default"]), "learning",
             json.dumps(detectors or self.default_detectors), fusion or self.default_fusion),
        )  # fmt: skip
        self.db.commit()

    def machine(self, machine: str) -> dict[str, Any]:
        r = self.db.execute("SELECT * FROM machines WHERE machine=?", (machine,)).fetchone()
        if r is None:
            raise KeyError(f"machine {machine!r} is not registered")
        return dict(r)

    def set_hold(self, machine: str, hold: bool) -> None:
        """While a machine has an open anomaly case, nothing it records is learned from."""
        self.db.execute("UPDATE machines SET hold=? WHERE machine=?", (int(hold), machine))
        self.db.commit()

    # -- versions ---------------------------------------------------------------------------------
    def versions(self, machine: str) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM baselines WHERE machine=? ORDER BY version", (machine,))
        return [{**dict(r), "reading_ids": json.loads(r["reading_ids"])} for r in rows]

    def _rows(self, ids: list[int]) -> np.ndarray:
        if not ids:
            return np.empty((0, WIDTH))
        q = f"SELECT id, vec FROM summaries WHERE id IN ({','.join('?' * len(ids))})"
        by_id = {r["id"]: np.frombuffer(r["vec"], dtype=np.float64) for r in self.db.execute(q, ids)}
        return np.vstack([by_id[i] for i in ids])

    def model(self, machine: str, version: int | None = None) -> MachineModel | None:
        m = self.machine(machine)
        v = m["active_version"] if version is None else version
        if v is None:
            return None
        if (machine, v) not in self._models:
            b = self.db.execute(
                "SELECT * FROM baselines WHERE machine=? AND version=?", (machine, v)
            ).fetchone()
            if b is None:
                return None
            mm = MachineModel(json.loads(m["detectors"]), m["fusion"])
            self._models[(machine, v)] = mm.fit(self._rows(json.loads(b["reading_ids"])))
        return self._models[(machine, v)]

    def _new_version(
        self, machine: str, ids: list[int], reason: str, status: str, drift: float | None = None,
        note: str | None = None, now: float | None = None,
    ) -> int:  # fmt: skip
        m = self.machine(machine)
        mm = MachineModel(json.loads(m["detectors"]), m["fusion"]).fit(self._rows(ids))
        v = (
            self.db.execute("SELECT MAX(version) FROM baselines WHERE machine=?", (machine,)).fetchone()[0]
            or 0
        ) + 1
        self.db.execute(
            "INSERT INTO baselines (machine, version, created, parent, reason, status, reading_ids, n_rows, alarm,"
            " fused_median, drift, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (machine, v, time.time() if now is None else now, m["active_version"], reason, status,
             json.dumps(ids), len(ids), mm.alarm, mm.fused_median, drift, note),
        )  # fmt: skip
        self._models[(machine, v)] = mm
        if status == "active":
            self._activate(machine, v)
        self.db.commit()
        return v

    def _activate(self, machine: str, v: int) -> None:
        self.db.execute(
            "UPDATE baselines SET status='retired' WHERE machine=? AND status='active' AND version<>?",
            (machine, v),
        )
        self.db.execute("UPDATE baselines SET status='active' WHERE machine=? AND version=?", (machine, v))
        self.db.execute(
            "UPDATE machines SET active_version=?, state='monitoring' WHERE machine=?", (v, machine)
        )

    def propose(self, machine: str, reading_ids: list[int], reason: str, note: str | None = None) -> int:
        """A candidate version that waits for the owner (recalibration after inspection or repair)."""
        usable = [
            r["id"]
            for r in self.db.execute(
                f"SELECT id, quality, running FROM summaries WHERE machine=? AND id IN ({','.join('?' * len(reading_ids))})",
                (machine, *reading_ids),
            )
            if json.loads(r["quality"])["ok"] and r["running"]
        ]
        if len(usable) < MIN_ROWS:
            raise ValueError(f"need at least {MIN_ROWS} usable running readings, got {len(usable)}")
        return self._new_version(machine, usable, reason, "proposed", note=note)

    def approve(self, machine: str, version: int, approved_by: str, approval_ref: str) -> None:
        """Only an owner approval activates a proposed version. The reference points at the logged approval."""
        b = self.db.execute(
            "SELECT status FROM baselines WHERE machine=? AND version=?", (machine, version)
        ).fetchone()
        if b is None or b["status"] != "proposed":
            raise ValueError(f"version {version} of {machine} is not waiting for approval")
        if not approved_by or not approval_ref:
            raise PermissionError("recalibration needs the owner's logged approval")
        self.db.execute(
            "UPDATE baselines SET approved_by=?, approval_ref=? WHERE machine=? AND version=?",
            (approved_by, approval_ref, machine, version),
        )
        self._activate(machine, version)
        self.db.commit()

    def reject(self, machine: str, version: int, note: str = "") -> None:
        self.db.execute(
            "UPDATE baselines SET status='rejected', note=? WHERE machine=? AND version=? AND status='proposed'",
            (note, machine, version),
        )
        self.db.commit()

    # -- readings ---------------------------------------------------------------------------------
    def history(self, machine: str, last: int | None = None) -> list[dict[str, Any]]:
        q = "SELECT id, reading_id, ts, source, quality, running, risk, version, clean, excluded, after_gap" \
            " FROM summaries WHERE machine=? ORDER BY ts, id"  # fmt: skip
        rows = [dict(r) for r in self.db.execute(q, (machine,))]
        return rows[-last:] if last else rows

    def vectors(self, ids: list[int]) -> np.ndarray:
        return self._rows(ids)

    def _store(self, machine: str, reading_id: str | None, ts: float, source: str, row: np.ndarray,
               quality: dict[str, Any], running: bool, risk: float | None, version: int | None, clean: bool,
               excluded: str | None, after_gap: bool) -> int:  # fmt: skip
        cur = self.db.execute(
            "INSERT INTO summaries (machine, reading_id, ts, source, vec, quality, running, risk, version, clean,"
            " excluded, after_gap) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (machine, reading_id, ts, source, np.asarray(row, dtype=np.float64).tobytes(), json.dumps(quality),
             int(running), risk, version, int(clean), excluded, int(after_gap)),
        )  # fmt: skip
        self.db.commit()
        return int(cur.lastrowid or 0)

    def _after_gap(self, machine: str, ts: float) -> bool:
        rows = self.db.execute(
            "SELECT ts FROM summaries WHERE machine=? AND running=1 AND ts<? ORDER BY ts DESC LIMIT ?",
            (machine, ts, int(self.a["settle_readings"]) + 1),
        ).fetchall()
        if not rows:
            return False
        times = [ts] + [r["ts"] for r in rows]
        gaps = np.diff(times[::-1])  # oldest -> newest
        return bool(np.any(gaps > self.a["gap_hours"] * 3600))

    def observe(
        self, machine: str, row: np.ndarray, quality: dict[str, Any], ts: float | None = None,
        source: str = "live", reading_id: str | None = None,
    ) -> dict[str, Any]:  # fmt: skip
        """Feed one reading (summary row + autoencoder score column). Idempotent on reading_id."""
        if source not in SOURCES:
            raise ValueError(f"source must be one of {SOURCES}")
        ts = time.time() if ts is None else ts
        if reading_id is not None:
            prev = self.db.execute("SELECT id FROM summaries WHERE reading_id=?", (reading_id,)).fetchone()
            if prev is not None:
                return {"duplicate": True, "id": prev["id"]}
        m = self.machine(machine)
        row = np.asarray(row, dtype=np.float64)
        if row.shape != (WIDTH,):
            raise ValueError(f"row must have {WIDTH} values")
        if not quality.get("ok", False):
            rid = self._store(machine, reading_id, ts, source, row, quality, False, None, m["active_version"],
                              False, "quality:" + ",".join(quality.get("reasons", [])), False)  # fmt: skip
            return {
                **Observation("unusable", None, m["active_version"], False, "quality", False).as_dict(),
                "id": rid,
            }

        if m["state"] == "learning":
            rid = self._store(
                machine, reading_id, ts, source, row, quality, True, None, None, True, None, False
            )
            days = (ts - m["registered"]) / 86400
            ids = [r["id"] for r in self.db.execute(
                "SELECT id FROM summaries WHERE machine=? AND clean=1 ORDER BY ts, id", (machine,))]  # fmt: skip
            if days >= m["learning_days"] and len(ids) >= MIN_ROWS:
                v = self._new_version(machine, ids, "learning_complete", "active", now=ts)
                return {**Observation("monitoring", None, v, True, None, False,
                                      events=[{"kind": "baseline_learned", "version": v, "n": len(ids)}]).as_dict(),
                        "id": rid}  # fmt: skip
            prog = {
                "day": min(int(days) + 1, int(np.ceil(m["learning_days"]))),
                "of": int(np.ceil(m["learning_days"])),
            }
            return {
                **Observation("learning", None, None, True, None, False, progress=prog).as_dict(),
                "id": rid,
            }

        model = self.model(machine)
        assert model is not None
        v = int(m["active_version"])
        learned = self._rows(json.loads(self.db.execute(
            "SELECT reading_ids FROM baselines WHERE machine=? AND version=?", (machine, v)).fetchone()[0]))  # fmt: skip
        if row[S.log_rms][0] < float(np.median(learned[:, S.log_rms])) - self.a["off_log_rms_drop"]:
            rid = self._store(
                machine, reading_id, ts, source, row, quality, False, None, v, False, "not_running", False
            )
            return {**Observation("not_running", None, v, False, "not_running", False).as_dict(), "id": rid}

        after_gap = self._after_gap(machine, ts)
        parts = {k: float(x[0]) for k, x in model.parts(row[None, :]).items()}
        risk = float(model.risk(row[None, :])[0])
        clean = risk < self.a["freeze_at"] and not m["hold"] and not after_gap
        excluded = None if clean else ("hold" if m["hold"] else "after_gap" if after_gap else "above_freeze")
        rid = self._store(
            machine, reading_id, ts, source, row, quality, True, risk, v, clean, excluded, after_gap
        )
        events = self._drift_check(machine, v, ts) if clean else []
        return {**Observation("monitoring", risk, v, clean, excluded, after_gap, parts=parts, events=events).as_dict(),
                "id": rid}  # fmt: skip

    # -- adaptation -------------------------------------------------------------------------------
    def _drift_check(self, machine: str, v: int, ts: float) -> list[dict[str, Any]]:
        created = self.db.execute(
            "SELECT created FROM baselines WHERE machine=? AND version=?", (machine, v)
        ).fetchone()[0]
        clean = [r["id"] for r in self.db.execute(
            "SELECT id FROM summaries WHERE machine=? AND clean=1 AND version=? AND ts>=? ORDER BY ts, id",
            (machine, v, created))]  # fmt: skip
        if not clean or len(clean) % int(self.a["adapt_every"]):
            return []
        recent = clean[-int(self.a["drift_window"]) :]
        model = self.model(machine, v)
        assert model is not None
        d = float(np.median(model.risk(self._rows(recent))))
        if abs(d) < self.a["drift_small"]:
            return [{"kind": "drift_check", "drift": round(d, 3), "result": "stable"}]
        if abs(d) >= self.a["drift_large"]:
            return [{"kind": "drift", "drift": round(d, 3), "result": "needs_owner_recalibration"}]
        flagged = [r["id"] for r in self.db.execute(
            "SELECT id FROM summaries WHERE machine=? AND version=? AND risk>=?",
            (machine, v, self.a["clear_anomaly"]))]  # fmt: skip
        m = self.machine(machine)
        cand = MachineModel(json.loads(m["detectors"]), m["fusion"]).fit(self._rows(recent))
        if flagged:
            kept = float(np.mean(cand.risk(self._rows(flagged)) >= 1.0))
            if kept < self.a["keep_flagged_share"]:
                return [{"kind": "drift", "drift": round(d, 3), "result": "rejected_would_unlearn_anomalies",
                         "kept_flagged_share": round(kept, 3)}]  # fmt: skip
        nv = self._new_version(machine, recent, "auto_drift", "active", drift=d, now=ts)
        return [{"kind": "drift", "drift": round(d, 3), "result": "adapted", "version": nv}]
