// Live data from dhadkan/live.py. One WebSocket: "state" snapshots when something
// changes, "frame" at 2 Hz (the 0.5 s slice under the play cursor of each machine's
// latest 10 s reading). Nothing here invents a value; missing data stays null.
import { useEffect, useSyncExternalStore } from "react";

export type Shape = "pump" | "motor" | "loom" | "mill";
export type Status = "ok" | "watch" | "alarm" | "early_match" | "no_data";

export interface Registry {
  cosine: number;
  flag_level: number;
  matched: boolean;
  fault: string;
  owner_label: string;
  from_area: string;
  from_area_local: string;
}
export interface Reading {
  tick: number;
  ts: string;
  clip: string;
  score: number;
  threshold: number;
  risk: number;
  match_cos: number | null;
  match_level: number | null;
  mode: string;
  mix: number;
}
export interface Machine {
  id: string;
  factory: string;
  name: string;
  name_local: string;
  type: string;
  source: string;
  shape: Shape;
  mode: string;
  mix: number;
  risk3: number | null;
  status: Status;
  days_rising: number;
  known_fault: string | null;
  registry: Registry | null;
  latest: Reading | null;
  history: { tick: number; ts: string; risk: number; match: boolean }[];
  band_z: number[] | null;
}
export interface Leg {
  machine: string;
  factory: string;
  xy_km: [number, number];
  arrive: string;
  leave: string;
}
export interface Route {
  date: string;
  weekday: string;
  technician: string;
  stops: string[];
  drive_km: number;
  hours_used: number;
  hours_available: number;
  not_today: string[];
  not_today_ids: string[];
  planned_at: string;
  base_xy: [number, number];
  legs: Leg[];
}
export interface Message {
  id: number;
  run_id: string;
  ts: string;
  factory: string;
  language: string;
  body: string;
  ask: string;
  reply: string | null;
  approved: number | null;
  machine: string;
  visit_day: string;
  text_en: string | null;
}
export interface LiveEvent {
  seq: number;
  kind: string;
  clock: string;
  replay_t: number | null;
  [k: string]: unknown;
}
export interface ToolCall {
  id: number;
  run_id: string;
  ts: string;
  actor: string;
  tool: string;
  input: string;
  output: string;
}
export interface Fingerprint {
  id: number;
  machine_type: string;
  fault: string;
  owner_label: string;
  factory: string;
  machine: string;
  created: number;
  embedding: number[];
}
export interface Label {
  sound: string;
  fault: string;
  source: string;
  id: number;
  text: string;
  machine: string;
  factory: string;
  via: string;
}
export interface State {
  version: number;
  tick: number;
  clock: string | null;
  tick_hours: number;
  machines: Machine[];
  fingerprints: Fingerprint[];
  pending_label: Label | null;
  messages: Message[];
  route: Route | null;
  tool_calls: ToolCall[];
  events: LiveEvent[];
  replay: { on: boolean; phase: string; t: number | null };
  link: { up: boolean; pending: number };
}
export interface Factory {
  id: string;
  name: string;
  area: string;
  area_local: string;
  xy_km: [number, number];
  owner: string;
  language: string;
  phone: string;
}
export interface EvalCond {
  auc: number;
  pauc: number;
  n_normal: number;
  n_anomalous: number;
  false_alarm_rate: number;
  detection_rate: number;
}
export interface EvalType {
  headline: string;
  conditions: Record<string, EvalCond | null>;
  per_unit: Record<string, { auc: number; pauc: number } | null>;
  registry: {
    from: string;
    to: string;
    case: string;
    false_flags: number;
    n: number;
    alarm: number;
    registry: number;
    either: number;
  }[];
  tflite: { size_bytes: number; max_rel_score_diff_vs_keras: number };
}
export interface Meta {
  factories: Factory[];
  technician: {
    name: string;
    name_local: string;
    base: string;
    xy_km: [number, number];
    hours_per_day: number;
    day_start: string;
  };
  mel_hz: number[];
  slices: number;
  frame_s: number;
  fault_machine: string;
  similar_machine: string;
  has_key: boolean;
  note: string;
  eval: {
    run_id: string;
    code_sha256: string;
    assumptions: Record<string, unknown>;
    types: Record<string, EvalType>;
  } | null;
}
export interface FrameM {
  risk: number;
  energy: number;
  spec: number[];
}

