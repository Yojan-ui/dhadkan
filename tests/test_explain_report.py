"""Explain an alert by sound; the time machine's history; the owner's savings report."""

import asyncio

import librosa
import numpy as np
import pytest
from fastapi.testclient import TestClient

from edge.config import MODELS
from server import app as app_mod
from server import explain, folk, forecast, report
from tests.conftest import ims_world, needs_ims

pytestmark = pytest.mark.skipif(not (MODELS / "fan" / "model.tflite").exists(), reason="needs trained models")
NOTE = "ब्लोअर में खट-खट की आवाज़ आ रही है"


def test_changed_band_picks_the_strongest_run():
    z = [0.0] * 64
    z[10:13] = [2.5, 3.0, 2.5]  # sum 8
    z[40:42] = [6.0, 5.0]  # sum 11
    assert explain.changed_band(z) == (40, 41)
    assert explain.changed_band([1.0] * 64) is None


def test_pitch_words_cover_the_spectrum():
    assert explain.pitch_word(100) == "low rumble"
    assert explain.pitch_word(2500) == "high-pitched whine"
    assert explain.pitch_word(7000) == "hiss or squeal"


def test_change_is_measured_on_the_audio():
    sr = 16_000
    t = np.arange(10 * sr) / sr
    before = 0.01 * np.sin(2 * np.pi * 3000 * t).astype(np.float32)
    now = (before * 2.0).astype(np.float32)  # 4x the power at 3 kHz
    hz = librosa.mel_frequencies(66, fmin=0, fmax=sr / 2)[1:-1].tolist()  # same bands as the app
    band = int(np.argmin(np.abs(np.asarray(hz) - 3000)))
    z = [0.0] * 64
    z[band] = 5.0
    ex = explain.explain(now, before, z, hz)
    assert ex["status"] == "ok" and ex["word"] == "high-pitched whine"
    assert ex["change_pct"] == pytest.approx(300, abs=5)
    assert len(explain.cut(now, ex["start_s"])) == 3 * sr


def test_inr_uses_indian_grouping():
    assert report.inr(1234567) == "Rs 12,34,567"
    assert report.inr(950) == "Rs 950"
    assert report.inr(93310) == "Rs 93,310"


@pytest.fixture(scope="module")
def replayed(tmp_path_factory):
    folk_transcribe = folk.transcribe
    folk.transcribe = lambda path, language=None, **k: (NOTE, language)
    d = tmp_path_factory.mktemp("replay")
    app = app_mod.create_app(d / "node.db", d / "registry.db", stream=False)
    asyncio.run(app.state.live.run_replay(tick_s=0))
    folk.transcribe = folk_transcribe
    return app, TestClient(app)


def test_time_machine_history_marks_warning_and_fault(replayed):
    _, client = replayed
    h = client.get("/api/history/A-F1").json()
    assert len(h["rows"]) == 14 * 3
    assert h["first_warning"]["tick"] < h["fault_sound"]["tick"]  # warned days before
    warn = next(r for r in h["rows"] if r["tick"] == h["first_warning"]["tick"])
    # A-F1 replays fan id_00. If its recorded faults score below the alarm on average (the model can't
    # separate them; docs/RESULTS.md), the page must say so instead of counting down.
    separable = forecast.fault_levels()["fan/id_00"]["level"] > 1.0
    assert warn["status"] in ("alarm", "early_match")
    assert warn["forecast"]["status"] == ("estimate" if separable else "not_separable")
    before = [r for r in h["rows"] if r["tick"] < h["first_warning"]["tick"]]
    assert all(r["status"] not in ("alarm", "early_match") for r in before)
    r = client.get(f"/api/reading/A-F1?tick={h['rows'][0]['tick']}").json()
    assert len(r["spec"]) == 20 and r["mode"] == "normal"


@pytest.fixture(scope="module")
def failed_bearing(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    yield ims_world(tmp_path_factory.mktemp("ims"), mp)
    mp.undo()


@needs_ims
def test_explain_endpoint_and_clips(failed_bearing):
    """At the end of the real IMS run the vibration has changed a lot: the page can say where and play it."""
    _, client, *_ = failed_bearing
    ex = client.get("/api/explain/A-G1").json()
    assert ex["status"] == "ok" and ex["lo_hz"] < ex["hi_hz"] and ex["change_pct"] > 0
    assert ex["before"]["tick"] < ex["now"]["tick"]
    for which in ("before", "now"):
        wav = client.get(f"/api/explain/A-G1/{which}.wav")
        assert wav.status_code == 200 and wav.content[:4] == b"RIFF"
        assert abs(len(wav.content) - 44 - 3 * 16_000 * 2) <= 2  # 3 s of 16-bit mono


@needs_ims
def test_savings_counts_only_approved_visits(failed_bearing):
    app, client, *_ = failed_bearing
    assert client.get("/api/report/A").json()["visits"] == []
    msg = next(m for m in app.state.live.messages() if m["machine"] == "A-G1")
    client.post("/api/reply", json={"message_id": msg["id"], "reply": "हाँ"})
    rep = client.get("/api/report/A").json()
    assert [v["machine"] for v in rep["visits"]] == ["A-G1"]
    assert rep["totals"]["saved_inr"] == rep["visits"][0]["saved_inr"] > 0
    assert "alarm" in rep["visits"][0]["how"]
    pdf = client.get("/api/report/A.pdf")
    assert pdf.headers["content-type"] == "application/pdf" and pdf.content[:4] == b"%PDF"
    assert client.get("/api/report/Z.pdf").status_code == 404
