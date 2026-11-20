// Pure parts of the live recorder (no browser APIs): tested in web/tests/recorder.test.ts.

export const SR = 16000; // the server's model rate (edge/config.SR)
export const CLIP_S = 10;

/** Float samples (-1..1) to little-endian int16 bytes, as /api/ingest/reading expects. */
export function toInt16(x: Float32Array): Uint8Array {
  const out = new Uint8Array(x.length * 2);
  const v = new DataView(out.buffer);
  for (let i = 0; i < x.length; i++) {
    const s = Math.max(-1, Math.min(1, x[i]));
    v.setInt16(i * 2, Math.round(s * 32767), true);
  }
  return out;
}

const B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
/** Base64 without btoa (works the same in the browser, a worker and node). */
export function base64(bytes: Uint8Array): string {
  let s = "";
  let i = 0;
  for (; i + 2 < bytes.length; i += 3) {
    const n = (bytes[i] << 16) | (bytes[i + 1] << 8) | bytes[i + 2];
    s += B64[(n >> 18) & 63] + B64[(n >> 12) & 63] + B64[(n >> 6) & 63] + B64[n & 63];
  }
  const rest = bytes.length - i;
  if (rest === 1) {
    const n = bytes[i] << 16;
    s += B64[(n >> 18) & 63] + B64[(n >> 12) & 63] + "==";
  } else if (rest === 2) {
    const n = (bytes[i] << 16) | (bytes[i + 1] << 8);
    s += B64[(n >> 18) & 63] + B64[(n >> 12) & 63] + B64[(n >> 6) & 63] + "=";
  }
  return s;
}

/** Resample to 16 kHz: box low-pass then linear interpolation (the browser path uses OfflineAudioContext
 *  when it has one; this is the fallback and what the tests check). */
export function resample(x: Float32Array, from: number, to = SR): Float32Array {
  if (from === to) return x;
  const ratio = from / to;
  const k = Math.max(1, Math.floor(ratio));
  let y = x;
  if (k > 1) {
    y = new Float32Array(x.length);
    let acc = 0;
    for (let i = 0; i < x.length; i++) {
      acc += x[i] - (i >= k ? x[i - k] : 0);
      y[i] = acc / Math.min(i + 1, k);
    }
  }
  const n = Math.floor(x.length / ratio);
  const out = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    const t = i * ratio;
    const a = Math.floor(t);
    const b = Math.min(a + 1, y.length - 1);
    out[i] = y[a] + (y[b] - y[a]) * (t - a);
  }
  return out;
}

/** Loudness for the level meter: dBFS of the RMS, and 0..1 for the bar (-60 dB .. 0 dB). */
export function level(x: Float32Array): { db: number; bar: number } {
  let s = 0;
  for (let i = 0; i < x.length; i++) s += x[i] * x[i];
  const db = 10 * Math.log10(s / Math.max(1, x.length) + 1e-12);
  return { db, bar: Math.max(0, Math.min(1, (db + 60) / 60)) };
}

export interface Pending {
  key: string; // device:seq, the server's idempotency key
  machine: string;
  device: string;
  seq: number;
  ts: number;
  samples_b64: string;
}

export function readingBody(p: Pending) {
  return {
    device: p.device,
    seq: p.seq,
    machine: p.machine,
    kind: "audio",
    sr: SR,
    samples_b64: p.samples_b64,
    source: "live",
    ts: p.ts,
  };
}

/** Oldest first, so the server sees the machine's clips in the order they were recorded. */
export function sendOrder(q: Pending[]): Pending[] {
  return [...q].sort((a, b) => a.ts - b.ts || a.seq - b.seq);
}

export type Lang = "en" | "hi";
export const T = {
  title: { en: "Live recorder", hi: "लाइव रिकॉर्डर" },
  live: { en: "LIVE, not simulated", hi: "लाइव, सिमुलेशन नहीं" },
  factory: { en: "Factory", hi: "फ़ैक्ट्री" },
  machine: { en: "Machine", hi: "मशीन" },
  newMachine: { en: "Register a new machine", hi: "नई मशीन जोड़ें" },
  name: { en: "Name", hi: "नाम" },
  type: { en: "Type", hi: "प्रकार" },
  hp: { en: "Motor HP", hi: "मोटर HP" },
  add: { en: "Add machine", hi: "मशीन जोड़ें" },
  learn: { en: "Learn normal", hi: "सामान्य आवाज़ सीखें" },
  monitor: { en: "Monitor", hi: "निगरानी" },
  stop: { en: "Stop", hi: "रोकें" },
  quick: { en: "Quick learn (about 2 minutes, demo)", hi: "जल्दी सीखें (लगभग 2 मिनट, डेमो)" },
  every: { en: "A clip every", hi: "हर" },
  seconds: { en: "s", hi: "सेकंड" },
  learning: { en: "Learning its normal", hi: "सामान्य आवाज़ सीख रहे हैं" },
  monitoring: { en: "Monitoring", hi: "निगरानी चालू" },
  clips: { en: "clips", hi: "क्लिप" },
  risk: { en: "Score (1.0 = its alarm level)", hi: "स्कोर (1.0 = अलार्म स्तर)" },
  queued: { en: "waiting to send (offline)", hi: "भेजने का इंतज़ार (ऑफ़लाइन)" },
  mic: { en: "Microphone level", hi: "माइक की आवाज़" },
  micDenied: {
    en: "Microphone blocked. Allow the microphone for this site in the browser settings, then reload.",
    hi: "माइक बंद है। ब्राउज़र सेटिंग में इस साइट के लिए माइक चालू करें, फिर पेज दोबारा खोलें।",
  },
  needHttps: {
    en: "The microphone needs HTTPS on a phone. Open the link from .\\dev.ps1 tunnel, or use http://localhost on the laptop.",
    hi: "फ़ोन पर माइक के लिए HTTPS चाहिए। .\\dev.ps1 tunnel वाला लिंक खोलें, या लैपटॉप पर http://localhost इस्तेमाल करें।",
  },
  faultOn: { en: "Mark: fault added", hi: "निशान: खराबी डाली" },
  faultOff: { en: "Mark: fault removed", hi: "निशान: खराबी हटाई" },
  hold: {
    en: "Hold the phone near the machine, same place each time.",
    hi: "फ़ोन मशीन के पास रखें, हर बार उसी जगह।",
  },
} as const;
export const t = (k: keyof typeof T, lang: Lang) => T[k][lang];
