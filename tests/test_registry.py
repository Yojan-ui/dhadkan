import numpy as np
import pytest
from fastapi.testclient import TestClient

from dhadkan import api, embed, registry
from dhadkan.config import EMBED_DIM


def unit(i):
    v = np.zeros(EMBED_DIM)
    v[i] = 1.0
    return v.tolist()


@pytest.fixture
def db(tmp_path):
    return registry.connect(tmp_path / "r.db")


@pytest.fixture
def client(tmp_path):
    stub = lambda text, lex: {"sound": "dhum", "fault": "imbalance",
                              "scores": {"imbalance": 1.0}, "source": "llm"}
    return TestClient(api.create_app(tmp_path / "r.db", llm=stub))


def test_publish_and_match_sorted_by_cosine(db):
    registry.publish(db, "pump", "bearing", "khat-khat", "A", "A-P1", unit(0))
    registry.publish(db, "pump", "clog", "gud-gud", "A", "A-P2", unit(1))
    q = np.array(unit(0)) * 0.9 + np.array(unit(1)) * 0.1
    m = registry.match(db, "pump", q.tolist())
    assert [x["fault"] for x in m] == ["bearing", "clog"]
    assert m[0]["cosine"] == pytest.approx(0.9 / np.hypot(0.9, 0.1))


def test_match_only_same_machine_type(db):
    registry.publish(db, "fan", "imbalance", "thar-thar", "A", "A-F1", unit(0))
    assert registry.match(db, "pump", unit(0)) == []


def test_match_excludes_own_fingerprint(db):
    registry.publish(db, "pump", "bearing", "khat-khat", "A", "A-P1", unit(0))
    assert registry.match(db, "pump", unit(0), exclude_machine="A-P1") == []


def test_stored_embedding_is_unit_length(db):
    registry.publish(db, "pump", "bearing", "x", "A", "A-P1", (np.array(unit(3)) * 7).tolist())
    v = np.array(registry.fingerprints(db)[0]["embedding"])
    assert np.linalg.norm(v) == pytest.approx(1.0)


@pytest.mark.parametrize("bad", [[0.0] * EMBED_DIM, [1.0] * (EMBED_DIM - 1),
                                 [float("nan")] + [0.0] * (EMBED_DIM - 1)])
def test_rejects_bad_embeddings(db, bad):
    with pytest.raises(ValueError):
        registry.publish(db, "pump", "bearing", "x", "A", "A-P1", bad)


def test_cosine_identity_and_orthogonal():
    assert embed.cosine(unit(0), [unit(0), unit(1)]).tolist() == pytest.approx([1.0, 0.0])


def test_api_publish_list_match(client):
    r = client.post("/fingerprints", json={"machine_type": "pump", "fault": "bearing",
                                           "owner_label": "खट-खट", "factory": "A",
                                           "machine": "A-P1", "embedding": unit(2)})
    assert r.status_code == 200
    assert len(client.get("/fingerprints", params={"machine_type": "pump"}).json()) == 1
    m = client.post("/match", json={"machine_type": "pump", "embedding": unit(2)}).json()
    assert m[0]["cosine"] == pytest.approx(1.0)


def test_api_refuses_audio(client):
    """Only embeddings cross the boundary: an extra audio field is rejected."""
    r = client.post("/fingerprints", json={"machine_type": "pump", "fault": "bearing",
                                           "owner_label": "x", "factory": "A", "machine": "A-P1",
                                           "embedding": unit(0), "audio": [0.1] * 16000})
    assert r.status_code == 422


def test_api_refuses_long_vectors(client):
    r = client.post("/match", json={"machine_type": "pump", "embedding": [0.1] * 16000})
    assert r.status_code == 422


def test_api_unknown_machine_type(client):
    r = client.post("/match", json={"machine_type": "loom", "embedding": unit(0)})
    assert r.status_code == 422


@pytest.mark.parametrize("text,fault,sound", [
    ("पंप से खट-खट आवाज़ आ रही है", "bearing", "khat-khat"),
    ("मोटर मधून शिट्टी सारखा आवाज येतोय", "loose_belt", "seeti"),
    ("blower is going thar thar since morning", "imbalance", "thar-thar"),
    ("पाइप में गुड़गुड़ हो रहा है", "clog", "gud-gud"),
])
def test_api_text_label_from_lexicon(client, text, fault, sound):
    r = client.post("/labels/text", json={"text": text, "factory": "A", "machine": "A-P1"}).json()
    assert (r["fault"], r["sound"], r["source"]) == (fault, sound, "lexicon")
    assert r["id"] >= 1


def test_api_text_label_llm_fallback(client):
    r = client.post("/labels/text", json={"text": "धुम धुम करता है"}).json()
    assert r["source"] == "llm" and r["fault"] == "imbalance"


def test_api_voice_label_transcribes_locally(client, monkeypatch):
    monkeypatch.setattr(api.folk, "transcribe", lambda path: ("पंप में घर-घर हो रहा है", "hi"))
    r = client.post("/labels/voice", files={"audio": ("note.ogg", b"fake", "audio/ogg")},
                    data={"factory": "A", "machine": "A-P1"}).json()
    assert r["fault"] == "bearing" and r["sound"] == "ghar-ghar"
