"""Machines fed by a live recorder (a phone or the laptop microphone), through the same pipeline as every
other reading: baseline -> detectors -> health -> the maintenance agent and the planner's events.

    feed = LiveFeed(brain)
    feed.add_machine("A", "Table fan", "fan", 0.1)        # a new machine, or take over an existing one
    feed.start("A-F1", device="phone-1", quick=True)        # learn its normal again, from live clips only
    feed.reading("A-F1", audio, device="phone-1", seq=7)    # one 10 s clip -> health, events, the stream
    feed.mark("A-F1", "fault_on")                           # for the measured stage trial (scripts/live_trial)

What changes for a machine the recorder feeds:
  * its earlier (simulated) readings and baseline versions are archived, not deleted, and its normal is
    learned again from live clips only (BaselineStore.restart);
  * the simulator stops feeding it; its readings carry source "live" and wall-clock time;
  * "quick learn" (a demo setting): the normal is learned from about 2 minutes of clips (QUICK_CLIPS
    readings of 10 s) instead of days. Everything about it is labelled "short learning period, demo", and
    the health confidence says the normal was learned from few readings;
  * messages the agent or the planner sends about it go through the real notifier (WhatsApp, or the
    labelled dry run), so the owner's numbered reply comes back to the planner. Messages about simulated
    machines stay internal, as before.

A reading is identified by device:seq (the ingest idempotency): a phone that resends after a lost reply is
never counted twice.
"""

import json
import re
import time
from datetime import datetime
from typing import Any

import numpy as np

from edge import features, summary
from edge.detectors import MIN_ROWS, WIDTH
from edge.summary import S

