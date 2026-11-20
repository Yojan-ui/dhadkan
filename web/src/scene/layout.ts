// A small industrial estate: the sheds stand in a row on one ground plane, facing a shared road.
// The map is schematic (not to scale); travel times come from the real km in sim/cluster.json.
import * as THREE from "three";
import type { Factory, Machine, Shape } from "../api";

export const SHED = { w: 5.6, d: 4.6, wall: 0.45, roof: 2.5 };
const GAP = 1.6;
export const ROAD = { z: SHED.d / 2 + 1.45, w: 2.0 };

export function shedCenter(i: number, n: number) {
  return new THREE.Vector3((i - (n - 1) / 2) * (SHED.w + GAP), 0, 0);
}
export const roadPoint = (x: number, lane = 0) => new THREE.Vector3(x, 0.05, ROAD.z + lane);
export function basePoint(n: number) {
  return roadPoint(-((n - 1) / 2) * (SHED.w + GAP) - SHED.w / 2 - 0.9);
}
export function estateExtent(n: number) {
  const half = ((n - 1) / 2) * (SHED.w + GAP) + SHED.w / 2;
  return { minX: -half - 1.4, maxX: half + 0.6, minZ: -SHED.d / 2 - 0.3, maxZ: ROAD.z + ROAD.w / 2 + 0.2 };
}

const SLOTS: [number, number][] = [
  [-1.35, -0.95],
  [1.35, -0.95],
  [-1.35, 1.05],
  [1.35, 1.05],
  [0, 0.05], // a fifth machine (registered from the live recorder)
];

export function machinePositions(factories: Factory[], machines: Machine[]) {
  const pos: Record<string, THREE.Vector3> = {};
  factories.forEach((f, fi) => {
    const c = shedCenter(fi, factories.length);
    machines
      .filter((m) => m.factory === f.id)
      .forEach((m, i) => {
        const [x, z] = SLOTS[i % SLOTS.length];
        pos[m.id] = new THREE.Vector3(c.x + x, 0.12, c.z + z);
      });
  });
  return pos;
}

export type Geo = "box" | "cyl" | "cone";
export type Paint = "body" | "accent" | "iron" | "steel" | "cream" | "dark";
export interface Part {
  g: Geo;
  p: [number, number, number];
  s: [number, number, number];
  r?: [number, number, number];
  c: Paint;
}
/** A pulley, disc or wheel that turns. `ratio` = its speed relative to the motor's. */
export interface Spinner {
  p: [number, number, number];
  axis: "x" | "y" | "z";
  radius: number;
  width: number;
  ratio: number;
  /** fan or impeller: a small hub and this many blades (a pulley has one spoke and a full disc) */
  blades?: number;
}
/** A belt between two spinners of the same shape (indices into its spinner list). */
export type Belt = [number, number];

export const ACCENT: Record<Shape, string> = {
  pump: "#3F7A57", // pump green
  motor: "#3B6A8C", // motor/blower blue
  loom: "#6A7B8B", // loom blue-grey
  mill: "#E4D8BC", // mill cream
};
export const PAINT: Record<Exclude<Paint, "accent">, string> = {
  body: "#C9C4BC",
  iron: "#58544E",
  steel: "#9EA3A9",
  cream: "#EDE3CC",
  dark: "#34312E",
};

