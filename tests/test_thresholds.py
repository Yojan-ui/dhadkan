"""Alarm levels: the shipped phone model uses exactly the thresholds the evaluation measured."""

import json

import numpy as np
import pytest

from edge import scoring
from edge.config import MODELS, RUNS, THRESHOLD_QUANTILE
from edge.runtime import EdgeModel

EVALS = sorted(RUNS.glob("eval_*.json"))
pytestmark = pytest.mark.skipif(
    not EVALS or not (MODELS / "fan" / "meta.json").exists(), reason="needs a training run"
)


@pytest.fixture(scope="module")
def ev():
    return json.loads(EVALS[-1].read_text(encoding="utf-8"))


@pytest.mark.parametrize("mtype", ["pump", "fan", "gearbox"])
def test_threshold_is_95th_percentile_of_own_validation_normals(ev, mtype):
    rep = ev["types"][mtype]
    meta = json.loads((MODELS / mtype / "meta.json").read_text(encoding="utf-8"))
    for unit, thr in rep["thresholds"].items():
        val = [r["score"] for r in rep["clip_scores"] if r["unit"] == unit and r["split"] == "val"]
        assert len(val) >= 10
        assert thr == pytest.approx(float(np.quantile(val, THRESHOLD_QUANTILE)))
        assert meta["units"][unit]["threshold"] == pytest.approx(thr)  # the phone ships this number
        # by construction about 1 in 20 healthy readings crosses it; never most of them
        assert np.mean(np.array(val) > thr) <= 0.1


@pytest.mark.parametrize("mtype", ["pump", "fan", "gearbox"])
def test_runtime_risk_is_score_over_own_threshold(mtype):
    em = EdgeModel(mtype)
    unit = next(iter(em.meta["units"]))
    audio = np.random.default_rng(0).normal(0, 0.01, 160_000).astype(np.float32)
    r = em.score_audio(audio, unit)
    assert r["threshold"] == em.unit(unit)["threshold"]
    assert r["risk"] == pytest.approx(scoring.risk(r["score"], r["threshold"]))
    assert len(r["band_z"]) == 64 and len(r["embedding"]) == 64


def test_each_machine_has_its_own_alarm_level(ev):
    thr = ev["types"]["pump"]["thresholds"]
    assert len(thr) >= 2 and len({round(v, 6) for v in thr.values()}) == len(thr)
