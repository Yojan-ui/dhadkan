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
import io
import json
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime

import librosa
import numpy as np
import soundfile as sf
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from . import agent, data, embed, features, folk, registry, route
from .config import HOP, N_MELS, ROOT, RUNS, SR
from .sim import Cluster

SLICES = 20                  # 10 s reading -> 20 slices of 0.5 s (the 2 Hz stream)
FRAME_S = 0.5
FAULT_MACHINE, SIMILAR_MACHINE = "A-F1", "B-F1"
VOICE_NOTE = ROOT / "sim" / "voice_notes" / "A_hindi_blower_khatkhat.wav"
WEB_DIST = ROOT / "web" / "dist"
REPLAY_FAULT_AT, REPLAY_LABEL_AT, REPLAY_PUBLISH_AT, REPLAY_MAX = 4, 5, 7, 20   # in readings


def _wav(audio):
    buf = io.BytesIO()
    sf.write(buf, audio, SR, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def latest_eval():
    runs = sorted(RUNS.glob("eval_*.json"))
    if not runs:
        return None
    ev = json.loads(runs[-1].read_text(encoding="utf-8"))
    out = {"run_id": ev["run_id"], "code_sha256": ev["code_sha256"][:12],
           "assumptions": ev["assumptions"], "types": {}}
    for t, rep in ev["types"].items():
        out["types"][t] = {
            "headline": rep["headline_condition"],
            "conditions": {c: m and {k: m.get(k) for k in ("auc", "pauc", "n_normal", "n_anomalous",
                                                          "false_alarm_rate", "detection_rate")}
                           for c, m in rep["conditions"].items()},
            "per_unit": rep["per_unit"],
            "registry": [{"from": r["from"], "to": r["to"], "case": r["case"],
                          "false_flags": r["registry_false_flag_rate"], **r["by_mix"]["0.25"]}
                         for r in rep["registry_transfer"]],
            "tflite": rep["tflite"]}
    return out


class Live:
    """Owns the cluster, the per-reading analysis the stream plays, and the event list."""

    def __init__(self, node_db=None, registry_db=None):
        kw = {k: v for k, v in (("node_db", node_db), ("registry_db", registry_db)) if v}
        self.c = Cluster(**kw)
        self.lock = threading.RLock()
        self.cache = {}             # machine -> analysis of its latest reading
        self.tick = -1              # tick of the cached analysis (read by the stream without the DB)
        self.cursor = 0
        self.events, self.seq = [], 0
        self.version = 0
        self.pending_label = None
        self.replay = {"on": False, "started": None, "phase": "off"}
        self.prev_status = {}
        self.mel_hz = librosa.mel_frequencies(N_MELS + 2, fmin=0, fmax=SR / 2)[1:-1].tolist()
        with self.lock:
            if self.c.tick_no() >= 0:
                self._analyse_latest()
                self.prev_status = {r["machine_id"]: r["status"] for r in self.ranking()}

    # -- analysis of the latest reading -------------------------------------
    def _analyse_latest(self):
        tick = self.c.tick_no()
        for mid, m in self.c.machines.items():
            row = self.c.history(mid, 1)[0]
            audio, _ = self.c.audio(mid, tick, row["mode"], row["mix"])
            em = self.c.model(m["type"])
            r = em.score_audio(audio, m["source"])
            mel = features.log_mel(audio)
            z = (mel - em.mu) / em.sd                      # vs the model's training normal
            n_fr = mel.shape[1]
            step = int(0.5 * SR / HOP)
            spec, risk, energy = [], [], []
            for i in range(SLICES):
                a, b = int(i * n_fr / SLICES), max(int((i + 1) * n_fr / SLICES), int(i * n_fr / SLICES) + 1)
                spec.append(np.clip((z[:, a:b].mean(axis=1) + 3) / 6, 0, 1).round(3).tolist())
                w = int(np.clip(round(((a + b) / 2 - 16) / step), 0, len(r["window_errors"]) - 1))
                risk.append(round(r["window_errors"][w] / r["threshold"], 3))
                seg = audio[int(i * FRAME_S * SR):int((i + 1) * FRAME_S * SR)]
                energy.append(round(float(10 * np.log10(np.mean(seg ** 2) + 1e-12)), 1))
            self.cache[mid] = {"tick": tick, "spec": spec, "risk": risk, "energy": energy,
                               "band_z": np.round(r["band_z"], 2).tolist()}
        self.tick, self.cursor = tick, 0

    # -- state ----------------------------------------------------------------
    def ranking(self):
        with self.lock:
            return agent.Tools(self.c, "view", "live").get_risk_ranking() if self.c.tick_no() >= 0 else []

    def event(self, kind, **data_):
        with self.lock:
            self._event(kind, **data_)

    def _event(self, kind, **data_):
        self.seq += 1
        self.events.append({"seq": self.seq, "kind": kind, "clock": self.c.now().isoformat(timespec="minutes"),
                            "replay_t": self.replay_t(), **data_})
        self.events = self.events[-60:]
        self.version += 1

    def replay_t(self):
        s = self.replay["started"]
        return None if s is None else round(time.monotonic() - s, 1)

    def route(self):
        row = self.c.db.execute("SELECT output, ts FROM tool_calls WHERE tool='plan_technician_route' "
                                "ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return None
        p = json.loads(row["output"])
        tech, cfg = self.c.cfg["technician"], self.c.cfg
        ids = [s.split()[0] for s in p["stops"]]
        stops = [{"id": i, "xy_km": self.c.factories[self.c.machines[i]["factory"]]["xy_km"],
                  "service_h": cfg["service_hours"][self.c.machines[i]["type"]]} for i in ids]
        start = datetime.fromisoformat(f"{p['date']}T{tech.get('day_start', '09:00')}")
        tl = route.timeline(stops, tech["xy_km"], cfg["road_kmph"], start)
        return {**p, "planned_at": row["ts"], "base_xy": tech["xy_km"],
                "legs": [{"machine": s["id"], "factory": self.c.machines[s["id"]]["factory"],
                          "xy_km": st["xy_km"], "arrive": s["arrive"].isoformat(timespec="minutes"),
                          "leave": s["leave"].isoformat(timespec="minutes")} for s, st in zip(tl, stops)],
                "not_today_ids": [s.split()[0] for s in p["not_today"]]}

    def messages(self):
        rows = [dict(r) for r in self.c.db.execute("SELECT * FROM messages ORDER BY id")]
        drafts = {}
        for t in self.c.db.execute("SELECT run_id, input, output FROM tool_calls WHERE tool='draft_owner_alert'"):
            out = json.loads(t["output"])
            if "text" in out:
                drafts[(t["run_id"], out["text"])] = out.get("text_en")
        for r in rows:
            r["machine"], r["visit_day"] = r["ask"].split("|")
            r["text_en"] = drafts.get((r["run_id"], r["body"]))
        return rows

    def state(self):
        with self.lock:
            rank = {r["machine_id"]: r for r in self.ranking()}
            machines = []
            for mid, m in self.c.machines.items():
                hist = self.c.history(mid)
                last = hist[-1] if hist else None
                mode, mix = self.c.mode(mid)
                r = rank.get(mid, {})
                machines.append({
                    **{k: m[k] for k in ("id", "factory", "name", "name_local", "type", "source", "shape")},
                    "mode": mode, "mix": mix, "risk3": r.get("risk"), "status": r.get("status", "no_data"),
                    "days_rising": r.get("days_rising", 0), "known_fault": r.get("known_fault"),
                    "registry": r.get("registry"),
                    "latest": last and {k: last[k] for k in ("tick", "ts", "clip", "score", "threshold", "risk",
                                                             "match_cos", "match_level", "mode", "mix")},
                    "history": [{"tick": h["tick"], "ts": h["ts"], "risk": round(h["risk"], 3),
                                 "match": h["match_cos"] is not None and h["match_cos"] > h["match_level"]}
                                for h in hist],
                    "band_z": self.cache.get(mid, {}).get("band_z"),
                })
            calls = [dict(r) for r in self.c.db.execute(
                "SELECT id, run_id, ts, actor, tool, input, output FROM tool_calls ORDER BY id DESC LIMIT 40")]
            return {
                "version": self.version, "tick": self.c.tick_no(),
                "clock": self.c.now().isoformat(timespec="minutes") if self.c.tick_no() >= 0 else None,
                "tick_hours": self.c.cfg["tick_hours"], "machines": machines,
                "fingerprints": registry.fingerprints(self.c.reg),
                "pending_label": self.pending_label, "messages": self.messages(), "route": self.route(),
                "tool_calls": calls, "events": self.events,
                "replay": {**self.replay, "started": None, "t": self.replay_t()},
            }

    def meta(self):
        cfg = self.c.cfg
        return {"factories": cfg["factories"], "technician": cfg["technician"], "mel_hz": self.mel_hz,
                "slices": SLICES, "frame_s": FRAME_S, "fault_machine": FAULT_MACHINE,
                "similar_machine": SIMILAR_MACHINE, "has_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
                "note": cfg.get("_note", ""), "eval": latest_eval()}

    def frame(self):
        i = self.cursor
        out = {}
        for mid, a in self.cache.items():
            out[mid] = {"risk": a["risk"][i], "energy": a["energy"][i], "spec": a["spec"][i]}
        return {"type": "frame", "tick": self.tick, "slice": i, "machines": out}

    # -- actions --------------------------------------------------------------
    def step(self, n=1):
        with self.lock:
            for _ in range(n):
                self.c.step()
            self._analyse_latest()
            rank = self.ranking()
            for r in rank:
                was = self.prev_status.get(r["machine_id"])
                if r["status"] == "early_match" and was != "early_match":
                    src = None
                    fid = self.c.history(r["machine_id"], 1)[0]["match_fp"]
                    if fid is not None:
                        src = next((f["machine"] for f in registry.fingerprints(self.c.reg) if f["id"] == fid), None)
                    self.event("early_match", machine=r["machine_id"], source=src,
                               cosine=r["registry"]["cosine"], level=r["registry"]["flag_level"],
                               fault=r["registry"]["fault"])
                if r["status"] == "alarm" and was not in ("alarm", None):
                    self.event("alarm", machine=r["machine_id"], risk=r["risk"])
            self.prev_status = {r["machine_id"]: r["status"] for r in rank}
            self.event("reading", tick=self.c.tick_no())

    def set_mode(self, mid, mode):
        if mid not in self.c.machines or mode not in ("normal", "fault", "onset"):
            raise HTTPException(400, "unknown machine or mode")
        with self.lock:
            self.c.set_mode(mid, mode, 0.0)
            self.event("mode", machine=mid, mode=mode, synthetic=mode == "onset")

    def label(self, text=None, factory="A", machine=FAULT_MACHINE):
        """Owner's note -> weak label. No text: the sample voice note through local Whisper."""
        lang = None
        if not text:
            text, lang = folk.transcribe(str(VOICE_NOTE), language=self.c.factories[factory]["language"])
        llm = folk.claude_label if os.environ.get("ANTHROPIC_API_KEY") else None
        lab = folk.label_text(text, folk.load_lexicon(), llm)
        with self.lock:
            wid = registry.add_weak_label(self.c.reg, factory, machine, text, lang, lab)
            self.pending_label = {**lab, "id": wid, "text": text, "machine": machine, "factory": factory,
                                  "via": "whisper" if lang else "typed"}
            self.event("label", **self.pending_label)
        return self.pending_label

    def publish(self, last=4):
        with self.lock:
            lab = self.pending_label
            if not lab or lab["fault"] == "unknown":
                raise HTTPException(400, "no usable label to confirm")
            mid = lab["machine"]
            m = self.c.machines[mid]
            fp = self.c.confirmed_embedding(mid, last)
            fid = registry.publish(self.c.reg, m["type"], lab["fault"], lab["sound"], lab["factory"], mid, fp,
                                   lab["id"])
            checks = []   # what the registry says about each same-type machine elsewhere, right now
            for oid, o in self.c.machines.items():
                if o["type"] != m["type"] or o["factory"] == m["factory"] or not self.c.history(oid):
                    continue
                emb = json.loads(self.c.history(oid, 1)[0]["embedding"])
                cos = float(embed.cosine(emb, [fp])[0])
                level = self.c.model(o["type"]).match_level(o["source"], fp)
                checks.append({"machine": oid, "cosine": round(cos, 3), "level": round(level, 3),
                               "match": cos > level, "same_model": o["source"] == m["source"]})
            self.pending_label = None
            self.event("published", fingerprint=fid, machine=mid, fault=lab["fault"], sound=lab["sound"],
                       checks=checks)
            return {"fingerprint": fid, "checks": checks}

    def run_agent(self, engine="rules"):
        with self.lock:
            before = self.c.db.execute("SELECT COALESCE(MAX(id), 0) FROM tool_calls").fetchone()[0]
            run_id, note = agent.run_claude(self.c) if engine == "claude" else agent.run_rules(self.c)
            for t in self.c.db.execute("SELECT id, tool, input FROM tool_calls WHERE id>? ORDER BY id", (before,)):
                self.event("tool_call", id=t["id"], tool=t["tool"], input=json.loads(t["input"]), run_id=run_id)
            planned = any(e["kind"] == "tool_call" and e["tool"] == "plan_technician_route"
                          and e["run_id"] == run_id for e in self.events)
            if planned:
                self.event("route", run_id=run_id, machines=[leg["machine"] for leg in self.route()["legs"]])
            self.event("digest", run_id=run_id, note=note)
            return {"run_id": run_id, "note": note}

    def reply(self, message_id, text):
        with self.lock:
            ok = agent.record_reply(self.c, message_id, text)
            row = self.c.db.execute("SELECT ask FROM messages WHERE id=?", (message_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "no such message")
            self.event("reply", message_id=message_id, machine=row["ask"].split("|")[0], reply=text, approved=ok)
            return {"approved": ok}

    def reset(self):
        with self.lock:
            self.c.reset()
            self.cache, self.pending_label, self.prev_status, self.tick = {}, None, {}, -1
            self.events = []
            self.event("reset")

    # -- demo replay ----------------------------------------------------------
    async def run_replay(self, tick_s=SLICES * FRAME_S):
        """Scripted demo on recorded MIMII clips. Fault on A-F1 at 0:40 (reading 4)."""
        await asyncio.to_thread(self.reset)
        self.replay.update(on=True, started=time.monotonic(), phase="baseline")
        label_task = None
        try:
            for k in range(REPLAY_MAX + 1):
                t0 = time.monotonic()
                if k == REPLAY_FAULT_AT:
                    self.replay["phase"] = "fault"
                    await asyncio.to_thread(self.set_mode, FAULT_MACHINE, "fault")
                if k == REPLAY_LABEL_AT:
                    self.replay["phase"] = "voice note"
                    label_task = asyncio.create_task(asyncio.to_thread(self.label))
                if k == REPLAY_PUBLISH_AT:
                    await label_task
                    self.replay["phase"] = "published"
                    await asyncio.to_thread(self.publish, REPLAY_PUBLISH_AT - REPLAY_FAULT_AT)
                    await asyncio.to_thread(self.set_mode, SIMILAR_MACHINE, "onset")
                await asyncio.to_thread(self.step)
                b = next(m for m in self.ranking() if m["machine_id"] == SIMILAR_MACHINE)
                if k > REPLAY_PUBLISH_AT and (b["status"] == "early_match" or k == REPLAY_MAX):
                    await asyncio.sleep(2.5)            # let the match pulse land before the route
                    self.replay["phase"] = "digest"
                    await asyncio.to_thread(self.run_agent, "rules")
                    break
                await asyncio.sleep(max(0.0, tick_s - (time.monotonic() - t0)))
            self.replay["phase"] = "awaiting owner"
            self.version += 1
        except asyncio.CancelledError:
            self.replay["phase"] = "stopped"
            raise
        except Exception as e:      # show it in the UI instead of dying silently
            self.replay["phase"] = f"error: {e}"
            raise
        finally:
            self.replay["on"] = False
            self.version += 1


class Hub:
    def __init__(self, live: Live):
        self.live, self.clients = live, set()

    async def send_all(self, msg):
        dead = []
        text = json.dumps(msg, ensure_ascii=False)
        for ws in list(self.clients):
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    async def loop(self):
        """2 Hz: frame every tick; state whenever something changed."""
        sent_version, sent_tick = -1, None
        while True:
            lv = self.live
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


class ReplayIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    on: bool


def create_app(node_db=None, registry_db=None, stream=True):
    live = Live(node_db, registry_db)
    hub = Hub(live)
    tasks = {}

    @asynccontextmanager
    async def lifespan(app):
        if stream:
            tasks["loop"] = asyncio.create_task(hub.loop())
        yield
        for t in tasks.values():
            t.cancel()

    app = FastAPI(title="Dhadkan live", lifespan=lifespan)
    app.state.live = live

    def busy():
        if live.replay["on"]:
            raise HTTPException(409, "demo replay is running; switch it off first")

    @app.get("/api/meta")
    def meta():
        return live.meta()

    @app.get("/api/state")
    def state():
        return live.state()

    @app.post("/api/step")
    async def step(body: StepIn):
        busy()
        await asyncio.to_thread(live.step, max(1, min(body.n, 9)))
        return {"tick": live.tick}

    @app.post("/api/mode")
    def mode(body: ModeIn):
        busy()
        live.set_mode(body.machine, body.mode)
        return {"ok": True}

    @app.post("/api/label")
    async def label(body: LabelIn):
        busy()
        return await asyncio.to_thread(live.label, body.text)

    @app.post("/api/publish")
    def publish():
        busy()
        return live.publish()

    @app.post("/api/agent")
    async def run_agent(body: AgentIn):
        busy()
        if body.engine not in ("rules", "claude"):
            raise HTTPException(400, "engine is rules or claude")
        if body.engine == "claude" and not os.environ.get("ANTHROPIC_API_KEY"):
            raise HTTPException(400, "ANTHROPIC_API_KEY is not set")
        return await asyncio.to_thread(live.run_agent, body.engine)

    @app.post("/api/reply")
    def reply(body: ReplyIn):
        return live.reply(body.message_id, body.reply)

    @app.post("/api/reset")
    def reset():
        busy()
        live.reset()
        return {"ok": True}

    @app.post("/api/replay")
    async def replay(body: ReplayIn):
        t = tasks.get("replay")
        if t and not t.done():
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
        if body.on:
            tasks["replay"] = asyncio.create_task(live.run_replay())
        return {"on": body.on}

    @app.get("/api/audio/{mid}/{kind}.wav")
    def audio(mid: str, kind: str):
        """live: the 10 s the model scored last. normal: a held-out normal clip of the same unit."""
        if mid not in live.c.machines or kind not in ("live", "normal"):
            raise HTTPException(404)
        m = live.c.machines[mid]
        if kind == "normal":
            ref = next(c for c in data.clips_for(m["type"])
                       if c.unit == m["source"] and c.split == "val" and c.label == 0)
            return Response(_wav(features.load(ref.path)), media_type="audio/wav",
                            headers={"X-Clip": ref.path.name})
        with live.lock:
            hist = live.c.history(mid, 1)
            if not hist:
                raise HTTPException(404, "no reading yet")
            h = hist[0]
            a, name = live.c.audio(mid, h["tick"], h["mode"], h["mix"])
        return Response(_wav(a), media_type="audio/wav", headers={"X-Clip": name})

    @app.get("/api/voice_note.wav")
    def voice_note():
        return Response(VOICE_NOTE.read_bytes(), media_type="audio/wav")

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        await sock.send_text(json.dumps({"type": "state", "state": await asyncio.to_thread(live.state)},
                                        ensure_ascii=False))
        hub.clients.add(sock)
        try:
            while True:
                await sock.receive_text()       # nothing expected; keeps the socket open
        except WebSocketDisconnect:
            hub.clients.discard(sock)

    if WEB_DIST.exists():
        app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")
    return app

