"""Prove (spec section 5): repair verification, signed records (tamper check), passport privacy."""

import copy
import json

import pytest

from server import prove
from tests.conftest import OWNER_A, ims_world, needs_ims


@pytest.fixture(autouse=True)
def _keys(tmp_path, monkeypatch):
    monkeypatch.setattr(prove, "KEYS", tmp_path / "keys")
    monkeypatch.setattr(prove, "LABELS", tmp_path / "labels" / "verified.jsonl")


@pytest.mark.parametrize(
    "before,after,want",
    [([2.0, 2.4, 2.2], [0.5, 0.6, 0.7, 0.6, 0.5, 0.6], "fixed"),
     ([3.0, 3.0, 3.0], [1.5, 1.4, 1.6, 1.5, 1.5, 1.5], "partly"),
     ([2.0, 2.0, 2.0], [1.9, 2.1, 2.0, 2.0, 1.95, 2.05], "failed")],
)  # fmt: skip
def test_verification_outcomes(before, after, want):
    v = prove.verify_repair(before, after)
    assert v["outcome"] == want and v["words"]["hi_latn"]
    if want == "failed":
        assert "nakli part ho sakta hai" in v["words"]["hi_latn"]  # "possible", never an accusation


def test_signature_detects_tampering():
    doc = {
        "kind": "test",
        "machine": {"id": "A-G1"},
        "verification": {"outcome": "fixed", "after_median_risk": 0.6},
    }
    sig = prove.sign(doc)
    assert prove.verify_signature(doc, sig["signature"])
    forged = copy.deepcopy(doc)
    forged["verification"]["outcome"] = "failed"
    assert not prove.verify_signature(forged, sig["signature"])
    assert not prove.verify_signature(doc, sig["signature"][:-4] + "AAAA")
    assert (
        prove.KEYS / "records_ed25519.pem"
    ).stat().st_mode & 0o777 == 0o600  # private key not world-readable


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setattr(prove, "KEYS", tmp_path_factory.mktemp("k") / "keys")
    mp.setattr(prove, "LABELS", tmp_path_factory.mktemp("l") / "verified.jsonl")
    yield ims_world(tmp_path_factory.mktemp("prove"), mp)
    mp.undo()


@needs_ims
def test_repair_record_passport_and_privacy(world):
    _, client, _, _ = world
    client.post("/api/alerts/reply", json={"phone": OWNER_A, "text": "1"})
    before = client.get("/api/passport/A-G1").json()["passport"]
    assert before["grade"] in ("C", "D")  # an open alert on a failing bearing
    rec = client.post("/api/repair/A-G1", json={"mechanic": "Ramesh", "part": "bearing 6205"}).json()
    v = rec["record"]["verification"]
    assert v["outcome"] == "fixed" and rec["record"]["demo_note"]  # the demo says where "after" came from
    assert client.post(
        "/api/records/verify", json={"record": rec["record"], "signature": rec["signature"]}
    ).json()["valid"]
    bad = {**rec["record"], "part": "something else"}
    assert not client.post("/api/records/verify", json={"record": bad, "signature": rec["signature"]}).json()[
        "valid"
    ]
    pdf = client.get(f"/api/records/{rec['id']}.pdf")
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    assert prove.LABELS.exists()  # a verified repair becomes a labelled fault
    p = client.get("/api/passport/A-G1").json()
    text = json.dumps(p)
    assert p["passport"]["grade"] == "B" and p["passport"]["verified_repairs"][0]["outcome"] == "fixed"
    for leak in ("Ramesh", OWNER_A, "+91", ".ogg", ".wav", "audio_path", "success_rate"):
        assert leak not in text, leak  # numbers only: no mechanic, no phone, no audio
    assert prove.verify_signature(p["passport"], p["signature"])
    stats = client.get("/api/owner/A/mechanics").json()
    assert stats == [
        {"mechanic": "Ramesh", "repairs": 1, "fixed": 1, "partly": 0, "failed": 0, "success_rate": 1.0}
    ]
    assert all("mechanic" not in r for r in client.get("/api/records").json())
    rows = client.get("/api/forecast/parts?days=14").json()["rows"]
    assert all("A-G1" not in e["machines"] for e in rows)  # a verified repair needs no part
