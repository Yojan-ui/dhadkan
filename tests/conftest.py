"""Shared fixtures: a live server that has replayed the real NASA IMS bearing failure (Demo mode).

The alert, WhatsApp, diagnosis and booking paths are exercised on Loom 1 (A-G1), which replays NASA IMS
test 2 bearing 1. That machine deterministically crosses its alarm on real data. The MIMII/DCASE
units the scripted story replays do not: on the official DCASE test split the model cannot separate
fan id_00's faults (docs/RESULTS.md), so tests must not depend on them alarming.
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from edge.config import MODELS, RAW

OWNER_A, SUPERVISOR_A = "+91 90000 11111", "+91 90000 22222"
# a coarser replay than the UI's (~110 readings); the alarm still comes from the real curve
TEST_READINGS = 40

needs_ims = pytest.mark.skipif(
    not (MODELS / "ims_replay.json").exists() or not (RAW / "ims" / "2nd_test_b1.npy").exists(),
    reason="needs NASA IMS data (make data) and edge/models/ims_replay.json",
)


class Recorder:
    """A WhatsApp provider that records instead of sending."""

    name = "whatsapp_meta"

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(self, to: str, text: str, media: list[Any], lang: str = "en") -> dict[str, Any]:
        self.sent.append({"to": to, "text": text, "media": [m.mime for m in media], "lang": lang})
        return {"provider": self.name, "ids": [f"wamid.{len(self.sent)}"]}


def ims_world(d: Path, mp: pytest.MonkeyPatch, to: str | None = None, lang: str | None = None):
    """Run Demo mode to the end at full speed. Returns (app, client, live, recorder)."""
    from server import app as app_mod
    from server import imsdemo
    from server.owner_alerts import OwnerAlerts

    for k in ("WA_TOKEN", "WA_PHONE_NUMBER_ID", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"):
        mp.delenv(k, raising=False)
    mp.setenv("DHADKAN_PHONE_A", OWNER_A)
    mp.setenv("DHADKAN_SUPERVISOR_A", SUPERVISOR_A)
    mp.setenv("WA_APP_SECRET", "test-secret")
    mp.setenv("WA_VERIFY_TOKEN", "verify-me")
    mp.setattr(imsdemo, "READINGS", TEST_READINGS)
    app = app_mod.create_app(d / "node.db", d / "registry.db", stream=False)
    live = app.state.live
    rec = Recorder()
    live.alerts = OwnerAlerts(live, d / "node.db", providers=(rec, rec))
    asyncio.run(live.run_ims_demo(to, lang, tick_s=0))
    return app, TestClient(app), live, rec
