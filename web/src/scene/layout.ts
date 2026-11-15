// Where things sit on the map, and each machine as a handful of primitives.
// Shed positions come from the factories' km coordinates in sim/cluster.json (1 unit = 1 km;
// the sheds themselves are drawn far larger than to scale).
import * as THREE from "three";
import type { Factory, Machine, Shape } from "../api";

export const SHED = { w: 5.4, d: 4.6, wall: 0.55 };

export const world = (xy: [number, number]) => new THREE.Vector3(xy[0], 0, -xy[1]);

const SLOTS: [number, number][] = [
  [-1.25, -1.0],
  [1.25, -1.0],
  [-1.25, 1.05],
  [1.25, 1.05],
];

export function machinePositions(factories: Factory[], machines: Machine[]) {
  const pos: Record<string, THREE.Vector3> = {};
  for (const f of factories) {
    const c = world(f.xy_km);
    machines
      .filter((m) => m.factory === f.id)
      .forEach((m, i) => {
        const [x, z] = SLOTS[i % 4];
        pos[m.id] = new THREE.Vector3(c.x + x, 0.12, c.z + z);
      });
  }
  return pos;
}

export function clusterCenter(factories: Factory[]) {
  const v = new THREE.Vector3();
  factories.forEach((f) => v.add(world(f.xy_km)));
  return v.divideScalar(Math.max(1, factories.length));
}

export type Geo = "box" | "cyl" | "cone";
export interface Part {
  g: Geo;
  p: [number, number, number];
  s: [number, number, number];
  r?: [number, number, number];
  c: 0 | 1 | 2;
}

const H = Math.PI / 2;
// Each shape fits in about 1.4 x 1.0 x 1.2 units, origin on the floor.
export const SHAPES: Record<Shape, Part[]> = {
  pump: [
    { g: "box", p: [0, 0.05, 0], s: [1.3, 0.1, 0.6], c: 2 },
    { g: "cyl", p: [-0.3, 0.32, 0], s: [0.42, 0.55, 0.42], r: [0, 0, H], c: 0 }, // motor
    { g: "cyl", p: [-0.3, 0.32, 0], s: [0.46, 0.06, 0.46], r: [0, 0, H], c: 1 }, // motor end ring
    { g: "cyl", p: [0.32, 0.32, 0], s: [0.5, 0.32, 0.5], r: [H, 0, 0], c: 1 }, // volute
    { g: "cyl", p: [0.32, 0.75, 0], s: [0.14, 0.6, 0.14], c: 0 }, // discharge pipe
    { g: "cyl", p: [0.62, 0.32, 0], s: [0.14, 0.4, 0.14], r: [0, 0, H], c: 0 }, // suction pipe
  ],
  motor: [
    { g: "box", p: [0, 0.05, 0], s: [1.2, 0.1, 0.7], c: 2 },
    { g: "cyl", p: [-0.32, 0.3, 0], s: [0.4, 0.5, 0.4], r: [0, 0, H], c: 0 }, // motor
    { g: "cyl", p: [0.25, 0.45, 0], s: [0.78, 0.32, 0.78], r: [H, 0, 0], c: 1 }, // fan housing
    { g: "box", p: [0.25, 0.92, 0], s: [0.3, 0.32, 0.28], c: 0 }, // outlet
    { g: "box", p: [0.25, 0.14, 0], s: [0.5, 0.1, 0.4], c: 2 },
  ],
  loom: [
    { g: "box", p: [-0.6, 0.5, -0.35], s: [0.1, 1.0, 0.1], c: 2 },
    { g: "box", p: [0.6, 0.5, -0.35], s: [0.1, 1.0, 0.1], c: 2 },
    { g: "box", p: [-0.6, 0.5, 0.35], s: [0.1, 1.0, 0.1], c: 2 },
    { g: "box", p: [0.6, 0.5, 0.35], s: [0.1, 1.0, 0.1], c: 2 },
    { g: "box", p: [0, 1.0, -0.35], s: [1.3, 0.1, 0.12], c: 0 }, // top beam
    { g: "cyl", p: [0, 0.75, 0.3], s: [0.18, 1.15, 0.18], r: [0, 0, H], c: 1 }, // cloth roller
    { g: "cyl", p: [0, 0.35, -0.3], s: [0.24, 1.15, 0.24], r: [0, 0, H], c: 1 }, // warp beam
    { g: "box", p: [0, 0.82, 0], s: [1.1, 0.04, 0.5], c: 0 }, // reed / cloth
    { g: "box", p: [0.82, 0.35, 0], s: [0.28, 0.4, 0.4], c: 0 }, // gearbox
  ],
  mill: [
    { g: "box", p: [0, 0.25, 0], s: [0.8, 0.5, 0.8], c: 2 }, // stand
    { g: "cyl", p: [0, 0.64, 0], s: [0.78, 0.28, 0.78], c: 1 }, // stones
    { g: "cone", p: [0, 1.0, 0], s: [0.55, 0.4, 0.55], c: 0 }, // hopper
    { g: "cyl", p: [0.65, 0.3, 0.1], s: [0.32, 0.45, 0.32], r: [0, 0, H], c: 0 }, // motor
    { g: "box", p: [0.42, 0.55, 0.1], s: [0.06, 0.5, 0.12], c: 2 }, // belt
    { g: "box", p: [0, 0.38, 0.47], s: [0.24, 0.14, 0.2], c: 0 }, // spout
  ],
};

/** Mid-height of each shape, for ripples and labels. */
export const TOP: Record<Shape, number> = { pump: 1.05, motor: 1.1, loom: 1.08, mill: 1.22 };

export function partMatrix(base: THREE.Vector3, part: Part, offset = new THREE.Vector3()) {
  const q = new THREE.Quaternion().setFromEuler(new THREE.Euler(...(part.r ?? [0, 0, 0])));
  return new THREE.Matrix4().compose(
    new THREE.Vector3(
      base.x + part.p[0] + offset.x,
      base.y + part.p[1] + offset.y,
      base.z + part.p[2] + offset.z,
    ),
    q,
    new THREE.Vector3(...part.s),
  );
}
