// The twin's visual state comes only from backend fields; a lost sensor is never shown as stopped.
import { test } from "node:test";
import assert from "node:assert/strict";
import { isRunning, twinState, twinWords } from "../src/theme.ts";
import type { Machine } from "../src/api.ts";

const base = {
  id: "A-P1",
  name: "Pump 1",
  status: "ok",
  risk3: 0.6,
  latest: { tick: 3, risk: 0.6 },
  learning: null,
  operating: {
    running: true,
    run: "running",
    sensor: "live",
    last_ts: "2026-11-24T22:00",
    ticks_since: 0,
    simulated: false,
  },
} as unknown as Machine;
const m = (over: object) => ({ ...base, ...over }) as Machine;

test("states map from status and operating state", () => {
  assert.equal(twinState(base), "normal");
  assert.equal(twinState(m({ status: "watch" })), "watch");
  assert.equal(twinState(m({ status: "alarm" })), "alert");
  assert.equal(twinState(m({ status: "early_match" })), "alert");
  assert.equal(twinState(m({ status: "learning" })), "learning");
  assert.equal(twinState(m({ latest: null, status: "no_data" })), "offline");
});

test("stopped stops the animation; a lost sensor keeps the last known state and says so", () => {
  const stopped = m({
    operating: { ...base.operating, running: false, run: "stopped", sensor: "stale", simulated: true },
  });
  const lost = m({ operating: { ...base.operating, running: null, sensor: "lost", simulated: true } });
  assert.equal(twinState(stopped), "offline");
  assert.equal(isRunning(stopped), false);
  assert.equal(twinWords(stopped), "stopped");
  assert.equal(twinState(lost), "offline");
  assert.equal(isRunning(lost), true);
  assert.match(twinWords(lost), /^no signal · last /);
});

test("labels show risk against the machine's own alarm, never a health percentage", () => {
  assert.equal(twinWords(m({ status: "alarm", risk3: 1.09 })), "alert · 1.09× alarm");
  assert.doesNotMatch(twinWords(base), /%/);
});