const H = Math.PI / 2;
// Each shape fits in about 2.2 x 1.0 m, origin on the floor at its centre.
export const SHAPES: Record<Shape, { parts: Part[]; spin: Spinner[]; belts: Belt[] }> = {
  pump: {
    parts: [
      { g: "box", p: [0, 0.06, 0], s: [1.45, 0.12, 0.62], c: "iron" },
      { g: "box", p: [-0.32, 0.15, 0], s: [0.5, 0.08, 0.42], c: "iron" },
      { g: "cyl", p: [-0.32, 0.36, 0], s: [0.44, 0.62, 0.44], r: [0, 0, H], c: "body" }, // motor
      { g: "cyl", p: [-0.46, 0.36, 0], s: [0.47, 0.06, 0.47], r: [0, 0, H], c: "dark" },
      { g: "cyl", p: [-0.18, 0.36, 0], s: [0.47, 0.06, 0.47], r: [0, 0, H], c: "dark" },
      { g: "cyl", p: [-0.68, 0.36, 0], s: [0.4, 0.12, 0.4], r: [0, 0, H], c: "iron" }, // fan cowl
      { g: "box", p: [-0.3, 0.62, 0], s: [0.16, 0.1, 0.14], c: "body" }, // terminal box
      { g: "cyl", p: [0.36, 0.38, 0], s: [0.56, 0.3, 0.56], r: [H, 0, 0], c: "accent" }, // volute
      { g: "box", p: [0.36, 0.14, 0], s: [0.3, 0.12, 0.34], c: "iron" },
      { g: "cyl", p: [0.86, 0.38, 0], s: [0.17, 0.7, 0.17], r: [0, 0, H], c: "iron" }, // inlet pipe
      { g: "cyl", p: [0.62, 0.38, 0], s: [0.27, 0.04, 0.27], r: [0, 0, H], c: "iron" },
      { g: "cyl", p: [1.19, 0.38, 0], s: [0.27, 0.04, 0.27], r: [0, 0, H], c: "iron" },
      { g: "cyl", p: [0.36, 0.8, 0], s: [0.15, 0.5, 0.15], c: "accent" }, // outlet pipe
      { g: "cyl", p: [0.36, 0.6, 0], s: [0.25, 0.04, 0.25], c: "iron" },
      { g: "cyl", p: [0.36, 1.05, 0], s: [0.25, 0.04, 0.25], c: "iron" },
      { g: "cyl", p: [0.36, 0.9, 0.09], s: [0.09, 0.03, 0.09], r: [H, 0, 0], c: "cream" }, // gauge
    ],
    spin: [
      { p: [0.06, 0.36, 0], axis: "x", radius: 0.13, width: 0.06, ratio: 1 }, // coupling
      { p: [-0.755, 0.36, 0], axis: "x", radius: 0.17, width: 0.02, ratio: 1, blades: 7 }, // motor cooling fan
      { p: [0.36, 0.38, 0.16], axis: "z", radius: 0.2, width: 0.025, ratio: 1, blades: 5 }, // impeller (shown)
    ],
    belts: [],
  },
  motor: {
    parts: [
      { g: "box", p: [0, 0.06, 0], s: [1.4, 0.12, 0.72], c: "iron" },
      { g: "box", p: [-0.38, 0.14, -0.02], s: [0.42, 0.08, 0.46], c: "iron" },
      { g: "cyl", p: [-0.38, 0.32, -0.02], s: [0.4, 0.56, 0.4], r: [H, 0, 0], c: "body" }, // motor
      { g: "cyl", p: [-0.38, 0.32, -0.14], s: [0.43, 0.05, 0.43], r: [H, 0, 0], c: "dark" },
      { g: "cyl", p: [-0.38, 0.32, 0.1], s: [0.43, 0.05, 0.43], r: [H, 0, 0], c: "dark" },
      { g: "box", p: [-0.38, 0.56, 0], s: [0.14, 0.1, 0.16], c: "body" },
      { g: "cyl", p: [-0.38, 0.32, 0.3], s: [0.05, 0.12, 0.05], r: [H, 0, 0], c: "steel" }, // shaft
      { g: "box", p: [0.3, 0.28, -0.05], s: [0.5, 0.32, 0.5], c: "iron" }, // pedestal
      { g: "cyl", p: [0.3, 0.7, -0.05], s: [0.86, 0.34, 0.86], r: [H, 0, 0], c: "accent" }, // blower housing
      { g: "cyl", p: [0.3, 0.7, -0.24], s: [0.36, 0.06, 0.36], r: [H, 0, 0], c: "iron" }, // inlet ring
      { g: "box", p: [0.3, 1.2, -0.05], s: [0.32, 0.28, 0.3], c: "accent" }, // outlet duct
      { g: "box", p: [0.3, 1.36, -0.05], s: [0.38, 0.04, 0.36], c: "iron" },
      { g: "cyl", p: [0.3, 0.7, 0.2], s: [0.05, 0.22, 0.05], r: [H, 0, 0], c: "steel" },
    ],
    spin: [
      { p: [-0.38, 0.32, 0.34], axis: "z", radius: 0.09, width: 0.07, ratio: 1 },
      { p: [0.3, 0.7, 0.34], axis: "z", radius: 0.18, width: 0.07, ratio: 0.09 / 0.18 },
      { p: [0.3, 0.7, -0.255], axis: "z", radius: 0.16, width: 0.02, ratio: 0.09 / 0.18, blades: 8 }, // blower wheel
      { p: [-0.38, 0.32, -0.315], axis: "z", radius: 0.17, width: 0.02, ratio: 1, blades: 7 }, // motor fan
    ],
    belts: [[0, 1]],
  },
  loom: {
    parts: [
      { g: "box", p: [-0.66, 0.48, 0], s: [0.08, 0.92, 0.86], c: "accent" }, // side frames
      { g: "box", p: [0.66, 0.48, 0], s: [0.08, 0.92, 0.86], c: "accent" },
      { g: "box", p: [0, 0.04, -0.38], s: [1.4, 0.08, 0.08], c: "iron" },
      { g: "box", p: [0, 0.04, 0.38], s: [1.4, 0.08, 0.08], c: "iron" },
      { g: "box", p: [0, 0.96, -0.36], s: [1.42, 0.08, 0.1], c: "accent" }, // top beam
      { g: "cyl", p: [0, 0.32, -0.3], s: [0.3, 1.24, 0.3], r: [0, 0, H], c: "body" }, // warp beam
      { g: "box", p: [0, 0.6, -0.1], s: [1.16, 0.015, 0.5], r: [0.45, 0, 0], c: "cream" }, // warp sheet
      { g: "box", p: [0, 0.74, 0.12], s: [1.24, 0.14, 0.05], c: "iron" }, // reed
      { g: "box", p: [0, 0.8, 0.34], s: [1.3, 0.06, 0.1], c: "body" }, // breast beam
      { g: "cyl", p: [0, 0.42, 0.32], s: [0.2, 1.22, 0.2], r: [0, 0, H], c: "body" }, // cloth roller
      { g: "cyl", p: [0, 0.42, 0.32], s: [0.25, 1.0, 0.25], r: [0, 0, H], c: "cream" }, // cloth
      { g: "cyl", p: [0, 0.36, 0.06], s: [0.06, 1.38, 0.06], r: [0, 0, H], c: "steel" }, // crank shaft
      { g: "cyl", p: [-1.0, 0.2, 0.36], s: [0.28, 0.38, 0.28], r: [0, 0, H], c: "body" }, // drive motor
      { g: "box", p: [-1.0, 0.06, 0.36], s: [0.34, 0.1, 0.3], c: "iron" },
    ],
    spin: [
      { p: [-0.79, 0.2, 0.36], axis: "x", radius: 0.07, width: 0.06, ratio: 1 },
      { p: [-0.79, 0.36, 0.06], axis: "x", radius: 0.22, width: 0.06, ratio: 0.07 / 0.22 }, // flywheel
      { p: [0.74, 0.36, 0.06], axis: "x", radius: 0.14, width: 0.04, ratio: 0.07 / 0.22 }, // gear wheel
      { p: [-1.2, 0.2, 0.36], axis: "x", radius: 0.12, width: 0.02, ratio: 1, blades: 6 }, // motor fan
    ],
    belts: [[0, 1]],
  },
  mill: {
    parts: [
      { g: "box", p: [0, 0.26, 0], s: [0.72, 0.52, 0.72], c: "iron" }, // stand
      { g: "cyl", p: [0, 0.66, 0], s: [0.84, 0.28, 0.84], c: "accent" }, // stone casing
      { g: "cyl", p: [0, 0.82, 0], s: [0.62, 0.05, 0.62], c: "accent" },
      { g: "cone", p: [0, 1.06, 0], s: [0.6, 0.42, 0.6], c: "body" }, // hopper
      { g: "cyl", p: [0, 1.28, 0], s: [0.62, 0.03, 0.62], c: "iron" },
      { g: "box", p: [0, 0.5, 0.42], s: [0.16, 0.1, 0.18], r: [-0.3, 0, 0], c: "iron" }, // spout
      { g: "box", p: [0, 0.2, 0.56], s: [0.28, 0.34, 0.18], c: "cream" }, // flour bag
      { g: "cyl", p: [0.82, 0.2, -0.05], s: [0.3, 0.42, 0.3], r: [0, 0, H], c: "body" }, // motor
      { g: "box", p: [0.82, 0.06, -0.05], s: [0.4, 0.1, 0.34], c: "iron" },
    ],
    spin: [
      { p: [0.46, 0.2, -0.05], axis: "x", radius: 0.07, width: 0.07, ratio: 1 },
      { p: [0.44, 0.44, 0.02], axis: "x", radius: 0.2, width: 0.07, ratio: 0.07 / 0.2 },
      { p: [1.04, 0.2, -0.05], axis: "x", radius: 0.13, width: 0.02, ratio: 1, blades: 6 }, // motor fan
      { p: [0, 0.86, 0], axis: "y", radius: 0.27, width: 0.025, ratio: 0.07 / 0.2, blades: 3 }, // stone feeder arms
    ],
    belts: [[0, 1]],
  },
};

