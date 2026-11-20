// Live recorder (phone or laptop, ?view=record): listens to a real machine and sends 10 s clips to Dhadkan.
// Clips go to /api/ingest/reading as 16 kHz mono int16 with source "live", this device's id and a sequence
// number (the server counts a resent clip once). Clips that cannot be sent wait in IndexedDB, oldest first.
import { useCallback, useEffect, useRef, useState } from "react";
import { get, post } from "../api";
import {
  CLIP_S,
  SR,
  base64,
  level,
  readingBody,
  resample,
  sendOrder,
  t,
  toInt16,
  type Lang,
  type Pending,
} from "./recorderCore";
import "./recorder.css";

interface LiveStatus {
  machine: string;
  name: string;
  live: boolean;
  quick: boolean;
  label: string | null;
  state: "learning" | "monitoring";
  progress: { clips: number; target: number };
  readings: number;
  risk: number | null;
  status_health: string | null;
  urgency: string | null;
}
interface Index {
  factories: { id: string; name: string; name_local?: string }[];
  machines: {
    id: string;
    factory: string;
    name: string;
    name_local?: string;
    type: string;
    live: LiveStatus | null;
  }[];
  quick: { clips: number; seconds: number; clip_s: number; label: string };
}
type Mode = "idle" | "learn" | "monitor";

// ---------------------------------------------------------------- device id, sequence, offline queue
function deviceId(): string {
  try {
    let d = localStorage.getItem("dhadkan.device");
    if (!d) {
      d = "rec-" + Math.random().toString(36).slice(2, 10);
      localStorage.setItem("dhadkan.device", d);
    }
    return d;
  } catch {
    return "rec-" + Math.random().toString(36).slice(2, 10);
  }
}
function nextSeq(): number {
  try {
    const n = Number(localStorage.getItem("dhadkan.seq") ?? "0") + 1;
    localStorage.setItem("dhadkan.seq", String(n));
    return n;
  } catch {
    return Date.now(); // no storage: still unique per clip on this device
  }
}

function openQueue(): Promise<IDBDatabase | null> {
  return new Promise((resolve) => {
    try {
      const r = indexedDB.open("dhadkan-recorder", 1);
      r.onupgradeneeded = () => r.result.createObjectStore("queue", { keyPath: "key" });
      r.onsuccess = () => resolve(r.result);
      r.onerror = () => resolve(null);
    } catch {
      resolve(null);
    }
  });
}
function idb<T>(
  db: IDBDatabase,
  mode: IDBTransactionMode,
  fn: (s: IDBObjectStore) => IDBRequest<T>,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const req = fn(db.transaction("queue", mode).objectStore("queue"));
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

// ---------------------------------------------------------------- the microphone
class Mic {
  ctx: AudioContext | null = null;
  stream: MediaStream | null = null;
  node: ScriptProcessorNode | null = null;
  chunks: Float32Array[] = [];
  onLevel: (bar: number) => void = () => undefined;

  async start() {
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false, channelCount: 1 },
    });
    this.ctx = new AudioContext();
    const src = this.ctx.createMediaStreamSource(this.stream);
    // ScriptProcessor is old but works on every phone browser; 4096 samples ~ 85 ms at 48 kHz
    this.node = this.ctx.createScriptProcessor(4096, 1, 1);
    this.node.onaudioprocess = (e) => {
      const x = new Float32Array(e.inputBuffer.getChannelData(0));
      this.chunks.push(x);
      this.onLevel(level(x).bar);
    };
    src.connect(this.node);
    this.node.connect(this.ctx.destination);
  }

  async clip(seconds: number, cancelled: () => boolean): Promise<Float32Array | null> {
    if (!this.ctx) return null;
    this.chunks = [];
    const rate = this.ctx.sampleRate;
    const need = Math.round(seconds * rate);
    while (this.chunks.reduce((n, c) => n + c.length, 0) < need) {
      if (cancelled()) return null;
      await new Promise((r) => setTimeout(r, 100));
    }
    const all = new Float32Array(need);
    let o = 0;
    for (const c of this.chunks) {
      all.set(c.subarray(0, Math.min(c.length, need - o)), o);
      o += c.length;
      if (o >= need) break;
    }
    return to16k(all, rate);
  }

  stop() {
    this.node?.disconnect();
    this.stream?.getTracks().forEach((tr) => tr.stop());
    void this.ctx?.close();
    this.ctx = this.stream = this.node = null;
  }
}

