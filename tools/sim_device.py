"""A simulated sensing device for /api/ingest: what an ESP32 vibration node or the phone app would send.

    python -m tools.sim_device --url http://127.0.0.1:8600 --machine sim-pump --wav some.wav
    python -m tools.sim_device --url http://127.0.0.1:8600 --machine sim-motor --vibration --fault-after 30

Every reading is posted with source="simulated" (never "live"). --vibration makes a synthetic motor
signal (shaft tone + noise at 1 kHz); --fault-after N adds a growing bearing-like tone from reading N.
It is synthetic, for exercising the pipeline, and is never reported as a result.
"""

import argparse
import base64
import time
from typing import Any

import numpy as np


def vibration(
    seq: int, fault_after: int | None, sr: int = 1000, seconds: float = 2.0, seed: int = 0
) -> np.ndarray:
    rng = np.random.default_rng(seed + seq)
    t = np.arange(int(sr * seconds)) / sr
    x = 0.2 * np.sin(2 * np.pi * 24.6 * t) + 0.05 * rng.standard_normal(len(t))
    if fault_after is not None and seq >= fault_after:
        grow = min(1.0, (seq - fault_after + 1) / 20)
        x += grow * 0.3 * np.sin(2 * np.pi * 88.2 * t) * (1 + 0.5 * np.sin(2 * np.pi * 24.6 * t))
    return x


def payload(device: str, seq: int, machine: str, x: np.ndarray, sr: int, kind: str, ts: float | None = None
            ) -> dict[str, Any]:  # fmt: skip
    pcm = np.clip(x * 32767, -32768, 32767).astype("<i2").tobytes()
    return {"device": device, "seq": seq, "machine": machine, "kind": kind, "sr": sr, "source": "simulated",
            "samples_b64": base64.b64encode(pcm).decode(), **({"ts": ts} if ts is not None else {})}  # fmt: skip


def main() -> None:
    import httpx
    import soundfile as sf

    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8600")
    ap.add_argument("--machine", default="sim-motor")
    ap.add_argument("--device", default="sim-device-1")
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--every", type=float, default=0.2, help="seconds between readings")
    ap.add_argument("--wav")
    ap.add_argument("--vibration", action="store_true")
    ap.add_argument("--fault-after", type=int)
    ap.add_argument("--learning-days", type=float, default=0.0)
    args = ap.parse_args()
    http = httpx.Client(base_url=args.url, timeout=10)
    http.post(
        "/api/ingest/register",
        json={"machine": args.machine, "mtype": "motor", "learning_days": args.learning_days},
    )
    for seq in range(args.n):
        if args.wav:
            x, sr = sf.read(args.wav, dtype="float32", always_2d=True)
            x, kind = x[:, 0], "audio"
        else:
            sr, kind = 1000, "vibration"
            x = vibration(seq, args.fault_after, sr)
        r = http.post("/api/ingest/reading", json=payload(args.device, seq, args.machine, x, sr, kind)).json()
        print(seq, r.get("state"), None if r.get("risk") is None else round(r["risk"], 2), r.get("source"))
        time.sleep(args.every)


if __name__ == "__main__":
    main()