/** Height above the floor for ripples, rings' lights and floating labels. */
export const TOP: Record<Shape, number> = { pump: 1.1, motor: 1.4, loom: 1.04, mill: 1.32 };

const _q = new THREE.Quaternion();
const _e = new THREE.Euler();
export function partMatrix(
  base: THREE.Vector3,
  part: Part,
  offset: THREE.Vector3,
  out = new THREE.Matrix4(),
) {
  _q.setFromEuler(_e.set(...(part.r ?? [0, 0, 0])));
  return out.compose(
    new THREE.Vector3(
      base.x + part.p[0] + offset.x,
      base.y + part.p[1] + offset.y,
      base.z + part.p[2] + offset.z,
    ),
    _q,
    new THREE.Vector3(...part.s),
  );
}

const ALIGN = {
  x: new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 0, 1), -H),
  y: new THREE.Quaternion(),
  z: new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1, 0, 0), H),
};
const AXIS = { x: new THREE.Vector3(1, 0, 0), y: new THREE.Vector3(0, 1, 0), z: new THREE.Vector3(0, 0, 1) };
const _sq = new THREE.Quaternion();
const _at = new THREE.Vector3();
const _sc = new THREE.Vector3();
/** A spinner's disc (or a fan's small hub) at rotation `angle`. */
export function discMatrix(
  base: THREE.Vector3,
  s: Spinner,
  angle: number,
  offset: THREE.Vector3,
  out: THREE.Matrix4,
) {
  _sq.setFromAxisAngle(AXIS[s.axis], angle).multiply(ALIGN[s.axis]);
  _at.set(base.x + s.p[0] + offset.x, base.y + s.p[1] + offset.y, base.z + s.p[2] + offset.z);
  const d = s.blades ? s.radius * 0.55 : s.radius * 2;
  return out.compose(_at, _sq, _sc.set(d, s.width * (s.blades ? 2.2 : 1), d));
}
/** Blade (or the pulley's single spoke) number k at rotation `angle`. Blades are pitched like a fan's. */
export function bladeMatrix(
  base: THREE.Vector3,
  s: Spinner,
  angle: number,
  k: number,
  offset: THREE.Vector3,
  out: THREE.Matrix4,
) {
  const n = s.blades ?? 1;
  _sq.setFromAxisAngle(AXIS[s.axis], angle + (k * 2 * Math.PI) / n).multiply(ALIGN[s.axis]);
  _at.set(base.x + s.p[0] + offset.x, base.y + s.p[1] + offset.y, base.z + s.p[2] + offset.z);
  if (!s.blades) return out.compose(_at, _sq, _sc.set(s.radius * 1.9, s.width + 0.012, 0.035));
  // one blade from the hub outwards: shift half its length along the blade, pitch it 25 degrees
  const pitch = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1, 0, 0), 0.45);
  const q = _sq.clone().multiply(pitch);
  const along = new THREE.Vector3(s.radius * 0.5, 0, 0).applyQuaternion(_sq);
  return out.compose(_at.add(along), q, _sc.set(s.radius * 0.9, s.width, s.radius * 0.42));
}

