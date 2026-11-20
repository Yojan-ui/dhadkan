"""Live server for the web dashboard: the simulated cluster over HTTP + WebSocket.

    uvicorn dhadkan.live:create_app --factory --port 8600    # also serves web/dist if built

Every 0.5 s the WebSocket sends, per machine, the slice of its latest 10 s
reading under the play cursor: log-mel column, the 1 s window's anomaly score
over that machine's alarm level, and loudness. A reading is one real clip
scored by the exported .tflite model (sim.Cluster.step); nothing here makes
numbers up. State snapshots and discrete events (fault, label, fingerprint
published, early match, agent tool calls, route, owner reply) go out when they
happen.

Demo replay: resets, then plays one reading every 10 s (one clip in real time)
with a scripted fault on A-F1 at 0:40, the owner's voice note transcribed by
local Whisper, the fingerprint published, a SYNTHETIC onset on B-F1, and the
rules agent once B-F1 matches (or after 20 readings).
"""

import asyncio
import base64
import io
import json
import re
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import librosa
import numpy as np
import soundfile as sf
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from edge import data, embed, features
from edge.config import HOP, MODELS, N_MELS, RUNS, SR
from edge.outbox import Outbox
from edge.runtime import EdgeModel

from . import agent, costs, diagnosis, folk, forecast, provider, registry, report, route
from . import explain as sound
from .config import NODE_DB, VOICE_NOTE, WEB_DIST
from .notify import env, whatsapp
from .owner_alerts import ALERT_DIR, OwnerAlerts
from .sim import Cluster

SLICES = 20  # 10 s reading -> 20 slices of 0.5 s (the 2 Hz stream)
FRAME_S = 0.5
FAULT_MACHINE, SIMILAR_MACHINE = "A-F1", "B-F1"
IMS_MACHINE = "A-G1"  # Loom 1: Demo mode plays the NASA IMS bearing failure as its motor bearing
REPLAY_FAULT_AT, REPLAY_LABEL_AT, REPLAY_PUBLISH_AT, REPLAY_MAX = 4, 5, 7, 20  # live readings
# Before the live part, the replay records 14 simulated days (fast) in which A-F1's
# bearing sound grows slowly: a SYNTHETIC onset from day 6, +0.03 strength per reading.
SEED_DAYS, SEED_ONSET_AT, SEED_ONSET_STEP = 14, 15, 0.03
TIME_MACHINE_DAYS = 14


def _wav(audio: np.ndarray) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, audio, SR, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def latest_eval() -> dict[str, Any] | None:
    runs = sorted(RUNS.glob("eval_*.json"))
    if not runs:
        return None
    ev = json.loads(runs[-1].read_text(encoding="utf-8"))
    out = {
        "run_id": ev["run_id"],
        "code_sha256": ev["code_sha256"][:12],
        "assumptions": ev["assumptions"],
        "types": {},
    }
    for t, rep in ev["types"].items():
        out["types"][t] = {
            "headline": rep["headline_condition"],
            "conditions": {
                c: m
                and {
                    k: m.get(k)
                    for k in ("auc", "pauc", "n_normal", "n_anomalous", "false_alarm_rate", "detection_rate")
                }
                for c, m in rep["conditions"].items()
            },
            "per_unit": rep["per_unit"],
            "registry": [
                {
                    "from": r["from"],
                    "to": r["to"],
                    "case": r["case"],
                    "false_flags": r["registry_false_flag_rate"],
                    **r["by_mix"]["0.25"],
                }
                for r in rep["registry_transfer"]
            ],
            "tflite": rep["tflite"],
        }
    return out


def slices(audio: np.ndarray, r: dict[str, Any], em: EdgeModel) -> dict[str, Any]:
    """One reading -> SLICES x (log-mel column, window risk, loudness), plus band deviations."""
    mel = features.log_mel(audio)
    z = (mel - em.mu) / em.sd  # vs the model's training normal
    n_fr = mel.shape[1]
    step = int(0.5 * SR / HOP)
    spec, risk, energy = [], [], []
    for i in range(SLICES):
        a = int(i * n_fr / SLICES)
        b = max(int((i + 1) * n_fr / SLICES), a + 1)
        spec.append(np.clip((z[:, a:b].mean(axis=1) + 3) / 6, 0, 1).round(3).tolist())
        w = int(np.clip(round(((a + b) / 2 - 16) / step), 0, len(r["window_errors"]) - 1))
        risk.append(round(r["window_errors"][w] / r["threshold"], 3))
        seg = audio[int(i * FRAME_S * SR) : int((i + 1) * FRAME_S * SR)]
        energy.append(round(float(10 * np.log10(np.mean(seg**2) + 1e-12)), 1))
    return {"spec": spec, "risk": risk, "energy": energy, "band_z": np.round(r["band_z"], 2).tolist()}


