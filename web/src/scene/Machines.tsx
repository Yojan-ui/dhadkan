// The estate as a handful of instanced meshes: machine parts (box / cylinder / cone), shed
// structure, turning pulleys and spokes, belts, and an invisible hit box per machine.
// Machines shake with the live anomaly score of the 1 s window under the play cursor; pulleys, fans,
// impellers and belts turn at the machine type's ASSUMED nameplate speed (meta.nominal_rpm), scaled down to
// read on screen. A machine the backend reports as STOPPED spins down and stays still; a lost sensor keeps
// its last known state (the label says so).
import { useEffect, useLayoutEffect, useMemo, useRef } from "react";
import { useFrame } from "@react-three/fiber";
import { Select } from "@react-three/postprocessing";
import * as THREE from "three";
import { mergeGeometries } from "three/examples/jsm/utils/BufferGeometryUtils.js";
import { store, type Factory, type Machine } from "../api";
import { isRunning } from "../theme";
import { useKeyed } from "./fx";
import { occluders } from "./labels";
import {
  ACCENT,
  PAINT,
  SHAPES,
  SHED,
  beltRuns,
  bladeMatrix,
  discMatrix,
  partMatrix,
  shedCenter,
  type Geo,
  type Paint,
  type Part,
} from "./layout";

const GEOS: Record<Geo, THREE.BufferGeometry> = {
  box: new THREE.BoxGeometry(1, 1, 1),
  cyl: new THREE.CylinderGeometry(0.5, 0.5, 1, 24),
  cone: new THREE.CylinderGeometry(0.5, 0.18, 1, 20),
};
const SPIN_GEO = new THREE.CylinderGeometry(0.5, 0.5, 1, 18);
const VIS_SPEED = 0.025; // on-screen turn rate = nameplate rpm x this (1440 rpm -> 3.8 rad/s)
const RISE_S = 0.5,
  STAGGER_S = 0.12,
  RISE_DEPTH = 3;
const easeOut = (t: number) => 1 - Math.pow(1 - Math.min(1, Math.max(0, t)), 3);

interface Inst {
  mi: number; // machine index, -1 = shed structure
  roof?: boolean; // roof frame (hidden when the camera is inside a shed)
  fi: number; // factory index (for the entrance)
  part: Part;
  base: THREE.Vector3;
  color: THREE.Color;
}

const SHED_COLORS = { floor: "#2A2C2F", wall: "#3A3D41", post: "#4B4F55", truss: "#5C6168" };

