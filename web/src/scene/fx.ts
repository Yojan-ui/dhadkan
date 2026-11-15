// Shared animation state that is not React state (read every frame).
export const pulses = new Map<string, number>();     // machine id -> clock time the pulse started (s)

export function pulse(id: string, now = performance.now() / 1000) {
  pulses.set(id, now);
}

export const ease = (t: number) => (t <= 0 ? 0 : t >= 1 ? 1 : 1 - Math.pow(1 - t, 3));