/** The two straight runs of a belt between spinners a and b (unit-box matrices) and its length. */
export function beltRuns(base: THREE.Vector3, a: Spinner, b: Spinner, offset: THREE.Vector3) {
  const ca = new THREE.Vector3(...a.p).add(base).add(offset);
  const cb = new THREE.Vector3(...b.p).add(base).add(offset);
  const axis = AXIS[a.axis];
  const dir = cb.clone().sub(ca).normalize();
  const n = new THREE.Vector3().crossVectors(axis, dir).normalize();
  const runs: THREE.Matrix4[] = [];
  for (const sgn of [1, -1]) {
    const p0 = ca.clone().addScaledVector(n, sgn * a.radius);
    const p1 = cb.clone().addScaledVector(n, sgn * b.radius);
    const len = p0.distanceTo(p1);
    const x = p1.clone().sub(p0).normalize();
    const y = new THREE.Vector3().crossVectors(axis, x).normalize();
    const basis = new THREE.Matrix4().makeBasis(x, y, axis);
    const q = new THREE.Quaternion().setFromRotationMatrix(basis);
    runs.push(
      new THREE.Matrix4().compose(
        p0.add(p1).multiplyScalar(0.5),
        q,
        new THREE.Vector3(len, 0.018, a.width * 0.8),
      ),
    );
  }
  return runs;
}