class Live:
    """Owns the cluster, the per-reading analysis the stream plays, and the event list."""

    def __init__(self, node_db: Path | None = None, registry_db: Path | None = None) -> None:
        kw = {k: v for k, v in (("node_db", node_db), ("registry_db", registry_db)) if v}
        self.c = Cluster(**kw)
        self.lock = threading.RLock()
        # fingerprints leave the factory through the phone's outbox; `link_up` simulates signal
        self.link_up = True
        self.outbox = Outbox(Path(node_db or NODE_DB).with_name("outbox.db"), self._send)
        self.alerts = OwnerAlerts(self, node_db)
        self.demo: dict[str, Any] = {"on": False, "phase": "off"}
        self.last_agent: dict[str, Any] | None = None  # the owner agent's last run (timeline, checklist)
        self._ims_saved: dict[str, Any] | None = None
        self.cache = {}  # machine -> analysis of its latest reading
        self._readings: dict[tuple[str, int], dict[str, Any]] = {}  # rebuilt past readings (LRU-ish)
        self._explained: dict[tuple[str, int], dict[str, Any]] = {}
        self.tick = -1  # tick of the cached analysis (read by the stream without the DB)
        self.cursor = 0
        self.events, self.seq = [], 0
        self.version = 0
        self.pending_label = None
        self.replay: dict[str, Any] = {"on": False, "started": None, "phase": "off", "progress": 0.0}
        self.prev_status = {}
        self.mel_hz = librosa.mel_frequencies(N_MELS + 2, fmin=0, fmax=SR / 2)[1:-1].tolist()
        with self.lock:
            # A Demo-mode run (NASA IMS readings on Loom 1) cannot be resumed after a restart: its source is
            # gone, so its readings would be scored as the wrong machine. Start clean instead.
            if self.c.db.execute("SELECT 1 FROM readings WHERE mode='ims' LIMIT 1").fetchone():
                self.reset()
            if self.c.tick_no() >= 0:
                self._analyse()
                self.prev_status = {r["machine_id"]: r["status"] for r in self.ranking()}

    # -- analysis of the latest reading -------------------------------------
    def _analyse(self, scored: dict[str, tuple[np.ndarray, dict[str, Any]]] | None = None) -> None:
        """Per-slice view of the latest reading. `scored` reuses sim.step's results."""
        tick = self.c.tick_no()
        feed = getattr(getattr(self, "brain", None), "feed", None)
        for mid, m in self.c.machines.items():
            if feed is not None and feed.is_live(mid):
                continue  # the live recorder's clip is in the cache already
            if self.c.operating(mid) != ("running", "ok") or not self.c.history(mid, 1):
                self.cache.pop(mid, None)  # nothing live: never stream an old reading as if it were now
                continue
            if scored and mid in scored:
                audio, r = scored[mid]
            else:
                row = self.c.history(mid, 1)[0]
                audio, _ = self.c.audio(mid, tick, row["mode"], row["mix"])
                r = self._score(mid, tick, audio)
            self.cache[mid] = {"tick": tick, **slices(audio, r, self.c.model(m["type"]))}
        self.tick, self.cursor = tick, 0

    def _score(self, mid: str, tick: int, audio: np.ndarray) -> dict[str, Any]:
        if mid in self.c.ims:
            return self.c.ims[mid].result(tick)
        return self.c.scorer(mid).score_audio(audio, self.c.machines[mid]["source"])

    # -- state ----------------------------------------------------------------
    def ranking(self) -> list[dict[str, Any]]:
        with self.lock:
            return agent.Tools(self.c, "view", "live").get_risk_ranking() if self.c.tick_no() >= 0 else []

    def event(self, kind: str, **data_: Any) -> None:
        with self.lock:
            self._event(kind, **data_)

    def _event(self, kind: str, **data_: Any) -> None:
        self.seq += 1
        self.events.append(
            {
                "seq": self.seq,
                "kind": kind,
                "clock": self.c.now().isoformat(timespec="minutes"),
                "replay_t": self.replay_t(),
                **data_,
            }
        )
        self.events = self.events[-60:]
        self.version += 1

    def replay_t(self) -> float | None:
        s = self.replay["started"]
        return None if s is None else round(time.monotonic() - s, 1)

    def route(self) -> dict[str, Any] | None:
        row = self.c.db.execute(
            "SELECT output, ts FROM tool_calls WHERE tool='plan_technician_route' " "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        p = json.loads(row["output"])
        tech, cfg = self.c.cfg["technician"], self.c.cfg
        ids = [s.split()[0] for s in p["stops"]]
        stops = [
            {
                "id": i,
                "xy_km": self.c.factories[self.c.machines[i]["factory"]]["xy_km"],
                "service_h": cfg["service_hours"][self.c.machines[i]["type"]],
            }
            for i in ids
        ]
        start = datetime.fromisoformat(f"{p['date']}T{tech.get('day_start', '09:00')}")
        tl = route.timeline(stops, tech["xy_km"], cfg["road_kmph"], start)
        return {
            **p,
            "planned_at": row["ts"],
            "base_xy": tech["xy_km"],
            "legs": [
                {
                    "machine": s["id"],
                    "factory": self.c.machines[s["id"]]["factory"],
                    "xy_km": st["xy_km"],
                    "arrive": s["arrive"].isoformat(timespec="minutes"),
                    "leave": s["leave"].isoformat(timespec="minutes"),
                }
                for s, st in zip(tl, stops, strict=False)
            ],
            "not_today_ids": [s.split()[0] for s in p["not_today"]],
        }

    def messages(self) -> list[dict[str, Any]]:
        rows = [dict(r) for r in self.c.db.execute("SELECT * FROM messages ORDER BY id")]
        drafts = {}
        for t in self.c.db.execute(
            "SELECT run_id, input, output FROM tool_calls WHERE tool='draft_owner_alert'"
        ):
            out = json.loads(t["output"])
            if "text" in out:
                drafts[(t["run_id"], out["text"])] = out.get("text_en")
        for r in rows:
            r["machine"], r["visit_day"] = r["ask"].split("|")
            r["text_en"] = drafts.get((r["run_id"], r["body"]))
        return rows

    def state(self) -> dict[str, Any]:
        with self.lock:
            rank = {r["machine_id"]: r for r in self.ranking()}
            machines = []
            for mid, m in self.c.machines.items():
                hist = self.c.history(mid)
                last = hist[-1] if hist else None
                mode, mix = self.c.mode(mid)
                r = rank.get(mid, {})
                machines.append(
                    {
                        **{
                            k: m[k]
                            for k in ("id", "factory", "name", "name_local", "type", "source", "shape")
                        },
                        "mode": mode,
                        "mix": mix,
                        "risk3": r.get("risk"),
                        "status": r.get("status", "no_data"),
                        "days_rising": r.get("days_rising", 0),
                        "known_fault": r.get("known_fault"),
                        "learning": r.get("learning"),
                        "fault_name": self._fault_name(mid, r.get("known_fault")),
                        "registry": r.get("registry"),
                        "forecast": self._forecast(mid, hist),
                        "rupees_at_risk": r.get("rupees_at_risk", 0),
                        "latest": last
                        and {
                            k: last[k]
                            for k in (
                                "tick",
                                "ts",
                                "clip",
                                "score",
                                "threshold",
                                "risk",
                                "match_cos",
                                "match_level",
                                "mode",
                                "mix",
                            )
                        },
                        "history": [
                            {
                                "tick": h["tick"],
                                "ts": h["ts"],
                                "risk": round(h["risk"], 3),
                                "match": h["match_cos"] is not None and h["match_cos"] > h["match_level"],
                            }
                            for h in hist
                        ],
                        "band_z": self.cache.get(mid, {}).get("band_z"),
                        "operating": self._operating(mid, last),
                        "maintenance": self._maintenance(mid),
                    }
                )
            feed = getattr(getattr(self, "brain", None), "feed", None)
            for m in machines:  # a machine the live recorder feeds: its view comes from its live readings
                live = feed is not None and feed.is_live(m["id"])
                m.update(feed.twin_entry(m["id"]) if live else {"live": None})
                if live:
                    m["band_z"] = (self.cache.get(m["id"]) or {}).get("band_z")
            calls = [
                dict(r)
                for r in self.c.db.execute(
                    "SELECT id, run_id, ts, actor, tool, input, output FROM tool_calls ORDER BY id DESC LIMIT 40"
                )
            ]
            return {
                "version": self.version,
                "tick": self.c.tick_no(),
                "clock": self.c.now().isoformat(timespec="minutes") if self.c.tick_no() >= 0 else None,
                "tick_hours": self.c.cfg["tick_hours"],
                "machines": machines,
                "fingerprints": registry.fingerprints(self.c.reg),
                "pending_label": self.pending_label,
                "messages": self.messages(),
                "route": self.route(),
                "tool_calls": calls,
                "events": self.events,
                "replay": {**self.replay, "started": None, "t": self.replay_t()},
                "link": {"up": self.link_up, "pending": len(self.outbox.pending())},
                "alerts": self.alerts.notifier.recent(20),
                "whatsapp": {"provider": self.alerts.provider},
                "demo": self.demo,
            }

    def reading(self, mid: str, tick: int | None = None) -> dict[str, Any]:
        """Rebuild one past reading exactly (same clips, same mix) and slice it like the live stream."""
        with self.lock:
            hist = self.c.history(mid)
            if not hist:
                raise HTTPException(404, "no reading yet")
            row = hist[-1] if tick is None else next((h for h in hist if h["tick"] == tick), None)
            if row is None:
                raise HTTPException(404, f"no reading {tick} for {mid}")
            key = (mid, row["tick"])
            if key not in self._readings:
                audio, _ = self.c.audio(mid, row["tick"], row["mode"], row["mix"])
                r = self._score(mid, row["tick"], audio)
                self._readings[key] = slices(audio, r, self.c.model(self.c.machines[mid]["type"]))
                if len(self._readings) > 256:
                    self._readings.pop(next(iter(self._readings)))
            return {
                "machine": mid,
                "tick": row["tick"],
                "ts": row["ts"],
                "mode": row["mode"],
                "mix": row["mix"],
                "clip": row["clip"],
                "score": row["score"],
                "threshold": row["threshold"],
                "risk": row["risk"],
                **{k: v for k, v in self._readings[key].items() if k != "risk"},
                "risk_slices": self._readings[key]["risk"],
            }

    def _forecast(self, mid: str, hist: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not hist:
            return None
        src = self.c.machines[mid]["source"]
        est = forecast.public(forecast.from_history(hist, self.c.cfg["tick_hours"], src))
        return {**est, "fault_level_clips": forecast.fault_levels().get(src, {}).get("n")}

    def history(self, mid: str, days: float = TIME_MACHINE_DAYS) -> dict[str, Any]:
        """Each reading of the last `days`, with the status and estimate as they stood at that moment."""
        with self.lock:
            hist = self.c.history(mid)
        m, th = self.c.machines[mid], self.c.cfg["tick_hours"]
        rows = []
        for i, h in enumerate(hist):
            risk3, _, status = agent.status_of(hist[max(0, i - 2) : i + 1])
            rows.append(
                {
                    "tick": h["tick"],
                    "ts": h["ts"],
                    "risk": round(h["risk"], 3),
                    "risk3": round(risk3, 3),
                    "status": status,
                    "mode": h["mode"],
                    "mix": round(h["mix"], 3),
                    "match": h["match_cos"] is not None and h["match_cos"] > h["match_level"],
                }
            )
        warn = next((r for r in rows if r["status"] in ("alarm", "early_match")), None)
        broke = next((r for r in rows if r["mode"] == "fault" or r["mix"] >= 1.0), None)
        keep = int(days * 24 / th)
        for i in range(max(0, len(rows) - keep), len(rows)):
            rows[i]["forecast"] = forecast.public(forecast.from_history(hist[: i + 1], th, m["source"]))
        return {
            "machine": mid,
            "days": days,
            "tick_hours": th,
            "rows": rows[-keep:],
            "first_warning": warn and {k: warn[k] for k in ("tick", "ts", "status")},
            "fault_sound": broke and {k: broke[k] for k in ("tick", "ts", "mode", "mix")},
            "fault_level": forecast.fault_levels().get(m["source"])
            or {"level": forecast.EXTRA.get(m["source"], {}).get("level"), "healthy": None, "n": None},
        }

    def _audio_of(self, mid: str, row: dict[str, Any]) -> np.ndarray:
        audio, _ = self.c.audio(mid, row["tick"], row["mode"], row["mix"])
        return audio

    def explain(self, mid: str) -> dict[str, Any]:
        """Why this machine is flagged, by sound: band, change vs its earliest normal reading, words."""
        with self.lock:
            hist = self.c.history(mid)
            if not hist:
                raise HTTPException(404, "no reading yet")
            now = hist[-1]
            key = (mid, now["tick"])
            if key in self._explained:
                return self._explained[key]
            before = next((h for h in hist if h["mode"] == "normal"), None)
            m = self.c.machines[mid]
            now_audio = self._audio_of(mid, now)
            if mid in self.c.ims:  # vibration: compare each band with the bearing's own first reading
                before = hist[0]
                b_mel = features.log_mel(self._audio_of(mid, before))
                band_z = (
                    (features.log_mel(now_audio).mean(axis=1) - b_mel.mean(axis=1))
                    / (b_mel.std(axis=1) + 1e-6)
                ).tolist()
            else:
                band_z = self.c.scorer(mid).score_audio(now_audio, m["source"])["band_z"]
            if before is None or before["tick"] == now["tick"]:
                out: dict[str, Any] = {"status": "no_normal_reading"}
            else:
                out = sound.explain(now_audio, self._audio_of(mid, before), band_z, self.mel_hz)
            fp = next((f for f in reversed(registry.fingerprints(self.c.reg)) if f["machine"] == mid), None)
            rank = next((x for x in self.ranking() if x["machine_id"] == mid), {})
            out.update(
                machine=mid,
                now={k: now[k] for k in ("tick", "ts", "clip", "mode", "mix")},
                before=before and {k: before[k] for k in ("tick", "ts", "clip", "mode", "mix")},
                owner_said=(fp or (rank.get("registry") or {})).get("owner_label"),
                fault=rank.get("known_fault"),
            )
            self._explained[key] = out
            return out

    def explain_clip(self, mid: str, which: str) -> np.ndarray:
        ex = self.explain(mid)
        if ex.get("status") != "ok":
            raise HTTPException(404, "no clear band to play")
        row = ex["now"] if which == "now" else ex["before"]
        with self.lock:
            audio = self._audio_of(mid, row)
        return sound.cut(audio, ex["start_s"])

    def savings(self, factory: str) -> dict[str, Any]:
        """Numbers for the owner's savings report: approved visits, each counted once, and what is at risk now."""
        if factory not in self.c.factories:
            raise HTTPException(404, "unknown factory")
        with self.lock:
            f = self.c.factories[factory]
            rank = {r["machine_id"]: r for r in self.ranking()}
            route = self.route()
            legs = {leg["machine"]: leg for leg in (route or {}).get("legs", [])}
            visits, seen = [], set()
            for msg in self.messages():
                mid = msg["machine"]
                if msg["factory"] != factory or msg["approved"] != 1 or mid in seen:
                    continue
                seen.add(mid)
                m = self.c.machines[mid]
                h = self.history(mid)
                r = rank.get(mid, {})
                cost = costs.breakdown(
                    factory, m["type"], r.get("known_fault"), self.c.cfg["service_hours"][m["type"]]
                )
                reg = r.get("registry") or {}
                if (h["first_warning"] or {}).get("status") == "early_match":
                    fault = (reg.get("fault") or "a").replace("_", " ")
                    how = f"sounded like the {fault} fault confirmed at {reg.get('from_area', 'another factory')}, before its own alarm"
                else:
                    how = "its own sound crossed its alarm level"
                    if r.get("known_fault"):
                        how += f"; owner's word: {r['known_fault'].replace('_', ' ')}"
                visits.append(
                    {
                        "machine": mid,
                        "name": m["name"],
                        "first_warning": h["first_warning"] and h["first_warning"]["ts"].replace("T", " "),
                        "how": how,
                        "visit": (legs.get(mid) or {}).get("arrive", msg["visit_day"]).replace("T", " "),
                        **cost,
                    }
                )
            keys = ("output_inr", "part_markup_inr", "wages_inr", "saved_inr", "downtime_avoided_h")
            totals = {k: sum(v[k] for v in visits) for k in keys}
            watch = []
            for mid, m in self.c.machines.items():
                if m["factory"] != factory or mid not in rank:
                    continue
                est = rank[mid]["days_to_fault_level"]
                days = {
                    "estimate": (
                        f"likely {est['low']}-{est['high']} days"
                        if est.get("high") is not None
                        else f"at least {est['low']} days"
                    ),
                    "at_fault_level": "sounds like its recorded fault now",
                    "no_trend": "no upward trend",
                    "too_few": "too few readings yet",
                    "not_separable": "no estimate: faults score below alarm here",
                }[est["status"]]
                watch.append(
                    {
                        "machine": mid,
                        "name": m["name"],
                        "status": rank[mid]["status"],
                        "days": days,
                        "rupees_at_risk": rank[mid]["rupees_at_risk"],
                    }
                )
            c = costs.load()
            first, last = self.c.now(0), self.c.now()
            month = last.strftime("%B %Y")
            period = (
                f"{month} (month to date: readings {first:%d %b} - {last:%d %b %Y})"
                if first.month == last.month
                else f"readings {first:%d %b %Y} - {last:%d %b %Y}"
            )
            return {
                "factory": f,
                "period_label": period,
                "visits": visits,
                "totals": totals,
                "watch": watch,
                "assumptions": [
                    ("lost output per hour, this factory", f"Rs {c['output_per_hour_inr'][factory]:,}"),
                    (
                        "unplanned downtime (pump / blower / gearbox)",
                        " / ".join(
                            f"{c['unplanned_downtime_hours'][t]} h" for t in ("pump", "fan", "gearbox")
                        ),
                    ),
                    (
                        "idle workers (pump / blower / gearbox)",
                        " / ".join(str(c["workers_idle"][t]) for t in ("pump", "fan", "gearbox")),
                    ),
                    ("wage per worker-hour", f"Rs {c['wage_per_hour_inr']}"),
                    (
                        "emergency part markup",
                        f"{round(c['emergency_part_markup'] * 100)}% on MOCK dealer prices",
                    ),
                    (
                        "planned service time",
                        " / ".join(f"{self.c.cfg['service_hours'][t]} h" for t in ("pump", "fan", "gearbox")),
                    ),
                ],
                "generated": time.strftime("%Y-%m-%d %H:%M"),
                "source": "node.db (readings, messages), registry.db, data/sim/costs.json",
            }

    def _operating(self, mid: str, last: dict[str, Any] | None) -> dict[str, Any]:
        """Running and sensor state for the twin. `running` is None when nothing is known (sensor lost)."""
        run, sensor = self.c.operating(mid)
        now = self.c.tick_no()
        since = None if last is None else now - last["tick"]
        if sensor == "lost":
            sensor_status, running = "lost", None
        elif last is None:
            sensor_status, running = "none", None
        else:
            sensor_status, running = ("live" if since == 0 else "stale"), run == "running"
        if run == "stopped" and sensor == "ok":
            running = False
        return {"running": running, "run": run, "sensor": sensor_status,
                "last_ts": last["ts"] if last else None, "ticks_since": since,
                "simulated": run != "running" or sensor != "ok"}  # fmt: skip

    def _maintenance(self, mid: str) -> dict[str, Any]:
        """Booked jobs and verified repairs for this machine (from the alert log and signed records)."""
        from . import prove

        jobs = [a for a in self.alerts.notifier.recent(50) if a["machine"] == mid and a["kind"] == "job"]
        try:
            recs = prove.records(self, mid)
        except Exception:
            recs = []
        last = recs[-1]["record"] if recs else None
        return {"job_booked": bool(jobs), "job_status": jobs[0]["status"] if jobs else None,
                "last_repair": last and {"at": last.get("repaired_at"),
                                         "outcome": (last.get("verification") or {}).get("outcome")}}  # fmt: skip

    def set_operating(self, mid: str, run: str | None, sensor: str | None) -> dict[str, Any]:
        if mid not in self.c.machines:
            raise HTTPException(404, "unknown machine")
        with self.lock:
            try:
                r, sn = self.c.set_operating(mid, run, sensor)
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
            if (r, sn) != ("running", "ok"):
                self.cache.pop(mid, None)
            self.event(
                "operating", machine=mid, run=r, sensor=sn, simulated=True
            )  # the change itself was simulated
            self.version += 1
            return {"machine": mid, "run": r, "sensor": sn, "simulated": (r, sn) != ("running", "ok")}

    def meta(self) -> dict[str, Any]:
        cfg = self.c.cfg
        return {
            "factories": cfg["factories"],
            "technician": cfg["technician"],
            "mel_hz": self.mel_hz,
            "slices": SLICES,
            "frame_s": FRAME_S,
            "fault_machine": FAULT_MACHINE,
            "similar_machine": SIMILAR_MACHINE,
            "nominal_rpm": cfg.get("nominal_rpm", {}),
            "rpm_note": cfg.get("_rpm_note", ""),
            "has_llm": provider.available(),
            "llm": {"provider": "ollama", "model": provider.chat_model()},
            "note": cfg.get("_note", ""),
            "eval": latest_eval(),
        }

    def frame(self) -> dict[str, Any]:
        i = self.cursor
        out = {}
        for mid, a in self.cache.items():
            out[mid] = {"risk": a["risk"][i], "energy": a["energy"][i], "spec": a["spec"][i]}
        return {"type": "frame", "tick": self.tick, "slice": i, "machines": out}

    # -- actions --------------------------------------------------------------
    def step(self, n: int = 1) -> None:
        with self.lock:
            scored = None
            for _ in range(n):
                scored = self.c.step()
            self._analyse(scored)
            self.alerts.release_due()  # quiet hours / power cuts end and the daily digest, on the factory clock
            rank = self.ranking()
            for r in rank:
                was = self.prev_status.get(r["machine_id"])
                if r["status"] == "early_match" and was != "early_match":
                    src = None
                    fid = self.c.history(r["machine_id"], 1)[0]["match_fp"]
                    if fid is not None:
                        src = next(
                            (f["machine"] for f in registry.fingerprints(self.c.reg) if f["id"] == fid), None
                        )
                    self.event(
                        "early_match",
                        machine=r["machine_id"],
                        source=src,
                        cosine=r["registry"]["cosine"],
                        level=r["registry"]["flag_level"],
                        fault=r["registry"]["fault"],
                    )
                if r["status"] == "alarm" and was not in ("alarm", None):
                    self.event("alarm", machine=r["machine_id"], risk=r["risk"])
            self.prev_status = {r["machine_id"]: r["status"] for r in rank}
            self.event("reading", tick=self.c.tick_no())

    def set_mode(self, mid: str, mode: str) -> None:
        if mid not in self.c.machines or mode not in ("normal", "fault", "onset"):
            raise HTTPException(400, "unknown machine or mode")
        with self.lock:
            self.c.set_mode(mid, mode, 0.0)
            self.event("mode", machine=mid, mode=mode, synthetic=mode == "onset")

    def label(
        self, text: str | None = None, factory: str = "A", machine: str = FAULT_MACHINE
    ) -> dict[str, Any]:
        """Owner's note -> weak label. No text: the sample voice note through local Whisper."""
        lang = None
        if not text:
            text, lang = folk.transcribe(str(VOICE_NOTE), language=self.c.factories[factory]["language"])
        llm = folk.llm_label if provider.available() else None
        lab = folk.label_text(text, folk.load_lexicon(), llm)
        with self.lock:
            wid = registry.add_weak_label(self.c.reg, factory, machine, text, lang, lab)
            self.pending_label = {
                **lab,
                "id": wid,
                "text": text,
                "machine": machine,
                "factory": factory,
                "via": "whisper" if lang else "typed",
            }
            self.event("label", **self.pending_label)
        return self.pending_label

    def publish(self, last: int = 4) -> dict[str, Any]:
        """Owner confirmed the label: queue the fingerprint for the registry and try to send it."""
        with self.lock:
            lab = self.pending_label
            if not lab or lab["fault"] == "unknown":
                raise HTTPException(400, "no usable label to confirm")
            mid = lab["machine"]
            payload = {
                "machine_type": self.c.machines[mid]["type"],
                "fault": lab["fault"],
                "owner_label": lab["sound"],
                "factory": lab["factory"],
                "machine": mid,
                "embedding": self.c.confirmed_embedding(mid, last),
                "weak_label_id": lab["id"],
            }
            idem = self.outbox.put("fingerprint", payload)
            self.pending_label = None
            return {"queued": idem, **self.sync()}

    def _send(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        """In-process transport to the registry; fails like a dead network while the link is down."""
        if not self.link_up:
            raise ConnectionError("no signal at the shed")
        p = payload
        fid = registry.publish(
            self.c.reg,
            p["machine_type"],
            p["fault"],
            p["owner_label"],
            p["factory"],
            p["machine"],
            p["embedding"],
            p.get("weak_label_id"),
            p["idem"],
        )
        return {"id": fid}

    def sync(self, force: bool = False) -> dict[str, Any]:
        """Flush the outbox; every fingerprint that arrives is checked against other sheds."""
        with self.lock:
            out = self.outbox.flush(force=force)
            published = [self._published(i["result"]["id"], i["payload"]) for i in out["sent"]]
            if out["pending"]:
                self.event("queued", pending=out["pending"])
            return {"published": published, "pending": out["pending"], "rejected": out["rejected"]}

    def _published(self, fid: int, p: dict[str, Any]) -> dict[str, Any]:
        mid, fp = p["machine"], p["embedding"]
        m = self.c.machines[mid]
        checks = []  # what the registry says about each same-type machine elsewhere, right now
        for oid, o in self.c.machines.items():
            if o["type"] != m["type"] or o["factory"] == m["factory"] or not self.c.history(oid):
                continue
            emb = json.loads(self.c.history(oid, 1)[0]["embedding"])
            cos = float(embed.cosine(emb, [fp])[0])
            level = self.c.model(o["type"]).match_level(o["source"], fp)
            checks.append(
                {
                    "machine": oid,
                    "cosine": round(cos, 3),
                    "level": round(level, 3),
                    "match": cos > level,
                    "same_model": o["source"] == m["source"],
                }
            )
        self.event(
            "published", fingerprint=fid, machine=mid, fault=p["fault"], sound=p["owner_label"], checks=checks
        )
        return {"fingerprint": fid, "machine": mid, "checks": checks}

    def set_link(self, up: bool) -> dict[str, Any]:
        with self.lock:
            self.link_up = up
            self.event("link", up=up)
            if up:
                return self.sync(force=True)
            return {"published": [], "pending": len(self.outbox.pending()), "rejected": []}

    def run_agent(self, engine: str = "rules") -> dict[str, Any]:
        with self.lock:
            before = self.c.db.execute("SELECT COALESCE(MAX(id), 0) FROM tool_calls").fetchone()[0]
            run_id, note = agent.run_llm(self.c) if engine == provider.NAME else agent.run_rules(self.c)
            for t in self.c.db.execute(
                "SELECT id, tool, input FROM tool_calls WHERE id>? ORDER BY id", (before,)
            ):
                self.event(
                    "tool_call", id=t["id"], tool=t["tool"], input=json.loads(t["input"]), run_id=run_id
                )
            planned = any(
                e["kind"] == "tool_call" and e["tool"] == "plan_technician_route" and e["run_id"] == run_id
                for e in self.events
            )
            if planned:
                self.event("route", run_id=run_id, machines=[leg["machine"] for leg in self.route()["legs"]])
            self.event("digest", run_id=run_id, note=note)
        # each owner message also goes out on WhatsApp (or is logged as dry run / no contact)
        sent = self.alerts.after_digest(run_id)
        for a in sent:
            self.event(
                "whatsapp",
                machine=a["machine"],
                status=a["status"],
                channel=a["channel"],
                severity=a["severity"],
            )
        return {"run_id": run_id, "note": note, "alerts": [a["id"] for a in sent]}

    def reply(self, message_id: int, text: str) -> dict[str, Any]:
        with self.lock:
            ok = agent.record_reply(self.c, message_id, text)
            row = self.c.db.execute("SELECT ask FROM messages WHERE id=?", (message_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "no such message")
            self.event(
                "reply", message_id=message_id, machine=row["ask"].split("|")[0], reply=text, approved=ok
            )
            return {"approved": ok}

    def reset(self) -> None:
        with self.lock:
            self.c.reset()
            self._end_ims()
            self.cache, self.pending_label, self.prev_status, self.tick = {}, None, {}, -1
            self._readings, self._explained = {}, {}
            self.outbox.db.execute("DELETE FROM outbox")
            self.outbox.db.commit()
            self.alerts.notifier.clear()  # WhatsApp alerts from an earlier replay must not show in this one
            self.alerts.expert.clear()
            self.link_up = True
            self.events = []
            self.event("reset")

    # -- Demo mode: a real bearing failure (NASA IMS) on Loom 1, and a real WhatsApp alert ----------
    def _fault_name(self, mid: str, owner_word: str | None) -> dict[str, str] | None:
        """The fault the diagnosis card names (classifier first, then the owner's word), in hi/mr/en."""
        key = diagnosis.fault_for(owner_word, self.alerts.classified.get(mid))
        return diagnosis.load()["faults"][key]["name"] if key else None

    def _end_ims(self) -> None:
        if self._ims_saved is not None:
            self.c.cfg.update(self._ims_saved["cfg"])
            for mid, src in self._ims_saved["sources"].items():
                self.c.machines[mid]["source"] = src
            self._ims_saved = None
        self.c.ims.clear()
        self.alerts.contact_override.clear()
        self.alerts.lang_override.clear()
        self.alerts.classified.clear()

    async def run_ims_demo(self, to: str | None, lang: str | None = None, tick_s: float = 0.45) -> None:
        """Replay NASA IMS test 2, bearing 1 (outer race failure, Feb 2004) as Loom 1's motor bearing.

        At the first alarm the CWRU-trained classifier names the fault from the real vibration, the rules
        agent plans the visit, and the WhatsApp alert goes to `to` (a judge's phone) in `lang`.
        """
        from .imsdemo import ImsSource

        src = await asyncio.to_thread(ImsSource)
        await asyncio.to_thread(self.reset)
        mid = IMS_MACHINE
        with self.lock:
            m = self.c.machines[mid]
            self._ims_saved = {
                "cfg": {k: self.c.cfg[k] for k in ("start", "tick_hours")},
                "sources": {mid: m["source"]},
            }
            first = datetime.strptime(src.meta["first_snapshot"], "%Y.%m.%d.%H.%M.%S")
            first += timedelta(days=src.start_day)  # the clock starts after the learning window
            self.c.cfg.update(start=first.isoformat(), tick_hours=src.tick_hours)
            m["source"] = src.unit
            forecast.register(
                src.unit,
                src.meta["failure_level"],
                src.meta["window_days"],
                log=True,
                ratios_log10=src.meta.get("ratios_log10"),
            )
            self.c.ims[mid] = src
            self.c.set_mode(mid, "ims")
            if to:
                self.alerts.contact_override[mid] = to
            if lang:
                self.alerts.lang_override[mid] = lang
        self.demo.update(
            on=True,
            phase="replaying NASA IMS test 2",
            machine=mid,
            alerted=False,
            readings=src.readings,
            at=0,
        )
        self.version += 1
        try:
            for k in range(src.readings):
                t0 = time.monotonic()
                await asyncio.to_thread(self.step)
                self.demo["at"] = k + 1
                row = next(r for r in self.ranking() if r["machine_id"] == mid)
                if self.demo["alerted"] or row["status"] == "alarm":
                    # name the fault from the last hour of vibration; after the alarm, every reading
                    # (the classifier's answer changes as the damage grows; the UI shows the latest)
                    guess = src.classify(k)  # computed from the real vibration by edge/ims.py
                    self.alerts.classified[mid] = guess["class"]
                    first_alarm = not self.demo["alerted"]
                    if first_alarm or guess["class"] != (self.demo.get("classified") or {}).get("class"):
                        self.event("classified", machine=mid, **guess, true_fault=src.meta["true_fault"])
                    self.demo.update(classified=guess)
                    if first_alarm:
                        self.demo.update(phase="alert", alerted=True, first_classified=guess, alarm_at=k)
                        await asyncio.to_thread(self.run_agent, "rules")
                        from . import owner_agent  # the owner's agent acts on the alarm by itself

                        await asyncio.to_thread(owner_agent.on_alarm, self, mid)
                await asyncio.sleep(max(0.0, tick_s - (time.monotonic() - t0)))
            self.demo["phase"] = "bearing failed (end of IMS test 2)"
        except asyncio.CancelledError:
            self.demo["phase"] = "stopped"
            raise
        except Exception as e:
            self.demo["phase"] = f"error: {e}"
            raise
        finally:
            self.demo["on"] = False
            self.version += 1

    # -- demo replay ----------------------------------------------------------
    def _seed(self, days: float) -> None:
        """Record `days` of history quickly; A-F1's bearing sound starts growing (SYNTHETIC) on day 6."""
        n = int(days * 24 / self.c.cfg["tick_hours"])
        for k in range(n):
            with self.lock:
                if k == SEED_ONSET_AT:
                    self.c.set_mode(FAULT_MACHINE, "onset", 0.0, SEED_ONSET_STEP)
                self.c.step()
            self.replay["progress"] = round((k + 1) / n, 3)
            if k % 3 == 2:
                self.version += 1
        with self.lock:
            self._analyse()
            self.prev_status = {r["machine_id"]: r["status"] for r in self.ranking()}
            self.event("seeded", days=days, readings=n)

    async def run_replay(self, tick_s: float = SLICES * FRAME_S, seed_days: float = SEED_DAYS) -> None:
        """Scripted demo on recorded MIMII clips: 14 recorded days, then live with A-F1's fault at 0:40."""
        await asyncio.to_thread(self.reset)
        self.replay.update(on=True, started=None, phase="recording 14 days", progress=0.0)
        self.version += 1
        label_task = None
        try:
            await asyncio.to_thread(self._seed, seed_days)
            self.replay.update(started=time.monotonic(), phase="live", progress=1.0)
            for k in range(REPLAY_MAX + 1):
                t0 = time.monotonic()
                if k == REPLAY_FAULT_AT:
                    self.replay["phase"] = "fault"
                    await asyncio.to_thread(self.set_mode, FAULT_MACHINE, "fault")
                if k == REPLAY_LABEL_AT:
                    self.replay["phase"] = "voice note"
                    label_task = asyncio.create_task(asyncio.to_thread(self.label))
                if k == REPLAY_PUBLISH_AT:
                    if label_task:
                        await label_task
                    self.replay["phase"] = "published"
                    await asyncio.to_thread(self.publish, REPLAY_PUBLISH_AT - REPLAY_FAULT_AT)
                    await asyncio.to_thread(self.set_mode, SIMILAR_MACHINE, "onset")
                await asyncio.to_thread(self.step)
                b = next(m for m in self.ranking() if m["machine_id"] == SIMILAR_MACHINE)
                if k > REPLAY_PUBLISH_AT and (b["status"] == "early_match" or k == REPLAY_MAX):
                    await asyncio.sleep(min(2.5, tick_s))  # let the match pulse land before the route
                    self.replay["phase"] = "digest"
                    await asyncio.to_thread(self.run_agent, "rules")
                    break
                await asyncio.sleep(max(0.0, tick_s - (time.monotonic() - t0)))
            self.replay["phase"] = "awaiting owner"
            self.version += 1
        except asyncio.CancelledError:
            self.replay["phase"] = "stopped"
            raise
        except Exception as e:  # show it in the UI instead of dying silently
            self.replay["phase"] = f"error: {e}"
            raise
        finally:
            self.replay["on"] = False
            self.version += 1


class Hub:
    def __init__(self, live: Live) -> None:
        self.live, self.clients = live, set()

    async def send_all(self, msg: dict[str, Any]) -> None:
        dead = []
        text = json.dumps(msg, ensure_ascii=False)
        for ws in list(self.clients):
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    async def loop(self) -> None:
        """2 Hz: frame every tick; state whenever something changed."""
        sent_version, sent_tick = -1, None
        last_alert_tick = time.monotonic()
        while True:
            lv = self.live
            if time.monotonic() - last_alert_tick > 60:  # escalations and queued alerts, once a minute
                last_alert_tick = time.monotonic()
                await asyncio.to_thread(lv.alerts.tick)
            if lv.tick != sent_tick:
                lv.cursor, sent_tick = 0, lv.tick
            if self.clients:
                if lv.version != sent_version:
                    sent_version = lv.version
                    st = await asyncio.to_thread(lv.state)
                    await self.send_all({"type": "state", "state": st})
                if lv.cache:
                    await self.send_all(lv.frame())
            lv.cursor = (lv.cursor + 1) % SLICES
            await asyncio.sleep(FRAME_S)


class StepIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n: int = 1


class ModeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    machine: str
    mode: str


class LabelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str | None = None


class AgentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    engine: str = "rules"


class ReplyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: int
    reply: str


class DemoIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    on: bool = True
    to: str | None = Field(default=None, max_length=24)
    lang: str | None = Field(default=None, pattern="^(" + "|".join(diagnosis.LANGS) + ")$")


class AlertReplyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone: str
    text: str


class OperatingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run: str | None = None
    sensor: str | None = None


class LinkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    up: bool


class ReplayIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    on: bool


def create_app(node_db: Path | None = None, registry_db: Path | None = None, stream: bool = True) -> FastAPI:
    live = Live(node_db, registry_db)
    hub = Hub(live)
    tasks = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if stream:
            tasks["loop"] = asyncio.create_task(hub.loop())
        yield
        for t in tasks.values():
            t.cancel()

    app = FastAPI(title="Dhadkan live", lifespan=lifespan)
    app.state.live = live
    from . import cc_api, ingest, mill_api
    from .brain import Brain

    brain = Brain(live, Path(node_db or NODE_DB).parent)
    app.state.brain = brain
    app.include_router(mill_api.router())
    app.include_router(ingest.router(brain=brain))
    app.include_router(cc_api.router(brain))
    from .a2a import router as a2a_router

    app.include_router(a2a_router.router(live))

    def busy() -> None:
        if live.replay["on"]:
            raise HTTPException(409, "demo replay is running; switch it off first")

    @app.get("/api/meta")
    def meta() -> dict[str, Any]:
        return live.meta()

    @app.get("/api/state")
    def state() -> dict[str, Any]:
        return live.state()

    @app.post("/api/step")
    async def step(body: StepIn) -> dict[str, Any]:
        busy()
        await asyncio.to_thread(live.step, max(1, min(body.n, 9)))
        return {"tick": live.tick}

    @app.post("/api/mode")
    def mode(body: ModeIn) -> dict[str, Any]:
        busy()
        live.set_mode(body.machine, body.mode)
        return {"ok": True}

    @app.post("/api/label")
    async def label(body: LabelIn) -> dict[str, Any]:
        busy()
        return await asyncio.to_thread(live.label, body.text)

    @app.post("/api/publish")
    def publish() -> dict[str, Any]:
        busy()
        return live.publish()

    @app.post("/api/machine/{mid}/operating")
    def operating(mid: str, body: OperatingIn) -> dict[str, Any]:
        """SIMULATED: stop/start a machine or drop/restore its sensor (demo controls, labelled on screen)."""
        return live.set_operating(mid, body.run, body.sensor)

    @app.post("/api/link")
    def link(body: LinkIn) -> dict[str, Any]:
        """Simulate the shed losing / regaining signal (the outbox holds fingerprints meanwhile)."""
        return live.set_link(body.up)

    @app.post("/api/agent")
    async def run_agent(body: AgentIn) -> dict[str, Any]:
        busy()
        if body.engine not in ("rules", provider.NAME):
            raise HTTPException(400, f"engine is rules or {provider.NAME}")
        if body.engine == provider.NAME and not provider.available():
            raise HTTPException(400, "no local LLM: start Ollama and pull " + provider.chat_model())
        return await asyncio.to_thread(live.run_agent, body.engine)

    @app.post("/api/reply")
    def reply(body: ReplyIn) -> dict[str, Any]:
        return live.reply(body.message_id, body.reply)

    @app.post("/api/reset")
    def reset() -> dict[str, Any]:
        busy()
        live.reset()
        return {"ok": True}

    @app.post("/api/replay")
    async def replay(body: ReplayIn) -> dict[str, Any]:
        t = tasks.get("replay")
        if t and not t.done():
            t.cancel()
            with suppress(asyncio.CancelledError):
                await t
        if body.on:
            tasks["replay"] = asyncio.create_task(live.run_replay())
        return {"on": body.on}

    @app.get("/api/audio/{mid}/{kind}.wav")
    def audio(mid: str, kind: str, tick: int | None = None) -> Response:
        """live: the 10 s the model scored last. normal: a held-out normal clip of the same unit."""
        if mid not in live.c.machines or kind not in ("live", "normal"):
            raise HTTPException(404)
        m = live.c.machines[mid]
        if kind == "normal":
            ref = next(
                c
                for c in data.clips_for(m["type"])
                if c.unit == m["source"] and c.split == "val" and c.label == 0
            )
            return Response(
                _wav(features.load(ref.path)),
                media_type="audio/wav",
                headers={
                    "X-Clip": f"{ref.path.parent.name}/{ref.path.name}"
                },  # DCASE reuses names across splits
            )
        with live.lock:
            hist = live.c.history(mid, 1)
            if not hist:
                raise HTTPException(404, "no reading yet")
            h = hist[0]
            if tick is not None:  # a past reading, for the time machine
                h = next((x for x in live.c.history(mid) if x["tick"] == tick), None)
                if h is None:
                    raise HTTPException(404, f"no reading {tick}")
            a, name = live.c.audio(mid, h["tick"], h["mode"], h["mix"])
        return Response(_wav(a), media_type="audio/wav", headers={"X-Clip": name})

    @app.get("/api/reading/{mid}")
    def reading(mid: str, tick: int | None = None) -> dict[str, Any]:
        if mid not in live.c.machines:
            raise HTTPException(404, "unknown machine")
        return live.reading(mid, tick)

    @app.get("/api/history/{mid}")
    def history(mid: str) -> dict[str, Any]:
        if mid not in live.c.machines:
            raise HTTPException(404, "unknown machine")
        return live.history(mid)

    @app.get("/api/explain/{mid}")
    def explain(mid: str) -> dict[str, Any]:
        if mid not in live.c.machines:
            raise HTTPException(404, "unknown machine")
        return live.explain(mid)

    @app.get("/api/explain/{mid}/{which}.wav")
    def explain_clip(mid: str, which: str) -> Response:
        if mid not in live.c.machines or which not in ("before", "now"):
            raise HTTPException(404)
        return Response(_wav(live.explain_clip(mid, which)), media_type="audio/wav")

    @app.get("/api/report/{factory}.pdf")
    def report_pdf(factory: str) -> Response:
        pdf = report.render(live.savings(factory))
        name = f"dhadkan-savings-{factory}.pdf"
        return Response(
            pdf, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{name}"'}
        )

    @app.get("/api/report/{factory}")
    def report_json(factory: str) -> dict[str, Any]:
        return live.savings(factory)

    @app.post("/api/demo")
    async def demo(body: DemoIn) -> dict[str, Any]:
        """Demo mode: replay the NASA IMS bearing failure on Loom 1 and alert `to` on WhatsApp."""
        for name in ("replay", "demo"):
            t = tasks.get(name)
            if t and not t.done():
                t.cancel()
                with suppress(asyncio.CancelledError):
                    await t
        if not body.on:
            await asyncio.to_thread(live.reset)
            return {"on": False}
        if not (MODELS / "ims_replay.json").exists():
            raise HTTPException(
                409, "IMS data not prepared: run python -m data.real ims && python -m edge.ims"
            )
        to = body.to or env.get("DHADKAN_DEMO_TO")
        tasks["demo"] = asyncio.create_task(live.run_ims_demo(to, body.lang))
        return {"on": True, "to": bool(to), "provider": live.alerts.provider}

    @app.get("/api/alert-media/{name}")
    def alert_media(name: str) -> Response:
        path = ALERT_DIR / name
        if "/" in name or ".." in name or not path.is_file() or path.suffix not in (".ogg", ".png"):
            raise HTTPException(404)
        return Response(path.read_bytes(), media_type="audio/ogg" if path.suffix == ".ogg" else "image/png")

    @app.get("/api/diagnosis/{mid}")
    def diagnosis_card(mid: str, lang: str | None = None) -> dict[str, Any]:
        if mid not in live.c.machines:
            raise HTTPException(404, "unknown machine")
        lang = lang or live.c.factories[live.c.machines[mid]["factory"]]["language"]
        card = live.alerts.card(mid, lang)
        return {"card": card, "text": live.alerts.text(mid, lang) if card else None}

    class RepairIn(BaseModel):
        model_config = ConfigDict(extra="forbid")
        mechanic: str | None = Field(default=None, max_length=60)
        part: str | None = Field(default=None, max_length=120)

    @app.post("/api/repair/{mid}")
    def repair(mid: str, body: RepairIn) -> dict[str, Any]:
        """After a repair: the 60 s re-record (demo: the unit's own normal recordings), verified, signed."""
        from . import prove

        if mid not in live.c.machines:
            raise HTTPException(404)
        after, note = prove.demo_after_readings(live, mid)
        rec = prove.make_record(live, mid, after, body.mechanic, body.part, demo_note=note)
        live.event("repair_verified", machine=mid, outcome=rec["record"]["verification"]["outcome"])
        return rec

    @app.get("/api/records")
    def list_records(machine: str | None = None) -> list[dict[str, Any]]:
        from . import prove

        return [{k: v for k, v in r.items() if k != "mechanic"} for r in prove.records(live, machine)]

    @app.get("/api/records/public-key")
    def records_public_key() -> Response:
        from . import prove

        return Response(prove.public_key_pem(), media_type="application/x-pem-file")

    class VerifyIn(BaseModel):
        record: dict[str, Any]
        signature: str = Field(max_length=200)

    @app.post("/api/records/verify")
    def verify_record(body: VerifyIn) -> dict[str, bool]:
        from . import prove

        return {"valid": prove.verify_signature(body.record, body.signature)}

    @app.get("/api/records/{rid}.pdf")
    def record_pdf(rid: int) -> Response:
        from . import prove

        rec = next((r for r in prove.records(live) if r["id"] == rid), None)
        if rec is None:
            raise HTTPException(404)
        return Response(prove.record_pdf(rec), media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="repair-{rid}.pdf"'})  # fmt: skip

    @app.get("/api/passport/{mid}")
    def passport(mid: str) -> dict[str, Any]:
        from . import prove

        if mid not in live.c.machines:
            raise HTTPException(404)
        return prove.passport(live, mid)

    class DecideIn(BaseModel):
        model_config = ConfigDict(extra="forbid")
        machine: str = Field(max_length=16)
        choice: str = Field(pattern="^(book|later|details)$")
        lang: str | None = Field(default=None, pattern="^(" + "|".join(diagnosis.LANGS) + ")$")

    @app.post("/api/owner/decide")
    def owner_decide(body: DecideIn) -> dict[str, Any]:
        """Owner screen buttons (Mechanic bulao / Kal / Details): the same path as a WhatsApp reply."""
        if body.machine not in live.c.machines:
            raise HTTPException(404)
        return live.alerts.decide(body.machine, body.choice, body.lang)

    @app.get("/api/owner/{factory}/mechanics")
    def owner_mechanics(factory: str) -> list[dict[str, Any]]:
        """Owner only (the owner screen); never part of the shareable passport."""
        from . import prove

        if factory not in live.c.factories:
            raise HTTPException(404)
        return prove.mechanic_stats(live, factory)

    @app.get("/api/twin")
    def twin(focus: str | None = None) -> dict[str, Any]:
        """Everything the Digital Twin HUD panels show, from the live readings."""
        from . import twin as twin_mod

        with live.lock:
            return twin_mod.summary(live, focus)

    class ChatIn(BaseModel):
        model_config = ConfigDict(extra="forbid")
        text: str = Field(min_length=1, max_length=600)
        factory: str | None = Field(default=None, max_length=4)
        lang: str | None = Field(default=None, pattern="^(en|hi|mr|gu|ta|kn|te|hi_latn)$")

    @app.post("/api/agent/chat")
    async def agent_chat(body: ChatIn) -> dict[str, Any]:
        """The owner's question in their language -> the agent's reply and its tool timeline."""
        from . import owner_agent

        return await asyncio.to_thread(owner_agent.chat, live, body.text, body.factory, body.lang)

    @app.get("/api/agent/trace")
    def agent_trace(machine: str | None = None, case: str | None = None) -> dict[str, Any]:
        """The planner's trace for a machine or case (read-only): event, reasoning, tools, approvals, ₹."""
        if machine is not None and machine not in live.c.machines:
            raise HTTPException(404, "unknown machine")
        return brain.planner_trace(case, machine, limit=4)

    @app.post("/api/agent/voice")
    async def agent_voice(
        request: Request, factory: str | None = None, lang: str | None = None
    ) -> dict[str, Any]:
        """A voice note (raw audio body): local Whisper -> the same agent. lang = Whisper's language hint."""
        from . import folk, owner_agent

        raw = await request.body()
        if not raw or len(raw) > 8_000_000:
            raise HTTPException(400, "send 1 byte to 8 MB of audio")
        ALERT_DIR.mkdir(parents=True, exist_ok=True)
        path = ALERT_DIR / f"agent_voice_{int(time.time() * 1000)}.audio"
        path.write_bytes(raw)
        hint = lang if lang in ("hi", "mr", "gu", "ta", "kn", "te", "en") else None
        text, heard = await asyncio.to_thread(folk.transcribe, str(path), hint)
        out = await asyncio.to_thread(owner_agent.chat, live, text, factory, None)
        return {**out, "heard": text, "whisper_language": heard}

    @app.get("/api/forecast/parts")
    def parts_forecast(days: float = 14.0) -> dict[str, Any]:
        """Cluster parts forecast for dealers: counts of machines whose days-to-fault range reaches `days`."""
        from . import fix

        rows = fix.parts_forecast(live, days)
        return {"horizon_days": days, "rows": rows, "text": fix.forecast_text(rows, days),
                "stock_note": "dealer stock is mock data (data/sim/parts_mock.json)"}  # fmt: skip

    @app.post("/api/alerts/reply")
    def alert_reply(body: AlertReplyIn) -> dict[str, Any]:
        """The same path a WhatsApp reply takes (for testing without a public webhook)."""
        got = live.alerts.on_reply(body.phone, body.text)
        if got is None:
            raise HTTPException(404, "no open alert for that number")
        return got

    def _media(m: dict[str, Any], provider: Any) -> bytes:
        """The message's bytes: inline for the demo phones (?view=phones), else fetched from the provider."""
        if "data" in m:
            return base64.b64decode(m["data"])
        return provider.download(m)

    async def _incoming(
        msgs: list[dict[str, Any]], provider: Any, simulated: bool = False
    ) -> list[dict[str, Any]]:
        """Text replies go to the reply path; voice notes are downloaded and transcribed locally; photos are
        stored for the case. The demo phones come through here too (simulated=True), so both take one path."""
        from .wa_loops import log_incoming

        out = []
        for m in msgs:
            kind = "audio" if m.get("audio") else "image" if m.get("image") else "text"
            with live.lock:
                log_incoming(live.c.db, m["from"], m.get("text"), kind, (m.get("audio") or m.get("image") or {}).get("mime"),
                             simulated)  # fmt: skip
            if m.get("audio") and (hasattr(provider, "download") or "data" in m["audio"]):
                try:
                    audio = await asyncio.to_thread(_media, m["audio"], provider)
                    got = await asyncio.to_thread(
                        live.alerts.on_audio, m["from"], audio, m["audio"].get("mime", "")
                    )
                except Exception as e:  # a failed download or transcription must not drop the webhook
                    live.event("owner_voice_failed", error=type(e).__name__)
                    got = None
            elif m.get("image") and (hasattr(provider, "download") or "data" in m["image"]):
                try:
                    img = await asyncio.to_thread(_media, m["image"], provider)
                    got = await asyncio.to_thread(live.alerts.on_image, m["from"], img, m["image"].get("mime", ""),
                                                  m.get("text"))  # fmt: skip
                except Exception as e:
                    live.event("photo_failed", error=type(e).__name__)
                    got = None
            elif m.get("text") is not None:
                got = live.alerts.on_message(m["from"], m["text"])
            else:
                got = None
            if got:
                out.append(got)
        return out

    @app.get("/webhooks/whatsapp")
    def wa_verify(request: Request) -> Response:
        q = request.query_params
        if (
            q.get("hub.mode") == "subscribe"
            and q.get("hub.verify_token")
            and q.get("hub.verify_token") == env.get("WA_VERIFY_TOKEN")
        ):
            return Response(q.get("hub.challenge", ""), media_type="text/plain")
        raise HTTPException(403, "verify token mismatch")

    @app.post("/webhooks/whatsapp")
    async def wa_incoming(request: Request) -> dict[str, Any]:
        raw = await request.body()
        secret = env.get("WA_APP_SECRET")
        if not secret or not whatsapp.verify_meta_signature(
            secret, raw, request.headers.get("X-Hub-Signature-256")
        ):
            raise HTTPException(403, "bad signature")
        return {"handled": await _incoming(whatsapp.parse_meta(json.loads(raw)), live.alerts.notifier.wa)}

    @app.post("/webhooks/twilio")
    async def twilio_incoming(request: Request) -> Response:
        form = {k: str(v) for k, v in (await request.form()).items()}
        token = env.get("TWILIO_AUTH_TOKEN")
        url = (env.get("PUBLIC_BASE_URL") or str(request.base_url).rstrip("/")) + "/webhooks/twilio"
        if not token or not whatsapp.verify_twilio_signature(
            token, url, form, request.headers.get("X-Twilio-Signature")
        ):
            raise HTTPException(403, "bad signature")
        await _incoming(whatsapp.parse_twilio(form), live.alerts.notifier.wa)
        return Response("<Response/>", media_type="application/xml")

    class WaSimIn(BaseModel):
        model_config = ConfigDict(extra="forbid")
        role: str = Field(pattern="^(owner|mechanic)$")
        factory: str = Field(default="A", min_length=1, max_length=8)
        text: str | None = Field(default=None, max_length=600)
        image_b64: str | None = Field(default=None, max_length=8_000_000)
        audio_b64: str | None = Field(default=None, max_length=8_000_000)
        mime: str | None = Field(default=None, max_length=40)

    def _phones(factory: str) -> dict[str, Any]:
        from .wa_loops import mechanic_phone, owner_phone

        alerts = live.alerts
        jobs = [j for j in brain.desk.jobs() if j["factory"] == factory]
        if jobs:
            tech = brain.agent.t.technicians.get(jobs[-1]["technician"])
            mech = jobs[-1]["phone"]
        else:
            tech = next(
                (t for t in brain.agent.t.technicians.people if t.get("role") == "own_mechanic"), None
            )
            mech = mechanic_phone(alerts, tech, factory) if tech else None
        return {"owner": owner_phone(alerts, factory), "mechanic": mech, "technician": tech and tech["name"],
                "provider": alerts.provider}  # fmt: skip

    @app.get("/api/wa/phones")
    def wa_phones(factory: str = "A") -> dict[str, Any]:
        """Both sides of the conversation for the demo phones: the owner's and the mechanic's threads."""
        from .wa_loops import thread

        if factory not in live.c.factories:
            raise HTTPException(404, "unknown factory")
        ph = _phones(factory)
        with live.lock:
            return {**ph, "owner_thread": thread(live.c.db, ph["owner"] or "") if ph["owner"] else [],
                    "mechanic_thread": thread(live.c.db, ph["mechanic"] or "") if ph["mechanic"] else []}  # fmt: skip

    @app.post("/api/wa/simulate")
    async def wa_simulate(body: WaSimIn) -> dict[str, Any]:
        """A message from the owner's or the mechanic's phone, through the same code path as the webhook.
        Only with the dry-run provider: a demo must never pose as a real person on a real account."""
        if live.alerts.provider != "dry_run":
            raise HTTPException(
                409, "simulated phones only work with the dry-run provider (DHADKAN_DRY_RUN=1)"
            )
        ph = _phones(body.factory)
        who = ph[body.role]
        if not who:
            raise HTTPException(409, f"no {body.role} phone for factory {body.factory}")
        msg: dict[str, Any] = {"from": who, "text": body.text, "id": f"sim-{time.time_ns()}"}
        if body.image_b64:
            msg["image"] = {"data": body.image_b64, "mime": body.mime or "image/jpeg"}
        elif body.audio_b64:
            msg["audio"] = {"data": body.audio_b64, "mime": body.mime or "audio/wav"}
            msg["text"] = None
        elif not body.text:
            raise HTTPException(422, "text, image_b64 or audio_b64 is needed")
        handled = await _incoming([msg], live.alerts.notifier.wa, simulated=True)
        return {"handled": handled, "from": who, "role": body.role}

    @app.get("/api/voice/note/{name}")
    def voice_note_file(name: str) -> Response:
        """A cached voice note (Twilio fetches media by URL). Only cache file names, nothing else."""
        from . import voice

        if not re.fullmatch(r"[0-9a-f]{24}\.ogg", name) or not (voice.CACHE / name).exists():
            raise HTTPException(404, "no such voice note")
        return Response((voice.CACHE / name).read_bytes(), media_type="audio/ogg")

    @app.get("/api/voice/machine/{mid}.ogg")
    async def voice_for_machine(mid: str, lang: str | None = None) -> Response:
        """The owner screen's "suno": this machine's alert text, read aloud from the same template."""
        if mid not in live.c.machines:
            raise HTTPException(404, "unknown machine")
        alerts = live.alerts
        lg = lang if lang in ("hi", "hi_latn", "mr", "gu", "en", "ta") else alerts.owner_lang(mid)
        text = await asyncio.to_thread(alerts.text, mid, lg)
        if not text:
            raise HTTPException(404, "no alert for this machine right now")
        vm = await asyncio.to_thread(alerts.voice_media, "alert", mid, lg, text)
        if not vm:
            raise HTTPException(
                503,
                "no voice for this language right now (DHADKAN_VOICE, or the models: .\\dev.ps1 voice-models)",
            )
        return Response(Path(vm[0]["path"]).read_bytes(), media_type="audio/ogg",
                        headers={"X-Voice-Engine": str(vm[0].get("engine")), "X-Voice-Seconds": str(vm[0].get("seconds"))})  # fmt: skip

    @app.get("/api/voice_note.wav")
    def voice_note() -> Response:
        return Response(VOICE_NOTE.read_bytes(), media_type="audio/wav")

    @app.websocket("/ws")
    async def ws(sock: WebSocket) -> None:
        await sock.accept()
        await sock.send_text(
            json.dumps({"type": "state", "state": await asyncio.to_thread(live.state)}, ensure_ascii=False)
        )
        hub.clients.add(sock)
        try:
            while True:
                await sock.receive_text()  # nothing expected; keeps the socket open
        except WebSocketDisconnect:
            hub.clients.discard(sock)

    if WEB_DIST.exists():
        app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")
    return app
