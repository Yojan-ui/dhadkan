// Colours, and the few rules that turn a reading into words a mechanic would use.
import { useEffect, useState } from "react";
import type { Factory, Machine } from "./api";

export const C = {
  bg: "#000000",
  ground: "#1c1d20",
  slab: "#25272b",
  wall: "#2f3237",
  partA: "#4b4f56",
  partB: "#5d626a",
  partC: "#3b3e44",
  ink: "#ffffff",
  muted: "#8a8a8a",
  rule: "#1a1a1a",
  track: "#3d4046",
  grey: "#7d8189",
  amber: "#FE5101",
  accent: "#FE5101",
};

const hex = (h: string) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16) / 255);
const mix = (a: number[], b: number[], t: number) => a.map((v, i) => v + (b[i] - v) * t);

/** How close a machine is to its own alarm level, 0..1 (the ring's fill). */
export function fill(m: Machine) {
  const r = m.risk3 ?? m.latest?.risk ?? 0;
  return Math.max(0, Math.min(1, r));
}

/** Ring colour: grey while healthy, amber near alarm, signal orange at alarm or on a registry match. */
export function ringRGB(m: Machine): [number, number, number] {
  if (m.status === "alarm" || m.status === "early_match") return hex(C.accent) as [number, number, number];
  const r = m.risk3 ?? 0;
  // healthy machines sit around 0.6-0.85 of their alarm level (it is their 95th percentile)
  const t = Math.max(0, Math.min(1, (r - 0.85) / 0.15));
  return mix(hex(C.grey), hex(C.amber), t) as [number, number, number];
}
export function ringCSS(m: Machine) {
  const [r, g, b] = ringRGB(m);
  return `rgb(${Math.round(r * 255)},${Math.round(g * 255)},${Math.round(b * 255)})`;
}

const FAULT_EN: Record<string, string> = {
  bearing: "bearing",
  imbalance: "imbalance",
  loose_belt: "loose belt",
  clog: "clogging",
};

/** One line, the way the mechanic would say it. */
export function mechanicLine(m: Machine, factories: Factory[]) {
  if (!m.latest) return "No reading yet.";
  const pct = Math.round((m.risk3 ?? 0) * 100);
  const rising = m.days_rising > 1 ? ` Getting worse for ${m.days_rising} days.` : "";
  const fault = m.known_fault ? (FAULT_EN[m.known_fault] ?? m.known_fault) : null;
  switch (m.status) {
    case "alarm":
      return `Past its alarm level (${pct}%). ${fault ? `Sounds like ${fault} trouble. ` : ""}Get it checked.${rising}`;
    case "early_match": {
      const area =
        m.registry?.from_area ?? factories.find((f) => f.id !== m.factory)?.area ?? "another factory";
      return `No alarm yet (${pct}%), but it sounds like the ${fault ?? "fault"} confirmed in ${area}. Have it looked at.`;
    }
    case "watch":
      return `Close to its alarm level (${pct}%). Keep an ear on it.${rising}`;
    default:
      return pct < 85
        ? `Sounds like its normal self (${pct}% of alarm).`
        : `A little off its usual sound (${pct}% of alarm). Nothing to do yet.${rising}`;
  }
}

/** Risk (score / own alarm level) as "81%". */
export function pct(r: number | null | undefined) {
  return r == null ? "--" : `${Math.round(r * 100)}%`;
}

export const STATUS_WORD: Record<string, string> = {
  ok: "ok",
  watch: "watch",
  alarm: "alarm",
  early_match: "early match",
  no_data: "no data",
};

export const SHAPE_WORD: Record<string, string> = {
  pump: "pump",
  motor: "motor + blower",
  loom: "loom",
  mill: "flour mill",
};

export function sourceWord(m: Machine) {
  const [t, u] = m.source.split("/");
  if (t === "gearbox")
    return `MIMII DUE gearbox ${u.replace("section_", "section ")} (stands in for the ${SHAPE_WORD[m.shape]}'s gear drive)`;
  return `MIMII ${t} ${u.replace("_", " ")}`;
}

export function fmtClock(iso: string | null | undefined, withDay = true) {
  if (!iso) return "--";
  const d = new Date(iso);
  const day = d.toLocaleDateString("en-GB", { weekday: "short", day: "2-digit", month: "short" });
  const t = d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", hour12: false });
  return withDay ? `${day} ${t}` : t;
}

export function fmtT(s: number | null | undefined) {
  if (s == null) return "0:00";
  const m = Math.floor(s / 60);
  return `${m}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
}

/** Rupees with Indian digit grouping: 93310 -> "₹93,310". */
export function inr(v: number) {
  return `₹${Math.round(v).toLocaleString("en-IN")}`;
}

export function useMedia(q: string) {
  const [v, setV] = useState(() => window.matchMedia(q).matches);
  useEffect(() => {
    const mq = window.matchMedia(q);
    const f = () => setV(mq.matches);
    mq.addEventListener("change", f);
    return () => mq.removeEventListener("change", f);
  }, [q]);
  return v;
}
export const useReducedMotion = () => useMedia("(prefers-reduced-motion: reduce)");
export const usePhone = () => useMedia("(max-width: 767px)");

/** The digital twin's visual state, from the backend's status and operating state only. */
export type Twin = "normal" | "watch" | "alert" | "offline" | "learning";
export const TWIN_HEX: Record<Twin, string> = {
  normal: "#3E9E6A", // subtle green
  watch: "#E3A23B", // amber
  alert: "#FE5101", // signal orange: needs attention
  offline: "#7D8189", // grey
  learning: "#5E7A96", // blue-grey: still learning its normal
};
export function twinState(m: Machine): Twin {
  const op = m.operating;
  if (op && (op.running === false || op.sensor === "lost")) return "offline";
  if (m.status === "no_data" || !m.latest) return "offline";
  if (m.status === "alarm" || m.status === "early_match") return "alert";
  if (m.status === "watch") return "watch";
  if (m.status === "learning") return "learning";
  return "normal";
}
/** Is it turning? Only a confirmed stop stops the animation; an unknown state keeps the last known one. */
export function isRunning(m: Machine): boolean {
  return m.operating?.running !== false;
}
/** Short words for the twin's labels (never a health %: risk is shown against its own alarm level). */
export function twinWords(m: Machine): string {
  const op = m.operating;
  if (op?.running === false) return "stopped";
  if (op?.sensor === "lost") return `no signal · last ${fmtClock(op.last_ts, false)}`;
  if (m.status === "no_data" || !m.latest) return "no reading yet";
  if (m.status === "learning")
    return m.learning ? `learning · day ${m.learning.day}/${m.learning.of}` : "learning";
  const r = m.risk3 ?? m.latest.risk;
  const x = `${r.toFixed(2)}× alarm`;
  if (m.status === "alarm") return `alert · ${x}`;
  if (m.status === "early_match") return "matches a known fault";
  if (m.status === "watch") return `watch · ${x}`;
  return `normal · ${x}`;
}
export function twinRGB(m: Machine): [number, number, number] {
  return hex(TWIN_HEX[twinState(m)]) as [number, number, number];
}
