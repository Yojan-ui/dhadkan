"""Download a small subset of MIMII (pump, fan) and MIMII DUE (gearbox).

The full archives are 1-10 GB each. Zenodo serves HTTP range requests, so we
read the zip's central directory and pull only the members we need. MIMII
files are 8-channel; we keep channel 0 only (mono, 16 kHz) to save disk.

Datasets (CC BY-SA 4.0):
  MIMII      https://zenodo.org/records/3384388  (Purohit et al., 2019)
  MIMII DUE  https://zenodo.org/records/4740355  (Tanabe et al., 2021)

Usage:  python -m data.download [--small]
"""

import argparse
import io
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import soundfile as sf
from remotezip import RemoteIOError, RemoteZip

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "raw"

MIMII = "https://zenodo.org/records/3384388/files/{name}?download=1"
DUE = "https://zenodo.org/records/4740355/files/{name}?download=1"

# (archive url, output dir, [(member regex, count), ...])
# Counts are per class. Files are taken in sorted order, so reruns are stable.
PLAN = [
    (
        MIMII.format(name="6_dB_pump.zip"),
        "mimii_6dB/pump",
        [
            (r"pump/id_00/normal/", 60),
            (r"pump/id_00/abnormal/", 20),
            (r"pump/id_02/normal/", 60),
            (r"pump/id_02/abnormal/", 20),
        ],
    ),
    (
        MIMII.format(name="6_dB_fan.zip"),
        "mimii_6dB/fan",
        [
            (r"fan/id_00/normal/", 60),
            (r"fan/id_00/abnormal/", 20),
            (r"fan/id_02/normal/", 60),
            (r"fan/id_02/abnormal/", 20),
        ],
    ),
    # Same pump, recorded with louder factory noise: the "noisy shed" check.
    (
        MIMII.format(name="-6_dB_pump.zip"),
        "mimii_-6dB/pump",
        [
            (r"pump/id_00/normal/", 20),
            (r"pump/id_00/abnormal/", 20),
        ],
    ),
    (
        DUE.format(name="dev_data_gearbox.zip"),
        "mimii_due",
        [(rf"gearbox/train/section_0{s}_source_train_normal", 60) for s in range(3)]
        + [
            (rf"gearbox/(source_)?test/section_0{s}_source_test_{c}", 20)
            for s in range(3)
            for c in ("normal", "anomaly")
        ]
        + [
            # Changed operating condition (speed/load): the "speed change" check.
            (rf"gearbox/(target_)?test/section_0{s}_target_test_{c}", 10)
            for s in range(3)
            for c in ("normal", "anomaly")
        ],
    ),
]

SMALL = 0.5  # --small keeps half of each count


def select(names: list[str], plan: list[tuple[str, int]], scale: float) -> list[str]:
    picked = []
    for pattern, count in plan:
        rx = re.compile(pattern)
        hits = sorted(n for n in names if rx.search(n) and n.endswith(".wav"))
        if not hits:
            sys.exit(f"no members match {pattern!r}; archive layout changed?")
        picked += hits[: max(4, int(count * scale))]
    return picked


_local = threading.local()


def _zip(url: str) -> RemoteZip:
    """One open archive per thread, so the central directory is read once."""
    if getattr(_local, "url", None) != url:
        _local.zip, _local.url = RemoteZip(url), url
    return _local.zip


def fetch(url: str, member: str, out_dir: Path) -> str:
    out = out_dir / member
    if out.exists():
        return "skip"
    for attempt in range(8):
        try:
            raw = _zip(url).read(member)
            break
        except RemoteIOError:  # Zenodo answers 429 when pushed too hard
            if attempt == 7:
                raise
            time.sleep(min(60, 5 * 2**attempt))
            _local.url = None
    audio, sr = sf.read(io.BytesIO(raw), dtype="int16", always_2d=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part")
    sf.write(tmp, audio[:, 0], sr, subtype="PCM_16", format="WAV")
    tmp.rename(out)
    return "ok"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--small", action="store_true", help="half the clips")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    scale = SMALL if args.small else 1.0

    for url, sub, plan in PLAN:
        out_dir = DATA / sub
        print(f"reading index: {url.split('/files/')[1].split('?')[0]}", flush=True)
        with RemoteZip(url) as z:
            names = z.namelist()
        members = select(names, plan, scale)
        print(f"  {len(members)} files -> {out_dir.relative_to(ROOT)}", flush=True)
        with ThreadPoolExecutor(args.workers) as pool:
            jobs = [pool.submit(fetch, url, m, out_dir) for m in members]
            for i, job in enumerate(jobs, 1):
                job.result()
                if i % 20 == 0 or i == len(jobs):
                    print(f"  {i}/{len(jobs)}", flush=True)
    print("done. Next: python -m edge.train")


if __name__ == "__main__":
    main()
