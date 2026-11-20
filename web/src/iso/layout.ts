// Where everything sits on the isometric floor (world units, y up). Three bays, one per factory.
import type { Machine } from "../api";

export const FLOOR = { w: 40, d: 26, h: 0.6 };
export const BAY_X: Record<string, number> = { A: -13, B: 0, C: 13 };
const SLOTS: [number, number][] = [
  [-2.6, -3.2],
  [2.6, -3.2],
  [-2.6, 3.4],
  [2.6, 3.4],
  [0, 0.1], // a fifth machine (registered from the live recorder) stands in the middle of the bay
];

export function machinePositions(machines: Machine[]): Record<string, [number, number, number]> {
  const out: Record<string, [number, number, number]> = {};
  const byFactory: Record<string, Machine[]> = {};
  for (const m of machines) (byFactory[m.factory] ??= []).push(m);
  for (const [f, list] of Object.entries(byFactory)) {
    const bx = BAY_X[f] ?? 0;
    list.forEach((m, i) => {
      const [dx, dz] = SLOTS[i % SLOTS.length];
      out[m.id] = [bx + dx, FLOOR.h / 2, dz + Math.floor(i / SLOTS.length) * 7];
    });
  }
  return out;
}

// overhead rail: a loop around all bays and a cross through the middle
export const RAIL_Y = 4.2;
export const RAIL_LOOP: [number, number][] = [
  [-18, -9.5],
  [18, -9.5],
  [18, 9.5],
  [-18, 9.5],
];
export const RAIL_CROSS: [number, number][][] = [
  [
    [-18, 0.2],
    [18, 0.2],
  ],
  [
    [-6.5, -9.5],
    [-6.5, 9.5],
  ],
  [
    [6.5, -9.5],
    [6.5, 9.5],
  ],
];
export const LANES_X = [-6.5, 6.5]; // yellow aisle lines between bays
export const CABINS: { x: number; z: number; w: number; d: number; h: number }[] = [
  { x: -16.5, z: -11.2, w: 4.4, d: 2.4, h: 2.2 },
  { x: 16.2, z: 11.2, w: 5, d: 2.4, h: 2.4 },
];
export const ARMS: [number, number][] = [
  [-8.5, -6.2],
  [8.5, 6.4],
  [-4.2, 8.2],
  [4.6, -8],
];