function shedParts(fi: number, n: number): Inst[] {
  const c = shedCenter(fi, n);
  const { w, d, wall, roof } = SHED;
  const out: Inst[] = [];
  let overhead = false; // parts hidden with the roof when the camera is inside a bay
  const box = (
    p: [number, number, number],
    s: [number, number, number],
    col: string,
    r?: [number, number, number],
  ) =>
    out.push({
      mi: -1,
      fi,
      part: { g: "box", p, s, r, c: "iron" },
      base: c,
      color: new THREE.Color(col),
      roof: overhead || p[1] >= roof - 0.01,
    });
  box([0, 0.06, 0], [w, 0.12, d], SHED_COLORS.floor);
  box([0, 0.12 + wall / 2, -d / 2], [w, wall, 0.12], SHED_COLORS.wall);
  box([-w / 2, 0.12 + wall / 2, 0], [0.12, wall, d], SHED_COLORS.wall);
  box([w / 2, 0.12 + wall / 2, 0], [0.12, wall, d], SHED_COLORS.wall);
  box([-w * 0.33, 0.12 + wall / 2, d / 2], [w * 0.34, wall, 0.12], SHED_COLORS.wall); // front, with the door gap
  box([w * 0.33, 0.12 + wall / 2, d / 2], [w * 0.34, wall, 0.12], SHED_COLORS.wall);
  for (const x of [-w / 2, 0, w / 2])
    for (const z of [-d / 2, d / 2]) box([x, roof / 2, z], [0.13, roof, 0.13], SHED_COLORS.post);
  for (const z of [-d / 2, d / 2]) box([0, roof, z], [w + 0.1, 0.1, 0.1], SHED_COLORS.truss); // eaves
  const rise = 0.7,
    half = d / 2,
    len = Math.hypot(half, rise),
    tilt = Math.atan2(rise, half);
  box([0, roof + rise, 0], [w + 0.1, 0.1, 0.1], SHED_COLORS.truss); // ridge
  for (const x of [-w / 2, -w / 4, 0, w / 4, w / 2]) {
    box([x, roof + rise / 2, -half / 2], [0.08, 0.08, len], SHED_COLORS.truss, [tilt, 0, 0]);
    box([x, roof + rise / 2, half / 2], [0.08, 0.08, len], SHED_COLORS.truss, [-tilt, 0, 0]);
  }
  // steel: crane runway beams on the long walls, a crane bridge with its hoist, a cable tray and an
  // electrical panel on the back wall, and walkway markings on the floor (static scenery, no data)
  const rail = roof - 0.32;
  overhead = true;
  for (const z of [-d / 2 + 0.12, d / 2 - 0.12]) {
    box([0, rail, z], [w - 0.1, 0.14, 0.1], SHED_COLORS.truss);
    box([0, rail + 0.08, z], [w - 0.1, 0.02, 0.16], SHED_COLORS.truss);
  }
  const cx = w / 2 - 0.35; // crane parked at the end of the bay, out of the line of sight
  box([cx, rail + 0.13, 0], [0.12, 0.12, d - 0.2], "#5A4E2C"); // crane bridge (muted yellow)
  box([cx, rail - 0.02, 0.2], [0.22, 0.14, 0.2], "#4B4F55"); // hoist
  box([cx, rail - 0.45, 0.2], [0.012, 0.7, 0.012], "#2A2C30"); // hoist rope
  box([cx, rail - 0.82, 0.2], [0.08, 0.06, 0.05], "#4B4F55"); // hook block
  box([0, roof - 0.62, -d / 2 + 0.12], [w - 0.4, 0.04, 0.22], "#3C4046"); // cable tray
  overhead = false;
  box([-w / 2 + 0.12, 0.62, -0.2], [0.08, 0.7, 0.42], "#2F3237"); // electrical panel on the side wall
  box([-w / 2 + 0.17, 0.78, -0.2], [0.012, 0.08, 0.3], "#55595F");
  for (const z of [-0.42, 0.5]) box([0, 0.125, z], [w - 0.5, 0.004, 0.035], "#6E6233"); // walkway between the machine rows
  return out;
}

const BELT_VS = /* glsl */ `
attribute float aLen; attribute float aSpeed;
varying vec2 vUv; varying float vLen; varying float vSpeed;
void main() {
  vUv = uv; vLen = aLen; vSpeed = aSpeed;
  gl_Position = projectionMatrix * modelViewMatrix * instanceMatrix * vec4(position, 1.0);
}`;
const BELT_FS = /* glsl */ `
uniform float uTime;
varying vec2 vUv; varying float vLen; varying float vSpeed;
void main() {
  float stripe = step(0.82, fract(vUv.x * vLen * 9.0 - uTime * vSpeed * 9.0));
  gl_FragColor = vec4(mix(vec3(0.13, 0.12, 0.11), vec3(0.42, 0.40, 0.37), stripe), 1.0);
}`;

interface Props {
  machines: Machine[];
  factories: Factory[];
  pos: Record<string, THREE.Vector3>;
  rpm: Record<string, number>;
  hover: string | null;
  selected: string | null;
  reduced: boolean;
  roofs: boolean;
  onHover: (id: string | null) => void;
  onPick: (id: string) => void;
  onInspect: (id: string) => void;
}

