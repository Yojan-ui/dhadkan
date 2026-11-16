"""Per-machine baseline: learning period, versions, drift safeguards, owner-approved recalibration.

Rows are synthetic summary vectors (no audio): a machine's "normal" is a fixed vector plus noise.
"""

import numpy as np
import pytest

from edge.baseline import BaselineStore
from edge.detectors import AE_COL, WIDTH
from edge.summary import S

DAY = 86400.0
OK = {"ok": True, "reasons": []}


def normal_rows(n: int, seed: int = 0, shift: float = 0.0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    base = np.zeros(WIDTH)
    base[S.log_rms] = -2.0
    base[S.mel_mean] = np.linspace(-3, -6, 64)
    base[S.mel_std] = 0.3
    base[S.mel_p95] = base[S.mel_mean] + 0.5
    base[S.mfcc_mean] = np.linspace(5, -5, 20)
    base[S.mfcc_std] = 1.0
    base[S.centroid] = 20.0
    base[S.flatness] = 0.3
    base[AE_COL] = 1.0
    x = base + rng.normal(0, 0.05, size=(n, WIDTH))
    x[:, S.log_rms] += shift
    return x


def fault_row(strength: float, seed: int) -> np.ndarray:
    """A growing fault: louder and a band rises (bearing tone)."""
    r = normal_rows(1, seed)[0]
    r[S.log_rms] += 0.3 * strength
    mm = r[S.mel_mean].copy()
    mm[40:48] += 1.5 * strength
    r[S.mel_mean] = mm
    r[S.mel_p95] = mm + 0.5
    return r


def learned_store(tmp_path, n: int = 40, **kw) -> tuple[BaselineStore, float]:
    st = BaselineStore(tmp_path / "b.db", detectors=["rms", "spectral"], **kw)
    st.register("m1", "pump", learning_days=3, now=0.0)
    rows = normal_rows(n)
    t = 0.0
    for i, r in enumerate(rows):
        t = i * (3.2 * DAY / n)
        out = st.observe("m1", r, OK, ts=t, source="simulated", reading_id=f"l{i}")
    assert out["state"] == "monitoring" and out["version"] == 1
    return st, t


def test_learning_period_raises_no_alarm_and_counts_days(tmp_path):
    st = BaselineStore(tmp_path / "b.db")
    st.register("m1", "pump", learning_days=5, now=0.0)
    out = st.observe("m1", normal_rows(1)[0], OK, ts=1.5 * DAY, source="simulated")
    assert out["state"] == "learning" and out["risk"] is None
    assert out["progress"] == {"day": 2, "of": 5}
    assert st.versions("m1") == []


def test_baseline_v1_after_learning_and_false_alarms_near_design(tmp_path):
    st, t = learned_store(tmp_path, n=60)
    v = st.versions("m1")
    assert (
        [b["version"] for b in v] == [1]
        and v[0]["reason"] == "learning_complete"
        and v[0]["status"] == "active"
    )
    risks = [st.observe("m1", r, OK, ts=t + (i + 1) * 3600, source="simulated")["risk"]
             for i, r in enumerate(normal_rows(300, seed=7))]  # fmt: skip
    far = float(np.mean(np.array(risks) >= 1))
    assert far < 0.15  # ~5% by design; small fit set widens it


def test_growing_fault_is_flagged_and_never_learned(tmp_path):
    st, t = learned_store(tmp_path, n=60, assumptions={"adapt_every": 5, "drift_window": 20})
    outs = []
    for i in range(80):
        strength = max(0.0, (i - 20) / 40)  # normal for 20 readings, then a slow ramp
        outs.append(
            st.observe("m1", fault_row(strength, 100 + i), OK, ts=t + (i + 1) * 3600, source="simulated")
        )
    assert outs[-1]["risk"] > 1.5
    assert not any(o["clean"] for o in outs if o["risk"] >= 0.8)
    # whatever versions were made, the final faulty readings are still above the alarm of the active one
    assert all(o["risk"] >= 1 for o in outs[-10:])
    assert all(b["reason"] in ("learning_complete", "auto_drift") for b in st.versions("m1"))


def test_small_drift_adapts_and_keeps_flagging_past_anomalies(tmp_path):
    st, t = learned_store(tmp_path, n=60, assumptions={"adapt_every": 20, "drift_window": 40})
    st.observe("m1", fault_row(1.0, 999), OK, ts=t + 3600, source="simulated")  # one real anomaly
    events = []
    for i, r in enumerate(normal_rows(40, seed=3, shift=0.04)):  # belt beds in: slightly louder
        events += st.observe("m1", r, OK, ts=t + (i + 2) * 3600, source="simulated").get("events") or []
    kinds = [e.get("result") for e in events]
    assert "adapted" in kinds or "stable" in kinds
    active = st.model("m1")
    assert active is not None
    assert active.risk(fault_row(1.0, 999)[None, :])[0] >= 1


def test_large_drift_needs_owner_recalibration(tmp_path):
    st, t = learned_store(
        tmp_path, n=60, assumptions={"adapt_every": 10, "drift_window": 10, "freeze_at": 5.0}
    )
    events = []
    for i, r in enumerate(normal_rows(10, seed=4, shift=0.3)):
        events += st.observe("m1", r, OK, ts=t + (i + 1) * 3600, source="simulated").get("events") or []
    assert events and events[-1]["result"] == "needs_owner_recalibration"
    assert len(st.versions("m1")) == 1


def test_recalibration_only_with_owner_approval(tmp_path):
    st, t = learned_store(tmp_path, n=60)
    ids = [st.observe("m1", r, OK, ts=t + (i + 1) * 3600, source="simulated")["id"]
           for i, r in enumerate(normal_rows(20, seed=5))]  # fmt: skip
    v = st.propose("m1", ids, reason="after_repair", note="new bearing")
    assert st.machine("m1")["active_version"] == 1
    with pytest.raises(PermissionError):
        st.approve("m1", v, approved_by="", approval_ref="")
    st.approve("m1", v, approved_by="owner", approval_ref="msg:42")
    assert st.machine("m1")["active_version"] == v
    b = {x["version"]: x for x in st.versions("m1")}
    assert b[1]["status"] == "retired" and b[v]["approval_ref"] == "msg:42"
    with pytest.raises(ValueError):
        st.approve("m1", v, approved_by="owner", approval_ref="msg:43")


def test_hold_gap_off_and_bad_quality_are_not_learned(tmp_path):
    st, t = learned_store(tmp_path, n=60)
    st.set_hold("m1", True)
    held = st.observe("m1", normal_rows(1, 11)[0], OK, ts=t + 3600, source="simulated")
    assert held["clean"] is False and held["excluded"] == "hold"
    st.set_hold("m1", False)
    gap = st.observe("m1", normal_rows(1, 12)[0], OK, ts=t + 3 * DAY, source="simulated")
    assert gap["after_gap"] is True and gap["clean"] is False
    off = normal_rows(1, 13)[0]
    off[S.log_rms] -= 2.0
    assert st.observe("m1", off, OK, ts=t + 3 * DAY + 3600, source="simulated")["state"] == "not_running"
    bad = st.observe("m1", normal_rows(1, 14)[0], {"ok": False, "reasons": ["clipped"]}, ts=t + 4 * DAY,
                     source="simulated")  # fmt: skip
    assert bad["state"] == "unusable" and bad["risk"] is None


def test_duplicate_reading_is_ignored_and_source_is_labelled(tmp_path):
    st, t = learned_store(tmp_path, n=60)
    a = st.observe("m1", normal_rows(1, 20)[0], OK, ts=t + 3600, source="upload", reading_id="r-1")
    b = st.observe("m1", normal_rows(1, 21)[0], OK, ts=t + 7200, source="upload", reading_id="r-1")
    assert b == {"duplicate": True, "id": a["id"]}
    assert st.history("m1", last=1)[0]["source"] == "upload"
    with pytest.raises(ValueError):
        st.observe("m1", normal_rows(1)[0], OK, source="made-up")


def test_versions_survive_restart(tmp_path):
    st, _t = learned_store(tmp_path, n=60)
    r = normal_rows(1, 30)[0]
    before = st.model("m1").risk(r[None, :])[0]  # type: ignore[union-attr]
    st2 = BaselineStore(tmp_path / "b.db", detectors=["rms", "spectral"])
    assert st2.model("m1").risk(r[None, :])[0] == pytest.approx(before)  # type: ignore[union-attr]
