"""Which clip is used for what. Splits are deterministic (sorted file names)."""
from dataclasses import dataclass
from pathlib import Path

from .config import RAW


@dataclass(frozen=True)
class Clip:
    path: Path
    mtype: str        # pump | fan | gearbox
    unit: str         # pump/id_00, gearbox/section_01, ...
    split: str        # train | val | test
    label: int        # 0 normal, 1 anomalous
    condition: str    # 6dB | -6dB | source | target


def _split_normals(files, n_val, n_test):
    files = sorted(files)
    n_train = len(files) - n_val - n_test
    return ([(f, "train") for f in files[:n_train]]
            + [(f, "val") for f in files[n_train:n_train + n_val]]
            + [(f, "test") for f in files[n_train + n_val:]])


def mimii(mtype, snr="6dB"):
    clips = []
    root = RAW / f"mimii_{snr}" / mtype / mtype
    for unit_dir in sorted(root.glob("id_*")):
        unit = f"{mtype}/{unit_dir.name}"
        normals = list((unit_dir / "normal").glob("*.wav"))
        abnormal = sorted((unit_dir / "abnormal").glob("*.wav"))
        if snr == "6dB":
            split = _split_normals(normals, n_val=10, n_test=16)
        else:  # the noisy recordings are only ever used for testing
            split = [(f, "test") for f in sorted(normals)]
        clips += [Clip(f, mtype, unit, s, 0, snr) for f, s in split]
        clips += [Clip(f, mtype, unit, "test", 1, snr) for f in abnormal]
    return clips


def gearbox():
    clips = []
    root = RAW / "mimii_due" / "gearbox"
    for sec in ("00", "01", "02"):
        unit = f"gearbox/section_{sec}"
        train = list((root / "train").glob(f"section_{sec}_*.wav"))
        for f, s in _split_normals(train, n_val=10, n_test=0):
            clips.append(Clip(f, "gearbox", unit, s, 0, "source"))
        for domain in ("source", "target"):
            for f in sorted((root / f"{domain}_test").glob(f"section_{sec}_*.wav")):
                clips.append(Clip(f, "gearbox", unit, "test",
                                  int("_anomaly_" in f.name), domain))
    return clips


def clips_for(mtype):
    if mtype == "gearbox":
        return gearbox()
    return mimii(mtype, "6dB") + (mimii(mtype, "-6dB") if mtype == "pump" else [])
