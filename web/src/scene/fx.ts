import { useRef } from "react";

// Shared animation state that is not React state (read every frame).
export const pulses = new Map<string, number>(); // machine id -> clock time the pulse started (s)

export function pulse(id: string, now = performance.now() / 1000) {
  pulses.set(id, now);
}

/** Memo keyed by a string: recompute only when the key changes (not on every new array). */
export function useKeyed<T>(key: string, make: () => T): T {
  const r = useRef<{ k: string; v: T } | null>(null);
  if (r.current === null || r.current.k !== key) r.current = { k: key, v: make() };
  return r.current.v;
}

export const ease = (t: number) => (t <= 0 ? 0 : t >= 1 ? 1 : 1 - Math.pow(1 - t, 3));
