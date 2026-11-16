"""Readings from devices: the phone app, an ESP32 + MPU6050 vibration node (hardware/esp32_vibration),
an uploaded recording, or the simulated device (tools/sim_device.py).

    POST /api/ingest/register   {machine, mtype, learning_days?}
    POST /api/ingest/reading    {device, seq, machine, kind, sr, samples_b64, source, ts?}
    GET  /api/ingest/{machine}  latest readings with their source label

Every reading carries its source ("live" = a device in a shed, "upload" = a recording someone sent,
"simulated" = the demo device) and is stored with it; the dashboard shows the label. A reading is
identified by device:seq, so a device that resends after a lost reply is never counted twice.

Samples are little-endian int16, base64. Audio is resampled to the model's 16 kHz. Vibration (an ESP32
samples its accelerometer at ~1 kHz) is resampled the same way so ONE pipeline learns each machine's
normal; its spectrum above the sensor's Nyquist frequency is empty and is the same in every reading, so
it does not move the per-machine comparison. This path is tested with the simulated device only.
"""

import base64
from pathlib import Path
from typing import Any

import librosa
import numpy as np
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from edge import summary
from edge.baseline import SOURCES, BaselineStore
from edge.config import ROOT, SR
from edge.detectors import WIDTH

MAX_SECONDS = 30.0


class RegisterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    machine: str = Field(min_length=1, max_length=64)
    mtype: str = Field(min_length=1, max_length=32)
    learning_days: float = Field(default=7.0, ge=0.0, le=30.0)
    ts: float | None = None


class ReadingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device: str = Field(min_length=1, max_length=64)
    seq: int = Field(ge=0)
    machine: str = Field(min_length=1, max_length=64)
    kind: str = Field(pattern="^(audio|vibration)$")
    sr: int = Field(ge=200, le=48_000)
    samples_b64: str
    source: str = "live"
    ts: float | None = None


def decode(b64: str, sr: int) -> np.ndarray:
    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception as e:
        raise HTTPException(422, "samples_b64 is not base64") from e
    if len(raw) % 2:
        raise HTTPException(422, "samples must be int16")
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if len(x) > MAX_SECONDS * sr:
        raise HTTPException(413, f"at most {MAX_SECONDS:.0f} s per reading")
    return x if sr == SR else librosa.resample(x, orig_sr=sr, target_sr=SR)


def router(db_path: Path | None = None) -> APIRouter:
    r = APIRouter(prefix="/api/ingest")
    store = BaselineStore(db_path or ROOT / "data" / "ingest.db")

    @r.post("/register")
    def register(body: RegisterIn) -> dict[str, Any]:
        store.register(body.machine, body.mtype, learning_days=body.learning_days, now=body.ts)
        return store.machine(body.machine)

    @r.post("/reading")
    def reading(body: ReadingIn) -> dict[str, Any]:
        if body.source not in SOURCES:
            raise HTTPException(422, f"source must be one of {SOURCES}")
        try:
            store.machine(body.machine)
        except KeyError as e:
            raise HTTPException(404, "register the machine first") from e
        audio = decode(body.samples_b64, body.sr)
        q = summary.quality(audio)
        row = np.full(WIDTH, np.nan)
        if q["seconds"] >= summary.MIN_SECONDS:
            row[: summary.DIM] = summary.summarise(audio)
        else:
            row[: summary.DIM] = 0.0
        out = store.observe(body.machine, row, q, ts=body.ts, source=body.source,
                            reading_id=f"{body.device}:{body.seq}")  # fmt: skip
        return {**out, "quality": q, "kind": body.kind, "source": body.source}

    @r.get("/{machine}")
    def latest(machine: str, last: int = 20) -> dict[str, Any]:
        try:
            m = store.machine(machine)
        except KeyError as e:
            raise HTTPException(404) from e
        return {"machine": m, "readings": store.history(machine, last=max(1, min(last, 500))),
                "versions": [{k: v[k] for k in ("version", "reason", "status", "n_rows")} for v in store.versions(machine)]}  # fmt: skip

    return r
