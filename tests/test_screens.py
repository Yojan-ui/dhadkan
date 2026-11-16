"""Owner screen decisions (spec section 7) take the same path as a WhatsApp reply."""

import pytest

from tests.conftest import ims_world, needs_ims


@needs_ims
def test_owner_screen_details_then_book(tmp_path):
    mp = pytest.MonkeyPatch()
    try:
        mp.setenv("DHADKAN_MECHANIC_A", "+91 90000 55555")
        _, client, live, rec = ims_world(tmp_path, mp)
        d = client.post(
            "/api/owner/decide", json={"machine": "A-G1", "choice": "details", "lang": "gu"}
        ).json()
        assert "રિપેર થઈ શકે" in d["text"]  # details in the owner's language
        modes = {m: live.c.mode(m) for m in live.c.machines}
        out = client.post("/api/owner/decide", json={"machine": "A-G1", "choice": "book"}).json()
        assert out["booked"] and out["job_status"] == "sent"
        assert any(s["to"] == "+91 90000 55555" for s in rec.sent)  # the mechanic got the job
        assert {m: live.c.mode(m) for m in live.c.machines} == modes  # no machine touched
        again = client.post("/api/owner/decide", json={"machine": "A-G1", "choice": "book"}).json()
        assert again["booked"] is False  # nothing left to approve
        assert client.post("/api/owner/decide", json={"machine": "A-G1", "choice": "stop"}).status_code == 422
    finally:
        mp.undo()
