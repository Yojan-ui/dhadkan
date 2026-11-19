// The live recorder's encoding: what the phone sends must be exactly what /api/ingest/reading decodes.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  base64,
  level,
  readingBody,
  resample,
  sendOrder,
  SR,
  toInt16,
  type Pending,
} from "../src/screens/recorderCore.ts";

test("int16 little-endian, clipped to -1..1", () => {
  const b = toInt16(new Float32Array([0, 1, -1, 2, 0.5]));
  const v = new DataView(b.buffer);
  assert.deepEqual(
    [0, 1, 2, 3, 4].map((i) => v.getInt16(i * 2, true)),
    [0, 32767, -32767, 32767, 16384],
  );
});

test("base64 matches the standard encoding for every remainder", () => {
  for (const s of ["", "a", "ab", "abc", "abcd", "Dhadkan live ✓"]) {
    const bytes = new TextEncoder().encode(s);
    assert.equal(base64(bytes), Buffer.from(bytes).toString("base64"));
  }
});

test("resample 48 kHz to 16 kHz keeps the length and a low tone", () => {
  const n = 48000;
  const x = new Float32Array(n);
  for (let i = 0; i < n; i++) x[i] = Math.sin((2 * Math.PI * 100 * i) / 48000);
  const y = resample(x, 48000);
  assert.equal(y.length, SR);
  const peak = Math.max(...Array.from(y.slice(1000, 2000)).map(Math.abs));
  assert.ok(peak > 0.9 && peak <= 1.0, `peak ${peak}`);
  assert.equal(resample(x, SR), x); // already 16 kHz: untouched
});

test("level meter: silence is empty, full scale is full", () => {
  assert.equal(level(new Float32Array(100)).bar, 0);
  assert.ok(level(new Float32Array(100).fill(1)).bar > 0.99);
});

test("queued clips are sent oldest first with the idempotency key", () => {
  const p = (seq: number, ts: number): Pending => ({
    key: `d:${seq}`,
    machine: "A-F1",
    device: "d",
    seq,
    ts,
    samples_b64: "",
  });
  assert.deepEqual(
    sendOrder([p(3, 30), p(1, 10), p(2, 10)]).map((x) => x.seq),
    [1, 2, 3],
  );
  const body = readingBody(p(7, 70));
  assert.deepEqual(
    [body.device, body.seq, body.source, body.kind, body.sr],
    ["d", 7, "live", "audio", 16000],
  );
});