export const HISTORY = 60; // spectrogram columns kept per machine (30 s)

// ---------------------------------------------------------------- store
type Fn = () => void;
class Store {
  state: State | null = null;
  meta: Meta | null = null;
  frame: Record<string, FrameM> = {};
  frameAt = 0; // performance.now() of the last frame
  slice = 0; // play cursor within the current 10 s reading
  spec: Record<string, number[][]> = {}; // newest last
  connected = false;
  private subs = new Set<Fn>();
  private eventSubs = new Set<(e: LiveEvent) => void>();
  private lastSeq = 0;
  private ws: WebSocket | null = null;

  subscribe = (f: Fn) => {
    this.subs.add(f);
    return () => {
      this.subs.delete(f);
    };
  };
  onEvent = (f: (e: LiveEvent) => void) => {
    this.eventSubs.add(f);
    return () => {
      this.eventSubs.delete(f);
    };
  };
  private emit() {
    this.subs.forEach((f) => f());
  }

  connect() {
    if (this.ws) return;
    fetch("/api/meta")
      .then((r) => r.json())
      .then((m) => {
        this.meta = m;
        this.emit();
      });
    const open = () => {
      const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
      this.ws = ws;
      ws.onopen = () => {
        this.connected = true;
        this.emit();
      };
      ws.onclose = () => {
        this.connected = false;
        this.ws = null;
        this.emit();
        setTimeout(open, 1500);
      };
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        if (msg.type === "state") this.setState(msg.state);
        else if (msg.type === "frame") {
          this.slice = msg.slice;
          this.setFrame(msg.machines);
        }
      };
    };
    open();
  }

  private setState(s: State) {
    const first = this.state === null;
    if (s.tick < (this.state?.tick ?? -1) || s.tick < 0) this.spec = {};
    this.state = s;
    const fresh = s.events.filter((e) => e.seq > this.lastSeq);
    if (s.events.length && s.events[s.events.length - 1].seq < this.lastSeq) this.lastSeq = 0; // server reset
    this.lastSeq = Math.max(this.lastSeq, ...s.events.map((e) => e.seq), 0);
    if (!first) fresh.forEach((e) => this.eventSubs.forEach((f) => f(e)));
    this.emit();
  }

  /** The stream loops each machine's latest 10 s reading, so the last 30 s it played can be rebuilt. */
  async prefill(id: string) {
    if ((this.spec[id]?.length ?? 0) >= HISTORY) return;
    const r = await fetch(`/api/reading/${id}`);
    if (!r.ok) return;
    const reading: { spec: number[][] } = await r.json();
    const n = reading.spec.length;
    const cursor = this.slice;
    this.spec[id] = Array.from(
      { length: HISTORY },
      (_, k) => reading.spec[(((cursor - HISTORY + 1 + k) % n) + n) % n],
    );
    this.emit();
  }

  private setFrame(m: Record<string, FrameM>) {
    this.frame = m;
    this.frameAt = performance.now();
    for (const [id, f] of Object.entries(m)) {
      const h = (this.spec[id] ??= []);
      h.push(f.spec);
      if (h.length > HISTORY) h.shift();
    }
    this.emit();
  }
}

export const store = new Store();

export function useLive() {
  useEffect(() => store.connect(), []);
  return useSyncExternalStore(store.subscribe, () => store.state);
}
export function useMeta() {
  return useSyncExternalStore(store.subscribe, () => store.meta);
}
export function useFrame() {
  return useSyncExternalStore(store.subscribe, () => store.frame);
}
export function useConnected() {
  return useSyncExternalStore(store.subscribe, () => store.connected);
}

export async function post<T = unknown>(path: string, body: unknown = {}): Promise<T> {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail ?? r.statusText);
  return j as T;
}
