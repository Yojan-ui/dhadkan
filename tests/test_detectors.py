"""Unsupervised detectors: each separates a synthetic fault from one machine's normal; the comparison
protocol never chooses on held-out units; the product uses the record's choice."""

import json

import numpy as np
import pytest

from edge import compare, detectors, summary
from edge.detectors import DETECTORS, MachineModel
from tests.test_baseline import fault_row, normal_rows


def broad_fault(seed: int) -> np.ndarray:
    """A fault that also moves the cepstrum and the autoencoder score (each detector sees its own part)."""
    r = fault_row(1.0, seed)
    r[summary.S.mfcc_mean] += 1.0
    r[detectors.AE_COL] += 0.5
    return r


@pytest.mark.parametrize("name", [n for n in DETECTORS if n != "transient"])
def test_each_detector_ranks_a_clear_fault_above_normal(name):
    m = MachineModel([name]).fit(normal_rows(80, seed=1))
    normal = m.risk(normal_rows(50, seed=2))
    faulty = m.risk(np.vstack([broad_fault(100 + i) for i in range(20)]))
    assert np.median(faulty) > np.quantile(normal, 0.9)


def test_alarm_level_is_the_machines_own_95th_percentile():
    m = MachineModel(["rms", "spectral"]).fit(normal_rows(200, seed=3))
    assert float(np.mean(m.oof_risk >= 1)) == pytest.approx(0.05, abs=0.02)
    with pytest.raises(ValueError):
        MachineModel(["rms"]).fit(normal_rows(5))


def test_explanation_names_the_changed_bands_and_loudness():
    m = MachineModel(["rms"]).fit(normal_rows(80, seed=4))
    ex = m.explain(fault_row(1.0, 7))
    assert ex["loudness_db"] > 3
    assert {b["band"] for b in ex["bands"]} <= set(range(40, 48))


def test_autoencoder_is_skipped_when_a_reading_has_no_score():
    x = normal_rows(30)
    x[:, detectors.AE_COL] = np.nan
    assert MachineModel(["rms", "ae"]).fit(x).names == ["rms"]


def test_summary_and_quality_on_real_shaped_audio():
    rng = np.random.default_rng(0)
    a = (0.05 * rng.standard_normal(16000 * 3)).astype(np.float32)
    v = summary.summarise(a)
    assert v.shape == (summary.DIM,) and np.isfinite(v).all()
    assert summary.quality(a)["ok"]
    clipped = a.copy()
    clipped[:2000] = 1.0
    assert summary.quality(clipped)["reasons"] == ["clipped"]
    silent = np.zeros_like(a)
    assert "dropout" in summary.quality(silent)["reasons"]
    assert summary.quality(a[:8000])["reasons"] == ["too_short"]


def test_selection_and_held_out_units_never_overlap():
    rec_path = compare.latest()
    assert rec_path is not None, "run python -m edge.compare"
    rec = json.loads(rec_path.read_text(encoding="utf-8"))
    sel, held = set(rec["protocol"]["selection_units"]), set(rec["protocol"]["held_out_units"])
    assert sel and held and not sel & held
    assert all(u.split("/")[1] in compare.SELECTION_IDS for u in sel)
    best = max(rec["selection"], key=lambda n: (rec["selection"][n]["pauc"], rec["selection"][n]["auc"]))
    assert rec["chosen"] == best  # chosen from the selection numbers only
    assert detectors.chosen()["candidate"] == rec["chosen"]
