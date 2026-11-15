"""Offline queue: fingerprints wait for signal, arrive once, in order, and survive restarts."""

import numpy as np
import pytest
from fastapi.testclient import TestClient

from edge.config import EMBED_DIM
from edge.outbox import Outbox, http_send
from server import registry, registry_api


def fp(i=0, **kw):
    v = np.zeros(EMBED_DIM)
    v[i] = 1.0
    return {
        "machine_type": "fan",
        "fault": "bearing",
        "owner_label": "khat-khat",
        "factory": "A",
        "machine": f"A-F{i}",
        "embedding": v.tolist(),
        **kw,
    }


class Net:
    """In-process registry behind a switchable link. `lose_ack` stores the item but drops the reply."""

    def __init__(self, db):
        self.db, self.up, self.lose_ack, self.calls = db, True, False, 0

    def __call__(self, kind, p):
        self.calls += 1
        if not self.up:
            raise ConnectionError("no signal")
        fid = registry.publish(
            self.db, p["machine_type"], p["fault"], p["owner_label"], p["factory"], p["machine"],
            p["embedding"], None, p["idem"],
        )  # fmt: skip
        if self.lose_ack:
            self.lose_ack = False
            raise TimeoutError("reply lost")
        return {"id": fid}


@pytest.fixture()
def reg(tmp_path):
    return registry.connect(tmp_path / "registry.db")


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_offline_then_online_delivers_once(tmp_path, reg):
    net, clock = Net(reg), Clock()
    box = Outbox(tmp_path / "out.db", net, clock)
    net.up = False
    box.put("fingerprint", fp(1))
    r = box.flush()
    assert r["sent"] == [] and r["pending"] == 1 and registry.fingerprints(reg) == []
    net.up = True
    assert box.flush()["pending"] == 1  # still backing off: no retry storm
    clock.t += 10
    r = box.flush()
    assert len(r["sent"]) == 1 and r["pending"] == 0
    assert len(registry.fingerprints(reg)) == 1


def test_lost_reply_is_not_stored_twice(tmp_path, reg):
    net, clock = Net(reg), Clock()
    box = Outbox(tmp_path / "out.db", net, clock)
    net.lose_ack = True
    box.put("fingerprint", fp(2))
    assert box.flush()["pending"] == 1  # registry stored it, phone never heard back
    clock.t += 10
    r = box.flush()
    assert r["pending"] == 0 and net.calls == 2
    rows = registry.fingerprints(reg)
    assert len(rows) == 1 and r["sent"][0]["result"]["id"] == rows[0]["id"]


def test_queue_survives_restart_and_keeps_order(tmp_path, reg):
    net = Net(reg)
    net.up = False
    box = Outbox(tmp_path / "out.db", net)
    for i in (3, 4, 5):
        box.put("fingerprint", fp(i))
    box.flush()
    del box
    net.up = True
    again = Outbox(tmp_path / "out.db", net)  # phone restarted
    assert [p["payload"]["machine"] for p in again.pending()] == ["A-F3", "A-F4", "A-F5"]
    r = again.flush(force=True)
    assert [s["payload"]["machine"] for s in r["sent"]] == ["A-F3", "A-F4", "A-F5"]
    assert [f["machine"] for f in registry.fingerprints(reg)] == ["A-F3", "A-F4", "A-F5"]


def test_refused_item_is_set_aside_not_retried(tmp_path, reg):
    net = Net(reg)
    box = Outbox(tmp_path / "out.db", net)
    box.put("fingerprint", fp(6, embedding=[0.0] * EMBED_DIM))  # all-zero embedding: registry refuses
    box.put("fingerprint", fp(7))
    r = box.flush()
    assert len(r["rejected"]) == 1 and "zeros" in r["rejected"][0]["error"]
    assert [s["payload"]["machine"] for s in r["sent"]] == ["A-F7"] and r["pending"] == 0
    assert box.flush()["sent"] == []


def test_outbox_never_carries_audio(tmp_path, reg):
    box = Outbox(tmp_path / "out.db", Net(reg))
    with pytest.raises(ValueError, match="audio"):
        box.put("fingerprint", {**fp(1), "audio": [0.1, 0.2]})


def test_http_transport_against_registry_api(tmp_path):
    client = TestClient(registry_api.create_app(tmp_path / "r.db", llm=None))
    box = Outbox(tmp_path / "out.db", http_send("http://registry", client=client))
    box.put("fingerprint", fp(8))
    box.put("fingerprint", fp(9))
    r = box.flush()
    assert len(r["sent"]) == 2 and r["pending"] == 0
    # the same delivery again (as after a lost reply) maps to the same row
    again = http_send("http://registry", client=client)(
        "fingerprint", {**fp(8), "idem": r["sent"][0]["idem"]}
    )
    assert again["id"] == r["sent"][0]["result"]["id"]
    assert len(client.get("/fingerprints").json()) == 2


def test_http_transport_offline_keeps_item(tmp_path):
    send = http_send("http://127.0.0.1:9", timeout=0.2)  # nothing listens on port 9
    box = Outbox(tmp_path / "out.db", send)
    box.put("fingerprint", fp(1))
    assert box.flush()["pending"] == 1