async function to16k(x: Float32Array, rate: number): Promise<Float32Array> {
  if (rate === SR) return x;
  try {
    const n = Math.floor((x.length * SR) / rate);
    const off = new OfflineAudioContext(1, n, SR);
    const buf = off.createBuffer(1, x.length, rate);
    buf.copyToChannel(new Float32Array(x), 0);
    const s = off.createBufferSource();
    s.buffer = buf;
    s.connect(off.destination);
    s.start();
    return (await off.startRendering()).getChannelData(0).slice();
  } catch {
    return resample(x, rate);
  }
}

/** Ends any running recording loop and closes the microphone (used by the screen's cleanup). */
function halt(run: { current: number }, mic: { current: Mic | null }) {
  run.current++;
  mic.current?.stop();
  mic.current = null;
}

async function keepScreenOn(): Promise<{ release: () => Promise<void> } | null> {
  try {
    const wl = (
      navigator as unknown as {
        wakeLock?: { request: (t: string) => Promise<{ release: () => Promise<void> }> };
      }
    ).wakeLock;
    return wl ? await wl.request("screen") : null;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------- the screen
export function RecorderScreen() {
  const [lang, setLang] = useState<Lang>(() => (navigator.language.startsWith("hi") ? "hi" : "en"));
  const [index, setIndex] = useState<Index | null>(null);
  const [factory, setFactory] = useState("A");
  const [machine, setMachine] = useState("");
  const [quick, setQuick] = useState(true);
  const [every, setEvery] = useState(10);
  const [mode, setMode] = useState<Mode>("idle");
  const [status, setStatus] = useState<LiveStatus | null>(null);
  const [bar, setBar] = useState(0);
  const [queued, setQueued] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState<{ name: string; mtype: string; hp: number } | null>(null);
  const mic = useRef<Mic | null>(null);
  const run = useRef(0); // bumps on every start/stop: an old loop sees it changed and ends
  const db = useRef<IDBDatabase | null>(null);
  const device = useRef(deviceId());
  const secure = window.isSecureContext;

  const refresh = useCallback(async () => {
    try {
      const ix = await get<Index>("/api/ingest/live");
      setIndex(ix);
      return ix;
    } catch (e) {
      setError(String((e as Error).message));
      return null;
    }
  }, []);

  useEffect(() => {
    void refresh();
    void openQueue().then((d) => {
      db.current = d;
      if (d) void idb(d, "readonly", (s) => s.count()).then(setQueued);
    });
    if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => undefined);
    const link = document.createElement("link");
    link.rel = "manifest";
    link.href = "/manifest.webmanifest";
    document.head.appendChild(link);
    return () => {
      halt(run, mic);
      link.remove();
    };
  }, [refresh]);

  const flush = useCallback(async () => {
    const d = db.current;
    if (!d) return;
    const all = sendOrder(await idb<Pending[]>(d, "readonly", (s) => s.getAll()));
    for (const p of all) {
      try {
        const out = await post<LiveStatus & { duplicate: boolean }>("/api/ingest/reading", readingBody(p));
        setStatus(out);
        await idb(d, "readwrite", (s) => s.delete(p.key));
      } catch (e) {
        if (String((e as Error).message).includes("Failed to fetch")) break; // still offline: try later
        await idb(d, "readwrite", (s) => s.delete(p.key)); // refused by the server (e.g. not started): drop it
        setError(String((e as Error).message));
      }
    }
    setQueued(await idb(d, "readonly", (s) => s.count()));
  }, []);

  useEffect(() => {
    const id = setInterval(() => void flush(), 15000);
    window.addEventListener("online", flush);
    return () => {
      clearInterval(id);
      window.removeEventListener("online", flush);
    };
  }, [flush]);

  const send = useCallback(async (x: Float32Array, mid: string) => {
    const seq = nextSeq();
    const p: Pending = {
      key: `${device.current}:${seq}`,
      machine: mid,
      device: device.current,
      seq,
      ts: Date.now() / 1000 - x.length / SR, // when the clip started
      samples_b64: base64(toInt16(x)),
    };
    try {
      const out = await post<LiveStatus>("/api/ingest/reading", readingBody(p));
      setStatus(out);
      setError(null);
      return out;
    } catch (e) {
      const msg = String((e as Error).message);
      if (msg.includes("Failed to fetch") && db.current) {
        await idb(db.current, "readwrite", (s) => s.put(p)); // offline: keep it, send later
        setQueued((n) => n + 1);
      } else setError(msg);
      return null;
    }
  }, []);

  const stop = useCallback(() => {
    run.current++;
    mic.current?.stop();
    mic.current = null;
    setMode("idle");
    setBar(0);
  }, []);

  const start = useCallback(
    async (m: Mode) => {
      if (!machine) return;
      setError(null);
      const my = ++run.current;
      const cancelled = () => run.current !== my;
      try {
        const st = await post<LiveStatus>("/api/ingest/live/start", {
          machine,
          device: device.current,
          quick,
        });
        if (m === "learn" || !st.live) setStatus(st);
      } catch (e) {
        setError(String((e as Error).message));
        return;
      }
      try {
        const mc = new Mic();
        mc.onLevel = setBar;
        await mc.start();
        mic.current = mc;
      } catch (e) {
        const name = (e as DOMException).name;
        setError(name === "NotAllowedError" || name === "SecurityError" ? t("micDenied", lang) : String(e));
        return;
      }
      setMode(m);
      const wakeLock = await keepScreenOn();
      while (!cancelled()) {
        const x = await mic.current?.clip(CLIP_S, cancelled);
        if (!x || cancelled()) break;
        const out = await send(x, machine);
        if (m === "learn" && out && out.state === "monitoring") break; // learned: switch to Monitor yourself
        if (m === "monitor" && every > CLIP_S) {
          const until = Date.now() + (every - CLIP_S) * 1000;
          while (!cancelled() && Date.now() < until) await new Promise((r) => setTimeout(r, 200));
        }
      }
      void wakeLock?.release().catch(() => undefined);
      if (!cancelled()) stop();
    },
    [machine, quick, every, lang, send, stop],
  );

  const mark = async (label: "fault_on" | "fault_off") => {
    try {
      await post("/api/ingest/live/mark", { machine, label });
    } catch (e) {
      setError(String((e as Error).message));
    }
  };

  const addMachine = async () => {
    if (!form?.name.trim()) return;
    try {
      const spec = await post<{ id: string }>("/api/ingest/live/machine", { factory, ...form });
      await refresh();
      setMachine(spec.id);
      setForm(null);
    } catch (e) {
      setError(String((e as Error).message));
    }
  };

  const machines = (index?.machines ?? []).filter((m) => m.factory === factory);
  const learning = status?.state === "learning";
  const risk = status?.risk ?? null;
  const pct = status ? Math.round((100 * status.progress.clips) / Math.max(1, status.progress.target)) : 0;

  return (
    <main className="rec" lang={lang}>
      <header className="rec-top">
        <b>{t("title", lang)}</b>
        <span className="rec-live">{t("live", lang)}</span>
        <button className="rec-lang" onClick={() => setLang(lang === "en" ? "hi" : "en")}>
          {lang === "en" ? "हिंदी" : "English"}
        </button>
      </header>

      {!secure && <p className="rec-error">{t("needHttps", lang)}</p>}
      {error && <p className="rec-error">{error}</p>}

      <section className="rec-card">
        <label>
          {t("factory", lang)}
          <select
            value={factory}
            disabled={mode !== "idle"}
            onChange={(e) => (setFactory(e.target.value), setMachine(""))}
          >
            {(index?.factories ?? []).map((f) => (
              <option key={f.id} value={f.id}>
                {lang === "hi" && f.name_local ? f.name_local : f.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t("machine", lang)}
          <select value={machine} disabled={mode !== "idle"} onChange={(e) => setMachine(e.target.value)}>
            <option value="">—</option>
            {machines.map((m) => (
              <option key={m.id} value={m.id}>
                {(lang === "hi" && m.name_local ? m.name_local : m.name) + ` (${m.type})`}
                {m.live ? " · LIVE" : ""}
              </option>
            ))}
          </select>
        </label>
        {mode === "idle" && !form && (
          <button className="rec-link" onClick={() => setForm({ name: "", mtype: "fan", hp: 0.1 })}>
            + {t("newMachine", lang)}
          </button>
        )}
        {form && (
          <div className="rec-form">
            <input
              placeholder={t("name", lang)}
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
            />
            <select value={form.mtype} onChange={(e) => setForm({ ...form, mtype: e.target.value })}>
              {["fan", "pump", "gearbox", "valve", "slider"].map((x) => (
                <option key={x}>{x}</option>
              ))}
            </select>
            <input
              type="number"
              min={0.05}
              step={0.05}
              aria-label={t("hp", lang)}
              value={form.hp}
              onChange={(e) => setForm({ ...form, hp: Number(e.target.value) })}
            />
            <button onClick={addMachine}>{t("add", lang)}</button>
          </div>
        )}
        <label className="rec-check">
          <input
            type="checkbox"
            checked={quick}
            disabled={mode !== "idle"}
            onChange={(e) => setQuick(e.target.checked)}
          />
          {t("quick", lang)}
        </label>
        <label>
          {t("every", lang)}{" "}
          <select value={every} onChange={(e) => setEvery(Number(e.target.value))} disabled={mode !== "idle"}>
            {[10, 20, 30, 60].map((s) => (
              <option key={s} value={s}>
                {s} {t("seconds", lang)}
              </option>
            ))}
          </select>
        </label>
      </section>

      <div className="rec-buttons">
        {mode === "idle" ? (
          <>
            <button className="rec-big learn" disabled={!machine} onClick={() => void start("learn")}>
              {t("learn", lang)}
            </button>
            <button className="rec-big monitor" disabled={!machine} onClick={() => void start("monitor")}>
              {t("monitor", lang)}
            </button>
          </>
        ) : (
          <button className="rec-big stop" onClick={stop}>
            ■ {t("stop", lang)}
          </button>
        )}
      </div>

      <section className="rec-card">
        <div className="rec-meter" aria-label={t("mic", lang)}>
          <div style={{ width: `${Math.round(bar * 100)}%` }} />
        </div>
        <p className="rec-hint">{t("hold", lang)}</p>
        {status && (
          <>
            <p className="rec-state">
              {learning ? t("learning", lang) : t("monitoring", lang)}
              {status.label && <span className="rec-demo"> · {status.label}</span>}
            </p>
            {learning ? (
              <>
                <div className="rec-progress">
                  <div style={{ width: `${pct}%` }} />
                </div>
                <p className="rec-count">
                  {status.progress.clips}/{status.progress.target} {t("clips", lang)}
                </p>
              </>
            ) : (
              <p className={`rec-risk ${risk !== null && risk >= 1 ? "high" : ""}`}>
                {risk === null ? "—" : risk.toFixed(2)}
                <small>{t("risk", lang)}</small>
              </p>
            )}
            {status.urgency && !learning && (
              <p className="rec-urgency">{status.urgency.replaceAll("_", " ")}</p>
            )}
          </>
        )}
        {queued > 0 && (
          <p className="rec-queued">
            {queued} {t("queued", lang)}
          </p>
        )}
      </section>

      {status && !learning && (
        <div className="rec-marks">
          <button onClick={() => void mark("fault_on")}>{t("faultOn", lang)}</button>
          <button onClick={() => void mark("fault_off")}>{t("faultOff", lang)}</button>
        </div>
      )}
      <p className="rec-foot">
        {device.current} · 16 kHz · {CLIP_S} s
      </p>
    </main>
  );
}
