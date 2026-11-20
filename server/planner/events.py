"""Events: what happened, typed, stored once, handled once.

    bus = EventBus(store)
    bus.emit("alarm", machine="A-G1", factory="A", payload={...}, idem="alarm:A-G1#1")
    bus.subscribe(planner.handle)
    bus.drain()                       # handle what is pending (also after a restart)

Every event is a row in the maintenance store (table events) keyed by an idempotency key: the same
key emitted twice (a retried webhook, a restart mid-tick) is stored and handled once. A handler that
raises leaves the event `failed` with the error, never half-handled silently; `retry_failed()` puts
failed events back in the queue.

Who emits what:
    brain.on_tick        alarm, health_change, daily_tick
    maint/agent          verify_result, repair_done, network_restored, part_out_of_stock,
                         mechanic_reply (a booking was declined), mechanic_no_show
    owner_alerts         owner_reply, owner_voice_note, mechanic_reply (a message from the mechanic)
    owner_agent.chat     owner_reply (channel "chat"), handled at once so the owner gets an answer
"""

import hashlib
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from server.maint.store import Store

TYPES = ("alarm", "health_change", "owner_reply", "owner_voice_note", "owner_photo", "investigation_result",
         "mechanic_reply", "mechanic_no_show", "part_out_of_stock", "network_restored", "repair_done",
         "verify_result", "daily_tick")  # fmt: skip


@dataclass
class Event:
    type: str
    idem: str
    factory: str | None = None
    machine: str | None = None
    case_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    source: str = "system"
    ts: float = 0.0
    status: str = "pending"

    @classmethod
    def from_row(cls, r: dict[str, Any]) -> "Event":
        return cls(type=r["type"], idem=r["idem"], factory=r["factory"], machine=r["machine"],
                   case_id=r["case_id"], payload=r["payload"] or {}, source=r["source"], ts=r["ts"],
                   status=r["status"])  # fmt: skip


def default_idem(type_: str, key: str | None, payload: dict[str, Any]) -> str:
    """A stable key from the event's content, for emitters that have no natural key."""
    h = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:12]
    return f"{type_}:{key or '-'}:{h}"


Handler = Callable[[Event], Any]


class EventBus:
    def __init__(self, store: Store, lock: Any = None) -> None:
        self.s = store
        self.lock = lock or threading.RLock()
        self.handlers: list[Handler] = []
        self.wake = threading.Event()
        self._draining = threading.Lock()

    def subscribe(self, handler: Handler) -> None:
        self.handlers.append(handler)

    def emit(self, type_: str, *, machine: str | None = None, factory: str | None = None,
             case_id: str | None = None, payload: dict[str, Any] | None = None, idem: str | None = None,
             source: str = "system") -> Event | None:  # fmt: skip
        """Store the event. Returns it if new, None if this key was seen before (a duplicate)."""
        if type_ not in TYPES:
            raise ValueError(f"unknown event type {type_!r}")
        payload = payload or {}
        idem = idem or default_idem(type_, case_id or machine or factory, payload)
        with self.lock:
            new = self.s.put_event(idem, type_, factory, machine, case_id, payload, source)
            if not new:
                return None
            self.s.log(source, "event", case_id, type=type_, idem=idem, machine=machine)
            row = self.s.event(idem)
        self.wake.set()
        assert row is not None
        return Event.from_row(row)

    def pending(self, limit: int = 50) -> list[Event]:
        with self.lock:
            return [Event.from_row(r) for r in self.s.events(status="pending", limit=limit)]

    def handle(self, ev: Event) -> Any:
        """Run every handler on one event; the event ends `handled` or `failed` (with the error)."""
        with self.lock:
            row = self.s.event(ev.idem)
            if row is None or row["status"] == "handled":
                return None
            self.s.set_event(ev.idem, status="running", attempts=row["attempts"] + 1)
        out = None
        try:
            for h in self.handlers:
                out = h(ev)
        except Exception as e:
            with self.lock:
                self.s.set_event(ev.idem, status="failed", error=f"{type(e).__name__}: {e}"[:500])
                self.s.log("system", "event_failed", ev.case_id, idem=ev.idem, error=str(e)[:300])
            raise
        with self.lock:
            decision = out.get("decision") if isinstance(out, dict) else None
            self.s.set_event(ev.idem, status="handled", decision=decision, handled=self.s.clock(), error=None)
        return out

    def drain(self, limit: int = 50) -> int:
        """Handle pending events in order (one drainer at a time). Events left `running` by a crash are
        taken up again first. Returns how many were handled."""
        if not self._draining.acquire(blocking=False):
            return 0
        try:
            with self.lock:
                for r in self.s.events(status="running"):  # a restart in the middle of a handler
                    self.s.set_event(r["idem"], status="pending")
            n = 0
            for ev in self.pending(limit):
                try:
                    self.handle(ev)
                    n += 1
                except Exception:  # recorded on the event; the next one still runs
                    continue
            return n
        finally:
            self._draining.release()

    def retry_failed(self) -> int:
        with self.lock:
            rows = self.s.events(status="failed")
            for r in rows:
                self.s.set_event(r["idem"], status="pending")
        return len(rows)
