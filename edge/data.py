"""Which clip is used for what. Splits are deterministic (sorted file names).

pump, fan, valve, slider: DCASE 2020 Task 2 development set, official splits. The train split is
  normal only; its last 10 clips per machine ID are held out (val) for that machine's alarm level.
  The test split (normal + anomalous) is the one the DCASE baseline is scored on.
gearbox: MIMII DUE (DCASE 2021 Task 2 development set), source and target (changed speed/load) test.
"""

from dataclasses import dataclass
from pathlib import Path

from .config import RAW


@dataclass(frozen=True)
class Clip:
    path: Path
    mtype: str  # pump | fan | valve | slider | gearbox
    unit: str  # pump/id_00, gearbox/section_01, ...
    split: str  # train | val | test
    label: int  # 0 normal, 1 anomalous
    condition: str  # test (DCASE 2020) | source | target (gearbox)


def _split_normals(files: list[Path], n_val: int, n_test: int) -> list[tuple[Path, str]]:
    files = sorted(files)
    n_train = len(files) - n_val - n_test
    return (
        [(f, "train") for f in files[:n_train]]
        + [(f, "val") for f in files[n_train : n_train + n_val]]
        + [(f, "test") for f in files[n_train + n_val :]]
    )


def dcase2020(mtype: str, n_val: int = 10) -> list[Clip]:
    root = RAW / "dcase2020" / mtype
    clips = []
    ids = sorted({f.name.split("_")[2] for f in (root / "train").glob("normal_id_*.wav")})
    for i in ids:
        unit = f"{mtype}/id_{i}"
        train = sorted((root / "train").glob(f"normal_id_{i}_*.wav"))
        clips += [Clip(f, mtype, unit, "train", 0, "test") for f in train[:-n_val]]
        clips += [Clip(f, mtype, unit, "val", 0, "test") for f in train[-n_val:]]
        for f in sorted((root / "test").glob(f"*_id_{i}_*.wav")):
            clips.append(Clip(f, mtype, unit, "test", int(f.name.startswith("anomaly")), "test"))
    return clips


def gearbox() -> list[Clip]:
    clips = []
    root = RAW / "mimii_due" / "gearbox"
    for sec in ("00", "01", "02"):
        unit = f"gearbox/section_{sec}"
        train = list((root / "train").glob(f"section_{sec}_*.wav"))
        for f, s in _split_normals(train, n_val=10, n_test=0):
            clips.append(Clip(f, "gearbox", unit, s, 0, "source"))
        for domain in ("source", "target"):
            for f in sorted((root / f"{domain}_test").glob(f"section_{sec}_*.wav")):
                clips.append(Clip(f, "gearbox", unit, "test", int("_anomaly_" in f.name), domain))
    return clips


# The condition each type is headlined on (the other conditions are stress tests).
HEADLINE = {"pump": "test", "fan": "test", "valve": "test", "slider": "test", "gearbox": "source"}
HEADLINE_CONDITIONS = ("test", "source")


def clips_for(mtype: str) -> list[Clip]:
    return gearbox() if mtype == "gearbox" else dcase2020(mtype)