export function Machines({
  machines,
  factories,
  pos,
  rpm,
  hover,
  selected,
  reduced,
  roofs,
  onHover,
  onPick,
  onInspect,
}: Props) {
  const box = useRef<THREE.InstancedMesh>(null);
  const cyl = useRef<THREE.InstancedMesh>(null);
  const cone = useRef<THREE.InstancedMesh>(null);
  const refs = useMemo(() => ({ box, cyl, cone }), []);
  const discs = useRef<THREE.InstancedMesh>(null);
  const spokes = useRef<THREE.InstancedMesh>(null);
  const belts = useRef<THREE.InstancedMesh>(null);
  const hit = useRef<THREE.InstancedMesh>(null);
  const live = useRef<number[]>([]);
  const born = useRef<number | null>(null);
  const settled = useRef(false);

  const fIndex = useMemo(() => Object.fromEntries(factories.map((f, i) => [f.id, i])), [factories]);
  const shapeKey = machines.map((m) => m.id + m.shape).join() + factories.map((f) => f.id).join();
  const insts = useKeyed(shapeKey, () => {
    const by: Record<Geo, Inst[]> = { box: [], cyl: [], cone: [] };
    factories.forEach((_, fi) => shedParts(fi, factories.length).forEach((p) => by.box.push(p)));
    machines.forEach((m, mi) => {
      const base = pos[m.id];
      if (!base) return;
      const fi = fIndex[m.factory] ?? 0;
      SHAPES[m.shape].parts.forEach((part) => {
        const paint = part.c === "accent" ? ACCENT[m.shape] : PAINT[part.c as Exclude<Paint, "accent">];
        by[part.g].push({ mi, fi, part, base, color: new THREE.Color(paint) });
      });
    });
    return by;
  });
  const spinners = useKeyed(shapeKey, () =>
    machines.flatMap((m, mi) =>
      pos[m.id]
        ? SHAPES[m.shape].spin.map((s) => ({ mi, fi: fIndex[m.factory] ?? 0, s, base: pos[m.id], m }))
        : [],
    ),
  );
  const bladeList = useKeyed(shapeKey, () =>
    spinners.flatMap((it, si) => Array.from({ length: it.s.blades ?? 1 }, (_, k) => ({ si, k }))),
  );
  const beltList = useKeyed(shapeKey, () =>
    machines.flatMap((m, mi) =>
      pos[m.id]
        ? SHAPES[m.shape].belts.map(([a, b]) => ({
            mi,
            fi: fIndex[m.factory] ?? 0,
            a: SHAPES[m.shape].spin[a],
            b: SHAPES[m.shape].spin[b],
            base: pos[m.id],
            m,
          }))
        : [],
    ),
  );

  const beltGeo = useMemo(() => {
    const g = new THREE.BoxGeometry(1, 1, 1);
    const n = Math.max(1, beltList.length * 2);
    g.setAttribute("aLen", new THREE.InstancedBufferAttribute(new Float32Array(n), 1));
    g.setAttribute("aSpeed", new THREE.InstancedBufferAttribute(new Float32Array(n), 1));
    return g;
  }, [beltList.length]);
  const beltMat = useMemo(
    () =>
      new THREE.ShaderMaterial({
        vertexShader: BELT_VS,
        fragmentShader: BELT_FS,
        uniforms: { uTime: { value: 0 } },
      }),
    [],
  );

  // colours: hovered / selected machine a touch lighter
  useLayoutEffect(() => {
    const lift = new THREE.Color("#ffffff");
    (Object.keys(insts) as Geo[]).forEach((g) => {
      const mesh = refs[g].current;
      if (!mesh) return;
      insts[g].forEach((it, i) => {
        const id = it.mi >= 0 ? machines[it.mi].id : null;
        const c = it.color.clone();
        if (id && (id === hover || id === selected)) c.lerp(lift, 0.18);
        mesh.setColorAt(i, c);
      });
      mesh.count = insts[g].length;
      if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
    });
    const h = hit.current;
    if (h) {
      machines.forEach((m, i) => {
        const p = pos[m.id] ?? new THREE.Vector3();
        h.setMatrixAt(
          i,
          new THREE.Matrix4().compose(
            new THREE.Vector3(p.x, p.y + 0.6, p.z),
            new THREE.Quaternion(),
            new THREE.Vector3(2.1, 1.4, 1.2),
          ),
        );
      });
      h.count = machines.length;
      h.instanceMatrix.needsUpdate = true;
      h.computeBoundingSphere();
    }
    settled.current = false;
  }, [insts, hover, selected, machines, pos, refs, roofs]);

  // the estate's meshes hide labels that sit behind them
  useEffect(() => {
    const meshes = [box.current, cyl.current, cone.current].filter(
      (m): m is THREE.InstancedMesh => m !== null,
    );
    meshes.forEach((m) => occluders.add(m));
    return () => meshes.forEach((m) => occluders.delete(m));
  }, [insts]);

  const off = useMemo(() => new THREE.Vector3(), []);
  const tmp = useMemo(() => new THREE.Matrix4(), []);
  const angle = useRef<number[]>([]);
  const speed = useRef<number[]>([]); // 0 stopped .. 1 running, eased
  useFrame(({ clock }, dt) => {
    const t = clock.elapsedTime;
    if (born.current === null) born.current = t;
    const t0 = born.current;
    const rise = (fi: number) =>
      reduced ? 0 : -RISE_DEPTH * (1 - easeOut((t - t0 - fi * STAGGER_S) / RISE_S));
    const entering = !reduced && t - t0 < RISE_S + STAGGER_S * factories.length + 0.1;
    const k = 1 - Math.exp(-dt * 10);
    const coast = 1 - Math.exp(-dt * 1.4); // a motor spins down (and up) over about two seconds
    const amp = machines.map((m, i) => {
      const run = isRunning(m) ? 1 : 0;
      speed.current[i] = (speed.current[i] ?? run) + (run - (speed.current[i] ?? run)) * coast;
      const target = store.frame[m.id]?.risk ?? 0; // live only: a stopped or silent machine has no frame
      live.current[i] = (live.current[i] ?? 0) + (target - (live.current[i] ?? 0)) * k;
      // tiny jitter when healthy, a visible shake as the 1 s score passes ~0.8 of its alarm level
      const a = 0.003 + 0.05 * Math.min(1.2, Math.max(0, live.current[i] - 0.75));
      return reduced ? 0 : a * speed.current[i];
    });
    const shake = (mi: number, fi: number) => {
      const a = amp[mi];
      const s = mi * 7.13;
      return off.set(
        a * Math.sin(t * 47 + s) * Math.sin(t * 3.1 + s),
        rise(fi) + a * 0.4 * Math.sin(t * 59 + s * 2),
        a * Math.sin(t * 53 + s * 3),
      );
    };
    (Object.keys(insts) as Geo[]).forEach((g) => {
      const mesh = refs[g].current;
      if (!mesh) return;
      insts[g].forEach((it, i) => {
        if (it.mi < 0) {
          if (settled.current && !entering) return; // sheds only move during the entrance
          off.set(0, rise(it.fi) - (it.roof && !roofs ? 50 : 0), 0); // roofs drop out of sight when zoomed in
        } else shake(it.mi, it.fi);
        mesh.setMatrixAt(i, partMatrix(it.base, it.part, off, tmp));
      });
      mesh.instanceMatrix.needsUpdate = true;
      if (!entering && !settled.current) mesh.computeBoundingSphere();
    });
    if (!entering) settled.current = true;

    const d = discs.current,
      sp = spokes.current;
    if (d && sp) {
      spinners.forEach((it, i) => {
        const w = ((rpm[it.m.type] ?? 0) / 60) * 2 * Math.PI * VIS_SPEED * it.s.ratio * speed.current[it.mi];
        angle.current[i] = ((angle.current[i] ?? 0) + (reduced ? 0 : w * dt)) % (Math.PI * 2);
        d.setMatrixAt(i, discMatrix(it.base, it.s, angle.current[i], shake(it.mi, it.fi), tmp));
      });
      bladeList.forEach(({ si, k }, j) => {
        const it = spinners[si];
        sp.setMatrixAt(j, bladeMatrix(it.base, it.s, angle.current[si], k, shake(it.mi, it.fi), tmp));
      });
      d.count = spinners.length;
      sp.count = bladeList.length;
      d.instanceMatrix.needsUpdate = sp.instanceMatrix.needsUpdate = true;
    }
    const b = belts.current;
    if (b) {
      const lenA = beltGeo.getAttribute("aLen") as THREE.InstancedBufferAttribute;
      const spdA = beltGeo.getAttribute("aSpeed") as THREE.InstancedBufferAttribute;
      const scale = new THREE.Vector3();
      beltList.forEach((it, i) => {
        const runs = beltRuns(it.base, it.a, it.b, shake(it.mi, it.fi));
        const surface =
          ((rpm[it.m.type] ?? 0) / 60) * 2 * Math.PI * VIS_SPEED * it.a.radius * speed.current[it.mi]; // belt speed on screen
        runs.forEach((mtx, j) => {
          b.setMatrixAt(i * 2 + j, mtx);
          lenA.setX(i * 2 + j, scale.setFromMatrixScale(mtx).x);
          spdA.setX(i * 2 + j, reduced ? 0 : (j ? -1 : 1) * surface);
        });
      });
      b.count = beltList.length * 2;
      b.instanceMatrix.needsUpdate = true;
      lenA.needsUpdate = spdA.needsUpdate = true;
      beltMat.uniforms.uTime.value = t;
    }
  });

  const mat = useMemo(() => new THREE.MeshStandardMaterial({ roughness: 0.55, metalness: 0.3 }), []);
  const steel = useMemo(
    () => new THREE.MeshStandardMaterial({ color: "#A7ACB2", roughness: 0.35, metalness: 0.7 }),
    [],
  );
  const spokeMat = useMemo(
    () => new THREE.MeshStandardMaterial({ color: "#3A3734", roughness: 0.6, metalness: 0.4 }),
    [],
  );
  return (
    <group>
      {(Object.keys(GEOS) as Geo[]).map((g) => (
        <instancedMesh
          key={g}
          ref={refs[g]}
          args={[GEOS[g], mat, Math.max(1, insts[g].length)]}
          castShadow
          receiveShadow
          frustumCulled={false}
        />
      ))}
      <instancedMesh
        ref={discs}
        args={[SPIN_GEO, steel, Math.max(1, spinners.length)]}
        castShadow
        frustumCulled={false}
      />
      <instancedMesh
        ref={spokes}
        args={[GEOS.box, spokeMat, Math.max(1, bladeList.length)]}
        castShadow
        frustumCulled={false}
      />
      <instancedMesh
        ref={belts}
        args={[beltGeo, beltMat, Math.max(1, beltList.length * 2)]}
        frustumCulled={false}
      />
      <instancedMesh
        ref={hit}
        args={[GEOS.box, undefined, Math.max(1, machines.length)]}
        onPointerMove={(e) => {
          e.stopPropagation();
          if (e.instanceId != null) onHover(machines[e.instanceId].id);
        }}
        onPointerOut={() => onHover(null)}
        onClick={(e) => {
          e.stopPropagation();
          if (e.instanceId != null) onPick(machines[e.instanceId].id);
        }}
        onDoubleClick={(e) => {
          e.stopPropagation();
          if (e.instanceId != null) onInspect(machines[e.instanceId].id);
        }}
      >
        <meshBasicMaterial visible={false} />
      </instancedMesh>
      <OutlineProxies machines={machines} pos={pos} />
    </group>
  );
}

/** One merged copy of each alerting machine, never drawn itself, so the outline effect can trace it. */
function OutlineProxies({ machines, pos }: { machines: Machine[]; pos: Record<string, THREE.Vector3> }) {
  const alerting = machines.filter((m) => (m.status === "alarm" || m.status === "early_match") && pos[m.id]);
  const key = alerting.map((m) => m.id).join();
  const geos = useKeyed(key, () =>
    alerting.map((m) => {
      const zero = new THREE.Vector3();
      const parts = SHAPES[m.shape].parts.map((p) =>
        GEOS[p.g].clone().applyMatrix4(partMatrix(zero, p, zero)),
      );
      return { id: m.id, geo: mergeGeometries(parts, false) };
    }),
  );
  const mat = useMemo(() => new THREE.MeshBasicMaterial({ colorWrite: false, depthWrite: false }), []);
  return (
    <Select enabled>
      {geos.map(({ id, geo }) => (
        <mesh key={id} geometry={geo} material={mat} position={pos[id].toArray()} />
      ))}
    </Select>
  );
}