QUICK_LEARN_S = 120  # demo: about two minutes of clips
CLIP_S = 10
QUICK_CLIPS = max(MIN_ROWS, QUICK_LEARN_S // CLIP_S)  # 12 clips of 10 s
# the learning period, from the first clip's start to the last one's: 12 clips 10 s apart span 110 s
QUICK_DAYS = (QUICK_CLIPS - 1) * CLIP_S / 86400
QUICK_LABEL = "short learning period, demo"
SLICES = 20  # must match server/app.py SLICES (the stream's slices per reading)
SHAPE = {"pump": "pump", "fan": "motor", "gearbox": "loom", "valve": "motor", "slider": "motor"}
MTYPES = ("pump", "fan", "gearbox", "valve", "slider")

SCHEMA = """
CREATE TABLE IF NOT EXISTS live_machines (
  machine TEXT PRIMARY KEY, device TEXT, quick INTEGER, learning_days REAL, target INTEGER, started REAL,
  spec TEXT, marks TEXT DEFAULT '[]');
"""


class LiveFeed:
    def __init__(self, brain: Any) -> None:
        self.b = brain
        self.db = brain.store.db
        self.db.executescript(SCHEMA)
        self.rows: dict[str, dict[str, Any]] = {}
        for r in self.db.execute("SELECT * FROM live_machines"):
            row = dict(r)
            if row["spec"]:  # a machine registered from the recorder: put it back in the cluster
                self._add_to_cluster(json.loads(row["spec"]))
            self.rows[row["machine"]] = row

    # ------------------------------------------------------------------ which machines
    def is_live(self, mid: str) -> bool:
        return bool((self.rows.get(mid) or {}).get("started"))

    def live_ids(self) -> list[str]:
        return [m for m in self.rows if self.is_live(m)]

    def _add_to_cluster(self, spec: dict[str, Any]) -> None:
        c = self.b.c
        if spec["id"] not in c.machines:
            c.machines[spec["id"]] = spec
            c.cfg["machines"].append(spec)
        self.b.base.register(spec["id"], spec["type"], learning_days=QUICK_DAYS, now=time.time())
        self.b.store.add_machine(spec["id"], spec["name"], spec["factory"], "flour_mill", spec["type"],
                                 float(spec.get("motor_hp") or 1), "blower" if spec["type"] == "fan" else "mill")  # fmt: skip

    def add_machine(self, factory: str, name: str, mtype: str, hp: float) -> dict[str, Any]:
        """Register a new machine from the recorder, in one of the cluster's factories."""
        c = self.b.c
        if factory not in c.factories:
            raise ValueError(f"unknown factory {factory!r}")
        if mtype not in MTYPES:
            raise ValueError(f"type must be one of {MTYPES}")
        name = re.sub(r"\s+", " ", name).strip()[:40]
        if not name:
            raise ValueError("a name is needed")
        n = 1 + sum(1 for m in c.machines if m.startswith(f"{factory}-L"))
        spec = {"id": f"{factory}-L{n}", "factory": factory, "name": name, "name_local": name, "type": mtype,
                "source": "live", "shape": SHAPE[mtype], "motor_hp": round(float(hp), 2)}  # fmt: skip
        with self.b.lock:
            self._add_to_cluster(spec)
            self.db.execute("INSERT OR REPLACE INTO live_machines (machine, spec) VALUES (?,?)",
                            (spec["id"], json.dumps(spec)))  # fmt: skip
            self.db.commit()
            self.rows[spec["id"]] = dict(
                self.db.execute("SELECT * FROM live_machines WHERE machine=?", (spec["id"],)).fetchone()
            )
            self.b.live.version += 1
        return spec

    # ------------------------------------------------------------------ start / stop
    def start(self, mid: str, device: str, quick: bool = True) -> dict[str, Any]:
        """The recorder takes over this machine: learn its normal again from live clips only."""
        if mid not in self.b.c.machines:
            raise KeyError(mid)
        days = QUICK_DAYS if quick else float(self.b.c.cfg.get("learning_days", 7))
        target = QUICK_CLIPS if quick else max(MIN_ROWS, 30)
        now = time.time()
        with self.b.lock:
            archive = self.b.base.restart(mid, days, now=now)
            self.db.execute("INSERT INTO live_machines (machine, device, quick, learning_days, target, started, marks)"
                            " VALUES (?,?,?,?,?,?,'[]') ON CONFLICT(machine) DO UPDATE SET device=excluded.device,"
                            " quick=excluded.quick, learning_days=excluded.learning_days, target=excluded.target,"
                            " started=excluded.started, marks='[]'", (mid, device, int(quick), days, target, now))  # fmt: skip
            self.db.commit()
            self.rows[mid] = dict(
                self.db.execute("SELECT * FROM live_machines WHERE machine=?", (mid,)).fetchone()
            )
            self.b.live.cache.pop(mid, None)
            self.b.health[mid] = self.b.assess(mid)
            label = f" ({QUICK_LABEL})" if quick else ""
            self.b.activity.record("memory", f"live recorder: learn {self.b.c.machines[mid]['name']}'s normal again{label}",
                                   "completed", machine=mid, reasoning=f"earlier readings archived as {archive}; "
                                   f"{target} live clips of {CLIP_S} s needed", tools=["baseline.restart"])  # fmt: skip
            self.b.live.event(
                "live_started", machine=mid, device=device, quick=quick, label=QUICK_LABEL if quick else None
            )
            self.b.live.version += 1
        return self.status(mid)

    def stop(self, mid: str) -> dict[str, Any]:
        """Hand the machine back to the simulator (its normal is learned again from simulated readings)."""
        with self.b.lock:
            if self.is_live(mid):
                self.b.base.restart(mid, float(self.b.c.cfg.get("learning_days", 7)), now=self.b.clock())
                self.db.execute("UPDATE live_machines SET started=NULL WHERE machine=?", (mid,))
                self.db.commit()
                self.rows[mid]["started"] = None
                self.b.live.cache.pop(mid, None)
                self.b.live.event("live_stopped", machine=mid)
                self.b.live.version += 1
        return {"machine": mid, "live": False}

    def mark(self, mid: str, label: str, ts: float | None = None) -> dict[str, Any]:
        """A moment the presenter marks (e.g. fault_on when the coin goes on): the trial measures from it."""
        if not self.is_live(mid):
            raise KeyError(mid)
        with self.b.lock:
            marks = json.loads(self.rows[mid]["marks"] or "[]")
            marks.append({"label": label[:40], "ts": time.time() if ts is None else ts})
            self.db.execute("UPDATE live_machines SET marks=? WHERE machine=?", (json.dumps(marks), mid))
            self.db.commit()
            self.rows[mid]["marks"] = json.dumps(marks)
            self.b.live.event("live_mark", machine=mid, label=label)
        return {"machine": mid, "marks": marks}

    # ------------------------------------------------------------------ one clip
    def reading(
        self, mid: str, audio: np.ndarray, device: str, seq: int, ts: float | None = None
    ) -> dict[str, Any]:
        if not self.is_live(mid):
            raise KeyError(mid)
        b = self.b
        ts = time.time() if ts is None else float(ts)
        rid = f"{device}:{seq}"
        with b.lock:
            q = summary.quality(audio)
            row = np.full(WIDTH, np.nan)
            row[: summary.DIM] = summary.summarise(audio) if q["seconds"] >= summary.MIN_SECONDS else 0.0
            obs = b.base.observe(mid, row, q, ts=ts, source="live", reading_id=rid)
            if obs.get("duplicate"):
                return {"duplicate": True, "id": obs["id"], **self.status(mid)}
            for ev in obs.get("events") or []:
                if ev.get("kind") == "baseline_learned":
                    quick = bool(self.rows[mid]["quick"])
                    b.activity.record("memory", "store the learned normal" + (f" ({QUICK_LABEL})" if quick else ""),
                                      "completed", machine=mid, reasoning=f"baseline v{ev['version']} from {ev['n']} live readings",
                                      tools=["baseline.versions"])  # fmt: skip
                    b.live.event("live_learned", machine=mid, version=ev["version"], n=ev["n"],
                                 label=QUICK_LABEL if quick else None)  # fmt: skip
            prev = b.health.get(mid, {}).get("urgency")
            h = b.health[mid] = b.assess(mid)
            if h["status"] not in ("learning", "no_signal", "not_running"):
                b.agent.observe(mid, h, reading_ref=f"{mid}@{rid}")
            if prev != h["urgency"]:
                b._emit_health(f"live-{device}-{seq}", mid, prev, h)
            b.agent.tick()
            b.sync.run()
            if not b.planner.llm_on:
                b.bus.drain()
            b.store.db.commit()
            b.live.cache[mid] = {"tick": b.live.tick, **self.slices(mid, audio, obs.get("risk"))}
            b.version += 1
            b.live.version += 1
        if b.planner.llm_on:
            b.bus.wake.set()
        return {"duplicate": False, "id": obs["id"], "observation": obs["state"], "risk": obs.get("risk"),
                "quality": q, "health": {k: h.get(k) for k in ("status", "urgency", "severity", "confidence")},
                **self.status(mid)}  # fmt: skip

    def slices(self, mid: str, audio: np.ndarray, risk: float | None) -> dict[str, Any]:
        """The stream's view of a live clip (same shape as server/app.slices): log-mel columns against this
        machine's own learned normal (or the clip itself while learning), loudness, and the clip's risk."""
        mel = features.log_mel(audio)
        v = self.b.base.versions(mid)
        if v:
            learned = self.b.base.vectors(v[-1]["reading_ids"])[:, S.mel_mean]
            mu, sd = np.median(learned, axis=0), learned.std(axis=0) + 0.5
        else:
            mu, sd = mel.mean(axis=1), mel.std(axis=1) + 0.5
        z = (mel - mu[:, None]) / sd[:, None]
        n_fr = mel.shape[1]
        spec, energy = [], []
        seg_n = max(1, len(audio) // SLICES)
        for i in range(SLICES):
            a = int(i * n_fr / SLICES)
            bb = max(int((i + 1) * n_fr / SLICES), a + 1)
            spec.append(np.clip((z[:, a:bb].mean(axis=1) + 3) / 6, 0, 1).round(3).tolist())
            seg = audio[i * seg_n : (i + 1) * seg_n]
            energy.append(round(float(10 * np.log10(np.mean(seg**2) + 1e-12)), 1))
        r = 0.0 if risk is None else round(float(risk), 3)
        return {"spec": spec, "risk": [r] * SLICES, "energy": energy,
                "band_z": np.round(z.mean(axis=1), 2).tolist()}  # fmt: skip

    # ------------------------------------------------------------------ what the recorder and screens show
    def status(self, mid: str) -> dict[str, Any]:
        row = self.rows.get(mid) or {}
        m = self.b.base.machine(mid)
        hist = [h for h in self.b.base.history(mid) if h["source"] == "live"]
        learning = m["state"] == "learning"
        clips = sum(1 for h in hist if h["clean"] and h["risk"] is None)
        last = hist[-1] if hist else None
        quick = bool(row.get("quick"))
        h = self.b.health.get(mid, {})
        return {"machine": mid, "name": self.b.c.machines[mid]["name"], "live": self.is_live(mid),
                "device": row.get("device"), "quick": quick, "label": QUICK_LABEL if quick else None,
                "state": "learning" if learning else "monitoring",
                "progress": {"clips": min(clips, row.get("target") or QUICK_CLIPS) if learning else row.get("target"),
                             "target": row.get("target") or QUICK_CLIPS},
                "readings": len(hist), "risk": last and last["risk"], "last_ts": last and last["ts"],
                "status_health": h.get("status"), "urgency": h.get("urgency"),
                "marks": json.loads(row.get("marks") or "[]")}  # fmt: skip

    def twin_entry(self, mid: str) -> dict[str, Any]:
        """Overrides for the twin's machine entry (server/app.Live.state): risk, history and the LIVE badge
        come from the live readings, not from the simulator."""
        st = self.status(mid)
        hist = [h for h in self.b.base.history(mid, last=60) if h["source"] == "live"]
        h = self.b.health.get(mid, {})
        status = ("learning" if st["state"] == "learning" else "alarm" if h.get("urgency") in
                  ("inspect", "maintenance", "urgent_owner_attention") else "watch" if h.get("urgency") in
                  ("measure_again", "monitor") else "ok")  # fmt: skip
        iso = lambda t: datetime.fromtimestamp(t).isoformat(timespec="seconds")  # noqa: E731
        last = hist[-1] if hist else None
        latest = last and {"tick": len(hist), "ts": iso(last["ts"]), "clip": f"live:{last['reading_id']}",
                           "score": last["risk"] or 0.0, "threshold": 1.0, "risk": last["risk"] or 0.0,
                           "match_cos": None, "match_level": None, "mode": "live", "mix": 0.0}  # fmt: skip
        return {"status": status, "mode": "live", "latest": latest,
                "learning": ({"day": st["progress"]["clips"], "of": st["progress"]["target"]} if st["state"] == "learning" else None),
                "history": [{"tick": i, "ts": iso(x["ts"]), "risk": round(x["risk"], 3), "match": False}
                            for i, x in enumerate(hist) if x["risk"] is not None],
                "live": {k: st[k] for k in ("device", "quick", "label", "state", "progress", "readings", "risk", "last_ts")}}  # fmt: skip

    # ------------------------------------------------------------------ messages
    def send(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Brain transport hook: a message about a live-fed machine goes through the real notifier."""
        mid = payload.get("machine")
        if kind != "notify" or not mid or not self.is_live(mid):
            return {"id": f"{kind}:{str(payload.get('idem', ''))[:10]}", "dry_run": True}
        alerts = self.b.live.alerts
        f = self.b.c.machines[mid]["factory"]
        a = alerts.notifier.send(machine=mid, factory=f, severity="planner", to=alerts.contact(f, machine=mid),
                                 lang=alerts.owner_lang(mid), body=str(payload.get("text", "")), kind="planner")  # fmt: skip
        return {"id": f"alert:{a['id']}", "status": a.get("status"), "provider": alerts.provider}
